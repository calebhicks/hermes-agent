"""Real registry dispatch enforces lane restrictions even with a Relay lease."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent import relay_runtime
from agent.delegation_context import delegated_child_context
from gateway import session_context
from hermes_cli import goals
from tools import goal_control_tool  # noqa: F401
from tools.registry import registry


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    (tmp_path / "config.yaml").write_text("goals:\n  max_turns: 3\n")
    goals._DB_CACHE.clear()
    yield tmp_path
    session_context.clear_session_vars([])
    goals._DB_CACHE.clear()


@contextmanager
def turn(*, relay, continuable=True, parent=""):
    tokens = session_context.set_session_vars(
        platform="imessage", source="imessage", chat_type="dm",
        session_id="current-probe", async_delivery=continuable,
    )
    lease = relay_runtime.ConversationLease(
        profile_key=relay_runtime.current_profile_key(), session_id="current-probe",
        platform="imessage", host=SimpleNamespace(), session=None,
        parent_session_id=parent,
    )
    value = relay_runtime.RelayTurnContext(lease=lease, turn_id="probe", task_id="probe")
    token = relay_runtime._CURRENT_TURN.set(value if relay else None)
    try:
        assert bool(relay_runtime.active_turn("current-probe")) == relay
        yield
    finally:
        relay_runtime._CURRENT_TURN.reset(token)
        session_context.clear_session_vars(tokens)


def call(action, session_id="current-probe", **args):
    return json.loads(registry.dispatch("goal_control", {"action": action, **args}, session_id=session_id))


@pytest.mark.parametrize("relay", [False, True])
@pytest.mark.parametrize("lane", ["worker", "noncontinuable", "delegated", "wrong-session", "parent", "delegation-error", "continuation-error"])
def test_unauthorized_lanes_refused_before_persistence(home, monkeypatch, relay, lane):
    if lane == "parent" and not relay:
        pytest.skip("A parent lease exists only on the Relay path")
    def broken():
        raise RuntimeError("context unavailable")
    if lane == "delegation-error":
        monkeypatch.setattr("agent.delegation_context.is_delegated_child_context", broken)
    if lane == "continuation-error":
        monkeypatch.setattr(session_context, "async_delivery_supported", broken)
    if lane == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", "worker-probe")
    with turn(relay=relay, continuable=lane != "noncontinuable",
              parent="parent-probe" if lane == "parent" else ""):
        if lane == "delegated":
            with delegated_child_context("child-probe"):
                result = call("create", session_id="child-probe", goal="refused", turn_budget=1)
        else:
            result = call("create", session_id="other-probe" if lane == "wrong-session" else "current-probe",
                          goal="refused", turn_budget=1)
    assert not result["success"]
    expected = {"worker": "kanban worker", "noncontinuable": "continuable",
                "delegated": "delegated child", "wrong-session": "mismatch", "parent": "delegated child", "delegation-error": "could not be verified",
                "continuation-error": "could not be verified"}
    assert expected[lane] in result["error"]
    assert not (home / "state.db").exists()


@pytest.mark.parametrize("relay", [False, True])
def test_real_current_session_lifecycle_and_user_pause(home, relay):
    with turn(relay=relay):
        assert call("status")["goal"]["status"] == "none"
        assert call("create", goal="bounded probe", turn_budget=2)["success"]
        assert goals.load_goal("current-probe").goal == "bounded probe"
        assert goals.load_goal("other-probe") is None
        assert not call("create", goal="replace", turn_budget=2)["success"]
        assert call("pause")["success"]
        resumed = call("resume")
        assert resumed["success"] and resumed["goal"]["turns_used"] == 0
        assert call("clear")["success"]
        assert call("status")["goal"]["status"] == "cleared"
        assert not call("create", goal="over budget", turn_budget=4)["success"]
        assert call("create", goal="owner pause must hold", turn_budget=2)["success"]
        goals.GoalManager(session_id="current-probe").pause(reason="user-paused")
        assert not call("pause")["success"]
        assert not call("resume")["success"]
        assert not call("clear")["success"]
        assert not call("create", goal="replace user pause", turn_budget=2)["success"]
        assert goals.load_goal("current-probe").paused_reason == "user-paused"

    schema = registry.get_schema("goal_control")["parameters"]
    assert set(schema["properties"]) == {"action", "goal", "turn_budget"}
    assert schema["additionalProperties"] is False
