"""La carte d'accord des applications (ui/static/js/views/gateway-request.js) échappe tout ce
qui vient de l'appli : nom, version, raison, programme. Une appli qui s'appellerait
« <img src=x onerror=…> » ne fait rien exécuter dans la fenêtre d'AIODOS, en français comme en
anglais. Rendu par Node.js (ignoré s'il est absent, comme test_ui_syntax.py)."""
import json
import shutil
import subprocess

import pytest

from aiodos import i18n, paths

NODE = shutil.which("node")
# Les textes de la langue, posés dans l'i18n.js que la carte importe (le même module)
SCRIPT = r"""import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
globalThis.location = { search: '' };
globalThis.sessionStorage = { getItem: () => '', setItem() {} };
globalThis.history = { replaceState() {} };
globalThis.window = { addEventListener() {} };
const card = pathToFileURL(process.argv[2]);
const { setTexts } = await import(new URL('../i18n.js', card).href);
setTexts(process.argv[4], JSON.parse(readFileSync(process.argv[5], 'utf8')));
const mod = await import(card.href);
const req = JSON.parse(process.argv[3]);
process.stdout.write(mod.renderRequest(req));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
@pytest.mark.parametrize("lang, later", [("fr", "pas encore servi"),
                                         ("en", "not served by this version yet")],
                         ids=["fr", "en"])
def test_the_consent_card_escapes_what_the_app_says(tmp_path, lang, later):
    hostile = '<img src=x onerror="alert(1)">'
    request = {"id": "req_1", "user_code": "482 197", "client_name": hostile,
               "client_version": hostile, "purpose": hostile, "program": hostile,
               "program_generic": False, "scopes": ["chat", "embed"],
               "where": ["here", "mine", "friends", "public"], "expires": 9e9,
               "created": 1.7e9, "upgrade": None, "existing": {"app_id": "x", "created": 1.7e9},
               "served": {"chat": ["friends", "here", "mine"], "embed": ["here"]}}
    script = tmp_path / "rendu.mjs"
    script.write_text(SCRIPT, encoding="utf-8")
    texts = tmp_path / f"textes-{lang}.json"
    texts.write_text(json.dumps(i18n.texts(lang), ensure_ascii=False), encoding="utf-8")
    card = paths.STATIC_DIR / "js" / "views" / "gateway-request.js"
    done = subprocess.run([NODE, str(script), str(card), json.dumps(request), lang, str(texts)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    html = done.stdout
    assert "<img" not in html and "&lt;img" in html
    assert "482 197" in html
    assert later in html                                  # le cercle public, pas encore servi
    assert "gateway_request." not in html                 # aucun texte manquant
    assert html.count('data-where=') == 4 and html.count('data-scope=') == 2
