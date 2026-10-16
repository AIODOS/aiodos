"""État des mises à jour, dans data/updates/state.json :

    current   version installée qui a démarré correctement (absente : version d'origine)
    pending   {"version", "attempts", "previous", "notes", "failed_at"} : installée,
              à essayer au prochain démarrage
    refused   {version: {"reason", "at"}} : versions qui ne s'installent pas ou ne
              démarrent pas ici, à ne plus essayer
    notice    {"kind": "updated" | "rollback", "version", …} : à montrer une fois
    restarts  instants des derniers redémarrages pour mise à jour

Le lanceur de la version d'origine lit ce fichier à chaque démarrage : son format
ne doit pas changer (on peut seulement y ajouter des clés). Écrit sous un verrou,
de façon atomique, car le lanceur et le nœud qu'il a démarré y écrivent tous deux.
"""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_WAIT = 5.0
LOCK_STALE = 30.0


def _file(root: Path) -> Path:
    return Path(root) / "state.json"


def load(root: Path) -> dict:
    try:
        data = json.loads(_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextmanager
def _locked(root: Path):
    lock = Path(root) / "state.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:                          # verrou d'un processus arrêté en route
                if time.time() - lock.stat().st_mtime > LOCK_STALE:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("État des mises à jour verrouillé.") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def update(root: Path, change) -> dict:
    """Lit l'état, le passe à change(état) qui le modifie sur place, puis l'enregistre."""
    with _locked(root):
        data = load(root)
        change(data)
        target = _file(root)
        temporary = target.with_name(f"state.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, target)
        return data


def refuse(data: dict, version: str, reason: str):
    """À appeler dans update() : cette version ne sera plus essayée ici."""
    data.setdefault("refused", {})[version] = {"reason": reason[:300], "at": time.time()}
    if (data.get("pending") or {}).get("version") == version:
        data["pending"] = None
    if data.get("current") == version:
        data["current"] = None
