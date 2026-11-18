"""Read-only OpenRouter quota and balance display, separate from generation."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math

from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, QLineEdit


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fetch_account(api_key, management_key=""):
    import requests
    result = {}
    for name, endpoint, key in (("quota", "key", api_key), ("credits", "credits", management_key or api_key)):
        if not key:
            result[name + "_error"] = "Anahtar girilmedi"
            continue
        try:
            response = requests.get("https://openrouter.ai/api/v1/" + endpoint,
                                    headers={"Authorization": "Bearer " + key}, timeout=10)
            if response.status_code != 200:
                result[name + "_error"] = ("Bakiye için yönetim anahtarı gerekli (HTTP 403)" if name == "credits" and response.status_code == 403
                                           else f"Sorgu başarısız (HTTP {response.status_code})")
                continue
            data = response.json().get("data", {})
            if name == "quota":
                quota = data.get("free_model_daily_requests") or {}
                if all(number(quota.get(k)) for k in ("used", "limit", "remaining")):
                    result["quota"] = {k: quota[k] for k in ("used", "limit", "remaining")}
                else:
                    result["quota_error"] = "Günlük sayaç API tarafından bildirilmedi"
                if number(data.get("limit_remaining")):
                    result["key_remaining"] = data["limit_remaining"]
            else:
                values = [data.get("total_credits"), data.get("total_usage")]
                if all(number(v) for v in values):
                    result["credit_remaining"] = str(Decimal(str(values[0])) - Decimal(str(values[1])))
                else:
                    result["credits_error"] = "Kredi bakiyesi API tarafından bildirilmedi"
        except Exception:
            # No raw response, exception, request headers or credentials in UI/logs.
            result[name + "_error"] = "Bağlantı kurulamadı; değer bilinmiyor"
    result["checked_at"] = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S %z")
    result["credit_source"] = "Yönetim anahtarının hesabı" if management_key else "Seçilen anahtarın hesabı"
    return result


class AccountWorker(QThread):
    ready = pyqtSignal(dict)

    def __init__(self, key, management_key, loader):
        super().__init__()
        self.key, self.management_key, self.loader = key, management_key, loader

    def run(self):
        try:
            self.ready.emit(self.loader(self.key, self.management_key))
        except Exception:
            self.ready.emit({"quota_error": "Sorgu tamamlanamadı", "credits_error": "Sorgu tamamlanamadı"})
        finally:
            self.key = self.management_key = ""


class AccountPanel(QWidget):
    def __init__(self, key_getter, parent=None, loader=None):
        super().__init__(parent)
        self.key_getter, self.loader = key_getter, loader or fetch_account
        self.worker = None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("OpenRouter hesabı:"))
        self.source = QComboBox()
        self.source.addItem("Ücretsiz katmandaki anahtar", "free")
        self.source.addItem("Ücretli katmandaki anahtar", "paid")
        row.addWidget(self.source)
        self.refresh_button = QPushButton("Hakları ve Krediyi Yenile")
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.quota_label = QLabel("Günlük ücretsiz hak: henüz sorgulanmadı")
        self.credit_label = QLabel("Kalan kredi: henüz sorgulanmadı")
        self.key_label = QLabel()
        self.updated_label = QLabel("Günlük haklar UTC gününe göre yenilenir. Gösterilen değerler son sorguya aittir.")
        for label in (self.quota_label, self.credit_label, self.key_label, self.updated_label):
            label.setWordWrap(True)
            layout.addWidget(label)
        self.management_key = QLineEdit()
        self.management_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.management_key.setPlaceholderText("Bakiye sorgusu için yönetim anahtarı (gerekirse; kaydedilmez)")
        layout.addWidget(self.management_key)
        self.refresh_button.clicked.connect(self.refresh)
        self.source.currentIndexChanged.connect(self.refresh)
        self.management_key.editingFinished.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(60000)
        self.timer.timeout.connect(lambda: self.refresh() if self.isVisible() else None)
        self.timer.start()

    def refresh(self):
        if self.worker and self.worker.isRunning():
            return
        key = self.key_getter(self.source.currentData())
        self.quota_label.setText("Günlük ücretsiz hak: sorgulanıyor…")
        self.credit_label.setText("Kalan kredi: sorgulanıyor…")
        self.key_label.clear()
        self.refresh_button.setEnabled(False)
        self.source.setEnabled(False)
        self.management_key.setEnabled(False)
        self.worker = AccountWorker(key, self.management_key.text().strip(), self.loader)
        self.worker.ready.connect(self.show_result)
        self.worker.finished.connect(self.query_finished)
        self.worker.start()

    def query_finished(self):
        self.refresh_button.setEnabled(True)
        self.source.setEnabled(True)
        self.management_key.setEnabled(True)

    def show_result(self, data):
        quota = data.get("quota")
        self.quota_label.setText(f"Günlük ücretsiz hak: kalan {quota['remaining']} / {quota['limit']} • Kullanılan: {quota['used']}"
                                if quota else "Günlük ücretsiz hak: " + data.get("quota_error", "Bilinmiyor"))
        if "credit_remaining" in data:
            try:
                balance = f"${Decimal(data['credit_remaining']):.4f} USD"
            except (InvalidOperation, ValueError):
                balance = "Bilinmiyor"
            self.credit_label.setText(f"Kalan kredi: {balance} • {data.get('credit_source', '')}")
        else:
            self.credit_label.setText("Kalan kredi: " + data.get("credits_error", "Bilinmiyor"))
        if "key_remaining" in data:
            self.key_label.setText(f"Seçilen API anahtarının harcama limitinde kalan: ${data['key_remaining']:.4f} (hesap bakiyesinden ayrıdır)")
        self.updated_label.setText("Son sorgu: " + data.get("checked_at", "Bilinmiyor") + " • Günlük haklar UTC gününe göre yenilenir.")
