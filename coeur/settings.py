"""Réglages de l'application, stockés dans la base (valeurs JSON).

Les clés API ne sortent jamais en clair vers l'interface : public_view() les
remplace par un indicateur « renseignée » et les 4 derniers caractères.
"""
import json
import socket

from coeur import db
from coeur.node import android

SECRET_KEYS = {"mistral_key", "qwen_key"}

# Seuil de pertinence mesuré pour chaque modèle d'embedding sur un jeu de test en
# français (docs/embeddings.md) : chaque modèle a sa propre échelle de scores.
# Tant que l'utilisateur ne fixe pas rag_min_score, le seuil suit le modèle choisi.
RAG_THRESHOLDS = {
    "embeddinggemma": 0.40,
    "bge-m3": 0.45,
    "snowflake-arctic-embed2": 0.29,
    "qwen3-embedding:0.6b": 0.42,
    "nomic-embed-text": 0.70,         # aucun seuil ne lui convient en français
}

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
    "embed_model": "embeddinggemma",  # choix mesuré : docs/embeddings.md
    "rag_min_score": 0.40,            # pertinence minimale d'un extrait (suit le modèle)
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
    "lend_models": False,             # répondre aux conversations de mes machines appairées
    "share_when_idle": True,          # comme l'économiseur d'écran de SETI@home
    "idle_minutes": 10,
    "share_on_ac_only": True,         # téléphone : seulement en charge
    "share_min_battery": 80,          # téléphone : charge minimale pour calculer (%)
    "share_max_temp": 40,             # téléphone : température maximale de la batterie (°C)
    "share_max_gpu": 100,
}


def machine_name() -> str:
    """Nom par défaut du nœud : celui de la machine. Sous Android, qui répond
    « localhost », le modèle du téléphone."""
    name = socket.gethostname()
    if name == "localhost" and android.is_android():
        return android.model() or "Téléphone Android"
    return name


def _decode(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def rag_threshold(model) -> float:
    """Seuil de pertinence mesuré pour ce modèle (« bge-m3:latest » → « bge-m3 ») ;
    pour un modèle non mesuré, celui du modèle par défaut."""
    name = str(model or "").strip()
    for candidate in (name, name.split(":")[0]):
        if candidate in RAG_THRESHOLDS:
            return RAG_THRESHOLDS[candidate]
    return DEFAULTS["rag_min_score"]


def get(key):
    row = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        if key == "node_name":
            return machine_name()
        if key == "rag_min_score":
            return rag_threshold(get("embed_model"))
        return DEFAULTS.get(key)
    return _decode(row["value"])


def get_all() -> dict:
    values = dict(DEFAULTS)
    values["node_name"] = machine_name()
    stored = set()
    for row in db.query("SELECT key, value FROM settings"):
        if row["key"] in DEFAULTS:
            values[row["key"]] = _decode(row["value"])
            stored.add(row["key"])
    if "rag_min_score" not in stored:
        values["rag_min_score"] = rag_threshold(values["embed_model"])
    return values


def set_value(key, value):
    set_many({key: value})


def set_many(values: dict):
    """Enregistre les réglages connus. Pour une clé API : None l'efface,
    une chaîne vide la laisse inchangée (l'interface ne la connaît pas).

    Un nouveau modèle d'embedding ramène le seuil de pertinence à la valeur
    mesurée pour lui, sauf si le seuil est fixé dans le même envoi ; laissé
    vide, le modèle revient à celui par défaut."""
    with db.transaction() as conn:
        if "embed_model" in values:
            model = str(values["embed_model"] or "").strip() or DEFAULTS["embed_model"]
            if model != get("embed_model") and "rag_min_score" not in values:
                conn.execute("DELETE FROM settings WHERE key='rag_min_score'")
            values = dict(values, embed_model=model)
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
