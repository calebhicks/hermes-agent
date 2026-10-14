"""Native heartbeat modes remain private after retiring dynamic narration."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,enabled", [("generic", True), (True, True), ("dynamic", False), (False, False)])
async def test_native_heartbeat_modes_never_expose_activity(monkeypatch, mode, enabled):
    import gateway.run as run_mod
    import gateway.run_turn as turn_mod

    monkeypatch.setattr(run_mod, "_load_gateway_config", lambda: {
        "display": {"long_running_notifications": mode},
    })
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "1")
    monkeypatch.setattr(turn_mod.asyncio, "sleep", AsyncMock())
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="heartbeat")),
        edit_message=AsyncMock(return_value=SimpleNamespace(success=True)),
    )
    runner._delivery_adapter_for = lambda source: adapter
    runner._resolve_turn_toolsets = lambda *args: ([], [])
    runner._should_emit_long_running_notification = MagicMock(side_effect=[True, True, True, False])
    runner._agent_activity_summary = MagicMock(side_effect=AssertionError("raw activity must not be read"))
    source = SimpleNamespace(platform=Platform.SLACK, chat_id="chat")
    disp = runner._run_agent_display_settings(source)
    disp._generic_status_phrase = lambda kind: "Still working."
    turn_ctx = SimpleNamespace(
        source=source, session_key="session", agent_holder=[object()],
        _status_thread_metadata={"thread_id": "thread"},
        _cleanup_progress=True, _cleanup_msg_ids=[],
    )
    await runner._run_agent_notify_long_running(disp, turn_ctx, [None])
    runner._agent_activity_summary.assert_not_called()
    if enabled:
        adapter.send.assert_awaited_once()
        assert adapter.send.call_args.args == ("chat", "Still working.")
        assert adapter.send.call_args.kwargs["metadata"]["thread_id"] == "thread"
        adapter.edit_message.assert_awaited_once_with("chat", "heartbeat", "Still working.")
        assert turn_ctx._cleanup_msg_ids == ["heartbeat"]
    else:
        adapter.send.assert_not_awaited()
        adapter.edit_message.assert_not_awaited()
