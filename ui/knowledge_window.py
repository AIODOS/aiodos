"""Fenêtre d'organisation de la base de connaissance.

Gère les bibliothèques (créer / renommer / supprimer / activer) et affiche leur
contenu en **arborescence dépliable** (collection › dossiers › fichiers / URLs),
avec tri par type.
"""
import re

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QComboBox,
    QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QFileDialog, QMessageBox, QInputDialog, QHeaderView, QAbstractItemView,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIcon

from core import knowledge, config
from api.workers import IndexWorker
from ui.styles import CP

_FILE_FILTER = (
    "Tous les fichiers (*);;"
    "Documents (*.pdf *.docx *.pptx *.xlsx *.rtf *.epub *.odt *.html);;"
    "Texte et code (*.txt *.md *.py *.json *.js *.ts *.csv *.log *.xml *.yaml)")

_KIND_LABEL = {"file": "📄 Fichier", "folder": "📄 Fichier",
               "url": "🌐 URL", "conversation": "💬 Conversation"}
_FILTERS = [("Tout", None), ("📄 Fichiers", "file"),
            ("🗀 Dossiers", "folder"), ("🌐 URLs", "url")]


class KnowledgeWindow(QWidget):
    changed = pyqtSignal()      # émis quand la base change (pour rafraîchir le sidebar)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Organiser la base de connaissance — Cœur IA")
        self.resize(1060, 700)
        if config.LOGO_ICO.exists():
            self.setWindowIcon(QIcon(str(config.LOGO_ICO)))

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)
        root.addWidget(self._build_left())
        root.addWidget(self._build_right(), 1)
        self.refresh()

    # -------------------------------------------------------------- left panel
    def _build_left(self):
        w = QWidget()
        w.setFixedWidth(280)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        t = QLabel("Bibliothèques")
        t.setObjectName("title")
        v.addWidget(t)

        v.addWidget(self._lbl("Ranger les ajouts dans :"))
        self.target_combo = QComboBox()
        v.addWidget(self.target_combo)

        b_new = QPushButton("＋ Nouvelle bibliothèque")
        b_new.clicked.connect(self._new_coll)
        v.addWidget(b_new)
        hb = QHBoxLayout()
        b_ren = QPushButton("Renommer")
        b_ren.clicked.connect(self._rename_coll)
        b_del = QPushButton("✕ Supprimer")
        b_del.clicked.connect(self._delete_coll)
        hb.addWidget(b_ren)
        hb.addWidget(b_del)
        v.addLayout(hb)

        v.addWidget(self._lbl("Coche = utilisée dans les réponses"))
        self.coll_list = QListWidget()
        self.coll_list.itemChanged.connect(self._on_toggle)
        v.addWidget(self.coll_list, 1)
        return w

    # ------------------------------------------------------------- right panel
    def _build_right(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        ha = QHBoxLayout()
        for txt, fn in (("＋ Fichier(s)", self._add_files),
                        ("＋ Dossier", self._add_folder),
                        ("＋ URL(s)", self._add_urls)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            ha.addWidget(b)
        ha.addStretch(1)
        b_exp = QPushButton("Tout déplier")
        b_exp.clicked.connect(lambda: self.tree.expandAll())
        b_col = QPushButton("Tout replier")
        b_col.clicked.connect(lambda: self.tree.collapseAll())
        ha.addWidget(b_exp)
        ha.addWidget(b_col)
        v.addLayout(ha)

        hf = QHBoxLayout()
        hf.addWidget(self._lbl("Afficher :"))
        self.filter_combo = QComboBox()
        for label, _ in _FILTERS:
            self.filter_combo.addItem(label)
        self.filter_combo.currentIndexChanged.connect(self._fill_tree)
        hf.addWidget(self.filter_combo)
        hf.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("dim")
        hf.addWidget(self.status)
        v.addLayout(hf)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Contenu", "Type", "Passages"])
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = self.tree.header()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.tree, 1)

        hr = QPushButton("✕ Retirer la sélection (entrée ou dossier)")
        hr.clicked.connect(self._remove_selected)
        v.addWidget(hr)
        return w

    def _lbl(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("section")
        return lbl

    # ---------------------------------------------------------------- refresh
    def refresh(self):
        info = knowledge.list_collections()
        names = sorted(info.keys())

        cur = self.target_combo.currentText()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItems(names)
        if cur in names:
            self.target_combo.setCurrentText(cur)
        self.target_combo.blockSignals(False)

        self.coll_list.blockSignals(True)
        self.coll_list.clear()
        for name in names:
            meta = info[name]
            it = QListWidgetItem(f"{name}   ({meta['chunks']})")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if meta["enabled"]
                             else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, name)
            self.coll_list.addItem(it)
        self.coll_list.blockSignals(False)
        self._fill_tree()

    def _fill_tree(self):
        kind_filter = _FILTERS[self.filter_combo.currentIndex()][1]
        self.tree.clear()
        entries = knowledge.list_entries()
        if kind_filter:
            entries = [e for e in entries if e["kind"] == kind_filter]

        by_coll = {}
        for e in entries:
            by_coll.setdefault(e["collection"], []).append(e)

        total = 0
        for coll in sorted(by_coll):
            items = by_coll[coll]
            root = QTreeWidgetItem([coll, "", f"{len(items)} élément(s)"])
            root.setData(0, Qt.ItemDataRole.UserRole, ("coll", coll, None))
            font = root.font(0)
            font.setBold(True)
            root.setFont(0, font)
            self.tree.addTopLevelItem(root)
            for e in items:
                total += 1
                if e["kind"] == "url":
                    leaf = QTreeWidgetItem([e["source"], "🌐 URL", str(e["chunks"])])
                    leaf.setData(0, Qt.ItemDataRole.UserRole,
                                 ("entry", coll, e["source"]))
                    root.addChild(leaf)
                    continue
                parts = [p for p in re.split(r"[\\/]", e["source"]) if p]
                node = root
                for part in parts[:-1]:
                    node = self._folder_child(node, part, coll)
                leaf = QTreeWidgetItem(
                    [parts[-1] if parts else e["source"],
                     _KIND_LABEL.get(e["kind"], e["kind"]), str(e["chunks"])])
                leaf.setData(0, Qt.ItemDataRole.UserRole,
                             ("entry", coll, e["source"]))
                node.addChild(leaf)
            root.setExpanded(True)
        scope = f" · filtre : {_FILTERS[self.filter_combo.currentIndex()][0]}" \
            if kind_filter else ""
        self.status.setText(f"{total} entrée(s){scope}")

    def _folder_child(self, parent, name, coll):
        label = "🗀 " + name
        for i in range(parent.childCount()):
            ch = parent.child(i)
            d = ch.data(0, Qt.ItemDataRole.UserRole)
            if d and d[0] == "folder" and ch.text(0) == label:
                return ch
        node = QTreeWidgetItem([label, "", ""])
        node.setData(0, Qt.ItemDataRole.UserRole, ("folder", coll, None))
        parent.addChild(node)
        node.setExpanded(True)
        return node

    # ------------------------------------------------------------ collections
    def _target(self):
        return self.target_combo.currentText() or knowledge.DEFAULT_COLLECTION

    def _new_coll(self):
        name, ok = QInputDialog.getText(
            self, "Nouvelle bibliothèque", "Nom (ex : 2024, Sécurité, Projet X) :")
        if ok and name.strip():
            knowledge.create_collection(name.strip())
            self.refresh()
            self.target_combo.setCurrentText(name.strip())
            self.changed.emit()

    def _rename_coll(self):
        it = self.coll_list.currentItem()
        if not it:
            QMessageBox.information(self, "Renommer",
                                   "Sélectionne d'abord une bibliothèque à gauche.")
            return
        old = it.data(Qt.ItemDataRole.UserRole)
        new, ok = QInputDialog.getText(
            self, "Renommer la bibliothèque", "Nouveau nom :", text=old)
        if ok and new.strip():
            knowledge.rename_collection(old, new.strip())
            self.refresh()
            self.changed.emit()

    def _delete_coll(self):
        it = self.coll_list.currentItem()
        if not it:
            QMessageBox.information(self, "Supprimer",
                                   "Sélectionne d'abord une bibliothèque à gauche.")
            return
        name = it.data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(
                self, "Supprimer",
                f"Supprimer « {name} » et tout son contenu ?") \
                == QMessageBox.StandardButton.Yes:
            knowledge.delete_collection(name)
            self.refresh()
            self.changed.emit()

    def _on_toggle(self, item):
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            knowledge.set_collection_enabled(
                name, item.checkState() == Qt.CheckState.Checked)
            self.changed.emit()

    # ----------------------------------------------------------------- ajouts
    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Ajouter des fichiers", "", _FILE_FILTER)
        if paths:
            self._index(IndexWorker(paths=paths, collection=self._target()))

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Ajouter un dossier entier (récursif)")
        if folder:
            self._index(IndexWorker(folder=folder, collection=self._target()))

    def _add_urls(self):
        text, ok = QInputDialog.getMultiLineText(
            self, "Ajouter des URLs",
            "Une URL par ligne — les pages seront téléchargées et analysées :", "")
        if ok and text.strip():
            urls = [u for u in text.splitlines() if u.strip()]
            self._index(IndexWorker(urls=urls, collection=self._target()))

    def _index(self, worker):
        self._iw = worker
        self.status.setText("Indexation… (ne ferme pas la fenêtre)")
        self._iw.progress.connect(
            lambda d, t, s: self.status.setText(f"Indexation {d}/{t} · {s}"))
        self._iw.finished_ok.connect(self._on_indexed)
        self._iw.failed.connect(
            lambda e: (self.status.setText("Erreur"),
                       QMessageBox.warning(self, "Indexation", e)))
        self._iw.start()

    def _on_indexed(self, collection, n):
        self.status.setText(f"✅ {n} élément(s) ajouté(s) à « {collection} »")
        self.refresh()
        self.changed.emit()

    # ---------------------------------------------------------------- retirer
    def _collect_sources(self, item):
        """Toutes les (collection, source) sous un nœud."""
        out = []
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if d and d[0] == "entry":
            out.append((d[1], d[2]))
        for i in range(item.childCount()):
            out.extend(self._collect_sources(item.child(i)))
        return out

    def _remove_selected(self):
        item = self.tree.currentItem()
        if not item:
            return
        targets = self._collect_sources(item)
        if not targets:
            QMessageBox.information(
                self, "Retirer", "Sélectionne un fichier, un dossier ou une URL.")
            return
        label = item.text(0)
        if QMessageBox.question(
                self, "Retirer",
                f"Retirer « {label} » ({len(targets)} entrée(s)) de la base ?") \
                != QMessageBox.StandardButton.Yes:
            return
        for coll, source in targets:
            knowledge.remove_source(source, coll)
        self.refresh()
        self.changed.emit()
