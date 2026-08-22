"""Dynamic long-running gateway heartbeat text.

This module turns the existing current-run activity snapshot into a bounded
human phase for chat heartbeats. It never relays raw activity text, tool
arguments, paths, URLs, ids, stack traces, or model text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

_MAX_STATUS_CHARS = 160
_ELAPSED_MINUTES_THRESHOLD = 2

_PHASE_TEXT = {
    "researching": "Researching now",
    "waiting_on_expert_work": "Waiting on expert work now",
    "implementing": "Implementing now",
    "testing": "Testing now",
    "verifying": "Verifying now",
    "blocked_waiting": "Blocked, waiting now",
}

_RESEARCH_TOOLS = {
    "browser",
    "browser_click",
    "browser_navigate",
    "browser_screenshot",
    "browser_search",
    "browser_type",
    "fetch",
    "grep",
    "list_files",
    "open",
    "read_file",
    "rg",
    "scrape",
    "search",
    "web",
    "web_search",
}

_IMPLEMENT_TOOLS = {
    "apply_patch",
    "edit",
    "multi_edit",
    "write",
    "write_file",
}

_TEST_TOOLS = {
    "execute_code",
    "run_tests",
    "test",
}

_WAIT_TOOLS = {
    "delegate_task",
    "delegate",
    "subagent",
}

_VERIFY_WORDS = (
    "verify",
    "verifying",
    "validated",
    "validation",
    "lint",
    "typecheck",
)

_TEST_WORDS = (
    "pytest",
    "test",
    "tests",
    "testing",
    "unittest",
    "vitest",
)

_WAIT_WORDS = (
    "delegate",
    "subagent",
    "expert",
    "waiting on",
    "waiting for",
    "queued",
    "cooldown",
    "rate limit",
)

_BLOCKED_WORDS = (
    "blocked",
    "stalled",
    "timeout",
    "timed out",
    "retrying",
)


@dataclass(frozen=True)
class DynamicStatus:
    text: str
    semantic_key: str


@dataclass
class DynamicHeartbeatPolicy:
    """Per-run delivery policy for dynamic long-running heartbeats."""

    max_new_bubbles: int = 3
    last_semantic_key: str | None = None
    new_bubble_count: int = 0

    def should_deliver(
        self,
        status: DynamicStatus | Any | None,
        *,
        has_edit_target: bool,
    ) -> bool:
        if status is None:
            return False
        semantic_key = getattr(status, "semantic_key", None)
        if not semantic_key or semantic_key == self.last_semantic_key:
            return False
        if not has_edit_target and self.new_bubble_count >= self.max_new_bubbles:
            return False
        return True

    def record_delivery(
        self,
        status: DynamicStatus | Any,
        *,
        sent_new_bubble: bool,
    ) -> None:
        semantic_key = getattr(status, "semantic_key", None)
        if semantic_key:
            self.last_semantic_key = str(semantic_key)
        if sent_new_bubble:
            self.new_bubble_count += 1


def build_dynamic_long_running_status(
    activity: Mapping[str, Any] | None,
    *,
    elapsed_seconds: float,
) -> DynamicStatus | None:
    """Return a safe dynamic heartbeat, or ``None`` when the state is unknown."""
    phase = _classify_phase(activity)
    if phase is None:
        return None

    text = _PHASE_TEXT[phase]
    elapsed_minutes = int(max(0.0, float(elapsed_seconds)) // 60)
    if elapsed_minutes >= _ELAPSED_MINUTES_THRESHOLD:
        text = f"{text} - {elapsed_minutes} min in."
    else:
        text = f"{text}."
    if len(text) > _MAX_STATUS_CHARS:
        return None
    return DynamicStatus(text=text, semantic_key=phase)


def _classify_phase(activity: Mapping[str, Any] | None) -> str | None:
    if not isinstance(activity, Mapping):
        return None

    current_tool = _normalize_tool_names(activity.get("current_tool"))
    desc = str(
        activity.get("last_activity_description")
        or activity.get("last_activity_desc")
        or activity.get("description")
        or ""
    ).lower()

    if _contains_phrase(desc, _BLOCKED_WORDS):
        return "blocked_waiting"
    if _contains_phrase(desc, _WAIT_WORDS) or current_tool & _WAIT_TOOLS:
        return "waiting_on_expert_work"
    if _contains_phrase(desc, _TEST_WORDS) or current_tool & _TEST_TOOLS:
        return "testing"
    if _contains_phrase(desc, _VERIFY_WORDS):
        return "verifying"
    if current_tool & _IMPLEMENT_TOOLS:
        return "implementing"
    if "terminal" in current_tool and not desc:
        return "implementing"
    if "terminal" in current_tool:
        return "implementing"
    if current_tool & _RESEARCH_TOOLS:
        return "researching"
    return None


def _contains_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    for phrase in phrases:
        if re.search(rf"(?<![a-z0-9_]){re.escape(phrase)}(?![a-z0-9_])", text):
            return True
    return False


def _normalize_tool_names(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = str(value).replace(",", " ").split()
    names: set[str] = set()
    for raw in raw_items:
        name = raw.strip().lower()
        if not name:
            continue
        names.add(name)
        if "__" in name:
            names.add(name.rsplit("__", 1)[-1])
    return names
