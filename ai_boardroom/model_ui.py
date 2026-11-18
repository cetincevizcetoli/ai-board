"""Model library and pickers. Catalog requests never use API credentials."""
from copy import deepcopy
import re
from urllib.parse import quote

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QHeaderView, QDialog, QDialogButtonBox, QFormLayout, QMessageBox)

from .model_catalog import cached_catalog, fetch_catalog, save_catalog, merged_models, price_label

PROVIDERS = ["openrouter", "openai", "anthropic", "gemini", "groq"]


class CatalogWorker(QThread):
    result = pyqtSignal(dict)
    error = pyqtSignal(str)

    def run(self):
        try:
            data = fetch_catalog()
            save_catalog(data)
            self.result.emit(data)
        except Exception as exc:
            self.error.emit(f"Katalog yenilenemedi ({type(exc).__name__}); önceki liste korundu.")


class ModelPicker(QDialog):
    def __init__(self, entries, mode, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ücretsiz model seç" if mode == "free" else "Ücretli / diğer model seç")
        self.resize(820, 500)
        self.entries = [m for m in entries if (m.get("tier") == "free") == (mode == "free")]
        self.selected = None
        layout = QVBoxLayout(self)
        note = QLabel("Seçim yalnızca bu alanı değiştirir. Ücretsiz kullanım fiyatı çağrı öncesinde yeniden doğrulanır.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Model adı veya kimliği ara...")
        self.search.textChanged.connect(self.populate)
        layout.addWidget(self.search)
        self.items = QListWidget()
        self.items.itemDoubleClicked.connect(lambda item: self.choose())
        layout.addWidget(self.items)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.choose)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.populate()

    def populate(self):
        self.items.clear()
        search = self.search.text().casefold()
        for entry in self.entries:
            text = f"{entry.get('name', entry['id'])} — {entry['id']} | {entry['provider']} | {price_label(entry)}"
            if search in text.casefold():
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, entry)
                self.items.addItem(item)

    def choose(self):
        item = self.items.currentItem()
        if item:
            self.selected = dict(item.data(Qt.ItemDataRole.UserRole))
            self.accept()


class ModelEditor(QDialog):
    def __init__(self, entry=None, parent=None):
        super().__init__(parent)
        entry = entry or {}
        self.setWindowTitle("Model kaydı ekle / düzelt")
        self.resize(650, 240)
        layout = QFormLayout(self)
        self.provider = QComboBox()
        self.provider.addItems(PROVIDERS)
        self.provider.setCurrentText(entry.get("provider", "openrouter"))
        self.name = QLineEdit(entry.get("name", ""))
        self.identifier = QLineEdit(entry.get("id", ""))
        layout.addRow("API sağlayıcısı:", self.provider)
        layout.addRow("Görünen ad:", self.name)
        layout.addRow("Tam model kimliği:", self.identifier)
        note = QLabel("Elle eklenen bir kayda ücretsiz etiketi verilemez. Ücretsiz fiyatı yalnızca güncel OpenRouter kataloğu doğrular. Katalog kaydını düzenlemek yerel ad/kimlik kaydı oluşturur; resmî fiyatı değiştirmez.")
        note.setWordWrap(True)
        layout.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def validate(self):
        identifier = self.identifier.text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:/@+\-]{1,200}", identifier):
            QMessageBox.warning(self, "Model kimliği", "Boşluksuz, sağlayıcının verdiği tam model kimliğini girin.")
            return
        self.accept()

    def value(self):
        identifier = self.identifier.text().strip()
        return {"provider": self.provider.currentText(), "id": identifier,
                "name": self.name.text().strip() or identifier}


class ModelLibrary(QWidget):
    changed = pyqtSignal()

    def __init__(self, custom=None, auto_refresh=True, parent=None):
        super().__init__(parent)
        self.catalog = cached_catalog()
        self.custom_models = deepcopy(custom or [])
        self.worker = None
        layout = QVBoxLayout(self)
        note = QLabel("OpenRouter listesinde yalnızca metinle sohbet edebilen ve çıktısı metin olan modeller gösterilir. "
                      "Yeni bir OpenRouter modeli için önce kataloğu yenileyin. Model düzelttikten sonra Ayarları Kaydet'e basın.")
        note.setWordWrap(True)
        layout.addWidget(note)
        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Ad veya model kimliği ara...")
        self.search.textChanged.connect(self.populate)
        controls.addWidget(self.search)
        self.filter = QComboBox()
        self.filter.addItem("Ücretsiz", "free")
        self.filter.addItem("Ücretli / fiyatı bilinmeyen", "paid")
        self.filter.addItem("Tümü", "all")
        self.filter.currentIndexChanged.connect(self.populate)
        controls.addWidget(self.filter)
        self.refresh_btn = QPushButton("Kataloğu Yenile")
        self.refresh_btn.clicked.connect(self.refresh)
        controls.addWidget(self.refresh_btn)
        layout.addLayout(controls)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Ad", "Tam model kimliği", "API", "Katman / Fiyat", "Kaynak"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        for label, callback in [("Model Ekle", self.add_model), ("Seçileni Düzenle", self.edit_model),
                                ("Elle Ekleneni Kaldır", self.remove_model), ("Model Sayfasını Aç", self.open_selected),
                                ("OpenRouter Modelleri", lambda: self.open_url("https://openrouter.ai/models")),
                                ("Ücretsiz Modeller", lambda: self.open_url("https://openrouter.ai/collections/free-models"))]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self.populate()
        if auto_refresh and not self.catalog.get("models"):
            QTimer.singleShot(0, self.refresh)

    def entries(self):
        return merged_models(self.catalog, self.custom_models)

    def populate(self):
        entries = self.entries()
        mode, query = self.filter.currentData(), self.search.text().casefold()
        visible = [e for e in entries if (mode == "all" or (e.get("tier") == "free") == (mode == "free"))
                   and query in (e.get("name", "") + " " + e["id"]).casefold()]
        self.table.setRowCount(len(visible))
        for row, entry in enumerate(visible):
            for column, value in enumerate([entry.get("name", entry["id"]), entry["id"], entry["provider"],
                                            price_label(entry), "Yerel kayıt" if entry.get("custom") else "OpenRouter kataloğu"]):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, entry)
                self.table.setItem(row, column, item)
        self.status.setText(f"Gösterilen: {len(visible)} | Son katalog kontrolü: {self.catalog.get('checked_at') or 'Henüz yenilenmedi'}")

    def selected(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def refresh(self):
        if self.worker and self.worker.isRunning():
            return
        self.status.setText("Resmî katalog arka planda yenileniyor...")
        self.refresh_btn.setEnabled(False)
        self.worker = CatalogWorker()
        self.worker.result.connect(self.received)
        self.worker.error.connect(self.status.setText)
        self.worker.finished.connect(lambda: self.refresh_btn.setEnabled(True))
        self.worker.start()

    def received(self, data):
        self.catalog = data
        self.populate()
        self.changed.emit()

    def edit(self, original=None):
        dialog = ModelEditor(original, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            value = dialog.value()
            if value["provider"] == "openrouter" and not any(
                entry["id"] == value["id"] for entry in self.catalog.get("models", [])
            ):
                QMessageBox.information(self, "OpenRouter modeli doğrulanamadı",
                    "Bu kimlik metin sohbet kataloğunda bulunamadı. Kataloğu yenileyip model kimliğini kontrol edin.")
                return
            old_key = (original.get("provider"), original.get("id")) if original else None
            new_key = (value["provider"], value["id"])
            self.custom_models = [m for m in self.custom_models if (m["provider"], m["id"]) not in (old_key, new_key)]
            self.custom_models.append(value)
            self.populate()
            self.changed.emit()

    def add_model(self):
        self.edit()

    def edit_model(self):
        if self.selected():
            self.edit(self.selected())

    def remove_model(self):
        entry = self.selected()
        if not entry:
            return
        if not entry.get("custom"):
            QMessageBox.information(self, "Katalog kaydı", "Resmî kayıt buradan silinmez. Düzenle ile yerel bir kayıt ekleyebilirsiniz.")
            return
        self.custom_models = [m for m in self.custom_models if (m["provider"], m["id"]) != (entry["provider"], entry["id"])]
        self.populate()
        self.changed.emit()

    @staticmethod
    def open_url(url):
        QDesktopServices.openUrl(QUrl(url))

    def open_selected(self):
        entry = self.selected()
        if not entry:
            return
        if entry["provider"] == "openrouter":
            self.open_url("https://openrouter.ai/" + quote(entry["id"], safe="/:"))
        else:
            urls = {"openai": "https://platform.openai.com/docs/models", "anthropic": "https://platform.claude.com/docs/en/about-claude/models/overview",
                    "gemini": "https://ai.google.dev/gemini-api/docs/models", "groq": "https://console.groq.com/docs/models"}
            self.open_url(urls[entry["provider"]])
