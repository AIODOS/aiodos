"""Le fichier de découverte gateway.json (docs/api-applications.md § 1.2) : où une
application trouve la passerelle d'AIODOS, et dans quel état elle est.

- Windows : %LOCALAPPDATA%\\AIODOS\\gateway.json ; ailleurs : $XDG_STATE_HOME/aiodos/
  (~/.local/state/aiodos/). AIODOS_DISCOVERY_FILE en donne un autre ; « - » coupe l'écriture
  (les essais s'en servent pour ne jamais toucher au vrai fichier).
- Jamais écrit par un nœud lancé par les essais (AIODOS_NO_DIALOG), ni par une instance qui
  n'utilise pas le dossier de données habituel (nœud d'essai, --data-dir).
- Écrit au démarrage, à chaque ouverture, fermeture ou changement de port de la passerelle,
  et à l'arrêt (running: false) ; d'un coup (fichier temporaire, puis os.replace).
- Jamais dedans : une clé, le jeton ou le port de l'interface, une commande, un nom de
  machine ou d'utilisateur. Les clients n'en retiennent que l'adresse, et seulement si elle
  est sur cette machine.
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

from aiodos import __version__, instance

log = logging.getLogger("aiodos.gateway")

ENV = "AIODOS_DISCOVERY_FILE"
FORMAT = 1


def path() -> Path | None:
    chosen = os.environ.get(ENV)
    if chosen == "-":
        return None
    if chosen:
        return Path(chosen)
    if os.environ.get(instance.NO_DIALOG_ENV) or not _default_data_dir():
        return None
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            return None
        return Path(base) / "AIODOS" / "gateway.json"
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "aiodos" / "gateway.json"


def _default_data_dir() -> bool:
    """Cette instance utilise-t-elle le dossier de données habituel (celui de la copie
    installée, ou data/ à côté du code) ? Un nœud d'essai lancé avec un autre dossier
    (--data-dir, AIODOS_DATA_DIR) n'écrit pas le fichier ; la version lancée par le lanceur
    des mises à jour, elle, reçoit le dossier habituel."""
    from aiodos import paths
    default = paths.installed_data_dir() or (paths.APP_ROOT / "data")
    try:
        return paths.data_dir().resolve() == Path(default).resolve()
    except OSError:
        return False


def content(*, running: bool, state: str, url: str, apps: bool, requests: bool,
            approval_ui: bool, error: str | None) -> dict:
    from aiodos.gateway import info
    return {"product": "aiodos", "format": FORMAT, "version": __version__, "api": info.API,
            "pid": os.getpid(), "instance": info.INSTANCE, "running": running,
            "gateway": {"state": state, "url": url, "apps": apps, "requests": requests,
                        "approval_ui": approval_ui, "error": error},
            "updated": time.time()}


def write(data: dict) -> bool:
    """Écrit le fichier d'un coup ; faux s'il n'y en a pas, ou en cas d'échec (journal). Une
    autre instance d'AIODOS vivante (une copie de développement à côté de la version
    installée…) garde le fichier qu'elle a écrit la première."""
    target = path()
    if target is None:
        return False
    current = read()
    if (isinstance(current, dict) and current.get("running")
            and isinstance(current.get("pid"), int) and current["pid"] != os.getpid()
            and instance._alive(current["pid"])):
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + f".{os.getpid()}.part")
        part.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(part, target)
        return True
    except OSError as exc:
        log.warning("Fichier de découverte %s non écrit : %s", target, exc)
        return False


def read() -> dict | None:
    target = path()
    try:
        return json.loads(target.read_text(encoding="utf-8")) if target else None
    except (OSError, ValueError):
        return None


def mark_stopped():
    """À l'arrêt : running false, si c'est bien cette instance qui l'avait écrit."""
    current = read()
    if not isinstance(current, dict) or current.get("pid") != os.getpid():
        return
    current["running"] = False
    current["gateway"] = {**(current.get("gateway") or {}), "state": "closed"}
    current["updated"] = time.time()
    write(current)
