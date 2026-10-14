"""Old experimental configuration must not divert the native goal lifecycle."""
import hashlib
import json
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from hermes_cli import goals

GOAL = "Validate a synthetic public report."


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-not-a-credential")
    # Retained settings are inert, including previously enabled exact goals.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
        "auxiliary": {"goal_judge": {"timeout": 17, "jev": {
            "enabled": True, "allowed_goal_sha256": [hashlib.sha256(GOAL.encode()).hexdigest()],
        }}}})
    call = MagicMock(return_value=MagicMock(choices=[MagicMock(message=MagicMock(
        content='{"verdict":"done","reason":"native evidence"}'))]))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", call)
    def unexpected_transport(*args, **kwargs):
        pytest.fail("retired backend must not open an HTTP client")
    monkeypatch.setattr("httpx.Client", unexpected_transport)
    goals._DB_CACHE.clear()
    yield call
    goals._DB_CACHE.clear()


def test_stale_opt_in_uses_native_judge_with_full_timeout(native):
    verdict, reason, *_ = goals.judge_goal(GOAL, "Synthetic validation passed")
    assert (verdict, reason) == ("done", "native evidence")
    native.assert_called_once()
    assert native.call_args.kwargs["timeout"] == 17


def test_owned_wait_resume_and_blocked_remain_native(native):
    process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        manager = goals.GoalManager("synthetic-owned-wait", default_max_turns=5)
        manager.set(GOAL, max_turns=5)
        background = [{"pid": process.pid, "status": "running", "command": "Synthetic validation", "notify_on_complete": True}]
        native.return_value.choices[0].message.content = json.dumps({"verdict": "wait", "wait_on_pid": process.pid, "reason": "validation running"})
        first = manager.evaluate_after_turn("Synthetic validator running", background_processes=background)
        assert first["verdict"] == "wait" and not first["should_continue"]
        assert manager.state.waiting_on_pid == process.pid
        before = (native.call_count, manager.state.turns_used)
        manager.evaluate_after_turn("Still running", background_processes=background)
        assert (native.call_count, manager.state.turns_used) == before
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        native.return_value.choices[0].message.content = '{"verdict":"done","reason":"validation passed"}'
        assert manager.evaluate_after_turn("Synthetic validation passed", background_processes=[])["status"] == "done"
        assert goals.load_goal(manager.session_id).status == "done"

        blocked = goals.GoalManager("synthetic-blocked", default_max_turns=5)
        blocked.set(GOAL, max_turns=5)
        native.return_value.choices[0].message.content = '{"verdict":"blocked","reason":"Owner must choose region"}'
        result = blocked.evaluate_after_turn("No region selected; only requester can select")
        assert result["verdict"] == "blocked" and result["status"] == "paused"
        assert goals.load_goal(blocked.session_id).status == "paused"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
