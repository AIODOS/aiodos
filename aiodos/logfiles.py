"""Journaux bornés : ce qui s'écrit sans fin ne remplit ni le disque ni le téléphone.

- BoundedFile : un fichier de `limit` octets au plus, puis sa fin passe dans <nom>.1. Il
  sert à erreurs.log (la sortie d'erreur sans console, __main__.py) et aux journaux des
  programmes qu'AIODOS lance.
- capture : la sortie d'un programme lancé (llama-server, ggml-rpc-server, sd-server), lue
  par un fil et écrite dans un BoundedFile. Ces programmes écrivent des lignes pour chaque
  demande servie (chaque embedding, y compris ceux calculés pour un ami, chaque recherche ou
  conversation prêtée) jusqu'à leur arrêt. Le fichier ouvert en écriture et laissé au
  programme grandissait sans limite, des jours durant pour le moteur intégré (faille 11 de
  l'audit du 20/10/2026, toujours ouverte à la relecture finale du 21/10).
"""
import io
import os
import threading
from pathlib import Path

PIECE = 64 * 1024                  # lu à la fois dans la sortie d'un programme


class BoundedFile(io.FileIO):
    """Un fichier de `limit` octets au plus, puis <nom>.1. La limite tient au niveau des
    octets, pour tout ce qui y passe (texte, ou sys.stderr.buffer qu'une bibliothèque peut
    utiliser directement) : une écriture qui la dépasserait fait d'abord passer la fin du
    fichier dans <nom>.1. Le fichier est réduit par son propre descripteur, pas par son
    chemin : effacé à la main pendant qu'il est ouvert (possible hors Windows), il reste
    borné. Un verrou garde deux fils de réduire en même temps (le second écraserait <nom>.1
    par une copie presque vide)."""

    def __init__(self, path: Path, limit: int):
        super().__init__(path, "a+")
        self._backup = Path(path).with_name(Path(path).name + ".1")
        self._limit = limit
        self._lock = threading.Lock()
        with self._lock:
            size = self._size()
            if size > limit:                     # laissé trop gros par une version d'avant
                self._rotate(size)

    def write(self, data) -> int:
        view = memoryview(data).cast("B")
        with self._lock:
            size = self._size()
            if size and size + len(view) > self._limit:
                self._rotate(size)
            part = view[-self._limit:] if len(view) > self._limit else view
            while part:
                part = part[super().write(part):]
        return len(view)

    def _size(self) -> int:
        try:
            return os.fstat(self.fileno()).st_size
        except OSError:
            return 0

    def _rotate(self, size: int):
        """Sa fin (à partir d'une ligne entière) dans <nom>.1, puis il est vidé ; il reste le
        même fichier pour un autre processus qui l'a ouvert (le lanceur)."""
        start = max(0, size - self._limit)
        try:
            self.seek(start)
            tail = self.read(self._limit) or b""
            if start:
                cut = tail.find(b"\n")
                tail = tail[cut + 1:] if cut >= 0 else b""
            with open(self._backup, "wb") as out:
                out.write(tail)
        except OSError:      # disque plein, sauvegarde ouverte ailleurs : la place d'abord
            pass
        try:
            self.truncate(0)
        except OSError:
            pass


def capture(stream, path: Path, limit: int) -> threading.Thread:
    """Copie `stream` (la sortie d'un programme, en octets) dans `path`, de `limit` octets
    au plus (puis <nom>.1), jusqu'à sa fin, dans un fil à part ; le journal repart de rien
    (celui du lancement précédent est remplacé : il dit pourquoi ce lancement-ci échoue). Une
    écriture impossible (disque plein) est perdue, la lecture continue : le programme
    n'attend jamais sur un tuyau plein. → le fil, fini quand le programme (et ce qu'il a
    lancé lui-même avec la même sortie) l'a fermée."""
    sink = BoundedFile(path, limit)
    with sink._lock:
        try:
            sink.truncate(0)
        except OSError:
            pass
    try:
        sink._backup.unlink(missing_ok=True)
    except OSError:
        pass
    read = getattr(stream, "read1", None) or stream.read

    def run():
        try:
            while True:
                try:
                    piece = read(PIECE)
                except (OSError, ValueError):
                    break
                if not piece:
                    break
                try:
                    sink.write(piece)
                except (OSError, ValueError):
                    pass
        finally:
            sink.close()
            try:
                stream.close()
            except OSError:
                pass

    thread = threading.Thread(target=run, daemon=True, name=f"aiodos-journal-{path.stem}")
    thread.start()
    return thread
