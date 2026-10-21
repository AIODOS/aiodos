"""Le mécanisme des textes (aiodos/i18n) : formes selon le nombre, paramètres, langue de la
requête (Accept-Language, lang=), du nœud (réglage, système), textes gardés pour plus tard
(lazy), et ce que l'interface reçoit (GET /api/i18n)."""
import pytest
from fastapi.testclient import TestClient

from aiodos import i18n, settings
from aiodos.api.server import create_app
from aiodos.i18n import catalog, language
from aiodos.i18n.middleware import requested

AUTH = {"X-Aiodos-Token": "jeton"}


@pytest.fixture
def client():
    with TestClient(create_app("jeton", {"testserver"})) as c:
        yield c


def test_parameters_and_forms():
    assert catalog.fill("{n} sur « {name} »", {"n": 2, "name": "PC"}, "fr") == "2 sur « PC »"
    assert catalog.fill("{absent} reste", {}, "fr") == "{absent} reste"
    assert catalog.fill("{{json}} et {n}", {"n": 1}, "en") == "{json} et 1"
    forms = {"one": "{n} image", "other": "{n} images"}
    assert [catalog.fill(forms, {"n": n}, "fr") for n in (0, 1, 2)] == \
        ["0 image", "1 image", "2 images"]
    assert [catalog.fill(forms, {"n": n}, "en") for n in (0, 1, 2)] == \
        ["0 images", "1 image", "2 images"]
    assert catalog.fill(forms, {}, "en") == "{n} images"         # sans n : la forme « other »


def test_t_follows_the_current_language():
    with i18n.using("fr"):
        assert i18n.t("guard.token_invalid") == "Jeton manquant ou invalide."
        assert i18n.t("jobs.cleared", n=1) == "1 tâche retirée."
        assert i18n.number(0.45, 2) == "0,45"
    with i18n.using("en"):
        assert i18n.t("guard.token_invalid") == "Missing or invalid token."
        assert i18n.t("jobs.cleared", n=3) == "3 tasks removed."
        assert i18n.number(0.45, 2) == "0.45"
    assert i18n.t("nexiste.pas") == "nexiste.pas"


def test_accept_language():
    assert language.from_header("fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7") == "fr"
    assert language.from_header("de-DE,de;q=0.9,en;q=0.5,fr;q=0.8") == "fr"
    assert language.from_header("en-GB") == "en"
    assert language.from_header("de-DE,es") is None
    assert language.from_header("fr;q=0,en") == "en"
    assert language.from_header("") is None
    scope = {"type": "http", "query_string": b"t=x&lang=en",
             "headers": [(b"accept-language", b"fr")]}
    assert requested(scope) == "en"                              # lang= passe d'abord
    assert requested({"type": "http", "query_string": b"", "headers": []}) is None


def test_system_language(monkeypatch):
    monkeypatch.setenv("AIODOS_LANGUAGE", "en_US.UTF-8")
    language.forget()
    assert language.node() == "en"
    monkeypatch.setenv("AIODOS_LANGUAGE", "de")         # ni français ni anglais : la vraie
    monkeypatch.setattr(language, "_system_tags", lambda: ["de-DE", "it"])
    monkeypatch.setitem(language._node, "system", None)
    language.forget()
    assert language.node() == "en"                       # l'anglais, faute de mieux
    monkeypatch.setattr(language, "_system_tags", lambda: ["de-DE", "fr-CH"])
    monkeypatch.setitem(language._node, "system", None)
    language.forget()
    assert language.node() == "fr"


def test_the_guard_answers_in_the_language_asked(client):
    fr = client.get("/api/settings", headers={"Accept-Language": "fr-FR,fr;q=0.9"})
    en = client.get("/api/settings", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert fr.status_code == en.status_code == 401
    assert fr.json()["detail"] == "Jeton manquant ou invalide."
    assert en.json()["detail"] == "Missing or invalid token."
    refused = client.get("/api/settings", headers={"Host": "ailleurs:1", "Accept-Language": "en"})
    assert refused.status_code == 400 and refused.text == "Host refused."


def test_interface_texts_follow_settings_then_browser(client):
    answer = client.get("/api/i18n?prefer=de-DE,en-GB,fr", headers=AUTH).json()
    assert answer["lang"] == "en" and answer["setting"] == "auto"
    assert answer["texts"]["nav.settings"] == "Settings"
    assert client.get("/api/i18n?prefer=fr-CA", headers=AUTH).json()["lang"] == "fr"
    assert client.get("/api/i18n?prefer=de", headers=AUTH).json()["lang"] == "en"
    assert client.put("/api/settings", json={"language": "fr"}, headers=AUTH).status_code == 200
    answer = client.get("/api/i18n?prefer=en-US", headers=AUTH).json()
    assert answer["lang"] == "fr" and answer["texts"]["nav.settings"] == "Réglages"
    assert language.node() == "fr"


def test_setting_changes_the_node_language_and_tells_listeners(client, monkeypatch):
    heard = []
    monkeypatch.setattr(language, "_listeners", [heard.append])
    client.put("/api/settings", json={"language": "en"}, headers=AUTH)
    assert language.node() == "en" and heard == ["en"]
    assert i18n.t("guard.host_refused") == "Host refused."          # hors requête : le nœud
    client.put("/api/settings", json={"language": "auto"}, headers=AUTH)
    assert language.node() == "fr" and heard == ["en", "fr"]       # AIODOS_LANGUAGE=fr


def test_the_node_language_follows_the_data_folder(tmp_path, monkeypatch):
    """La ligne de commande parle avant que --data-dir ne soit lu : la langue du nœud ne
    reste pas celle d'un autre dossier (essai du 21/10 : « Nœud AIODOS prêt » en français
    alors que la base demandait l'anglais)."""
    settings.set_value("language", "en")
    mine = str(tmp_path)
    monkeypatch.setenv("AIODOS_DATA_DIR", str(tmp_path / "autre"))
    assert language.node() == "fr"                       # pas de base là : le système
    monkeypatch.setenv("AIODOS_DATA_DIR", mine)
    assert language.node() == "en"                       # sans forget() ni changed()


def test_an_unknown_language_is_not_stored(client):
    client.put("/api/settings", json={"language": "en"}, headers=AUTH)
    client.put("/api/settings", json={"language": "de"}, headers=AUTH)
    assert settings.get("language") == "en" and language.node() == "en"


def test_default_instructions_follow_the_language():
    with i18n.using("en"):
        english = settings.get("system_prompt")
    with i18n.using("fr"):
        french = settings.get("system_prompt")
    assert english.startswith("You are AIODOS") and "English" in english
    assert french.startswith("Tu es AIODOS") and "français" in french
    settings.set_value("system_prompt", "Réponds en latin.")
    with i18n.using("en"):
        assert settings.get("system_prompt") == "Réponds en latin."   # les siennes, telles quelles


def test_the_waiting_window_speaks_the_node_language():
    pytest.importorskip("webview")
    from aiodos.ui import window
    with i18n.using("en"):
        english = window.waiting_page()
        failed = window.waiting_page(i18n.t("window.start_failed", error="port pris"))
    assert 'lang="en"' in english and "AIODOS is starting" in english
    assert "AIODOS could not start: port pris" in failed and "Details: " in failed
    with i18n.using("fr"):
        assert "AIODOS démarre" in window.waiting_page()


def test_lazy_texts():
    kept = i18n.lazy("jobs.cleared", n=2)
    assert kept == ["jobs.cleared", {"n": 2}]
    with i18n.using("en"):
        assert i18n.render(kept) == "2 tasks removed."
    with i18n.using("fr"):
        assert i18n.render(kept) == "2 tâches retirées."
    assert i18n.render("déjà traduit") == "déjà traduit"
    assert i18n.render(["autre.version", {}]) == ""                 # clé inconnue : rien
    # Venu d'ailleurs : seuls des textes et des nombres passent en paramètres
    assert i18n.render(["jobs.remote_on", {"n": {"x": 1}, "name": "PC"}]) == "{n} sur « PC »"


def test_job_titles_are_translated_for_the_reader(client):
    from aiodos.jobs import queue, registry

    def split(params):
        return i18n.lazy("app.paired", name=params["name"]), [], {}
    monkeypatch_kinds = dict(registry.JOB_KINDS)
    registry.register_job_kind("essai_i18n", split)
    try:
        job_id = queue.create_job("essai_i18n", {"name": "PC"})
        job = queue.job_summary(job_id)
        assert job["title"] == "Machine appairée : « PC »"
        assert job["title_t"] == ["app.paired", {"name": "PC"}]
        assert "_title" not in job["result"]
        # Une route ordinaire (fil de Starlette) : la langue de la requête la suit
        listed = client.get("/api/jobs", headers={**AUTH, "Accept-Language": "en"}).json()
        mine = next(j for j in listed["jobs"] if j["id"] == job_id)
        assert mine["title"] == "Machine paired: “PC”"
    finally:
        registry.JOB_KINDS.clear()
        registry.JOB_KINDS.update(monkeypatch_kinds)
