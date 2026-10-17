"""Installeur Windows : données d'une copie installée, moteurs joints, outil de
construction (sans rien télécharger ni compiler)."""
import sys
import types

import pytest

from aiodos import paths
from aiodos.updates import engines, platforms


def test_an_installed_copy_keeps_its_data_with_the_user(tmp_path, monkeypatch):
    monkeypatch.delenv("AIODOS_DATA_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(paths, "INSTALLED_MARK", tmp_path / "absent.json")
    assert paths.installed_data_dir() is None                  # dépôt, archive : data/ à côté
    mark = tmp_path / "installation.json"
    mark.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(paths, "INSTALLED_MARK", mark)
    assert paths.data_dir() == tmp_path / "local" / "AIODOS" / "data"
    monkeypatch.setenv("AIODOS_DATA_DIR", str(tmp_path / "choisi"))   # --data-dir, essais
    assert paths.data_dir() == tmp_path / "choisi"


def test_engines_joined_by_the_installer_are_found(tmp_path, monkeypatch):
    origin, version = tmp_path / "origine", tmp_path / "version"
    folder = origin / "engines" / engines.IMAGES
    folder.mkdir(parents=True)
    (folder / platforms.executable("sd-server")).write_bytes(b"MZ")
    monkeypatch.setattr(paths, "APP_ROOT", version)
    monkeypatch.delenv("AIODOS_BASE_DIR", raising=False)
    assert engines.bundled(engines.IMAGES) is None
    monkeypatch.setenv("AIODOS_BASE_DIR", str(origin))           # version lancée par le lanceur
    assert engines.bundled(engines.IMAGES) == folder
    assert engines.bundled(engines.LLAMA) is None


def test_the_build_tool_pins_what_was_tested(monkeypatch):
    sys.path.insert(0, str(paths.APP_ROOT / "tools"))
    import build_installer
    found = build_installer.pinned(["requests", "numpy"])
    assert [p.split("==")[0] for p in found] == ["requests", "numpy"]
    assert all(p.split("==")[1] for p in found)
    with pytest.raises(build_installer.BuildError, match="pas installé"):
        build_installer.pinned(["paquet-qui-n-existe-pas"])
    assert build_installer.version() == __import__("aiodos").__version__


def test_the_runtime_ignores_the_user_packages(monkeypatch):
    sys.path.insert(0, str(paths.APP_ROOT / "tools"))
    import build_installer
    fake_site = types.SimpleNamespace(USER_SITE=r"C:\Users\x\AppData\Roaming\Python\Python314\site-packages",
                                      ENABLE_USER_SITE=True)
    path = [r"C:\AIODOS\runtime\python314.zip", fake_site.USER_SITE,
            r"C:\AIODOS\runtime\Lib\site-packages"]
    monkeypatch.setitem(sys.modules, "site", fake_site)
    monkeypatch.setattr(sys, "path", list(path))
    exec(build_installer.SITECUSTOMIZE, {})
    assert sys.path == [path[0], path[2]] and fake_site.ENABLE_USER_SITE is False
