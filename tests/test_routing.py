import json
from pathlib import Path
import queue
import tempfile
import types
import sys
import unittest
from unittest.mock import patch

from test_boardroom import DiscussionWorker, RAW_CALL
from ai_boardroom.config import active_api_keys
from ai_boardroom.model_catalog import CATALOG_VERSION, cached_catalog, is_free, merged_models, normalize_catalog
from ai_boardroom.routing import RoutePolicy, PolicyError, RequestPacer, error_kind, retry_after_seconds
from ai_boardroom.session_log import build_routing_report


def catalog():
    return {"models": [{"provider": "openrouter", "id": mid, "name": mid,
                        "pricing": {"prompt": price, "completion": price}, "tier": "free" if price == "0" else "paid"}
                       for mid, price in (("a:free", "0"), ("b:free", "0"), ("c:free", "0"), ("paid", "1"))]}


class Limited(Exception):
    status_code = 429

    def __init__(self, message="Provider returned error", retry_after=None):
        super().__init__(message)
        self.response = types.SimpleNamespace(headers={"Retry-After": str(retry_after)} if retry_after is not None else {})


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = [1000.0]
        self.pacer = RequestPacer(clock=lambda: self.clock[0])
        self.cfg = {"mode": "free", "free_interval_seconds": 6, "max_retries": 0,
                    "free_fallbacks": ["b:free", "paid"], "backoff_seconds": 10,
                    "free_catalog_fallback": False, "free_retry_same_model": True}
        self.worker = self.make_worker()
        self.quota_guard = patch("ai_boardroom.routing.fetch_free_quota", return_value=None)
        self.quota_guard.start()
        self.addCleanup(self.quota_guard.stop)
        self.worker._quota_loader = lambda key: None
        self.waits = []
        def wait(seconds, reason=""):
            self.worker._check_stop()
            self.waits.append(seconds)
            self.clock[0] += seconds
        self.worker._routing_wait = wait
        self.guard = patch.object(DiscussionWorker, "_raw_call", side_effect=AssertionError("Network forbidden"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def make_worker(self):
        worker = DiscussionWorker("Test", [{"name": "Mimar", "role": "Test", "provider": "openrouter", "model": "a:free"}],
                                  1, {"openrouter": "private-test-key"}, queue.Queue(),
                                  summary_provider="openrouter", summary_model="b:free",
                                  session_path=Path(self.temp.name), routing_config=self.cfg,
                                  catalog_loader=catalog, pacer=self.pacer)
        return worker

    def test_free_layer_rejects_paid_even_with_key(self):
        with self.assertRaises(PolicyError):
            self.worker._request_with_fallback("openrouter", "paid", "prompt")

    def test_quota_zero_blocks_all_generation_and_is_saved(self):
        self.worker._quota_loader = lambda key: {"used": 52, "limit": 50, "remaining": 0}
        self.worker.run()
        self.assertEqual(self.worker.session_status, "failed")
        self.assertEqual(self.worker.api_calls, [])
        self.assertFalse(any(e["event"] == "attempt" for e in self.worker.routing_events))
        state = json.loads((Path(self.temp.name) / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual(state["free_quota"]["remaining"], 0)
        self.assertIn("kalan 0", state["last_error"])

    def test_quota_endpoint_failure_does_not_claim_zero_or_prevent_response(self):
        self.worker._quota_loader = lambda key: (_ for _ in ()).throw(RuntimeError("secret"))
        self.worker._raw_call = lambda *a, **k: "Yanıt"
        self.assertEqual(self.worker._request_with_fallback("openrouter", "a:free", "prompt")[0], "Yanıt")
        self.assertIsNone(self.worker.free_quota)

    def test_first_success_pins_fallback_and_next_round_uses_only_that_model(self):
        called = []
        def fake(prov, mid, *args, **kwargs):
            called.append(mid)
            if mid == "a:free":
                raise Limited()
            return "Görüş"
        self.worker._raw_call = fake
        self.worker.call_llm(self.worker.personas[0], [], 1)
        self.assertEqual(self.worker.role_models["Mimar"]["route_model"], "b:free")
        self.worker.call_llm(self.worker.personas[0], [], 2)
        self.assertEqual(called, ["a:free", "b:free", "b:free"])

    def test_later_roles_skip_session_failures_after_model_cooldown_ends(self):
        from ai_boardroom.api_transport import ResponseDeadlineExceeded
        worker = self.worker
        worker.routing_config["free_fallbacks"] = ["b:free", "c:free"]
        roles = [{**worker.personas[0], "name": name} for name in ("Mimar", "Eleştirmen", "Ürün")]
        called = []

        def fake(provider, model, *args, **kwargs):
            purpose = worker._request_context["purpose"]
            called.append((purpose, model))
            if model == "a:free":
                return "---META---\nGÖRÜŞ: konu | öneri | gerekçe"
            if model == "b:free" and purpose.endswith("Eleştirmen"):
                raise ResponseDeadlineExceeded()
            return "Görüş"

        worker._raw_call = fake
        for role in roles:
            worker.call_llm(role, [], 1)
            # Later participants may start after a 60-second cooldown.
            worker._model_cooldowns.clear()
        worker.call_llm(roles[0], [], 2)
        self.assertEqual(called, [
            ("Tur 1 / Mimar", "a:free"), ("Tur 1 / Mimar", "b:free"),
            ("Tur 1 / Eleştirmen", "b:free"), ("Tur 1 / Eleştirmen", "c:free"),
            ("Tur 1 / Ürün", "c:free"), ("Tur 2 / Mimar", "b:free"),
        ])
        skipped = [event for event in worker.routing_events if event.get("reason") == "session_model_failed"]
        self.assertTrue(any(event["previous_purpose"] == "Tur 1 / Mimar"
                            and event["response_detail"] == "meta_only" for event in skipped))
        self.assertTrue(any(event["previous_reason"] == "response_timeout" for event in skipped))
        report = build_routing_report(worker.snapshot())
        self.assertIn("Önceki deneme: Tur 1 / Eleştirmen / response_timeout", report)

    def test_previously_excluded_route_does_not_fill_fallback_limit(self):
        policy = RoutePolicy({**self.cfg, "free_fallbacks": ["a:free", "b:free"], "free_max_fallbacks": 1},
                             catalog_loader=catalog)
        candidates = policy.candidates("openrouter", "c:free", {"openrouter": "test-key"},
                                       excluded={("openrouter", "a:free")})
        self.assertEqual([item["model"] for item in candidates], ["c:free", "b:free"])

    def test_pinned_model_failure_never_uses_other_model(self):
        self.worker._raw_call = lambda *a, **k: "Görüş"
        self.worker.call_llm(self.worker.personas[0], [], 1)
        called = []
        def fail(prov, mid, *a, **k):
            called.append(mid)
            raise RuntimeError("failure")
        self.worker._raw_call = fail
        with self.assertRaisesRegex(Exception, "başka modele geçilmedi"):
            self.worker.call_llm(self.worker.personas[0], [], 2)
        self.assertEqual(called, ["a:free"])
        self.assertEqual(self.worker.role_models["Mimar"]["route_model"], "a:free")

    def test_pin_is_separate_for_each_role_and_not_applied_to_summary(self):
        self.worker._raw_call = lambda *a, **k: "Görüş"
        self.worker.call_llm(self.worker.personas[0], [], 1)
        other = {**self.worker.personas[0], "name": "Eleştirmen", "model": "b:free"}
        self.worker.call_llm(other, [], 1)
        self.assertEqual(self.worker.role_models["Eleştirmen"]["route_model"], "b:free")
        self.worker._request_with_fallback("openrouter", "b:free", "özet", purpose="summary")
        self.assertEqual(len(self.worker.role_models), 2)
        self.assertIn("Sabit rol", build_routing_report(self.worker.snapshot()))

    def test_provider_cannot_silently_change_actual_model_of_pinned_role(self):
        self.worker._raw_call = lambda *a, **k: "Görüş"
        self.worker.call_llm(self.worker.personas[0], [], 1)
        def different(prov, mid, *a, **k):
            self.worker._save_call_usage(prov, mid, "other-model")
            return "Başka modelin görüşü"
        self.worker._raw_call = different
        with self.assertRaisesRegex(PolicyError, "gerçek model kimliği değişti"):
            self.worker.call_llm(self.worker.personas[0], [], 2)
        self.assertEqual(self.worker.role_models["Mimar"]["actual_model"], "a:free")

    def test_quota_reader_only_returns_counters(self):
        response = types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "data": {"label": "private", "free_model_daily_requests": {"used": 2, "limit": 50, "remaining": 48, "private": "secret"}}})
        from ai_boardroom.routing import fetch_free_quota
        # The class fixture patches the default loader; test the original function.
        self.quota_guard.stop()
        from ai_boardroom.routing import fetch_free_quota
        with patch("requests.get", return_value=response) as request:
            self.assertEqual(fetch_free_quota("dummy"), {"used": 2, "limit": 50, "remaining": 48})
            self.assertEqual(request.call_args.args[0], "https://openrouter.ai/api/v1/key")

    def test_chat_catalog_excludes_audio_generator_even_when_it_returns_text(self):
        result = normalize_catalog({"data": [
            {"id": "music", "architecture": {"input_modalities": ["text"], "output_modalities": ["text", "audio"]}},
            {"id": "chat", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}}]})
        self.assertEqual([m["id"] for m in result], ["chat"])
        self.assertEqual(self.worker.api_calls, [])

    def test_free_layer_rejects_native_provider_not_known_to_be_free(self):
        self.worker.api_keys["gemini"] = "dummy"
        with self.assertRaises(PolicyError):
            self.worker._request_with_fallback("gemini", "gemini-flash", "prompt")

    def test_free_fallback_never_calls_paid_model(self):
        called = []
        def fake(prov, model, *args, **kwargs):
            called.append(model)
            if model == "a:free":
                raise Limited()
            return "cevap"
        self.worker._raw_call = fake
        text, prov, actual = self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        self.assertEqual(called, ["a:free", "b:free"])
        self.assertEqual(actual, "b:free")
        self.assertTrue(self.worker._last_response_info["fallback_used"])
        self.assertTrue(any(e["event"] == "fallback" for e in self.worker.routing_events))

    def test_retry_after_and_pacing_are_respected(self):
        self.worker.routing_config["max_retries"] = 1
        called = []
        def fake(*args, **kwargs):
            called.append(self.clock[0])
            if len(called) == 1:
                raise Limited(retry_after=35)
            return "cevap"
        self.worker._raw_call = fake
        self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        self.assertGreaterEqual(called[1] - called[0], 35)

    def test_shared_pacer_covers_retries_models_and_next_session(self):
        started = []
        self.worker._raw_call = lambda *a, **k: (started.append(self.clock[0]) or "cevap")
        self.worker._request_with_fallback("openrouter", "a:free", "one")
        self.worker._request_with_fallback("openrouter", "b:free", "two")
        other = self.make_worker()
        other._routing_wait = lambda seconds, reason="": self.clock.__setitem__(0, self.clock[0] + seconds)
        other._raw_call = self.worker._raw_call
        other._request_with_fallback("openrouter", "a:free", "three")
        self.assertEqual(started, [1000.0, 1006.0, 1012.0])

    def test_daily_account_quota_does_not_hammer_other_openrouter_models(self):
        called = []
        def fake(prov, model, *args, **kwargs):
            called.append(model)
            raise Limited("Rate limit exceeded: free-models-per-day")
        self.worker._raw_call = fake
        with self.assertRaises(RuntimeError):
            self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        self.assertEqual(called, ["a:free"])
        with self.assertRaises(RuntimeError):
            self.worker._request_with_fallback("openrouter", "b:free", "prompt")
        self.assertEqual(called, ["a:free"])

    def test_provider_specific_daily_error_is_not_account_daily_quota(self):
        self.assertEqual(error_kind(Limited("Provider returned error: daily quota exceeded")), "rate_limit")

    def test_cancel_during_backoff_prevents_fallback(self):
        self.worker.routing_config["max_retries"] = 1
        called = []
        def fake(prov, model, *args, **kwargs):
            called.append(model)
            raise Limited()
        self.worker._raw_call = fake
        def cancel(seconds, reason=""):
            self.worker.stop()
            self.worker._check_stop()
        self.worker._routing_wait = cancel
        with self.assertRaises(Exception):
            self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        self.assertEqual(called, ["a:free"])

    def test_all_alternatives_fail_still_writes_incomplete_report(self):
        self.worker._raw_call = lambda *a, **k: (_ for _ in ()).throw(Limited())
        self.worker.run()
        self.assertEqual(self.worker.session_status, "failed")
        folder = Path(self.temp.name)
        self.assertTrue((folder / "transcript.json").exists())
        self.assertTrue((folder / "oturum_raporu.md").exists())
        self.assertFalse((folder / "ozet.md").exists())
        report = (folder / "oturum_raporu.md").read_text(encoding="utf-8")
        self.assertIn("Eksik", report)
        self.assertIn("429", report)
        self.assertIn("b:free", report)
        self.assertNotIn("private-test-key", report)

    def test_fallback_keeps_role_and_records_actual_responder_in_report(self):
        def fake(prov, model, *args, **kwargs):
            if model == "a:free":
                raise Limited()
            self.worker._save_call_usage(prov, model, "backend/actual", 10, 20, 0)
            return "cevap\n---META---\nGÖRÜŞ: konu | görüş | gerekçe"
        self.worker._raw_call = fake
        self.worker.run()
        self.assertEqual(self.worker.session_status, "completed")
        entry = self.worker.transcript[0]
        self.assertEqual(entry["speaker"], "Mimar")
        self.assertEqual(entry["requested_model"], "a:free")
        self.assertEqual(entry["model"], "backend/actual")
        report = (Path(self.temp.name) / "ozet.md").read_text(encoding="utf-8")
        self.assertIn("a:free", report)
        self.assertIn("backend/actual", report)
        self.assertIn("Yedek model kullanıldı", report)

    def test_paid_mode_needs_explicit_permission(self):
        policy = RoutePolicy({"mode": "paid"})
        with self.assertRaises(PolicyError):
            policy.candidates("openai", "paid", {"openai": "key"})

    def test_free_and_paid_keys_are_separate_without_automatic_migration(self):
        cfg = {"api_keys": {"openrouter": "free-key", "openai": "legacy-key"},
               "paid_api_keys": {"openrouter": "paid-key"}, "routing": {"mode": "free"}}
        self.assertEqual(active_api_keys(cfg), {"openrouter": "free-key"})
        self.assertEqual(active_api_keys(cfg, "paid"), {"openrouter": "paid-key"})
        self.assertNotIn("openai", active_api_keys({"api_keys": {"openai": "legacy"}}, "paid"))

    def test_custom_model_cannot_override_paid_price_as_free(self):
        data = {"models": catalog()["models"]}
        entries = merged_models(data, [{"provider": "openrouter", "id": "paid", "name": "Fake free", "tier": "free"}])
        entry = next(e for e in entries if e["id"] == "paid")
        self.assertFalse(is_free(entry))
        self.assertEqual(entry["tier"], "paid")

    def test_catalog_rejects_unknown_cost_and_nontext_models(self):
        self.assertFalse(is_free({"pricing": {"prompt": "0"}}))
        models = normalize_catalog({"data": [{"id": "image", "architecture": {"output_modalities": ["image"]}},
                                            {"id": "missing-architecture"},
                                            {"id": "text", "architecture": {"output_modalities": ["text"],
                                                                              "input_modalities": ["text"]},
                                             "pricing": {"prompt": "0", "completion": "0"}}]})
        self.assertEqual([m["id"] for m in models], ["text"])

    def test_old_catalog_and_local_alias_cannot_restore_media_models(self):
        cache = Path(self.temp.name) / "openrouter_models.json"
        media = {"provider": "openrouter", "id": "media", "tier": "free"}
        cache.write_text(json.dumps({"models": [media]}), encoding="utf-8")
        with patch("ai_boardroom.model_catalog.CATALOG_FILE", cache):
            self.assertEqual(cached_catalog()["models"], [])
            chat = catalog()["models"]
            cache.write_text(json.dumps({"version": CATALOG_VERSION, "models": chat}), encoding="utf-8")
            self.assertEqual(cached_catalog()["models"], chat)
            self.assertNotIn("media", {m["id"] for m in merged_models(cached_catalog(), [media])})

    def test_paid_openrouter_models_must_be_in_chat_catalog(self):
        policy = RoutePolicy({"mode": "paid", "paid_enabled": True, "paid_fallbacks": [
            {"provider": "openrouter", "model": "media"},
            {"provider": "openrouter", "model": "paid"},
        ]}, catalog_loader=catalog)
        keys = {"openrouter": "test-key", "openai": "test-key"}
        with self.assertRaisesRegex(PolicyError, "metin sohbet"):
            policy.candidates("openrouter", "media", keys)
        candidates = policy.candidates("openai", "native-chat", keys)
        self.assertEqual([item["model"] for item in candidates], ["native-chat", "paid"])

    def test_fresh_catalog_failure_blocks_generation(self):
        self.worker.route_policy.catalog_loader = lambda: (_ for _ in ()).throw(OSError("offline"))
        with self.assertRaises(PolicyError):
            self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        self.assertFalse(self.worker.api_calls)

    def test_unknown_cost_is_not_reported_as_zero_usage(self):
        report = build_routing_report({"api_calls": [{"cost": None}], "usage_mode": "paid"})
        self.assertIn("Maliyeti bildirilmeyen çağrı: 1", report)
        self.assertIn("bildirilmedi", report)

    def test_retry_after_numeric_and_date(self):
        self.assertEqual(retry_after_seconds(Limited(retry_after=17)), 17)
        error = Limited()
        error.response.headers["Retry-After"] = "Wed, 21 Oct 2015 07:28:00 GMT"
        self.assertEqual(retry_after_seconds(error, now=1445412470), 10)

    def test_removed_model_skips_unverified_request_and_uses_verified_free(self):
        called = []
        self.worker._raw_call = lambda provider, model, *args, **kwargs: (called.append(model) or "cevap")
        self.worker._request_with_fallback("openrouter", "removed:free", "prompt")
        self.assertEqual(called, ["b:free"])
        self.assertTrue(any(e.get("reason") == "not_in_catalog" for e in self.worker.routing_events))

    def test_zero_alternatives_does_not_call_fallback_for_removed_model(self):
        self.worker.routing_config["automatic_fallback"] = False
        with self.assertRaises(RuntimeError):
            self.worker._request_with_fallback("openrouter", "removed:free", "prompt")
        self.assertFalse(self.worker.api_calls)

    def test_long_account_retry_after_does_not_hammer_other_models(self):
        self.worker.routing_config["max_retries"] = 1
        called = []
        def fake(provider, model, *args, **kwargs):
            called.append(model)
            raise Limited("free-models-per-min", retry_after=600)
        self.worker._raw_call = fake
        with self.assertRaises(RuntimeError):
            self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        self.assertEqual(called, ["a:free"])
        self.assertFalse(self.waits)

    def test_alias_resolution_is_not_mislabeled_as_failed_primary_fallback(self):
        def fake(provider, model, *args, **kwargs):
            self.worker._save_call_usage(provider, model, "canonical/model", 1, 1, 0)
            return "cevap"
        self.worker._raw_call = fake
        _, _, actual = self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        self.assertEqual(actual, "canonical/model")
        self.assertFalse(self.worker._last_response_info["fallback_used"])

    def test_free_request_sends_server_side_zero_price_ceiling(self):
        from ai_boardroom import api_transport
        self.worker._request_runner = api_transport.call_provider
        requests = []
        def factory(**options):
            def create(**request):
                requests.append(request)
                return types.SimpleNamespace(model="a:free", usage=None,
                    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")])
            return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        with patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=factory)}):
            RAW_CALL(self.worker, "openrouter", "a:free", "prompt", "dummy")
        self.assertEqual(requests[0]["extra_body"]["provider"]["max_price"], {"prompt": 0, "completion": 0})

    def test_legacy_config_load_does_not_rewrite_or_promote_keys(self):
        from ai_boardroom import config
        folder = Path(self.temp.name)
        file = folder / "config.json"
        original = json.dumps({"api_keys": {"openai": "legacy-secret", "openrouter": "free-secret"}})
        file.write_text(original, encoding="utf-8")
        with patch.multiple(config, CONFIG_DIR=folder, CONFIG_FILE=file, SESSIONS_DIR=folder / "sessions"):
            loaded = config.load_config()
        self.assertEqual(file.read_text(encoding="utf-8"), original)
        self.assertEqual(loaded["routing"]["mode"], "free")
        self.assertEqual(loaded["paid_api_keys"]["openai"], "")
        self.assertEqual(loaded["api_keys"]["openai"], "legacy-secret")

    def test_live_progress_signal_includes_model_trial_and_fallback_position(self):
        events = []
        self.worker.routing_event.connect(events.append)
        def fake(provider, model, *args, **kwargs):
            if model == "a:free":
                raise Limited()
            return "cevap"
        self.worker._raw_call = fake
        self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        attempt = next(e for e in events if e["event"] == "attempt" and e["model"] == "b:free")
        self.assertEqual(attempt["candidate_index"], 2)
        self.assertEqual(attempt["candidate_count"], 2)
        self.assertEqual(attempt["attempt"], 1)
        self.assertEqual(attempt["attempt_limit"], 1)
        with patch.object(self.worker._stop_event, "wait", return_value=False):
            DiscussionWorker._routing_wait(self.worker, 0.01)
        self.assertEqual(events[-1]["event"], "wait")
        self.assertEqual(events[-1]["model"], "b:free")

    def test_meta_only_reply_is_not_shown_as_empty_success_and_uses_fallback(self):
        def fake(provider, model, *args, **kwargs):
            return "---META---\nGÖRÜŞ: konu | görüş | neden" if model == "a:free" else "Görünür yanıt"
        self.worker._raw_call = fake
        text, _, model = self.worker._request_with_fallback("openrouter", "a:free", "prompt", system_prompt="role")
        self.assertEqual(model, "b:free")
        self.assertEqual(text, "Görünür yanıt")
        self.assertEqual(self.worker.api_errors[0]["reason"], "empty_response")
        self.assertEqual(self.worker.api_errors[0]["response_detail"], "meta_only")

    def test_legacy_zero_paid_limit_does_not_disable_free_catalog_fallback(self):
        self.worker.routing_config.update(free_catalog_fallback=True, free_max_fallbacks=0, max_fallbacks=0)
        data = catalog()
        for number in range(20):
            data["models"].append({"provider": "openrouter", "id": f"extra/{number}:free", "pricing": {"prompt": "0", "completion": "0"}})
        self.worker.route_policy.catalog_loader = lambda: data
        candidates = self.worker.route_policy.candidates("openrouter", "a:free", self.worker.api_keys)
        self.assertEqual(len(candidates), 23)
        self.assertEqual(candidates[1]["model"], "b:free")
        self.assertNotIn("paid", [entry["model"] for entry in candidates])

    def test_empty_then_429_then_catalog_success_without_repeating_failed_models(self):
        self.worker.routing_config.update(free_catalog_fallback=True, free_max_fallbacks=0,
                                          free_retry_same_model=False, max_retries=2, max_fallbacks=0)
        called = []
        def fake(provider, model, *args, **kwargs):
            called.append(model)
            if model == "a:free":
                return "---META---\nGÖRÜŞ: konu | görüş | neden"
            if model == "b:free":
                raise Limited()
            return "Üçüncü modelin görünür yanıtı"
        self.worker._raw_call = fake
        _, _, actual = self.worker._request_with_fallback("openrouter", "a:free", "prompt", system_prompt="role")
        self.assertEqual(called, ["a:free", "b:free", "c:free"])
        self.assertEqual(actual, "c:free")
        self.assertEqual([e["reason"] for e in self.worker.api_errors], ["empty_response", "rate_limit"])

    def test_free_catalog_can_be_disabled_for_explicit_priority_pool_only(self):
        self.worker.routing_config.update(free_catalog_fallback=False, free_max_fallbacks=0)
        candidates = self.worker.route_policy.candidates("openrouter", "a:free", self.worker.api_keys)
        self.assertEqual([entry["model"] for entry in candidates], ["a:free", "b:free"])

    def test_positive_free_limit_caps_alternatives_but_not_paid_layer_safety(self):
        self.worker.routing_config.update(free_catalog_fallback=True, free_max_fallbacks=1, max_fallbacks=0)
        candidates = self.worker.route_policy.candidates("openrouter", "a:free", self.worker.api_keys)
        self.assertEqual([entry["model"] for entry in candidates], ["a:free", "b:free"])
        paid = RoutePolicy({"mode": "paid", "paid_enabled": True, "max_fallbacks": 0,
                            "paid_fallbacks": [{"provider": "openai", "model": "other"}]})
        self.assertEqual(paid.candidates("openai", "primary", {"openai": "test"}), [{"provider": "openai", "model": "primary"}])

    def test_exhausted_message_reports_actual_attempt_count_and_last_reason(self):
        self.worker.routing_config.update(automatic_fallback=False)
        self.worker._raw_call = lambda *a, **k: (_ for _ in ()).throw(Limited())
        with self.assertRaises(RuntimeError) as raised:
            self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        message = str(raised.exception)
        self.assertIn("1 model denendi", message)
        self.assertIn("HTTP 429", message)
        self.assertIn("ayarlarda kapalı", message)

    def test_total_timeout_uses_next_model_without_retry_or_stale_usage(self):
        from ai_boardroom import api_transport
        self.worker.routing_config.update(response_timeout_seconds=30, max_retries=2, free_retry_same_model=True)
        calls = []
        def runner(**request):
            calls.append((request["model"], request["timeout_seconds"]))
            if request["model"] == "a:free":
                raise api_transport.ResponseDeadlineExceeded()
            return {"text": "güncel yanıt", "actual_model": "actual/b", "cost": 0}
        self.worker._request_runner = runner
        self.worker._raw_call = types.MethodType(RAW_CALL, self.worker)
        text, _, model = self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        self.assertEqual(calls, [("a:free", 30), ("b:free", 30)])
        self.assertEqual(model, "actual/b")
        self.assertEqual(len(self.worker.api_calls), 1)
        self.assertEqual(self.worker.api_errors[0]["reason"], "response_timeout")

    def test_manual_skip_is_bound_to_request_id_and_cannot_skip_next_model(self):
        from ai_boardroom import api_transport
        old = []
        def event_received(event):
            if event["event"] == "attempt" and event["model"] == "a:free":
                old.append(event["request_id"])
                self.assertTrue(self.worker.request_skip_current_model(event["request_id"]))
            elif event["event"] == "attempt" and event["model"] == "b:free":
                self.assertFalse(self.worker.request_skip_current_model(old[0]))
        self.worker.routing_event.connect(event_received)
        def runner(**request):
            if request["skip_event"].is_set():
                raise api_transport.ModelSkipped()
            return {"text": "yeni modelin yanıtı", "actual_model": "b:free"}
        self.worker._request_runner = runner
        self.worker._raw_call = types.MethodType(RAW_CALL, self.worker)
        self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        self.assertEqual(self.worker.api_errors[0]["reason"], "user_skip")
        self.assertFalse(self.worker.request_skip_current_model(old[0]))
        self.assertEqual(self.worker.api_calls[0]["actual_model"], "b:free")

    def test_cancelled_transport_stops_discussion_without_requesting_fallback(self):
        from ai_boardroom import api_transport
        from ai_boardroom.orchestrator import DiscussionStopped
        calls = []
        def runner(**request):
            calls.append(request["model"])
            raise api_transport.RequestCancelled()
        self.worker._request_runner = runner
        self.worker._raw_call = types.MethodType(RAW_CALL, self.worker)
        with self.assertRaises(DiscussionStopped):
            self.worker._request_with_fallback("openrouter", "a:free", "prompt")
        self.assertEqual(calls, ["a:free"])
        self.assertFalse(self.worker.api_calls)


    def test_openrouter_limit_source_distinguishes_shared_upstream_and_account(self):
        error = Limited()
        error.body = {"error": {"metadata": {"limit_source": "upstream_provider_shared_pool"}}}
        self.assertEqual(error_kind(error), "upstream_rate_limit")
        error.body = {"metadata": {"limit_source": "openrouter_free_tier_per_minute"}}
        error.response.headers["X-RateLimit-Reset"] = "1042000"
        self.assertEqual(error_kind(error), "account_rate_limit")
        self.assertEqual(retry_after_seconds(error, now=1000), 42)
        error.body["metadata"]["limit_source"] = "openrouter_free_tier_daily"
        self.assertEqual(error_kind(error), "daily_quota")
        self.assertEqual(retry_after_seconds(error, now=1000), 0)


if __name__ == "__main__":
    unittest.main()
