"""A failed text approval prompt must not leave an invisible human wait."""
import concurrent.futures
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.config import Platform
from tools import approval


class TextAdapter:
    typed_command_prefix = "/"

    def pause_typing_for_chat(self, chat_id):
        pass

    async def send(self, chat_id, content, metadata=None):
        raise AssertionError("test transport must not execute")


def runner_for(future):
    ctx = SimpleNamespace(
        _status_adapter=TextAdapter(), _status_chat_id="owner", session_key="test-thread",
        source=SimpleNamespace(platform=Platform.TELEGRAM, chat_type="dm", chat_id="owner"),
        _status_thread_metadata={"thread_id": "side-thread"},
    )
    runner = TurnRunner(None, ctx)
    runner._close_native_stream_boundary = Mock()

    def schedule(coro, *args, **kwargs):
        coro.close()
        return future

    runner._schedule = Mock(side_effect=schedule)
    return runner


@pytest.mark.parametrize("kind", ["refused", "exception", "unscheduled"])
def test_definitive_text_prompt_failure_withdraws_wait(monkeypatch, kind):
    future = concurrent.futures.Future()
    if kind == "refused":
        future.set_result(SendResult(success=False, error="target refused", retryable=False))
    elif kind == "exception":
        future.set_exception(RuntimeError("transport unavailable"))
    else:
        future = None
    runner = runner_for(future)
    monkeypatch.setattr(approval, "_gateway_queues", {})
    monkeypatch.setattr(approval, "_gateway_prompt_index", {})
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 0)
    data = {"command": "echo test", "pattern_key": "echo test"}
    result = approval._await_gateway_decision("test-thread", runner._approval_notify_sync, data)
    assert result.get("notify_failed") is True
    assert not approval.list_gateway_approvals("test-thread")
    assert runner._schedule.call_count == 1


@pytest.mark.parametrize("outcome", ["sent", "refused", "exception"])
def test_ambiguous_text_prompt_settles_only_its_original_request(monkeypatch, outcome):
    from tools.approval_gateway_wait import _ApprovalEntry

    class PendingSend(concurrent.futures.Future):
        def result(self, timeout=None):
            if not self.done():
                raise concurrent.futures.TimeoutError()
            return super().result(timeout)

    future = PendingSend()
    runner = runner_for(future)
    original = _ApprovalEntry({"command": "echo test"})
    other = _ApprovalEntry({"command": "echo other"})
    monkeypatch.setattr(approval, "_gateway_queues", {"test-thread": [original, other]})
    monkeypatch.setattr(approval, "_gateway_prompt_index", {})
    runner._approval_notify_sync(original.data)
    assert approval._gateway_queues["test-thread"] == [original, other]
    assert not original.event.is_set()
    assert original.settle is None
    assert runner._schedule.call_count == 1
    if outcome == "exception":
        future.set_exception(RuntimeError("send failed"))
    else:
        future.set_result(SendResult(success=outcome == "sent", message_id="late-prompt"))
    if outcome == "sent":
        assert original.settle is not None
        assert approval.resolve_gateway_approval_by_prompt(
            platform=Platform.TELEGRAM, chat_id="owner", prompt_message_id="late-prompt", choice="once",
        ) == 1
        assert original.result == "once"
    else:
        assert original.cancelled == "approval prompt was not delivered"
        assert original.result is None
    assert original.event.is_set()
    assert approval._gateway_queues["test-thread"] == [other]
    assert not other.event.is_set()
    assert runner._schedule.call_count == 1
