"""Bulle de message dans le fil de conversation, avec rendu markdown."""
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt

from ui.styles import CP


class MessageWidget(QFrame):
    """Une bulle (utilisateur ou assistant). Texte rendu en markdown."""

    def __init__(self, role, text=""):
        super().__init__()
        self.role = role
        self._buffer = text
        is_user = role == "user"
        bg = CP["user"] if is_user else CP["assistant"]
        self.setStyleSheet(
            f"QFrame {{ background: {bg}; border: 1px solid {CP['border']};"
            f" border-radius: 12px; }}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(6)

        who = QLabel("Vous" if is_user else "Cœur IA")
        who.setStyleSheet(
            f"color: {CP['text_dim'] if is_user else CP['accent']};"
            f" font-weight: 600; background: transparent; border: none;")
        lay.addWidget(who)

        self.body = QLabel()
        self.body.setTextFormat(Qt.TextFormat.MarkdownText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setStyleSheet(
            "background: transparent; border: none;")
        lay.addWidget(self.body)

        self.sources = QLabel()
        self.sources.setObjectName("dim")
        self.sources.setWordWrap(True)
        self.sources.setStyleSheet(
            f"color: {CP['text_dim']}; font-size: 11px;"
            f" background: transparent; border: none;")
        self.sources.setVisible(False)
        lay.addWidget(self.sources)

        self._render()

    def append(self, chunk):
        self._buffer += chunk
        self._render()

    def set_text(self, text):
        self._buffer = text
        self._render()

    def text(self):
        return self._buffer

    def set_sources(self, names):
        if names:
            self.sources.setText("📚 " + ", ".join(names))
            self.sources.setVisible(True)

    def _render(self):
        self.body.setText(self._buffer or "…")
