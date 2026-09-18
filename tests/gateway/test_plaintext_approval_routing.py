"""Tests for #46866: plain-text approval responses must resolve a blocking
dangerous-command approval instead of being steered/queued.

When the agent is blocked inside tools/approval.py waiting for a dangerous
command to be approved, a messaging user who replies "yes" / "approve" /
"deny" (without the leading slash) must have that response routed to the
approval handler.  Previously the bare-word reply fell through to the
steer/queue/interrupt logic in _handle_active_session_busy_message — the
approval never resolved, timed out, and auto-denied.

Slash forms (/approve, /deny) already bypass at the base-adapter guard;
this covers the bare-word forms Signal/SMS users naturally type.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_source_for(*, platform=Platform.BLUEBUBBLES, user_id="u1", chat_id="c1", thread_id=None) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
        thread_id=thread_id,
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
    )


def _make_reply_event(text: str, *, source: SessionSource, reply_to_message_id: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="reply-msg",
        reply_to_message_id=reply_to_message_id,
        reply_to_is_own_message=True,
    )


def _clear_approval_state():
    from tools import approval as mod
    mod._gateway_queues.clear()
    if hasattr(mod, "_gateway_prompt_index"):
        mod._gateway_prompt_index.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    mod._permanent_approved.clear()
    mod._pending.clear()


def _make_runner():
    """Minimal GatewayRunner that exercises the real busy-session handler."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="***"),
            Platform.BLUEBUBBLES: PlatformConfig(enabled=True, token="***"),
        }
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._send_with_retry = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="reply1")
    )
    # _unwrap_ephemeral is a real base-adapter method; emulate its contract.
    adapter._unwrap_ephemeral = lambda r: (r, 0) if isinstance(r, str) else (None, 0)
    runner.adapters = {Platform.TELEGRAM: adapter, Platform.BLUEBUBBLES: adapter}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner.session_store = None
    runner._is_user_authorized = lambda _source: True
    # _handle_active_session_busy_message uses these only on the
    # non-approval fall-through path; harmless to stub.
    runner._busy_input_mode = "interrupt"
    runner._busy_text_mode = "interrupt"
    return runner, adapter


def _register_blocking_approval(runner):
    """Register a real blocking approval entry for the runner's session."""
    from tools.approval import _ApprovalEntry, _gateway_queues
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    entry = _ApprovalEntry({"command": "rm -rf /tmp/test"})
    _gateway_queues.setdefault(session_key, []).append(entry)
    return session_key, entry


def _register_blocking_approval_for(runner, source: SessionSource, *, command: str):
    from tools.approval import _ApprovalEntry, _gateway_queues

    session_key = runner._session_key_for_source(source)
    entry = _ApprovalEntry({"command": command})
    _gateway_queues.setdefault(session_key, []).append(entry)
    return session_key, entry


@pytest.mark.parametrize("reply", ["yes", "approve", "ok", "y", "confirm"])
def test_plaintext_yes_resolves_approval(reply):
    _clear_approval_state()
    runner, adapter = _make_runner()
    session_key, entry = _register_blocking_approval(runner)

    handled = asyncio.run(
        runner._handle_active_session_busy_message(_make_event(reply), session_key)
    )

    assert handled is True
    assert entry.event.is_set()
    assert entry.result == "once"
    # The user gets a confirmation reply, not silence.
    adapter._send_with_retry.assert_awaited()
    _clear_approval_state()


def test_no_pending_approval_does_not_consume_conversational_yes():
    """A bare 'yes' with NO blocking approval must NOT be treated as an
    approval — it falls through to normal busy handling (design intent:
    'yes' in conversation must not execute a dangerous command)."""
    _clear_approval_state()
    runner, adapter = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    # No approval registered.

    handled = asyncio.run(
        runner._handle_active_session_busy_message(_make_event("yes"), session_key)
    )

    # No approval existed, so nothing was resolved — the "yes" is treated
    # as ordinary text, not as a dangerous-command approval (design intent).
    # (It still flows through normal busy handling, which may send a busy
    # ack; the contract here is only that no approval was consumed.)
    from tools.approval import _gateway_queues
    assert session_key not in _gateway_queues
    _clear_approval_state()


@pytest.mark.parametrize("reply", ["yes", "approve", "👍"])
def test_exact_prompt_reply_resolves_originating_approval_across_sibling_session(reply):
    _clear_approval_state()
    runner, adapter = _make_runner()
    origin = _make_source_for(thread_id="origin")
    sibling = _make_source_for(thread_id="sibling")
    origin_key, origin_entry = _register_blocking_approval_for(
        runner, origin, command="rm -rf /tmp/origin"
    )
    sibling_key, sibling_entry = _register_blocking_approval_for(
        runner, sibling, command="rm -rf /tmp/sibling"
    )

    from tools.approval import bind_gateway_approval_prompt

    assert bind_gateway_approval_prompt(
        session_key=origin_key,
        request_id=origin_entry.data["request_id"],
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-origin",
    )

    handled = asyncio.run(
        runner._handle_active_session_busy_message(
            _make_reply_event(reply, source=sibling, reply_to_message_id="prompt-origin"),
            sibling_key,
        )
    )

    assert handled is True
    assert origin_entry.event.is_set()
    assert origin_entry.result == "once"
    assert not sibling_entry.event.is_set()
    assert sibling_entry.result is None
    adapter._send_with_retry.assert_awaited()
    _clear_approval_state()


def test_exact_prompt_reply_consumes_once_and_duplicate_resolves_nothing():
    _clear_approval_state()
    runner, _adapter = _make_runner()
    origin = _make_source_for(thread_id="origin")
    sibling = _make_source_for(thread_id="sibling")
    origin_key, origin_entry = _register_blocking_approval_for(
        runner, origin, command="rm -rf /tmp/origin"
    )
    sibling_key, _sibling_entry = _register_blocking_approval_for(
        runner, sibling, command="rm -rf /tmp/sibling"
    )

    from tools.approval import bind_gateway_approval_prompt, resolve_gateway_approval_by_prompt

    bind_gateway_approval_prompt(
        session_key=origin_key,
        request_id=origin_entry.data["request_id"],
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-origin",
    )

    assert resolve_gateway_approval_by_prompt(
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-origin",
        choice="once",
    ) == 1
    assert resolve_gateway_approval_by_prompt(
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-origin",
        choice="once",
    ) == 0
    assert origin_entry.result == "once"
    from tools.approval import _gateway_queues

    assert sibling_key in _gateway_queues
    _clear_approval_state()


def test_exact_prompt_reply_resolves_bound_request_id_not_origin_fifo():
    _clear_approval_state()
    runner, _adapter = _make_runner()
    origin = _make_source_for(thread_id="origin")
    origin_key, first_entry = _register_blocking_approval_for(
        runner, origin, command="rm -rf /tmp/first"
    )
    _same_key, second_entry = _register_blocking_approval_for(
        runner, origin, command="rm -rf /tmp/second"
    )

    from tools.approval import bind_gateway_approval_prompt, resolve_gateway_approval_by_prompt

    bind_gateway_approval_prompt(
        session_key=origin_key,
        request_id=second_entry.data["request_id"],
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-second",
    )

    assert resolve_gateway_approval_by_prompt(
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-second",
        choice="once",
    ) == 1
    assert not first_entry.event.is_set()
    assert first_entry.result is None
    assert second_entry.event.is_set()
    assert second_entry.result == "once"
    _clear_approval_state()


def test_exact_prompt_reply_stale_binding_resolves_nothing():
    _clear_approval_state()
    runner, _adapter = _make_runner()
    origin = _make_source_for(thread_id="origin")
    origin_key, origin_entry = _register_blocking_approval_for(
        runner, origin, command="rm -rf /tmp/origin"
    )

    from tools.approval import (
        bind_gateway_approval_prompt,
        resolve_gateway_approval,
        resolve_gateway_approval_by_prompt,
    )

    bind_gateway_approval_prompt(
        session_key=origin_key,
        request_id=origin_entry.data["request_id"],
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-origin",
    )
    assert resolve_gateway_approval(origin_key, "deny", request_id="different-request") == 0
    assert resolve_gateway_approval(origin_key, "deny", request_id=origin_entry.data["request_id"]) == 1

    assert resolve_gateway_approval_by_prompt(
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-origin",
        choice="once",
    ) == 0
    assert origin_entry.result == "deny"
    _clear_approval_state()


@pytest.mark.parametrize(
    ("platform", "chat_id", "prompt_message_id"),
    [
        ("telegram", "c1", "prompt-origin"),
        ("bluebubbles", "other-chat", "prompt-origin"),
        ("bluebubbles", "c1", "other-prompt"),
    ],
)
def test_exact_prompt_reply_wrong_platform_chat_or_prompt_resolves_nothing(
    platform, chat_id, prompt_message_id
):
    _clear_approval_state()
    runner, _adapter = _make_runner()
    origin = _make_source_for(thread_id="origin")
    origin_key, origin_entry = _register_blocking_approval_for(
        runner, origin, command="rm -rf /tmp/origin"
    )

    from tools.approval import bind_gateway_approval_prompt, resolve_gateway_approval_by_prompt

    bind_gateway_approval_prompt(
        session_key=origin_key,
        request_id=origin_entry.data["request_id"],
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-origin",
    )

    assert resolve_gateway_approval_by_prompt(
        platform=platform,
        chat_id=chat_id,
        prompt_message_id=prompt_message_id,
        choice="once",
    ) == 0
    assert not origin_entry.event.is_set()
    assert origin_entry.result is None
    _clear_approval_state()


def test_unanchored_approval_word_does_not_cross_sessions():
    _clear_approval_state()
    runner, _adapter = _make_runner()
    origin = _make_source_for(thread_id="origin")
    sibling = _make_source_for(thread_id="sibling")
    _origin_key, origin_entry = _register_blocking_approval_for(
        runner, origin, command="rm -rf /tmp/origin"
    )
    sibling_key = runner._session_key_for_source(sibling)

    handled = asyncio.run(
        runner._handle_active_session_busy_message(
            MessageEvent(
                text="yes",
                message_type=MessageType.TEXT,
                source=sibling,
                message_id="unanchored-yes",
            ),
            sibling_key,
        )
    )

    assert handled is True
    assert not origin_entry.event.is_set()
    assert origin_entry.result is None
    _clear_approval_state()


def test_clear_session_drops_exact_prompt_binding():
    from tools import approval as mod

    _clear_approval_state()
    runner, _adapter = _make_runner()
    origin = _make_source_for(thread_id="origin")
    origin_key, origin_entry = _register_blocking_approval_for(
        runner, origin, command="rm -rf /tmp/origin"
    )
    mod.bind_gateway_approval_prompt(
        session_key=origin_key,
        request_id=origin_entry.data["request_id"],
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-origin",
    )

    mod.clear_session(origin_key)

    assert origin_entry.event.is_set()
    assert origin_entry.result == "deny"
    _clear_approval_state()


def test_generic_thumbs_up_does_not_resolve_unanchored_approval():
    _clear_approval_state()
    runner, _adapter = _make_runner()
    source = _make_source_for()
    session_key, entry = _register_blocking_approval_for(
        runner, source, command="rm -rf /tmp/origin"
    )
    handled = asyncio.run(runner._handle_active_session_busy_message(
        _make_event("👍"), session_key,
    ))
    assert handled is True
    assert not entry.event.is_set()
    assert entry.result is None
    _clear_approval_state()


def test_native_tapback_requires_bound_owner_and_exact_target():
    _clear_approval_state()
    runner, _adapter = _make_runner()
    source = _make_source_for(user_id="owner")
    session_key, entry = _register_blocking_approval_for(
        runner, source, command="rm -rf /tmp/origin"
    )
    from tools.approval import bind_gateway_approval_prompt
    bind_gateway_approval_prompt(
        session_key=session_key, request_id=entry.data["request_id"],
        platform="bluebubbles", chat_id="c1", prompt_message_id="prompt-origin",
        owner_user_id="owner",
    )
    runner._make_adapter_auth_check = lambda _platform: (
        lambda user_id, _chat_type, _chat_id: user_id == "owner"
    )
    assert not asyncio.run(runner._handle_native_approval_tapback(
        platform="bluebubbles", chat_id="c1", target_message_id="wrong",
        owner_user_id="owner",
    ))
    assert not asyncio.run(runner._handle_native_approval_tapback(
        platform="bluebubbles", chat_id="c1", target_message_id="prompt-origin",
        owner_user_id="other",
    ))
    assert asyncio.run(runner._handle_native_approval_tapback(
        platform="bluebubbles", chat_id="c1", target_message_id="prompt-origin",
        owner_user_id="owner",
    ))
    assert entry.result == "once"
    _clear_approval_state()


def test_wrong_bound_owner_does_not_consume_native_binding():
    _clear_approval_state()
    runner, _adapter = _make_runner()
    source = _make_source_for(user_id="owner")
    session_key, entry = _register_blocking_approval_for(
        runner, source, command="rm -rf /tmp/origin"
    )
    from tools.approval import bind_gateway_approval_prompt, resolve_gateway_approval_by_prompt

    assert bind_gateway_approval_prompt(
        session_key=session_key,
        request_id=entry.data["request_id"],
        platform="bluebubbles",
        chat_id="c1",
        prompt_message_id="prompt-origin",
        owner_user_id="owner",
    )
    assert resolve_gateway_approval_by_prompt(
        platform="bluebubbles", chat_id="c1", prompt_message_id="prompt-origin",
        choice="once", owner_user_id="other", require_owner=True,
    ) == 0
    assert resolve_gateway_approval_by_prompt(
        platform="bluebubbles", chat_id="c1", prompt_message_id="prompt-origin",
        choice="once", owner_user_id="owner", require_owner=True,
    ) == 1
    _clear_approval_state()


def test_expired_prompt_binding_resolves_nothing(monkeypatch):
    _clear_approval_state()
    from tools import approval as mod

    session_key = "imessage-timeout-session"
    prompt_id = "prompt-origin"

    def notify(data):
        assert mod.bind_gateway_approval_prompt(
            session_key=session_key,
            request_id=data["request_id"],
            platform="imessage",
            chat_id="c1",
            prompt_message_id=prompt_id,
            owner_user_id="owner",
        )

    monkeypatch.setattr(mod, "_get_approval_timeout", lambda: 0)
    result = mod._await_gateway_decision(
        session_key,
        notify,
        {"command": "rm -rf /tmp/origin", "pattern_key": "test"},
    )
    assert result["resolved"] is False
    assert mod.resolve_gateway_approval_by_prompt(
        platform="imessage", chat_id="c1", prompt_message_id=prompt_id,
        choice="once", owner_user_id="owner", require_owner=True,
    ) == 0
    _clear_approval_state()


def test_prompt_resolution_race_consumes_once():
    _clear_approval_state()
    runner, _adapter = _make_runner()
    source = _make_source_for(user_id="owner")
    session_key, entry = _register_blocking_approval_for(
        runner, source, command="rm -rf /tmp/origin"
    )
    from tools.approval import bind_gateway_approval_prompt, resolve_gateway_approval_by_prompt

    bind_gateway_approval_prompt(
        session_key=session_key, request_id=entry.data["request_id"],
        platform="imessage", chat_id="c1", prompt_message_id="prompt-origin",
        owner_user_id="owner",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: resolve_gateway_approval_by_prompt(
            platform="imessage", chat_id="c1", prompt_message_id="prompt-origin",
            choice="once", owner_user_id="owner", require_owner=True,
        ), range(2)))
    assert sorted(results) == [0, 1]
    assert entry.result == "once"
    _clear_approval_state()


def test_native_tapback_auth_uses_event_platform():
    _clear_approval_state()
    runner, _adapter = _make_runner()
    source = _make_source_for(user_id="owner")
    session_key, entry = _register_blocking_approval_for(
        runner, source, command="rm -rf /tmp/origin"
    )
    from tools.approval import bind_gateway_approval_prompt

    bind_gateway_approval_prompt(
        session_key=session_key, request_id=entry.data["request_id"],
        platform="telegram", chat_id="c1", prompt_message_id="prompt-origin",
        owner_user_id="owner",
    )
    seen = []

    def auth_check(platform):
        seen.append(platform)
        return lambda user_id, _chat_type, _chat_id: user_id == "owner"

    runner._make_adapter_auth_check = auth_check
    assert asyncio.run(runner._handle_native_approval_tapback(
        platform="telegram", chat_id="c1", target_message_id="prompt-origin",
        owner_user_id="owner",
    ))
    assert seen == [Platform.TELEGRAM]
    assert entry.result == "once"
    _clear_approval_state()
