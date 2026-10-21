"""Moteurs locaux (audit de sécurité du 20/10/2026).

- sd-server n'a pas de clé et répond à toute page web (CORS) : il n'écoute que sur
  127.0.0.1, sur un port que les navigateurs refusent (sinon il n'est pas lancé), et
  seulement s'il y est seul, au lancement puis à chaque demande ; serving() le revoit
  pour qui suit un calcul (imaging/engine.py) ;
- un fichier de modèle d'images n'est relié (lien physique) qu'à un fichier que personne
  d'autre ne peut modifier, droits relus sur le lien fait ; avant chaque lancement de
  sd-server, un fichier que d'autres peuvent modifier (ancien lien, même privé de son
  autre nom, même en lecture seule) devient une copie vérifiée, jamais plus longue que
  prévu, et sans copie possible sd-server n'est pas lancé ; hors Windows, une ACL interdit
  le lien (imaging/local_copies.py) ;
- le nom d'un modèle « reparti: » ne devient jamais un chemin hors du dossier d'Ollama,
  ni un nom réservé de Windows, même sans os.path.isreserved (cluster/weights.py) ;
- le nom d'un modèle Whisper, qui vient aussi de la machine qui confie la transcription
  (un ami), n'est pris que parmi ceux de faster-whisper, et seul celui du réglage se
  télécharge, sauf dans le contexte media.downloads(True), à ouvrir par la machine qui
  calcule pour une de mes machines (services/media.py, services/whisper_child.py ;
  network/worker.py l'ouvre pour une de mes machines, jamais pour un ami) ;
- les programmes de llama.cpp et sd-server écrivent dans un journal borné, et seuls les
  derniers journaux des programmes d'avant restent ; la clé de llama-server passe par son
  environnement, jamais par sa ligne de commande, et un llama-server qui ne l'exigerait pas
  est arrêté (cluster/engine.py, llm/engine.py, logfiles.py).

Les liens physiques sont essayés deux fois : avec une doublure des droits, qui suit le
fichier et non son nom (numéro de fichier), et sous Windows avec de vrais droits (icacls :
« Utilisateurs authentifiés : modification » sur un fichier d'essai)."""
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from aiodos import settings
from aiodos.cluster import weights
from aiodos.cluster.engine import EngineError
from aiodos.imaging import catalog, download, engine, local_copies
from aiodos.jobs.registry import Interrupted
from aiodos.services import media, whisper_child
from aiodos.updates import private
from tests.test_images import ready, tiny_model  # noqa: F401  (fixtures)
from tests.test_images_local import _put, comfy  # noqa: F401
from tests.test_whisper_child import whisper  # noqa: F401

APP_ROOT = Path(__file__).resolve().parent.parent
OTHERS = ["Utilisateurs authentifiés (S-1-5-11)"]
who_listens = pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")),
                                 reason="ce système ne dit pas qui écoute sur un port")
windows = pytest.mark.skipif(sys.platform != "win32", reason="droits et noms de Windows")


# ------------------------------------------------------------------ sd-server
@pytest.fixture
def launches(tiny_model, monkeypatch):
    """sd-server remplacé par une doublure ; chaque lancement est noté : ses arguments,
    et le nombre de noms de chaque fichier du modèle à ce moment-là."""
    engine.stop()
    ready()
    monkeypatch.setattr(engine, "info", lambda: {"folder": str(tiny_model["folder"]),
                                                 "version": "f0aac52"})
    monkeypatch.setattr(engine, "devices", lambda max_age=0: [])
    monkeypatch.setattr(engine.ollama, "unload_chat_models", lambda: [])
    seen = []

    class Server:
        def __init__(self, name, program, args, port, env=None):
            self.args, self.port, self.process = args, port, None

        def start(self, timeout, ready=None, stop=None, waiting=None, **extra):
            seen.append({"args": self.args, "links": {
                name: os.stat(tiny_model["folder"] / name).st_nlink
                for name in tiny_model["contents"]}})
            return self

        @property
        def alive(self):
            return True

        def stop(self):
            pass
    monkeypatch.setattr(engine, "Process", Server)
    yield seen
    engine.stop()


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_sd_server_listens_on_a_local_port_that_browsers_refuse(launches):
    """Une page web (même ouverte sur ce PC) ne peut pas joindre ces ports : Chrome, Edge,
    Firefox et Safari les refusent (« bad ports » de la norme Fetch)."""
    url = engine.acquire("essai")
    args = launches[0]["args"]
    port = int(args[args.index("--listen-port") + 1])
    assert args[args.index("--listen-ip") + 1] == "127.0.0.1"
    assert port in engine.BROWSER_BLOCKED_PORTS and url == f"http://127.0.0.1:{port}"
    assert all(p > 1024 for p in engine.BROWSER_BLOCKED_PORTS)        # sans droits particuliers


def test_no_port_that_a_browser_policy_can_reopen():
    """ExplicitlyAllowedNetworkPorts (stratégie d'entreprise de Chrome et d'Edge) peut
    rouvrir 6566 et 10080 : sur un poste ainsi géré, une page web joindrait sd-server."""
    assert not {6566, 10080} & set(engine.BROWSER_BLOCKED_PORTS)
    fetch_bad_ports_above_1024 = {1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000,
                                  6566, 6665, 6666, 6667, 6668, 6669, 6679, 6697, 10080}
    assert set(engine.BROWSER_BLOCKED_PORTS) <= fetch_bad_ports_above_1024
    assert len(set(engine.BROWSER_BLOCKED_PORTS)) == 15


def test_a_program_that_joins_sd_server_on_its_port_is_caught_at_the_next_image(
        launches, monkeypatch, caplog):
    """sd-server déjà chargé : avant de lui confier une nouvelle image (une retouche envoie
    son image de départ), AIODOS revoit qui écoute sur son port. Un autre programme qui s'y
    est ajouté, ou sd-server qui n'y écoute plus : arrêté, et relancé sur un autre port."""
    listening, started, stopped = {1000}, [], []
    monkeypatch.setattr(engine, "_listeners", lambda port: set(listening))
    monkeypatch.setattr(engine, "_sockets_of", lambda pid: {pid})
    monkeypatch.setattr(engine, "listen_port", lambda: 6665 + len(started))

    class Server:
        alive = True

        def __init__(self, name, program, args, port, env=None):
            self.port = port
            self.process = types.SimpleNamespace(pid=1000 + len(started))

        def start(self, timeout, ready=None, stop=None, waiting=None, **extra):
            started.append(self)
            return self

        def stop(self):
            stopped.append(self)
    monkeypatch.setattr(engine, "Process", Server)
    first = engine.acquire("essai")
    engine.release()
    assert engine.acquire("essai") == first and len(started) == 1       # seul : gardé
    engine.release()
    listening.add(4242)                                                 # quelqu'un s'ajoute
    with caplog.at_level("WARNING", logger="aiodos.images"):
        second = engine.acquire("essai")
    engine.release()
    assert stopped == [started[0]] and len(started) == 2 and second != first
    assert "un autre programme écoute aussi" in caplog.text
    listening.clear()                                                   # il n'y écoute plus
    third = engine.acquire("essai")
    engine.release()
    assert stopped == started[:2] and len(started) == 3 and third != second


def test_a_busy_port_is_skipped(monkeypatch):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken, free = busy.getsockname()[1], _unused_port()
        monkeypatch.setattr(engine, "BROWSER_BLOCKED_PORTS", (taken, free))
        assert {engine.listen_port() for _ in range(8)} == {free}


def test_without_a_free_safe_port_sd_server_is_not_launched(launches, monkeypatch, caplog):
    """Un programme de la machine peut occuper exprès les 15 ports : se replier sur un port
    quelconque rendrait sd-server joignable par une page web."""
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        monkeypatch.setattr(engine, "BROWSER_BLOCKED_PORTS", (busy.getsockname()[1],))
        with caplog.at_level("WARNING", logger="aiodos.images"), \
                pytest.raises(EngineError, match="Aucun port sûr n'est libre pour sd-server"):
            engine.acquire("essai")
    assert launches == [] and engine.loaded() is None and "navigateurs" in caplog.text


class _Serving:
    """sd-server lancé : `pid` est le programme qui le fait tourner, `alive` dit s'il
    tourne encore."""
    pid, alive = None, True

    def __init__(self, name, program, args, port, env=None):
        self.port = port
        self.process = types.SimpleNamespace(pid=type(self).pid)

    def start(self, timeout, ready=None, stop=None, waiting=None, **extra):
        return self

    def stop(self):
        pass


@who_listens
def test_sd_server_is_checked_again_while_it_computes(launches, monkeypatch):
    """serving(base), pour qui suit un calcul : arrêté en route, ou remplacé sur son
    port, ce n'est plus lui qui répond."""
    with socket.socket() as listening:                      # sd-server : ce programme-ci
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        port = listening.getsockname()[1]
        monkeypatch.setattr(engine, "listen_port", lambda: port)
        monkeypatch.setattr(_Serving, "pid", os.getpid())
        monkeypatch.setattr(engine, "Process", _Serving)
        base = engine.acquire("essai")
        assert engine.serving(base) is True
        assert engine.serving(f"http://127.0.0.1:{port + 1}") is False      # pas lui
        monkeypatch.setattr(_Serving, "alive", False)                       # tombé en route
        assert engine.serving(base) is False
    engine.stop()
    assert engine.serving(base) is False


@who_listens
def test_a_program_that_takes_the_port_of_a_fallen_sd_server_is_not_believed(launches,
                                                                            monkeypatch):
    """sd-server (un autre programme) ne tient plus son port ; un squatteur s'y installe
    et répondrait « terminé » avec sa propre image."""
    fallen = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        port = _unused_port()
        monkeypatch.setattr(engine, "listen_port", lambda: port)
        monkeypatch.setattr(_Serving, "pid", fallen.pid)
        monkeypatch.setattr(engine, "Process", _Serving)
        base = engine.acquire("essai")
        assert engine.serving(base) is False                 # plus personne sur son port
        with socket.socket() as squatter:
            squatter.bind(("127.0.0.1", port))
            squatter.listen()
            assert engine.serving(base) is False
    finally:
        fallen.kill()
        fallen.wait()


@who_listens
def test_sd_server_must_be_alone_on_its_port():
    """Un programme qui aurait pris le port pendant le chargement du modèle recevrait les
    demandes (l'image de départ d'une retouche comprise) et rendrait ses images."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with socket.socket() as squatter:
            squatter.bind(("127.0.0.1", 0))
            port = squatter.getsockname()[1]
            loading = types.SimpleNamespace(process=types.SimpleNamespace(pid=other.pid))
            assert engine._owned(loading, port) is False             # personne : il charge
            squatter.listen()
            with pytest.raises(EngineError, match="Un autre programme écoute"):
                engine._owned(loading, port)
            me = types.SimpleNamespace(process=types.SimpleNamespace(pid=os.getpid()))
            assert engine._owned(me, port) is True                   # c'est bien lui
    finally:
        other.kill()
        other.wait()


@who_listens
def test_a_squatted_port_cancels_the_launch_and_stops_sd_server(launches, monkeypatch):
    stopped = []
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        port = squatter.getsockname()[1]
        monkeypatch.setattr(engine, "listen_port", lambda: port)

        class Loaded:
            def __init__(self, name, program, args, port, env=None):
                self.port = port
                self.process = types.SimpleNamespace(pid=os.getpid() + 1)   # pas nous

            def start(self, timeout, ready=None, stop=None, waiting=None, **extra):
                ready()
                return self

            def stop(self):
                stopped.append(True)
        monkeypatch.setattr(engine, "Process", Loaded)
        with pytest.raises(EngineError, match="Un autre programme écoute"):
            engine.acquire("essai")
    assert stopped and engine.loaded() is None


# ------------------------------------------------------------------ liens physiques
def _identity(path: Path) -> tuple:
    info = os.stat(path)
    return info.st_dev, info.st_ino


def _modifiable_by_others(monkeypatch, *files: Path) -> set:
    """private.foreign_writers : d'autres comptes peuvent modifier ces fichiers-là, sous
    quelque nom qu'on les désigne, même une fois leur nom d'origine effacé (comme les
    droits sous Windows, portés par le fichier et non par son nom) ; personne d'autre que
    moi le reste. Rend l'ensemble des fichiers ouverts, à compléter."""
    foreign = {_identity(f) for f in files}
    monkeypatch.setattr(private, "foreign_writers",
                        lambda p: OTHERS if _identity(Path(p)) in foreign else [])
    return foreign


def _open_to_others(path: Path):
    """Vrais droits de Windows : « Utilisateurs authentifiés : modification » sur le fichier
    lui-même, comme pour un fichier créé dans C:\\models."""
    subprocess.run(["icacls", str(path), "/grant", "*S-1-5-11:(M)"], check=True,
                   capture_output=True)


def _rewritten_by_another_account(vae: Path, shared: Path):
    """Réécrit à taille égale par son autre nom, sa date remise : seules la taille et la
    date sont revues (catalog.file_state), AIODOS le croit toujours vérifié."""
    before = os.stat(vae)
    with open(shared, "r+b") as f:
        f.write(b"#" * before.st_size)
    os.utime(shared, ns=(before.st_atime_ns, before.st_mtime_ns))


def test_a_model_file_others_can_modify_is_copied_not_linked(comfy, tiny_model, monkeypatch):
    for name, content in tiny_model["contents"].items():
        _put(comfy, name, content)
    shared = comfy / "vae.gguf"
    _modifiable_by_others(monkeypatch, shared)
    download.start("essai")
    ready()
    vae, llm = (tiny_model["folder"] / name for name in ("vae.gguf", "llm.gguf"))
    assert os.stat(vae).st_nlink == 1                       # copié : il a ses propres droits
    assert os.stat(llm).st_nlink == 2                       # privé : relié, aucune place en plus
    shared.write_bytes(b"#" * shared.stat().st_size)        # réécrit depuis le dossier d'origine
    assert vae.read_bytes() == tiny_model["contents"]["vae.gguf"]


def test_a_source_swapped_just_before_the_link_is_copied_instead(comfy, tiny_model, monkeypatch):
    """Ses droits relus, la source est privée ; puis un compte qui peut renommer dans son
    dossier y met son propre fichier, au bon contenu, avant le lien. Les droits sont relus
    sur le lien fait : il devient une copie."""
    for name, content in tiny_model["contents"].items():
        _put(comfy, name, content)
    source = comfy / "vae.gguf"
    foreign = _modifiable_by_others(monkeypatch)
    real = local_copies._private

    def checked_then_swapped(path):
        answer = real(path)
        if Path(path) == source and answer:
            data = source.read_bytes()
            source.unlink()
            source.write_bytes(data)                        # le fichier de l'autre compte
            foreign.add(_identity(source))
        return answer
    monkeypatch.setattr(local_copies, "_private", checked_then_swapped)
    download.start("essai")
    ready()
    vae = tiny_model["folder"] / "vae.gguf"
    assert not private.foreign_writers(vae) and os.stat(vae).st_nlink == 1
    assert vae.read_bytes() == tiny_model["contents"]["vae.gguf"]


def _old_link(tiny_model, tmp_path, monkeypatch) -> tuple:
    """Le VAE relié à un fichier d'un dossier commun, comme le faisait une version d'avant."""
    vae = tiny_model["folder"] / "vae.gguf"
    shared = tmp_path / "C-models" / "vae.gguf"
    shared.parent.mkdir()
    os.link(vae, shared)
    _modifiable_by_others(monkeypatch, shared)
    return vae, shared


def test_an_old_link_to_a_file_others_can_modify_becomes_a_verified_copy(launches, tiny_model,
                                                                           tmp_path, monkeypatch):
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)
    copies = []
    real = local_copies._copy
    monkeypatch.setattr(local_copies, "_copy", lambda *a, **k: copies.append(a) or real(*a, **k))
    engine.acquire("essai")
    assert launches[0]["links"]["vae.gguf"] == 1            # copié avant le lancement
    assert not os.path.samefile(vae, shared) and shared.exists()
    assert vae.read_bytes() == tiny_model["contents"]["vae.gguf"]
    assert catalog.file_state(tiny_model["files"]["vae"], check=False) == "ok"   # déjà noté
    assert not vae.with_name("vae.gguf.copie").exists()
    engine.stop()
    engine.acquire("essai")                                 # une fois pour toutes
    assert launches[1]["links"]["vae.gguf"] == 1 and len(copies) == 1


def test_an_old_link_changed_from_the_other_folder_is_not_loaded(launches, tiny_model, tmp_path,
                                                                 monkeypatch):
    """Réécrit à taille égale depuis le dossier d'origine, sa date remise : AIODOS le
    croyait vérifié ; sd-server ne le charge pas, il est noté à refaire, rien n'est effacé."""
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)
    _rewritten_by_another_account(vae, shared)
    assert catalog.file_state(tiny_model["files"]["vae"], check=False) == "ok"   # le piège
    with pytest.raises(catalog.ModelError, match="a changé depuis sa vérification"):
        engine.acquire("essai")
    assert launches == []
    assert catalog.file_state(tiny_model["files"]["vae"], check=False) == "bad"
    assert os.path.samefile(vae, shared) and not vae.with_name("vae.gguf.copie").exists()


def test_an_old_link_whose_other_name_was_deleted_is_still_checked(launches, tiny_model,
                                                                   tmp_path, monkeypatch):
    """L'autre compte réécrit le fichier, remet la date, puis efface son nom à lui : il n'en
    reste qu'un (st_nlink == 1), mais le fichier garde les droits ouverts de là-bas. Les
    droits décident, pas le nombre de noms."""
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)
    _rewritten_by_another_account(vae, shared)
    shared.unlink()
    assert os.stat(vae).st_nlink == 1 and private.foreign_writers(vae) == OTHERS
    assert catalog.file_state(tiny_model["files"]["vae"], check=False) == "ok"   # le piège
    with pytest.raises(catalog.ModelError, match="a changé depuis sa vérification"):
        engine.acquire("essai")
    assert launches == []
    assert catalog.file_state(tiny_model["files"]["vae"], check=False) == "bad"


def test_a_file_others_can_modify_is_copied_even_with_a_single_name(launches, tiny_model,
                                                                    monkeypatch):
    """Un modèle déplacé depuis un dossier commun (ou un ancien lien intact dont l'autre nom
    a disparu, le dossier ComfyUI effacé) : copié une fois, il prend les droits du dossier
    d'AIODOS."""
    vae = tiny_model["folder"] / "vae.gguf"
    _modifiable_by_others(monkeypatch, vae)
    engine.acquire("essai")
    assert not private.foreign_writers(vae) and launches[0]["links"]["vae.gguf"] == 1
    assert vae.read_bytes() == tiny_model["contents"]["vae.gguf"]
    assert catalog.file_state(tiny_model["files"]["vae"], check=False) == "ok"


def test_without_room_for_the_copy_sd_server_is_not_launched(launches, tiny_model, tmp_path,
                                                            monkeypatch):
    """Disque plein (un autre compte peut le remplir) : hacher le fichier encore relié puis
    le lancer laisserait le temps de le réécrire entre les deux."""
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(local_copies, "_copy", full)
    with pytest.raises(catalog.ModelError, match="Libère de la place") as error:
        engine.acquire("essai")
    assert "vae.gguf" in str(error.value) and len(str(error.value)) < 400
    assert launches == [] and os.path.samefile(vae, shared)
    assert not vae.with_name("vae.gguf.copie").exists()
    assert catalog.file_state(tiny_model["files"]["vae"], check=False) == "ok"   # pas « à refaire »


def test_an_old_link_held_open_elsewhere_is_not_launched(launches, tiny_model, tmp_path,
                                                         monkeypatch):
    """Copie faite et vérifiée, mais l'autre compte tient le fichier ouvert : il ne peut pas
    être remplacé. Pas de lancement sur le fichier encore relié."""
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)
    real = os.replace

    def held(src, dst):
        if Path(dst) == vae:
            raise PermissionError(13, "Le processus ne peut pas accéder au fichier")
        return real(src, dst)
    monkeypatch.setattr(local_copies.os, "replace", held)
    with pytest.raises(catalog.ModelError, match="tient ouvert") as error:
        engine.acquire("essai")
    assert launches == [] and not vae.with_name("vae.gguf.copie").exists()
    # Un conseil applicable : le fichier reste « ok », un nouveau téléchargement ne le
    # reprendrait pas
    assert "supprime ce fichier" in str(error.value) and len(str(error.value)) < 400


def test_a_copy_that_fails_for_another_reason_does_not_blame_the_disk(launches, tiny_model,
                                                                      tmp_path, monkeypatch):
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)

    def refused(*args, **kwargs):
        raise PermissionError(13, "Accès refusé")
    monkeypatch.setattr(local_copies, "_copy", refused)
    with pytest.raises(catalog.ModelError, match="la copie a échoué") as error:
        engine.acquire("essai")
    message = str(error.value)
    assert "Libère de la place" not in message and "télécharge-le de nouveau" not in message
    assert "supprime ce fichier" in message and len(message) < 400 and launches == []


@windows
def test_an_old_link_marked_read_only_is_still_replaced(launches, tiny_model, tmp_path,
                                                        monkeypatch):
    """L'attribut lecture seule est porté par le fichier, donc par ses deux noms, et l'autre
    compte peut le poser : os.replace refusait alors de le remplacer, et les images restaient
    bloquées (« Réessaie dans un moment » à chaque fois)."""
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)
    os.chmod(shared, stat.S_IREAD)
    try:
        assert not os.access(vae, os.W_OK)
        engine.acquire("essai")
        assert launches[0]["links"]["vae.gguf"] == 1 and not os.path.samefile(vae, shared)
        assert vae.read_bytes() == tiny_model["contents"]["vae.gguf"]
        assert not vae.with_name("vae.gguf.copie").exists()
    finally:
        for path in (vae, shared):
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


@windows
def test_a_read_only_old_link_also_held_open_is_left_as_it_was(launches, tiny_model, tmp_path,
                                                               monkeypatch):
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)
    os.chmod(shared, stat.S_IREAD)
    real = os.replace

    def held(src, dst):
        if Path(dst) == vae and os.access(vae, os.W_OK):     # lecture seule retirée
            raise PermissionError(13, "Le processus ne peut pas accéder au fichier")
        return real(src, dst)
    monkeypatch.setattr(local_copies.os, "replace", held)
    try:
        with pytest.raises(catalog.ModelError, match="tient ouvert"):
            engine.acquire("essai")
        assert launches == [] and os.path.samefile(vae, shared)
        assert not os.access(shared, os.W_OK)                     # attribut remis
        assert not vae.with_name("vae.gguf.copie").exists()
    finally:
        os.chmod(shared, stat.S_IREAD | stat.S_IWRITE)


def _endless(monkeypatch, path: Path, size: int) -> list:
    """Le fichier `path`, lu par imaging/local_copies.py, ne finit jamais : qui peut encore
    l'écrire l'allonge pendant qu'on le copie. Rend la liste des longueurs lues."""
    real_open, read = open, []

    class Endless:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.file.close()

        def read(self, n):
            data = self.file.read(n) or b"#" * n                     # toujours plus
            read.append(len(data))
            assert sum(read) < size + (64 << 20), "copie sans fin"
            return data

    def opening(name, mode="r", *args, **kwargs):
        file = real_open(name, mode, *args, **kwargs)
        return Endless(file) if Path(name) == path and "r" in mode else file
    monkeypatch.setattr(local_copies, "open", opening, raising=False)
    return read


def test_a_file_that_keeps_growing_is_not_copied_to_the_end(launches, tiny_model, tmp_path,
                                                            monkeypatch):
    """Qui peut encore écrire l'ancien fichier l'allonge sans fin pendant la copie : elle ne
    lit pas plus que la taille attendue (sinon elle remplirait le disque d'AIODOS)."""
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)
    size = tiny_model["files"]["vae"]["size"]
    read = _endless(monkeypatch, vae, size)
    with pytest.raises(catalog.ModelError, match="a changé depuis sa vérification"):
        engine.acquire("essai")
    assert sum(read) == size + 1 and launches == []
    assert not vae.with_name("vae.gguf.copie").exists()
    assert catalog.file_state(tiny_model["files"]["vae"], check=False) == "bad"   # à refaire


def test_a_source_found_elsewhere_that_keeps_growing_is_downloaded(comfy, tiny_model,
                                                                  monkeypatch):
    """De même en reprenant un fichier trouvé sur ce PC (ComfyUI…) : la copie s'arrête à la
    taille attendue, et le fichier est téléchargé."""
    for name, content in tiny_model["contents"].items():
        _put(comfy, name, content)
    source = comfy / "vae.gguf"
    _modifiable_by_others(monkeypatch, source)                      # copié, pas relié
    size = tiny_model["files"]["vae"]["size"]
    read = _endless(monkeypatch, source, size)
    fetched = []
    monkeypatch.setattr(download, "_resume", lambda spec, part: fetched.append(spec["name"])
                        or part.write_bytes(tiny_model["contents"][spec["name"]]))
    download.start("essai")
    ready()
    assert sum(read) == size + 1 and fetched == ["vae.gguf"]
    vae = tiny_model["folder"] / "vae.gguf"
    assert vae.read_bytes() == tiny_model["contents"]["vae.gguf"]


@pytest.mark.parametrize("attributes, linked", [
    (["system.posix_acl_access"], False),       # setfacl : une ACL que les bits ne montrent pas
    (["system.nfs4_acl"], False),
    (["security.selinux", "user.xdg.origin.url"], True),   # pas d'ACL : relié comme avant
    (None, False)])                             # macOS : pas d'os.listxattr, on ne sait pas
def test_outside_windows_a_file_with_an_acl_is_copied_not_linked(comfy, tiny_model, monkeypatch,
                                                                 attributes, linked):
    """private.foreign_writers ne lit que les bits de mode hors Windows : un fichier qui
    porte une ACL « user:autre:rw- » y paraît privé. Le relier laisserait l'autre compte
    modifier le modèle d'AIODOS."""
    for name, content in tiny_model["contents"].items():
        _put(comfy, name, content)
    _modifiable_by_others(monkeypatch)                              # bits de mode : privé
    monkeypatch.setattr(local_copies, "_RIGHTS_READ_WHOLE", False)
    if attributes is None:
        monkeypatch.delattr(os, "listxattr", raising=False)
    else:
        monkeypatch.setattr(os, "listxattr", lambda path: list(attributes), raising=False)
    download.start("essai")
    ready()
    vae = tiny_model["folder"] / "vae.gguf"
    assert (os.stat(vae).st_nlink == 2) is linked
    assert vae.read_bytes() == tiny_model["contents"]["vae.gguf"]


def test_an_old_link_with_an_acl_becomes_a_verified_copy(launches, tiny_model, tmp_path,
                                                         monkeypatch):
    """Hors Windows : un ancien lien vers un fichier qui porte une ACL est copié avant le
    lancement ; la copie, faite dans le dossier d'AIODOS, n'en porte pas."""
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)
    _modifiable_by_others(monkeypatch)                              # bits de mode : privé
    monkeypatch.setattr(local_copies, "_RIGHTS_READ_WHOLE", False)
    with_acl = {_identity(shared)}
    monkeypatch.setattr(os, "listxattr", lambda path: ["system.posix_acl_access"]
                        if _identity(Path(path)) in with_acl else [], raising=False)
    engine.acquire("essai")
    assert launches[0]["links"]["vae.gguf"] == 1 and not os.path.samefile(vae, shared)
    assert vae.read_bytes() == tiny_model["contents"]["vae.gguf"]


def test_a_common_models_folder_is_reported_not_copied_at_each_launch(launches, tiny_model,
                                                                      monkeypatch, caplog):
    """Réglage images_models_dir vers un dossier commun : une copie y prendrait les mêmes
    droits, la refaire à chaque lancement ne servirait à rien ; c'est dit au journal."""
    monkeypatch.setattr(private, "foreign_writers", lambda p: OTHERS)
    monkeypatch.setattr(local_copies, "_common", set())
    copies = []
    monkeypatch.setattr(local_copies, "_copy", lambda *a, **k: copies.append(a) or True)
    with caplog.at_level("WARNING", logger="aiodos.images"):
        engine.acquire("essai")
        engine.stop()
        engine.acquire("essai")
    assert copies == [] and len(launches) == 2
    assert caplog.text.count("Le dossier des modèles d'images") == 1


def test_a_folder_whose_copies_stay_open_is_not_copied_into_again(launches, tiny_model,
                                                                  monkeypatch, caplog):
    """Le dossier est privé, mais ce qu'on y crée ne l'est pas (droits par défaut d'un
    système Unix dont le groupe a d'autres membres) : une seule copie essayée."""
    folder = tiny_model["folder"]
    kept = {_identity(folder / name) for name in ("diffusion.gguf", "llm.gguf")}
    monkeypatch.setattr(private, "foreign_writers",
                        lambda p: [] if Path(p) == folder or _identity(Path(p)) in kept
                        else OTHERS)
    monkeypatch.setattr(local_copies, "_common", set())
    copies = []
    real = local_copies._copy
    monkeypatch.setattr(local_copies, "_copy", lambda *a, **k: copies.append(a) or real(*a, **k))
    with caplog.at_level("WARNING", logger="aiodos.images"):
        engine.acquire("essai")
        engine.stop()
        engine.acquire("essai")
    assert len(copies) == 1 and len(launches) == 2
    assert caplog.text.count("Le dossier des modèles d'images") == 1
    assert not (folder / "vae.gguf.copie").exists()


def test_stopping_while_an_old_link_is_copied_leaves_nothing_behind(launches, tiny_model,
                                                                     tmp_path, monkeypatch):
    vae, shared = _old_link(tiny_model, tmp_path, monkeypatch)
    stop = threading.Event()
    stop.set()
    with pytest.raises(Interrupted):
        engine.acquire("essai", stop=stop)
    assert launches == [] and os.path.samefile(vae, shared)
    assert not vae.with_name("vae.gguf.copie").exists()


# Les mêmes avec de vrais droits de Windows (pas de doublure de foreign_writers)
@windows
def test_real_rights_an_old_link_whose_other_name_was_deleted(launches, tiny_model, tmp_path):
    vae = tiny_model["folder"] / "vae.gguf"
    shared = tmp_path / "C-models" / "vae.gguf"
    shared.parent.mkdir()
    os.link(vae, shared)
    _open_to_others(shared)
    assert private.foreign_writers(vae)
    assert not private.foreign_writers(tiny_model["folder"])     # le dossier d'AIODOS, lui
    _rewritten_by_another_account(vae, shared)
    shared.unlink()
    assert os.stat(vae).st_nlink == 1
    with pytest.raises(catalog.ModelError, match="a changé depuis sa vérification"):
        engine.acquire("essai")
    assert launches == []


@windows
def test_real_rights_an_intact_file_open_to_others_becomes_private(launches, tiny_model):
    vae = tiny_model["folder"] / "vae.gguf"
    _open_to_others(vae)
    assert private.foreign_writers(vae)
    engine.acquire("essai")
    assert not private.foreign_writers(vae) and launches[0]["links"]["vae.gguf"] == 1
    assert vae.read_bytes() == tiny_model["contents"]["vae.gguf"]


@windows
def test_real_rights_a_source_swapped_before_the_link(comfy, tiny_model, monkeypatch):
    for name, content in tiny_model["contents"].items():
        _put(comfy, name, content)
    source = comfy / "vae.gguf"
    real = local_copies._private

    def checked_then_swapped(path):
        answer = real(path)
        if Path(path) == source and answer:
            data = source.read_bytes()
            source.unlink()
            source.write_bytes(data)
            _open_to_others(source)
        return answer
    monkeypatch.setattr(local_copies, "_private", checked_then_swapped)
    download.start("essai")
    ready()
    vae = tiny_model["folder"] / "vae.gguf"
    assert not private.foreign_writers(vae) and os.stat(vae).st_nlink == 1


@windows
def test_real_rights_no_launch_when_the_copy_is_impossible(launches, tiny_model, tmp_path,
                                                          monkeypatch):
    vae = tiny_model["folder"] / "vae.gguf"
    shared = tmp_path / "C-models" / "vae.gguf"
    shared.parent.mkdir()
    os.link(vae, shared)
    _open_to_others(shared)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(local_copies, "_copy", full)
    with pytest.raises(catalog.ModelError, match="Libère de la place"):
        engine.acquire("essai")
    assert launches == []


# ------------------------------------------------------------------ « reparti: »
HOSTILE = [r"..\..\..\..\Users\x\Downloads\piege", "../../secret", r"C:\autre\dossier\x",
           "C:/autre/dossier/x", r"a:\x", r"D:..\..\x", "\\\\192.168.1.50\\partage\\x",
           "//192.168.1.50/partage/x", "\\\\?\\UNC\\192.168.1.50\\s\\x", "x:\\\\h\\s\\m",
           "C:5/espace/modele", "con", "nul:latest", "COM1", "modele:..\\..\\x", "modele:a/b",
           "modele:flux:x", "a/b/c/d", "https://hf.co/a/b", ".cache/x", "", "  "]


@pytest.mark.parametrize("name", HOSTILE)
def test_a_distributed_model_name_never_leaves_ollamas_folder(name, tmp_path):
    with pytest.raises(weights.WeightsError, match="illisible"):
        weights.manifest_path(name, tmp_path)


@pytest.mark.parametrize("name, parts", [
    ("qwen3.6", ("registry.ollama.ai", "library", "qwen3.6", "latest")),
    ("qwen3.5:9b", ("registry.ollama.ai", "library", "qwen3.5", "9b")),
    ("user/modele:Q4_K_M", ("registry.ollama.ai", "user", "modele", "Q4_K_M")),
    ("hf.co/bartowski/Qwen-GGUF:Q4_K_M", ("hf.co", "bartowski", "Qwen-GGUF", "Q4_K_M")),
    ("registry.ollama.ai/library/qwen3:latest",
     ("registry.ollama.ai", "library", "qwen3", "latest"))])
def test_ollama_names_still_lead_to_their_manifest(name, parts, tmp_path):
    path = weights.manifest_path(name, tmp_path)
    assert path.parts[-4:] == parts and path.is_relative_to(tmp_path / "manifests")


@windows
@pytest.mark.parametrize("name", ["con", "prn/modele", "aux.txt", "nul:latest", "COM1",
                                  "lpt9.gguf", "espace/modele.", "x/y/z:tag.",
                                  "localhost:11434/espace/modele"])
def test_windows_reserved_names_are_refused_even_without_isreserved(name, tmp_path,
                                                                     monkeypatch):
    """os.path.isreserved n'existe qu'à partir de Python 3.13 (postes de développement) :
    « con » serait ouvert comme la console, et la lecture du manifeste attendrait sans
    fin ; « nom: » serait un flux NTFS, « nom. » le dossier « nom »."""
    monkeypatch.delattr(os.path, "isreserved", raising=False)
    with pytest.raises(weights.WeightsError, match="illisible"):
        weights.manifest_path(name, tmp_path)
    assert weights.manifest_path("qwen3.6", tmp_path).name == "latest"   # les noms ordinaires


def test_a_manifest_cannot_send_llama_cpp_to_a_file_elsewhere(tmp_path):
    root = tmp_path / "ollama"
    (root / "blobs").mkdir(parents=True)
    (root / "ailleurs.gguf").write_bytes(b"GGUF")            # un fichier hors des blobs
    manifest = root / "manifests" / "registry.ollama.ai" / "library" / "piege" / "latest"
    manifest.parent.mkdir(parents=True)
    layers = [[{"mediaType": weights.MODEL_LAYER, "digest": digest}]
              for digest in ("sha256:\\..\\..\\ailleurs.gguf", "sha256:/../../ailleurs.gguf",
                             "sha256:" + "A" * 64, "sha256:" + "a" * 63, 5, None)]
    odd = [{"layers": [5]}, {"layers": "x"}, [1]]           # manifestes mal formés
    for content in [{"layers": entry} for entry in layers] + odd:
        manifest.write_text(json.dumps(content), encoding="utf-8")
        with pytest.raises(weights.WeightsError):
            weights.gguf_path("piege", root)


# ------------------------------------------------------------------ Whisper
def test_a_name_that_is_not_a_whisper_model_never_reaches_faster_whisper(whisper):
    """Pris tel quel, un chemin serait chargé comme dossier de modèle (et sa présence
    révélée par le message d'erreur), « compte/dépôt » téléchargé de Hugging Face."""
    for name in ("attaquant/depot-geant", "C:/Windows", "C:\\Windows", "..\\..\\Users", "./small",
                 "small/", "small ", "", None, 3, ["small"]):
        with pytest.raises(media.TranscriptionError, match="Modèle Whisper inconnu"):
            media.transcribe(Path("note.wav"), name)
    assert whisper() == [] and media._child.process is None


def test_the_transcription_program_checks_the_name_itself(monkeypatch):
    calls = []

    def fetch(name, local_files_only=False):
        calls.append(("fetch", name, local_files_only))
        return f"cache/{name}"

    def load(*args, **kwargs):
        calls.append(("load", args))
        raise AssertionError("modèle chargé")
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(
        available_models=lambda: ["small", "medium"], download_model=fetch, WhisperModel=load))
    names = ("attaquant/depot-geant", "C:/Windows", "..", "small/../..", ["small"], None)
    lines = iter([json.dumps({"id": i, "path": "a.wav", "model": name, "threads": 0,
                              "download": True}) + "\n" for i, name in enumerate(names)] + [""])
    replies = []
    whisper_child.serve(lambda: next(lines), replies.append)
    assert [r["id"] for r in replies] == list(range(len(names)))
    assert all("error" in r and not r.get("missing") for r in replies)
    assert calls == []


def test_a_model_asked_by_another_machine_is_not_downloaded_here(whisper, monkeypatch):
    monkeypatch.setenv("FAKE_WHISPER_CACHED", "small")
    settings.set_value("whisper_model", "small")
    with pytest.raises(media.TranscriptionError, match="absent de cette machine") as error:
        media.transcribe(Path("note.wav"), "large-v3")
    assert "Modèle Whisper" in str(error.value) and len(str(error.value)) < 400   # lisible là-bas
    assert {"fetch": "large-v3", "local_only": True} in whisper()
    assert not [e for e in whisper() if "load" in e]
    assert media.transcribe(Path("note.wav"), "small").endswith("trois")         # il sert encore


def test_the_model_of_my_setting_is_downloaded_and_one_already_here_is_used(whisper, monkeypatch):
    monkeypatch.setenv("FAKE_WHISPER_CACHED", "small")
    settings.set_value("whisper_model", "medium")
    assert media.transcribe(Path("note.wav"), "medium").endswith("trois")
    assert media.transcribe(Path("note.wav"), "small").endswith("trois")         # déjà là
    assert [e for e in whisper() if "fetch" in e] == [
        {"fetch": "medium", "local_only": False}, {"fetch": "small", "local_only": True}]
    assert [e["load"] for e in whisper() if "load" in e] == ["cache/medium", "cache/small"]


def test_a_missing_model_is_said_apart_so_the_unit_can_be_handed_back(whisper, monkeypatch):
    """Pas un échec du calcul : dit à part, pour que la machine qui calcule pour une autre
    puisse rendre l'unité au lieu de la faire compter en échec (fichiers de ma bibliothèque
    en échec, ami qui met cette machine de côté)."""
    monkeypatch.setenv("FAKE_WHISPER_CACHED", "aucun")   # vide : retiré sous Windows
    settings.set_value("whisper_model", "small")
    with pytest.raises(media.WhisperMissing) as error:
        media.transcribe(Path("note.wav"), "medium")
    assert isinstance(error.value, media.TranscriptionError)
    with pytest.raises(media.TranscriptionError) as other:              # un vrai échec
        media.transcribe(Path("illisible.wav"), "small")
    assert not isinstance(other.value, media.WhisperMissing)


def test_for_my_machines_any_known_model_is_downloaded(whisper, monkeypatch):
    """La machine qui calcule le décide (media.downloads) d'après le niveau de confiance de
    celle qui confie l'unité (une de mes machines), jamais d'après l'ordre reçu."""
    monkeypatch.setenv("FAKE_WHISPER_CACHED", "aucun")   # vide : retiré sous Windows
    settings.set_value("whisper_model", "small")
    with media.downloads(True):
        assert media.transcribe(Path("note.wav"), "large-v3").endswith("trois")
    with media.downloads(False), pytest.raises(media.WhisperMissing):
        media.transcribe(Path("note.wav"), "small")                    # même pas le mien
    with pytest.raises(media.WhisperMissing):
        media.transcribe(Path("note.wav"), "medium")                   # hors contexte : la règle
    assert [e for e in whisper() if "fetch" in e] == [
        {"fetch": "large-v3", "local_only": False}, {"fetch": "small", "local_only": True},
        {"fetch": "medium", "local_only": True}]


def test_a_machine_that_does_not_transcribe_downloads_nothing(whisper, monkeypatch):
    """Transcription coupée ici : un ami qui confie une unité audio ne lui fait pas
    télécharger son modèle Whisper (« small », environ 500 Mo)."""
    monkeypatch.setenv("FAKE_WHISPER_CACHED", "aucun")   # vide : retiré sous Windows
    settings.set_value("whisper_model", "small")
    settings.set_value("media_transcribe", False)
    with pytest.raises(media.WhisperMissing):
        media.transcribe(Path("note.wav"), "small")
    assert {"fetch": "small", "local_only": True} in whisper()


def test_a_bad_request_does_not_unload_the_model_in_memory(monkeypatch):
    """Un nom inconnu, ou un modèle absent (qu'un ami peut demander), ne vide pas le modèle
    chargé : la demande suivante n'a pas à le recharger (plusieurs secondes)."""
    loads = []

    def fetch(name, local_files_only=False):
        if local_files_only and name != "small":
            raise FileNotFoundError("pas dans le cache")
        return f"cache/{name}"

    class Model:
        def __init__(self, where, **kwargs):
            loads.append(where)

        def transcribe(self, path, vad_filter):
            return iter(()), types.SimpleNamespace(language="fr")
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(
        available_models=lambda: ["small", "medium"], download_model=fetch, WhisperModel=Model))
    asked = [("small", False), ("attaquant/depot", True), ("medium", False), ("small", False)]
    lines = iter([json.dumps({"id": i, "path": "a.wav", "model": name, "threads": 0,
                              "download": download}) + "\n"
                  for i, (name, download) in enumerate(asked)] + [""])
    replies = []
    whisper_child.serve(lambda: next(lines), replies.append)
    assert loads == ["cache/small"]
    assert [r.get("missing") for r in replies if "error" in r] == [None, True]


def test_the_accepted_names_are_those_of_faster_whisper_and_of_the_settings():
    view = (APP_ROOT / "aiodos" / "ui" / "static" / "js" / "views" / "settings.js").read_text(
        encoding="utf-8")
    block = re.search(r"key: 'whisper_model'.*?\]\] \}", view, re.S).group(0)
    offered = re.findall(r"\['([^']+)', '", block)
    assert offered and set(offered) <= set(media.WHISPER_MODELS)
    assert settings.DEFAULTS["whisper_model"] in media.WHISPER_MODELS
    done = subprocess.run([sys.executable, "-c", "import json, faster_whisper; "
                           "print(json.dumps(faster_whisper.available_models()))"],
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        pytest.skip("faster-whisper absent")
    assert set(media.WHISPER_MODELS) <= set(json.loads(done.stdout.strip().splitlines()[-1]))


# ------------------------------------------- journaux et clé des moteurs (11 et 20)
# Relecture finale du 21/10/2026 : les journaux des programmes de llama.cpp et de sd-server
# grandissaient sans limite (un fichier ouvert en écriture, laissé au programme), et la clé de
# llama-server passait sur sa ligne de commande, que tout compte de la machine lit.
FAKE_LLAMA = """import http.server, os, sys
KEY, STRICT = os.environ.get("LLAMA_API_KEY"), sys.argv[2] == "exige"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        allowed = (self.path == "/health" or not STRICT
                   or self.headers.get("Authorization") == f"Bearer {KEY}")
        self.send_response(200 if allowed else 401)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""
CHATTY = """import socket, sys, time
listening = socket.socket()
listening.bind(("127.0.0.1", int(sys.argv[1])))
listening.listen()
for n in range(int(sys.argv[2])):
    sys.stdout.write(f"ligne {n} " + "x" * 200 + chr(10))
print("fin", flush=True)
time.sleep(60)
"""


def _script(tmp_path, name, text) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_the_log_of_a_chatty_engine_stays_bounded(tmp_path, monkeypatch):
    """Un programme qui écrit sans fin (4 Mo ici) : son journal reste sous sa limite, la
    fin passe dans <nom>.log.1, et rien ne fait attendre le programme."""
    from aiodos.cluster import engine as programs
    monkeypatch.setattr(programs, "ENGINE_LOG_BYTES", 64 * 1024)
    port = programs.free_port()
    chatty = programs.Process("bavard", Path(sys.executable),
                              [_script(tmp_path, "bavard.py", CHATTY), str(port), "20000"], port)
    chatty.start(timeout=30)
    try:
        deadline = time.monotonic() + 20
        while "fin" not in chatty.log_path.read_text(encoding="utf-8", errors="replace"):
            assert time.monotonic() < deadline, "le journal n'a pas tout reçu"
            time.sleep(0.1)
        backup = chatty.log_path.with_name("bavard.log.1")
        assert chatty.log_path.stat().st_size <= 64 * 1024
        assert 0 < backup.stat().st_size <= 64 * 1024
        assert chatty.alive
    finally:
        chatty.stop()


def test_only_a_few_logs_of_former_engines_are_kept(data_dir):
    """Un nom par port (llama-rpc-50123…) : chaque prêt ou groupe en ajoutait un, jamais
    effacé. Les KEEP_LOGS plus récents restent, et celui d'un programme en marche."""
    from aiodos import paths
    from aiodos.cluster import engine as programs
    folder = paths.log_path().parent
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(10):
        for suffix in (".log", ".log.1"):
            path = folder / f"llama-rpc-{5000 + n}{suffix}"
            path.write_text("x", encoding="utf-8")
            os.utime(path, (1000 + n, 1000 + n))
    (folder / "llama-routeur.log").write_text("x", encoding="utf-8")
    (folder / "llama-server-7000.log").write_text("x", encoding="utf-8")
    running = folder / "llama-rpc-5000.log"
    programs._live.add(running)
    try:
        programs._tidy_logs("llama-rpc-6000")
    finally:
        programs._live.discard(running)
    kept = sorted(p.name for p in folder.glob("llama-rpc-*.log"))
    assert kept == ["llama-rpc-5000.log"] + [f"llama-rpc-{5000 + n}.log" for n in range(6, 10)]
    assert (folder / "llama-rpc-5009.log.1").exists() and not (folder / "llama-rpc-5001.log.1").exists()
    assert (folder / "llama-routeur.log").exists() and (folder / "llama-server-7000.log").exists()


def test_the_key_of_llama_server_is_never_on_its_command_line(tmp_path, monkeypatch):
    """Serveur d'un modèle réparti et routeur du moteur intégré : la clé passe par
    l'environnement (LLAMA_API_KEY), jamais par les arguments."""
    from aiodos.cluster import engine as programs
    from aiodos.llm import engine as integrated
    server = programs.llama_server(tmp_path, tmp_path / "m.gguf", [5001], "RPC0/Vulkan0", "1/1",
                                   "cle-secrete", 4096, 99)
    assert "cle-secrete" not in " ".join(server.args) and "--api-key" not in server.args
    assert server.env == {programs.KEY_VARIABLE: "cle-secrete"}
    launched = []

    class Launched:
        def __init__(self, name, program, args, port, env=None):
            self.args, self.env, self.port, self.alive = args, env, port, False
            launched.append(self)

        def start(self, timeout, ready=None):
            self.alive = True
            return self

        def stop(self):
            self.alive = False
    monkeypatch.setattr(integrated.programs, "folder", lambda: tmp_path)
    monkeypatch.setattr(integrated.programs, "Process", Launched)
    monkeypatch.setattr(integrated.registry, "available", lambda: [])
    router = integrated.Router()
    router.url()
    assert launched[0].env == {programs.KEY_VARIABLE: router.key}
    assert router.key not in " ".join(launched[0].args) and "--api-key" not in launched[0].args


@pytest.mark.parametrize("strict", [True, False])
def test_a_llama_server_that_does_not_require_its_key_is_stopped(tmp_path, strict):
    """Un llama.cpp qui ne lirait pas LLAMA_API_KEY tournerait ouvert à tout programme de
    la machine : refusé avec une raison claire, plutôt que lancé sans clé en silence."""
    from aiodos.cluster import engine as programs
    port = programs.free_port()
    fake = programs.Process("faux-llama", Path(sys.executable),
                            [_script(tmp_path, "faux_llama.py", FAKE_LLAMA), str(port),
                             "exige" if strict else "ouvert"], port,
                            env={programs.KEY_VARIABLE: "cle-du-lancement"})
    try:
        if strict:
            assert fake.start(timeout=30, ready=lambda: programs.loaded(port)) is fake
            assert fake.alive
        else:
            with pytest.raises(EngineError, match="n'exige pas sa clé"):
                fake.start(timeout=30, ready=lambda: programs.loaded(port))
            assert not fake.alive
    finally:
        fake.stop()
