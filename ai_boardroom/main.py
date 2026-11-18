import sys
import queue
import os
import shutil
import json
import html
from copy import deepcopy

from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QTextEdit, QTextBrowser, QPushButton, QComboBox, QScrollArea,
    QGroupBox, QFormLayout, QSpinBox, QMessageBox, QTabWidget,
    QListWidget, QListWidgetItem, QFileDialog, QAbstractItemView, QFrame, QCompleter, QCheckBox, QDialog, QSizePolicy,
    QDialogButtonBox, QInputDialog
)

from .config import load_config, save_config, SESSIONS_DIR, OPENROUTER_MODELS, active_api_keys
from .orchestrator import DiscussionWorker, SummaryWorker
from .session_log import new_session_dir, build_summary_report, build_tracking_html, load_session_report, build_api_html
from .routing import DEFAULT_ROUTING, RoutePolicy
from .model_ui import ModelLibrary, ModelPicker
from .activity_ui import ActivityPanel
from .account_ui import AccountPanel

PROVIDER_OPTIONS = ["anthropic", "openai", "gemini", "groq", "openrouter"]
PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI (GPT)",
    "gemini": "Google (Gemini)",
    "groq": "Groq (Llama vb.)",
    "openrouter": "OpenRouter (Çeşitli)"
}
COLORS = ["#378ADD", "#D85A30", "#1D9E75", "#7F77DD", "#B3307E"]

PERSONA_TEMPLATES = {
    "Özel (Serbest)": {"name": "", "role": ""},
    "Mimar (Kodlama)": {"name": "Mimar", "role": "Sistem mimarı, performans ve veritabanı uzmanı. Sağlam altyapı tasarlar."},
    "Eleştirmen (Analiz)": {"name": "Eleştirmen", "role": "Kritik yaklaşım sergileyen, güvenlik açıklarını ve mantık hatalarını yakalayan uzman."},
    "Ürün-UX (Tasarım)": {"name": "Ürün-UX", "role": "Kullanıcı deneyimi, akış hızı ve işlevsellik odaklı ürün yöneticisi."},
    "Sanatçı (Görsel)": {"name": "Sanatçı", "role": "Estetik, renk teorisi ve görsel kompozisyon uzmanı. Görsel kaliteyi artırır."},
    "Müzisyen (Ses)": {"name": "Müzisyen", "role": "Ses tasarımı, ritim ve işitsel uyum konusunda uzman."},
    "Yazar (İçerik)": {"name": "Yazar", "role": "Etkili iletişim, marka dili ve metin akıcılığı konusunda uzman."}
}


def configure_model_suggestions(field, provider):
    if provider == "openrouter":
        completer = QCompleter(list(OPENROUTER_MODELS), field)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        field.setCompleter(completer)
        field.setToolTip("17 Kasım 2026'da gerçek API yanıtı alınan model kimlikleri:\n" +
                         "\n".join(OPENROUTER_MODELS) +
                         "\nBaşka bir geçerli OpenRouter kimliğini de yazabilirsiniz. Ücretsiz modellerde kota olabilir.")
    else:
        field.setCompleter(None)
        field.setToolTip("Seçilen sağlayıcının model kimliğini yazın.")


class PersonaRow(QFrame):
    def __init__(self, persona, color, remove_cb, up_cb, down_cb):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.color = color
        self.remove_cb = remove_cb
        self.up_cb = up_cb
        self.down_cb = down_cb

        layout = QFormLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.template_combo = QComboBox()
        self.template_combo.addItems(list(PERSONA_TEMPLATES.keys()))
        self.template_combo.currentIndexChanged.connect(self.on_template_changed)
        self.template_combo.setToolTip(
            "Dikkat: bir şablon seçmek İsim ve Rol/Bakış Açısı alanlarını\n"
            "uyarmadan siler ve şablonun metniyle değiştirir."
        )

        self.name_input = QLineEdit(persona.get("name", ""))
        self.role_input = QLineEdit(persona.get("role", ""))
        self.role_input.setToolTip(
            "Bu alan hem katılımcının kimliğini hem de cevap uzunluğunu/tonunu belirler.\n"
            "Örnek: 'Kısa ve öz cevap ver, 3 cümleyi geçme.'"
        )
        self.provider_combo = QComboBox()
        for p in PROVIDER_OPTIONS:
            self.provider_combo.addItem(PROVIDER_LABELS[p], p)
        idx = PROVIDER_OPTIONS.index(persona.get("provider", "openai")) if persona.get("provider") in PROVIDER_OPTIONS else 0
        self.provider_combo.setCurrentIndex(idx)
        self.model_input = QLineEdit(persona.get("model", ""))
        self.provider_combo.currentIndexChanged.connect(
            lambda: configure_model_suggestions(self.model_input, self.provider_combo.currentData()))
        configure_model_suggestions(self.model_input, self.provider_combo.currentData())

        header_layout = QHBoxLayout()
        self.name_label = QLabel(f"● Katılımcı")
        self.name_label.setStyleSheet(f"color:{color}; font-weight:bold;")
        self.up_btn = QPushButton("▲")
        self.up_btn.setFixedWidth(30)
        self.up_btn.clicked.connect(lambda: self.up_cb(self))
        self.down_btn = QPushButton("▼")
        self.down_btn.setFixedWidth(30)
        self.down_btn.clicked.connect(lambda: self.down_cb(self))
        self.remove_btn = QPushButton("Sil")
        self.remove_btn.setFixedWidth(50)
        self.remove_btn.clicked.connect(lambda: self.remove_cb(self))

        header_layout.addWidget(self.name_label)
        header_layout.addStretch()
        header_layout.addWidget(self.up_btn)
        header_layout.addWidget(self.down_btn)
        header_layout.addWidget(self.remove_btn)

        layout.addRow(header_layout)
        layout.addRow("Şablon (Alan):", self.template_combo)
        layout.addRow("İsim:", self.name_input)
        layout.addRow("Rol/Bakış Açısı:", self.role_input)
        layout.addRow("Sağlayıcı:", self.provider_combo)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_input)
        self.pick_model_btn = QPushButton("Model Seç")
        model_row.addWidget(self.pick_model_btn)
        layout.addRow("Model:", model_row)

    def on_template_changed(self):
        tmpl = self.template_combo.currentText()
        if tmpl in PERSONA_TEMPLATES and tmpl != "Özel (Serbest)":
            self.name_input.setText(PERSONA_TEMPLATES[tmpl]["name"])
            self.role_input.setText(PERSONA_TEMPLATES[tmpl]["role"])

    def to_dict(self):
        return {
            "name": self.name_input.text().strip() or "İsimsiz",
            "role": self.role_input.text().strip(),
            "provider": self.provider_combo.currentData(),
            "model": self.model_input.text().strip(),
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI-Boardroom 2.0 — İnsan denetimli müzakere")
        self.resize(1000, 750)

        self.cfg = load_config()
        self.cfg.setdefault("routing", deepcopy(DEFAULT_ROUTING))
        self.cfg.setdefault("paid_api_keys", {})
        self.cfg.setdefault("custom_models", [])
        self.pending_user_msgs = queue.Queue()
        self.worker = None
        self.summary_worker = None
        self.session_dir = None
        self.last_summary_md = ""
        self._summary_retry_failed = False

        self.tabs = QTabWidget()
        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(6, 6, 6, 6)
        self.activity_alert = QFrame()
        self.activity_alert.setObjectName("activityAlert")
        alert_layout = QHBoxLayout(self.activity_alert)
        self.activity_alert_label = QLabel()
        self.activity_alert_label.setTextFormat(Qt.TextFormat.PlainText)
        self.activity_alert_label.setWordWrap(True)
        alert_layout.addWidget(self.activity_alert_label, 1)
        details = QPushButton("Durumu Göster")
        details.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        alert_layout.addWidget(details)
        api_details = QPushButton("API Günlüğü")
        api_details.clicked.connect(lambda: self.tabs.setCurrentWidget(self.api_tab))
        alert_layout.addWidget(api_details)
        dismiss = QPushButton("Kapat")
        dismiss.clicked.connect(self.activity_alert.hide)
        alert_layout.addWidget(dismiss)
        self.activity_alert.hide()
        workspace_layout.addWidget(self.activity_alert)
        workspace_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(workspace)

        self.setup_boardroom_tab()
        self.setup_summary_tab()
        self.setup_history_tab()
        self.setup_settings_tab()
        self.model_library = ModelLibrary(self.cfg["custom_models"], auto_refresh=self.cfg.get("catalog_auto_refresh", True))
        self.model_library.changed.connect(self.on_catalog_changed)
        self.tabs.addTab(self.model_library, "Model Kütüphanesi")
        self.setup_tracking_tab()
        self.api_view = QTextBrowser()
        self.api_view.setHtml(build_api_html({"usage_mode": self.usage_mode_combo.currentData()}))
        self.api_tab = QWidget()
        api_layout = QVBoxLayout(self.api_tab)
        self.account_panel = AccountPanel(lambda mode: (self.key_inputs['openrouter'] if mode == 'free'
                                                        else self.paid_key_inputs['openrouter']).text().strip())
        api_layout.addWidget(self.account_panel)
        api_layout.addWidget(self.api_view, 1)
        self.tabs.addTab(self.api_tab, "API Kullanımı / Bakiye")
        self.tabs.currentChanged.connect(lambda: self.account_panel.refresh()
                                        if self.tabs.currentWidget() is self.api_tab else None)

        self.load_history_list()
        self.on_usage_mode_changed()

    def setup_boardroom_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        top_row = QHBoxLayout()
        self.project_input = QTextEdit()
        self.project_input.setPlaceholderText("Tartışılacak konuyu veya projeyi kısaca anlat...")
        self.project_input.setMaximumHeight(60)
        top_row.addWidget(self.project_input)
        layout.addLayout(top_row)

        self.activity_panel = ActivityPanel()
        self.activity_panel.notification.connect(self.on_activity_notification)
        self.activity_panel.skip_requested.connect(self.on_skip_model)
        self.activity_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.activity_panel)

        self.transcript_view = QTextBrowser()
        self.transcript_view.setPlaceholderText("Tartışma başladığında katılımcıların yanıtları burada görünecek.")
        self.transcript_view.setMinimumHeight(120)
        layout.addWidget(self.transcript_view, 1)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Hazır.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        self.layer_status_label = QLabel("Kullanım katmanı: Ücretsiz")
        status_row.addWidget(self.layer_status_label)
        layout.addLayout(status_row)
        self.session_location_label = QLabel(f"Kayıt kökü: {SESSIONS_DIR}")
        self.session_location_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.session_location_label.setWordWrap(True)
        layout.addWidget(self.session_location_label)

        input_row = QHBoxLayout()
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("İstediğin an araya girip yazabilirsin...")
        self.user_input.returnPressed.connect(self.on_send_user_msg)
        send_btn = QPushButton("Gönder")
        self.send_btn = send_btn
        send_btn.setEnabled(False)
        send_btn.clicked.connect(self.on_send_user_msg)
        self.objection_btn = QPushButton("İtiraz Gönder")
        self.objection_btn.setToolTip("Bu mesajı yanıtlara bağlanabilen bir kullanıcı itirazı olarak kaydet.")
        self.objection_btn.setEnabled(False)
        self.objection_btn.clicked.connect(lambda: self.on_send_user_msg(objection=True))
        input_row.addWidget(self.user_input)
        input_row.addWidget(send_btn)
        input_row.addWidget(self.objection_btn)
        layout.addLayout(input_row)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Tartışmayı Başlat")
        self.start_btn.clicked.connect(self.on_start)
        self.continue_btn = QPushButton("Sonraki Tura Geç")
        self.continue_btn.clicked.connect(self.on_continue)
        self.continue_btn.setEnabled(False)
        self.handoff_btn = QPushButton("Modeli Devret…")
        self.handoff_btn.clicked.connect(self.on_model_handoff)
        self.handoff_btn.hide()
        self.stop_btn = QPushButton("Durdur")
        self.stop_btn.clicked.connect(self.on_stop)
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.continue_btn)
        btn_row.addWidget(self.handoff_btn)
        btn_row.addWidget(self.stop_btn)
        export_transcript_btn = QPushButton("Konuşmayı Dışa Aktar")
        export_transcript_btn.clicked.connect(self.on_export_transcript)
        btn_row.addWidget(export_transcript_btn)
        layout.addLayout(btn_row)

        self.tabs.addTab(tab, "Tartışma Paneli")

    def setup_summary_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)

        info_label = QLabel(
            "Özeti çıkaracak sağlayıcı/model 'Ayarlar' sekmesinden değiştirilir."
        )
        info_label.setStyleSheet("color:#777; font-size:11px;")
        layout.addWidget(info_label)

        self.summary_view = QTextBrowser()
        layout.addWidget(self.summary_view)

        btn_row = QHBoxLayout()
        self.retry_summary_btn = QPushButton("Özeti Yeniden Oluştur")
        self.retry_summary_btn.setEnabled(False)
        self.retry_summary_btn.clicked.connect(self.on_retry_summary)
        btn_row.addWidget(self.retry_summary_btn)
        self.export_btn = QPushButton("Özeti Dışa Aktar (.md)")
        self.export_btn.clicked.connect(self.on_export_md)
        btn_row.addWidget(self.export_btn)
        layout.addLayout(btn_row)

        self.tabs.addTab(tab, "Özet / Plan")

    def setup_tracking_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        info = QLabel("Görüşler ve destekler modelin META kayıtlarından izlenir. Sessizlik onay sayılmaz; raporu inceleyin.")
        info.setWordWrap(True)
        layout.addWidget(info)
        self.tracking_view = QTextBrowser()
        layout.addWidget(self.tracking_view)
        row = QHBoxLayout()
        self.cancel_decision_combo = QComboBox()
        self.cancel_decision_btn = QPushButton("Seçili öneriyi iptal et")
        self.cancel_decision_btn.setEnabled(False)
        self.cancel_decision_btn.clicked.connect(self.on_cancel_decision)
        row.addWidget(self.cancel_decision_combo)
        row.addWidget(self.cancel_decision_btn)
        layout.addLayout(row)
        self.tabs.addTab(tab, "Müzakere Takibi")

    def on_cancel_decision(self):
        decision_id = self.cancel_decision_combo.currentData()
        if decision_id and self.worker and self.worker.accepting_messages:
            try:
                self.worker.submit_user_message(f"İPTAL: {decision_id}")
            except Exception:
                self.on_error("İptal talimatı kaydedilemedi. Disk alanını kontrol edin.")
                self.on_stop()

    def setup_history_tab(self):
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.history_list = QListWidget()
        self.history_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.history_list.currentItemChanged.connect(self.on_history_select)
        left_layout.addWidget(QLabel("Birden fazla seçim yapabilirsiniz (Ctrl/Shift):"))
        left_layout.addWidget(self.history_list)
        self.delete_history_btn = QPushButton("Seçili Kayıtları Sil")
        self.delete_history_btn.clicked.connect(self.on_delete_history)
        left_layout.addWidget(self.delete_history_btn)
        folder_label = QLabel(f"Kayıt klasörü: {SESSIONS_DIR}")
        folder_label.setWordWrap(True)
        folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left_layout.addWidget(folder_label)
        open_folder_btn = QPushButton("Seçili Kaydın Klasörünü Aç")
        open_folder_btn.clicked.connect(self.on_open_session_folder)
        left_layout.addWidget(open_folder_btn)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.history_view = QTextBrowser()
        right_layout.addWidget(self.history_view)
        self.export_history_btn = QPushButton("Geçmişi Dışa Aktar (.md)")
        self.export_history_btn.clicked.connect(self.on_export_history_md)
        right_layout.addWidget(self.export_history_btn)

        layout.addWidget(left_widget, 1)
        layout.addWidget(right_widget, 2)

        self.tabs.addTab(tab, "Geçmiş Kayıtlar")

    def setup_settings_tab(self):
        tab = QWidget()
        main_layout = QVBoxLayout(tab)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)

        route = self.cfg["routing"]
        mode_group = QGroupBox("Kullanım Katmanı")
        mode_form = QFormLayout(mode_group)
        self.usage_mode_combo = QComboBox()
        self.usage_mode_combo.addItem("Ücretsiz — yalnızca doğrulanmış sıfır fiyatlı OpenRouter modelleri", "free")
        self.usage_mode_combo.addItem("Ücretli / diğer sağlayıcılar — ayrı anahtar ve açık izin", "paid")
        self.usage_mode_combo.setCurrentIndex(1 if route.get("mode") == "paid" else 0)
        mode_form.addRow("Etkin katman:", self.usage_mode_combo)
        library_button = QPushButton("Model Kütüphanesini Aç")
        library_button.clicked.connect(lambda: self.tabs.setCurrentWidget(self.model_library))
        mode_form.addRow(library_button)
        layout.addWidget(mode_group)

        self.free_api_group = QGroupBox("Ücretsiz API — OpenRouter")
        free_form = QFormLayout(self.free_api_group)
        self.key_inputs = {"openrouter": QLineEdit(self.cfg["api_keys"].get("openrouter", ""))}
        self.key_inputs["openrouter"].setEchoMode(QLineEdit.EchoMode.Password)
        free_form.addRow("Ücretsiz kullanım anahtarı:", self.key_inputs["openrouter"])
        free_note = QLabel("OpenRouter teknik olarak tek anahtarla iki katmanı da destekler. Buradaki ayrım uygulamanın harcama sınırıdır: ücretsiz katman ücretli modele geçmez. Diğer sağlayıcıların hesabının ücretsiz olduğu model adından anlaşılamaz.")
        free_note.setWordWrap(True)
        free_form.addRow(free_note)
        self.free_pool = QListWidget()
        self.free_pool.setMaximumHeight(100)
        for identifier in route.get("free_fallbacks", OPENROUTER_MODELS):
            self.free_pool.addItem(identifier)
        free_form.addRow("Ücretsiz yedek havuzu (sıralı):", self.free_pool)
        free_buttons = QHBoxLayout()
        add_free = QPushButton("Ücretsiz Yedek Ekle")
        add_free.clicked.connect(lambda: self.add_fallback("free"))
        remove_free = QPushButton("Seçili Yedeği Kaldır")
        remove_free.clicked.connect(lambda: self.free_pool.takeItem(self.free_pool.currentRow()))
        free_buttons.addWidget(add_free)
        free_buttons.addWidget(remove_free)
        free_form.addRow(free_buttons)
        self.free_catalog_fallback_check = QCheckBox("Tercih edilen yedeklerden sonra kütüphanedeki diğer ücretsiz modelleri de dene")
        self.free_catalog_fallback_check.setChecked(route.get("free_catalog_fallback", True))
        free_form.addRow(self.free_catalog_fallback_check)
        self.free_retry_same_check = QCheckBox("Hata veren ücretsiz modeli önce yeniden dene (kapalıysa sıradaki modele geç)")
        self.free_retry_same_check.setChecked(route.get("free_retry_same_model", False))
        free_form.addRow(self.free_retry_same_check)
        self.free_max_fallback_spin = QSpinBox()
        self.free_max_fallback_spin.setRange(0, 100)
        self.free_max_fallback_spin.setSpecialValueText("Tüm uygun ücretsiz modeller")
        self.free_max_fallback_spin.setValue(route.get("free_max_fallbacks", 0))
        free_form.addRow("Ücretsiz yedek sınırı (0 = tümü):", self.free_max_fallback_spin)
        self.free_interval_spin = QSpinBox()
        self.free_interval_spin.setRange(4, 120)
        self.free_interval_spin.setSuffix(" sn")
        self.free_interval_spin.setValue(route.get("free_interval_seconds", 6))
        free_form.addRow("Çağrı başlangıçları arası en az:", self.free_interval_spin)
        preset = QPushButton("Katılımcıları ve Özeti Ücretsiz OpenRouter Modellerine Ayarla")
        preset.clicked.connect(self.apply_free_preset)
        free_form.addRow(preset)
        layout.addWidget(self.free_api_group)

        self.paid_api_group = QGroupBox("Ücretli / diğer API — ayrı yapılandırma")
        paid_form = QFormLayout(self.paid_api_group)
        self.allow_paid_check = QCheckBox("Bu katmandaki çağrıları ve hesabımın olası ücretlerini açıkça etkinleştiriyorum")
        self.allow_paid_check.setChecked(bool(route.get("paid_enabled", False)))
        paid_form.addRow(self.allow_paid_check)
        self.paid_key_inputs = {}
        for prov in PROVIDER_OPTIONS:
            inp = QLineEdit(self.cfg["paid_api_keys"].get(prov, ""))
            inp.setEchoMode(QLineEdit.EchoMode.Password)
            self.paid_key_inputs[prov] = inp
            paid_form.addRow(PROVIDER_LABELS[prov] + ":", inp)
        note = QLabel("Eski diğer sağlayıcı anahtarları korunur; ücretli alana otomatik taşınmaz ve kullanılmaz. Kullanmak istediğiniz anahtarları buraya girin. Gemini/Groq gibi servislerin ücretsiz hesabı da olabilir; ücretlendirme hesabınıza bağlıdır ve burada bilinmiyor olarak gösterilebilir.")
        note.setWordWrap(True)
        paid_form.addRow(note)
        self.paid_pool = QListWidget()
        self.paid_pool.setMaximumHeight(90)
        for item in route.get("paid_fallbacks", []):
            widget_item = QListWidgetItem(f"{item['provider']} / {item['model']}")
            widget_item.setData(Qt.ItemDataRole.UserRole, item)
            self.paid_pool.addItem(widget_item)
        paid_form.addRow("Açıkça seçilmiş ücretli yedekler:", self.paid_pool)
        paid_buttons = QHBoxLayout()
        add_paid = QPushButton("Ücretli / Diğer Yedek Ekle")
        add_paid.clicked.connect(lambda: self.add_fallback("paid"))
        remove_paid = QPushButton("Seçili Yedeği Kaldır")
        remove_paid.clicked.connect(lambda: self.paid_pool.takeItem(self.paid_pool.currentRow()))
        paid_buttons.addWidget(add_paid)
        paid_buttons.addWidget(remove_paid)
        paid_form.addRow(paid_buttons)
        self.max_fallback_spin = QSpinBox()
        self.max_fallback_spin.setRange(0, 5)
        self.max_fallback_spin.setSpecialValueText("Yedek yok")
        self.max_fallback_spin.setValue(route.get("max_fallbacks", 3))
        paid_form.addRow("Ücretli farklı yedek sınırı:", self.max_fallback_spin)
        self.paid_interval_spin = QSpinBox()
        self.paid_interval_spin.setRange(1, 120)
        self.paid_interval_spin.setValue(route.get("paid_interval_seconds", 3))
        self.paid_interval_spin.setSuffix(" sn")
        paid_form.addRow("Çağrı aralığı:", self.paid_interval_spin)
        layout.addWidget(self.paid_api_group)

        retry_group = QGroupBox("Hata ve yedek model yönetimi")
        retry_form = QFormLayout(retry_group)
        self.auto_fallback_check = QCheckBox("Hata devam ederse aynı katmandaki yedek modele geç")
        self.auto_fallback_check.setChecked(route.get("automatic_fallback", True))
        retry_form.addRow(self.auto_fallback_check)
        self.response_timeout_spin = QSpinBox()
        self.response_timeout_spin.setRange(30, 60)
        self.response_timeout_spin.setSingleStep(30)
        self.response_timeout_spin.setSuffix(" sn")
        self.response_timeout_spin.setValue(route.get("response_timeout_seconds", 60))
        retry_form.addRow("Bir model yanıtı için toplam süre sınırı:", self.response_timeout_spin)
        self.retry_count_spin = QSpinBox()
        self.retry_count_spin.setRange(0, 2)
        self.retry_count_spin.setValue(route.get("max_retries", 1))
        self.retry_count_spin.setToolTip("Ücretsiz katmanda bu sayı yalnızca 'Hata veren ücretsiz modeli önce yeniden dene' seçeneği açıksa uygulanır. Varsayılan kapalıdır; hata sonrası sıradaki modele geçilir.")
        retry_form.addRow("Aynı modelde ek deneme:", self.retry_count_spin)
        self.backoff_spin = QSpinBox()
        self.backoff_spin.setRange(10, 120)
        self.backoff_spin.setSuffix(" sn")
        self.backoff_spin.setValue(route.get("backoff_seconds", 20))
        retry_form.addRow("İlk hata sonrası bekleme:", self.backoff_spin)
        retry_note = QLabel("Ortak hız sınırında OpenRouter’ın bildirdiği bekleme süresi uygulanır. Başarısız veya gönderildikten sonra atlanan istekler günlük haktan düşebilir. Günlük kota ortak olduğu için model değişince yenilenmez.")
        retry_note.setWordWrap(True)
        retry_form.addRow(retry_note)
        layout.addWidget(retry_group)
        self.usage_mode_combo.currentIndexChanged.connect(self.on_usage_mode_changed)

        general_group = QGroupBox("Genel Ayarlar")
        general_layout = QHBoxLayout(general_group)
        general_layout.addWidget(QLabel("Tur Sayısı:"))
        self.rounds_spin = QSpinBox()
        self.rounds_spin.setRange(1, 10)
        self.rounds_spin.setValue(self.cfg.get("rounds", 2))
        self.rounds_spin.setToolTip(
            "Her ek tur, her katılımcı için ayrı bir API çağrısı demektir.\n"
            "Free-tier limitlerini hızlandırabilir."
        )
        general_layout.addWidget(self.rounds_spin)
        general_layout.addStretch()
        layout.addWidget(general_group)

        summary_group = QGroupBox("Özet Ayarları (özeti hangi model çıkarsın)")
        summary_form = QFormLayout(summary_group)
        self.summary_provider_combo = QComboBox()
        for p in PROVIDER_OPTIONS:
            self.summary_provider_combo.addItem(PROVIDER_LABELS[p], p)
        saved_summary_prov = self.cfg.get("summary_provider", "gemini")
        if saved_summary_prov in PROVIDER_OPTIONS:
            self.summary_provider_combo.setCurrentIndex(PROVIDER_OPTIONS.index(saved_summary_prov))
        self.summary_model_input = QLineEdit(self.cfg.get("summary_model", ""))
        self.summary_model_input.setPlaceholderText("Boş bırakılırsa varsayılan model kullanılır")
        self.summary_provider_combo.currentIndexChanged.connect(
            lambda: configure_model_suggestions(self.summary_model_input, self.summary_provider_combo.currentData()))
        configure_model_suggestions(self.summary_model_input, self.summary_provider_combo.currentData())
        summary_form.addRow("Sağlayıcı:", self.summary_provider_combo)
        summary_model_row = QHBoxLayout()
        summary_model_row.addWidget(self.summary_model_input)
        summary_pick = QPushButton("Model Seç")
        summary_pick.clicked.connect(self.pick_summary_model)
        summary_model_row.addWidget(summary_pick)
        summary_form.addRow("Model:", summary_model_row)
        summary_hint = QLabel(
            "Özet de seçilen katmanın kurallarını kullanır. Yalnızca açıkça seçilmiş aynı katman yedekleri denenir; "
            "ücretsizden ücretliye otomatik geçiş yapılmaz."
        )
        summary_hint.setWordWrap(True)
        summary_hint.setStyleSheet("color:#777; font-size:11px;")
        summary_form.addRow(summary_hint)
        layout.addWidget(summary_group)

        self.persona_group = QGroupBox("Katılımcılar (Sıralamayı Yukarı/Aşağı Butonlarıyla Değiştirin)")
        self.persona_layout = QVBoxLayout(self.persona_group)
        self.persona_rows = []
        for p in self.cfg.get("personas", []):
            self.add_persona_row(p)
        layout.addWidget(self.persona_group)

        btn_layout = QHBoxLayout()
        add_persona_btn = QPushButton("+ Yeni Katılımcı Ekle")
        add_persona_btn.clicked.connect(lambda: self.add_persona_row())
        info_btn = QPushButton("ℹ️ Sıralama Stratejileri (Tavsiye)")
        info_btn.setStyleSheet("color: #2b78e4; font-weight: bold;")
        info_btn.clicked.connect(self.show_sorting_info)
        btn_layout.addWidget(add_persona_btn)
        btn_layout.addWidget(info_btn)
        layout.addLayout(btn_layout)

        save_btn = QPushButton("Ayarları Kaydet")
        save_btn.setStyleSheet("font-weight: bold; padding: 10px; margin-top: 15px;")
        save_btn.clicked.connect(lambda: self.on_save_settings())
        layout.addWidget(save_btn)

        layout.addStretch()
        scroll.setWidget(content_widget)
        main_layout.addWidget(scroll)

        self.tabs.addTab(tab, "Ayarlar")

    def on_usage_mode_changed(self):
        free = self.usage_mode_combo.currentData() == "free"
        self.free_api_group.setEnabled(free)
        self.paid_api_group.setEnabled(not free)
        if not self._busy():
            self.layer_status_label.setText("Kullanım katmanı: " + ("Ücretsiz" if free else "Ücretli / diğer — açık izin gerekli"))
        if hasattr(self, "model_library"):
            self.on_catalog_changed()

    def on_catalog_changed(self):
        mode = self.usage_mode_combo.currentData()
        models = self.model_library.entries()
        openrouter_ids = {e["id"] for e in models if e["provider"] == "openrouter"}
        free_ids = {e["id"] for e in models if e["provider"] == "openrouter" and e["tier"] == "free"}
        for index in range(self.free_pool.count()):
            item = self.free_pool.item(index)
            item.setHidden(item.text() not in free_ids)
        for index in range(self.paid_pool.count()):
            item = self.paid_pool.item(index)
            route = item.data(Qt.ItemDataRole.UserRole)
            item.setHidden(route["provider"] == "openrouter" and route["model"] not in openrouter_ids)
        entries = [e for e in models if (e.get("tier") == "free") == (mode == "free")]
        for field, provider in [(row.model_input, row.provider_combo.currentData()) for row in self.persona_rows] + [
                (self.summary_model_input, self.summary_provider_combo.currentData())]:
            identifiers = [e["id"] for e in entries if e["provider"] == provider]
            if identifiers:
                completer = QCompleter(identifiers, field)
                completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                field.setCompleter(completer)
            else:
                field.setCompleter(None)

    def choose_model(self, mode=None):
        mode = mode or self.usage_mode_combo.currentData()
        entries = self.model_library.entries()
        if not entries:
            QMessageBox.information(self, "Model kütüphanesi", "Önce Model Kütüphanesi'nden Kataloğu Yenile düğmesine basın.")
            self.tabs.setCurrentWidget(self.model_library)
            return None
        picker = ModelPicker(entries, mode, self)
        return picker.selected if picker.exec() == QDialog.DialogCode.Accepted else None

    def pick_persona_model(self, row):
        entry = self.choose_model()
        if entry:
            row.provider_combo.setCurrentIndex(PROVIDER_OPTIONS.index(entry["provider"]))
            row.model_input.setText(entry["id"])

    def pick_summary_model(self):
        entry = self.choose_model()
        if entry:
            self.summary_provider_combo.setCurrentIndex(PROVIDER_OPTIONS.index(entry["provider"]))
            self.summary_model_input.setText(entry["id"])

    def add_fallback(self, mode):
        entry = self.choose_model(mode)
        if not entry:
            return
        if mode == "free":
            if entry["provider"] == "openrouter" and not any(self.free_pool.item(i).text() == entry["id"] for i in range(self.free_pool.count())):
                self.free_pool.addItem(entry["id"])
        else:
            value = {"provider": entry["provider"], "model": entry["id"]}
            if not any(self.paid_pool.item(i).data(Qt.ItemDataRole.UserRole) == value for i in range(self.paid_pool.count())):
                item = QListWidgetItem(f"{value['provider']} / {value['model']}")
                item.setData(Qt.ItemDataRole.UserRole, value)
                self.paid_pool.addItem(item)

    def apply_free_preset(self):
        self.usage_mode_combo.setCurrentIndex(0)
        for index, row in enumerate(self.persona_rows):
            row.provider_combo.setCurrentIndex(PROVIDER_OPTIONS.index("openrouter"))
            row.model_input.setText(OPENROUTER_MODELS[index % len(OPENROUTER_MODELS)])
        self.summary_provider_combo.setCurrentIndex(PROVIDER_OPTIONS.index("openrouter"))
        self.summary_model_input.setText(OPENROUTER_MODELS[0])

    def collect_settings(self):
        self.cfg["api_keys"]["openrouter"] = self.key_inputs["openrouter"].text().strip()
        self.cfg["paid_api_keys"] = {p: field.text().strip() for p, field in self.paid_key_inputs.items()}
        self.cfg["routing"] = {"mode": self.usage_mode_combo.currentData(), "paid_enabled": self.allow_paid_check.isChecked(),
            "automatic_fallback": self.auto_fallback_check.isChecked(), "free_interval_seconds": self.free_interval_spin.value(),
            "paid_interval_seconds": self.paid_interval_spin.value(), "max_retries": self.retry_count_spin.value(),
            "backoff_seconds": self.backoff_spin.value(), "max_fallbacks": self.max_fallback_spin.value(),
            "free_catalog_fallback": self.free_catalog_fallback_check.isChecked(),
            "free_retry_same_model": self.free_retry_same_check.isChecked(),
            "free_max_fallbacks": self.free_max_fallback_spin.value(),
            "response_timeout_seconds": self.response_timeout_spin.value(),
            "free_fallbacks": [self.free_pool.item(i).text() for i in range(self.free_pool.count())],
            "paid_fallbacks": [self.paid_pool.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.paid_pool.count())]}
        self.cfg["custom_models"] = deepcopy(self.model_library.custom_models)
        self.cfg["rounds"] = self.rounds_spin.value()
        self.cfg["summary_provider"] = self.summary_provider_combo.currentData()
        self.cfg["summary_model"] = self.summary_model_input.text().strip()
        self.cfg["personas"] = [row.to_dict() for row in self.persona_rows]

    def show_sorting_info(self):
        info_text = """Yapay zeka modelleri 'Çıpalama Etkisi' (Anchoring Effect) ile çalışır. İlk konuşan model, tartışmanın sınırlarını ve ana odağını belirler.

🛠️ Mühendislik ve Yazılım Projeleri İçin:
1. Mimar (Temeli atar)
2. Eleştirmen (Açıkları bulur)
3. Ürün-UX (Kullanıcıya uyarlar)

🎨 Tasarım ve İçerik Projeleri İçin:
1. Yazar / Sanatçı (Vizyonu belirler)
2. Ürün-UX (Akışı planlar)
3. Eleştirmen (Hataları arar)

💡 Unutmayın: Toplantının 'başkanı' her zaman 1. sıradaki kişidir!"""
        QMessageBox.information(self, "En Verimli Sıralama Stratejileri", info_text)

    def add_persona_row(self, persona_data=None):
        if len(self.persona_rows) >= 5:
            QMessageBox.warning(self, "Uyarı", "Maksimum 5 katılımcı ekleyebilirsiniz.")
            return
        if not persona_data:
            persona_data = {"name": "Yeni Katılımcı", "role": "", "provider": "openrouter", "model": OPENROUTER_MODELS[0]}
        color = COLORS[len(self.persona_rows) % len(COLORS)]
        row = PersonaRow(persona_data, color, self.remove_persona_row, self.move_persona_up, self.move_persona_down)
        row.pick_model_btn.clicked.connect(lambda: self.pick_persona_model(row))
        self.persona_rows.append(row)
        self.persona_layout.addWidget(row)
        self.update_row_buttons()

    def remove_persona_row(self, row_widget):
        if len(self.persona_rows) <= 1:
            QMessageBox.warning(self, "Uyarı", "En az 1 katılımcı olmalıdır.")
            return
        self.persona_layout.removeWidget(row_widget)
        self.persona_rows.remove(row_widget)
        row_widget.deleteLater()
        self.update_row_buttons()

    def move_persona_up(self, row_widget):
        idx = self.persona_rows.index(row_widget)
        if idx > 0:
            self.persona_rows.insert(idx - 1, self.persona_rows.pop(idx))
            self.persona_layout.insertWidget(idx - 1, row_widget)
            self.update_row_buttons()

    def move_persona_down(self, row_widget):
        idx = self.persona_rows.index(row_widget)
        if idx < len(self.persona_rows) - 1:
            self.persona_rows.insert(idx + 1, self.persona_rows.pop(idx))
            self.persona_layout.insertWidget(idx + 1, row_widget)
            self.update_row_buttons()

    def update_row_buttons(self):
        can_remove = len(self.persona_rows) > 1
        for i, row in enumerate(self.persona_rows):
            row.remove_btn.setEnabled(can_remove)
            row.up_btn.setEnabled(i > 0)
            row.down_btn.setEnabled(i < len(self.persona_rows) - 1)
            row.color = COLORS[i % len(COLORS)]
            row.name_label.setStyleSheet(f"color:{row.color}; font-weight:bold;")

    def load_history_list(self):
        self.history_list.clear()
        self.history_view.clear()
        if not SESSIONS_DIR.exists():
            return
        dirs = sorted([d for d in SESSIONS_DIR.iterdir() if d.is_dir()], reverse=True)
        for d in dirs:
            self.history_list.addItem(d.name)

    def on_history_select(self, current, previous):
        if not current:
            return
        session_name = current.text()
        try:
            report, state = load_session_report(SESSIONS_DIR / session_name)
            self.history_view.setMarkdown(report)
            if state.get("status"):
                self.history_view.append(build_tracking_html(state))
                self.history_view.append(build_api_html(state))
        except Exception:
            self.history_view.setPlainText("Kayıt okunamadı. Dosyaları veya disk erişimini kontrol edin.")

    def on_delete_history(self):
        if self._busy():
            return
        selected_items = self.history_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "Uyarı", "Silmek için listeden kayıt seçmelisiniz.")
            return
        count = len(selected_items)
        reply = QMessageBox.question(
            self, "Kayıt Silme Onayı",
            f"Seçili {count} kayıt tamamen silinecek. Emin misiniz?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            for item in selected_items:
                session_name = item.text()
                session_path = SESSIONS_DIR / session_name
                try:
                    if session_path.exists() and session_path.is_dir():
                        shutil.rmtree(session_path)
                except Exception:
                    pass
            self.load_history_list()
            self.history_view.clear()

    def on_open_session_folder(self):
        current = self.history_list.currentItem()
        folder = SESSIONS_DIR / current.text() if current else SESSIONS_DIR
        if not folder.exists() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(self, "Kayıt klasörü", "Klasör açılamadı. Ekrandaki kayıt yolunu kullanabilirsiniz.")

    def on_export_history_md(self):
        current = self.history_list.currentItem()
        if not current:
            return
        session_name = current.text()
        session_path = SESSIONS_DIR / session_name
        try:
            content, _ = load_session_report(session_path)
        except Exception:
            QMessageBox.warning(self, "Kayıt", "Oturum kaydı okunamadı.")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Geçmişi Kaydet",
            f"{session_name}_tam_kayit.md",
            "Markdown Dosyaları (*.md);;Tüm Dosyalar (*)"
        )
        if file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception:
                pass

    def on_export_md(self):
        if not self.last_summary_md:
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Özeti Kaydet", "",
            "Markdown Dosyaları (*.md);;Tüm Dosyalar (*)"
        )
        if file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(self.last_summary_md)
            except Exception:
                pass

    def on_save_settings(self, show_message=True):
        self.collect_settings()
        try:
            save_config(self.cfg)
        except Exception:
            QMessageBox.critical(self, "Ayarlar", "Ayarlar kaydedilemedi. Disk erişimini kontrol edin.")
            return False
        if show_message:
            QMessageBox.information(self, "Başarılı", "Ayarlar kaydedildi. Ücretsiz ve ücretli anahtarlar ayrı tutulur.")
        return True

    def append_message(self, entry):
        color = "#888888"
        model_txt = ""
        if entry["speaker"] == "Sen":
            color = "#444444"
        else:
            idx_map = {row.to_dict()["name"]: i for i, row in enumerate(self.persona_rows)}
            if entry["speaker"] in idx_map:
                color = COLORS[idx_map[entry["speaker"]] % len(COLORS)]
            if entry.get("model"):
                prov_label = PROVIDER_LABELS.get(entry.get("provider", ""), entry.get("provider", ""))
                model_txt = f" <span style='color:#999; font-size:11px'>· {prov_label} ({html.escape(entry['model'])})</span>"

        round_txt = f"Tur {entry['round']}" if entry.get("round") else ""
        references = " · ".join(entry[key] for key in ("message_id", "user_objection_id") if entry.get(key))
        if references:
            round_txt += " · " + html.escape(references)
        route_note = ""
        if entry.get("fallback_used"):
            requested = html.escape(entry.get("requested_model", "bilinmiyor"))
            route_note = f"<br><small>İstenen: {requested} → Yedek model yanıtladı; ajan rolü korundu.</small>"
        if entry.get("model_handoff"):
            handoff = entry["model_handoff"]
            old = html.escape(handoff["from"]["actual_model"])
            new = html.escape(handoff["to"]["actual_model"])
            route_note = f"<br><small>Kullanıcı seçimiyle model devri: {old} → {new}</small>"
        rendered_html = (
            f"<p><b style='color:{color}'>{html.escape(entry['speaker'])}</b>{model_txt} "
            f"<span style='color:#999; font-size:11px'>{round_txt}</span>{route_note}<br>"
            f"{html.escape(entry['text']).replace(chr(10), '<br>')}</p><hr>"
        )
        self.transcript_view.append(rendered_html)

    def on_export_transcript(self):
        if not self.worker:
            return
        file_path, _ = QFileDialog.getSaveFileName(self, "Oturum Kaydı", "oturum.md",
                                                 "Markdown (*.md);;JSON (*.json)")
        if not file_path:
            return
        try:
            with self.worker._lock:
                state = self.worker.snapshot()
                if file_path.lower().endswith(".json"):
                    content = json.dumps({"project": self.worker.project,
                                          "transcript": self.worker.transcript, **state}, ensure_ascii=False, indent=2)
                else:
                    content = build_summary_report(self.worker.project, self.worker.transcript,
                        self.worker.summary_text or "Nihai rapor oluşturulmadı.",
                        self.worker.summary_source or "—", session_status=self.worker.session_status, state=state)
            with open(file_path, "w", encoding="utf-8") as stream:
                stream.write(content)
        except Exception:
            QMessageBox.critical(self, "Dışa aktarma", "Dosya yazılamadı; farklı bir kayıt konumu seçin.")

    def on_send_user_msg(self, *, objection=False):
        text = self.user_input.text().strip()
        if not text:
            return
        if not self.worker or not self.worker.accepting_messages:
            QMessageBox.information(self, "Müdahale", "Müdahale için çalışan bir tartışma gerekli.")
            return
        try:
            if self.worker.submit_user_message(text, objection=objection):
                self.user_input.clear()
        except Exception:
            QMessageBox.critical(self, "Kayıt hatası", "Mesaj kaydedilemedi. Tartışmayı durdurun.")
            self.on_stop()

    def on_start(self):
        if self._busy():
            return
        project = self.project_input.toPlainText().strip()
        if not project:
            QMessageBox.warning(self, "Eksik bilgi", "Konu veya proje açıklaması giriniz.")
            return
        personas = [row.to_dict() for row in self.persona_rows]
        names = [p["name"].casefold() for p in personas]
        if not personas or len(names) != len(set(names)) or "sen" in names:
            QMessageBox.warning(self, "Katılımcılar", "Katılımcı isimleri benzersiz olmalı; Sen adı ayrılmıştır.")
            return
        self.collect_settings()
        route = self.cfg["routing"]
        if route["mode"] == "paid" and not route["paid_enabled"]:
            QMessageBox.warning(self, "Ücretli katman kapalı", "Ücretli / diğer API bölümünde açık kullanım iznini etkinleştirin.")
            return
        if route["mode"] == "free" and (any(p["provider"] != "openrouter" for p in personas) or self.cfg["summary_provider"] != "openrouter"):
            QMessageBox.warning(self, "Ücretsiz katman", "Ücretsiz modda yalnızca doğrulanmış OpenRouter modelleri kullanılır. Ayarlardaki ücretsiz şablon düğmesiyle sağlayıcıları değiştirebilir veya Model Seç'i kullanabilirsiniz. Eski ayarlar kendiliğinden değiştirilmez.")
            return
        keys = active_api_keys(self.cfg)
        required = {p["provider"] for p in personas} | {self.cfg["summary_provider"]}
        if any(not keys.get(provider) for provider in required):
            QMessageBox.warning(self, "API anahtarı", "Seçili katmanda kullanılan sağlayıcıların anahtarlarını girin.")
            return
        if not self.on_save_settings(show_message=False):
            return
        rounds = self.rounds_spin.value()

        self.transcript_view.clear()
        self.summary_view.clear()
        self.last_summary_md = ""
        self.session_dir = new_session_dir(project)
        self.session_location_label.setText(f"Bu oturumun kaydı: {self.session_dir}")
        self.activity_alert.hide()
        self.activity_panel.begin()

        while not self.pending_user_msgs.empty():
            try:
                self.pending_user_msgs.get_nowait()
            except queue.Empty:
                break

        self.worker = DiscussionWorker(
            project=project,
            personas=personas,
            rounds=rounds,
            api_keys=active_api_keys(self.cfg),
            pending_user_msgs=self.pending_user_msgs,
            summary_provider=self.cfg.get("summary_provider"),
            summary_model=self.cfg.get("summary_model"),
            session_path=self.session_dir,
            routing_config=deepcopy(self.cfg["routing"]),
        )
        self.worker.message_ready.connect(self.append_message)
        self.worker.status.connect(self.status_label.setText)
        self.worker.round_paused.connect(self.on_round_paused)
        self.worker.finished_all.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.state_changed.connect(self.on_state_changed)
        self.worker.routing_event.connect(self.on_route_activity)
        self.worker.start()
        self.send_btn.setEnabled(True)
        self.objection_btn.setEnabled(True)
        self.retry_summary_btn.setEnabled(False)
        self.project_input.setReadOnly(True)
        self.delete_history_btn.setEnabled(False)

        self.start_btn.setEnabled(False)
        self.continue_btn.setText("Sonraki Tura Geç")
        self.continue_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.tabs.setCurrentIndex(0)

    def on_round_paused(self, rnd):
        if self.worker and self.worker._stop_event.is_set():
            self.continue_btn.setEnabled(False)
            self.handoff_btn.hide()
            self.activity_panel.stopping()
            return
        missing = self.worker.handoff_targets() if self.worker else []
        self.activity_panel.pause(rnd, missing)
        self.continue_btn.setText("Eksik Yanıtları Dene" if missing else "Sonraki Tura Geç")
        self.status_label.setText("Aynı modeli yeniden deneyebilir veya eksik bir rolü başka modele devredebilirsiniz."
                                  if missing else f"Tur {rnd} tamamlandı. Sonraki Tura Geç'e basın.")
        self.continue_btn.setEnabled(True)
        self.handoff_btn.setVisible(bool(missing))

    def on_model_handoff(self):
        worker = self.worker
        if not worker:
            return
        targets = worker.handoff_targets()
        if not targets:
            return
        if len(targets) == 1:
            speaker = targets[0]
        else:
            speaker, approved = QInputDialog.getItem(self, "Modeli Devret", "Yanıtı eksik rol:", targets, 0, False)
            if not approved:
                return
        entries = worker.handoff_options(speaker, self.model_library.entries())
        if not entries:
            QMessageBox.information(self, "Modeli Devret",
                "Bu oturumun kullanım katmanında, henüz denenmemiş uygun aday bulunamadı. Model Kütüphanesi'ni yenileyebilir veya aynı modeli yeniden deneyebilirsiniz.")
            return
        previous = worker.role_models[speaker]["actual_model"]
        picker = ModelPicker(entries, worker.routing_config["mode"], self)
        picker.setWindowTitle(f"Modeli Devret · {speaker}")
        picker.findChild(QLabel).setText(f"Sabit model: {previous}. Seçtiğiniz aday bir kez denenir. "
                                         "Geçerli yanıt gelirse rolü devralır.")
        buttons = picker.findChild(QDialogButtonBox)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Seçilen Modeli Dene")
        if picker.exec() == QDialog.DialogCode.Accepted and picker.selected:
            if worker.request_handoff(speaker, picker.selected):
                self.continue_btn.setEnabled(False)
                self.handoff_btn.hide()
            else:
                QMessageBox.information(self, "Modeli Devret", "Bu rol veya model artık devir için uygun değil.")

    def on_continue(self):
        retry = self.worker and self.worker.session_status == "round_incomplete"
        self.activity_panel.begin("Bu turun eksik yanıtları yeniden deneniyor..." if retry
                                  else "Sonraki tur hazırlanıyor...", clear=False)
        self.continue_btn.setEnabled(False)
        self.handoff_btn.hide()
        if self.worker:
            self.worker.resume()

    def _busy(self):
        return bool((self.worker and self.worker.isRunning()) or
                    (self.summary_worker and self.summary_worker.isRunning()))

    def on_stop(self):
        if self.worker:
            self.worker.stop()
        self.stop_btn.setEnabled(False)
        self.handoff_btn.hide()
        self.send_btn.setEnabled(False)
        self.objection_btn.setEnabled(False)
        self.cancel_decision_btn.setEnabled(False)
        self.status_label.setText("Durdurma istendi; istemci isteği güvenle kapatılıyor...")
        self.activity_panel.stopping()
        self.show_activity_alert("Durdurma istendi. Yeni model denenmeyecek; istemci isteği kapatılıyor.", "warning")

    def on_skip_model(self):
        request_id = self.activity_panel.current_request_id
        if self.worker and request_id and self.worker.request_skip_current_model(request_id):
            self.activity_panel.skipping()
            self.show_activity_alert("Bu model atlanıyor. Mevcut istek kapatıldıktan sonra sıradaki uygun model denenecek.", "warning")
        else:
            self.activity_panel.skip_button.setEnabled(False)
            self.show_activity_alert("Bu istek zaten sona erdi veya değişti; güncel model bildirimi bekleniyor.", "info")

    def show_activity_alert(self, text, tone="warning"):
        colors = {"warning": ("#392b16", "#c28a32", "#ffe7ba"),
                  "success": ("#16352d", "#4ca88b", "#d4fff0"),
                  "info": ("#142a40", "#4594db", "#e1efff")}
        background, border, foreground = colors.get(tone, colors["warning"])
        self.activity_alert.setStyleSheet(f"QFrame#activityAlert{{background:{background};border:1px solid {border};border-radius:5px}}"
                                          f"QFrame#activityAlert QLabel{{color:{foreground};font-weight:600;border:0}}")
        self.activity_alert_label.setText(text)
        self.activity_alert.show()

    def on_activity_notification(self, text, tone):
        if self.tabs.currentIndex() == 0:
            self.activity_alert.hide()
        else:
            self.show_activity_alert(text, tone)

    def on_route_activity(self, event):
        self.activity_panel.handle_event(event)

    def on_state_changed(self, state):
        self.api_view.setHtml(build_api_html(state))
        self.layer_status_label.setText("Bu oturumun katmanı: " + ("Ücretsiz" if state.get("usage_mode") == "free" else "Ücretli / diğer"))
        self.send_btn.setEnabled(bool(self.worker and self.worker.accepting_messages))
        self.objection_btn.setEnabled(self.send_btn.isEnabled())
        self.tracking_view.setHtml(build_tracking_html(state))
        selected = self.cancel_decision_combo.currentData()
        self.cancel_decision_combo.clear()
        for decision in state.get("decisions", []):
            if decision["durum"] != "İPTAL":
                self.cancel_decision_combo.addItem(f"{decision['id']}: {decision['ozet']}", decision["id"])
        index = self.cancel_decision_combo.findData(selected)
        if index >= 0:
            self.cancel_decision_combo.setCurrentIndex(index)
        self.cancel_decision_btn.setEnabled(bool(self.worker and self.worker.accepting_messages
                                                 and self.cancel_decision_combo.count()))

    def on_worker_finished(self):
        self.activity_panel.finish(self.worker.session_status)
        if self.worker.session_status == "completed":
            self.show_activity_alert("Oturum tamamlandı. Rapor hazır; model denemeleri ve geçişler kaydedildi.", "success")
        self.start_btn.setEnabled(True)
        self.continue_btn.setEnabled(False)
        self.handoff_btn.hide()
        self.stop_btn.setEnabled(False)
        self.send_btn.setEnabled(False)
        self.objection_btn.setEnabled(False)
        self.cancel_decision_btn.setEnabled(False)
        self.project_input.setReadOnly(False)
        self.delete_history_btn.setEnabled(True)
        self.retry_summary_btn.setEnabled(bool(self.worker and
            self.worker.completed_rounds == self.worker.rounds and
            self.worker.session_status in ("completed", "summary_failed")))
        labels = {"stopped": "Durduruldu — eksik oturum; mesajlar korundu.",
                  "review_required": "Son müdahale değerlendirilmedi; nihai rapor üretilmedi.",
                  "summary_failed": "Turlar tamamlandı; nihai rapor oluşturulamadı.",
                  "failed": "Hata — eksik oturum; kaydedilen mesajlar korundu.",
                  "save_failed": "Kayıt hatası — konuşmayı dışa aktarın."}
        if self.worker.session_status in labels:
            self.status_label.setText(labels[self.worker.session_status])
        self.load_history_list()

    def on_retry_summary(self):
        if self._busy() or not self.worker:
            return
        if self.worker.completed_rounds != self.worker.rounds or self.worker.session_status not in ("completed", "summary_failed"):
            QMessageBox.warning(self, "Eksik oturum", "Nihai rapor için tüm turlar tamamlanmış olmalı.")
            return
        self.collect_settings()
        if self.cfg["routing"]["mode"] != self.worker.routing_config["mode"]:
            QMessageBox.warning(self, "Katman değişti", "Bu oturumun özeti aynı kullanım katmanında üretilmelidir. Katman değişikliği için yeni oturum açın.")
            return
        if self.cfg["routing"]["mode"] == "paid" and not self.cfg["routing"]["paid_enabled"]:
            QMessageBox.warning(self, "Ücretli katman", "Ücretli çağrı izni kapalı.")
            return
        self.worker.api_keys = active_api_keys(self.cfg)
        self.worker.route_policy = RoutePolicy(deepcopy(self.cfg["routing"]))
        self.worker.routing_config = self.worker.route_policy.config
        self.status_label.setText("Özet arka planda yeniden oluşturuluyor...")
        self.worker._stop_event.clear()
        self.activity_panel.begin("Özet modeli ve yedekler kontrol ediliyor...", clear=False)
        self.show_activity_alert("Özet yeniden oluşturuluyor. Canlı durumu izleyebilir veya API günlüğünü açabilirsiniz.", "info")
        self.summary_worker = SummaryWorker(self.worker, self.cfg.get("summary_provider"), self.cfg.get("summary_model"))
        self._summary_retry_failed = False
        self.summary_worker.result.connect(self.on_summary_rebuilt)
        self.summary_worker.error.connect(self.on_summary_retry_error)
        self.summary_worker.finished.connect(self.on_summary_thread_finished)
        self.start_btn.setEnabled(False)
        self.retry_summary_btn.setEnabled(False)
        self.delete_history_btn.setEnabled(False)
        self.summary_worker.start()

    def on_summary_rebuilt(self, text, source):
        try:
            self.worker.save_summary_result(text, source)
            self.on_finished(text, self.worker.transcript, source)
        except Exception:
            self._summary_retry_failed = True
            self.on_error("Yeni rapor kaydedilemedi. Önceki raporu ve disk alanını kontrol edin.")

    def on_summary_thread_finished(self):
        self.on_worker_finished()
        if self._summary_retry_failed:
            self.activity_panel.finish("summary_failed")
            self.status_label.setText("Yeni özet üretilemedi; önceki rapor korundu. Nedenler model günlüğünde.")

    def on_summary_retry_error(self, message):
        self._summary_retry_failed = True
        self.activity_panel.history_toggle.setChecked(True)
        self.show_activity_alert(message, "warning")

    def closeEvent(self, event):
        if self.account_panel.worker and self.account_panel.worker.isRunning():
            event.ignore()
            self.account_panel.timer.stop()
            QTimer.singleShot(250, self.close)
            return
        if self.model_library.worker and self.model_library.worker.isRunning():
            if self._busy():
                self.on_stop()
            QMessageBox.information(self, "Katalog yenileniyor", "Model kataloğu isteği bitince pencereyi kapatabilirsiniz.")
            event.ignore()
            return
        if self._busy():
            self.on_stop()
            QMessageBox.information(self, "İşlem sürüyor", "Durdurma istendi. Arka plan işlemi bitince pencereyi kapatın.")
            event.ignore()
        else:
            event.accept()

    def on_finished(self, summary, transcript, source_label):
        project = self.worker.project
        full_report = build_summary_report(project, transcript, summary, source_label, state=self.worker.snapshot())
        self.last_summary_md = full_report
        self.summary_view.setMarkdown(full_report)
        self.tabs.setCurrentIndex(1)
        self.status_label.setText("Rapor hazır — insan incelemesi gerekli.")

        self.load_history_list()


    def on_error(self, msg):
        if self.worker and (self.worker.session_status in ("failed", "summary_failed") or
                            (self.summary_worker and self.summary_worker.isRunning())):
            self.show_activity_alert(msg, "warning")
            self.status_label.setText("Model denemeleri sona erdi; nedenler yukarıdaki günlükte gösteriliyor.")
        else:
            QMessageBox.critical(self, "Hata", msg)
            self.status_label.setText("Hata oluştu.")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
