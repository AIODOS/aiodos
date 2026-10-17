"""Les scripts de l'interface se lisent sans erreur de syntaxe (node --check, si Node.js est
là) : une seule apostrophe mal échappée laissait toute l'interface blanche."""
import shutil
import subprocess

import pytest

from aiodos import paths

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_interface_scripts_parse(tmp_path):
    scripts = sorted((paths.STATIC_DIR / "js").rglob("*.js"))
    assert scripts
    for script in scripts:
        name = "_".join(script.relative_to(paths.STATIC_DIR).with_suffix(".mjs").parts)
        copy = tmp_path / name                    # .mjs : lu comme un module ES
        copy.write_bytes(script.read_bytes())
        done = subprocess.run([NODE, "--check", str(copy)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        assert done.returncode == 0, f"{script.name} : {done.stderr.strip()[:400]}"
