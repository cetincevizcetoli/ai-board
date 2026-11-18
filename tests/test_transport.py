import multiprocessing
import threading
import time
import unittest

from ai_boardroom.api_transport import (run_request, ResponseDeadlineExceeded, ModelSkipped, RequestCancelled, RemoteCallError)


def delayed_child(connection, payload):
    time.sleep(8)
    connection.send({"ok": True, "result": {"text": "late", "actual_model": "late/model"}})
    connection.close()


def quick_child(connection, payload):
    connection.send({"ok": True, "result": {"text": "ok", "actual_model": "actual/model", "cost": 0}})
    connection.close()


def failed_child(connection, payload):
    connection.send({"ok": False, "error": {"kind": "rate_limit", "type": "RateLimitError", "http_status": 429, "retry_after": 17}})
    connection.close()


def closed_child(connection, payload):
    connection.close()


def sdk_child(connection, payload):
    import sys
    import types
    from ai_boardroom import api_transport
    def factory(**options):
        def create(**request):
            if request["model"] == "error:free":
                raise RuntimeError("429 SDK error includes dummy-not-real")
            assert request["extra_body"]["provider"]["max_price"] == {"prompt": 0, "completion": 0}
            return types.SimpleNamespace(model="actual/sdk-model", usage=types.SimpleNamespace(
                prompt_tokens=10, completion_tokens=20, cost=0), choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="SDK yanıtı"), finish_reason="stop")])
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)), close=lambda: None)
    sys.modules["openai"] = types.SimpleNamespace(OpenAI=factory)
    api_transport._child_entry(connection, payload)


class TransportTests(unittest.TestCase):
    def request(self, target, **kwargs):
        return run_request("openrouter", "fake:free", "test", "dummy-not-real",
                           _target=target, **kwargs)

    def test_result_is_received_and_child_is_joined(self):
        result = self.request(quick_child, timeout_seconds=4)
        self.assertEqual(result["text"], "ok")
        self.assertFalse(multiprocessing.active_children())

    def test_wall_deadline_kills_child_instead_of_waiting_for_late_result(self):
        start = time.monotonic()
        with self.assertRaises(ResponseDeadlineExceeded):
            self.request(delayed_child, timeout_seconds=0.3)
        self.assertLess(time.monotonic() - start, 3)
        self.assertFalse(multiprocessing.active_children())

    def test_skip_interrupts_inflight_request_and_leaves_no_child(self):
        skip = threading.Event()
        timer = threading.Timer(0.25, skip.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(ModelSkipped):
            self.request(delayed_child, timeout_seconds=5, skip_event=skip)
        self.assertFalse(multiprocessing.active_children())

    def test_stop_has_priority_over_skip(self):
        stop, skip = threading.Event(), threading.Event()
        stop.set()
        skip.set()
        with self.assertRaises(RequestCancelled):
            self.request(delayed_child, cancel_event=stop, skip_event=skip)
        self.assertFalse(multiprocessing.active_children())

    def test_stop_interrupts_an_inflight_child(self):
        stop = threading.Event()
        timer = threading.Timer(0.25, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(RequestCancelled):
            self.request(delayed_child, timeout_seconds=5, cancel_event=stop)
        self.assertFalse(multiprocessing.active_children())

    def test_repeated_timeouts_do_not_accumulate_background_children(self):
        for _ in range(3):
            with self.assertRaises(ResponseDeadlineExceeded):
                self.request(delayed_child, timeout_seconds=0.2)
            self.assertFalse(multiprocessing.active_children())

    def test_error_keeps_safe_category_http_and_retry_after(self):
        from ai_boardroom.routing import error_kind, retry_after_seconds
        with self.assertRaises(RemoteCallError) as raised:
            self.request(failed_child, timeout_seconds=4)
        self.assertEqual(error_kind(raised.exception), "rate_limit")
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(retry_after_seconds(raised.exception), 17)
        self.assertNotIn("dummy-not-real", str(raised.exception))

    def test_crashed_or_closed_child_does_not_hang_until_timeout(self):
        with self.assertRaises(RemoteCallError):
            self.request(closed_child, timeout_seconds=4)
        self.assertFalse(multiprocessing.active_children())

    def test_real_child_entry_runs_adapter_and_returns_usage_without_credentials(self):
        result = self.request(sdk_child, timeout_seconds=4)
        self.assertEqual(result["text"], "SDK yanıtı")
        self.assertEqual(result["actual_model"], "actual/sdk-model")
        self.assertEqual(result["prompt_tokens"], 10)
        self.assertNotIn("dummy-not-real", str(result))
        self.assertFalse(multiprocessing.active_children())

    def test_real_child_error_does_not_return_raw_sdk_secret(self):
        with self.assertRaises(RemoteCallError) as raised:
            run_request("openrouter", "error:free", "test", "dummy-not-real", timeout_seconds=4, _target=sdk_child)
        self.assertEqual(raised.exception.routing_kind, "rate_limit")
        self.assertNotIn("dummy-not-real", str(raised.exception))
