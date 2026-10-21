"""Sécurité : points de l'audit du 20/10/2026 restés entre les groupes de corrections.

- Une machine n'annonce prêter sa carte graphique (« cluster.lends » de son profil) que si
  elle accepterait le prêt (node/identity.py) : sans un compagnon qui tient le serveur RPC,
  cluster/helper.lend refuse, et mes machines ne doivent pas la choisir pour n'échouer
  qu'au premier message (le téléphone : tests/test_android_app.py).
- L'appli Android embarque requests et urllib3 aux versions des PC (android/requirements.txt),
  celles avec lesquelles la garde des adresses est essayée (services/addresses.py).
- La construction de l'appli Android contrôle le dépôt avant d'en importer quoi que ce soit,
  puis, avant de rien construire, ses dossiers de travail (compagnon déjà compilé compris) et
  ses outils (SDK Android, JDK, Python de Chaquopy) : tools/build_android.py, comme les
  outils de publication.
- La vue Réseau garde les nombres et les listes reçus d'une autre machine avec les gardes de
  dom.js (finite, count) : une seule copie.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from importlib import metadata

import pytest

from aiodos import paths, settings
from aiodos.cluster import helper
from aiodos.network import peers, route
from aiodos.network.client import PeerError
from aiodos.node import identity
from aiodos.updates import private
from tests import i18n_js
from tests.nodes import APP_ROOT

sys.path.insert(0, str(APP_ROOT / "tools"))
import build_android  # noqa: E402

OTHER = "b" * 32
KEY = bytes(32)
# Ce qu'annonce le compagnon actuel (compagnon/src/lib.rs)
FULL = ("tunnel-owner", "service-usage", "service-hold")
ASK = {"version": "11707-abc", "tunnel": "guarded"}       # demande d'une de mes machines


# ============================================= carte annoncée seulement si elle est prêtée
class Companion:
    """Compagnon simulé : ce qu'il annonce savoir faire, et les services qu'on lui ouvre."""

    def __init__(self, features=FULL, running=True):
        self.features = frozenset(features)
        self.running = running
        self.exposed = []

    def supports(self, feature):
        return feature in self.features

    def expose(self, services):
        self.exposed.append(services)

    def service_usage(self):
        return None


class RpcServer:
    """ggml-rpc-server simulé."""
    port = 50999
    alive = False

    def start(self, timeout):
        self.alive = True

    def stop(self):
        self.alive = False


class Lender:
    executor = None

    def name(self):
        return "PC fixe"


@pytest.fixture
def card(monkeypatch, tmp_path):
    """Cette machine a une carte à prêter et le réglage activé ; llama.cpp et son serveur
    RPC sont des doublures, et aucun compagnon n'est attaché tant qu'un essai n'en met pas."""
    monkeypatch.setattr(helper.engine, "info", lambda max_age=0: {
        "folder": str(tmp_path), "version": "11707-abc",
        "devices": [{"name": "Vulkan1", "label": "RTX", "total_mib": 8000, "free_mib": 7000}]})
    monkeypatch.setattr(helper.engine, "rpc_server", lambda where, device, cache: RpcServer())
    monkeypatch.setattr(identity, "_not_lent", {"reason": None}, raising=False)   # journal vierge
    peers.save(OTHER, "PC portable", KEY, "127.0.0.1", 1)
    peers.set_net(OTHER, "c" * 64, {})
    settings.set_value("lend_gpu", True)
    route.detach()
    yield
    helper.stop_all()
    route.detach()


@pytest.mark.parametrize("companion, lends", [
    (None, False),                                         # Internet coupé : aucun compagnon
    (Companion(running=False), False),                     # arrêté
    (Companion(("tunnel-owner", "service-usage")), False),  # ne sait pas tenir un service
    (Companion(("tunnel-owner", "service-hold")), False),   # ne dit pas les flux
    (Companion(), True),
], ids=["sans-compagnon", "arrete", "sans-tenue", "sans-flux", "actuel"])
def test_a_machine_announces_its_card_only_if_it_would_lend_it(card, companion, lends):
    """Mes machines choisissent leurs prêteurs d'après « cluster.lends » (group.lenders).
    Annoncé alors que helper.lend refusait faute d'un compagnon qui tient le serveur RPC,
    l'échec n'arrivait qu'au premier message : l'annonce suit les conditions du prêt."""
    if companion is not None:
        route.attach(companion)
    announced = identity._cluster()
    assert announced["lends"] is lends
    assert announced["version"] == "11707-abc" and announced["device"] == "RTX"
    if lends:
        assert helper.lend(Lender(), OTHER, ASK)["device"] == "Vulkan1"
    else:
        with pytest.raises(PeerError, match="compagnon"):
            helper.lend(Lender(), OTHER, ASK)


def test_why_the_card_is_not_announced_is_logged_once(card, caplog):
    """Le réglage demande le prêt, mais le compagnon ne peut pas tenir le serveur : la raison
    est écrite au journal une fois (et de nouveau si elle change), pas à chaque profil."""
    route.attach(Companion(("tunnel-owner", "service-usage")))
    with caplog.at_level(logging.INFO, logger="aiodos.cluster"):
        for _ in range(3):
            assert identity._cluster()["lends"] is False
        route.attach(Companion())
        assert identity._cluster()["lends"] is True
        route.detach()                                     # Internet coupé ensuite
        identity._cluster()
    notes = [r.getMessage() for r in caplog.records if "pas proposée" in r.getMessage()]
    assert len(notes) == 2, notes
    assert "trop ancien" in notes[0] and "ne tourne pas" in notes[1]


def test_nothing_is_asked_of_llama_cpp_while_lending_is_off(card, monkeypatch):
    """Non-régression : prêt coupé, le profil ne lance pas llama.cpp pour rien."""
    settings.set_value("lend_gpu", False)
    monkeypatch.setattr(helper.engine, "info",
                        lambda max_age=0: pytest.fail("llama.cpp interrogé, prêt coupé"))
    route.attach(Companion())
    assert identity._cluster() == {"lends": False}


# ================================================ requests et urllib3 de l'appli Android
def _pins(path) -> dict:
    """{paquet: version} des lignes « nom==version » d'un fichier de paquets."""
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, _, version = line.partition("==")
            found[re.sub(r"[-_.]+", "-", name.strip().lower())] = version.split()[0]
    return found


def test_the_android_app_ships_the_requests_and_urllib3_of_the_tests():
    """La garde des adresses s'accroche à des fonctions internes d'urllib3 : elle n'est
    essayée qu'avec les versions installées ici (celles de l'installeur). L'appli Android en
    épinglait d'autres, jamais essayées."""
    android = _pins(APP_ROOT / "android" / "requirements.txt")
    installer = _pins(APP_ROOT / "tools" / "installer" / "paquets.txt")
    for package in ("requests", "urllib3"):
        assert android[package] == metadata.version(package) == installer[package], package


# ======================================================= construction de l'appli Android
def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e"))


def _commit_all(repo):
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x")
    return repo


def _runnable_copy(tmp_path):
    """Copie jetable du code et des outils, tout commité, dans un dossier privé."""
    repo = private.private_dir(tmp_path / "depot")
    for name in ("aiodos", "tools"):
        shutil.copytree(APP_ROOT / name, repo / name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    return _commit_all(repo)


def _run_tool(repo, *args):
    """tools/build_android.py de la copie, lancé comme outil ; « --help » : jamais plus loin
    que les imports et la lecture des options, donc jamais de construction."""
    return subprocess.run([sys.executable, str(repo / "tools" / "build_android.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"))


def test_the_android_build_checks_the_repository_before_importing_anything(tmp_path):
    """Un paquet planté et non suivi (tools/android_deps/__init__.py) passe devant
    android_deps.py : sans contrôle préalable, il s'exécutait sous le compte de l'auteur,
    avant que l'appli ne soit construite et signée."""
    repo = _runnable_copy(tmp_path)
    ran = tmp_path / "execute.txt"
    planted = repo / "tools" / "android_deps" / "__init__.py"
    planted.parent.mkdir()
    planted.write_text(f"open({str(ran)!r}, 'w').write('piege')\n", encoding="utf-8")
    done = _run_tool(repo, "--help")
    assert done.returncode != 0, done.stdout
    assert "Application non construite" in done.stderr and "non suivis" in done.stderr
    assert "tools/android_deps/" in done.stderr
    assert not ran.exists()


@pytest.mark.parametrize("tool", ["publish", "build_installer", "build_android"])
def test_the_tools_never_run_a_program_of_the_current_folder(tmp_path, tool):
    """Lancés depuis un autre dossier que le dépôt (le dossier parent, par exemple), un
    git.exe ou un module qui s'y trouverait s'exécutait avec la clé à portée : Windows
    cherche un programme dans le dossier courant avant le PATH, et « python -m » y prend ses
    modules. Une fois le dépôt contrôlé, l'amorce interdit le dossier courant à la recherche
    des programmes, et les « python -m » partent du dépôt (relecture finale du 21/10/2026)."""
    repo = _runnable_copy(tmp_path)
    elsewhere = tmp_path / "ailleurs"
    elsewhere.mkdir()
    code = ("import importlib, os, sys; sys.path.insert(0, sys.argv[1]); "
            "tool = importlib.import_module(sys.argv[2]); tool._amorce(sys.argv[3]); "
            "print(os.environ.get('NoDefaultCurrentDirectoryInExePath'))")
    done = subprocess.run([sys.executable, "-c", code, str(repo / "tools"), tool, str(repo)],
                          cwd=elsewhere, capture_output=True, text=True, encoding="utf-8",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "1"
    installer = (repo / "tools" / "build_installer.py").read_text(encoding="utf-8")
    modules = [call for call in _calls(installer, "subprocess.run(")
               if '"-m"' in call or call.startswith("subprocess.run(command")]
    assert len(modules) == 3 and all("cwd=ROOT" in call for call in modules), modules


def _calls(text: str, start: str):
    """Le texte de chaque appel `start…)` de `text`, parenthèses équilibrées."""
    found, at = [], text.find(start)
    while at >= 0:
        depth = 0
        for end in range(at + len(start) - 1, len(text)):
            depth += {"(": 1, ")": -1}.get(text[end], 0)
            if depth == 0:
                break
        found.append(text[at:end + 1])
        at = text.find(start, end)
    return found


def test_the_android_build_reads_no_planted_bytecode(tmp_path):
    """Un .pyc planté dans __pycache__, à la date et à la taille du vrai module, serait
    exécuté à sa place : l'amorce n'en lit aucun (même amorce que tools/publish.py)."""
    import importlib.util
    from importlib._bootstrap_external import _code_to_timestamp_pyc
    repo = _runnable_copy(tmp_path)
    ran = tmp_path / "execute.txt"
    source = repo / "tools" / "companion_build.py"
    info = source.stat()
    code = compile(f"open({str(ran)!r}, 'w').write('piege')\n", str(source), "exec")
    pyc = importlib.util.cache_from_source(str(source))
    os.makedirs(os.path.dirname(pyc), exist_ok=True)
    with open(pyc, "wb") as f:
        f.write(_code_to_timestamp_pyc(code, int(info.st_mtime), info.st_size))
    subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import companion_build", str(repo / "tools")], capture_output=True)
    assert ran.exists()                                  # le piège marche sans l'amorce
    ran.unlink()
    done = _run_tool(repo, "--help")
    assert done.returncode == 0 and "--install" in done.stdout, done.stderr
    assert not ran.exists()


def _open_to_others(*opened):
    """foreign_writers simulé : `opened` modifiables par « Utilisateurs authentifiés »."""
    names = {str(p) for p in opened}
    return lambda path: (["AUTORITE NT\\Utilisateurs authentifiés (S-1-5-11)"]
                         if str(path) in names else [])


def test_the_android_build_checks_its_work_folders_before_building(tmp_path, monkeypatch):
    """Appelé sans l'amorce (main), l'outil contrôle aussi le dépôt ; et avant de construire
    quoi que ce soit, ce qu'il réutilise d'une fois sur l'autre et livre tel quel : un
    compagnon déjà compilé qu'un autre compte aurait remplacé partirait sur le téléphone."""
    repo = private.private_dir(tmp_path / "depot")
    (repo / "aiodos").mkdir()
    (repo / "aiodos" / "x.py").write_text("X = 1\n", encoding="utf-8")
    _commit_all(repo)
    cache = repo / "build" / "android-cache"
    cached = cache / "compagnon" / "0123456789ab" / "aiodos-net-2.0.2-android-aarch64"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"\x7fELF")
    dex = repo / "android" / "build" / "intermediates" / "classes.dex"   # sortie de Gradle
    dex.parent.mkdir(parents=True)
    dex.write_bytes(b"dex\n")
    monkeypatch.setattr(build_android, "ROOT", repo)
    monkeypatch.setattr(build_android, "PROJECT", repo / "android")
    monkeypatch.setattr(build_android, "STAGE", repo / "build" / "android")
    monkeypatch.setattr(build_android, "CACHE", cache)
    reused = build_android.work_paths()
    assert {cached, cached.parent, dex} <= set(reused)
    apk = tmp_path / "AIODOS-essai.apk"
    apk.write_bytes(b"PK")
    built = []
    monkeypatch.setattr(build_android, "build",
                        lambda out, new_key=False: built.append(out) or apk)
    out = tmp_path / "dist"
    assert build_android.main(["--out", str(out)]) == 0 and built == [out]   # tout est privé
    planted = repo / "aiodos" / "piege.py"                 # module non suivi : il partirait
    planted.write_text("PIEGE = True\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Application non construite") as refused:
        build_android.main(["--out", str(out)])
    assert "non suivis" in str(refused.value) and "aiodos/piege.py" in str(refused.value)
    planted.unlink()
    monkeypatch.setattr(private, "foreign_writers", _open_to_others(cached))
    with pytest.raises(SystemExit, match="modifiable par d'autres comptes") as refused:
        build_android.main(["--out", str(out)])
    assert str(cached) in str(refused.value)
    assert built == [out]                                  # jamais construite après un refus


def test_the_android_build_waits_for_a_commit(tmp_path, monkeypatch):
    """L'appli embarque aiodos/ tel qu'il est dans le dossier, mais compile le compagnon
    depuis le dernier commit de compagnon/ : construite avant le commit, elle associerait
    le nouvel AIODOS au compagnon d'avant (sans ses bornes), sans que rien ne le dise. Un
    fichier modifié, d'aiodos/ ou de compagnon/, l'arrête (relecture finale du 21/10/2026)."""
    repo = private.private_dir(tmp_path / "depot")
    for part in ("aiodos", "compagnon"):
        (repo / part).mkdir()
        (repo / part / "x.txt").write_text("1\n", encoding="utf-8")
    _commit_all(repo)
    monkeypatch.setattr(build_android, "ROOT", repo)
    monkeypatch.setattr(build_android, "PROJECT", repo / "android")
    monkeypatch.setattr(build_android, "STAGE", repo / "build" / "android")
    monkeypatch.setattr(build_android, "CACHE", repo / "build" / "android-cache")
    apk = tmp_path / "AIODOS-essai.apk"
    apk.write_bytes(b"PK")
    built = []
    monkeypatch.setattr(build_android, "build",
                        lambda out, new_key=False: built.append(out) or apk)
    out = tmp_path / "dist"
    assert build_android.uncommitted() == []
    assert build_android.main(["--out", str(out)]) == 0 and built == [out]
    for changed in ("aiodos/x.txt", "compagnon/x.txt"):
        (repo / changed).write_text("2\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="pas commités") as refused:
            build_android.main(["--out", str(out)])
        assert changed in str(refused.value)
        _commit_all(repo)
    assert built == [out]                                  # jamais construite sans commit


def test_the_android_build_tools_must_be_private(tmp_path, monkeypatch):
    """Le SDK Android (d8, aapt2, la plateforme : ils passent dans l'appli), le JDK et le
    Python de Chaquopy construisent l'appli signée : ouverts à d'autres comptes, ils sont
    refusés avant que rien ne soit préparé, comme cargo et le NDK du compagnon. Le SDK de la
    machine de l'auteur (C:\\dev\\android-sdk, NDK compris) l'était."""
    sdk, jdk, python = (private.private_dir(tmp_path / name) for name in ("sdk", "jdk", "py"))
    (sdk / "platforms").mkdir()
    build_android.require_private_tools(sdk, jdk, python)         # privés : rien à dire
    for opened, what in ((sdk, "SDK Android"), (jdk, "JDK"), (python, "Python 3.13")):
        monkeypatch.setattr(private, "foreign_writers", _open_to_others(opened))
        with pytest.raises(build_android.BuildError, match=what) as refused:
            build_android.require_private_tools(sdk, jdk, python)
        assert str(opened) in str(refused.value)
    monkeypatch.setattr(private, "foreign_writers", _open_to_others(sdk))
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.setenv("JAVA_HOME", str(jdk))
    monkeypatch.setattr(build_android, "python313", lambda: str(python / "python.exe"))
    monkeypatch.setattr(build_android, "stage_all",
                        lambda: pytest.fail("appli préparée avec un SDK ouvert à d'autres"))
    with pytest.raises(build_android.BuildError, match="SDK Android"):
        build_android.build(tmp_path / "dist")


def test_the_companion_commit_is_read_without_the_repository_settings(monkeypatch):
    """Le commit de compagnon/ (nom du compagnon gardé d'une fois sur l'autre) est lu par
    un git lancé sans fsmonitor, crochets ni objets de remplacement (GIT_SAFE)."""
    seen = []

    def run(*command, **options):
        seen.append([str(c) for c in command])
        raise build_android.BuildError("arrêt de l'essai")
    monkeypatch.setattr(build_android, "run", run)
    with pytest.raises(build_android.BuildError, match="arrêt"):
        build_android.companion(paths.data_dir())
    assert seen and seen[0][:len(private.GIT_SAFE) + 1] == ["git", *private.GIT_SAFE]


# ========================================================= vue Réseau : une seule garde
NODE = shutil.which("node")

STUBS = {
    "api.js": "export const get = async () => ({});\n"
              "export const post = async () => ({ peers: [] });\n"
              "export const del = async () => ({});\n",
    "icons.js": "export const icon = () => '';\n",
    "ui.js": "export const modal = async () => ({ ok: false }); export const menu = () => {};\n"
             "export const toast = () => {}; export const errorToast = () => {};\n"
             "export const confirmDanger = async () => false;\n",
    "views/connected.js": "export const renderConnected = () => {};\n",
    # dom.js de l'interface, dont les gardes finite et count sont épiées
    "dom.js": "export * from './dom-interface.js';\n"
              "import { finite as f, count as c } from './dom-interface.js';\n"
              "export const finite = v => { (globalThis.calls ||= []).push('finite'); return f(v); };\n"
              "export const count = v => { (globalThis.calls ||= []).push('count'); return c(v); };\n",
}
HOSTILE = {"memory": {"total": "<img src=x onerror=alert(1)>"},
           "gpus": [{"name": "<b>x</b>", "vram_total": {"length": "<img onerror=alert(2)>"}}],
           "ollama": {"models": {"length": "<img src=x onerror=alert(3)>"}},
           "idle_seconds": "<img src=x onerror=alert(4)>"}


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_the_network_view_guards_with_the_interface_helpers(tmp_path):
    """network.js gardait ses propres copies de finite et count (« num », « count ») :
    deux copies d'une même garde finissent par diverger. Il prend celles de dom.js."""
    js = tmp_path / "js"
    (js / "views").mkdir(parents=True)
    static = paths.STATIC_DIR / "js"
    (js / "dom-interface.js").write_bytes((static / "dom.js").read_bytes())
    for name in ("state.js", "views/network.js"):
        (js / name).write_bytes((static / name).read_bytes())
    for name, text in STUBS.items():
        (js / name).write_text(text, encoding="utf-8")
    prelude = i18n_js.install(js, module="./js/i18n.js")      # les textes, en français
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (tmp_path / "run.js").write_text(
        prelude +
        "import { peerFacts } from './js/views/network.js';\n"
        "globalThis.calls = [];\n"
        "const html = peerFacts({ profile: JSON.parse(process.argv[2]) });\n"
        "console.log(JSON.stringify({ html, calls: globalThis.calls }));\n", encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "run.js"), json.dumps(HOSTILE)],
                          capture_output=True, text=True, encoding="utf-8", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert {"finite", "count"} <= set(result["calls"]), result["calls"]
    assert "<" not in result["html"] and "· 0 modèle" in result["html"]
