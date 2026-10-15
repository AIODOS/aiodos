"""Palette de couleurs et feuille de style sombre de Cœur IA."""

CP = {
    "bg0":       "#0a0c14",
    "bg1":       "#0f1320",
    "bg2":       "#161b2e",
    "bg3":       "#1e2438",
    "surface":   "#1a2034",
    "border":    "#2a3250",
    "text":      "#e6e9f5",
    "text_dim":  "#9aa3c0",
    "accent":    "#6c7cff",
    "accent2":   "#8b5cf6",
    "user":      "#1c2b4a",
    "assistant": "#161b2e",
    "ok":        "#3ecf8e",
    "warn":      "#f0a020",
    "err":       "#ff5c5c",
}

STYLESHEET = f"""
QWidget {{
    background: {CP['bg1']};
    color: {CP['text']};
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px;
}}
QPushButton {{
    background: {CP['bg3']};
    border: 1px solid {CP['border']};
    border-radius: 8px;
    padding: 8px 14px;
}}
QPushButton:hover {{ background: {CP['surface']}; border-color: {CP['accent']}; }}
QPushButton:pressed {{ background: {CP['bg2']}; }}
QPushButton:disabled {{ color: {CP['text_dim']}; border-color: {CP['bg3']}; }}
QPushButton#send {{ background: {CP['accent']}; border: none; font-weight: 600; }}
QPushButton#send:hover {{ background: {CP['accent2']}; }}
QPushButton#send:disabled {{ background: {CP['bg3']}; }}
QComboBox {{
    background: {CP['bg2']};
    border: 1px solid {CP['border']};
    border-radius: 8px;
    padding: 6px 10px;
    min-width: 160px;
}}
QComboBox QAbstractItemView {{
    background: {CP['bg2']};
    border: 1px solid {CP['border']};
    selection-background-color: {CP['accent']};
    outline: none;
}}
QPlainTextEdit, QTextEdit, QTextBrowser {{
    background: {CP['bg2']};
    border: 1px solid {CP['border']};
    border-radius: 10px;
    padding: 8px;
}}
QListWidget {{
    background: {CP['bg0']};
    border: 1px solid {CP['border']};
    border-radius: 8px;
    outline: none;
}}
QListWidget::item {{ padding: 8px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {CP['bg3']}; }}
QListWidget::item:hover {{ background: {CP['bg2']}; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {CP['border']}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {CP['accent']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QCheckBox {{ spacing: 8px; }}
QLabel#title {{ font-size: 17px; font-weight: 700; }}
QLabel#dim {{ color: {CP['text_dim']}; }}
QLabel#section {{ color: {CP['text_dim']}; font-size: 12px; font-weight: 600; }}
"""
