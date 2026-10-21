"""Les scripts de l'interface se lisent sans erreur de syntaxe (node --check, si Node.js est
là) : une seule apostrophe mal échappée laissait toute l'interface blanche. Et chaque nom
importé existe dans le module qui doit le fournir : un import manquant la laisse aussi blanche."""
import json
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


# Charge chaque module de l'interface : Node relie d'abord tous les imports (un nom absent :
# SyntaxError), puis exécute le module, qui échoue sans navigateur (document…) : sans importance
LINK = r"""import { pathToFileURL } from 'node:url';
process.on('unhandledRejection', () => {});
const broken = [];
for (const file of process.argv.slice(2)) {
  try { await import(pathToFileURL(file).href); }
  catch (e) {
    if (e instanceof SyntaxError || e?.code === 'ERR_MODULE_NOT_FOUND') {
      broken.push(`${file.split(/[\\/]/).slice(-2).join('/')} : ${e.message}`);
    }
  }
}
console.log(JSON.stringify(broken));
process.exit(0);
"""


def unlinked_imports(js_dir, tmp_path) -> list:
    """Les imports qui ne se relient pas, module par module (Node.js)."""
    copy = tmp_path / "js"
    shutil.copytree(js_dir, copy)
    (copy / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (tmp_path / "relier.mjs").write_text(LINK, encoding="utf-8")
    scripts = sorted(str(script) for script in copy.rglob("*.js"))
    done = subprocess.run([NODE, str(tmp_path / "relier.mjs"), *scripts], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 0, done.stderr[:400]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_interface_imports_exist(tmp_path):
    """Un module modifié sans celui qu'il importe (chat.js prend inNativeWindow dans api.js)
    passe node --check, mais toute l'interface reste blanche."""
    assert unlinked_imports(paths.STATIC_DIR / "js", tmp_path) == []
