"""Réglages de l'application, stockés dans la base (valeurs JSON).

Une clé d'API (SECRET_KEYS) ne sortirait jamais en clair vers l'interface : public_view()
la remplace par un indicateur « renseignée » et ses 4 derniers caractères. Il n'y en a plus
depuis le retrait du cloud (21/10/2026), mais la clé de la passerelle est protégée de même.
"""
import json
import logging
import os
import socket

from aiodos import db, i18n
from aiodos.node import android, vault

log = logging.getLogger("aiodos")

SECRET_KEYS: set = set()
# Secrets gardés dans la table des réglages mais hors de DEFAULTS (ni lus ni écrits par
# PUT /api/settings) : protégés par DPAPI comme les clés d'API (gateway/access.py les relit)
PROTECTED_ELSEWHERE = ("gateway_key",)

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
    # --- Langue des textes (i18n/) : « auto » (celle du système), « fr » ou « en » ---
    "language": "auto",
    # --- Conversation ---
    "default_model": "qwen3.5:9b",
    "think": True,                    # laisser les modèles « réfléchir » (Qwen3…)
    # Vide : les consignes par défaut, dans la langue en cours (i18n/*/settings.json)
    "system_prompt": "",
    "ollama_url": "http://127.0.0.1:11434",
    # Moteur des modèles (services/ollama.py) : « auto » = le moteur intégré (llama.cpp) dès
    # qu'il est là, sinon Ollama ; « integrated » ; « ollama » (décision du 20/10/2026)
    "llm_engine": "auto",
    # Mode automatique (network/placement.py) : mes machines seulement, sauf si l'on
    # accepte que la conversation passe aussi chez un ami le temps de la réponse
    "auto_route_friends": False,
    # --- Connaissances ---
    "embed_model": "embeddinggemma",  # choix mesuré : docs/embeddings.md
    "rag_min_score": 0.40,            # pertinence minimale d'un extrait (suit le modèle)
    "knowledge_enabled": True,
    "conversations_enabled": True,    # mémoire inter-conversations
    "peer_libraries_enabled": True,   # chercher aussi chez mes machines et mes amis (la question part)
    # --- Web ---
    "web_search_enabled": True,       # cherche quand la question le demande, après accord
    "web_results": 3,
    # --- Médias dans la base de connaissance ---
    "media_vision": True,
    "media_vision_mode": "both",      # ocr | describe | both
    "vision_model": "qwen2.5vl:7b",
    "media_transcribe": True,
    "whisper_model": "small",
    # --- Ce qui fait calculer mes machines ensemble est activé dès la première ouverture
    # (décision de l'auteur, 20/10/2026 : « le logiciel doit fonctionner dès la première
    # ouverture » ; on peut tout couper ensuite). Le réseau public, le prêt d'images aux amis
    # et l'accès des applications de ce PC aussi depuis le 27/10/2026 (« des personnes qui ne
    # connaissent pas AIODOS ne vont pas automatiquement aller dans les réglages »). Cf.
    # FOR_MY_MACHINES et OPEN_FROM_THE_START plus bas.
    # --- Réseau local (phase 1) ---
    "network_enabled": True,
    "p2p_port": 47701,                # échanges chiffrés entre nœuds
    "discovery_port": 47700,          # annonces UDP sur le réseau local
    "manual_peers": [],               # adresses ajoutées à la main (« ip:port »)
    # --- Internet (phase 2) : compagnon aiodos-net ---
    "internet_enabled": True,         # relier mes machines appairées par Internet
    "internet_relays": ["n0"],        # « n0 » (relais publics de n0), « none », ou https://…
    "internet_directory": ["n0-dns", "dht"],   # DNS de n0 d'abord, DHT Mainline en secours
    "internet_portmap": True,         # ouverture de port automatique sur la box (UPnP, PCP…)
    # --- Réseau public (phase 3.0) : un annuaire, activé par défaut ; les conversations
    # d'inconnus calculées ici : lend_public, plus bas ---
    "public_network": True,           # « Rejoindre le réseau public » (Internet requis)
    "p2p_route": "auto",              # chemin vers mes machines : auto | lan | internet
    "internet_max_blob_mb": 64,       # fichier confié par Internet : au-delà, il reste au local
    # --- Nœud & partage de puissance ---
    "node_name": "",
    "offload_enabled": True,          # confier des unités à mes machines appairées
    "share_enabled": True,            # calculer pour mes machines appairées
    "lend_models": True,              # répondre aux conversations de mes machines appairées
    # « Prêter ma puissance aux conversations d'inconnus » (network/public_lending.py,
    # 28/10/2026) : après mes machines et mes amis, jamais sur batterie ; activé par défaut
    "lend_public": True,
    # « Faire calculer mes conversations par des inconnus » (network/public_chat.py,
    # chat/public_consent.py, 28/10/2026) : en mode automatique, une machine du réseau public
    # bien plus rapide, ou qui a le modèle quand les miennes ne l'ont pas ; activé par défaut
    "use_public": True,
    # Accords demandés une fois, dans une conversation (None : pas encore demandé, puis vrai ou
    # faux ; se changent dans les Réglages) : un modèle plus gros d'une machine du réseau public
    # (qwen3.5:9b plutôt que 0.8b), et les passages de mes documents envoyés avec la conversation
    "public_bigger_model": None,
    "public_documents": None,
    "public_warned": False,           # la note « ta conversation part chez un inconnu » vue
    # Seulement quand le PC n'est pas utilisé, comme l'économiseur d'écran de SETI@home.
    # Désactivé (par défaut depuis le 17/10/2026) : le prêt continue pendant l'utilisation,
    # en priorité basse, jamais sur batterie (network/worker.py, docs/arriere-plan.md)
    "share_when_idle": False,
    "idle_minutes": 10,
    "share_on_ac_only": True,         # téléphone : seulement en charge
    "share_min_battery": 80,          # téléphone : charge minimale pour calculer (%)
    "share_max_temp": 40,             # téléphone : température maximale de la batterie (°C)
    # « Puissance prêtée » (node/places.py) : jamais toute la machine (décision de l'auteur,
    # 27/10/2026). Fixe les places de calcul et la part du temps de la carte graphique ;
    # remplace « GPU prêté au maximum » (share_max_gpu, 100 %), repris s'il était plus bas
    "lend_power": 60,
    # --- Travail de fond (jobs/pacing.py, docs/arriere-plan.md) ---
    # Toute la file en pause (jobs/queue.py) : « » non, « after_current » (les unités en
    # cours finissent), « now » (elles s'arrêtent, rendues sans compter d'essai)
    "queue_pause": "",
    "battery_mode": "eco",            # sur batterie : eco (ménagé) | pause | normal
    "close_to_tray": True,            # Windows : fermer la fenêtre la cache (ui/tray.py)
    # --- Gros modèles répartis entre mes machines (docs/gros-modeles.md) ---
    # Prêter ma carte graphique (llama.cpp RPC) : à mes machines seulement, jamais à un ami ni
    # au réseau public, ni depuis l'appli Android (cluster/helper.lend ; node/identity ne
    # l'annonce pas quand le prêt serait refusé). Reste activé après l'audit du 20/10/2026 : le
    # compagnon tient le serveur RPC prêté et n'y laisse servir aucun autre programme de la
    # machine (compagnon/src/hold.rs), sauf dans l'instant qui suit son démarrage ; et ce
    # serveur, sans défense contre un client malveillant, ne sert que mes machines (§ 4 de
    # docs/gros-modeles.md).
    "lend_gpu": True,
    "lend_gpu_device": "",            # carte prêtée (« Vulkan1 », « CUDA0 »…) ; vide : la meilleure
    "llamacpp_dir": "",               # dossier de llama.cpp ; vide : celui livré avec les versions
    # --- Images réparties entre mes machines (imaging/, docs/generation-repartie.md) ---
    "sdcpp_dir": "",                  # dossier de stable-diffusion.cpp ; vide : data/engines
    "images_models_dir": "",          # dossier des modèles d'images ; vide : data/images/models
    "images_device": "",              # carte qui calcule (« Vulkan1 », « CUDA0 »…) ; vide : la meilleure
    "images_other_dirs": "",          # autres dossiers de modèles sur ce PC (imaging/local_copies.py)
    "images_out_dir": "",             # dossier des images créées ; vide : Images\AIODOS (installé)
    "lend_images_friends": True,      # calculer les images de mes amis (jobs/kinds/images.py)
    "images_from_friends": False,     # faire calculer mes images par mes amis qui le proposent
    "images_enrich": True,            # enrichir la description avant de créer (chat/enrich.py)
    # --- Mises à jour : versions signées par l'auteur, reçues de mes machines appairées ---
    "auto_update": True,
    # --- Charte et conditions d'utilisation (aiodos/legal) : dernière version acceptée ---
    "terms_accepted": 0,
    # --- AIODOS ouvert aux autres outils (gateway/, docs/autres-outils.md) : API compatible
    # OpenAI et Ollama, serveur MCP, sur 127.0.0.1 seulement ; désactivé par défaut ---
    "gateway_enabled": False,
    "gateway_port": 11435,            # fixe (celui d'Ollama + 1), réglable
    # --- Applications (gateway/apps.py, docs/api-applications.md) : une clé par appli,
    # accordée dans AIODOS. Ouvert par défaut depuis le 27/10/2026 (avant : fermé jusqu'au
    # premier accord) : chaque appli reste à accepter, sur 127.0.0.1 seulement ---
    "gateway_apps": True,             # « Ouvrir AIODOS aux applications de ce PC »
    "gateway_requests": True,         # « Accepter les nouvelles demandes d'accès »
    "gateway_same_account": True,     # refuser les applis d'un autre compte Windows
    "gateway_log_days": 30,           # journal des applis : 30 jours (compteurs : 90)
    # --- Contexte des modèles de conversation du moteur intégré (llm/context.py) : 0 =
    # selon la mémoire vidéo (16 384 jetons si elle le permet, sinon 8 192) ---
    "chat_context": 0,
    "chat_context_overrides": {},     # {"modèle": jetons} : exceptions par modèle
    # --- Moteurs des applications (engines/, docs/conception/partage-puissance.md § 3.2) :
    # AIODOS rejoue les travaux des applis dans le ComfyUI de ce PC seulement après le « oui »
    # de la carte de détection ; vides : trouvés seuls (Desktop, version portable) ---
    "engines_comfyui_enabled": False,
    "engines_comfyui_path": "",
    "engines_comfyui_url": "",
    # Lancer ComfyUI sans fenêtre quand il n'est pas ouvert et qu'un travail attend
    # (engines/comfy_instance.py, livraison L2c) : sur demande de l'utilisateur
    "engines_comfyui_launch": False,
    "keep_awake_jobs": True,          # sur secteur, pas de veille pendant un travail d'appli
}


def machine_name() -> str:
    """Nom par défaut du nœud : celui de la machine. Sous Android, qui répond
    « localhost », le modèle du téléphone."""
    name = socket.gethostname()
    if name == "localhost" and android.is_android():
        return android.model() or i18n.t("settings.android_phone")
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


# Activés par défaut depuis le 20/10/2026 ; AIODOS_QUIET=1 (essais automatiques : nœuds sans
# réseau ni compagnon tant qu'un test ne les demande pas) les garde éteints.
FOR_MY_MACHINES = ("network_enabled", "internet_enabled", "share_enabled", "lend_models",
                   "lend_gpu")
# Activés par défaut depuis le 27/10/2026 (décision de l'auteur), éteints de même en essai ;
# le prêt aux conversations d'inconnus, et les miennes calculées par eux, depuis le 28/10/2026
OPEN_FROM_THE_START = ("public_network", "lend_images_friends", "gateway_apps", "lend_public",
                       "use_public")
# Réglages à trois états : None (pas encore demandé), vrai ou faux ; toute autre valeur est
# ignorée à l'enregistrement
ASKED_ONCE = ("public_bigger_model", "public_documents")


def default(key):
    if key in FOR_MY_MACHINES + OPEN_FROM_THE_START and os.environ.get("AIODOS_QUIET") == "1":
        return False
    if key == "system_prompt":
        return i18n.t("settings.default_system_prompt")
    return DEFAULTS.get(key)


def get(key):
    row = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        if key == "node_name":
            return machine_name()
        if key == "rag_min_score":
            return rag_threshold(get("embed_model"))
        return default(key)
    value = _decode(row["value"])
    return _open(key, value) if key in SECRET_KEYS else value


def _open(key, value):
    """Une clé d'API protégée (node/vault.py), en clair ; illisible ici : à saisir de nouveau."""
    try:
        return vault.unprotect_text(value)
    except vault.Unreadable as exc:
        log.warning("Réglage %s illisible ici (%s) : à saisir de nouveau", key, exc)
        return ""


def get_all() -> dict:
    values = {key: default(key) for key in DEFAULTS}
    values["node_name"] = machine_name()
    stored = set()
    for row in db.query("SELECT key, value FROM settings"):
        if row["key"] in DEFAULTS:
            value = _decode(row["value"])
            values[row["key"]] = _open(row["key"], value) if row["key"] in SECRET_KEYS else value
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
            if key == "language" and value not in ("auto", *i18n.LANGUAGES):
                continue                  # une langue qu'AIODOS ne parle pas : rien ne change
            if key in ASKED_ONCE and value not in (None, True, False):
                continue
            if key in SECRET_KEYS:
                if value is None:
                    value = ""
                elif not str(value).strip():
                    continue
                else:
                    value = vault.protect_text(str(value).strip())
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))
    if "language" in values:
        i18n.language.changed()


def protect_secrets() -> int:
    """Protège les clés d'API enregistrées avant DPAPI, et la clé générale de la
    passerelle (gardée hors des réglages ordinaires, PROTECTED_ELSEWHERE). → nombre de clés
    protégées."""
    done = 0
    for key in sorted(SECRET_KEYS) + list(PROTECTED_ELSEWHERE):
        row = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
        clear = _decode(row["value"]) if row else None
        if not isinstance(clear, str) or not clear or vault.is_protected(clear):
            continue
        sealed = vault.protect_text(clear)
        if sealed == clear or not vault.verify_text(sealed, clear):
            continue
        done += db.execute("UPDATE settings SET value=? WHERE key=? AND value=?",
                           (json.dumps(sealed), key, row["value"])).rowcount
    return done


def public_view() -> dict:
    values = get_all()
    for key in SECRET_KEYS:
        secret = values.get(key) or ""
        values[key] = ""
        values[key + "_set"] = bool(secret)
        values[key + "_hint"] = ("••••" + secret[-4:]) if len(secret) >= 8 else ""
    return values
