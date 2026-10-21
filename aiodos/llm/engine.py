"""Le routeur du moteur intégré : un seul llama-server (llama.cpp) en mode routeur, qui
sert tous les modèles de cette machine par leur nom, les charge à la demande et décharge le
plus ancien au-delà de MODELS_MAX, comme Ollama.

- Préréglages : un fichier INI (data/llm/modeles.ini) écrit d'après les modèles disponibles
  (llm/models.py) : poids, projecteur d'images, taille du contexte, « embeddings » pour un
  modèle d'embedding. Le routeur repart quand la liste change (modèle téléchargé, écarté).
- Un modèle d'embedding lit chaque texte d'un seul tenant : ses lots (batch-size,
  ubatch-size) ont la taille de son contexte, sinon llama-server les ramène à 512 jetons et
  refuse tout texte plus long.
- --fit on : llama.cpp place lui-même les couches dans la mémoire de la carte graphique.
- Sur 127.0.0.1 seulement, avec une clé tirée à chaque lancement, exigée par le routeur et
  par les llama-server qu'il lance pour chaque modèle (à un port au hasard) : sans elle, 401
  partout sauf /health. Elle passe par l'environnement (LLAMA_API_KEY), jamais par la ligne
  de commande, que tout compte de la machine lit (/proc/<pid>/cmdline, ps : faille 20 de
  l'audit du 20/10/2026) : le routeur retire « api-key » des arguments de ses llama-server,
  mais leur donne son environnement et leur transmet l'en-tête Authorization de chaque
  demande (vérifié avec llama.cpp b11707). Une fois prêt, il doit l'exiger (cluster/engine.
  Process.start : 401 sans elle), sinon il est arrêté. CORS reste ouvert : une page web peut
  joindre ces ports, sans la clé elle n'obtient rien.
- Le programme est celui de llama.cpp livré avec AIODOS (cluster/engine.folder()).
"""
import hashlib
import logging
import secrets
import threading
from pathlib import Path

import requests

from aiodos import paths
from aiodos.cluster import engine as programs
from aiodos.llm import context, models as registry

log = logging.getLogger("aiodos.llm")

MODELS_MAX = 2                     # un modèle de conversation et un modèle d'embedding
CHAT_CONTEXT = context.LOW            # contexte d'une conversation : llm/context.py
EMBED_CONTEXT = 2048
START_TIMEOUT = 60
NO_PROXY = {"http": None, "https": None}


class EngineUnavailable(RuntimeError):
    pass


def presets(models: list) -> str:
    """Le fichier de préréglages du routeur, une section par modèle."""
    sections = []
    for m in models:
        lines = [f"[{m.id}]", f"model = {m.path}"]
        if m.mmproj:
            lines.append(f"mmproj = {m.mmproj}")
        if m.embedding:
            lines += ["embeddings = true", f"ctx-size = {EMBED_CONTEXT}",
                      f"batch-size = {EMBED_CONTEXT}", f"ubatch-size = {EMBED_CONTEXT}"]
        else:
            lines.append(f"ctx-size = {context.chat_context(m.name)}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


class Router:
    def __init__(self):
        self._lock = threading.RLock()
        self.process = None
        self.key = secrets.token_urlsafe(24)
        self._signature = None         # empreinte des préréglages du routeur en marche

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    def url(self) -> str:
        """Adresse du routeur, lancé ou relancé s'il le faut (liste des modèles changée)."""
        with self._lock:
            models = registry.available()
            text = presets(models)
            signature = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if self.process is not None and self.process.alive and signature == self._signature:
                return f"http://127.0.0.1:{self.process.port}"
            self._stop()
            folder = programs.folder()
            if folder is None:
                raise EngineUnavailable("llama.cpp est introuvable sur cette machine : le moteur "
                                        "intégré ne peut pas démarrer.")
            ini = paths.data_dir() / "llm" / "modeles.ini"
            ini.parent.mkdir(parents=True, exist_ok=True)
            ini.write_text(text, encoding="utf-8")
            port = programs.free_port()
            args = ["--models-preset", str(ini), "--models-max", str(MODELS_MAX),
                    "--models-autoload", "--host", "127.0.0.1", "--port", str(port),
                    "--no-webui", "--fit", "on"]
            # La clé par l'environnement : les llama-server de chaque modèle en héritent
            process = programs.Process("llama-routeur", folder / f"{programs.SERVER}{programs.EXE}",
                                       args, port, env={programs.KEY_VARIABLE: self.key})
            try:
                process.start(START_TIMEOUT, ready=lambda: programs.loaded(port))
            except programs.EngineError as exc:
                raise EngineUnavailable(f"Le moteur intégré ne démarre pas : {exc}") from exc
            self.process, self._signature = process, signature
            log.info("Moteur intégré prêt : %d modèle(s), port %d", len(models), port)
            return f"http://127.0.0.1:{port}"

    def status(self) -> dict:
        """{identifiant: « loaded », « loading », « unloaded »…} ; {} si le routeur dort."""
        with self._lock:
            if self.process is None or not self.process.alive:
                return {}
            base = f"http://127.0.0.1:{self.process.port}"
        try:
            data = requests.get(base + "/models", headers=self.headers, timeout=5,
                                proxies=NO_PROXY).json().get("data") or []
        except (requests.RequestException, ValueError):
            return {}
        return {m.get("id"): (m.get("status") or {}).get("value") for m in data}

    def unload(self, model_id: str) -> bool:
        with self._lock:
            if self.process is None or not self.process.alive:
                return False
            base = f"http://127.0.0.1:{self.process.port}"
        try:
            return requests.post(base + "/models/unload", json={"model": model_id},
                                 headers=self.headers, timeout=10,
                                 proxies=NO_PROXY).status_code == 200
        except requests.RequestException:
            return False

    def stop(self):
        with self._lock:
            self._stop()

    def _stop(self):
        if self.process is not None:
            self.process.stop()
        self.process, self._signature = None, None


def log_path() -> Path:
    return paths.log_path().parent / "llama-routeur.log"
