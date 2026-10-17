"""Outil de publication (tools/publish.py), essayé dans un dépôt git jetable : la clé
naît hors du dépôt, une version « -dev » ou non signée par une clé de confiance est
refusée, et le paquet publié est complet, signé, prêt à être transmis."""
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile

import pytest

from aiodos.updates import manifest as manifests
from aiodos.updates.store import Store
from tests.nodes import APP_ROOT

IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.org",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.org"}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=dict(os.environ, **IDENTITY)).stdout.strip()


def tool(repo, key, *args):
    return subprocess.run([sys.executable, str(repo / "tools" / "publish.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          env=dict(os.environ, **IDENTITY, AIODOS_RELEASE_KEY=str(key),
                                   PYTHONIOENCODING="utf-8"))


def set_version(repo, number):
    init = repo / "aiodos" / "__init__.py"
    init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{number}"',
                           init.read_text(encoding="utf-8")), encoding="utf-8")


def test_publication_from_key_to_package(tmp_path):
    repo = tmp_path / "depot"
    shutil.copytree(APP_ROOT / "aiodos", repo / "aiodos",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("tools", "assets"):
        shutil.copytree(APP_ROOT / name, repo / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("lancer.bat", "lancer-noeud.sh", "requirements.txt", "requirements-node.txt",
                 "LICENSE", "NOTICE", "README.md"):
        shutil.copyfile(APP_ROOT / name, repo / name)
    (repo / "aiodos" / "updates" / "keys.py").write_text("TRUSTED_KEYS = {\n}\n", encoding="utf-8")
    set_version(repo, "3.0.0-dev")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "départ")
    key = tmp_path / "hors-du-depot" / "cle.pem"

    made = tool(repo, key, "key")
    assert made.returncode == 0, made.stderr
    assert key.is_file() and (key.parent / "LISEZMOI.txt").is_file()
    assert "PRIVATE KEY" not in made.stdout                 # jamais affichée
    assert tool(repo, key, "key").returncode != 0           # jamais écrasée
    data = tmp_path / "data"
    refused = tool(repo, key, "release", "--data-dir", str(data))
    assert refused.returncode != 0 and "version de développement" in refused.stderr

    set_version(repo, "3.0.0")
    git(repo, "commit", "-qm", "version 3.0.0, sans la clé", "--", "aiodos/__init__.py")
    refused = tool(repo, key, "release", "--data-dir", str(data))
    assert refused.returncode != 0 and "ne fait pas confiance" in refused.stderr

    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "clé de publication")
    done = tool(repo, key, "release", "--data-dir", str(data), "--out", str(tmp_path / "out"),
                "--notes", "Première version")
    assert done.returncode == 0, done.stderr
    trusted = {}
    exec((repo / "aiodos" / "updates" / "keys.py").read_text(encoding="utf-8"), trusted)
    store = Store(data / "updates")
    assert store.complete("3.0.0", trusted["TRUSTED_KEYS"])
    found = store.manifest("3.0.0", trusted["TRUSTED_KEYS"])
    assert found["notes"] == "Première version" and found["commit"] == git(repo, "rev-parse", "HEAD")
    assert git(repo, "tag", "--list", "v3.0.0") == "v3.0.0"
    assert (tmp_path / "out" / "aiodos-3.0.0.zip").is_file()
    with zipfile.ZipFile(store.file_path("3.0.0", "aiodos-3.0.0.zip",
                                         trusted["TRUSTED_KEYS"])) as z:
        names = z.namelist()
    assert "aiodos/__init__.py" in names and "lancer.bat" in names
    assert not [n for n in names if n.startswith(("tools/", "tests/", "data/"))]
    again = tool(repo, key, "release", "--data-dir", str(data))
    assert again.returncode != 0 and "déjà publiée" in again.stderr
    assert manifests.FORMAT == found["format"]


# --------------------------------------------------------------------- compagnon
sys.path.insert(0, str(APP_ROOT / "tools"))
import companion_build  # noqa: E402

from aiodos.network import companion as companion_mod  # noqa: E402
from aiodos.updates import platforms  # noqa: E402

REAL = companion_mod._built(APP_ROOT)                # compagnon compilé dans ce dépôt
HERE = platforms.tag()
COMMIT = "0123456789abcdef0123456789abcdef01234567"

# Faux cargo : « construit » en recopiant un programme, avec ou sans le commit demandé
FAKE_CARGO = '''import os, sys
args = sys.argv[1:]
triple, profile = args[args.index("--target") + 1], args[args.index("--profile") + 1]
mode = os.environ.get("FAUX_CARGO", "ok")
if mode == "echec":
    sys.exit(101)
out = os.path.join(os.environ["CARGO_TARGET_DIR"], triple, profile)
os.makedirs(out, exist_ok=True)
data = open(os.environ["FAUX_CARGO_PROGRAMME"], "rb").read()
if mode != "sans-commit":
    data += os.environ["AIODOS_NET_COMMIT"].encode()
name = "aiodos-net.exe" if "windows" in triple else "aiodos-net"
open(os.path.join(out, name), "wb").write(data)
'''


def fake_cargo(folder):
    script = folder / "faux_cargo.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(FAKE_CARGO, encoding="utf-8")
    if sys.platform == "win32":
        wrapper = folder / "cargo.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        wrapper = folder / "cargo"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                           encoding="utf-8")
        wrapper.chmod(0o755)
    return wrapper


def release(repo, key, data, number, *extra, **env):
    set_version(repo, number)
    git(repo, "commit", "-qm", f"version {number}", "--", "aiodos/__init__.py")
    return subprocess.run([sys.executable, str(repo / "tools" / "publish.py"), "release",
                           "--data-dir", str(data), *extra], capture_output=True, text=True,
                          encoding="utf-8", env=dict(os.environ, **IDENTITY, **env,
                                                     AIODOS_RELEASE_KEY=str(key),
                                                     PYTHONIOENCODING="utf-8"))


@pytest.mark.skipif(REAL is None or HERE is None, reason="compagnon aiodos-net non compilé")
def test_companion_built_from_the_published_commit_is_joined(tmp_path):
    repo = tmp_path / "depot"
    for name in ("aiodos", "tools", "assets"):
        shutil.copytree(APP_ROOT / name, repo / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("lancer.bat", "lancer-noeud.sh", "requirements.txt", "requirements-node.txt",
                 "LICENSE", "NOTICE", "README.md"):
        shutil.copyfile(APP_ROOT / name, repo / name)
    shutil.copytree(APP_ROOT / "compagnon", repo / "compagnon",
                    ignore=shutil.ignore_patterns("target"))
    (repo / "aiodos" / "updates" / "keys.py").write_text("TRUSTED_KEYS = {\n}\n", encoding="utf-8")
    git(repo, "init", "-q")
    key = tmp_path / "cle.pem"
    assert tool(repo, key, "key").returncode == 0
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "départ")
    data, out = tmp_path / "data", tmp_path / "out"
    cargo = {"AIODOS_CARGO": str(fake_cargo(tmp_path / "outils")),
             "FAUX_CARGO_PROGRAMME": str(REAL)}

    failed = release(repo, key, data, "3.0.0", "--targets", HERE, **cargo, FAUX_CARGO="echec")
    assert failed.returncode != 0 and "Rien n'a été publié" in failed.stderr
    unsigned = release(repo, key, data, "3.0.1", "--targets", HERE, **cargo,
                       FAUX_CARGO="sans-commit")
    assert unsigned.returncode != 0 and "ne porte pas le commit publié" in unsigned.stderr
    assert not (data / "updates" / "packages").exists()     # rien de publié

    done = release(repo, key, data, "3.0.2", "--targets", HERE, "--out", str(out), **cargo)
    assert done.returncode == 0, done.stderr
    assert f"Compagnon joint : aiodos-net-{HERE}" in done.stdout
    trusted = {}
    exec((repo / "aiodos" / "updates" / "keys.py").read_text(encoding="utf-8"), trusted)
    store = Store(data / "updates")
    found = store.manifest("3.0.2", trusted["TRUSTED_KEYS"])
    item = next(f for f in found["files"] if f["kind"] == f"aiodos-net-{HERE}")
    program = store.package_path("3.0.2", item)
    assert item["name"] == companion_build.file_name("3.0.2", HERE)
    assert program.read_bytes().endswith(found["commit"].encode())    # construit pour ce commit
    assert store.held("3.0.2", trusted["TRUSTED_KEYS"]) == found["files"]
    assert (out / item["name"]).read_bytes() == program.read_bytes()

    alone = release(repo, key, data, "3.0.3", "--no-companion", **cargo)
    assert alone.returncode == 0, alone.stderr
    assert [f["kind"] for f in store.manifest("3.0.3", trusted["TRUSTED_KEYS"])["files"]] == \
        ["source"]
    nothing = release(repo, key, data, "3.0.4", AIODOS_CARGO=str(tmp_path / "pas-de-cargo"))
    assert nothing.returncode == 0, nothing.stderr
    assert "ATTENTION : compagnon non joint" in nothing.stdout and "cargo" in nothing.stdout
    assert [f["kind"] for f in store.manifest("3.0.4", trusted["TRUSTED_KEYS"])["files"]] == \
        ["source"]


def pe(machine: int) -> bytes:
    return b"MZ" + bytes(0x3A) + struct.pack("<I", 0x40) + b"PE\0\0" + struct.pack("<H", machine)


def elf(machine: int, android: bool) -> bytes:
    return (b"\x7fELF" + bytes([2, 1, 1]) + bytes(9) + struct.pack("<HH", 3, machine)
            + (b"/system/bin/linker64" if android else b"/lib/ld-linux-aarch64.so.1"))


@pytest.mark.parametrize("content, target, ok", [
    (pe(0x8664), "windows-x86_64", True),
    (pe(0xAA64), "windows-x86_64", False),                  # ARM64 : autre plateforme
    (elf(183, android=True), "android-aarch64", True),
    (elf(183, android=False), "android-aarch64", False),    # Linux, pas Android
    (elf(62, android=True), "android-aarch64", False),      # x86-64
    (b"#!/bin/sh\n", "android-aarch64", False),
])
def test_companion_format_is_checked(tmp_path, content, target, ok):
    path = tmp_path / "programme"
    path.write_bytes(content + COMMIT.encode())
    if ok:
        companion_build.check(path, target, COMMIT)
    else:
        with pytest.raises(companion_build.BuildError, match="pas un programme"):
            companion_build.check(path, target, COMMIT)


def test_companion_from_another_commit_is_refused(tmp_path):
    path = tmp_path / "programme"
    path.write_bytes(elf(183, android=True) + b"f" * 40)
    with pytest.raises(companion_build.BuildError, match="ne porte pas le commit"):
        companion_build.check(path, "android-aarch64", COMMIT)
