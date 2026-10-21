"""Stockage (audit de sécurité du 20/10/2026) : des journaux bornés, qu'un appareil du réseau
ne peut pas faire grossir jusqu'à remplir le disque en provoquant des erreurs en boucle
(__main__._setup_logging), et hors Windows des données lisibles par l'utilisateur seul
(paths.keep_private) : la base y garde en clair les clés des machines appairées."""
import io
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from aiodos import __main__ as main
from aiodos import paths

APP_ROOT = Path(__file__).resolve().parent.parent
posix_only = pytest.mark.skipif(os.name == "nt", reason="droits Unix")
LIMIT = 64 * 1024
MARGIN = 8 * 1024                     # une trace d'erreur, un tampon d'écriture


def _run(script: str, data: Path, *args, **options) -> subprocess.CompletedProcess:
    """Un nœud à part : la journalisation se règle une fois par processus."""
    env = dict(os.environ, AIODOS_DATA_DIR=str(data), PYTHONIOENCODING="utf-8")
    if "stdout" not in options and "stderr" not in options:
        options["capture_output"] = True
    return subprocess.run([sys.executable, "-c", script, *args], cwd=APP_ROOT, env=env,
                          text=True, encoding="utf-8", timeout=120, **options)


# --- Journaux bornés --------------------------------------------------------------------

FLOOD = r'''
import logging
from aiodos import __main__ as main, paths
main.LOG_BYTES = %d
journal = paths.log_path()
journal.write_bytes(b"ancienne ligne\n" * 400000)   # 6 Mio laissés par une version d'avant
held = open(journal, "a", encoding="utf-8")         # le lanceur d'une version d'origine
main._setup_logging()
for i in range(3000):                                # 300 erreurs différentes, 10 fois chacune
    try:
        raise ValueError(f"requête malformée {i}")
    except ValueError:
        logging.getLogger(f"uvicorn.error.{i %% 300}").exception("Exception in ASGI application")
held.write("ligne du lanceur\n")
held.flush()
logging.getLogger("aiodos").warning("dernière ligne")
logging.shutdown()
print("fini")
''' % LIMIT


def test_the_log_stays_bounded_under_a_flood_of_errors(tmp_path):
    done = _run(FLOOD, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Logging error" not in done.stderr        # rotation possible, fichier tenu ailleurs
    logs = tmp_path / "logs"
    files = sorted(p.name for p in logs.glob("aiodos.log*"))
    assert files == ["aiodos.log", "aiodos.log.1", "aiodos.log.2", "aiodos.log.3"]
    for name in files:
        assert (logs / name).stat().st_size <= LIMIT + MARGIN, name
    current = (logs / "aiodos.log").read_text(encoding="utf-8")
    assert "dernière ligne" in current
    assert "ligne du lanceur" in current + (logs / "aiodos.log.1").read_text(encoding="utf-8")


ERRORS = r'''
import sys
from aiodos import __main__ as main
main.ERRORS_BYTES = %d
if "--sans-console" in sys.argv:
    sys.stderr = None                                # pythonw : pas de console
main._setup_logging()
for i in range(20000):
    print(f"avertissement d'une bibliothèque n° {i}", file=sys.stderr)
print("fin", file=sys.stderr)
sys.stderr.flush()
''' % LIMIT


def _check_errors_log(logs: Path):
    current = (logs / "erreurs.log").read_bytes()
    backup = (logs / "erreurs.log.1").read_bytes()
    assert len(current) <= LIMIT + MARGIN and len(backup) <= LIMIT + MARGIN
    assert b"\0" not in current and b"\0" not in backup
    assert current.rstrip().endswith(b"fin")
    assert backup.lstrip().startswith(b"avertissement")       # des lignes entières


def test_the_errors_log_is_bounded_without_a_console(tmp_path):
    done = _run(ERRORS, tmp_path, "--sans-console")
    assert done.returncode == 0, done.stdout + done.stderr
    _check_errors_log(tmp_path / "logs")


def test_a_relaunched_version_bounds_the_errors_log_it_inherits(tmp_path):
    """Sous Windows, updates/restart.relaunch donne au nouveau processus le erreurs.log de
    l'ancien comme sortie d'erreur : il doit le borner lui aussi."""
    logs = tmp_path / "logs"
    logs.mkdir()
    with open(logs / "erreurs.log", "ab") as inherited:
        done = _run(ERRORS, tmp_path, stdout=subprocess.PIPE, stderr=inherited)
    assert done.returncode == 0, done.stdout
    _check_errors_log(logs)


def test_rotation_keeps_whole_lines_and_the_same_file(tmp_path):
    journal = tmp_path / "j.log"
    journal.write_bytes(b"".join(b"ligne %05d\n" % i for i in range(10000)))
    with open(journal, "ab") as held:                # gardé ouvert par un autre processus
        main._keep_tail(journal, tmp_path / "j.log.1", 1000)
        held.write(b"suite\n")
        held.flush()
    backup = (tmp_path / "j.log.1").read_bytes()
    assert len(backup) <= 1000 and backup.startswith(b"ligne ")
    assert backup.endswith(b"ligne 09999\n")
    assert journal.read_bytes() == b"suite\n"


def test_rotation_of_a_log_deleted_meanwhile_does_not_fail(tmp_path):
    """Un journal effacé à la main pendant que le nœud tourne (possible hors Windows) :
    la rotation suivante ne lève rien, sans quoi la ligne en cours serait perdue."""
    main._keep_tail(tmp_path / "efface.log", tmp_path / "efface.log.1", 1000)
    assert not (tmp_path / "efface.log.1").exists()


class _Kept(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_an_error_repeated_in_a_loop_is_written_a_few_times_per_minute():
    clock = [1000.0]
    repeats = main._Repeats(clock=lambda: clock[0])
    first, second = _Kept(), _Kept()                 # le fichier et la console
    logger = logging.getLogger("essai.repetitions")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    for handler in (first, second):
        handler.addFilter(repeats)
        logger.addHandler(handler)

    def flood(times):
        for i in range(times):
            try:
                raise ValueError(f"pub {i}")
            except ValueError:
                logger.exception("Exception in ASGI application\n")

    try:
        flood(100)
        kept = main._Repeats.KEPT
        assert len(first.records) == len(second.records) == kept + 1
        assert all(r.exc_info for r in first.records[:kept])
        notice = first.records[-1]
        assert notice.exc_info is None and "plus écrite pendant une minute" in notice.getMessage()
        for i in range(50):
            logger.info("unité %d terminée", i)       # les informations passent toutes
        logger.error("autre chose")                   # une autre erreur aussi
        assert len(first.records) == kept + 1 + 51
        clock[0] += 61
        flood(1)
        assert "(et 89 fois de plus, non écrites)" in first.records[-1].getMessage()
        assert first.records[-1].exc_info
    finally:
        for handler in (first, second):
            logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)


def test_distinct_messages_of_the_companion_are_all_written():
    """Le compagnon verse son journal par log.warning("%s", ligne) : des lignes différentes
    ne sont pas « la même » alerte."""
    repeats = main._Repeats(clock=lambda: 0.0)
    kept = _Kept()
    kept.addFilter(repeats)
    logger = logging.getLogger("essai.compagnon")
    logger.propagate = False
    logger.addHandler(kept)
    try:
        for i in range(30):
            logger.warning("%s", f"relais {i} injoignable")
        assert len(kept.records) == 30
        for _ in range(30):                           # la même ligne : KEPT fois, puis l'avis
            logger.warning("%s", "relais 0 injoignable")
        assert len(kept.records) == 30 + main._Repeats.KEPT + 1
    finally:
        logger.removeHandler(kept)


# --- Données lisibles par l'utilisateur seul --------------------------------------------

def _with_mode(info: os.stat_result, mode: int) -> os.stat_result:
    values = list(info)
    values[0] = stat.S_IFMT(info.st_mode) | mode
    return os.stat_result(values)


@pytest.fixture
def unix(monkeypatch):
    """Droits Unix simulés (les essais tournent aussi sous Windows) : chmod et umask notés,
    et os.stat relit les droits posés par chmod (keep_private vérifie qu'ils ont pris).
    L'état de départ se fixe par _older : sans lui, os.stat donne les droits réels."""
    calls = {"chmod": {}, "umask": []}
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        mode = calls["chmod"].get(Path(path)) if isinstance(path, (str, os.PathLike)) else None
        return info if mode is None else _with_mode(info, mode)
    monkeypatch.setattr(paths, "_posix", lambda: True)
    monkeypatch.setattr(paths.os, "chmod",
                        lambda path, mode: calls["chmod"].__setitem__(Path(path), mode))
    monkeypatch.setattr(paths.os, "stat", fake_stat)
    monkeypatch.setattr(paths.os, "umask", lambda mask: calls["umask"].append(mask) or 0o022)
    monkeypatch.setenv(paths.USER_UMASK_ENV, "")     # keep_private le pose : rendu ensuite
    monkeypatch.setattr(paths, "_user_umask", None)
    monkeypatch.setattr(paths, "_left_open", [])
    return calls


def _older(unix, folder: Path):
    """Les droits d'une version d'avant le 20/10/2026 (umask 022) : 0755 et 0644, sous
    Windows comme sous Unix, quel que soit l'umask de qui lance les essais."""
    for path in (folder, *folder.rglob("*")):
        unix["chmod"][path] = 0o755 if path.is_dir() else 0o644


def test_the_data_folder_is_closed_to_other_accounts(unix, tmp_path, monkeypatch):
    folder = tmp_path / "donnees"
    folder.mkdir()
    _older(unix, folder)
    monkeypatch.setenv("AIODOS_DATA_DIR", str(folder))
    assert paths.data_dir() == folder
    assert unix["chmod"][folder] == 0o700


def test_files_left_by_an_older_version_become_private_at_start(unix, tmp_path, monkeypatch):
    folder = tmp_path / "donnees"
    (folder / "logs").mkdir(parents=True)
    names = ["aiodos.db", "aiodos.db-wal", "aiodos.db-shm", "instance.json",
             "logs/aiodos.log", "logs/erreurs.log", "logs/llama-routeur.log"]
    for name in names:
        (folder / name).write_text("secret", encoding="utf-8")
    _older(unix, folder)
    monkeypatch.setenv("AIODOS_DATA_DIR", str(folder))
    assert paths.keep_private() == []
    assert unix["umask"] == [0o077]                   # tout ce qui sera créé ensuite
    assert unix["chmod"][folder] == 0o700 and unix["chmod"][folder / "logs"] == 0o700
    for name in names:
        assert unix["chmod"][folder / name] == 0o600, name


def test_rights_that_cannot_change_are_listed_not_logged_early(unix, tmp_path, monkeypatch,
                                                               caplog):
    """Disque sans droits Unix (carte mémoire, partage) : keep_private rend ce qui reste
    ouvert, dossier compris, sans rien écrire lui-même (la journalisation n'est pas encore
    prête : ce serait perdu pour aiodos.log)."""
    folder = tmp_path / "donnees"
    (folder / "logs").mkdir(parents=True)
    for name in ("aiodos.db", "instance.json", "logs/aiodos.log"):
        (folder / name).write_text("secret", encoding="utf-8")
    _older(unix, folder)

    def refuse(path, mode):
        raise PermissionError("opération non permise")
    monkeypatch.setattr(paths.os, "chmod", refuse)
    monkeypatch.setenv("AIODOS_DATA_DIR", str(folder))
    with caplog.at_level(logging.DEBUG):
        left = paths.keep_private()
        paths.data_dir()                           # déjà fait : pas d'avertissement de plus
    assert caplog.records == []
    assert left == [f"{folder} (le dossier)", "aiodos.db", "instance.json", "logs",
                    "logs/aiodos.log"]


WARNED = r'''
import logging, os
from aiodos import __main__ as main, paths
folder = paths._data_path()
(folder / "logs").mkdir(parents=True, exist_ok=True)
for name in ("aiodos.db", "instance.json"):
    (folder / name).write_text("secret", encoding="utf-8")
os.chmod(folder, 0o755)                              # créé par une version d'avant
paths._posix = lambda: True                          # Unix simulé, disque sans droits Unix
os.umask = lambda mask: 0o022
def refuse(path, mode):
    raise PermissionError("opération non permise")
os.chmod = refuse
main._setup_logging()
logging.shutdown()
'''


def test_rights_that_cannot_change_are_reported_once_in_the_log(tmp_path):
    """L'avertissement arrive dans aiodos.log (l'appli Android, la fenêtre lancée du bureau
    n'ont pas d'autre sortie), en une seule ligne, dossier compris."""
    done = _run(WARNED, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    journal = (tmp_path / "logs" / "aiodos.log").read_text(encoding="utf-8")
    lines = [line for line in journal.splitlines() if "restent lisibles" in line]
    assert len(lines) == 1, journal
    assert "WARNING" in lines[0] and "le dossier" in lines[0]
    assert all(name in lines[0] for name in ("aiodos.db", "instance.json", "logs"))
    assert "restent lisibles" not in done.stderr


def _stat(mode: int, uid: int) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, uid, 10, 0, 0, 0))


def test_links_in_the_data_folder_are_followed(unix, tmp_path, monkeypatch):
    """La base ou un journal déplacés sur un autre disque, remplacés par un lien : le
    fichier lié est fermé s'il est à l'utilisateur, signalé sinon. Un dossier lié (des
    modèles partagés) garde ses droits, mais il est signalé s'il est ouvert."""
    folder = tmp_path / "donnees"
    (folder / "logs").mkdir(parents=True)
    targets = {                                  # lien → ce que donne os.stat (suivi)
        folder / "aiodos.db": _stat(0o100644, 1000),
        folder / "logs" / "aiodos.log": _stat(0o100644, 1001),
        folder / "models": _stat(0o040755, 1000),
        folder / "images": _stat(0o040700, 1000),
        folder / "casse": None,                  # lien cassé
    }
    for link in targets:
        link.write_text("", encoding="utf-8")    # pas de vrais liens sous Windows
    real_stat = os.stat                          # celui de la fixture unix

    def fake_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) in targets:
            target = targets[Path(path)]
            if target is None:
                raise FileNotFoundError(path)
            changed = unix["chmod"].get(Path(path))
            return target if changed is None else _with_mode(target, changed)
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(paths.os, "stat", fake_stat)
    monkeypatch.setattr(paths.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(paths.Path, "is_symlink", lambda self: self in targets)
    monkeypatch.setenv("AIODOS_DATA_DIR", str(folder))
    left = paths.keep_private()
    assert unix["chmod"][folder / "aiodos.db"] == 0o600           # sa cible, fermée
    for link in (folder / "logs" / "aiodos.log", folder / "models", folder / "images",
                 folder / "casse"):
        assert link not in unix["chmod"], link
    assert left == ["models (dossier lié, laissé tel quel)",
                    "logs/aiodos.log (lien vers le fichier d'un autre compte)"]


def test_start_makes_files_private_before_writing_the_log(monkeypatch):
    order = []
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(paths, "keep_private", lambda: order.append("droits"))
    monkeypatch.setattr(main, "_BoundedLog", lambda *a: order.append("journal")
                        or logging.NullHandler())
    monkeypatch.setattr(main.logging, "basicConfig", lambda **kw: None)
    main._setup_logging()
    assert order == ["droits", "journal"]


def test_nothing_changes_on_windows(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(paths, "_posix", lambda: False)
    monkeypatch.setattr(paths.os, "chmod", lambda *a: calls.append(a))
    monkeypatch.setattr(paths.os, "umask", lambda *a: calls.append(a))
    monkeypatch.setenv("AIODOS_DATA_DIR", str(tmp_path / "donnees"))
    assert paths.keep_private() == []
    paths.data_dir()
    assert calls == []


def _dacl_protected(path: Path) -> bool:
    """L'ACL du dossier est-elle protégée (sans héritage du parent) ?"""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.windll.advapi32
    dacl = 4                                       # DACL_SECURITY_INFORMATION
    need = wintypes.DWORD()
    advapi.GetFileSecurityW(str(path), dacl, None, 0, ctypes.byref(need))
    buffer = ctypes.create_string_buffer(need.value)
    assert advapi.GetFileSecurityW(str(path), dacl, buffer, need, ctypes.byref(need))
    control, revision = wintypes.WORD(), wintypes.DWORD()
    assert advapi.GetSecurityDescriptorControl(buffer, ctypes.byref(control),
                                               ctypes.byref(revision))
    return bool(control.value & 0x1000)            # SE_DACL_PROTECTED


@pytest.mark.skipif(os.name != "nt", reason="ACL Windows")
def test_on_windows_new_data_folders_keep_the_inherited_rights(tmp_path, monkeypatch):
    """Sous Windows (Python 3.13 et plus), mkdir(mode=0o700) poserait une ACL protégée,
    réservée au propriétaire : créée par une session élevée, la session normale ne pourrait
    plus ouvrir ses données. Les dossiers héritent donc des droits de leur parent."""
    folder = tmp_path / "donnees"
    monkeypatch.setenv("AIODOS_DATA_DIR", str(folder))
    paths.keep_private()
    paths.data_dir()
    paths.log_path()
    paths.work_dir()
    for made in (folder, folder / "logs", folder / "tmp" / "work"):
        assert not _dacl_protected(made), made


# --- erreurs.log borné au niveau des octets -----------------------------------------------

RAW = r'''
import sys
from aiodos import __main__ as main
main.ERRORS_BYTES = %d
sys.stderr = None                                    # pythonw : pas de console
main._setup_logging()
for i in range(8000):                                # une bibliothèque qui écrit en octets
    sys.stderr.buffer.write(b"trace %%05d " %% i + b"x" * 40 + b"\n")
sys.stderr.buffer.flush()
''' % LIMIT


def test_bytes_written_past_the_text_layer_are_bounded_too(tmp_path):
    """sys.stderr.buffer.write passe sous la couche texte : la limite tient quand même."""
    done = _run(RAW, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    logs = tmp_path / "logs"
    current = (logs / "erreurs.log").read_bytes()
    backup = (logs / "erreurs.log.1").read_bytes()
    assert len(current) <= LIMIT and len(backup) <= LIMIT
    assert current.endswith(b"trace 07999 " + b"x" * 40 + b"\n")
    assert b"\ntrace " in backup and b"\0" not in backup + current
    # (des octets écrits par blocs : une ligne peut être coupée entre les deux fichiers)


def test_a_single_write_larger_than_the_limit_keeps_its_end(tmp_path):
    path = tmp_path / "erreurs.log"
    with main._BoundedFile(path, 1000) as raw:
        assert raw.write(b"debut\n") == 6
        assert raw.write(b"a" * 5000 + b"fin\n") == 5004
    assert path.stat().st_size <= 1000
    assert path.read_bytes().endswith(b"a" * 10 + b"fin\n")
    assert (tmp_path / "erreurs.log.1").read_bytes() == b"debut\n"


def test_an_oversized_errors_log_is_reduced_when_opened(tmp_path):
    path = tmp_path / "erreurs.log"
    path.write_bytes(b"".join(b"ancienne %06d\n" % i for i in range(100000)))
    main._BoundedFile(path, 1000).close()
    assert path.stat().st_size == 0
    backup = (tmp_path / "erreurs.log.1").read_bytes()
    assert len(backup) <= 1000 and backup.startswith(b"ancienne ")
    assert backup.endswith(b"ancienne 099999\n")


def test_threads_reducing_at_the_same_time_keep_a_full_backup(tmp_path):
    """Deux fils qui atteignent la limite ensemble : sans verrou, le second écraserait
    erreurs.log.1 par une copie presque vide du fichier que le premier vient de vider."""
    sizes = []

    class Watched(main._BoundedFile):
        def _rotate(self, size):
            super()._rotate(size)
            sizes.append(self._backup.stat().st_size)

    line = b"x" * 99 + b"\n"
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        with Watched(tmp_path / "erreurs.log", 4000) as raw:
            def work():
                for _ in range(3000):
                    raw.write(line)
            threads = [threading.Thread(target=work) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
    finally:
        sys.setswitchinterval(previous)
    assert len(sizes) > 100
    assert min(sizes) > 4000 - len(line)
    assert (tmp_path / "erreurs.log").stat().st_size <= 4000


@posix_only
def test_an_errors_log_deleted_while_open_stays_bounded(tmp_path):
    path = tmp_path / "erreurs.log"
    with main._BoundedFile(path, 1000) as raw:
        path.unlink()                              # effacé à la main, encore ouvert
        for i in range(1000):
            raw.write(b"ligne %04d\n" % i)
        assert os.fstat(raw.fileno()).st_size <= 1000


@posix_only
def test_on_unix_the_database_and_logs_are_readable_by_the_user_only(tmp_path, monkeypatch):
    folder = tmp_path / "donnees"
    folder.mkdir()
    os.chmod(folder, 0o755)
    left = folder / "instance.json"
    left.write_text("{}", encoding="utf-8")
    os.chmod(left, 0o644)
    monkeypatch.setenv("AIODOS_DATA_DIR", str(folder))
    monkeypatch.setenv(paths.USER_UMASK_ENV, "")         # keep_private le pose : rendu ensuite
    monkeypatch.setattr(paths, "_user_umask", None)
    previous = os.umask(0o022)
    try:
        paths.keep_private()
        conn = sqlite3.connect(folder / "aiodos.db")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t(x)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            paths.log_path().write_text("journal", encoding="utf-8")
            for path in (folder / "aiodos.db", folder / "aiodos.db-wal",
                         folder / "aiodos.db-shm", left, paths.log_path()):
                assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
        finally:
            conn.close()
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        assert stat.S_IMODE((folder / "logs").stat().st_mode) == 0o700
    finally:
        os.umask(previous)


# --- Relecture des corrections (21/10/2026) -----------------------------------------------

def test_a_chmod_the_disk_ignores_is_reported(unix, tmp_path, monkeypatch, caplog):
    """Un partage CIFS sans extensions Unix, un disque Windows vu par WSL, une carte FAT :
    chmod réussit sans rien changer. Les fichiers restent ouverts, et doivent être signalés
    comme tels, pas comptés comme fermés."""
    folder = tmp_path / "donnees"
    (folder / "logs").mkdir(parents=True)
    for name in ("aiodos.db", "logs/aiodos.log"):
        (folder / name).write_text("secret", encoding="utf-8")
    other = tmp_path / "autre"
    other.mkdir()
    _older(unix, folder)
    _older(unix, other)
    monkeypatch.setattr(paths.os, "chmod", lambda path, mode: None)      # accepté, sans effet
    monkeypatch.setenv("AIODOS_DATA_DIR", str(folder))
    left = paths.keep_private()
    assert left == [f"{folder} (le dossier)", "aiodos.db", "logs", "logs/aiodos.log"]
    assert paths.left_open() == left                  # pour l'interface
    monkeypatch.setenv("AIODOS_DATA_DIR", str(other))
    with caplog.at_level(logging.WARNING, logger="aiodos"):
        paths.data_dir()                              # hors démarrage : dit au journal
    assert any("reste lisible" in r.getMessage() and str(other) in r.getMessage()
               for r in caplog.records)


def test_the_real_rights_are_written_at_start(unix, tmp_path, monkeypatch):
    folder = tmp_path / "donnees"
    folder.mkdir()
    (folder / "aiodos.db").write_text("", encoding="utf-8")
    _older(unix, folder)
    monkeypatch.setenv("AIODOS_DATA_DIR", str(folder))
    assert paths.rights() == "dossier 0755, base 0644"
    paths.keep_private()
    assert paths.rights() == "dossier 0700, base 0600"


def test_nothing_about_rights_is_written_on_windows(monkeypatch):
    monkeypatch.setattr(paths, "_posix", lambda: False)
    assert paths.rights() == ""


def test_what_aiodos_saves_for_the_user_gets_the_users_usual_rights(unix, tmp_path,
                                                                    monkeypatch):
    """L'umask 077 vaut pour tout le processus : « Enregistrer sous… » ou une galerie posée
    dans un dossier choisi reprennent les droits que l'utilisateur aurait eus sans AIODOS.
    Rien ne s'ouvre dans le dossier de données."""
    folder = tmp_path / "donnees"
    monkeypatch.setenv("AIODOS_DATA_DIR", str(folder))
    paths.for_user(tmp_path)                          # avant keep_private : rien
    assert unix["chmod"] == {}
    paths.keep_private()
    saved = tmp_path / "Images" / "chat.png"
    saved.parent.mkdir()
    saved.write_bytes(b"png")
    inside = folder / "images" / "chat.png"
    inside.parent.mkdir()
    inside.write_bytes(b"png")
    unix["chmod"].clear()
    for path in (saved, saved.parent, inside, inside.parent):
        paths.for_user(path)
    assert unix["chmod"] == {saved: 0o644, saved.parent: 0o755}


def test_the_gallery_and_save_as_give_the_users_rights(unix, tmp_path, monkeypatch):
    """paths.for_user existait sans être appelé (relecture finale du 21/10/2026) : la galerie
    posée hors des données (dossier choisi, Images/AIODOS), ses dossiers et ses images, et
    la copie de « Enregistrer sous… » restaient en 0700 et 0600."""
    from aiodos import settings
    from aiodos.imaging import gallery
    from aiodos.ui import window
    folder = tmp_path / "donnees"
    monkeypatch.setenv("AIODOS_DATA_DIR", str(folder))
    paths.keep_private()
    chosen = tmp_path / "Partage" / "AIODOS"
    settings.set_value("images_out_dir", str(chosen))
    unix["chmod"].clear()
    assert gallery.out_dir() == chosen
    assert unix["chmod"] == {chosen.parent: 0o755, chosen: 0o755}
    source = chosen / "chat.png"
    source.write_bytes(b"png")
    target = tmp_path / "Bureau" / "chat.png"
    target.parent.mkdir()
    dialog = type("Window", (), {"create_file_dialog": lambda self, *a, **k: str(target)})()
    monkeypatch.setitem(window._state, "window", dialog)
    monkeypatch.setattr(window, "_from_interface", lambda: True)
    monkeypatch.setattr(window, "_creation", lambda image_id: (
        type("Gallery", (), {"path_of": staticmethod(lambda row: source)}), {}))
    unix["chmod"].clear()
    assert window.save_image(1) == str(target)
    assert unix["chmod"] == {target: 0o644}


def test_what_stays_open_to_others_is_shown_in_the_interface(monkeypatch):
    """L'avertissement du journal ne se voit ni depuis la fenêtre ni depuis l'appli : l'état
    du nœud le donne à l'interface (vue « Cette machine »), jamais aux autres machines."""
    from fastapi.testclient import TestClient

    from aiodos.api.server import create_app
    from aiodos.node import identity
    monkeypatch.setattr(paths, "_left_open", ["aiodos.db (fichier d'un autre compte)"])
    monkeypatch.setattr(identity, "profile", lambda: {"name": "PC"})
    with TestClient(create_app("jeton", {"testserver"})) as client:
        node = client.get("/api/node", headers={"X-Aiodos-Token": "jeton"}).json()
    assert node["left_open"] == ["aiodos.db (fichier d'un autre compte)"]
    view = (APP_ROOT / "aiodos" / "ui" / "static" / "js" / "views" / "node.js").read_text(
        encoding="utf-8")
    assert "${leftOpen(n.left_open)}" in view and "esc(x)" in view


def test_a_relaunched_version_knows_the_users_umask(unix, monkeypatch, tmp_path):
    """Une version relancée par le lanceur des mises à jour hérite de l'umask 077 : le vrai
    lui vient de la précédente par USER_UMASK_ENV."""
    monkeypatch.setenv("AIODOS_DATA_DIR", str(tmp_path / "donnees"))
    paths.keep_private()                              # première version : umask 022
    assert os.environ[paths.USER_UMASK_ENV] == "022"
    monkeypatch.setattr(paths.os, "umask", lambda mask: paths.PRIVATE_UMASK)
    monkeypatch.setenv(paths.USER_UMASK_ENV, "002")
    paths.keep_private()                              # version relancée
    assert paths._user_umask == 0o002
    monkeypatch.setenv(paths.USER_UMASK_ENV, "n'importe quoi")
    paths.keep_private()
    assert paths._user_umask == paths.PRIVATE_UMASK   # illisible : le plus prudent


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_log_that_cannot_be_rotated_keeps_writing_and_stays_bounded(tmp_path, monkeypatch):
    """aiodos.log impossible à réduire (lecture seule, ouvert ailleurs sans partage, projeté
    en mémoire) : le journal n'est pas muet (pas de « Logging error » à chaque ligne), ne
    retente qu'une fois par minute, et se tait au double de sa limite."""
    path = tmp_path / "aiodos.log"
    refused = [True]
    real_open = open

    def fake_open(file, mode="r", *args, **kwargs):
        if mode == "r+b" and refused[0]:
            raise PermissionError(13, "accès refusé", str(file))
        return real_open(file, mode, *args, **kwargs)
    monkeypatch.setattr(main, "open", fake_open, raising=False)
    handler = main._BoundedLog(path, 2000, 3)
    handler.clock = clock = _Clock()
    errors = []
    handler.handleError = lambda record: errors.append(record)
    logger = logging.getLogger("essai.rotation")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for i in range(200):
            logger.warning("ligne %03d %s", i, "x" * 50)
        assert errors == []
        text = path.read_text(encoding="utf-8")
        assert "Rotation du journal impossible" in text and text.count("impossible") == 1
        assert "ligne 030" in text                    # écrit après l'échec, jusqu'au double
        assert 2000 < path.stat().st_size <= 2 * 2000 + 100
        clock.now += 30
        logger.warning("toujours en attente")         # moins d'une minute : pas d'essai
        assert path.stat().st_size <= 2 * 2000 + 100
        refused[0] = False
        clock.now += 31
        logger.warning("après la minute")
        assert handler.stuck is None
        assert path.read_text(encoding="utf-8").endswith("après la minute\n")
        assert (tmp_path / "aiodos.log.1").stat().st_size <= 2000
    finally:
        logger.removeHandler(handler)
        handler.close()


DESCRIPTOR_2 = r'''
import os, sys
from aiodos import __main__ as main
main.ERRORS_BYTES = %d
main._setup_logging()
for i in range(3000):
    print(f"python {i} " + "p" * 80, file=sys.stderr)
    os.write(2, (f"natif {i} " + "n" * 80 + "\n").encode())    # code C, faulthandler…
print("fin", file=sys.stderr)
sys.stderr.flush()
os.write(2, b"fin native\n")
''' % LIMIT


def test_a_relaunched_version_writing_on_descriptor_2_leaves_no_null_bytes(tmp_path):
    """Sous Windows, le descripteur 2 hérité par une version relancée n'est pas en mode
    ajout : après chaque réduction d'erreurs.log, ce qui y écrivait directement le faisait
    repartir de son ancienne position. Il ne restait que la dernière ligne (erreurs.log.1
    vide), après un trou d'octets nuls si elle venait du descripteur 2."""
    logs = tmp_path / "logs"
    logs.mkdir()
    with open(logs / "erreurs.log", "ab") as inherited:
        inherited.write(b"ligne de la version d'avant\n")
        inherited.flush()
        done = _run(DESCRIPTOR_2, tmp_path, stdout=subprocess.PIPE, stderr=inherited)
    assert done.returncode == 0, done.stdout
    current = (logs / "erreurs.log").read_bytes()
    backup = (logs / "erreurs.log.1").read_bytes()
    assert b"\0" not in current + backup
    assert len(current) <= LIMIT + MARGIN and len(backup) <= LIMIT + MARGIN
    assert current.endswith(b"fin native\n")
    assert b"\nfin\r\n" in current or b"\nfin\n" in current        # la dernière de Python
    assert b"natif 2999 " in current and b"python 2999 " in current
    assert backup.count(b"natif ") > 100                     # les lignes d'avant aussi
