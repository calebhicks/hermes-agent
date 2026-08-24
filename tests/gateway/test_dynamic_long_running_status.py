from types import SimpleNamespace

from gateway.dynamic_status import (
    DynamicHeartbeatPolicy,
    build_dynamic_long_running_status,
)


def test_dynamic_status_maps_activity_to_human_phase_with_elapsed_minutes():
    status = build_dynamic_long_running_status(
        {
            "current_tool": "web_search",
            "last_activity_desc": "executing tool: web_search",
        },
        elapsed_seconds=18 * 60 + 42,
    )

    assert status is not None
    assert status.semantic_key == "researching"
    assert status.text == "Researching now - 18 min in."


def test_dynamic_status_sanitizes_paths_urls_ids_args_and_source_text():
    status = build_dynamic_long_running_status(
        {
            "current_tool": "terminal",
            "last_activity_desc": (
                "executing tool: terminal command='pytest "
                "/Users/caleb/project/tests/test_alert.py::test_dedupe "
                "https://example.test/auth?id=abc123 CREDENTIAL_VALUE=private-value'"
            ),
            "tool_args": {"path": "/tmp/private", "query": "quoted source evidence"},
        },
        elapsed_seconds=7 * 60,
    )

    assert status is not None
    assert status.semantic_key == "testing"
    assert len(status.text) <= 160
    forbidden = [
        "/Users",
        "/tmp",
        "https://",
        "abc123",
        "CREDENTIAL_VALUE",
        "private-value",
        "test_alert.py",
        "quoted source evidence",
        "terminal",
        "pytest",
    ]
    assert all(item not in status.text for item in forbidden)


def test_dynamic_status_unknown_or_unsafe_activity_is_silent():
    assert (
        build_dynamic_long_running_status(
            {
                "current_tool": "unknown_internal_tool",
                "last_activity_desc": "raw stack trace /Users/caleb/app.py",
            },
            elapsed_seconds=10 * 60,
        )
        is None
    )


def test_dynamic_status_does_not_classify_substring_matches():
    status = build_dynamic_long_running_status(
        {"current_tool": "web_search", "last_activity_desc": "checking latest docs"},
        elapsed_seconds=180,
    )

    assert status is not None
    assert status.semantic_key == "researching"


def test_dynamic_status_elapsed_minutes_wait_for_threshold():
    status = build_dynamic_long_running_status(
        {"current_tool": "read_file", "last_activity_desc": "executing tool: read_file"},
        elapsed_seconds=61,
    )

    assert status is not None
    assert status.text == "Researching now."


def test_dynamic_policy_suppresses_unchanged_semantic_status():
    policy = DynamicHeartbeatPolicy()
    first = build_dynamic_long_running_status(
        {"current_tool": "read_file", "last_activity_desc": "executing tool: read_file"},
        elapsed_seconds=180,
    )
    second = build_dynamic_long_running_status(
        {"current_tool": "web_search", "last_activity_desc": "executing tool: web_search"},
        elapsed_seconds=360,
    )

    assert policy.should_deliver(first, has_edit_target=False) is True
    policy.record_delivery(first, sent_new_bubble=True)
    assert policy.should_deliver(second, has_edit_target=False) is False


def test_dynamic_policy_caps_permanent_message_bubbles_at_three():
    policy = DynamicHeartbeatPolicy(max_new_bubbles=3)
    phases = [
        ("web_search", "executing tool: web_search"),
        ("terminal", "executing tool: terminal pytest"),
        ("delegate_task", "executing tool: delegate_task"),
        ("read_file", "executing tool: read_file"),
    ]
    delivered = []
    for tool, desc in phases:
        status = build_dynamic_long_running_status(
            {"current_tool": tool, "last_activity_desc": desc},
            elapsed_seconds=10 * 60,
        )
        if policy.should_deliver(status, has_edit_target=False):
            policy.record_delivery(status, sent_new_bubble=True)
            delivered.append(status.semantic_key)

    assert delivered == ["researching", "testing", "waiting_on_expert_work"]


def test_dynamic_policy_allows_edit_updates_after_three_new_bubbles():
    policy = DynamicHeartbeatPolicy(max_new_bubbles=3)
    for key in ("researching", "testing", "waiting_on_expert_work"):
        status = SimpleNamespace(semantic_key=key)
        assert policy.should_deliver(status, has_edit_target=False) is True
        policy.record_delivery(status, sent_new_bubble=True)

    verifying = SimpleNamespace(semantic_key="verifying")
    assert policy.should_deliver(verifying, has_edit_target=False) is False
    assert policy.should_deliver(verifying, has_edit_target=True) is True


def test_dynamic_policy_is_current_run_local_state():
    run_a = DynamicHeartbeatPolicy()
    run_b = DynamicHeartbeatPolicy()
    status = build_dynamic_long_running_status(
        {"current_tool": "read_file", "last_activity_desc": "executing tool: read_file"},
        elapsed_seconds=180,
    )

    assert run_a.should_deliver(status, has_edit_target=False) is True
    run_a.record_delivery(status, sent_new_bubble=True)
    assert run_a.should_deliver(status, has_edit_target=False) is False
    assert run_b.should_deliver(status, has_edit_target=False) is True


def test_long_running_guard_stops_after_completion_or_session_rebind():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    agent = object()
    other_agent = object()
    state = SimpleNamespace(turn=SimpleNamespace(agent=agent))
    runner._peek_session_state = lambda session_key: state
    running_task = SimpleNamespace(done=lambda: False)
    done_task = SimpleNamespace(done=lambda: True)

    assert runner._should_emit_long_running_notification("session", agent, running_task)
    assert not runner._should_emit_long_running_notification("session", agent, done_task)

    state.turn.agent = other_agent
    assert not runner._should_emit_long_running_notification("session", agent, running_task)
