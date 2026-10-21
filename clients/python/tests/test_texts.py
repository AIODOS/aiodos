# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""Les deux clients disent la même chose : les textes des états, des codes et des conseils
sont ceux de clients/js/src/texts.js, mot pour mot, pour tout ce qu'ils ont en commun."""

import re

import pytest

from aiodos_client import texts
from support import SOURCE

JS = SOURCE.parents[2] / "js" / "src" / "texts.js"
ONLY_JS = {"proxy"}                                    # le proxy de Node : sans objet ici
ONLY_PYTHON = {"no_model", "image_by_url", "bad_image", "too_large"}


def _js_tables():
    source = JS.read_text(encoding="utf-8")
    tables = {}
    for name, body in re.findall(r"const (STATES|CODES|HINTS) = \{(.*?)\n\};", source, re.S):
        tables[name] = {}
        for lang, entries in re.findall(r"\b(fr|en): \{(.*?)\n  \}", body, re.S):
            found = re.findall(r"(\w+):\s*([\"'])((?:\\.|(?!\2).)*)\2", entries, re.S)
            tables[name][lang] = {key: re.sub(r"\\(.)", r"\1", value)
                                  for key, _, value in found}
    return tables


@pytest.fixture(scope="module")
def js():
    if not JS.is_file():
        pytest.skip("client JavaScript absent de cette copie")
    return _js_tables()


@pytest.mark.parametrize("lang", ["fr", "en"])
def test_states_are_the_same(js, lang):
    assert texts.STATES[lang] == js["STATES"][lang]


@pytest.mark.parametrize("lang", ["fr", "en"])
def test_codes_and_hints_are_the_same(js, lang):
    for ours, theirs in ((texts.ERRORS[lang], js["CODES"][lang]),
                         (texts.HINTS[lang], js["HINTS"][lang])):
        assert set(theirs) - set(ours) <= ONLY_JS
        assert set(ours) - set(theirs) <= ONLY_PYTHON
        for code in set(ours) & set(theirs):
            assert ours[code] == theirs[code], code


def test_the_reference_wordings():
    assert texts.state("needs_access") == "L'appli n'a pas encore accès à AIODOS."
    assert texts.state("pending", code="482 197") == ("Autorise l'appli dans la fenêtre "
                                                      "d'AIODOS. Code : 482 197")
    assert "Connexion refusée" in texts.error("unreachable", host="127.0.0.1:11435")
    assert texts.error("cancelled") == "Arrêté"
    assert texts.lang_of("en-US") == "en" and texts.lang_of("de") == "fr"
