# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Le paquet : en-têtes de licence et version partout, syntaxe de Python 3.10, imports en
tête de module (PyInstaller), jamais print, versions concordantes, LICENSE d'AIODOS."""

import ast
import re
import sys
from pathlib import Path

import pytest

import aiodos_client
from support import SOURCE

PACKAGE = SOURCE.parents[1]                     # clients/python
FILES = sorted(PACKAGE.glob("src/aiodos_client/*.py")) + sorted(PACKAGE.glob("tests/*.py"))


@pytest.mark.parametrize("path", FILES + [PACKAGE / "pyproject.toml", PACKAGE / "README.md"],
                         ids=lambda p: p.name)
def test_headers(path):
    head = path.read_text(encoding="utf-8").splitlines()[:4]
    assert any("SPDX-License-Identifier: Apache-2.0" in line for line in head)
    assert any("aiodos-client 0.2.0" in line for line in head)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_python_310_syntax(path):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))


def _late_imports(tree):
    """Les imports qui ne sont pas au niveau du module (ni dans un try/if de ce niveau)."""
    allowed = set()
    for node in tree.body:
        for child in ast.walk(node) if isinstance(node, (ast.Try, ast.If)) else [node]:
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                allowed.add(id(child))
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in allowed]


@pytest.mark.parametrize("path", sorted(SOURCE.glob("*.py")), ids=lambda p: p.name)
def test_imports_on_top_and_no_print(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert _late_imports(tree) == []
    calls = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "print"]
    assert calls == []


def test_versions_agree():
    project = (PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "0\.2\.0"$', project, re.MULTILINE)
    assert re.search(r"^dependencies = \[\]$", project, re.MULTILINE)
    assert aiodos_client.__version__ == "0.2.0" and aiodos_client.API_VERSION == 1
    init = (SOURCE / "__init__.py").read_text(encoding="utf-8")
    assert re.search(r'^__version__\s*=\s*"0\.2\.0"', init, re.MULTILINE)


def test_license_is_the_one_of_aiodos():
    ours = (PACKAGE / "LICENSE").read_bytes()
    root = PACKAGE.parents[1] / "LICENSE"
    if root.is_file():
        assert ours == root.read_bytes()
    assert b"Apache License" in ours and (PACKAGE / "NOTICE").is_file()


def test_standard_library_only():
    allowed = set(sys.stdlib_module_names) | {"aiodos_client"}
    for path in SOURCE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                assert name.split(".")[0] in allowed, f"{path.name} : {name}"
