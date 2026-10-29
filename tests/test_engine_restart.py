"""Le routeur du moteur intégré qui repart pendant une indexation (PC de l'auteur, 29/10/2026 :
une dizaine de départs, chacun coupant la demande en cours et rechargeant le modèle de vision).
Sa demande coupée ne compte pas d'échec ; une lecture manquée de la mémoire vidéo ne change
plus le contexte (donc ni les préréglages, ni le routeur)."""
import requests

from aiodos.jobs import executor
from aiodos.llm import context, engine, models as registry
from aiodos.services import model_install, ollama


def _cut(url):
    return requests.ConnectionError("Connection aborted.",
                                    request=requests.Request("POST", url).prepare())


def test_a_request_cut_by_the_router_restarting_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(ollama, "use_integrated", lambda: True)
    assert executor.engine_down(_cut("http://127.0.0.1:49400/v1/chat/completions"))
    assert executor.engine_down(_cut("http://127.0.0.1:49812/v1/embeddings"))
    assert not executor.engine_down(_cut("http://127.0.0.1:8080/wiki/page"))   # une page locale
    assert not executor.engine_down(_cut("https://site-introuvable.example/v1/x"))
    monkeypatch.setattr(ollama, "use_integrated", lambda: False)
    assert not executor.engine_down(_cut("http://127.0.0.1:49400/v1/chat/completions"))


def test_a_missed_vram_reading_keeps_the_context(monkeypatch):
    readings = iter([8 * 1024 ** 3, 0, 0, 8 * 1024 ** 3])     # nvidia-smi muet deux fois
    monkeypatch.setattr(model_install, "device", lambda: {"vram": next(readings)})
    monkeypatch.setattr(context, "_seen", {"vram": 0})
    monkeypatch.setattr(context.android, "is_android", lambda: False)
    found = [context.auto() for _ in range(4)]
    assert found == [context.HIGH] * 4


def test_the_presets_do_not_change_when_the_vram_reading_is_missed(monkeypatch, tmp_path):
    weights = tmp_path / "poids.gguf"
    weights.write_bytes(b"GGUF")
    model = registry.Model(name="qwen3.5:9b", id="qwen3.5-9b", path=weights, mmproj=None,
                           embedding=False, size=1, digest="", family="", parameters="",
                           quantization="", template="", source="ollama")
    readings = iter([8 * 1024 ** 3, 0])
    monkeypatch.setattr(model_install, "device", lambda: {"vram": next(readings)})
    monkeypatch.setattr(context, "_seen", {"vram": 0})
    monkeypatch.setattr(context.android, "is_android", lambda: False)
    assert engine.presets([model]) == engine.presets([model])
