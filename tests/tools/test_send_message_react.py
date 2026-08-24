"""Tests for send_message action='react'/'unreact' dispatch.

Kept separate from ``test_send_message_tool.py`` because that module skips
wholesale when optional Telegram dependencies are not installed.
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import tools.send_message_tool as smt


class _FakePhotonAdapter:
    """Adapter exposing add_reaction/remove_reaction coroutines."""

    def __init__(self):
        self.calls = []

    async def add_reaction(self, chat_id, emoji, message_id=None):
        self.calls.append(("add", chat_id, emoji, message_id))
        return {"success": True, "emoji": emoji}

    async def remove_reaction(self, chat_id, message_id=None):
        self.calls.append(("remove", chat_id, message_id))
        return {"success": True}


class _NoReactionAdapter:
    """Adapter with no reaction support at all."""


class _FakeIMessageRepairAdapter:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _authorized(authority):
        return authority == {
            "source": "gateway_inbound",
            "platform": "imessage",
            "chat_id": "2",
            "chat_type": "dm",
            "user_id": "owner@example.test",
            "delegated": False,
            "cron": False,
        }

    async def edit_message(self, **kwargs):
        self.calls.append(("edit", kwargs))
        return (
            {"success": True, "operation": "edit"}
            if self._authorized(kwargs["authority"])
            else {"success": False, "category": "origin_refused"}
        )

    async def undo_message(self, **kwargs):
        self.calls.append(("undo_send", kwargs))
        return (
            {"success": True, "operation": "undo_send"}
            if self._authorized(kwargs["authority"])
            else {"success": False, "category": "origin_refused"}
        )


def _runner_with(adapter):
    from gateway.config import Platform

    return SimpleNamespace(adapters={Platform("photon"): adapter})


@contextmanager
def _imessage_runner(adapter):
    from gateway.config import Platform
    from gateway.platform_registry import PlatformEntry, platform_registry

    platform_registry.register(PlatformEntry(
        name="imessage",
        label="iMessage",
        adapter_factory=lambda _cfg: adapter,
        check_fn=lambda: True,
        source="builtin",
    ))
    try:
        yield SimpleNamespace(adapters={Platform("imessage"): adapter})
    finally:
        platform_registry.unregister("imessage", scope=None)


@contextmanager
def _session(**overrides):
    from gateway.session_context import clear_session_vars, set_session_vars

    values = {
        "source": "gateway_inbound",
        "platform": "imessage",
        "chat_id": "2",
        "chat_type": "dm",
        "user_id": "owner@example.test",
        "cron_session": "",
    }
    values.update(overrides)
    tokens = set_session_vars(**values)
    try:
        yield
    finally:
        clear_session_vars(tokens)


def _call(args):
    return json.loads(smt.send_message_tool(args))


def test_react_dispatches_to_add_reaction():
    adapter = _FakePhotonAdapter()
    with patch("gateway.run._gateway_runner_ref", lambda: _runner_with(adapter)):
        result = _call(
            {"action": "react", "target": "photon:+15551234567", "emoji": "❤️"}
        )
    assert result["success"] is True
    assert adapter.calls == [("add", "+15551234567", "❤️", None)]


def test_react_without_live_gateway():
    with patch("gateway.run._gateway_runner_ref", lambda: None):
        result = _call(
            {"action": "react", "target": "photon:+15551234567", "emoji": "👍"}
        )
    assert result.get("success") is not True
    assert "live" in json.dumps(result)


def test_edit_dispatches_exact_target_with_live_owner_envelope():
    adapter = _FakeIMessageRepairAdapter()
    with _imessage_runner(adapter) as runner, _session(), \
         patch("tools.send_message_tool.prepare_send_message_platforms"), \
         patch("gateway.run._gateway_runner_ref", lambda: runner):
        result = _call({
            "action": "edit",
            "target": "imessage:99",
            "message_id": "11111111-1111-4111-8111-111111111111",
            "message": "replacement",
        })
    assert result["success"] is True
    operation, kwargs = adapter.calls[0]
    assert operation == "edit"
    assert kwargs["chat_id"] == "99"
    assert kwargs["message_id"] == "11111111-1111-4111-8111-111111111111"
    assert kwargs["message"] == "replacement"


def test_undo_send_dispatches_without_replacement_text():
    adapter = _FakeIMessageRepairAdapter()
    with _imessage_runner(adapter) as runner, _session(), \
         patch("tools.send_message_tool.prepare_send_message_platforms"), \
         patch("gateway.run._gateway_runner_ref", lambda: runner):
        result = _call({
            "action": "undo_send",
            "target": "imessage:99",
            "message_id": "11111111-1111-4111-8111-111111111111",
        })
    assert result["success"] is True
    assert adapter.calls[0][0] == "undo_send"
    assert "message" not in adapter.calls[0][1]


def test_repair_requires_exact_target_message_and_edit_text():
    for args in (
        {"action": "edit", "target": "imessage:2", "message": "x"},
        {"action": "edit", "target": "imessage:2", "message_id": "id"},
        {"action": "undo_send", "target": "imessage", "message_id": "id"},
        {"action": "undo_send", "target": "slack:2", "message_id": "id"},
    ):
        assert _call(args).get("success") is not True


def test_repair_refuses_every_non_owner_origin_envelope():
    refused = (
        {"source": "gateway_internal"},
        {"source": ""},
        {"platform": "local"},
        {"chat_id": "99"},
        {"chat_type": "group"},
        {"user_id": "other@example.test"},
        {"cron_session": "1"},
    )
    for override in refused:
        adapter = _FakeIMessageRepairAdapter()
        with _imessage_runner(adapter) as runner, _session(**override), \
             patch("tools.send_message_tool.prepare_send_message_platforms"), \
             patch("gateway.run._gateway_runner_ref", lambda: runner):
            result = _call({
                "action": "edit",
                "target": "imessage:2",
                "message_id": "11111111-1111-4111-8111-111111111111",
                "message": "replacement",
            })
        assert result == {"success": False, "category": "origin_refused"}


def test_repair_refuses_delegated_child_context():
    adapter = _FakeIMessageRepairAdapter()
    with _imessage_runner(adapter) as runner, _session(), \
         patch("tools.send_message_tool.prepare_send_message_platforms"), \
         patch("gateway.run._gateway_runner_ref", lambda: runner), \
         patch("agent.delegation_context.is_delegated_child_context", return_value=True):
        result = _call({
            "action": "undo_send",
            "target": "imessage:2",
            "message_id": "11111111-1111-4111-8111-111111111111",
        })
    assert result == {"success": False, "category": "origin_refused"}


def test_repair_timeout_is_structured_uncertain_and_not_retryable():
    adapter = _FakeIMessageRepairAdapter()
    async def timeout(awaitable, **_kwargs):
        awaitable.close()
        raise TimeoutError

    with _imessage_runner(adapter) as runner, _session(), \
         patch("tools.send_message_tool.prepare_send_message_platforms"), \
         patch("gateway.run._gateway_runner_ref", lambda: runner), \
         patch("tools.send_message_tool.asyncio.wait_for", side_effect=timeout):
        result = _call({
            "action": "edit",
            "target": "imessage:2",
            "message_id": "11111111-1111-4111-8111-111111111111",
            "message": "replacement",
        })
    assert result["delivery"] == "mutation_uncertain"
    assert result["category"] == "provider_timeout"
