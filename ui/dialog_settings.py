"""Réglages cloud : clés API Mistral/Qwen, recherche web, validation."""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QSpinBox,
    QCheckBox, QPushButton, QFormLayout, QComboBox,
)
from PyQt6.QtCore import Qt

from core import config
from ui.styles import CP


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Réglages cloud")
        self.setMinimumWidth(520)
        cfg = config.load()

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)

        intro = QLabel(
            "Le cloud est <b>optionnel</b> et <b>désactivé par défaut</b>.<br>"
            "Aucune donnée ne part sans ta validation explicite.")
        intro.setObjectName("dim")
        intro.setWordWrap(True)
        v.addWidget(intro)

        # --- Clés API ---
        v.addWidget(self._h("API cloud (modèles plus puissants)"))
        form = QFormLayout()
        form.setSpacing(8)
        self.mistral_key = QLineEdit(cfg.get("mistral_key", ""))
        self.mistral_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.mistral_key.setPlaceholderText("clé api.mistral.ai")
        form.addRow("Clé API Mistral :", self.mistral_key)

        self.qwen_key = QLineEdit(cfg.get("qwen_key", ""))
        self.qwen_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.qwen_key.setPlaceholderText("clé DashScope (Qwen)")
        form.addRow("Clé API Qwen :", self.qwen_key)

        self.mistral_model = QLineEdit(cfg.get("mistral_cloud_model", ""))
        form.addRow("Modèle Mistral cloud :", self.mistral_model)
        self.qwen_model = QLineEdit(cfg.get("qwen_cloud_model", ""))
        form.addRow("Modèle Qwen cloud :", self.qwen_model)
        v.addLayout(form)

        # --- Recherche web ---
        v.addWidget(self._h("Recherche web"))
        hr = QHBoxLayout()
        hr.addWidget(QLabel("Pages lues par recherche :"))
        self.web_results = QSpinBox()
        self.web_results.setRange(1, 8)
        self.web_results.setValue(int(cfg.get("web_results", 3)))
        hr.addWidget(self.web_results)
        hr.addStretch(1)
        v.addLayout(hr)

        # --- Médias (images / audio / vidéo) ---
        v.addWidget(self._h("Médias dans la base de connaissance"))
        self.media_vision = QCheckBox(
            "Images : lire le texte (OCR) et décrire le contenu (modèle de vision)")
        self.media_vision.setChecked(bool(cfg.get("media_vision", True)))
        v.addWidget(self.media_vision)

        fm = QFormLayout()
        fm.setSpacing(8)
        self.vision_mode = QComboBox()
        self.vision_mode.addItem("OCR + description", "both")
        self.vision_mode.addItem("OCR seulement (texte)", "ocr")
        self.vision_mode.addItem("Description seulement", "describe")
        idx = self.vision_mode.findData(cfg.get("media_vision_mode", "both"))
        self.vision_mode.setCurrentIndex(max(0, idx))
        fm.addRow("Mode images :", self.vision_mode)
        self.vision_model = QLineEdit(cfg.get("vision_model", ""))
        fm.addRow("Modèle de vision (Ollama) :", self.vision_model)
        v.addLayout(fm)

        self.media_transcribe = QCheckBox(
            "Audio / vidéo : transcrire la parole en texte (Whisper)")
        self.media_transcribe.setChecked(bool(cfg.get("media_transcribe", True)))
        v.addWidget(self.media_transcribe)
        fw = QFormLayout()
        self.whisper_model = QComboBox()
        for m in ("tiny", "base", "small", "medium", "large-v3"):
            self.whisper_model.addItem(m, m)
        wi = self.whisper_model.findData(cfg.get("whisper_model", "small"))
        self.whisper_model.setCurrentIndex(max(0, wi))
        fw.addRow("Modèle Whisper :", self.whisper_model)
        v.addLayout(fw)

        note = QLabel("Ces traitements sont locaux mais lents (quelques secondes "
                      "par image, plus pour l'audio) et téléchargent un modèle au "
                      "premier usage.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        v.addWidget(note)

        # --- Sécurité ---
        self.cloud_confirm = QCheckBox(
            "Demander une validation avant chaque envoi vers le cloud (recommandé)")
        self.cloud_confirm.setChecked(bool(cfg.get("cloud_confirm", True)))
        v.addWidget(self.cloud_confirm)

        # --- Boutons ---
        hb = QHBoxLayout()
        hb.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Enregistrer")
        save.setObjectName("send")
        save.clicked.connect(self._save)
        hb.addWidget(cancel)
        hb.addWidget(save)
        v.addLayout(hb)

    def _h(self, text):
        lbl = QLabel(text.upper())
        lbl.setObjectName("section")
        return lbl

    def _save(self):
        cfg = config.load()
        cfg["mistral_key"] = self.mistral_key.text().strip()
        cfg["qwen_key"] = self.qwen_key.text().strip()
        cfg["mistral_cloud_model"] = self.mistral_model.text().strip() or "mistral-large-latest"
        cfg["qwen_cloud_model"] = self.qwen_model.text().strip() or "qwen-max"
        cfg["web_results"] = self.web_results.value()
        cfg["cloud_confirm"] = self.cloud_confirm.isChecked()
        cfg["media_vision"] = self.media_vision.isChecked()
        cfg["media_vision_mode"] = self.vision_mode.currentData()
        cfg["vision_model"] = self.vision_model.text().strip() or "qwen2.5vl:7b"
        cfg["media_transcribe"] = self.media_transcribe.isChecked()
        cfg["whisper_model"] = self.whisper_model.currentData()
        config.save(cfg)
        self.accept()
