"""Outil de publication (tools/publish.py), essayé dans un dépôt git jetable : la clé
naît hors du dépôt, une version « -dev » ou non signée par une clé de confiance est
refusée, et le paquet publié est complet, signé, prêt à être transmis."""
import os
import re
import shutil
import subprocess
import sys
import zipfile

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
                 "LICENSE", "README.md"):
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
