"""Le moteur intégré sans routeur : un llama-server par modèle, lancé par AIODOS.

Le llama.cpp officiel pour Android (b11707) est compilé sans sous-programmes : son
llama-server refuse le mode routeur (« subprocess is not enabled on this build », vu sur le
S23 le 27/10/2026). llm/engine.Router passe alors ici, avec les mêmes fonctions (url, status,
unload, stop) :
- les mêmes réglages par modèle que les préréglages du routeur (engine.options), en
  arguments ; « --alias » donne au serveur l'identifiant que les demandes portent ;
- au plus `limit` modèles chargés (MODELS_MAX du routeur) : le moins récemment servi est
  arrêté pour faire place, comme le routeur décharge le plus ancien ;
- la même clé, par l'environnement, exigée par chacun (cluster/engine.Process.start).
Un modèle est chargé au lancement de son serveur : `url` attend qu'il soit prêt.
"""
import logging
import threading
import time

from aiodos.cluster import engine as programs
from aiodos.i18n import t

log = logging.getLogger("aiodos.llm")

NO_ROUTER = "subprocess is not enabled"   # réponse d'un llama-server sans mode routeur
LOAD_TIMEOUT = 600                         # un gros modèle sur un processeur lent


def arguments(options: list) -> list:
    """Préréglages du routeur ([(clé, valeur)], engine.options) en arguments de llama-server :
    « embeddings = true » devient « --embeddings »."""
    args = []
    for key, value in options:
        args += [f"--{key}"] if value == "true" else [f"--{key}", str(value)]
    return args


class Servers:
    def __init__(self, folder, key: str, limit: int, extra: list):
        self.folder, self.key, self.limit, self.extra = folder, key, limit, extra
        self._running = {}             # identifiant -> (Process, arguments)
        self._used = {}                # identifiant -> instant du dernier service
        self._state = threading.Lock()     # jamais tenu pendant un chargement

    def url(self, models: list, model_id: str, options) -> str:
        """Adresse du serveur de ce modèle, lancé s'il le faut. L'appelant (Router) tient son
        verrou : un seul lancement à la fois. EngineError si le modèle n'est pas là ou si son
        serveur ne démarre pas."""
        by_id = {m.id: m for m in models}
        for gone in [i for i in self._snapshot() if i not in by_id]:
            self.unload(gone)              # modèle écarté ou effacé
        model = by_id.get(model_id)
        if model is None:
            raise programs.EngineError(t("llm.model_missing", name=model_id))
        args = arguments(options(model)) + ["--alias", model.id, *self.extra]
        with self._state:
            current = self._running.get(model.id)
            self._used[model.id] = time.monotonic()
        if current and current[0].alive and current[1] == args:
            return f"http://127.0.0.1:{current[0].port}"
        self.unload(model.id)              # arrêté, ou ses réglages ont changé
        self._make_room()
        port = programs.free_port()
        program = self.folder / f"{programs.SERVER}{programs.EXE}"
        process = programs.Process(f"llama-{model.id}", program,
                                   [*args, "--host", "127.0.0.1", "--port", str(port)], port,
                                   env={programs.KEY_VARIABLE: self.key})
        process.start(LOAD_TIMEOUT, ready=lambda: programs.loaded(port))
        with self._state:
            self._running[model.id] = (process, args)
            self._used[model.id] = time.monotonic()
        log.info("Moteur intégré : « %s » chargé seul, port %d", model.id, port)
        return f"http://127.0.0.1:{port}"

    def _snapshot(self) -> dict:
        with self._state:
            return dict(self._running)

    def _make_room(self):
        """Arrête les moins récemment servis tant que `limit` serveurs tournent."""
        while True:
            with self._state:
                alive = [i for i, (p, _) in self._running.items() if p.alive]
                oldest = min(alive, key=lambda i: self._used.get(i, 0.0)) if alive else None
            if len(alive) < self.limit or oldest is None:
                return
            log.info("Moteur intégré : « %s » déchargé pour faire place", oldest)
            self.unload(oldest)

    def status(self) -> dict:
        """{identifiant: « loaded »} des serveurs en marche (comme /models du routeur)."""
        return {i: "loaded" for i, (p, _) in self._snapshot().items() if p.alive}

    def unload(self, model_id: str) -> bool:
        with self._state:
            current = self._running.pop(model_id, None)
        if current is None:
            return False
        was_alive = current[0].alive
        current[0].stop()
        return was_alive

    def stop(self):
        for model_id in list(self._snapshot()):
            self.unload(model_id)
