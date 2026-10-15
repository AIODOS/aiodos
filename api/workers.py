"""Workers QThread — tout appel réseau/IA tourne hors du thread UI.

Communication avec l'UI uniquement par signaux pyqtSignal.
"""
from PyQt6.QtCore import QThread, pyqtSignal

from core import ollama_client, knowledge, cloud, web, config


class ChatWorker(QThread):
    """Génère une réponse en streaming.

    Source de génération : modèle local (Ollama) ou cloud (Mistral/Qwen).
    Contexte injecté : recherche web (optionnelle) + bibliothèques + conversations.
    """
    chunk = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)
    context_used = pyqtSignal(list)    # libellés des sources de contexte utilisées

    def __init__(self, model, messages, libraries=True, conversations=False,
                 web_search=False, cloud_provider=None):
        super().__init__()
        self.model = model
        self.messages = messages
        self.libraries = libraries
        self.conversations = conversations
        self.web_search = web_search
        self.cloud_provider = cloud_provider

    def run(self):
        try:
            messages = list(self.messages)
            sources = []
            last_user = next((m["content"] for m in reversed(messages)
                              if m["role"] == "user"), "")

            # 1) Recherche web (sens unique : on lit des pages publiques)
            web_ok = False
            if self.web_search and last_user:
                blocks = []
                try:
                    for res in web.web_search(last_user, config.get("web_results")):
                        try:
                            title, txt = web.fetch_url_text(res["url"])
                        except Exception:
                            continue
                        blocks.append(f"[Web : {title} — {res['url']}]\n{txt[:3000]}")
                        sources.append("🌐 " + web.host_of(res["url"]))
                except Exception:
                    pass
                if blocks:
                    web_ok = True
                    messages.insert(max(0, len(messages) - 1), {
                        "role": "system",
                        "content": (
                            "Résultats de recherche web réels (récupérés à l'instant). "
                            "Réponds en t'appuyant dessus et cite les URL utilisées. "
                            "Ne dis pas que tu ne peux pas naviguer :\n\n"
                            + "\n\n".join(blocks)),
                    })
                else:
                    messages.insert(max(0, len(messages) - 1), {
                        "role": "system",
                        "content": ("La recherche web n'a renvoyé aucun résultat "
                                    "exploitable. Dis-le honnêtement à l'utilisateur "
                                    "et n'invente pas de sources."),
                    })

            # 2) Base de connaissance : bibliothèques + conversations
            if (self.libraries or self.conversations) and knowledge.count() > 0:
                # 2a) Sommaire : l'IA sait CE QU'ELLE possède (pas qu'un extrait)
                man = knowledge.manifest(self.libraries, self.conversations)
                if man:
                    messages.insert(1, {
                        "role": "system",
                        "content": (
                            "Tu disposes d'une base de connaissance locale. Contenu "
                            "disponible (par bibliothèque) :\n\n" + man +
                            "\n\nQuand l'utilisateur demande ce que tu « vois » ou ce "
                            "que tu as en mémoire, réfère-toi à cette liste. Des extraits "
                            "pertinents te sont fournis ci-dessous selon la question."),
                    })

            # 2b) Extraits pertinents (RAG)
            if (self.libraries or self.conversations) and last_user \
                    and knowledge.count() > 0:
                hits = knowledge.search(last_user, k=4,
                                        libraries=self.libraries,
                                        conversations=self.conversations)
                if hits:
                    ctx = "\n\n".join(
                        f"[Source : {h['source']}]\n{h['text']}" for h in hits)
                    messages.insert(max(0, len(messages) - 1), {
                        "role": "system",
                        "content": ("Contexte tiré des connaissances de l'utilisateur. "
                                    "Sers-t'en si pertinent :\n\n" + ctx),
                    })
                    sources += [h["source"] for h in hits]

            if sources:
                self.context_used.emit(sources)

            # 2c) Règle anti-invention — placée EN DERNIER (juste avant la question),
            #     position la plus forte pour contrer la mémoire des conversations.
            if web_ok:
                rule = ("RAPPEL : les résultats web ci-dessus sont réels et récents. "
                        "Appuie-toi dessus et cite uniquement leurs URL. N'invente "
                        "aucune autre adresse.")
            else:
                rule = ("RAPPEL IMPORTANT : ici tu fonctionnes en local, SANS accès à "
                        "Internet. N'invente JAMAIS d'URL ni de résultat de recherche, "
                        "et ne prétends pas avoir cherché sur le web. Si on te demande "
                        "des informations en ligne, réponds exactement : « Active la "
                        "case 🌐 Recherche web (à gauche) puis repose ta question. » "
                        "Ne fabrique aucune source.")
            messages.insert(max(1, len(messages) - 1),
                            {"role": "system", "content": rule})

            # 3) Génération
            if self.cloud_provider:
                stream = cloud.cloud_chat_stream(self.cloud_provider, messages)
            else:
                stream = ollama_client.chat_stream(self.model, messages)

            full = []
            for piece in stream:
                full.append(piece)
                self.chunk.emit(piece)
            self.finished_ok.emit("".join(full))
        except Exception as e:
            self.failed.emit(str(e))


class ModelListWorker(QThread):
    """Récupère la liste des modèles Ollama installés."""
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def run(self):
        try:
            self.loaded.emit(ollama_client.list_models())
        except Exception as e:
            self.failed.emit(str(e))


class IndexWorker(QThread):
    """Indexe des fichiers, un dossier entier, ou des URLs dans une collection."""
    progress = pyqtSignal(int, int, str)   # fait, total, source courante
    finished_ok = pyqtSignal(str, int)     # collection, nb d'éléments ajoutés
    failed = pyqtSignal(str)

    def __init__(self, paths=None, folder=None, urls=None,
                 collection="", recursive=True):
        super().__init__()
        self.paths = paths
        self.folder = folder
        self.urls = urls
        self.collection = collection or knowledge.DEFAULT_COLLECTION
        self.recursive = recursive

    def run(self):
        try:
            fp = lambda d, t, s: self.progress.emit(d, t, s)
            if self.urls:
                added, total = knowledge.add_urls(
                    self.urls, self.collection, file_progress=fp)
            elif self.folder:
                added, total = knowledge.add_folder(
                    self.folder, self.collection, self.recursive, file_progress=fp)
            else:
                added, total = knowledge.add_files(
                    self.paths or [], self.collection, file_progress=fp)
            if total == 0:
                self.failed.emit("Rien à indexer (sélection ou dossier vide).")
                return
            if added == 0:
                self.failed.emit(
                    "Aucun texte exploitable trouvé (images, PDF scannés ou "
                    "fichiers non pris en charge). Rien n'a été ajouté.")
                return
            self.finished_ok.emit(self.collection, added)
        except Exception as e:
            self.failed.emit(str(e))


class ConvIndexWorker(QThread):
    """Indexe (incrémental) une conversation dans la mémoire inter-conversations."""
    done = pyqtSignal()

    def __init__(self, sid, title, messages):
        super().__init__()
        self.sid = sid
        self.title = title
        self.messages = messages

    def run(self):
        try:
            knowledge.index_conversation(self.sid, self.title, self.messages)
        except Exception:
            pass
        self.done.emit()


class ConvBackfillWorker(QThread):
    """Indexe toutes les conversations existantes (au moment de l'activation)."""
    progress = pyqtSignal(int, int)
    done = pyqtSignal()

    def __init__(self, sessions):
        super().__init__()
        self.sessions = sessions   # liste de sessions complètes (avec messages)

    def run(self):
        total = len(self.sessions)
        for i, s in enumerate(self.sessions):
            try:
                knowledge.index_conversation(
                    s["id"], s.get("title", s["id"]), s.get("messages", []))
            except Exception:
                pass
            self.progress.emit(i + 1, total)
        self.done.emit()
