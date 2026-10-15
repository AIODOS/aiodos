"""Cœur IA — point d'entrée.

Atelier d'IA local : chat multi-modèles (Ollama) + base de connaissance.
Lancer avec :
    C:\\Users\\22eme\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe main.py
"""
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPalette, QColor, QIcon

from core import config
from ui.styles import STYLESHEET, CP
from ui.chat_window import ChatWindow


def main():
    # Windows : regrouper sous notre propre icône dans la barre des tâches
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Arkane.CoeurIA")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("Cœur IA")
    app.setStyleSheet(STYLESHEET)
    if config.LOGO_ICO.exists():
        app.setWindowIcon(QIcon(str(config.LOGO_ICO)))

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(CP["bg1"]))
    pal.setColor(QPalette.ColorRole.Base, QColor(CP["bg2"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(CP["text"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(CP["text"]))
    pal.setColor(QPalette.ColorRole.Button, QColor(CP["bg3"]))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(CP["text"]))
    app.setPalette(pal)

    win = ChatWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
