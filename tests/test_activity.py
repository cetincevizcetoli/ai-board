import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
except ImportError:
    QApplication = None


@unittest.skipUnless(QApplication, "Real Qt widgets are required for activity-panel tests")
class ActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from ai_boardroom.activity_ui import ActivityPanel
        cls.panel_type = ActivityPanel

    def setUp(self):
        self.now = [100.0]
        self.panel = self.panel_type(clock=lambda: self.now[0])
        self.addCleanup(self.panel.close)
        self.addCleanup(self.panel.timer.stop)
        self.notices = []
        self.panel.notification.connect(lambda message, tone: self.notices.append((message, tone)))
        self.panel.begin()

    def event(self, kind, **extra):
        self.panel.handle_event({"event": kind, "model": "demo/primary:free", "provider": "openrouter",
            "requested_model": "demo/primary:free", "purpose": "Tur 1 / Mimar", **extra})

    def test_attempt_shows_model_role_position_and_indeterminate_progress(self):
        self.event("attempt", attempt=2, attempt_limit=3, candidate_index=1, candidate_count=4)
        self.assertIn("Mimar", self.panel.model_label.text())
        self.assertIn("demo/primary:free", self.panel.model_label.text())
        self.assertIn("deneme 2/3", self.panel.detail.text())
        self.assertEqual(self.panel.progress.maximum(), 0)

    def test_wait_countdown_updates_and_does_not_pretend_to_send_request(self):
        self.event("wait", seconds=36, reason="Kota beklemesi")
        self.now[0] += 12
        self.panel.tick()
        self.assertIn("24 sn", self.panel.timer_label.text())
        self.now[0] += 30
        self.panel.tick()
        self.assertIn("bildirimi bekleniyor", self.panel.timer_label.text())
        self.assertEqual(self.panel.phase, "wait")
        self.assertEqual(self.panel.progress.value(), 1000)

    def test_network_wait_shows_elapsed_time_without_fake_completion_estimate(self):
        self.event("attempt", attempt=1)
        self.now[0] += 42
        self.panel.tick()
        self.assertIn("00:42", self.panel.timer_label.text())
        self.assertIn("kesin kalan süre bilinmiyor", self.panel.timer_label.text())
        self.assertNotIn("kalan: 0", self.panel.timer_label.text())

    def test_timer_updates_countdown_without_new_worker_messages(self):
        from PyQt6.QtTest import QTest
        self.event("wait", seconds=36)
        self.now[0] += 12
        QTest.qWait(350)
        self.assertIn("24 sn", self.panel.timer_label.text())

    def test_429_and_retry_emit_visible_alert_with_model(self):
        self.event("failure", http_status=429, reason="rate_limit")
        self.event("retry", seconds=20)
        self.assertTrue(any("429" in message and "demo/primary" in message for message, tone in self.notices))
        self.assertIn("yeniden denenecek", self.panel.title.text())
        self.assertIn("başka modele geçilmedi", self.panel.detail.text())
        self.assertFalse(self.panel.history_toggle.isChecked())
        self.assertIn("429", self.panel.latest_issue.text())

    def test_fallback_alert_names_original_and_selected_models_then_success(self):
        self.event("fallback", model="demo/fallback:free")
        message, tone = self.notices[-1]
        self.assertIn("demo/primary:free", message)
        self.assertIn("demo/fallback:free", message)
        self.assertEqual(tone, "warning")
        self.event("success", model="demo/fallback:free")
        self.assertIn("Yedek model yanıtladı", self.notices[-1][0])
        self.assertEqual(self.notices[-1][1], "success")

    def test_stopping_cancels_countdown_and_inflight_success_cannot_say_continue(self):
        self.event("wait", seconds=30)
        self.panel.stopping()
        self.event("success")
        self.assertIsNone(self.panel.deadline)
        self.assertEqual(self.panel.phase, "stopping")
        self.assertIn("Durdurma istendi", self.panel.title.text())
        self.panel.finish("stopped")
        self.assertFalse(self.panel.timer.isActive())
        self.assertNotEqual(self.panel.progress.maximum(), 0)

    def test_pause_and_new_attempt_reset_wait_deadline(self):
        self.event("wait", seconds=30)
        self.panel.pause(1)
        self.assertFalse(self.panel.timer.isActive())
        self.assertIsNone(self.panel.deadline)
        self.assertIn("devam etmeniz bekleniyor", self.panel.title.text())
        self.panel.begin("Tur 2", clear=False)
        self.event("attempt")
        self.assertTrue(self.panel.timer.isActive())
        self.assertIsNone(self.panel.deadline)

    def test_labels_are_plain_text_and_finished_state_retains_history(self):
        self.event("attempt", model="<model>")
        self.assertEqual(self.panel.model_label.textFormat(), Qt.TextFormat.PlainText)
        self.panel.finish("completed")
        self.assertIn("<model>", self.panel.history.toPlainText())
        self.assertFalse(self.panel.timer.isActive())
        self.assertIn("rapor hazır", self.panel.title.text())

    def test_plan_failure_and_next_model_are_visible_without_claiming_all_attempted(self):
        self.event("plan", candidates=[{"model": name} for name in ["demo/primary:free", "demo/second:free", "demo/third:free"]])
        self.event("attempt", candidate_index=1, candidate_count=3)
        self.event("failure", reason="empty_response", candidate_index=1, candidate_count=3)
        self.assertIn("demo/second:free", self.panel.queue_label.text())
        self.assertIn("görünür yanıt metni yok", self.notices[-1][0])
        self.assertIn("demo/third:free", self.panel.history.toPlainText())
        self.panel.finish("failed")
        self.assertIn("1 model denendi", self.panel.title.text())
        self.assertIn("Planlanan: 3", self.panel.detail.text())
        self.assertIn("görünür yanıt metni yok", self.notices[-1][0])

    def test_attempt_has_real_deadline_countdown_and_skip_control(self):
        self.event("attempt", request_id="request-1", timeout_seconds=60)
        self.now[0] += 42
        self.panel.tick()
        self.assertIn("18 sn", self.panel.timer_label.text())
        self.assertIn("Sınır: 60 sn", self.panel.timer_label.text())
        self.assertTrue(self.panel.skip_button.isEnabled())
        self.now[0] += 20
        self.panel.tick()
        self.assertIn("Süre doldu", self.panel.timer_label.text())

    def test_skip_button_signal_and_wait_pause_finish_disable_it(self):
        clicked = []
        self.panel.skip_requested.connect(lambda: clicked.append(True))
        self.event("attempt", request_id="request-1", timeout_seconds=30)
        self.panel.skip_button.click()
        self.assertEqual(clicked, [True])
        self.panel.skipping()
        self.assertFalse(self.panel.skip_button.isEnabled())
        self.event("wait", seconds=6)
        self.assertFalse(self.panel.skip_button.isEnabled())
        self.panel.finish("completed")
        self.assertFalse(self.panel.skip_button.isEnabled())

    def test_last_candidate_does_not_offer_a_nonexistent_next_model(self):
        self.event("attempt", request_id="request-last", timeout_seconds=60, candidate_index=3, candidate_count=3)
        self.assertFalse(self.panel.skip_button.isEnabled())
        self.assertIn("son model", self.panel.skip_button.toolTip())
