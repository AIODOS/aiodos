"""Configuration globale de Cœur IA — lecture/écriture de data/config.json."""
import json
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"

ASSETS_DIR = APP_ROOT / "assets"
LOGO_PNG = ASSETS_DIR / "logo.png"
LOGO_ICO = ASSETS_DIR / "logo.ico"

DEFAULTS = {
    "default_model": "",                 # rempli au 1er lancement avec un modèle dispo
    "embed_model": "nomic-embed-text",   # modèle d'embeddings pour la base de connaissance
    "knowledge_enabled": True,
    "conversations_enabled": False,      # utiliser le contenu de toutes les conversations
    "system_prompt": (
        "Tu es Cœur IA, un assistant local qui aide à coder, à réfléchir et à "
        "travailler. Réponds en français, de façon claire et structurée. "
        "Quand tu écris du code, utilise des blocs markdown avec le nom du langage."
    ),
    "ollama_url": "http://localhost:11434",
    # --- Cloud (recherche web + API Mistral/Qwen) ---
    "web_search_enabled": False,
    "web_results": 3,                    # nb de pages web lues par recherche
    "cloud_confirm": True,               # demander validation avant tout envoi cloud
    "mistral_key": "",
    "qwen_key": "",
    "mistral_cloud_model": "mistral-large-latest",
    "qwen_cloud_model": "qwen-max",
    # --- Médias (images / audio / vidéo dans la base de connaissance) ---
    "media_vision": True,                # OCR + description des images via Ollama
    "media_vision_mode": "both",         # "ocr" | "describe" | "both"
    "vision_model": "qwen2.5vl:7b",
    "media_transcribe": True,            # transcription audio/vidéo via Whisper
    "whisper_model": "small",
}


def _ensure():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load():
    _ensure()
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    return {**DEFAULTS, **data}


def save(cfg):
    _ensure()
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def get(key):
    return load().get(key, DEFAULTS.get(key))


def set_value(key, value):
    cfg = load()
    cfg[key] = value
    save(cfg)
