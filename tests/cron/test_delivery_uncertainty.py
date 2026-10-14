"""Retained no-replay boundary through native live and standalone delivery."""
from unittest.mock import AsyncMock, MagicMock, patch
from cron.scheduler import _deliver_result

class TestDeliverResultPartialSplitDelivery:
    def test_partial_split_delivery_is_not_resent_by_standalone(self):
        """A split send that failed after delivering its head must not fall through to standalone,
        which would deliver the whole payload again; the run reports the partial failure instead."""
        import asyncio
        import threading
        from gateway.config import Platform
        from gateway.platforms.base import SendResult

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        adapter = MagicMock()
        adapter.splits_long_messages = True
        adapter.send = AsyncMock(return_value=SendResult(
            success=False, error="Twilio 400: rejected",
            raw_response={"partial_overflow": True, "delivered_chunks": 2, "total_chunks": 5}))
        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
        job = {"id": "partial-job", "deliver": "origin", "origin": {"platform": "telegram", "chat_id": "123"}}
        standalone_send = AsyncMock(return_value={"success": True})
        try:
            with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
                 patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
                 patch("tools.send_message_tool._send_to_platform", new=standalone_send):
                result = _deliver_result(job, "Hello world", adapters={Platform.TELEGRAM: adapter}, loop=loop)
        finally:
            loop.call_soon_threadsafe(loop.stop)
        adapter.send.assert_awaited_once()
        standalone_send.assert_not_awaited()
        assert result and "delivered 2 of 5 chunks" in result, result

    def test_started_delivery_is_never_cancelled_or_resent(self):
        """A coroutine suspended after entering may still report cancel=True.
        Once the delivery started, cancelling can truncate the provider send and
        standalone fallback can duplicate the whole payload.
        """
        from concurrent.futures import Future
        from gateway.config import Platform

        adapter = AsyncMock()
        adapter.send.return_value = MagicMock(success=True)

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        loop = MagicMock()
        loop.is_running.return_value = True

        captured_future = Future()
        cancel_calls = []

        def suspended_cancel():
            cancel_calls.append(True)
            return True

        captured_future.cancel = suspended_cancel
        captured_future.result = MagicMock(side_effect=TimeoutError("timed out"))

        def fake_run_coro(coro, _loop):
            try:
                coro.send(None)
            except StopIteration:
                pass
            coro.close()
            return captured_future

        job = {
            "id": "midflight-job",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "123"},
        }

        standalone_send = AsyncMock(return_value={"success": True})

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro), \
             patch("tools.send_message_tool._send_to_platform", new=standalone_send):
            result = _deliver_result(
                job,
                "Hello world",
                adapters={Platform.TELEGRAM: adapter},
                loop=loop,
            )

        assert cancel_calls == [], "a started delivery must never be cancelled"
        assert result is None, f"expected assume-delivered, got error: {result!r}"
        standalone_send.assert_not_awaited()


class TestAmbiguousWriteNeverFallsBackToStandalone:
    def _run(self, side_effect):
        from concurrent.futures import Future
        from gateway.config import Platform

        adapter = AsyncMock()
        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
        loop = MagicMock()
        loop.is_running.return_value = True

        captured_future = Future()
        captured_future.cancel = MagicMock(return_value=False)
        captured_future.result = MagicMock(side_effect=side_effect)

        def fake_schedule(coro, _loop):
            coro.close()
            return captured_future

        job = {
            "id": "ambiguous-job",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "123"},
        }
        standalone_send = AsyncMock(return_value={"success": True})
        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("agent.async_utils.safe_schedule_threadsafe", side_effect=fake_schedule), \
             patch("tools.send_message_tool._send_to_platform", new=standalone_send):
            result = _deliver_result(
                job,
                "Hello world",
                adapters={Platform.TELEGRAM: adapter},
                loop=loop,
            )
        return result, standalone_send

    def test_mutation_uncertain_suppresses_the_standalone_fallback(self):
        from gateway.delivery import DeliveryResultError

        uncertain = MagicMock()
        uncertain.success = False
        uncertain.retryable = False
        uncertain.raw_response = {
            "delivery": "mutation_uncertain",
            "receipt": "imessage-0123456789abcdef",
        }
        error = DeliveryResultError(
            "iMessage fallback outcome is uncertain; do not retry",
            send_result=uncertain,
        )
        result, standalone_send = self._run(error)
        standalone_send.assert_not_awaited()
        assert result is not None and "mutation_uncertain" in result

    def test_ordinary_send_failure_still_falls_back_to_standalone(self):
        result, standalone_send = self._run(RuntimeError("telegram send failed"))
        standalone_send.assert_awaited()
        assert result is None

    def test_retryable_structured_failure_still_falls_back(self):
        from gateway.delivery import DeliveryResultError

        transient = MagicMock()
        transient.success = False
        transient.retryable = True
        transient.raw_response = {"delivery": "mutation_uncertain"}
        error = DeliveryResultError("transient", send_result=transient)
        result, standalone_send = self._run(error)
        standalone_send.assert_awaited()
        assert result is None

    def test_router_raise_site_carries_the_structured_result(self):
        from gateway.delivery import DeliveryResultError, _send_result_error

        failed = MagicMock()
        failed.success = False
        failed.error = ""
        error = DeliveryResultError("boom", send_result=failed)
        assert _send_result_error(failed) == ""
        assert error.send_result is failed
        assert isinstance(error, RuntimeError)
