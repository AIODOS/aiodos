"""La charte et les conditions d'utilisation (aiodos/legal) : les textes dans les deux
langues, leur accord dans l'appli (GET /api/legal, POST /api/legal/accept) ou par
l'installeur Windows (installation.json), et leur page dans l'installeur
(tools/installer_pages.py)."""
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiodos import legal, paths, settings
from aiodos.api.server import create_app

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import installer_pages  # noqa: E402

AUTH = {"X-Aiodos-Token": "jeton"}


@pytest.fixture
def client():
    with TestClient(create_app("jeton", {"testserver"})) as c:
        yield c


@pytest.mark.parametrize("doc", legal.DOCS)
def test_both_languages_say_the_same_thing(doc):
    """Même version, mêmes sections, dans le même ordre : une traduction qui manque une
    section, ou un texte changé dans une seule langue, se voit ici."""
    shape = {}
    for lang in ("fr", "en"):
        text = legal.text(doc, lang)
        title, body = legal.split(doc, lang)
        assert title and text.startswith(f"# {title}\n")
        assert re.search(rf"^\*Version {legal.VERSION}, ", body, re.M), (doc, lang)
        shape[lang] = [line.split(".")[0] for line in body.splitlines() if line.startswith("## ")]
    assert shape["fr"] == shape["en"] and shape["fr"]


def test_the_interface_reads_and_accepts_them(client):
    answer = client.get("/api/legal", headers={**AUTH, "Accept-Language": "fr"}).json()
    assert answer["version"] == legal.VERSION and answer["accepted"] == 0
    assert [d["id"] for d in answer["docs"]] == list(legal.DOCS)
    assert answer["docs"][0]["title"] == "Charte d'utilisation d'AIODOS"
    assert "<h2>" in answer["docs"][1]["html"] and "<h1>" not in answer["docs"][1]["html"]
    assert "<br" not in answer["docs"][0]["html"]          # les lignes repliées, rejointes
    english = client.get("/api/legal", headers={**AUTH, "Accept-Language": "en"}).json()
    assert english["docs"][1]["title"] == "AIODOS Terms of Use"
    # Une autre version que celle d'aujourd'hui (textes changés entre-temps) : refusée
    refused = client.post("/api/legal/accept", json={"version": legal.VERSION + 1}, headers=AUTH)
    assert refused.status_code == 409 and settings.get("terms_accepted") == 0
    done = client.post("/api/legal/accept", json={"version": legal.VERSION}, headers=AUTH)
    assert done.json() == {"accepted": legal.VERSION}
    assert client.get("/api/legal", headers=AUTH).json()["accepted"] == legal.VERSION
    assert client.post("/api/legal/accept", json={"version": 1}).status_code in (401, 403)


def test_accepted_in_the_windows_installer(tmp_path, monkeypatch):
    """L'installeur a fait accepter les textes : AIODOS ne les redemande pas, et le note dans
    ses réglages (une version reçue par mise à jour n'a pas installation.json à côté d'elle)."""
    mark = tmp_path / "installation.json"
    monkeypatch.setattr(paths, "INSTALLED_MARK", mark)
    assert legal.accepted() == 0
    mark.write_text(json.dumps({"version": "1.0.0", "terms": legal.VERSION}), encoding="utf-8")
    assert legal.accepted() == legal.VERSION
    mark.unlink()
    assert legal.accepted() == legal.VERSION == settings.get("terms_accepted")
    mark.write_text("{pas du json", encoding="utf-8")         # illisible : rien d'accepté de plus
    assert legal.installer_version() == 0


def test_the_installer_page_in_rtf():
    text = installer_pages.rtf(["# Charte {1}\n\n*Version 1*\n\n## Été\n\nLigne\nrepliée **gras**.\n\n"
                                "- puce\n  suite\n- 😀"])
    assert text.startswith("{\\rtf1\\ansi") and text.endswith("}\n")
    text.encode("ascii")                                       # tout hors ASCII est échappé
    assert "Charte \\{1\\}" in text
    assert "\\u201?t\\u233?" in text                           # « Été »
    assert "Ligne repli\\u233?e {\\b gras}." in text
    assert "{\\i Version 1}" in text
    assert "\\u8226?\\tab puce suite\\par" in text
    assert "\\u-10179?\\u-8704?" in text                       # 😀 : paire de substitution


def test_the_installer_has_the_texts_of_the_installed_code(tmp_path):
    """Les textes et leur version sont pris dans le code préparé (celui du commit)."""
    app = tmp_path / "app" / "aiodos" / "legal"
    for lang in ("fr", "en"):
        (app / lang).mkdir(parents=True)
        for doc in legal.DOCS:
            (app / lang / f"{doc}.md").write_text(f"# {doc} {lang}\n\nTexte.\n", encoding="utf-8")
    (app / "__init__.py").write_text('"""…"""\nVERSION = 7\n', encoding="utf-8")
    assert installer_pages.terms_version(tmp_path / "app") == 7
    logo = Path(__file__).resolve().parent.parent / "assets" / "logo.png"
    pages = installer_pages.prepare(tmp_path, logo)
    assert "terms en" in pages["legal"]["en"].read_text(encoding="ascii")
    assert len(pages["large"]) == len(installer_pages.LARGE)
    assert all(p.is_file() for p in pages["large"] + pages["small"])
