"""Réglages de l'application, stockés dans la base (valeurs JSON).

Les clés API ne sortent jamais en clair vers l'interface : public_view() les
remplace par un indicateur « renseignée » et les 4 derniers caractères.
"""
import json
import socket

from coeur import db

SECRET_KEYS = {"mistral_key", "qwen_key"}

DEFAULTS = {
    # --- Conversation ---
    "default_model": "",
    "think": True,                    # laisser les modèles « réfléchir » (Qwen3…)
    "system_prompt": (
        "Tu es Cœur IA, un assistant local qui aide à coder, à réfléchir et à "
        "travailler. Réponds en français, de façon claire et structurée. "
        "Quand tu écris du code, utilise des blocs markdown avec le nom du langage."
    ),
    "ollama_url": "http://127.0.0.1:11434",
    # --- Connaissances ---
    "embed_model": "nomic-embed-text",
    "rag_min_score": 0.70,            # pertinence minimale d'un extrait (calibré sur nomic)
    "knowledge_enabled": True,
    "conversations_enabled": False,   # mémoire inter-conversations
    # --- Web & cloud ---
    "web_search_enabled": False,
    "web_results": 3,
    "cloud_confirm": True,
    "mistral_key": "",
    "qwen_key": "",
    "mistral_cloud_model": "mistral-large-latest",
    "qwen_cloud_model": "qwen-max",
    # --- Médias dans la base de connaissance ---
    "media_vision": True,
    "media_vision_mode": "both",      # ocr | describe | both
    "vision_model": "qwen2.5vl:7b",
    "media_transcribe": True,
    "whisper_model": "small",
    # --- Réseau local (phase 1) : désactivé tant que l'utilisateur ne l'active pas ---
    "network_enabled": False,
    "p2p_port": 47701,                # échanges chiffrés entre nœuds
    "discovery_port": 47700,          # annonces UDP sur le réseau local
    "manual_peers": [],               # adresses ajoutées à la main (« ip:port »)
    # --- Nœud & partage de puissance ---
    "node_name": "",
    "offload_enabled": True,          # confier des unités à mes machines appairées
    "share_enabled": False,           # calculer pour mes machines appairées
    "share_when_idle": True,          # comme l'économiseur d'écran de SETI@home
    "idle_minutes": 10,
    "share_on_ac_only": True,
    "share_max_gpu": 100,
}


def _decode(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def get(key):
    row = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        if key == "node_name":
            return socket.gethostname()
        return DEFAULTS.get(key)
    return _decode(row["value"])


def get_all() -> dict:
    values = dict(DEFAULTS)
    values["node_name"] = socket.gethostname()
    for row in db.query("SELECT key, value FROM settings"):
        if row["key"] in DEFAULTS:
            values[row["key"]] = _decode(row["value"])
    return values


def set_value(key, value):
    set_many({key: value})


def set_many(values: dict):
    """Enregistre les réglages connus. Pour une clé API : None l'efface,
    une chaîne vide la laisse inchangée (l'interface ne la connaît pas)."""
    with db.transaction() as conn:
        for key, value in values.items():
            if key not in DEFAULTS:
                continue
            if key in SECRET_KEYS:
                if value is None:
                    value = ""
                elif not str(value).strip():
                    continue
                else:
                    value = str(value).strip()
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))


def public_view() -> dict:
    values = get_all()
    for key in SECRET_KEYS:
        secret = values.get(key) or ""
        values[key] = ""
        values[key + "_set"] = bool(secret)
        values[key + "_hint"] = ("••••" + secret[-4:]) if len(secret) >= 8 else ""
    return values
