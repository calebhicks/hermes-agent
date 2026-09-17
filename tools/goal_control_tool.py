"""Model-facing current-session goal controls."""

from __future__ import annotations

import json
import os
from typing import Any

from tools.registry import registry

_VALID_ACTIONS = {"status", "create", "pause", "resume", "clear"}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _goal_budget_limit() -> int:
    try:
        from hermes_cli.config import load_config

        cfg = (load_config() or {}).get("goals") or {}
        return max(1, int(cfg.get("max_turns", 20) or 20))
    except Exception:
        return 20


def _current_session_id() -> str:
    """Trusted current-session id, never a model-supplied target."""
    try:
        from gateway.session_context import get_session_env, session_context_engaged

        session_id = str(get_session_env("HERMES_SESSION_ID", "") or "").strip()
        if session_context_engaged():
            return session_id
    except Exception:
        session_id = ""
    if not session_id:
        session_id = str(os.environ.get("HERMES_SESSION_ID") or "").strip()
    return session_id


def _trusted_session_id(dispatch_session_id: str | None) -> tuple[str | None, str | None]:
    """Resolve the live session from runtime context; fail closed on mismatch/missing proof."""
    supplied = str(dispatch_session_id or "").strip()
    try:
        from agent.delegation_context import is_delegated_child_context

        if is_delegated_child_context():
            return None, "goal control is unavailable inside delegated child agents"
    except Exception:
        pass

    try:
        from agent.relay_runtime import active_turn

        turn = active_turn(supplied or None)
        if turn is not None:
            lease = turn.lease
            if lease.parent_session_id and lease.parent_session_id != lease.session_id:
                return None, "goal control is unavailable inside delegated child agents"
            return lease.session_id, None
    except Exception:
        pass

    current = _current_session_id()
    if supplied and current and supplied != current:
        return None, "current session identity mismatch"
    session_id = supplied or current
    if not session_id:
        return None, "goal control requires a trusted current session"

    try:
        from gateway.session_context import async_delivery_supported

        if not async_delivery_supported():
            return None, "goal control requires a continuable session"
    except Exception:
        pass
    if os.environ.get("HERMES_KANBAN_TASK"):
        return None, "goal control is unavailable in kanban worker sessions"
    return session_id, None


def _snapshot(manager) -> dict[str, Any]:
    state = manager.state
    if state is None:
        return {
            "status": "none",
            "goal": None,
            "turns_used": 0,
            "max_turns": _goal_budget_limit(),
            "paused_reason": None,
            "last_verdict": None,
            "last_reason": None,
            "waiting": False,
        }
    return {
        "status": state.status,
        "goal": state.goal,
        "turns_used": int(state.turns_used),
        "max_turns": int(state.max_turns),
        "paused_reason": state.paused_reason,
        "last_verdict": state.last_verdict,
        "last_reason": state.last_reason,
        "waiting": bool(state.waiting_on_pid or state.waiting_on_session or state.waiting_until),
    }


def _parse_action(args: dict[str, Any]) -> tuple[str | None, str | None]:
    action = args.get("action")
    if not isinstance(action, str) or not action.strip():
        return None, "action is required"
    action = action.strip().lower()
    if action not in _VALID_ACTIONS:
        return None, f"unknown action: {action}"
    return action, None


def _parse_create_args(args: dict[str, Any], budget_limit: int) -> tuple[str | None, int | None, str | None]:
    goal = args.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return None, None, "goal is required for create"
    raw_budget = args.get("turn_budget")
    if type(raw_budget) is not int:
        return None, None, "turn_budget is required for create and must be an integer"
    if raw_budget < 1:
        return None, None, "turn_budget must be >= 1"
    if raw_budget > budget_limit:
        return None, None, f"turn_budget must be <= configured goals.max_turns ({budget_limit})"
    return goal.strip(), raw_budget, None


def goal_control(args: dict[str, Any], *, session_id: str | None = None, **_kwargs) -> str:
    action, action_error = _parse_action(args or {})
    if action_error:
        return _json({"success": False, "error": action_error})

    trusted_session_id, trust_error = _trusted_session_id(session_id)
    if trust_error or not trusted_session_id:
        return _json({"success": False, "action": action, "error": trust_error or "unauthorized"})

    from hermes_cli.goals import GoalManager

    budget_limit = _goal_budget_limit()
    manager = GoalManager(session_id=trusted_session_id, default_max_turns=budget_limit)

    try:
        if action == "status":
            return _json({"success": True, "action": action, "message": manager.status_line(), "goal": _snapshot(manager)})

        if action == "create":
            if manager.has_goal():
                return _json({
                    "success": False,
                    "action": action,
                    "error": "a goal already exists; pause, clear, or let the owner replace it outside the model tool",
                    "goal": _snapshot(manager),
                })
            goal, turn_budget, create_error = _parse_create_args(args or {}, budget_limit)
            if create_error:
                return _json({"success": False, "action": action, "error": create_error})
            state = manager.set(goal or "", max_turns=turn_budget)
            return _json({
                "success": True,
                "action": action,
                "message": f"Goal set with {state.max_turns}-turn budget.",
                "goal": _snapshot(manager),
            })

        if action == "pause":
            state = manager.pause(reason="model-paused")
            return _json({
                "success": bool(state),
                "action": action,
                "message": "Goal paused." if state else "No goal set.",
                "goal": _snapshot(manager),
            })

        if action == "resume":
            state = manager.state
            if state is None or state.status != "paused":
                return _json({"success": False, "action": action, "error": "no paused goal to resume", "goal": _snapshot(manager)})
            reason = str(state.paused_reason or "")
            if reason.startswith("user-"):
                return _json({
                    "success": False,
                    "action": action,
                    "error": "goal was paused by the user and cannot be resumed by the model tool",
                    "goal": _snapshot(manager),
                })
            if int(state.turns_used) >= int(state.max_turns):
                return _json({
                    "success": False,
                    "action": action,
                    "error": "goal budget is exhausted; create a new goal with an explicit turn_budget instead",
                    "goal": _snapshot(manager),
                })
            resumed = manager.resume(reset_budget=False)
            return _json({
                "success": bool(resumed),
                "action": action,
                "message": "Goal resumed without resetting the turn budget.",
                "goal": _snapshot(manager),
            })

        if action == "clear":
            had_goal = manager.has_goal()
            manager.clear()
            return _json({
                "success": True,
                "action": action,
                "message": "Goal cleared." if had_goal else "No active goal.",
                "goal": _snapshot(manager),
            })
    except (RuntimeError, ValueError, IndexError) as exc:
        return _json({"success": False, "action": action, "error": str(exc), "goal": _snapshot(manager)})

    return _json({"success": False, "action": action, "error": "unhandled action"})


registry.register(
    name="goal_control",
    toolset="goals",
    schema={
        "name": "goal_control",
        "description": (
            "Manage the current session's standing goal. Uses only the trusted live session; "
            "there is no target session parameter. Does not create shell quality gates."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(_VALID_ACTIONS),
                    "description": "Control action for the current session's standing goal.",
                },
                "goal": {
                    "type": "string",
                    "description": "Plain goal text. Required only for action=create.",
                },
                "turn_budget": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Explicit turn budget for action=create. Must be no larger than "
                        "the configured goals.max_turns."
                    ),
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    handler=goal_control,
    description="Current-session standing goal controls",
    emoji="goal",
)
