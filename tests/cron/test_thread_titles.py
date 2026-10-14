"""Audience-facing titles through native cron thread creation."""
from unittest.mock import AsyncMock, MagicMock, patch

def test_open_thread_returns_id_on_thread_platform():
    """On a thread-capable adapter, _open_continuable_cron_thread returns
    the new thread id from create_handoff_thread."""
    from cron.scheduler_delivery import _open_continuable_cron_thread

    adapter = MagicMock()
    adapter.create_handoff_thread = AsyncMock(return_value="9001")

    # safe_schedule_threadsafe hands the coro to the gateway loop and
    # returns a future. Patch it to close the coro and return a ready
    # future carrying the adapter's thread id.
    def _run_now(coro, _loop):
        coro.close()
        fut = MagicMock()
        fut.result.return_value = "9001"
        return fut

    with patch("agent.async_utils.safe_schedule_threadsafe", side_effect=_run_now):
        tid = _open_continuable_cron_thread(
            {"id": "j1", "name": "Brief"}, adapter, "123", loop=MagicMock(),
        )
    assert tid == "9001"
    adapter.create_handoff_thread.assert_called_once_with("123", "Hermes — Brief")


def test_open_thread_uses_audience_facing_title_when_provided():
    """A job may keep its internal cron name out of the visible thread seed."""
    from cron.scheduler_delivery import _open_continuable_cron_thread

    adapter = MagicMock()
    adapter.create_handoff_thread = AsyncMock(return_value="9002")

    def _run_now(coro, _loop):
        coro.close()
        fut = MagicMock()
        fut.result.return_value = "9002"
        return fut

    with patch("agent.async_utils.safe_schedule_threadsafe", side_effect=_run_now):
        tid = _open_continuable_cron_thread(
            {
                "id": "j2",
                "name": "draper-brief-caleb-20260814",
                "origin": {"thread_title": "Daily GTM Brief — Aug 14"},
            },
            adapter,
            "123",
            loop=MagicMock(),
        )
    assert tid == "9002"
    adapter.create_handoff_thread.assert_called_once_with(
        "123", "Daily GTM Brief — Aug 14"
    )
