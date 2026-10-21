"""Les textes d'AIODOS existent en français et en anglais, et aucun ne reste écrit en dur :
- chaque fichier de textes a son pendant dans l'autre langue, avec les mêmes clés et les
  mêmes paramètres ({nom}) ;
- chaque clé que l'interface, le nœud ou l'appli Android utilise existe ;
- aucune chaîne de l'interface ni du nœud ne ressemble à un texte (français, ou une phrase)
  hors des catalogues, sauf celles marquées « i18n: tel quel » (motifs, protocoles) ;
- le catalogue anglais ne contient pas de français.
"""
import json
import re
from pathlib import Path

import pytest

from aiodos import paths
from aiodos.i18n import catalog
from tests import i18n_scan

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "aiodos" / "i18n"
STATIC = paths.STATIC_DIR
FIELD = re.compile(r"\{(\w+)\}")
EXTENSIONS = {"json", "js", "py", "css", "html", "md", "txt", "db", "log", "png", "jpg",
              "jpeg", "webp", "gif", "svg", "ico", "gguf", "safetensors", "exe", "zip",
              "toml", "yaml", "yml", "sh", "bat", "mjs", "wasm", "so", "dll"}
# Ce qui s'écrit de la même façon dans les deux langues, et contient des lettres françaises
SAME_IN_BOTH = {"settings.language_fr"}


def files(lang):
    return {p.name: p for p in sorted((I18N / lang).glob("*.json"))}


def features():
    return {p.stem for p in (I18N / catalog.SOURCE).glob("*.json")}


def fields(value) -> set:
    forms = value.values() if isinstance(value, dict) else [value]
    return {name for form in forms for name in FIELD.findall(str(form))}


def test_every_file_exists_in_both_languages():
    assert set(files("fr")) == set(files("en"))
    assert files("fr"), "aucun fichier de textes"


@pytest.mark.parametrize("name", sorted(files("fr")))
def test_same_keys_and_parameters(name):
    fr = json.loads((I18N / "fr" / name).read_text(encoding="utf-8"))
    en = json.loads((I18N / "en" / name).read_text(encoding="utf-8"))
    assert list(fr) == list(en), f"{name} : clés différentes (ou dans un autre ordre)"
    for key in fr:
        assert fields(fr[key]) == fields(en[key]), f"{name} : paramètres de « {key} »"
        for value in (fr[key], en[key]):
            if isinstance(value, dict):
                assert set(value) <= {"one", "other"} and "other" in value, f"{name}:{key}"
                assert all(isinstance(v, str) for v in value.values()), f"{name}:{key}"
            else:
                assert isinstance(value, str), f"{name}:{key}"


@pytest.mark.parametrize("lang", catalog.LANGUAGES)
def test_files_are_utf8_lf_and_well_formed(lang):
    for path in files(lang).values():
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} : BOM"
        assert b"\r" not in raw, f"{path.name} : fins de ligne CRLF"
        assert raw.endswith(b"\n"), f"{path.name} : pas de fin de ligne finale"
        data = json.loads(raw.decode("utf-8"))
        assert isinstance(data, dict) and data, path.name
        assert re.fullmatch(r"[a-z][a-z0-9_]*", path.stem), path.name


HTML_TAGS = re.compile(r"</?(b|i|u|s|br|hr|code|small|em|strong|kbd|div|span|a|p|ul|ol|li|h\d|"
                       r"img|button|input|label|select|option|table|tr|td|th|details|summary|"
                       r"pre|sup|sub|section|nav|main|header|footer|svg|path|script|style)"
                       r"\b[^>]*>", re.IGNORECASE)
ALLOWED_TAGS = {"b", "i", "br", "code", "small", "em", "strong", "kbd"}


@pytest.mark.parametrize("lang", catalog.LANGUAGES)
def test_markup_only_in_html_texts(lang):
    """Un texte est du texte brut (échappé à l'affichage) ; seule une clé en _html porte un
    peu de mise en forme, sans attribut ni lien."""
    for key, value in catalog.read(I18N / lang).items():
        for text in (value.values() if isinstance(value, dict) else [value]):
            tags = HTML_TAGS.findall(text)
            if key.endswith("_html"):
                assert set(t.lower() for t in tags) <= ALLOWED_TAGS, f"{lang}:{key}"
                assert not re.search(r"<[a-zA-Z]+\s", text), f"{lang}:{key} : attribut"
            else:
                assert not tags, f"{lang}:{key} : du HTML hors d'une clé en _html"


def test_no_french_in_english():
    english = catalog.read(I18N / "en")
    found = []
    for key, value in english.items():
        if key in SAME_IN_BOTH:
            continue
        for text in (value.values() if isinstance(value, dict) else [value]):
            words = {w.lower() for w in i18n_scan.WORD.findall(text.replace("’", "'"))}
            if (i18n_scan.FRENCH_CHARS.search(text.replace("’", "'"))
                    or words & i18n_scan.FUNCTION_WORDS
                    or any(i18n_scan.ELISION.match(w) for w in words)):
                found.append(f"{key} : {text[:80]}")
    assert not found, "Du français dans le catalogue anglais :\n" + "\n".join(found)


def test_french_differs_from_english():
    """Un texte resté le même dans les deux langues est le plus souvent un oubli."""
    fr, en = catalog.read(I18N / "fr"), catalog.read(I18N / "en")

    def words(value):              # les mots d'un texte (de sa forme la plus longue)
        forms = value.values() if isinstance(value, dict) else [value]
        return max(([w for w in i18n_scan.WORD.findall(FIELD.sub(" ", form)) if len(w) > 2]
                    for form in forms), key=len)
    same = [k for k in fr if fr[k] == en.get(k) and len(words(fr[k])) >= 3]
    assert not same, "Textes identiques dans les deux langues :\n" + "\n".join(same)


# ------------------------------------------------------------------ clés utilisées
def _key_like(text, known) -> bool:
    if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+", text):
        return False
    feature, _, rest = text.partition(".")
    return feature in known and rest.rsplit(".", 1)[-1] not in EXTENSIONS


def used_keys():
    """{clé: [où]} : les chaînes qui ont la forme d'une clé d'un catalogue existant, dans
    l'interface (scripts et HTML), le nœud, et l'appli Android (Python : android_app). Une
    chaîne marquée « i18n: tel quel » n'en est pas une (type d'objet d'une API…)."""
    known = features()
    used = {}
    for script in sorted(STATIC.rglob("*.js")):
        source = script.read_text(encoding="utf-8")
        lines = source.splitlines()
        for line, text in i18n_scan.js_literals(source):
            if _key_like(text, known) and not i18n_scan.marked(lines, line):
                used.setdefault(text, []).append(f"{script.name}:{line}")
    for page in sorted(STATIC.rglob("*.html")):
        for text in re.findall(r'data-t(?:-[a-z]+)?="([^"]+)"', page.read_text(encoding="utf-8")):
            used.setdefault(text, []).append(page.name)
    for module in sorted((ROOT / "aiodos").rglob("*.py")):
        lines = module.read_text(encoding="utf-8").splitlines()
        for start, last, text, line in _all_strings(module):
            if not _key_like(text, known):
                continue
            if any(i18n_scan.MARK in lines[k - 1]
                   for k in range(max(1, start - 1), min(len(lines), last) + 1)):
                continue
            used.setdefault(text, []).append(f"{module.relative_to(ROOT)}:{line}")
    return used


def _all_strings(module):
    """[(début de son instruction, dernière ligne, texte, ligne)] de toutes les chaînes."""
    import ast
    tree = ast.parse(module.read_text(encoding="utf-8"))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            statement = n
            while statement is not None and not isinstance(statement, ast.stmt):
                statement = parents.get(statement)
            start = statement.lineno if statement is not None else n.lineno
            found.append((min(start, n.lineno), n.end_lineno, n.value, n.lineno))
    return found


def test_every_used_key_exists():
    keys = set(catalog.read(I18N / "fr"))

    def known(key):            # « jobs.status_ » : le début d'une clé faite à l'affichage
        return key in keys or (key.endswith(("_", ".")) and any(k.startswith(key) for k in keys))
    missing = [f"{key} ({', '.join(where[:3])})" for key, where in sorted(used_keys().items())
               if not known(key)]
    assert not missing, "Clés absentes des catalogues :\n" + "\n".join(missing)


# ------------------------------------------------------------ textes écrits en dur
def test_no_hardcoded_text_in_interface_scripts():
    found = []
    for script in sorted(STATIC.rglob("*.js")):
        for line, text in i18n_scan.hardcoded_js(script):
            found.append(f"{script.relative_to(STATIC)}:{line} : {text.strip()[:90]!r}")
    assert not found, "Textes écrits en dur dans l'interface :\n" + "\n".join(found)


def test_no_hardcoded_text_in_interface_page():
    from html.parser import HTMLParser

    class Texts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.found = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1
            for name, value in attrs:
                if name in ("title", "alt", "aria-label", "placeholder") and value \
                        and value != "AIODOS":
                    self.found.append(f"{tag}[{name}] : {value}")

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.skip -= 1

        def handle_data(self, data):
            if not self.skip and data.strip() and data.strip() != "AIODOS":
                self.found.append(data.strip())

    for page in sorted(STATIC.rglob("*.html")):
        parser = Texts()
        parser.feed(page.read_text(encoding="utf-8"))
        assert not parser.found, f"{page.name} : textes écrits en dur : {parser.found}"


SCANNED = ["aiodos"]
NOT_SCANNED = ("aiodos/i18n/",)


def test_no_hardcoded_text_in_node():
    found = []
    for folder in SCANNED:
        for module in sorted((ROOT / folder).rglob("*.py")):
            rel = module.relative_to(ROOT).as_posix()
            if rel.startswith(NOT_SCANNED):
                continue
            for line, text in i18n_scan.hardcoded_py(module):
                found.append(f"{rel}:{line} : {text.strip()[:90]!r}")
    assert not found, (f"{len(found)} textes écrits en dur dans le nœud :\n" + "\n".join(found))
