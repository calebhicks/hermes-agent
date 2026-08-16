import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Reuse the repository's dependency bootstrap for environments without Slack SDKs.
import tests.gateway.test_slack  # noqa: F401
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.slack.adapter import SlackAdapter


class _Response:
    def __init__(self, status=200, payload=None, chunks=None):
        self.status = status
        self._body = json.dumps(payload if payload is not None else {}).encode()
        self._chunks = list(chunks) if chunks is not None else None
        self.content = self
        self.released = False

    async def read(self, _size):
        if self._chunks is not None:
            return self._chunks.pop(0) if self._chunks else b""
        if not self._body:
            return b""
        chunk, self._body = self._body, b""
        return chunk

    def release(self):
        self.released = True


class _Request:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class _Session:
    def __init__(self, response=None, side_effect=None):
        self.response = response or _Response(
            payload={"member": {"email": "user@example.com", "role": "member"}}
        )
        self.side_effect = side_effect
        self.get = MagicMock(side_effect=self._get)

    def _get(self, *_args, **_kwargs):
        if self.side_effect:
            raise self.side_effect
        return _Request(self.response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.fixture
def adapter():
    config = PlatformConfig(
        enabled=True,
        token="***",
        extra={
            "external_resource_authz": {
                "resource": "agent-draper",
                "endpoint": "https://access.example/api/admin/members",
                "token_secret": "ACCESS_CENTER_READ_TOKEN",
            }
        },
    )
    result = SlackAdapter(config)
    result._app = MagicMock()
    result._app.client = AsyncMock()
    result._get_client = MagicMock(return_value=result._app.client)
    result._app.client.users_info = AsyncMock(
        return_value={
            "user": {
                "profile": {"email": "User@Example.com", "email_verified": True}
            }
        }
    )
    return result


@pytest.mark.asyncio
async def test_external_membership_allows_verified_member_and_caches(adapter):
    session = _Session(
        _Response(payload={"member": {"email": "user@example.com", "role": "member"}})
    )
    source = SessionSource(Platform.SLACK, "D1", user_id="U1")
    with patch("plugins.platforms.slack.adapter.get_secret", return_value="read-token"), patch(
        "plugins.platforms.slack.adapter.aiohttp.ClientSession", return_value=session
    ):
        assert await adapter._authorize_external_resource(
            "U1", chat_id="D1", team_id="T1", source=source
        )
        assert await adapter._authorize_external_resource(
            "U1", chat_id="D1", team_id="T1", source=source
        )

    assert source.external_resource_authorized is True
    assert session.get.call_count == 1
    url = session.get.call_args.args[0]
    assert "slug=agent-draper" in url
    assert "email=user%40example.com" in url
    assert session.get.call_args.kwargs["allow_redirects"] is False
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer read-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "users_info",
    [
        {"user": {"profile": {"email": "user@example.com", "email_verified": False}}},
        {"user": {"profile": {"email": "", "email_verified": True}}},
        {"user": {"profile": {"email_verified": True}}},
        {"user": {"profile": {"email": "user@example.com"}}},
        None,
    ],
)
async def test_verified_workspace_email_required(adapter, users_info):
    adapter._app.client.users_info.return_value = users_info
    source = SessionSource(Platform.SLACK, "D1", user_id="U1")
    with patch("plugins.platforms.slack.adapter.get_secret", return_value="read-token"):
        assert not await adapter._authorize_external_resource(
            "U1", chat_id="D1", team_id="T1", source=source
        )
    assert source.external_resource_authorized is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"member": None},
        {"member": True},
        {"member": {"email": "user@example.com", "role": "owner"}},
        {"member": {"email": "other@example.com", "role": "member"}},
        {"member": {"email": "user@example.com", "principal": "other@example.com", "role": "member"}},
        {"members": []},
        {"active": True, "email": "user@example.com"},
    ],
)
async def test_malformed_denied_or_conflicting_access_center_response_fails_closed(adapter, payload):
    session = _Session(_Response(payload=payload))
    source = SessionSource(Platform.SLACK, "D1", user_id="U1")
    with patch("plugins.platforms.slack.adapter.get_secret", return_value="read-token"), patch(
        "plugins.platforms.slack.adapter.aiohttp.ClientSession", return_value=session
    ):
        assert not await adapter._authorize_external_resource(
            "U1", chat_id="D1", team_id="T1", source=source
        )
    assert source.external_resource_authorized is False


@pytest.mark.asyncio
async def test_timeout_network_error_redirect_and_oversized_response_fail_closed(adapter):
    for session in (
        _Session(side_effect=asyncio.TimeoutError()),
        _Session(_Response(status=302, payload={"active": True})),
        _Session(_Response(chunks=[b"{" + (b'"x":' + b'"y"' * 9000), b"}"])),
    ):
        source = SessionSource(Platform.SLACK, "D1", user_id="U1")
        with patch("plugins.platforms.slack.adapter.get_secret", return_value="read-token"), patch(
            "plugins.platforms.slack.adapter.aiohttp.ClientSession", return_value=session
        ):
            assert not await adapter._authorize_external_resource(
                "U1", chat_id="D1", team_id="T1", source=source
            )
        assert source.external_resource_authorized is False


@pytest.mark.asyncio
async def test_denials_are_not_cached_and_expired_allows_are_not_used(adapter):
    source = SessionSource(Platform.SLACK, "D1", user_id="U1")
    deny_session = _Session(_Response(payload={"member": None}))
    with patch("plugins.platforms.slack.adapter.get_secret", return_value="read-token"), patch(
        "plugins.platforms.slack.adapter.aiohttp.ClientSession", return_value=deny_session
    ):
        assert not await adapter._authorize_external_resource(
            "U1", chat_id="D1", team_id="T1", source=source
        )
        assert not await adapter._authorize_external_resource(
            "U1", chat_id="D1", team_id="T1", source=source
        )
    assert deny_session.get.call_count == 2

    adapter._external_resource_cache[("agent-draper", "user@example.com")] = 1.0
    stale_session = _Session(_Response(payload={"member": True}))
    with patch("plugins.platforms.slack.adapter.get_secret", return_value="read-token"), patch(
        "plugins.platforms.slack.adapter.aiohttp.ClientSession", return_value=stale_session
    ), patch("plugins.platforms.slack.adapter.time.monotonic", return_value=2.0):
        assert not await adapter._authorize_external_resource(
            "U1", chat_id="D1", team_id="T1", source=source
        )
    assert source.external_resource_authorized is False


def test_disabled_behavior_and_wire_marker_fail_closed():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="***"))
    source = SessionSource(
        Platform.SLACK,
        "D1",
        user_id="U1",
        external_resource_authorized=True,
    )
    assert not SessionSource.from_dict(source.to_dict()).external_resource_authorized
    assert not adapter.external_resource_authorization_required()
    assert adapter.external_resource_authorized(source)


def test_required_adapter_needs_local_marker_for_final_gate(adapter):
    source = SessionSource(Platform.SLACK, "D1", user_id="U1")
    assert not adapter.external_resource_authorized(source)
    source.external_resource_authorized = True
    assert adapter.external_resource_authorized(source)


@pytest.mark.asyncio
async def test_interactive_path_marks_source_before_gateway_auth(adapter):
    seen = {}

    class Runner:
        def _is_user_authorized(self, source):
            seen["marker"] = source.external_resource_authorized
            return True

    adapter._message_handler = Runner()._is_user_authorized

    async def external(_user_id, *, source, **_kwargs):
        source.external_resource_authorized = True
        return True

    with patch.object(adapter, "_authorize_external_resource", AsyncMock(side_effect=external)):
        assert await adapter._authorize_interactive_user(
            "U1", channel_id="D1", team_id="T1"
        )
    assert seen["marker"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "action"),
    [
        ("_handle_approval_action", {"action_id": "hermes_approve_once", "value": "sk"}),
        ("_handle_slash_confirm_action", {"action_id": "hermes_confirm_once", "value": "sk|cid"}),
        ("_handle_feedback_action", {"action_id": "hermes_feedback", "value": "good"}),
        ("_handle_clarify_action", {"action_id": "hermes_clarify_choice_0", "value": "cid|0"}),
    ],
)
async def test_builtin_block_kit_paths_require_interactive_authorization(
    adapter,
    handler_name,
    action,
):
    adapter._authorize_interactive_user = AsyncMock(return_value=False)
    body = {
        "message": {"ts": "1.1", "blocks": []},
        "channel": {"id": "D1"},
        "team": {"id": "T1"},
        "user": {"id": "U1", "name": "User"},
    }

    await getattr(adapter, handler_name)(AsyncMock(), body, action)

    adapter._authorize_interactive_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_slash_command_runs_external_auth_before_dispatch(adapter):
    class Runner:
        def _is_user_authorized(self, source):
            return source.external_resource_authorized

        async def handle(self, _event):
            return None

    adapter.set_message_handler(Runner().handle)
    handled = AsyncMock()

    async def external(_user_id, *, source, **_kwargs):
        source.external_resource_authorized = True
        return True

    with patch.object(adapter, "handle_message", handled), patch.object(
        adapter, "_authorize_external_resource", AsyncMock(side_effect=external)
    ):
        await adapter._handle_slash_command(
            {
                "command": "/help",
                "text": "",
                "user_id": "U1",
                "channel_id": "D1",
                "team_id": "T1",
            }
        )

    handled.assert_awaited_once()
    event = handled.call_args.args[0]
    assert event.source.external_resource_authorized is True


@pytest.mark.asyncio
async def test_message_path_carries_marker_to_final_source(adapter):
    seen = {}

    class Runner:
        def _is_user_authorized(self, source):
            return source.external_resource_authorized

        async def handle(self, event):
            seen["marker"] = event.source.external_resource_authorized

    adapter._bot_user_id = "U_BOT"
    adapter._team_bot_user_ids = {"T1": "U_BOT"}
    adapter._resolve_user_name = AsyncMock(return_value="User")
    adapter._resolve_channel_name = AsyncMock(return_value="Direct")
    handled = AsyncMock()
    adapter.set_message_handler(Runner().handle)

    async def external(_user_id, *, source, **_kwargs):
        source.external_resource_authorized = True
        return True

    with patch.object(adapter, "handle_message", handled), patch.object(
        adapter, "_authorize_external_resource", AsyncMock(side_effect=external)
    ):
        await adapter._handle_slack_message(
            {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "hello",
                "ts": "1.1",
                "team": "T1",
                "client_msg_id": "m1",
            }
        )

    handled.assert_awaited_once()
    event = handled.call_args.args[0]
    assert event.source.external_resource_authorized is True
