"""Fenêtre principale de Cœur IA : chat (local + cloud) + base de connaissance."""
import re

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QComboBox,
    QPlainTextEdit, QScrollArea, QCheckBox, QMessageBox, QFrame, QSizePolicy,
    QInputDialog, QTreeWidget, QTreeWidgetItem, QMenu,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeyEvent, QIcon, QPixmap

from core import config, chat_session, knowledge, cloud
from api.workers import (ChatWorker, ModelListWorker, ConvIndexWorker,
                         ConvBackfillWorker)
from ui.styles import CP
from ui.message_widget import MessageWidget
from ui.knowledge_window import KnowledgeWindow
from ui.dialog_settings import SettingsDialog

CLOUD_MISTRAL = "☁ Mistral (cloud)"
CLOUD_QWEN = "☁ Qwen (cloud)"
CLOUD_MODELS = [CLOUD_MISTRAL, CLOUD_QWEN]

# Tournures qui suggèrent une recherche web (pour proposer d'activer la case).
# On matche les RADICAUX (cherch* → cherche/chercher/recherche/rechercher…).
_WEB_INTENT = re.compile(
    r"(cherch\w*|recherch\w*|trouv\w*|google|navigue\w*|sur le web|sur internet|"
    r"en ligne|actualit[ée]s?\w*|site officiel|documentation (en ligne|web|à jour)|"
    r"derni[èe]re?s? (version|nouvelle|info))",
    re.IGNORECASE)


class ChatInput(QPlainTextEdit):
    """Zone de saisie : Entrée = envoyer, Maj+Entrée = nouvelle ligne."""

    def __init__(self, on_submit):
        super().__init__()
        self._on_submit = on_submit
        self.setPlaceholderText(
            "Écris ton message…  (Entrée pour envoyer, Maj+Entrée = saut de ligne)")
        self.setFixedHeight(90)

    def keyPressEvent(self, e: QKeyEvent):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._on_submit()
            return
        super().keyPressEvent(e)


class ChatWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cœur IA")
        self.resize(1180, 820)
        if config.LOGO_ICO.exists():
            self.setWindowIcon(QIcon(str(config.LOGO_ICO)))

        self.cfg = config.load()
        self.session = chat_session.new_session()
        self.worker = None
        self.current_assistant = None
        self._kw = None

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_chat_area(), 1)

        self._refresh_sessions()
        self._refresh_knowledge()
        self._load_models()

    # ----------------------------------------------------------------- sidebar
    def _build_sidebar(self):
        side = QFrame()
        side.setFixedWidth(290)
        side.setStyleSheet(f"QFrame {{ background: {CP['bg0']}; }}")
        v = QVBoxLayout(side)
        v.setContentsMargins(14, 16, 14, 16)
        v.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        logo = QLabel()
        if config.LOGO_PNG.exists():
            pix = QPixmap(str(config.LOGO_PNG)).scaled(
                40, 40, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            logo.setPixmap(pix)
        header.addWidget(logo)
        title = QLabel("Cœur IA")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        v.addLayout(header)

        hb = QHBoxLayout()
        btn_new = QPushButton("＋ Conversation vierge")
        btn_new.setObjectName("send")
        btn_new.clicked.connect(self._new_chat)
        hb.addWidget(btn_new, 1)
        btn_folder = QPushButton("🗀+")
        btn_folder.setFixedWidth(44)
        btn_folder.setToolTip("Nouveau dossier de conversations")
        btn_folder.clicked.connect(self._new_folder)
        hb.addWidget(btn_folder)
        v.addLayout(hb)

        v.addWidget(self._section_label("CONVERSATIONS"))
        self.sessions_tree = QTreeWidget()
        self.sessions_tree.setHeaderHidden(True)
        self.sessions_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.sessions_tree.customContextMenuRequested.connect(self._tree_menu)
        self.sessions_tree.itemClicked.connect(self._tree_clicked)
        self.sessions_tree.itemDoubleClicked.connect(self._tree_double)
        v.addWidget(self.sessions_tree, 1)

        v.addWidget(self._section_label("CONNAISSANCES"))
        self.know_toggle = QCheckBox("Utiliser les bibliothèques")
        self.know_toggle.setChecked(bool(self.cfg.get("knowledge_enabled", True)))
        self.know_toggle.stateChanged.connect(
            lambda: config.set_value("knowledge_enabled", self.know_toggle.isChecked()))
        v.addWidget(self.know_toggle)

        self.conv_toggle = QCheckBox("Utiliser toutes les conversations")
        self.conv_toggle.setChecked(bool(self.cfg.get("conversations_enabled", False)))
        self.conv_toggle.stateChanged.connect(self._toggle_conversations)
        v.addWidget(self.conv_toggle)

        self.know_count = QLabel("")
        self.know_count.setObjectName("dim")
        v.addWidget(self.know_count)

        btn_manage = QPushButton("📚 Organiser les connaissances…")
        btn_manage.clicked.connect(self._open_knowledge)
        v.addWidget(btn_manage)

        v.addWidget(self._section_label("CLOUD (sécurisé)"))
        self.web_toggle = QCheckBox("🌐 Recherche web (validée)")
        self.web_toggle.setChecked(bool(self.cfg.get("web_search_enabled", False)))
        self.web_toggle.stateChanged.connect(
            lambda: config.set_value("web_search_enabled", self.web_toggle.isChecked()))
        v.addWidget(self.web_toggle)

        btn_cloud = QPushButton("⚙ Réglages cloud / clés API…")
        btn_cloud.clicked.connect(self._open_settings)
        v.addWidget(btn_cloud)

        return side

    def _section_label(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("section")
        return lbl

    # --------------------------------------------------------------- chat area
    def _build_chat_area(self):
        area = QWidget()
        v = QVBoxLayout(area)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(QLabel("Modèle :"))
        self.model_combo = QComboBox()
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        top.addWidget(self.model_combo)
        self.btn_refresh = QPushButton("⟳")
        self.btn_refresh.setFixedWidth(40)
        self.btn_refresh.setToolTip("Rafraîchir la liste des modèles")
        self.btn_refresh.clicked.connect(self._load_models)
        top.addWidget(self.btn_refresh)
        top.addStretch(1)
        self.status = QLabel("…")
        self.status.setObjectName("dim")
        top.addWidget(self.status)
        v.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        self.msg_layout = QVBoxLayout(container)
        self.msg_layout.setContentsMargins(40, 8, 40, 8)
        self.msg_layout.setSpacing(12)
        self.msg_layout.addStretch(1)
        self.scroll.setWidget(container)
        v.addWidget(self.scroll, 1)
        self._show_welcome()

        bottom = QHBoxLayout()
        self.input = ChatInput(self._send)
        bottom.addWidget(self.input, 1)
        self.btn_send = QPushButton("Envoyer")
        self.btn_send.setObjectName("send")
        self.btn_send.setFixedHeight(90)
        self.btn_send.setFixedWidth(110)
        self.btn_send.clicked.connect(self._send)
        bottom.addWidget(self.btn_send)
        v.addLayout(bottom)
        return area

    def _show_welcome(self):
        w = QLabel(
            "Bienvenue dans <b>Cœur IA</b> — ton atelier d'IA local.<br><br>"
            "Choisis un modèle, pose une question ou demande du code.<br>"
            "Active les bibliothèques, la mémoire des conversations ou la "
            "recherche web selon tes besoins.")
        w.setObjectName("dim")
        w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        w.setWordWrap(True)
        self._welcome = w
        self.msg_layout.insertWidget(0, w)

    # ------------------------------------------------------------------ models
    def _load_models(self):
        self.status.setText("Connexion à Ollama…")
        self._ml = ModelListWorker()
        self._ml.loaded.connect(self._on_models_loaded)
        self._ml.failed.connect(self._on_models_failed)
        self._ml.start()

    def _on_models_loaded(self, models):
        cur = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self.model_combo.addItems(CLOUD_MODELS)
        target = cur or self.cfg.get("default_model") or (models[0] if models else "")
        idx = self.model_combo.findText(target)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.model_combo.blockSignals(False)
        if models:
            self.status.setText(f"✅ Ollama · {len(models)} modèle(s) local(aux)")
        else:
            self.status.setText("⚠ Aucun modèle local — téléchargement en cours ?")

    def _on_models_failed(self, err):
        self.status.setText("🔴 Ollama injoignable")
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(CLOUD_MODELS)
        self.model_combo.blockSignals(False)
        QMessageBox.warning(
            self, "Ollama",
            "Impossible de joindre Ollama (http://localhost:11434).\n"
            "Lance Ollama (ollama serve) puis clique sur ⟳.\n\n"
            f"Détail : {err}")

    def _on_model_changed(self, name):
        if name and name not in CLOUD_MODELS:
            config.set_value("default_model", name)

    def _provider_for(self, model):
        if model == CLOUD_MISTRAL:
            return "mistral"
        if model == CLOUD_QWEN:
            return "qwen"
        return None

    # --------------------------------------------------------- conversations UI
    def _refresh_sessions(self):
        self.sessions_tree.blockSignals(True)
        self.sessions_tree.clear()
        nodes = {}

        def folder_node(name):
            if name in nodes:
                return nodes[name]
            label = name if name else "Sans dossier"
            node = QTreeWidgetItem([f"🗀 {label}"])
            node.setData(0, Qt.ItemDataRole.UserRole, ("folder", name))
            self.sessions_tree.addTopLevelItem(node)
            node.setExpanded(True)
            nodes[name] = node
            return node

        folder_node("")
        for f in chat_session.list_folders():
            folder_node(f)
        for s in chat_session.list_sessions():
            parent = folder_node(s.get("folder", ""))
            it = QTreeWidgetItem([s["title"]])
            it.setData(0, Qt.ItemDataRole.UserRole, ("conv", s["id"]))
            parent.addChild(it)
        self.sessions_tree.blockSignals(False)

    def _tree_clicked(self, item, _col):
        kind, val = item.data(0, Qt.ItemDataRole.UserRole)
        if kind == "conv":
            self._open_session(val)

    def _tree_double(self, item, _col):
        kind, val = item.data(0, Qt.ItemDataRole.UserRole)
        if kind == "conv":
            self._rename_session(val)

    def _tree_menu(self, pos):
        item = self.sessions_tree.itemAt(pos)
        if not item:
            return
        kind, val = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        if kind == "conv":
            menu.addAction("Renommer", lambda: self._rename_session(val))
            move = menu.addMenu("Déplacer vers")
            move.addAction("Racine (sans dossier)",
                           lambda: self._move_session(val, ""))
            for f in chat_session.list_folders():
                move.addAction(f, lambda _=False, fn=f: self._move_session(val, fn))
            menu.addSeparator()
            menu.addAction("Supprimer", lambda: self._delete_session(val))
        else:
            menu.addAction("Renommer le dossier",
                           lambda: self._rename_folder(val))
            menu.addAction("Supprimer le dossier",
                           lambda: self._delete_folder(val))
        menu.exec(self.sessions_tree.viewport().mapToGlobal(pos))

    def _new_folder(self):
        name, ok = QInputDialog.getText(self, "Nouveau dossier", "Nom du dossier :")
        if ok and name.strip():
            chat_session.create_folder(name.strip())
            self._refresh_sessions()

    def _rename_folder(self, name):
        if not name:
            return
        new, ok = QInputDialog.getText(
            self, "Renommer le dossier", "Nouveau nom :", text=name)
        if ok and new.strip() and new.strip() != name:
            chat_session.create_folder(new.strip())
            for s in chat_session.list_sessions():
                if s.get("folder") == name:
                    chat_session.set_folder(s["id"], new.strip())
            chat_session.delete_folder(name)
            if self.session.get("folder") == name:
                self.session["folder"] = new.strip()
            self._refresh_sessions()

    def _delete_folder(self, name):
        if not name:
            return
        if QMessageBox.question(
                self, "Supprimer le dossier",
                f"Supprimer le dossier « {name} » ? "
                "Les conversations repassent à la racine.") \
                == QMessageBox.StandardButton.Yes:
            chat_session.delete_folder(name)
            self._refresh_sessions()

    def _rename_session(self, sid):
        s = chat_session.load(sid) or (self.session if self.session["id"] == sid else None)
        if not s:
            return
        new, ok = QInputDialog.getText(
            self, "Renommer la conversation", "Nouveau titre :", text=s["title"])
        if ok and new.strip():
            chat_session.rename(sid, new.strip())
            if self.session["id"] == sid:
                self.session["title"] = new.strip()
            self._refresh_sessions()

    def _move_session(self, sid, folder):
        chat_session.set_folder(sid, folder)
        if self.session["id"] == sid:
            self.session["folder"] = folder
        self._refresh_sessions()

    def _delete_session(self, sid):
        if QMessageBox.question(
                self, "Supprimer", "Supprimer cette conversation ?") \
                != QMessageBox.StandardButton.Yes:
            return
        chat_session.delete(sid)
        if self.session["id"] == sid:
            self.session = chat_session.new_session()
            self._clear_messages()
            self._show_welcome()
        self._refresh_sessions()

    def _new_chat(self):
        if self.session["messages"]:
            chat_session.save(self.session)
        self.session = chat_session.new_session()
        self._clear_messages()
        self._show_welcome()
        self._refresh_sessions()

    def _open_session(self, sid):
        if sid == self.session["id"]:
            return
        if self.session["messages"]:
            chat_session.save(self.session)
        loaded = chat_session.load(sid)
        if not loaded:
            return
        self.session = loaded
        self._clear_messages()
        for m in self.session["messages"]:
            self._add_bubble(m["role"], m["content"])
        self._scroll_to_bottom()
        if self.session.get("model"):
            idx = self.model_combo.findText(self.session["model"])
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)

    # --------------------------------------------------------------- knowledge
    def _refresh_knowledge(self):
        info = knowledge.list_collections()
        n_coll = len(info)
        n_entries = len(knowledge.list_entries())
        self.know_count.setText(f"{n_coll} bibliothèque(s) · {n_entries} entrée(s)")

    def _open_knowledge(self):
        if self._kw is None:
            self._kw = KnowledgeWindow(self)
            self._kw.changed.connect(self._refresh_knowledge)
        self._kw.refresh()
        self._kw.show()
        self._kw.raise_()
        self._kw.activateWindow()

    def _toggle_conversations(self):
        on = self.conv_toggle.isChecked()
        config.set_value("conversations_enabled", on)
        if on:
            sessions = [chat_session.load(s["id"])
                        for s in chat_session.list_sessions()]
            sessions = [s for s in sessions if s and s.get("messages")]
            if sessions:
                self.status.setText("Indexation des conversations…")
                self._bf = ConvBackfillWorker(sessions)
                self._bf.progress.connect(
                    lambda d, t: self.status.setText(f"Conversations {d}/{t}"))
                self._bf.done.connect(
                    lambda: self.status.setText("✅ Conversations indexées"))
                self._bf.start()

    def _open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec():
            self.cfg = config.load()

    # -------------------------------------------------------------------- chat
    def _confirm_web(self, query):
        return QMessageBox.question(
            self, "Recherche web",
            "Cœur IA va envoyer cette recherche au web (DuckDuckGo) :\n\n"
            f"« {query[:200]} »\n\n"
            "Aucune autre donnée ne sera envoyée. Continuer ?") \
            == QMessageBox.StandardButton.Yes

    def _confirm_cloud(self, provider, query):
        return QMessageBox.question(
            self, "Envoi vers le cloud",
            f"Cette requête va être envoyée à « {cloud.host(provider)} » "
            f"(cloud {provider}).\n\n"
            f"Contenu (aperçu) :\n« {query[:300]} »\n\n"
            "⚠ L'historique de la conversation est aussi envoyé. Continuer ?") \
            == QMessageBox.StandardButton.Yes

    def _send(self):
        if self.worker and self.worker.isRunning():
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        model = self.model_combo.currentText()
        if not model:
            QMessageBox.information(
                self, "Modèle", "Aucun modèle disponible. Patiente puis clique ⟳.")
            return

        provider = self._provider_for(model)
        web_on = self.web_toggle.isChecked()
        web_confirmed = False

        # Détection d'intention : recherche web demandée mais case décochée
        if not web_on and _WEB_INTENT.search(text):
            if QMessageBox.question(
                    self, "Recherche web",
                    "Ta question semble demander une recherche sur le web, mais "
                    "la recherche web est désactivée.\n\nL'activer pour ce message ?\n\n"
                    f"La requête « {text[:150]} » sera envoyée à DuckDuckGo "
                    "(aucune autre donnée).") == QMessageBox.StandardButton.Yes:
                web_on = True
                web_confirmed = True

        # Validations de sécurité avant tout envoi sortant
        if web_on and not web_confirmed and not self._confirm_web(text):
            web_on = False
        if provider:
            if not cloud.has_key(provider):
                QMessageBox.warning(
                    self, "Clé manquante",
                    f"Aucune clé API pour {provider}.\n"
                    "Renseigne-la dans « Réglages cloud / clés API ».")
                return
            if self.cfg.get("cloud_confirm", True) and \
                    not self._confirm_cloud(provider, text):
                return

        if getattr(self, "_welcome", None):
            self._welcome.setParent(None)
            self._welcome = None

        self.input.clear()
        self.session["model"] = model
        self.session["messages"].append({"role": "user", "content": text})
        self._add_bubble("user", text)
        if self.session["title"] == "Nouvelle conversation":
            self.session["title"] = (text[:40] + "…") if len(text) > 40 else text

        self.current_assistant = self._add_bubble("assistant", "")
        self._scroll_to_bottom()
        self._set_generating(True)

        messages = [{"role": "system", "content": self.cfg.get("system_prompt", "")}]
        messages += self.session["messages"]

        self.worker = ChatWorker(
            model, messages,
            libraries=self.know_toggle.isChecked(),
            conversations=self.conv_toggle.isChecked(),
            web_search=web_on,
            cloud_provider=provider)
        self.worker.chunk.connect(self._on_chunk)
        self.worker.context_used.connect(
            lambda srcs: self.current_assistant.set_sources(srcs))
        self.worker.finished_ok.connect(self._on_done)
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _on_chunk(self, piece):
        if self.current_assistant:
            self.current_assistant.append(piece)
            self._scroll_to_bottom()

    def _on_done(self, full):
        self.session["messages"].append({"role": "assistant", "content": full})
        chat_session.save(self.session)
        self._refresh_sessions()
        self._set_generating(False)
        if self.conv_toggle.isChecked():
            self._civ = ConvIndexWorker(
                self.session["id"], self.session["title"],
                self.session["messages"])
            self._civ.start()

    def _on_fail(self, err):
        if self.current_assistant:
            self.current_assistant.set_text(f"⚠ **Erreur** : {err}")
        self._set_generating(False)

    def _set_generating(self, on):
        self.btn_send.setEnabled(not on)
        self.btn_send.setText("…" if on else "Envoyer")
        self.input.setReadOnly(on)

    # ------------------------------------------------------------------ helpers
    def _add_bubble(self, role, text):
        w = MessageWidget(role, text)
        w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, w)
        return w

    def _clear_messages(self):
        while self.msg_layout.count() > 1:
            item = self.msg_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
        self._welcome = None

    def _scroll_to_bottom(self):
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))

    def closeEvent(self, e):
        if self.session["messages"]:
            chat_session.save(self.session)
        super().closeEvent(e)
