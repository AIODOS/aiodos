"""Le nœud AIODOS dans l'appli Android, sans Termux (android/, NodeService.java).

Le service au premier plan de l'appli démarre Python (Chaquopy) dans son propre processus,
puis appelle start() : les mêmes étapes que « python -m aiodos --headless » (__main__.py),
sauf ce qui n'a pas de sens dans une appli :
- le code ne se met pas à jour par le réseau : c'est celui de l'appli, et une nouvelle
  version arrive avec une nouvelle appli (updates/service.py le reçoit et le transmet
  quand même aux autres machines) ;
- les programmes (llama.cpp, le compagnon Internet) viennent du dossier des bibliothèques
  de l'appli, le seul d'où Android permet d'exécuter un programme. Rangés sous des noms en
  « lib….so » (tools/build_android.py), ils sont rendus à AIODOS sous leurs vrais noms par
  des liens : engines/llama.cpp/llama-server → <bibliothèques>/libllama-server.so ;
- la batterie et le Wi-Fi se lisent par Android (Device.java) au lieu de Termux:API.

Les données : <dossier de l'appli>/data, privé à l'appli (effacé avec elle).
À la première ouverture, « Démarrer avec le téléphone » est activé : le nœud sert les
autres machines sans qu'on ait à ouvrir l'appli. On peut le retirer dans « Cette machine ».
"""
import json
import logging
import os
import secrets
import threading
from pathlib import Path

log = logging.getLogger("aiodos")

APP_ENV = "AIODOS_ANDROID_APP"
ENGINES_ENV = "AIODOS_ENGINES_DIR"            # lu par updates/engines.py
AUTOSTART_ENV = "AIODOS_AUTOSTART_FLAG"       # lu par node/autostart.py
AUTOSTART = "autostart"                       # lu par BootReceiver.java
AUTOSTART_CHOSEN = "autostart-choisi"
COMPANION = "libaiodos-net.so"
# Les programmes rangés parmi les bibliothèques de l'appli, sous leur vrai nom
PROGRAMS = {"llama.cpp": {"llama-server": "libllama-server.so",
                          "ggml-rpc-server": "libggml-rpc-server.so"}}

_lock = threading.Lock()
_running = {"node": None, "url": None}


def in_app() -> bool:
    """Ce Python tourne-t-il dans l'appli Android (et non sous Termux, ni sur un PC) ?"""
    return os.environ.get(APP_ENV) == "1"


def _device():
    from java import jclass          # module de Chaquopy : seulement dans l'appli
    return jclass("fr.arkane.aiodos.Device")


def battery() -> dict | None:
    """La batterie lue par Android, au format de termux-battery-status (node/power.py),
    ou None hors de l'appli ou si Android ne la donne pas."""
    if not in_app():
        return None
    try:
        text = str(_device().battery() or "")
        return json.loads(text) if text else None
    except Exception as exc:  # noqa: BLE001 — erreur Java comprise : on ne sait pas
        log.debug("Batterie illisible : %s", exc)
        return None


def on_wifi() -> bool | None:
    """Relié à un Wi-Fi (et pas aux données mobiles) ? None si on ne sait pas."""
    if not in_app():
        return None
    try:
        value = int(_device().wifi())
    except Exception as exc:  # noqa: BLE001
        log.debug("Wi-Fi illisible : %s", exc)
        return None
    return None if value < 0 else value == 1


def link_programs(files_dir, native_dir) -> Path:
    """engines/<moteur>/<programme> → la bibliothèque qui le contient. Refaits à chaque
    démarrage : le dossier des bibliothèques change à chaque mise à jour de l'appli."""
    root = Path(files_dir) / "engines"
    for engine, programs in PROGRAMS.items():
        folder = root / engine
        folder.mkdir(parents=True, exist_ok=True)
        for name, library in programs.items():
            link, target = folder / name, Path(native_dir) / library
            if link.is_symlink() or link.exists():
                link.unlink()
            if target.is_file():
                link.symlink_to(target)
    return root


def prepare(files_dir, native_dir, cache_dir):
    """L'environnement du nœud dans l'appli (avant d'importer le reste d'AIODOS)."""
    native_dir = str(native_dir)
    libraries = [p for p in os.environ.get("LD_LIBRARY_PATH", "").split(":")
                 if p and p != native_dir]
    os.environ.update({
        APP_ENV: "1",
        "AIODOS_DATA_DIR": str(Path(files_dir) / "data"),
        "AIODOS_NET_BIN": str(Path(native_dir) / COMPANION),
        ENGINES_ENV: str(link_programs(files_dir, native_dir)),
        AUTOSTART_ENV: str(Path(files_dir) / AUTOSTART),
        "TMPDIR": str(cache_dir),
        # Les programmes de llama.cpp trouvent leurs bibliothèques à côté d'eux
        "LD_LIBRARY_PATH": ":".join([native_dir, *libraries]),
    })
    chosen = Path(files_dir) / AUTOSTART_CHOSEN
    if not chosen.exists():
        (Path(files_dir) / AUTOSTART).write_text(
            "Démarrer le nœud AIODOS avec le téléphone (appli AIODOS).\n", encoding="utf-8")
        chosen.write_text("Démarrage avec le téléphone activé à la première ouverture.\n",
                          encoding="utf-8")


def start(files_dir: str, native_dir: str, cache_dir: str) -> str:
    """Démarre le nœud, une fois par processus, et rend l'adresse de son interface (jeton
    compris). L'erreur du démarrage remonte telle quelle à l'appli, qui l'affiche."""
    with _lock:
        if _running["url"]:
            return _running["url"]
        prepare(files_dir, native_dir, cache_dir)
        from aiodos import __main__ as entry
        from aiodos import instance
        entry._setup_logging()
        args = entry.parser().parse_args(["--headless"])
        port = entry._free_port()
        token, show_key = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        if not instance.acquire(port, show_key):
            raise entry.NodeError("Un autre nœud AIODOS utilise déjà ces données.")
        node = entry._Node(args, port, token, show_key)
        try:
            url = node.start()
        except BaseException:
            node.stop()
            instance.release()
            raise
        _running.update(node=node, url=url)
        log.info("Nœud AIODOS de l'appli Android prêt")
        threading.Thread(target=_check_engine, daemon=True, name="aiodos-moteur").start()
        return url


def _check_engine():
    """Au journal : llama.cpp s'exécute-t-il depuis l'appli ? (sinon, rien ne tournerait ici)"""
    from aiodos.cluster import engine
    try:
        found = engine.info(max_age=0)
    except Exception as exc:  # noqa: BLE001 — au journal seulement
        found = None
        log.warning("llama.cpp de l'appli illisible : %s", exc)
    if found:
        log.info("llama.cpp %s prêt dans l'appli (%s)", found["version"],
                 ", ".join(d["name"] for d in found["devices"]) or "processeur")
    else:
        log.warning("llama.cpp introuvable ou inexécutable dans l'appli : aucun modèle ne "
                    "tournera sur ce téléphone")


def stop():
    """Arrêt complet (compagnon, llama.cpp), avant que l'appli quitte son processus."""
    with _lock:
        node = _running["node"]
        if node is None:
            return
        from aiodos import instance
        try:
            node.stop()
        finally:
            instance.release()
            _running.update(node=None, url=None)
