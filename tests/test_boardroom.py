import ast
import json
from pathlib import Path
import queue
import tempfile
import threading
import html
import types
import sys
import unittest
from unittest.mock import patch

try:
    from PyQt6.QtCore import QThread
except ImportError:
    from qt_stub import install
    install()

from ai_boardroom.orchestrator import DiscussionWorker, SummaryWorker
from ai_boardroom.session_log import atomic_write, build_tracking_html, build_summary_report, load_session_report

RAW_CALL = DiscussionWorker._raw_call


class BoardroomTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.personas = [{"name": name, "role": "test", "provider": "openai", "model": "fake"}
                         for name in ("Mimar", "Eleştirmen")]
        self.worker = DiscussionWorker("Test", self.personas, 1,
            {p: "test-key" for p in ["openai", "gemini", "groq", "anthropic", "openrouter"]}, queue.Queue(), session_path=self.path,
            routing_config={"mode": "paid", "paid_enabled": True, "paid_interval_seconds": 0, "backoff_seconds": 0})
        # Fail closed if a test accidentally reaches the network boundary.
        self.network = patch.object(DiscussionWorker, "_raw_call", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def data(self):
        return json.loads((self.path / "transcript.json").read_text(encoding="utf-8"))

    def test_multiple_meta_and_ids(self):
        _, meta = self.worker._parse_meta_block("Metin\n---META---\nAÇIK: a\nAÇIK: b\nKARAR: c\nKARAR: d")
        self.worker._apply_meta(meta, "Mimar", 1)
        self.assertEqual(len(self.worker.issues), 2)
        self.assertEqual([d["id"] for d in self.worker.decisions], ["D1", "D2"])
        self.worker._apply_meta({"cozuldu": ["I1", "I2"], "iptal": ["D1", "D2"]}, "Mimar", 1)
        self.assertTrue(all(i["durum"] == "ÇÖZÜLDÜ" for i in self.worker.issues))
        self.assertTrue(all(d["durum"] == "İPTAL" for d in self.worker.decisions))

    def test_cancellation_no_substring_guessing(self):
        self.worker._apply_meta({"karar": ["Bildirim", "Bildirim eklenecek"]}, "Mimar", 1)
        self.worker._apply_meta({"iptal": ["Bildirim iptal edilsin"]}, "Sen", 1)
        self.assertTrue(all(d["durum"] == "AKTİF" for d in self.worker.decisions))
        self.assertEqual(len(self.worker.unresolved_references), 1)
        self.worker.submit_user_message("İPTAL: D2")
        self.assertEqual(self.data()["decisions"][1]["durum"], "İPTAL")
        self.assertEqual(self.worker.decisions[0]["durum"], "AKTİF")

    def test_messages_saved_before_completion_and_on_error(self):
        count = []
        def response(*args):
            if count:
                self.assertEqual(len(self.data()["transcript"]), 1)
                raise RuntimeError("sensitive-provider-error")
            count.append(1)
            return "ilk cevap"
        self.worker.call_llm = response
        self.worker.run()
        self.assertEqual(self.data()["status"], "failed")
        self.assertEqual(len(self.data()["transcript"]), 1)
        self.assertNotIn("sensitive-provider-error", self.data()["last_error"])
        self.assertFalse((self.path / "ozet.md").exists())

    def test_stop_in_flight_saves_received_reply_no_next_call(self):
        def response(*args):
            self.worker.stop()
            return "istek sırasında durduruldu"
        self.worker.call_llm = response
        self.worker.synthesize = lambda *args: self.fail("No final report on stop")
        self.worker.run()
        self.assertEqual(self.data()["status"], "stopped")
        self.assertEqual(len(self.data()["transcript"]), 1)

    def test_stop_at_pause_preserves_records(self):
        self.worker.rounds = 3
        self.worker.call_llm = lambda *args: "cevap"
        self.worker.round_paused.connect(lambda r: self.worker.stop())
        self.worker.run()
        self.assertEqual(self.data()["status"], "stopped")
        self.assertEqual(self.data()["completed_rounds"], 1)

    def test_pause_resume_and_selected_round_counts(self):
        for rounds in (1, 2, 4):
            with self.subTest(rounds=rounds):
                worker = DiscussionWorker("Test", self.personas, rounds, {}, queue.Queue())
                worker.call_llm = lambda *args: "cevap"
                worker.round_paused.connect(lambda r: worker.resume())
                calls = []
                def summary(*args):
                    self.assertEqual(worker.completed_rounds, rounds)
                    calls.append(1)
                    return "rapor", "openai", "fake"
                worker.synthesize = summary
                worker.run()
                self.assertEqual(len(worker.transcript), 2 * rounds)
                self.assertEqual(worker.session_status, "completed")
                self.assertEqual(calls, [1])

    def test_pinned_model_timeout_runs_remaining_roles_then_retries_only_missing_role(self):
        from ai_boardroom.api_transport import ResponseDeadlineExceeded
        worker = self.worker
        worker.personas = [{**worker.personas[0], "name": name, "model": name.lower()} for name in ("A", "B", "C")]
        worker.rounds = 2
        calls = []
        pauses = []
        waits = []

        def response(provider, model, prompt, key, **kwargs):
            purpose = worker._request_context["purpose"]
            calls.append((purpose, model))
            if purpose == "Tur 2 / A" and calls.count((purpose, model)) == 1:
                raise ResponseDeadlineExceeded()
            if purpose == "Tur 2 / B":
                self.assertIn("A: Bu turdaki yanıt henüz alınamadı", prompt)
            if purpose == "Tur 2 / A":
                self.assertIn("Tur 2 / C", prompt)
            return "Katılımcının görüşü: " + purpose

        def resume(round_number):
            pauses.append((round_number, worker.completed_rounds, worker.session_status))
            if worker.session_status == "round_incomplete":
                self.assertEqual([m["speaker"] for m in worker.transcript if m["round"] == 2], ["B", "C"])
            worker.resume()

        worker._raw_call = response
        worker._routing_wait = lambda seconds, reason: waits.append((seconds, reason))
        worker.round_paused.connect(resume)
        worker.synthesize = lambda *args: ("Rapor", "openai", "summary")
        worker.run()
        self.assertEqual(pauses, [(1, 1, "paused"), (2, 1, "round_incomplete")])
        self.assertEqual(calls[-4:], [("Tur 2 / A", "a"), ("Tur 2 / B", "b"), ("Tur 2 / C", "c"), ("Tur 2 / A", "a")])
        self.assertTrue(any("Sabit model" in reason for _, reason in waits))
        self.assertEqual(worker.session_status, "completed")
        self.assertEqual(worker.round_failures[0]["recovered_by"], worker.transcript[-1]["message_id"])

    def test_explicit_handoff_preserves_old_opinion_and_pins_successful_replacement(self):
        from ai_boardroom.api_transport import ResponseDeadlineExceeded
        worker = self.worker
        worker.personas = [{**worker.personas[0], "name": name, "model": model} for name, model in (("A", "x"), ("B", "y"))]
        worker.rounds = 3
        calls, prompts, approvals = [], [], []
        new_model = {"provider": "openai", "id": "z", "tier": "unverified"}

        def response(provider, model, prompt, key, **kwargs):
            purpose = worker._request_context["purpose"]
            calls.append((purpose, model))
            if purpose == "Tur 1 / A":
                return ("X'in önceki görüşü\n---META---\nKARAR: Yerel kayıt\n"
                        "GÖRÜŞ: kayıt | yalnız yerel | ilk gerekçe\nKARŞI: D1 | X'in karşı görüşü")
            if purpose == "Tur 2 / A" and model == "x":
                raise ResponseDeadlineExceeded()
            if purpose == "Tur 2 / A" and model == "z":
                prompts.append(prompt)
                return "Öneriyi değiştiriyorum.\n---META---\nGÖRÜŞ: kayıt | dışa aktarım | ikinci gerekçe"
            return "Katılımcının güncel görüşü: " + purpose

        def resume(round_number):
            if worker.session_status == "round_incomplete":
                approvals.append(worker.request_handoff("A", new_model))
                if not approvals[-1]:
                    worker.stop()
            else:
                worker.resume()

        worker._raw_call = response
        worker.round_paused.connect(resume)
        worker.synthesize = lambda *args: (worker.evidence_report(), "openai", "summary")
        worker.run()
        self.assertEqual(approvals, [True])
        self.assertEqual(calls[2:], [("Tur 2 / A", "x"), ("Tur 2 / B", "y"), ("Tur 2 / A", "z"),
                                    ("Tur 3 / A", "z"), ("Tur 3 / B", "y")])
        self.assertIn("X'in önceki görüşü", prompts[0])
        self.assertIn("Tur 2 / B", prompts[0])
        self.assertIn("koruduğunu, değiştirdiğini veya reddettiğini", prompts[0])
        state = self.data()
        self.assertEqual(state["status"], "completed")
        transfer = state["model_handoffs"][0]
        self.assertEqual((transfer["from"]["actual_model"], transfer["to"]["actual_model"]), ("x", "z"))
        self.assertFalse(state["opinions"][1]["changed"])
        self.assertEqual(state["opinions"][1]["model_revision"], 1)
        self.assertEqual([opinion["model"] for opinion in state["opinions"]], ["x", "z"])
        self.assertEqual(state["votes"][0]["model"], "x")
        self.assertFalse(state["assessments"][0]["opponents"])
        self.assertEqual(next(m for m in state["transcript"] if m["message_id"] == transfer["message_id"])["model"], "z")
        self.assertIn("Kullanıcı seçimiyle model devirleri", (self.path / "ozet.md").read_text(encoding="utf-8"))

    def test_failed_handoff_is_single_attempt_and_leaves_original_model_pinned(self):
        from ai_boardroom.api_transport import ResponseDeadlineExceeded
        worker = self.worker
        worker.personas[0].update(name="A", model="x")
        worker.personas = worker.personas[:1]
        worker.rounds = 2
        worker.routing_config["max_retries"] = 2
        attempts, pauses = [], []
        choice = {"provider": "openai", "id": "z", "tier": "unverified"}

        def raw(provider, model, *args, **kwargs):
            purpose = worker._request_context["purpose"]
            attempts.append((purpose, model))
            if purpose == "Tur 2 / A" and model == "x" and attempts.count((purpose, model)) == 1:
                raise ResponseDeadlineExceeded()
            if model == "z":
                error = RuntimeError("Service unavailable")
                error.status_code = 503
                raise error
            return "Katılımcının görüşü"

        def resume(round_number):
            if worker.session_status == "round_incomplete":
                pauses.append((worker.role_models["A"]["actual_model"], bool(worker.handoff_options("A", [choice]))))
                if len(pauses) == 1 and worker.request_handoff("A", choice):
                    return
            worker.resume()

        worker._raw_call = raw
        worker._routing_wait = lambda *_: None
        worker.round_paused.connect(resume)
        worker.synthesize = lambda *args: (worker.evidence_report(), "openai", "summary")
        worker.run()
        self.assertEqual(pauses, [("x", True), ("x", False)])
        self.assertEqual(attempts, [("Tur 1 / A", "x"), ("Tur 2 / A", "x"), ("Tur 2 / A", "z"), ("Tur 2 / A", "x")])
        self.assertEqual(worker.model_handoffs, [])
        self.assertEqual(worker.round_failures[1]["handoff_target"], "z")
        self.assertEqual(worker.session_status, "completed")

    def test_stopping_at_incomplete_round_preserves_other_roles_without_final_report(self):
        from ai_boardroom.api_transport import ResponseDeadlineExceeded
        worker = self.worker
        worker.personas = [{**worker.personas[0], "name": name, "model": name.lower()} for name in ("A", "B", "C")]
        worker.rounds = 2

        def response(provider, model, *args, **kwargs):
            if worker._request_context["purpose"] == "Tur 2 / A":
                raise ResponseDeadlineExceeded()
            return "Görüş"

        worker._raw_call = response
        worker.round_paused.connect(lambda r: worker.stop() if worker.session_status == "round_incomplete" else worker.resume())
        worker.run()
        state = self.data()
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["completed_rounds"], 1)
        self.assertEqual([m["speaker"] for m in state["transcript"] if m["round"] == 2], ["B", "C"])
        self.assertIsNone(state["round_failures"][0]["recovered_by"])
        self.assertFalse((self.path / "ozet.md").exists())
        self.assertIn("Yanıt eksik", (self.path / "oturum_raporu.md").read_text(encoding="utf-8"))

    def test_all_summary_providers_fail_no_success_file(self):
        self.worker.call_llm = lambda *args: "cevap"
        self.worker._call_provider_with_retry = lambda *args: (_ for _ in ()).throw(RuntimeError("secret"))
        self.worker.run()
        self.assertEqual(self.data()["status"], "summary_failed")
        self.assertFalse((self.path / "ozet.md").exists())

    def test_summary_fallback(self):
        self.worker.completed_rounds = self.worker.rounds
        self.worker.api_keys = {"openai": "fake-key", "gemini": "fake-key"}
        self.worker.routing_config["paid_fallbacks"] = [{"provider": "openai", "model": "fake"}]
        calls = []
        def response(prov, *args):
            calls.append(prov)
            if prov == "gemini":
                raise RuntimeError("quota")
            return "rapor"
        self.worker._call_provider_with_retry = response
        self.assertEqual(self.worker.synthesize("gemini")[1], "openai")
        self.assertEqual(calls, ["gemini", "openai"])

    def test_user_message_durable_no_duplicate(self):
        self.worker.submit_user_message("Ek koşul")
        self.assertEqual(self.data()["transcript"][0]["text"], "Ek koşul")
        context = []
        self.worker._drain_user_messages(context)
        self.assertEqual(len(self.worker.transcript), 1)
        self.assertIn("Ek koşul", context[0])

    def test_last_moment_intervention_blocks_final_report(self):
        def response(persona, *args):
            if persona["name"] == "Eleştirmen":
                self.worker.submit_user_message("Yeni koşul")
            return "cevap"
        self.worker.call_llm = response
        self.worker.run()
        self.assertEqual(self.data()["status"], "review_required")
        self.assertFalse((self.path / "ozet.md").exists())

    def test_atomic_replace_failure_preserves_previous_file(self):
        destination = self.path / "file.json"
        destination.write_text("old", encoding="utf-8")
        with patch("ai_boardroom.session_log.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                atomic_write(destination, "new")
        self.assertEqual(destination.read_text(), "old")
        self.assertEqual(list(self.path.glob(".boardroom-*")), [])

    def test_rate_limit_retry_and_cancelled_delay(self):
        from ai_boardroom import orchestrator
        with patch.object(orchestrator, "CALL_DELAY_SECONDS", 0), patch.object(orchestrator, "RETRY_BACKOFF_SECONDS", 0):
            with patch.object(self.worker, "_raw_call", side_effect=[RuntimeError("429"), "ok"]) as call:
                self.assertEqual(self.worker._call_provider_with_retry("openai", "fake", "", ""), "ok")
                self.assertEqual(call.call_count, 2)
        self.worker.stop()
        from ai_boardroom.orchestrator import DiscussionStopped
        with self.assertRaises(DiscussionStopped):
            self.worker._call_provider_with_retry("openai", "fake", "", "")

    def test_incomplete_session_cannot_retry_final_report(self):
        with self.assertRaises(RuntimeError):
            self.worker.generate_summary_only()

    def test_ui_retry_uses_thread_start_not_network_call(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "ai_boardroom" / "main.py").read_text(encoding="utf-8"))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "on_retry_summary")
        calls = [n.func.attr for n in ast.walk(method) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertIn("start", calls)
        self.assertNotIn("generate_summary_only", calls)

    def test_question_routed_next_round_answer_and_changed_opinion(self):
        self.worker.rounds = 2
        prompts = []
        answers = iter([
            "Yerel olsun\n---META---\nGÖRÜŞ: depolama | SQLite | yerel kullanım",
            "Sorum var\n---META---\nSORU: Mimar | Çok kullanıcı varsa ne olacak?\nGÖRÜŞ: depolama | PostgreSQL | eşzamanlılık",
            "Haklısın\n---META---\nYANIT: Q1 | Çok kullanıcı için PostgreSQL\nGÖRÜŞ: depolama | PostgreSQL | Eleştirmenin eşzamanlılık itirazı",
            "Anlaştık\n---META---\nGÖRÜŞ: depolama | PostgreSQL | yanıt yeterli",
        ])
        def fake(prov, model, prompt, key, **kwargs):
            prompts.append(prompt)
            return next(answers)
        self.worker._call_provider_with_retry = fake
        self.worker.round_paused.connect(lambda r: self.worker.resume())
        self.worker.synthesize = lambda *args: ("rapor", "openai", "fake")
        self.worker.run()
        self.assertEqual(self.worker.session_status, "completed")
        self.assertIn("Çok kullanıcı varsa ne olacak?", prompts[2])
        self.assertIn("Eleştirmen", prompts[2])
        self.assertEqual(self.worker.questions[0]["answered_by"], "Mimar")
        change = next(o for o in self.worker.opinions if o["changed"])
        self.assertEqual(change["previous"], "SQLite")
        self.assertIn("eşzamanlılık", change["reason"])
        saved = self.data()
        self.assertEqual(saved["questions"][0]["status"], "answered")
        self.assertEqual(len(saved["opinions"]), 4)

    def test_second_round_keeps_shared_record_and_links_only_seen_objections(self):
        worker = self.worker
        worker.personas.append({**worker.personas[0], "name": "Ürün"})
        worker.rounds = 2
        prompts = {}

        def fake(provider, model, prompt, key, **kwargs):
            purpose = worker._request_context["purpose"]
            prompts[purpose] = prompt
            if purpose == "Tur 1 / Mimar":
                worker.submit_user_message("Bağlantı olmayabilir.", objection=True)
                return "Birinci tur Mimar"
            if purpose == "Tur 2 / Mimar":
                return "Çevrimdışı çalışmalı.\n---META---\nMÜDAHALE_YANITI: U1 | Yerel depolama ekledim."
            if purpose == "Tur 2 / Eleştirmen":
                worker.submit_user_message("Bakım maliyeti açıkta.", objection=True)
                return "İkinci tur Eleştirmen\n---META---\nMÜDAHALE_YANITI: U2 | Henüz görülmemiş bir itiraz."
            return purpose

        worker._call_provider_with_retry = fake
        worker.round_paused.connect(lambda r: worker.resume())
        worker.synthesize = lambda *args: (worker.evidence_report(), "openai", "fake")
        worker.run()
        self.assertEqual(worker.session_status, "completed")
        snapshots = [prompt.split("Önceki turun ortak kaydı:\n")[1].split("\n\nBu turda")[0]
                     for purpose, prompt in prompts.items() if purpose.startswith("Tur 2 /")]
        for snapshot in snapshots:
            self.assertEqual(snapshot, snapshots[0])
            for text in ("Birinci tur Mimar", "Tur 1 / Eleştirmen", "Tur 1 / Ürün", "Bağlantı olmayabilir."):
                self.assertIn(text, snapshot)
        later = prompts["Tur 2 / Eleştirmen"]
        self.assertIn("Birinci tur Mimar", later)
        self.assertIn("Çevrimdışı çalışmalı.", later)
        self.assertNotIn("Bakım maliyeti açıkta.", later)
        self.assertIn("Bakım maliyeti açıkta.", prompts["Tur 2 / Ürün"])
        first, second = self.data()["user_objections"]
        self.assertEqual(first["replies"][0]["text"], "Yerel depolama ekledim.")
        self.assertEqual(first["replies"][0]["message_id"], worker.transcript[4]["message_id"])
        self.assertEqual(second["replies"], [])
        report = (self.path / "ozet.md").read_text(encoding="utf-8")
        self.assertIn("Bakım maliyeti açıkta. — **Yanıt bekliyor**", report)
        self.assertIn("Yerel depolama ekledim.", report)

    def test_only_recipient_can_answer_and_unanswered_persists(self):
        self.worker._apply_meta({"soru": ["Eleştirmen | Neden?", "Bilinmeyen | soru"]}, "Mimar", 1)
        self.worker._apply_meta({"yanit": ["Q1 | kendim cevapladım"]}, "Mimar", 2)
        self.assertEqual(self.worker.questions[0]["status"], "open")
        self.assertEqual(len(self.worker.questions), 1)
        self.assertTrue(self.worker.meta_warnings)

    def test_objection_carried_and_only_author_can_resolve(self):
        self.worker._apply_meta({"karar": ["SQLite"]}, "Mimar", 1)
        self.worker._apply_meta({"itiraz": ["D1 | Çok kullanıcı"]}, "Eleştirmen", 1)
        self.assertIn("Çok kullanıcı", self.worker._negotiation_context("Mimar"))
        self.worker._apply_meta({"itiraz_cozuldu": ["O1 | sorun yok"]}, "Mimar", 2)
        self.assertEqual(self.worker.objections[0]["status"], "open")
        self.worker._apply_meta({"itiraz_cozuldu": ["O1 | tek kullanıcı olduğu doğrulandı"]}, "Eleştirmen", 2)
        self.assertEqual(self.worker.objections[0]["status"], "resolved")

    def test_missing_meta_is_visible_not_assumed_consensus(self):
        self.worker.call_llm = lambda *args: "sadece metin"
        self.worker.synthesize = lambda *args: ("rapor", "openai", "fake")
        self.worker.run()
        self.assertEqual(len(self.data()["meta_warnings"]), 2)
        self.assertEqual(self.worker.decisions, [])

    def test_proposal_is_not_consensus_until_all_final_round_support(self):
        self.worker.rounds = 3
        self.worker._apply_meta({"karar": ["SQLite"], "destek": ["D1 | uygun"]}, "Mimar", 1)
        self.worker._apply_meta({"destek": ["D1 | uygun"]}, "Eleştirmen", 1)
        self.assertFalse(self.worker.decision_assessments()[0]["consensus"])
        self.worker._apply_meta({"destek": ["D1 | yerel kullanım"]}, "Mimar", 3)
        self.worker._apply_meta({"destek": ["D1 | tek kullanıcı"]}, "Eleştirmen", 3)
        self.assertTrue(self.worker.decision_assessments()[0]["consensus"])
        self.worker._apply_meta({"itiraz": ["D1 | eşzamanlılık belirsiz"]}, "Eleştirmen", 3)
        self.assertFalse(self.worker.decision_assessments()[0]["consensus"])

    def test_dissent_changes_and_unanswered_in_final_evidence(self):
        self.worker._apply_meta({"karar": ["SQLite"], "destek": ["D1 | basit"],
                                 "gorus": ["depo | SQLite | yerel"], "soru": ["Eleştirmen | Kapasite?"]}, "Mimar", 1)
        self.worker._apply_meta({"karsi": ["D1 | çok kullanıcı riski"]}, "Eleştirmen", 1)
        self.worker._apply_meta({"gorus": ["depo | PostgreSQL | eşzamanlılık gerekçesi"]}, "Mimar", 1)
        report = self.worker.evidence_report()
        for expected in ("uzlaşı doğrulanmadı", "çok kullanıcı riski", "Kapasite?", "SQLite → PostgreSQL", "eşzamanlılık gerekçesi"):
            self.assertIn(expected, report)

    def test_cancelled_proposal_excluded_and_old_support_does_not_override_opposition(self):
        self.worker._apply_meta({"karar": ["SQLite"], "destek": ["D1 | basit"]}, "Mimar", 1)
        self.worker._apply_meta({"destek": ["D1 | basit"]}, "Eleştirmen", 1)
        self.worker._apply_meta({"karsi": ["D1 | fikrim değişti"]}, "Eleştirmen", 1)
        self.assertFalse(self.worker.decision_assessments()[0]["consensus"])
        self.worker._apply_meta({"iptal": ["D1"]}, "Sen", 1)
        self.assertEqual(self.worker.decision_assessments(), [])
        self.assertIn("İptal edilen öneriler", self.worker.evidence_report())

    def test_synthesize_refuses_early_final_report(self):
        with self.assertRaises(RuntimeError):
            self.worker.synthesize()

    def test_actual_summary_contains_five_sections(self):
        self.worker.completed_rounds = 1
        self.worker.summary_provider = "openai"
        self.worker._call_provider_with_retry = lambda *a: "İnsan incelemesiyle ilerleyin."
        report, _, _ = self.worker.synthesize()
        for section in ("Üzerinde uzlaşılan konular", "Görüş ayrılığı devam eden konular",
                        "Fikrini değiştiren ajanlar ve gerekçeleri", "Cevaplanmamış sorular",
                        "Nihai öneri ve uygulanabilir sonraki adımlar"):
            self.assertIn("## " + section, report)
        self.assertIn("Kayıtlardan doğrulanmış uzlaşı YOK.", report)
        self.assertIn("doğrulanmış karar değildir", report)

    def test_tracking_view_escapes_content_and_shows_round_change(self):
        self.worker._apply_meta({"gorus": ["konu | <b>ilk</b> | neden"]}, "Mimar", 1)
        self.worker._apply_meta({"gorus": ["konu | ikinci | yeni kanıt"]}, "Mimar", 2)
        rendered = build_tracking_html(self.worker.snapshot())
        self.assertIn("&lt;b&gt;ilk&lt;/b&gt;", rendered)
        self.assertIn("Değişti", rendered)
        self.assertIn("yeni kanıt", rendered)
        self.assertNotIn("<b>ilk</b>", rendered)

    def test_append_message_handler_executes_and_escapes_html(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "ai_boardroom" / "main.py").read_text(encoding="utf-8"))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "append_message")
        namespace = {"html": html, "COLORS": ["#000"], "PROVIDER_LABELS": {"openai": "OpenAI"}}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), namespace)
        rendered = []
        window = types.SimpleNamespace(persona_rows=[], transcript_view=types.SimpleNamespace(append=rendered.append))
        namespace["append_message"](window, {"speaker": "<Mimar>", "text": "a < b & c", "round": 1,
                                             "model": "<fake>", "provider": "openai"})
        self.assertIn("&lt;Mimar&gt;", rendered[0])
        self.assertIn("a &lt; b &amp; c", rendered[0])
        self.assertIn("&lt;fake&gt;", rendered[0])

    def test_disk_error_preserves_message_in_memory_and_emits_for_export(self):
        shown = []
        self.worker.message_ready.connect(shown.append)
        with patch("ai_boardroom.orchestrator.save_transcript_json", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.worker._record({"speaker": "Mimar", "round": 1, "text": "cevap"})
        self.assertEqual(shown[0]["text"], "cevap")
        self.assertEqual(self.worker.transcript, shown)

    def test_incomplete_export_identified_and_old_transcript_format_supported(self):
        old_entry = {"speaker": "Mimar", "round": 1, "text": "eski kayıt"}
        report = build_summary_report("Test", [old_entry], "Nihai rapor yok", session_status="stopped")
        self.assertIn("Eksik", report)
        self.assertIn("stopped", report)
        self.assertIn("eski kayıt", report)

    def test_summary_worker_entry_uses_background_thread_and_failure_preserves_report(self):
        self.worker.completed_rounds = 1
        self.worker.summary_text = "önceki rapor"
        caller = threading.get_ident()
        identities = []
        def response(*args):
            identities.append(threading.get_ident())
            raise RuntimeError("secret")
        self.worker._call_provider_with_retry = response
        retry = SummaryWorker(self.worker)
        # Exercise the worker's entry point without pretending to run a Qt event loop.
        background = threading.Thread(target=retry.run)
        background.start()
        background.join(3)
        self.assertFalse(background.is_alive())
        self.assertTrue(identities)
        self.assertNotEqual(identities[0], caller)
        self.assertEqual(self.worker.summary_text, "önceki rapor")

    def test_legacy_and_authoritative_checkpoint_loading(self):
        (self.path / "ozet.md").write_text("eski markdown", encoding="utf-8")
        self.assertEqual(load_session_report(self.path)[0], "eski markdown")
        self.worker.session_status = "stopped"
        self.worker._record({"speaker": "Mimar", "round": 1, "text": "korunan yeni konuşma"})
        report, state = load_session_report(self.path)
        self.assertIn("korunan yeni konuşma", report)
        self.assertIn("stopped", report)
        self.assertNotIn("eski markdown", report)

    def test_turkish_lowercase_meta_and_unknown_line_warning(self):
        _, meta = self.worker._parse_meta_block("cevap\n---META---\nİtiraz_çözüldü: O1 | çözüldü\nbilinmeyen: test")
        self.assertEqual(meta["itiraz_cozuldu"], ["O1 | çözüldü"])
        self.assertEqual(len(meta["warnings"]), 1)

    def test_real_model_fields_before_marker_are_preserved(self):
        text = "JSON basittir.\nGÖRÜŞ: depo | JSON | basitlik\n---META---\nKARAR: JSON kullanımı"
        clean, meta = self.worker._parse_meta_block(text)
        self.worker._apply_meta(meta, "Mimar", 1)
        self.assertEqual(clean, "JSON basittir.")
        self.assertEqual(self.worker.opinions[0]["stance"], "JSON")
        self.assertEqual(self.worker.decisions[0]["ozet"], "JSON kullanımı")

    def test_structured_fields_without_marker_are_read_with_warning(self):
        clean, meta = self.worker._parse_meta_block("Yanıt\n- **GÖRÜŞ:** depo | JSON | basitlik")
        self.assertEqual(clean, "Yanıt")
        self.assertEqual(meta["gorus"], ["depo | JSON | basitlik"])
        self.assertTrue(meta["warnings"])

    def test_five_provider_adapters_with_fake_sdks(self):
        from ai_boardroom import api_transport
        self.worker._request_runner = api_transport.call_provider
        calls = []
        def openai_factory(**options):
            def create(**request):
                calls.append(("openai-compatible", options, request))
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))])
            return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        def anthropic_factory(**options):
            def create(**request):
                calls.append(("anthropic", options, request))
                return types.SimpleNamespace(content=[types.SimpleNamespace(text="ok")])
            return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
        def gemini_factory(**options):
            def create(**request):
                calls.append(("gemini", options, request))
                return types.SimpleNamespace(text="ok")
            return types.SimpleNamespace(models=types.SimpleNamespace(generate_content=create))
        fake_google = types.ModuleType("google")
        fake_google.genai = types.SimpleNamespace(Client=gemini_factory)
        modules = {"google": fake_google, "anthropic": types.SimpleNamespace(Anthropic=anthropic_factory),
                   "openai": types.SimpleNamespace(OpenAI=openai_factory)}
        with patch.dict(sys.modules, modules):
            for provider in ("openai", "groq", "openrouter", "anthropic", "gemini"):
                self.assertEqual(RAW_CALL(self.worker, provider, "fake-model", "prompt", "fake-key", "system"), "ok")
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[1][1]["base_url"], "https://api.groq.com/openai/v1")
        self.assertEqual(calls[2][1]["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(calls[3][2]["system"], "system")
        self.assertEqual(calls[4][2]["config"]["system_instruction"], "system")

    def test_three_participants_three_rounds_only_ten_fake_calls(self):
        self.worker.personas.append({"name": "UX", "role": "ürün", "provider": "openai", "model": "fake"})
        self.worker.rounds = 3
        responses = iter([
            "Başlangıç\n---META---\nKARAR: SQLite\nGÖRÜŞ: depo | SQLite | basit",
            "İtiraz\n---META---\nİTİRAZ: D1 | eşzamanlılık\nSORU: Mimar | Kullanıcı sayısı?\nGÖRÜŞ: depo | PostgreSQL | çok kullanıcı",
            "Ürün\n---META---\nGÖRÜŞ: depo | PostgreSQL | çok kullanıcı",
            "Görüşüm değişti\n---META---\nYANIT: Q1 | Çok kullanıcı\nİPTAL: D1\nKARAR: PostgreSQL\nGÖRÜŞ: depo | PostgreSQL | eşzamanlılık itirazı",
            "Yanıt yeterli\n---META---\nİTİRAZ_ÇÖZÜLDÜ: O1 | SQLite iptal edildi\nGÖRÜŞ: depo | PostgreSQL | çok kullanıcı",
            "Ürün uygun\n---META---\nGÖRÜŞ: depo | PostgreSQL | uygun",
            "Son görüş\n---META---\nDESTEK: D2 | eşzamanlılık\nGÖRÜŞ: depo | PostgreSQL | uygun",
            "Son görüş\n---META---\nDESTEK: D2 | uygun\nGÖRÜŞ: depo | PostgreSQL | uygun",
            "Son görüş\n---META---\nDESTEK: D2 | uygun\nSORU: Mimar | Bakım bütçesi?\nGÖRÜŞ: depo | PostgreSQL | uygun",
            "Öneriyi insan incelemesinden sonra uygulayın; bakım bütçesini netleştirin.",
        ])
        calls = []
        def fake(*args, **kwargs):
            calls.append((args, kwargs))
            return next(responses)
        self.worker._call_provider_with_retry = fake
        self.worker.round_paused.connect(lambda r: self.worker.resume())
        self.worker.run()
        self.assertEqual(self.worker.session_status, "completed")
        self.assertEqual(len(calls), 10)
        self.assertEqual(len(self.data()["transcript"]), 9)
        self.assertEqual(self.worker.questions[0]["status"], "answered")
        self.assertEqual(self.worker.questions[1]["status"], "open")
        self.assertTrue(self.worker.decision_assessments()[0]["consensus"])
        report = (self.path / "ozet.md").read_text(encoding="utf-8")
        self.assertIn("SQLite → PostgreSQL", report)
        self.assertIn("Bakım bütçesi?", report)
        self.assertIn("İptal edilen öneriler", report)


if __name__ == "__main__":
    unittest.main()
