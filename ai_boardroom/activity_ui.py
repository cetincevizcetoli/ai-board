"""Visible, non-blocking progress for model calls and fallback waits."""
from datetime import datetime
import math
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel, QProgressBar, QPlainTextEdit, QPushButton


class ActivityPanel(QFrame):
    notification = pyqtSignal(str, str)
    skip_requested = pyqtSignal()

    def __init__(self, parent=None, clock=None):
        super().__init__(parent)
        self.setObjectName("activityPanel")
        self.clock = clock or time.monotonic
        self.deadline = None
        self.response_deadline = None
        self.response_limit = 0
        self.current_request_id = None
        self.duration = 0
        self.started_at = None
        self.current_model = ""
        self.current_purpose = ""
        self.phase = "idle"
        self.had_warning = False
        self.fallback_pending = False
        self.planned_models = []
        self.attempted_models = []
        self.failed_models = []
        self.skipped_models = []
        self.last_failure = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)
        header = QHBoxLayout()
        layout.addLayout(header)
        self.title = QLabel("Model durumu · Hazır")
        self.title.setStyleSheet("font-weight:600")
        self.title.setWordWrap(True)
        header.addWidget(self.title, 1)
        self.model_label = QLabel()
        self.queue_label = QLabel()
        self.detail = QLabel()
        self.timer_label = QLabel()
        for field in (self.title, self.model_label, self.queue_label, self.detail, self.timer_label):
            field.setTextFormat(Qt.TextFormat.PlainText)
            field.setWordWrap(True)
        layout.addWidget(self.model_label)
        layout.addWidget(self.timer_label)
        self.latest_issue = QLabel()
        self.latest_issue.setTextFormat(Qt.TextFormat.PlainText)
        self.latest_issue.setWordWrap(True)
        layout.addWidget(self.latest_issue)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.skip_button = QPushButton("Diğer Modeli Dene")
        self.skip_button.setEnabled(False)
        self.skip_button.clicked.connect(lambda: self.skip_requested.emit())
        header.addWidget(self.skip_button)
        self.history_toggle = QPushButton("Model günlüğü")
        self.history_toggle.setCheckable(True)
        self.history_toggle.toggled.connect(self.show_history)
        header.addWidget(self.history_toggle)
        layout.addWidget(self.queue_label)
        layout.addWidget(self.detail)
        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setMaximumBlockCount(120)
        self.history.setFixedHeight(88)
        self.history.hide()
        layout.addWidget(self.history)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.tick)
        self.tone("idle")
        self.show_history(False)
        self.update_compact_view()

    def tone(self, tone):
        colors = {"idle": ("#252b34", "#64748b", "#e5e7eb"),
                  "info": ("#142a40", "#4594db", "#e1efff"),
                  "warning": ("#392b16", "#c28a32", "#ffe7ba"),
                  "error": ("#3b2028", "#d46b7b", "#ffe0e5"),
                  "success": ("#16352d", "#4ca88b", "#d4fff0")}
        background, border, text = colors.get(tone, colors["info"])
        self.setStyleSheet(f"QFrame#activityPanel{{background:{background}; border:1px solid {border}; border-radius:5px}}"
                           f"QFrame#activityPanel QLabel{{color:{text};border:0}}"
                           f"QFrame#activityPanel QProgressBar{{background:#111827;border:0}}"
                           f"QFrame#activityPanel QProgressBar::chunk{{background:{border}}}")

    def show_history(self, visible):
        self.history.setVisible(visible)
        self.queue_label.setVisible(visible)
        self.detail.setVisible(visible)
        self.history_toggle.setText("Günlüğü gizle" if visible else "Model günlüğü")

    def update_compact_view(self):
        self.model_label.setVisible(bool(self.model_label.text()))
        self.timer_label.setVisible(self.timer.isActive() and bool(self.timer_label.text()))
        self.progress.setVisible(self.timer.isActive())
        self.skip_button.setVisible(self.skip_button.isEnabled())
        issue = self.last_failure if self.phase not in ("completed", "success", "paused", "round_incomplete") else ""
        self.latest_issue.setText(issue)
        self.latest_issue.setVisible(bool(issue))

    def log(self, text):
        self.history.appendPlainText(datetime.now().strftime("%H:%M:%S") + "  " + text)

    def begin(self, text="Seçilen modeller ve fiyatlar kontrol ediliyor...", clear=True):
        if clear:
            self.history.clear()
            self.history_toggle.setChecked(False)
            self.had_warning = False
            self.fallback_pending = False
            self.planned_models = []
            self.attempted_models = []
            self.failed_models = []
            self.skipped_models = []
            self.last_failure = ""
            self.queue_label.clear()
        self.phase = "preparing"
        self.current_model = ""
        self.current_purpose = ""
        self.deadline = None
        self.response_deadline = None
        self.current_request_id = None
        self.skip_button.setEnabled(False)
        self.started_at = self.clock()
        self.title.setText("İşlem başladı")
        self.model_label.setText(text)
        self.detail.setText("Uygulama çalışıyor; model isteği hazırlanıyor. Durdur ile iptal edebilirsiniz.")
        self.timer_label.setText("Geçen süre: 00:00")
        self.progress.setRange(0, 0)
        self.tone("info")
        self.timer.start()
        self.update_compact_view()

    def update_queue(self, event):
        index = event.get("candidate_index", 0)
        if index and self.planned_models:
            next_model = self.planned_models[index] if index < len(self.planned_models) else "Bu sırada başka aday yok"
            self.queue_label.setText(f"Bu adımın model sırası: {index}/{len(self.planned_models)} • Sonraki aday: {next_model}")

    def next_candidate(self, event):
        index = event.get("candidate_index", 0)
        return self.planned_models[index] if index and index < len(self.planned_models) else "Bu sırada başka aday yok"

    def handle_event(self, event):
        kind = event.get("event")
        model = event.get("model") or self.current_model or event.get("requested_model", "")
        if self.phase == "stopping":
            self.log(f"Durdurma sırasında kayıt: {kind} • {model}")
            return
        purpose = event.get("purpose") or self.current_purpose
        if model:
            self.current_model = model
        if purpose:
            self.current_purpose = purpose
        provider = event.get("provider", event.get("requested_provider", ""))
        self.model_label.setText(f"{purpose} | {provider} / {model}".strip(" |/"))
        self.deadline = None
        self.response_deadline = None
        self.skip_button.setEnabled(False)
        self.phase = kind or "preparing"
        self.started_at = self.clock()
        self.timer.start()
        if kind == "plan":
            self.planned_models = [candidate["model"] for candidate in event.get("candidates", [])]
            self.attempted_models = []
            self.failed_models = []
            self.skipped_models = []
            self.last_failure = ""
            count = len(self.planned_models)
            first = self.planned_models[0] if count else "Uygun aday yok"
            self.title.setText("Bu adımın model sırası hazırlandı")
            self.queue_label.setText(f"{count} uygun model sıraya alındı • İlk aday: {first}")
            self.detail.setText("Her modelin sonucu ve neden başarısız olduğu aşağıya yazılacak.")
            self.tone("info")
            self.progress.setRange(0, 0)
            self.log(f"Bu adımın sırası ({count} model):")
            for number, candidate in enumerate(self.planned_models, 1):
                self.log(f"  {number}. {candidate}")
            if not event.get("automatic_fallback", True):
                self.log("Otomatik yedek seçimi ayarlarda kapalı.")
        elif kind == "attempt":
            self.current_request_id = event.get("request_id")
            self.response_limit = max(0, float(event.get("timeout_seconds", 0)))
            self.response_deadline = self.clock() + self.response_limit if self.response_limit else None
            has_next = not event.get("candidate_count") or event.get("candidate_index", 1) < event["candidate_count"]
            self.skip_button.setEnabled(bool(self.current_request_id) and has_next)
            self.skip_button.setToolTip("Bu isteği kapatıp sıradaki modeli dene. Gönderilmiş istek günlük kotaya sayılabilir." if has_next else
                                       "Bu sıradaki son model. Başka aday yok; beklemeyi bitirmek için Durdur'u kullanabilirsiniz.")
            if model not in self.attempted_models:
                self.attempted_models.append(model)
            self.update_queue(event)
            self.title.setText("Modelden yanıt bekleniyor")
            position = f"Model {event['candidate_index']}/{event['candidate_count']} • " if event.get("candidate_count") else ""
            self.detail.setText(position + f"Bu modelde deneme {event.get('attempt', 1)}/{event.get('attempt_limit', 1)}. Yanıt gelince ekrana eklenecek.")
            self.progress.setRange(0, 1000 if self.response_deadline else 0)
            self.progress.setValue(0)
            self.tone("info")
            self.log(f"Deneniyor: {model} (deneme {event.get('attempt', 1)})")
            notice = f"Şimdi deneniyor: {model} ({event.get('candidate_index', 1)}/{event.get('candidate_count', 1)})"
            if self.last_failure:
                notice += " • Önceki sonuç: " + self.last_failure
            self.notification.emit(notice, "info")
        elif kind == "failure":
            self.had_warning = True
            code = event.get("http_status")
            why = {"rate_limit": "kota / hız sınırı", "daily_quota": "ortak günlük kota dolu",
                   "upstream_rate_limit": "model sağlayıcısının ortak kapasite sınırı",
                   "account_rate_limit": "hesabın ortak hız sınırı", "temporary": "geçici servis hatası",
                   "configuration": "anahtar / hesap erişim hatası", "model_unavailable": "model kullanılamıyor",
                   "empty_response": "görünür yanıt metni yok", "response_timeout": "toplam yanıt süresi doldu",
                   "user_skip": "kullanıcı diğer modeli seçti"}.get(event.get("reason"), "yanıt alınamadı")
            if event.get("response_detail") == "meta_only":
                why = "yalnızca META takip alanları geldi; kullanıcıya gösterilecek görüş yok"
            text = f"{model}: {why}" + (f" (HTTP {code})" if code else "")
            self.last_failure = text
            self.failed_models.append(text)
            self.update_queue(event)
            self.title.setText("Model yanıt veremedi")
            self.detail.setText(text + ". Yeniden deneme veya yedek model adımı belirleniyor.")
            self.tone("warning")
            self.progress.setRange(0, 0)
            self.log(text)
            self.notification.emit(text + " • Sıradaki aday: " + self.next_candidate(event), "warning")
        elif kind == "retry":
            self.had_warning = True
            self.title.setText("Aynı model yeniden denenecek")
            self.detail.setText(f"{model} için kota beklemesi uygulanıyor; henüz başka modele geçilmedi.")
            self.tone("warning")
            self.set_wait(event.get("seconds", 0))
            text = f"Yeniden denenecek: {model} • bekleme {event.get('seconds', 0):g} sn"
            self.log(text)
            self.notification.emit(text, "warning")
        elif kind == "wait":
            self.title.setText("Bekleniyor — işlem devam ediyor")
            self.detail.setText(str(event.get("reason", "Çağrı aralığı")) + ". Süre bitince sıradaki deneme yapılacak; Durdur ile iptal edebilirsiniz.")
            self.tone("warning")
            self.set_wait(event.get("seconds", 0))
            self.log(f"Bekleme: {model} • {event.get('seconds', 0):g} sn")
        elif kind == "fallback":
            self.had_warning = True
            self.fallback_pending = True
            self.update_queue(event)
            self.title.setText("Yedek model seçildi")
            original = event.get("requested_model", "İlk model")
            text = (self.last_failure or (original + " yanıt veremedi")) + f" → denenecek yedek: {model}"
            self.detail.setText("Ajanın görevi korunuyor. Yedek modele çağrı aralığı tamamlanınca istek gönderilecek.")
            self.tone("warning")
            self.progress.setRange(0, 0)
            self.log(text)
            self.notification.emit(text, "warning")
        elif kind == "success":
            self.title.setText("Model yanıtı alındı")
            self.detail.setText("Yanıt kaydediliyor; sıradaki adıma geçilecek.")
            self.tone("success")
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000)
            self.log(f"Yanıt alındı: {model}")
            if self.fallback_pending:
                self.notification.emit(f"Yedek model yanıtladı: {model}. Tartışma devam ediyor.", "success")
                self.fallback_pending = False
        elif kind == "skipped":
            self.skipped_models.append(model)
            self.update_queue(event)
            why = {"model_cooldown": "önceki hata sonrası geçici beklemede",
                   "session_model_failed": "bu oturumda önceki deneme başarısız oldu veya atlandı",
                   "provider_blocked": "sağlayıcının ortak kotası veya hesap erişimi kapalı",
                   "not_in_catalog": "güncel ücretsiz katalogda bulunamadı"}.get(event.get("reason"), "bu adımda kullanılamıyor")
            if event.get("reason") == "session_model_failed":
                previous = {"empty_response": "boş yanıt", "response_timeout": "zaman aşımı",
                            "upstream_rate_limit": "sağlayıcı kapasitesi", "rate_limit": "hız/kota sınırı",
                            "user_skip": "kullanıcı atladı", "model_unavailable": "model kullanılamıyor",
                            "temporary": "geçici servis hatası"}.get(event.get("previous_reason"), "istek başarısız")
                if event.get("response_detail") == "meta_only":
                    previous = "yalnızca META yanıtı"
                why = f"önceki deneme • {event.get('previous_purpose', '')} • {previous}"
            self.last_failure = f"{model}: {why}"
            self.title.setText("Kullanılamayan model atlandı")
            self.detail.setText(self.last_failure + ". Uygun yedekler kontrol ediliyor.")
            self.log(f"Atlandı: {model} • {why}")
            self.tone("warning")
        self.tick()
        self.update_compact_view()

    def set_wait(self, seconds):
        self.duration = max(0.0, float(seconds))
        self.deadline = self.clock() + self.duration
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)

    def tick(self):
        if self.response_deadline is not None:
            remaining = max(0.0, self.response_deadline - self.clock())
            elapsed = max(0, int(self.clock() - self.started_at))
            self.timer_label.setText(f"Bu istek için kalan süre: {math.ceil(remaining)} sn • Geçen: {elapsed // 60:02d}:{elapsed % 60:02d} • Sınır: {self.response_limit:g} sn")
            self.progress.setValue(int(1000 * (1 - remaining / self.response_limit)))
            if not remaining:
                self.timer_label.setText("Süre doldu; istek kapatılıyor ve yedek seçenekler kontrol ediliyor...")
        elif self.deadline is not None:
            remaining = max(0.0, self.deadline - self.clock())
            self.timer_label.setText(f"Sonraki denemeye kalan: {math.ceil(remaining)} sn")
            self.progress.setValue(int(1000 * (1 - remaining / self.duration)) if self.duration else 1000)
            if not remaining:
                self.timer_label.setText("Bekleme bitti; istek başlangıcı bildirimi bekleniyor...")
        elif self.started_at is not None:
            elapsed = max(0, int(self.clock() - self.started_at))
            self.timer_label.setText(f"Bu adımda geçen süre: {elapsed // 60:02d}:{elapsed % 60:02d}")
            if self.phase == "attempt" and elapsed >= 30:
                self.timer_label.setText(self.timer_label.text() + " • Model yanıtı hâlâ bekleniyor; kesin kalan süre bilinmiyor.")

    def pause(self, round_number, missing=()):
        self.timer.stop()
        self.deadline = self.started_at = None
        self.response_deadline = None
        self.skip_button.setEnabled(False)
        self.phase = "round_incomplete" if missing else "paused"
        if missing:
            self.title.setText(f"Tur {round_number} eksik. Yanıt beklenen: {', '.join(missing)}")
            self.detail.setText("Eksik Yanıtları Dene aynı modelleri çağırır. Modeli Devret ile bir rol için yeni aday seçebilirsiniz.")
        else:
            self.title.setText(f"Tur {round_number} tamamlandı — devam etmeniz bekleniyor")
            self.detail.setText("Program duraklatıldı. Müdahalenizi yazabilir, ardından Sonraki Tura Geç'e basabilirsiniz.")
        self.timer_label.clear()
        self.progress.setRange(0, 1000)
        self.progress.setValue(1000)
        self.tone("warning" if missing else "success")
        self.update_compact_view()

    def stopping(self):
        self.deadline = None
        self.response_deadline = None
        self.skip_button.setEnabled(False)
        self.started_at = self.clock()
        self.phase = "stopping"
        self.title.setText("Durdurma istendi")
        self.detail.setText("Yeni model denenmeyecek. İstemci isteği güvenle kapatılıyor; kaydedilmiş yanıtlar korunuyor.")
        self.progress.setRange(0, 0)
        self.tone("warning")
        self.timer.start()
        self.update_compact_view()

    def skipping(self):
        self.deadline = self.response_deadline = None
        self.skip_button.setEnabled(False)
        self.phase = "skipping"
        self.title.setText("Bu model atlanıyor")
        self.detail.setText("Mevcut istemci isteği kapatılıyor; çağrı aralığı korunarak sıradaki uygun modele geçilecek.")
        self.progress.setRange(0, 0)
        self.tone("warning")
        self.update_compact_view()

    def finish(self, status):
        self.timer.stop()
        self.deadline = self.started_at = None
        self.response_deadline = None
        self.skip_button.setEnabled(False)
        self.phase = status
        titles = {"completed": "Oturum tamamlandı — rapor hazır", "stopped": "Durduruldu — kayıtlar korundu",
                  "summary_failed": "Turlar tamamlandı; özet üretilemedi", "failed": "Uygun modellerden yanıt alınamadı",
                  "review_required": "Son müdahale için insan incelemesi gerekli", "save_failed": "Dosyaya kayıt hatası"}
        self.title.setText(titles.get(status, "İşlem sona erdi"))
        self.detail.setText("Model denemeleri ve geçişler aşağıdaki geçmişte, tam günlük API Kullanımı ve oturum raporunda bulunur.")
        if status in ("failed", "summary_failed"):
            self.title.setText(f"Yanıt alınamadı — bu adımda {len(self.attempted_models)} model denendi")
            self.detail.setText(f"Planlanan: {len(self.planned_models)} • Denenen: {len(self.attempted_models)} • Atlanan: {len(self.skipped_models)}. "
                                + ("Son neden: " + self.last_failure if self.last_failure else "Ayrıntı için model günlüğüne bakın."))
        self.queue_label.setText(f"Son adım: {len(self.attempted_models)} model denendi; {len(self.skipped_models)} aday atlandı.")
        self.timer_label.clear()
        self.progress.setRange(0, 1000)
        self.progress.setValue(1000 if status == "completed" else 0)
        self.tone("success" if status == "completed" else "warning")
        self.update_compact_view()
        if self.had_warning or status != "completed":
            self.notification.emit(self.title.text() + (" • Son neden: " + self.last_failure if status != "completed" and self.last_failure else ". Model denemeleri kaydedildi."), "success" if status == "completed" else "warning")
