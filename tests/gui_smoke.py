"""Real Qt event loop, temporary profile and fake API; no network or user settings."""
from copy import deepcopy
from pathlib import Path
import os
import sys
import tempfile
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFontDatabase, QFont
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox
from ai_boardroom import config
from ai_boardroom import main as ui
from ai_boardroom import orchestrator
from ai_boardroom import routing
from ai_boardroom import model_ui
from ai_boardroom import session_log
from manual_demo import prepare_offline
from ai_boardroom.model_ui import ModelPicker, ModelEditor


def main():
    with tempfile.TemporaryDirectory(prefix="boardroom-qt-test-") as temporary:
        cfg = deepcopy(config.DEFAULT_CONFIG)
        cfg["rounds"] = 3
        cfg["api_keys"] = {provider: "offline-test" for provider in cfg["api_keys"]}
        prepare_offline(ui, orchestrator, session_log, config, cfg, Path(temporary))
        original_catalog = model_ui.cached_catalog
        def catalog_with_reserve():
            data = original_catalog()
            data["models"].append({**data["models"][0], "id": "demo/zz-transfer:free", "name": "Devir adayı"})
            return data
        model_ui.cached_catalog = model_ui.fetch_catalog = routing.fetch_catalog = catalog_with_reserve
        ordinary_demo_call = orchestrator.DiscussionWorker._raw_call
        held = []
        failed_round_two = []
        recovery_clicked = []
        def demo_call(self, provider, model, prompt, key, system_prompt=None):
            if self._request_context.get("purpose") == "Tur 2 / Mimar" and not failed_round_two:
                failed_round_two.append(True)
                raise RuntimeError("Çevrimdışı model hatası")
            if model == "demo/three:free" and not held:
                held.append(True)
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    self._check_stop()
                    if self._skip_event.wait(0.01):
                        from ai_boardroom.api_transport import ModelSkipped
                        raise ModelSkipped()
                raise RuntimeError("Manual skip was not requested")
            return ordinary_demo_call(self, provider, model, prompt, key, system_prompt)
        orchestrator.DiscussionWorker._raw_call = demo_call
        cfg["routing"].update(free_fallbacks=[], max_fallbacks=0, free_catalog_fallback=True, free_max_fallbacks=0)
        errors = []
        QMessageBox.information = lambda *args: QMessageBox.StandardButton.Ok
        QMessageBox.critical = lambda *args: errors.append("UI error")
        QMessageBox.warning = lambda *args: errors.append("UI warning")
        app = QApplication([])
        # Windows offscreen Qt does not always discover native fonts automatically.
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
            app.setFont(QFont("Segoe UI", 10))
        window = ui.MainWindow()
        window.project_input.setPlainText("Çevrimdışı Qt testi")
        window.show()
        phase = ["discussion"]
        completed = []
        skip_clicked = []

        def preflight():
            try:
                from types import SimpleNamespace
                window.worker = SimpleNamespace(session_status="failed")
                window.on_error("1 model denendi; görünür yanıt metni yok.")
                assert window.activity_alert.isVisible() and not errors
                window.worker = None
                free_picker = ModelPicker(window.model_library.entries(), "free", window)
                paid_picker = ModelPicker(window.model_library.entries(), "paid", window)
                assert free_picker.items.count() == 4 and paid_picker.items.count() == 1
                free_picker.items.setCurrentRow(0)
                free_picker.choose()
                assert free_picker.selected["tier"] == "free"
                editor = ModelEditor(parent=window)
                editor.provider.setCurrentText("openai")
                editor.identifier.setText("demo/custom-model")
                editor.name.setText("Elle eklenen deneme")
                editor.validate()
                entry = editor.value()
                window.model_library.custom_models.append(entry)
                window.model_library.filter.setCurrentIndex(1)
                window.model_library.populate()
                assert window.model_library.table.rowCount() == 2
                for row in range(window.model_library.table.rowCount()):
                    if window.model_library.table.item(row, 1).text() == entry["id"]:
                        window.model_library.table.setCurrentCell(row, 0)
                window.model_library.remove_model()
                assert not window.model_library.custom_models
                window.model_library.filter.setCurrentIndex(0)
                window.model_library.table.setCurrentCell(0, 0)
                urls = []
                ui.QDesktopServices.openUrl = lambda url: (urls.append(url.toString()) or True)
                window.model_library.open_selected()
                assert urls[0].startswith("https://openrouter.ai/demo/")
                # A paid key alone must never enable a paid run.
                window.usage_mode_combo.setCurrentIndex(1)
                window.paid_key_inputs["openrouter"].setText("separate-paid-demo-key")
                warnings = []
                original_warning = QMessageBox.warning
                QMessageBox.warning = lambda *args: warnings.append(True)
                window.on_start()
                QMessageBox.warning = original_warning
                assert warnings and window.worker is None
                window.usage_mode_combo.setCurrentIndex(0)
                window.on_start()
            except Exception as exc:
                errors.append("UI preflight: " + str(exc))
                app.exit(1)

        def check():
            if errors:
                app.exit(1)
                return
            if (not skip_clicked and window.activity_panel.phase == "attempt" and
                    window.activity_panel.current_model == "demo/three:free" and window.activity_panel.skip_button.isEnabled()):
                window.activity_panel.skip_button.click()
                skip_clicked.append(True)
            if window.continue_btn.isEnabled():
                if window.worker.session_status == "round_incomplete":
                    if recovery_clicked:
                        return  # The picker runs a nested Qt event loop.
                    if window.continue_btn.text() != "Eksik Yanıtları Dene" or "Mimar" not in window.activity_panel.title.text():
                        errors.append("Incomplete round recovery action missing")
                        app.exit(1)
                        return
                    if len([m for m in window.worker.transcript if m["round"] == 2 and m["speaker"] not in ("Mimar", "Sen")]) != 2:
                        errors.append("Remaining participants were not called before recovery")
                        app.exit(1)
                        return
                    recovery_clicked.append(True)
                    def choose_transfer():
                        dialog = app.activeModalWidget()
                        if not isinstance(dialog, ModelPicker):
                            errors.append("Handoff model picker did not open")
                            return
                        dialog.search.setText("demo/zz-transfer:free")
                        dialog.items.setCurrentRow(0)
                        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
                    QTimer.singleShot(0, choose_transfer)
                    window.handoff_btn.click()
                    return
                if window.worker.completed_rounds == 1 and not window.worker.user_objections:
                    assert window.objection_btn.isEnabled()
                    window.user_input.setText("Çevrimdışı kullanım gerekli.")
                    window.objection_btn.click()
                window.on_continue()
            if phase[0] == "discussion" and window.worker and not window._busy() and window.retry_summary_btn.isEnabled():
                if (window.worker.session_status != "completed" or len(window.worker.transcript) != 10
                        or not recovery_clicked or not window.worker.round_failures[0]["recovered_by"]
                        or not window.worker.model_handoffs
                        or window.worker.model_handoffs[0]["to"]["actual_model"] != "demo/zz-transfer:free"
                        or "Kullanıcı seçimiyle model devri" not in window.transcript_view.toPlainText()):
                    errors.append("Discussion did not complete")
                    app.exit(1)
                    return
                if not all(text in window.tracking_view.toPlainText() for text in ("SQLite", "Çevrimdışı kullanım gerekli.", "Yanıt bekliyor")):
                    errors.append("Tracking view missing data")
                    app.exit(1)
                    return
                if not any(e["event"] == "fallback" for e in window.worker.routing_events):
                    errors.append("429 did not trigger a fallback")
                    app.exit(1)
                    return
                if not skip_clicked or not any(e.get("reason") == "user_skip" for e in window.worker.api_errors):
                    errors.append("Other model button did not skip the current request")
                    app.exit(1)
                    return
                plans = [e for e in window.worker.routing_events if e["event"] == "plan"]
                if not plans or len(plans[0]["candidates"]) != 4:
                    errors.append("Catalog pool was not used with empty manual pool and legacy zero limit")
                    app.exit(1)
                    return
                if "429" not in window.api_view.toPlainText():
                    errors.append("API usage view did not show quota error")
                    app.exit(1)
                    return
                if window.model_library.table.rowCount() != 4 or window.allow_paid_check.isChecked():
                    errors.append("Free model library or paid opt-in incorrect")
                    app.exit(1)
                    return
                if str(window.session_dir) not in window.session_location_label.text():
                    errors.append("Session location missing from UI")
                    app.exit(1)
                    return
                window.history_list.setCurrentRow(0)
                opened = []
                ui.QDesktopServices.openUrl = lambda url: (opened.append(url.toLocalFile()) or True)
                window.on_open_session_folder()
                if not opened or Path(opened[0]).resolve() != window.session_dir.resolve():
                    errors.append("Wrong session folder")
                    app.exit(1)
                    return
                phase[0] = "summary"
                window.on_retry_summary()
            elif phase[0] == "summary" and not window._busy() and window.retry_summary_btn.isEnabled():
                if window.worker.session_status != "completed" or not window.last_summary_md:
                    errors.append("Summary retry failed")
                    app.exit(1)
                    return
                if "demo/one:free" not in window.activity_panel.history.toPlainText() or "demo/two:free" not in window.activity_panel.history.toPlainText():
                    errors.append("Visible activity panel lost attempted model names")
                    app.exit(1)
                    return
                if window.activity_panel.timer.isActive() or window.activity_panel.phase != "completed":
                    errors.append("Activity indicator did not finish with the worker")
                    app.exit(1)
                    return
                completed.append(True)
                if "--screenshots" in sys.argv:
                    destination = Path(__file__).resolve().parents[1] / "outputs" / "ui_check"
                    destination.mkdir(parents=True, exist_ok=True)
                    for widget, name in [(window.model_library, "model_library"), (window.api_tab, "api_usage"),
                                         (window.tabs.widget(3), "settings")]:
                        window.tabs.setCurrentWidget(widget)
                        app.processEvents()
                        window.grab().save(str(destination / (name + ".png")))
                    window.setWindowTitle("ÇEVRİMDIŞI GÖRSEL TEST — gerçek API yok")
                    window.status_label.setText("SAHTE GÖRSEL TEST — gerçek çağrı yapılmıyor")
                    window.tabs.setCurrentIndex(0)
                    window.activity_panel.begin(clear=True)
                    common = {"requested_model": "demo/one:free", "provider": "openrouter", "purpose": "Tur 1 / Mimar"}
                    window.on_route_activity({**common, "event": "plan", "automatic_fallback": True, "catalog_fallback": True,
                                              "candidates": [{"provider": "openrouter", "model": mid} for mid in ["demo/one:free", "demo/two:free", "demo/three:free"]]})
                    window.on_route_activity({**common, "event": "attempt", "model": "demo/one:free", "request_id": "visual-1", "timeout_seconds": 60, "attempt": 1, "attempt_limit": 2, "candidate_index": 1, "candidate_count": 3})
                    window.on_route_activity({**common, "event": "failure", "model": "demo/one:free", "http_status": 429, "reason": "rate_limit"})
                    window.on_route_activity({**common, "event": "retry", "model": "demo/one:free", "seconds": 36})
                    window.on_route_activity({**common, "event": "wait", "model": "demo/one:free", "seconds": 36, "reason": "Kota sonrası yeniden deneme"})
                    app.processEvents()
                    window.grab().save(str(destination / "activity_wait.png"))
                    window.on_route_activity({**common, "event": "fallback", "model": "demo/two:free"})
                    window.on_route_activity({**common, "event": "attempt", "model": "demo/two:free", "request_id": "visual-2", "timeout_seconds": 60, "attempt": 1, "attempt_limit": 2, "candidate_index": 2, "candidate_count": 3})
                    app.processEvents()
                    window.grab().save(str(destination / "activity_fallback.png"))
                    window.grab().save(str(destination / "activity_deadline.png"))
                window.close()
                app.quit()

        timer = QTimer()
        timer.timeout.connect(check)
        timer.start(25)
        QTimer.singleShot(0, preflight)
        QTimer.singleShot(15000, lambda: (errors.append("Timeout"), window.on_stop(), app.exit(1)))
        result = app.exec()
        if window._busy():
            window.on_stop()
            if window.worker:
                window.worker.wait(3000)
            if window.summary_worker:
                window.summary_worker.wait(3000)
        if errors or not completed or result:
            print("Qt smoke test FAILED:", ", ".join(errors))
            return 1
        print("Qt smoke test OK: 3 rounds/9 messages, 429 fallback, explicit model handoff, API usage, paths, summary retry; no network.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
