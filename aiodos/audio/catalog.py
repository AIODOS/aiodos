"""Modèles d'audio connus d'AIODOS, par genre : voix (lire un texte), musique, effets sonores
(docs/conception/audio.md § 2). Tous tournent avec audio.cpp (audio/engine.py).

Chaque fichier est épinglé ici par son chemin, sa taille et son empreinte SHA-256, avec son
adresse sur Hugging Face à un commit précis : le téléchargement est vérifié, et deux machines
qui ont les mêmes empreintes calculent avec exactement le même modèle (unités confiées,
jobs/kinds/audio.py). Le chemin garde le dossier du paquet d'audio.cpp (« PocketTTS-GGUF/
french/… ») : les voix de PocketTTS doivent être à côté de son GGUF, dans « embeddings », et
MiniMax Music 3 est un dossier entier. Un fichier commun à deux modèles (MiDashengLM-Gen, pour
la musique et pour les effets) a le même chemin et la même empreinte : téléchargé une fois.
Tailles et empreintes relevées par l'API de Hugging Face le 29/10/2026 (champ « lfs.oid » ;
les petits fichiers de configuration, lus et hachés), dépôts publics, sans compte.

Chaque modèle a son genre (genre), son rang dans le genre (rank, 1 : le plus puissant) : le
classement d'Artificial Analysis quand il y est (elo), sinon une estimation ; la mémoire de
carte graphique qu'il demande (vram, Mio, estimée sauf mention) ; pour une voix, les langues
qu'il parle et ses voix par langue (la première : celle par défaut) ; pour la musique et les
effets, les durées proposées et la plus longue permise. Une licence ni Apache ni MIT ni CC BY a
un avertissement (notice), lu et accepté avant le téléchargement (décision de l'auteur du
29/10/2026, comme les images) ; la liste des licences acceptées est celle des images
(réglage « licenses_accepted », imaging/catalog.accept).

Les textes (description, avertissement, noms des voix) sont des clés de
i18n/*/audio_models.json, écrites dans la langue en cours ; le nom d'un modèle reste tel quel.
"""
from aiodos import i18n, settings
from aiodos.i18n import t

HF = "https://huggingface.co/{repo}/resolve/{commit}/{path}"
# Les paquets publiés par les auteurs d'audio.cpp, au commit du 27/10/2026
GGUF = ("audio-cpp/audio.cpp-gguf", "874fcb41ececda20bcbc0782d6b4ea7f7d069ea8")
MUSIC3 = ("audio-cpp/MiniMax-Music3-GGUF", "46fe77f8bb395ccdbe797d784225f7b84866e7ce")

VOICE, MUSIC, SOUND = "voice", "music", "sound"
GENRES = (VOICE, MUSIC, SOUND)
MAX_TEXT = 2000                   # un texte à lire, une description, des paroles


def _file(source: tuple, path: str, size: int, sha256: str, folder: str = "") -> dict:
    """Un fichier épinglé : son chemin ici (celui du dépôt, sous folder), sa taille, son
    empreinte, son adresse au commit."""
    repo, commit = source
    return {"path": f"{folder}{path}", "size": size, "sha256": sha256,
            "url": HF.format(repo=repo, commit=commit, path=path)}


def _music3(path: str, size: int, sha256: str) -> dict:
    return _file(MUSIC3, path, size, sha256, folder="MiniMax-Music3-GGUF/")


# MiDashengLM-Gen : la musique et les effets sonores (un seul fichier pour les deux)
MIDASHENG = _file(GGUF, "MiDashengLM-Gen-GGUF/midashenglm-gen-q8_0.gguf", 3381370752,
                  "1b0ec63a3e1c1711ee7ed26b6ea7ee2b1f013b61d751420c8c88d1d377e34677")
# Les durées proposées d'une musique et d'un effet, en secondes
MUSIC_SECONDS = [10, 20, 30, 60]
SOUND_SECONDS = [3, 5, 10, 20]
# MiDashengLM-Gen s'arrête à 10 s (essai du 29/10/2026 : 20 s demandées, 10,0 s rendues)
MIDASHENG_SECONDS = [3, 5, 10]

MODELS = {
    # ------------------------------------------------------------------ voix
    # Voxtral 4B TTS (Mistral, 03/2026), Elo 1090 au classement des voix d'Artificial
    # Analysis (poids ouverts, lu le 29/10/2026) : le premier qui parle français. Licence
    # CC BY-NC 4.0 (celle de ses voix de référence) ; 20 voix intégrées
    "voxtral-4b-tts": {
        "label": "Voxtral 4B TTS", "genre": VOICE, "family": "voxtral_tts", "task": "tts",
        "license": "CC BY-NC 4.0", "notice": "audio_models.notice_voxtral",
        "about": "audio_models.about_voxtral",
        "files": {"model": _file(GGUF, "Voxtral-4B-TTS-2603-GGUF/voxtral-4b-tts-2603-q8_0.gguf",
                                 4722918208,
                                 "46eca2bfc66c89c415b66995d3544e91dea3320c93765ec0596f5c616354972d")},
        "rank": 1, "elo": 1090, "vram": 8192,
        "languages": ["fr", "en", "es", "de", "it", "pt", "nl", "ar", "hi"],
        "voices": {"fr": [["fr_female", "audio_models.voice_female"],
                          ["fr_male", "audio_models.voice_male"]],
                   "en": [["casual_female", "audio_models.voice_female"],
                          ["casual_male", "audio_models.voice_male"],
                          ["cheerful_female", "audio_models.voice_cheerful"]],
                   "es": [["es_female", "audio_models.voice_female"],
                          ["es_male", "audio_models.voice_male"]],
                   "de": [["de_female", "audio_models.voice_female"],
                          ["de_male", "audio_models.voice_male"]],
                   "it": [["it_female", "audio_models.voice_female"],
                          ["it_male", "audio_models.voice_male"]],
                   "pt": [["pt_female", "audio_models.voice_female"],
                          ["pt_male", "audio_models.voice_male"]],
                   "nl": [["nl_female", "audio_models.voice_female"],
                          ["nl_male", "audio_models.voice_male"]],
                   "ar": [["ar_male", "audio_models.voice_male"]],
                   "hi": [["hi_female", "audio_models.voice_female"],
                          ["hi_male", "audio_models.voice_male"]]},
        "voice_option": "request",          # --request-option voice_id=…
    },
    # Qwen3-TTS 1.7B CustomVoice (Alibaba, 01/2026), Apache-2.0 : dix langues dont le
    # français, neuf voix intégrées. Pas au classement : rang estimé
    "qwen3-tts-1.7b": {
        "label": "Qwen3-TTS 1.7B", "genre": VOICE, "family": "qwen3_tts", "task": "tts",
        "license": "Apache-2.0", "about": "audio_models.about_qwen3_tts",
        "files": {"model": _file(GGUF, "Qwen3-TTS-12Hz-1.7B-CustomVoice-GGUF/"
                                 "qwen3-tts-12hz-1.7b-customvoice-q8_0.gguf", 2817055200,
                                 "c8ac3f94b6e73f137c0e7ecc9270bc8faec7a476ff799b87e48844d1d0987ac3")},
        "rank": 2, "elo": None, "vram": 6144,
        "languages": ["fr", "en", "zh", "ja", "ko", "de", "ru", "pt", "es", "it"],
        # Les mêmes voix dans toutes les langues
        "voices": {"*": [["Serena", "audio_models.voice_female"],
                         ["Ryan", "audio_models.voice_male"],
                         ["Vivian", "audio_models.voice_cheerful"],
                         ["Eric", "audio_models.voice_deep"]]},
        "voice_option": "speaker",          # --speaker …
    },
    # PocketTTS (Kyutai), 100 millions de paramètres, CC BY 4.0 : un paquet par langue, ses
    # voix à côté (embeddings/<voix>.safetensors). Très léger : tourne aussi sur le processeur
    "pocket-tts-fr": {
        "label": "PocketTTS (FR)", "genre": VOICE, "family": "pocket_tts", "task": "tts",
        "license": "CC BY 4.0", "about": "audio_models.about_pocket_tts",
        "files": {"model": _file(GGUF, "PocketTTS-GGUF/french/pocket-tts-french-q8_0.gguf",
                                 128117408,
                                 "ae7e6d536c20245d216a7ec9404124cf67f913380bf41a6380c55d6ca9dbaef5"),
                  "voice_cosette": _file(GGUF, "PocketTTS-GGUF/french/embeddings/cosette.safetensors",
                                         6244152,
                                         "e3c3d60c8c494592f03b2e9b1e963a90b6953229b78d6f6889898066129ebc89"),
                  "voice_jean": _file(GGUF, "PocketTTS-GGUF/french/embeddings/jean.safetensors",
                                      6096696,
                                      "698dd19a1a4e0ab59f7a82d245caa3130390d1d1d09d49d134c036069b2aceef")},
        "rank": 3, "elo": None, "vram": 1024, "languages": ["fr"],
        "voices": {"fr": [["cosette", "audio_models.voice_female"],
                          ["jean", "audio_models.voice_male"]]},
        "voice_option": "id",               # --voice-id …
    },
    "pocket-tts-en": {
        "label": "PocketTTS (EN)", "genre": VOICE, "family": "pocket_tts", "task": "tts",
        "license": "CC BY 4.0", "about": "audio_models.about_pocket_tts",
        "files": {"model": _file(GGUF, "PocketTTS-GGUF/english/pocket-tts-english-q8_0.gguf",
                                 127867968,
                                 "3f71cf6a51f93c2f2e554e98970a3f49fb9a98d0c64bed5a815e65be3d2ff588"),
                  "voice_alba": _file(GGUF, "PocketTTS-GGUF/english/embeddings/alba.safetensors",
                                      6194424,
                                      "69c32db63ca56843d994f81f343f62e0bf2d73f7e4c9bc73e44bb1110b1d8845"),
                  "voice_jean": _file(GGUF, "PocketTTS-GGUF/english/embeddings/jean.safetensors",
                                      6194424,
                                      "90be4b8f50bb4d2dbe27e3fb4e31417cf6a57928931f0a60426a1748821a3d12")},
        "rank": 3, "elo": None, "vram": 1024, "languages": ["en"],
        "voices": {"en": [["alba", "audio_models.voice_female"],
                          ["jean", "audio_models.voice_male"]]},
        "voice_option": "id",
    },
    # ------------------------------------------------------------------ musique
    # MiniMax Music 3 (08/2026) : le premier des modèles ouverts au classement de la musique
    # d'Artificial Analysis. Chansons avec paroles. Licence communautaire : son nom affiché,
    # autorisation au-delà de 20 M$ de chiffre d'affaires, contenus publiés signalés comme
    # générés. « Expérimental » dans audio.cpp 0.9.3 ; pas encore essayé ici (8 Go)
    "minimax-music-3": {
        "label": "MiniMax Music 3", "genre": MUSIC, "family": "minimax_music3", "task": "gen",
        "license": "MiniMax-Music3 Community License", "notice": "audio_models.notice_music3",
        "about": "audio_models.about_music3",
        "files": {"config": _music3("config.json", 107,
                                    "1ffcff67d916d5519d1ef9a36853232ee6bcb7e14a936623c4e85559f5dce106"),
                  "config_lm": _music3("config/language_model.json", 1596,
                                       "34ded58fcd5e8181557f1417e76af9f3e95109a5ce113a4771fd16f8625b53c0"),
                  "config_rvq": _music3("config/rvq_depth_decoder.json", 274,
                                        "b38c7f72d4b2fa8667442071f9e4a63ca5a7b5d0859e5e64ee7f79c4bc64520c"),
                  "config_condition": _music3("config/condition_encoder.json", 292,
                                              "1cdea3f36506719128ae4a29cdfff74b1134e607eb4a504ce6d810568fac8263"),
                  "config_transformer": _music3("config/transformer.json", 294,
                                                "f60856be934127a8b8223510aff2e38a9efd8bbdbc230c3d3339b96e2cb12f00"),
                  "config_vocoder": _music3("config/vocoder.json", 251,
                                            "4161afd07ba7d5b949e29c8a7a005592259fba73e9110b6ece9dfd38d3489eac"),
                  "tokenizer": _music3("tokenizer/tokenizer.json", 11423801,
                                       "b1537fa9e59a537276ecbc2e12d0438edff635a1f4a4948e679774b5feb3e610"),
                  "tokenizer_config": _music3("tokenizer/tokenizer_config.json", 377,
                                              "1689852cc9c45010de040c8302a8acdc0d2c4c6c740dd7e9dd0a8c704e16eada"),
                  "language_model": _music3("language_model_q4_0.gguf", 6006866496,
                                            "6f621dd636320403c03e9f755b3e2047f5754d055e0fcc6c0c444ae52ffbfa90"),
                  "rvq": _music3("rvq_depth_decoder_q8_0.gguf", 714028960,
                                 "d5b7495fb1a7845abfeab13df829510de442fd4ea0fd55467fe9895fe8ec5db6"),
                  "condition": _music3("condition_encoder.gguf", 100678368,
                                       "ae4be8b6154645b0717d1928f5c195359bdee8fd025099c49954ed302257cd20"),
                  "transformer": _music3("transformer_q4_0.gguf", 1396392768,
                                         "18d3b2461a2988f59e197ccf05dc3931663099290b42443baa2a30e4fd15e506"),
                  "vocoder": _music3("vocoder.gguf", 216705376,
                                     "5d40e6d52349d50d2aeaaaf6ba934afbf490e4cbc1ba5bb715c3133ea45cdca4")},
        "model_dir": "MiniMax-Music3-GGUF",        # --model : le dossier du paquet
        "rank": 1, "elo": None, "vram": 10240, "lyrics": "required",
        "seconds": MUSIC_SECONDS, "default_seconds": 30, "max_seconds": 180,
        "experimental": True,
    },
    # ACE-Step 1.5 turbo (01/2026), MIT : chansons avec paroles, une cinquantaine de langues.
    # En BF16 : la version 8 bits peut échouer ou changer de morceau (docs/models/ace_step.md
    # d'audio.cpp)
    "ace-step-1.5-turbo": {
        "label": "ACE-Step 1.5 turbo", "genre": MUSIC, "family": "ace_step", "task": "gen",
        "license": "MIT", "about": "audio_models.about_ace_step",
        "files": {"model": _file(GGUF, "ACE-Step1.5-GGUF/turbo/ace-step-1.5-turbo-bf16.gguf",
                                 10090406336,
                                 "7aa8cc911bb7e47647bba619cf25afc22a42e35c568a102cd9a3b28a54b94013")},
        "rank": 2, "elo": None, "vram": 12288, "lyrics": "optional",
        "args": ["--task-route", "text2music"],
        "seconds": MUSIC_SECONDS, "default_seconds": 30, "max_seconds": 240,
    },
    # Stable Audio 3 (Stability AI, 05/2026) : instrumental ; licence communautaire de
    # Stability (gratuite sous 1 M$ de chiffre d'affaires) et conditions de Gemma (son encodeur)
    "stable-audio-3-medium": {
        "label": "Stable Audio 3 Medium", "genre": MUSIC, "family": "stable_audio",
        "task": "gen", "license": "Stability AI Community License",
        "notice": "audio_models.notice_stable_audio", "about": "audio_models.about_sa3_medium",
        "files": {"model": _file(GGUF, "Stable-Audio-3-Medium-GGUF/stable-audio-3-medium-q8_0.gguf",
                                 3582601408,
                                 "d239eba564bd049b6fc0b103ee51cf179dc1a836ac7904c0d50088c6d45018a3")},
        "rank": 3, "elo": None, "vram": 6144, "lyrics": None,
        "seconds": MUSIC_SECONDS, "default_seconds": 30, "max_seconds": 360,
    },
    "stable-audio-3-small-music": {
        "label": "Stable Audio 3 Small", "genre": MUSIC, "family": "stable_audio",
        "task": "gen", "license": "Stability AI Community License",
        "notice": "audio_models.notice_stable_audio", "about": "audio_models.about_sa3_small",
        "files": {"model": _file(GGUF, "Stable-Audio-3-Small-Music-GGUF/"
                                 "stable-audio-3-small-music-q8_0.gguf", 1683576608,
                                 "a6d9176e11b857409b28cf5a69f4fefd8f3e61907b3b10d867f1f4c0a7eb9418")},
        "rank": 4, "elo": None, "vram": 4096, "lyrics": None,
        "seconds": MUSIC_SECONDS, "default_seconds": 30, "max_seconds": 120,
    },
    # MiDashengLM-Gen (mispeech), Apache-2.0 : un son mêlé décrit par couches (parole,
    # musique, effets, ambiance), 16 kHz mono, 10 s au plus ; « expérimental » dans
    # audio.cpp 0.9.3. Essai du 29/10/2026 (RTX 4070 portable, Vulkan) : 3,8 Go de carte,
    # 10 s de musique en 34 s
    "midashenglm-gen-music": {
        "label": "MiDashengLM-Gen", "genre": MUSIC, "family": "midashenglm_gen", "task": "gen",
        "license": "Apache-2.0", "about": "audio_models.about_midasheng",
        "files": {"model": MIDASHENG},
        "rank": 5, "elo": None, "vram": 6144, "lyrics": None, "prompt": "midasheng",
        "seconds": MIDASHENG_SECONDS, "default_seconds": 10, "max_seconds": 10,
        "experimental": True,
    },
    # ------------------------------------------------------------------ effets sonores
    "stable-audio-3-small-sfx": {
        "label": "Stable Audio 3 Small SFX", "genre": SOUND, "family": "stable_audio",
        "task": "gen", "license": "Stability AI Community License",
        "notice": "audio_models.notice_stable_audio", "about": "audio_models.about_sa3_sfx",
        "files": {"model": _file(GGUF, "Stable-Audio-3-Small-SFX-GGUF/"
                                 "stable-audio-3-small-sfx-q8_0.gguf", 1683576512,
                                 "bfe435d2c5841868a7025433929ee250e51e491be369a279f341221539c71841")},
        "rank": 1, "elo": None, "vram": 4096,
        "seconds": SOUND_SECONDS, "default_seconds": 5, "max_seconds": 30,
    },
    "midashenglm-gen-sound": {
        "label": "MiDashengLM-Gen", "genre": SOUND, "family": "midashenglm_gen", "task": "gen",
        "license": "Apache-2.0", "about": "audio_models.about_midasheng",
        "files": {"model": MIDASHENG},
        "rank": 2, "elo": None, "vram": 6144, "prompt": "midasheng",
        "seconds": MIDASHENG_SECONDS, "default_seconds": 5, "max_seconds": 10,
        "experimental": True,
    },
}


class ModelError(Exception):
    pass


def model(model_id: str) -> dict:
    found = MODELS.get(model_id)
    if found is None:
        raise ModelError(t("audio.unknown_model", model=model_id))
    return found


def genre(model_id: str) -> str:
    return model(model_id)["genre"]


def of_genre(name: str) -> list:
    """Les modèles de ce genre, dans l'ordre du catalogue."""
    return [m for m, spec in MODELS.items() if spec["genre"] == name]


def total_size(model_id: str) -> int:
    return sum(f["size"] for f in model(model_id)["files"].values())


def hashes(model_id: str) -> list:
    """Empreintes des fichiers du modèle : une machine qui les a toutes calcule pareil."""
    return sorted(f["sha256"] for f in model(model_id)["files"].values())


def rank(model_id: str) -> int:
    return int(model(model_id).get("rank") or 999)


def vram(model_id: str) -> int:
    return int(model(model_id).get("vram") or 0)


def label(model_id: str) -> str:
    return model(model_id)["label"]


# ------------------------------------------------------------ textes affichés
def _text(value, **params):
    return t(value, **params) if i18n.exists(value) else value


def about(model_id: str) -> str:
    return _text(model(model_id)["about"])


def notice(model_id: str) -> str | None:
    """L'avertissement d'une licence restrictive, lu et accepté avant le téléchargement."""
    found = model(model_id).get("notice")
    return _text(found) if found else None


def restricted(model_id: str) -> bool:
    return bool(MODELS.get(model_id, {}).get("notice"))


def accepted(model_id: str) -> bool:
    """Sa licence est-elle acceptée ici ? Vrai sans licence restrictive ; sinon, si elle l'a
    été (au téléchargement, ou en le choisissant pour une autre machine)."""
    if not restricted(model_id):
        return True
    return model_id in (settings.get("licenses_accepted") or [])


def accept(model_id: str):
    """Sa licence, lue et acceptée par l'utilisateur, notée ici (ModelError s'il n'est pas au
    catalogue)."""
    model(model_id)
    if not restricted(model_id) or accepted(model_id):
        return
    settings.set_value("licenses_accepted",
                       [*(settings.get("licenses_accepted") or []), model_id])


# ------------------------------------------------------------ voix, langues, durées
def languages(model_id: str) -> list:
    return list(model(model_id).get("languages") or [])


def speaks(model_id: str, language: str | None) -> bool:
    """Une voix qui parle cette langue (une musique, un effet : toujours)."""
    spec = model(model_id)
    if spec["genre"] != VOICE or not language:
        return True
    return language in spec.get("languages") or []


def voices(model_id: str, language: str | None = None) -> list:
    """[[identifiant, libellé]] des voix de ce modèle dans cette langue (la première : celle
    par défaut) ; [] pour une musique ou un effet."""
    table = model(model_id).get("voices") or {}
    found = table.get(language or "") or table.get("*") or []
    if not found and table:
        first = (model(model_id).get("languages") or [""])[0]
        found = table.get(first) or []
    return [[voice, _text(name)] for voice, name in found]


def seconds(model_id: str) -> list:
    """[[secondes, libellé]] des durées proposées (musique, effets) : « 20 s »."""
    return [[n, t("format.seconds", n=n)] for n in model(model_id).get("seconds") or []]
