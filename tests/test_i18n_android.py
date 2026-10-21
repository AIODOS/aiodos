"""L'appli Android en français et en anglais : chaque texte de res/values (l'anglais, langue
de repli) a son pendant dans res/values-fr, avec les mêmes paramètres ; le Java n'écrit plus
de texte en dur ; le nœud donne au Java la langue choisie dans les Réglages (Texts.java)."""
import re
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from aiodos import i18n
from aiodos.i18n import language
from aiodos.node import android_app
from tests import i18n_scan

ANDROID = Path(__file__).resolve().parent.parent / "android"
DEFAULT, FRENCH = ANDROID / "res" / "values", ANDROID / "res" / "values-fr"
ARGS = re.compile(r"%\d+\$[sd]")


def strings(folder: Path) -> dict:
    found = {}
    for path in sorted(folder.glob("*.xml")):
        for node in ET.parse(path).getroot().iter("string"):
            found[node.get("name")] = (path.name, node.get("translatable") != "false",
                                       "".join(node.itertext()))
    return found


def test_every_text_exists_in_both_languages():
    english, french = strings(DEFAULT), strings(FRENCH)
    wanted = {name for name, (_, translatable, _) in english.items() if translatable}
    assert wanted, "aucun texte dans res/values"
    assert wanted == set(french), "res/values-fr ne suit pas res/values"
    for name in wanted:
        assert english[name][0] == french[name][0], f"{name} : pas dans le même fichier"
        assert sorted(ARGS.findall(english[name][2])) == sorted(ARGS.findall(french[name][2])), name
        assert english[name][2] != french[name][2] or len(english[name][2]) < 8, name


def test_english_has_no_french():
    for name, (_, translatable, text) in strings(DEFAULT).items():
        assert not i18n_scan.FRENCH_CHARS.search(text.replace("’", "'")), name


def java_literals(source: str):
    """(ligne, texte) des chaînes "…" du Java, hors journaux (Log.*)."""
    for number, line in enumerate(source.splitlines(), 1):
        if line.strip().startswith(("//", "*", "/*")):            # commentaires
            continue
        if re.search(r"\bLog\.[a-z]\(", line) or i18n_scan.MARK in line:
            continue
        for match in re.finditer(r'"((?:[^"\\]|\\.)*)"', line):
            yield number, match.group(1)


def test_no_hardcoded_text_in_java():
    found = []
    for path in sorted((ANDROID / "src").rglob("*.java")):
        source = path.read_text(encoding="utf-8")
        found += [f"{path.name}:{n} : {text}" for n, text in java_literals(source)
                  if i18n_scan.looks_like_text(text)]
    assert not found, "Textes écrits en dur dans le Java :\n" + "\n".join(found)


def test_the_java_side_hears_the_chosen_language(monkeypatch):
    told = []

    class Texts:
        @staticmethod
        def choose(lang):
            told.append(lang)
    java = types.ModuleType("java")
    java.jclass = lambda name: Texts if name == "fr.arkane.aiodos.Texts" else None
    monkeypatch.setitem(sys.modules, "java", java)
    monkeypatch.setenv(android_app.APP_ENV, "1")
    from aiodos import settings
    settings.set_value("language", "en")
    android_app.tell_language()
    settings.set_value("language", "auto")
    android_app.tell_language()
    assert told == ["en", None]                 # None : la langue du téléphone


def test_outside_the_app_the_language_stays_in_python(monkeypatch):
    monkeypatch.setitem(sys.modules, "java", None)           # import java : ImportError
    monkeypatch.delenv(android_app.APP_ENV, raising=False)
    android_app.tell_language()                              # rien, sans erreur


@pytest.mark.parametrize("tag, expected", [("fr-FR", "fr"), ("en-US", "en"), ("de-DE", None)])
def test_in_the_app_the_phone_language_is_the_system_one(monkeypatch, tag, expected):
    class Locale:
        @staticmethod
        def getDefault():
            return types.SimpleNamespace(toLanguageTag=lambda: tag)
    java = types.ModuleType("java")
    java.jclass = lambda name: Locale if name == "java.util.Locale" else None
    monkeypatch.setitem(sys.modules, "java", java)
    monkeypatch.setenv(android_app.APP_ENV, "1")
    monkeypatch.delenv("AIODOS_LANGUAGE", raising=False)
    monkeypatch.setattr(language, "_windows_languages", lambda: [])
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(language.locale, "getlocale", lambda: (None, None))
    monkeypatch.setitem(language._node, "system", None)
    assert language.system() == expected
    language.forget()
    assert i18n.current() == (expected or "en")
    monkeypatch.setitem(language._node, "system", None)
