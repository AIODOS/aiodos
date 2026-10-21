"""La carte d'accord des applications (ui/static/js/views/gateway-request.js) échappe tout ce
qui vient de l'appli : nom, version, raison, programme. Une appli qui s'appellerait
« <img src=x onerror=…> » ne fait rien exécuter dans la fenêtre d'AIODOS. Rendu par Node.js
(ignoré s'il est absent, comme test_ui_syntax.py)."""
import json
import shutil
import subprocess

import pytest

from aiodos import paths

NODE = shutil.which("node")
SCRIPT = r"""import { pathToFileURL } from 'node:url';
globalThis.location = { search: '' };
globalThis.sessionStorage = { getItem: () => '', setItem() {} };
globalThis.history = { replaceState() {} };
globalThis.window = { addEventListener() {} };
const mod = await import(pathToFileURL(process.argv[2]).href);
const req = JSON.parse(process.argv[3]);
process.stdout.write(mod.renderRequest(req));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_the_consent_card_escapes_what_the_app_says(tmp_path):
    hostile = '<img src=x onerror="alert(1)">'
    request = {"id": "req_1", "user_code": "482 197", "client_name": hostile,
               "client_version": hostile, "purpose": hostile, "program": hostile,
               "program_generic": False, "scopes": ["chat", "embed"],
               "where": ["here", "mine", "friends", "public"], "expires": 9e9,
               "created": 1.7e9, "upgrade": None, "existing": {"app_id": "x", "created": 1.7e9},
               "served": {"chat": ["friends", "here", "mine"], "embed": ["here"]}}
    script = tmp_path / "rendu.mjs"
    script.write_text(SCRIPT, encoding="utf-8")
    card = paths.STATIC_DIR / "js" / "views" / "gateway-request.js"
    done = subprocess.run([NODE, str(script), str(card), json.dumps(request)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    html = done.stdout
    assert "<img" not in html and "&lt;img" in html
    assert "482 197" in html
    assert "pas encore servi" in html                     # le cercle public, pas encore servi
    assert html.count('data-where=') == 4 and html.count('data-scope=') == 2
