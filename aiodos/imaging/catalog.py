"""Modèles d'images et de vidéos connus d'AIODOS, et les fichiers qu'en a cette machine.

Un modèle de stable-diffusion.cpp tient en plusieurs fichiers : le cœur (diffusion), et
selon son architecture l'encodeur de texte (llm, t5xxl), le décodeur (vae), celui du son
(audio_vae), un second cœur (high_noise : l'expert « bruit fort » de Wan 2.2), un
découpage du texte (tokenizer), ou un seul fichier pour tout (model). Chacun est épinglé ici
par son nom, sa taille et son empreinte SHA-256, avec son adresse sur Hugging Face à un commit
précis : le téléchargement est vérifié, et deux machines qui ont les mêmes empreintes calculent
avec exactement le même modèle (unités confiées, jobs/kinds/images.py). Un fichier commun à
plusieurs modèles (le décodeur de FLUX.2, l'encodeur UMT5 de Wan…) porte le même nom et la
même empreinte : il n'est téléchargé qu'une fois.

Toutes les adresses se téléchargent sans compte : un dépôt officiel à accès conditionné
(« gated » : FLUX 3, LTX-2.5) est remplacé par une copie publique, notée à côté. Tailles et
empreintes relevées par l'API de Hugging Face le 29/10/2026 (champ « lfs.oid »), sans rien
télécharger.

Chaque modèle a un rang dans son genre (rank, 1 : le plus puissant) : le classement des notes
d'Artificial Analysis (elo, modèles à poids ouverts, lu le 29/10/2026), puis nos mesures ; et
la mémoire de carte graphique qu'il demande (vram, en Mio : poids du cœur et calcul ;
stable-diffusion.cpp place le reste en mémoire vive, « auto-fit »), une estimation à corriger
par nos mesures. Une variante plus grosse d'un même modèle (variant_of) a le même rang : à rang
égal, la plus grosse passe d'abord (imaging/ranking.py).

Une licence qui n'est ni Apache ni MIT a un avertissement (notice), lu et accepté avant le
téléchargement (décision de l'auteur du 29/10/2026 : « chacun est responsable de ses actes ») ;
celle qui encadre le prêt du modèle à d'autres a aussi un avertissement pour qui le prête
(lend_notice).

Les fichiers sont rangés dans data/images/models/ (réglage « images_models_dir » pour un
autre dossier, par exemple pour partager les modèles entre plusieurs copies d'AIODOS).
Leur empreinte n'est recalculée que si leur taille ou leur date changent : le résultat
est noté dans data/images/verified.json.

Les textes d'un modèle (description, licence, formats) sont des clés des textes
(i18n/*/imaging_models.json), écrites dans la langue en cours par about(), notice(),
lend_notice(), sizes() et durations() ; son nom (label) reste tel quel.
"""
import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from aiodos import i18n, paths, settings
from aiodos.i18n import t

log = logging.getLogger("aiodos.images")

HF = "https://huggingface.co/{repo}/resolve/{commit}/{path}"

# Option de sd-server pour chaque partie d'un modèle
FLAGS = {"diffusion": "--diffusion-model", "llm": "--llm", "vae": "--vae", "model": "--model",
         "t5xxl": "--t5xxl", "clip_l": "--clip_l", "audio_vae": "--audio-vae",
         "high_noise": "--high-noise-diffusion-model", "tokenizer": "--tokenizer"}

# Chaque modèle dit aussi ce qu'il sait faire : formats proposés (sizes : largeur, hauteur,
# clé du libellé), côtés en multiples de « multiple » entre les bornes « side », pixels au
# plus ; pour une vidéo, la cadence (fps) et les nombres d'images permis, « frame_step » × k
# + « frame_base » (durations : ceux proposés, avec leur durée en secondes). Les réglages du
# calcul : pas (steps), guidage (cfg ; 1 : sans, la consigne négative n'a alors pas d'effet),
# échantillonneur (sampler), décalage (flow_shift) ; pour Wan 2.2 A14B, ceux de l'expert
# « bruit fort » (high_noise : cfg, steps, -1 : selon la frontière des experts). Ce sont ceux
# des documents de stable-diffusion.cpp (docs/<modèle>.md) et de ses recettes
# (examples/server/recipes, relues contre les cartes des éditeurs), version master-1027.


def _file(repo: str, commit: str, path: str, size: int, sha256: str, name: str | None = None):
    """Un fichier épinglé : son nom ici (le dernier morceau de son chemin, sauf name), sa
    taille, son empreinte, son adresse au commit."""
    return {"name": name or path.rsplit("/", 1)[-1], "size": size, "sha256": sha256,
            "url": HF.format(repo=repo, commit=commit, path=path)}


# Fichiers communs à plusieurs modèles (le même fichier, téléchargé une fois)
# Le décodeur de FLUX.2, celui de FLUX 3 aussi (docs/flux3.md) ; Apache-2.0
FLUX2_VAE = _file("Comfy-Org/flux2-klein-4B", "5b3abca5c0dc1ecd4eaf3340868d09063fc1c965",
                  "split_files/vae/flux2-vae.safetensors", 336211292,
                  "868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3")
# L'encodeur de texte et le décodeur de Wan 2.1 et de Wan 2.2 A14B ; Apache-2.0
UMT5 = _file("city96/umt5-xxl-encoder-gguf", "b535255bee98c2b0a59ea7c0ae2dcd0c6657b3b7",
             "umt5-xxl-encoder-Q5_K_M.gguf", 4145878880,
             "eaea358bb438c5d211721a4feecc162000e3636e9cb96f51e216f1f44ebd12ce")
WAN21_VAE = _file("Comfy-Org/Wan_2.1_ComfyUI_repackaged",
                  "123acf1cc74bccbb9bfff8ac1ee72edc08c2341d",
                  "split_files/vae/wan_2.1_vae.safetensors", 253815318,
                  "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b")
# La consigne négative habituelle de Wan, en chinois, quand l'utilisateur n'en donne pas
WAN_NEGATIVE = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
                "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，"
                "画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，"
                "静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")
# Les formats d'une image (Z-Image Turbo, FLUX, Ming, HiDream, Qwen-Image)
IMAGE_SIZES = [[1024, 1024, "imaging_models.size_square"],
               [768, 1344, "imaging_models.size_portrait"],
               [1344, 768, "imaging_models.size_landscape"],
               [512, 512, "imaging_models.size_fast"]]
# Les formats d'une vidéo de Wan (le premier tient dans les bornes du réseau public :
# network/public_video.MAX_PIXELS)
WAN_SIZES = [[832, 480, "imaging_models.size_landscape"],
             [480, 832, "imaging_models.size_portrait"],
             [624, 624, "imaging_models.size_square"]]

MODELS = {
    # ------------------------------------------------------------------ images
    # FLUX 3 Image Klein (Black Forest Labs, 15/10/2026), Elo 1043 : le premier des modèles
    # ouverts. Dépôt officiel à accès conditionné : les GGUF de leejet (l'auteur de
    # stable-diffusion.cpp), même licence ; encodeur Qwen3-VL-4B (Apache-2.0)
    "flux-3-image-klein": {
        "label": "FLUX 3 Image Klein",
        "license": "FLUX Kommunity License v1.0",
        "notice": "imaging_models.notice_flux_3_image_klein",
        "about": "imaging_models.about_flux_3_image_klein",
        "files": {
            "diffusion": _file("leejet/flux-3-image-klein-GGUF",
                               "1c2ad2f9b9247ccca6506a23bdb033fdb0873321",
                               "flux3-image-klein-Q4_K.gguf", 3514023104,
                               "19ddbfbed61da4bb16ebf169aaf48e9e5db43aba557be1ee537e9f00f586092e"),
            "llm": _file("Qwen/Qwen3-VL-4B-Instruct-GGUF",
                         "1cd86afb9a95c410a6038ab3b40d8b578c892266",
                         "Qwen3VL-4B-Instruct-Q4_K_M.gguf", 2497281664,
                         "66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a"),
            "vae": FLUX2_VAE,
        },
        # Distillé : 16 pas, sans guidage (docs/flux3.md)
        "kind": "image", "rank": 1, "elo": 1043, "vram": 8192,
        "steps": 16, "cfg": 1.0, "negative": False, "sampler": "euler",
        "sizes": IMAGE_SIZES, "multiple": 16, "side": [256, 2048], "max_pixels": 1536 * 1536,
    },
    # Le même en 8 bits (6,2 Go au lieu de 3,5) : plus fidèle, pour une carte de 12 Go
    "flux-3-image-klein-q8": {
        "label": "FLUX 3 Image Klein (Q8)", "variant_of": "flux-3-image-klein",
        "license": "FLUX Kommunity License v1.0",
        "notice": "imaging_models.notice_flux_3_image_klein",
        "about": "imaging_models.about_flux_3_image_klein_q8",
        "files": {
            "diffusion": _file("leejet/flux-3-image-klein-GGUF",
                               "1c2ad2f9b9247ccca6506a23bdb033fdb0873321",
                               "flux3-image-klein-Q8_0.gguf", 6151716032,
                               "8b71ffd749bcacbd50cc0acca4f2d31a044c94412743e1e780db99bf13f89752"),
            "llm": _file("Qwen/Qwen3-VL-4B-Instruct-GGUF",
                         "1cd86afb9a95c410a6038ab3b40d8b578c892266",
                         "Qwen3VL-4B-Instruct-Q4_K_M.gguf", 2497281664,
                         "66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a"),
            "vae": FLUX2_VAE,
        },
        "kind": "image", "rank": 1, "elo": 1043, "vram": 12288,
        "steps": 16, "cfg": 1.0, "negative": False, "sampler": "euler",
        "sizes": IMAGE_SIZES, "multiple": 16, "side": [256, 2048], "max_pixels": 1536 * 1536,
    },
    # Qwen-Image-2.1 (Alibaba, 14/09/2026), Elo 1042 ; licence de recherche, non commerciale.
    # GGUF de leejet ; encodeur Qwen3-VL-8B (Apache-2.0) ; son propre décodeur (Comfy-Org).
    # Sa version Turbo (8 pas) n'est pas prise : même mémoire, plus rapide, mais rien ne dit
    # qu'elle soit meilleure (pas au classement)
    "qwen-image-2.1": {
        "label": "Qwen-Image-2.1",
        "license": "Qwen Research License",
        "notice": "imaging_models.notice_qwen_image_2_1",
        "about": "imaging_models.about_qwen_image_2_1",
        "files": {
            "diffusion": _file("leejet/Qwen-Image-2.1-GGUF",
                               "cc11433936a06e9765f7c0c0b1f0436cfd2b9856",
                               "qwen_image_2.1-Q4_K.gguf", 4197494816,
                               "29f9c83c249ff0292fb2943fceddfa2319b446601866c82a4f8be062abea72c2"),
            "llm": _file("Qwen/Qwen3-VL-8B-Instruct-GGUF",
                         "f982a07559d4a2f6c8744d840bf6fccab30eea96",
                         "Qwen3VL-8B-Instruct-Q4_K_M.gguf", 5027784800,
                         "67d1659bfe71b89d50b45a4ad1a9e5b997e5bb16ce5da66a6a6167abd569e9e2"),
            "vae": _file("Comfy-Org/Qwen-Image-2.1", "0217efd1d9c9452e84f6448903d6e21bfa171ffd",
                         "vae/qwen_image_2.1_vae_bf16.safetensors", 675509688,
                         "bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9"),
        },
        # La recette de sd-server (qwen-image-21) : sans guidage, 20 pas (40 chez Qwen)
        "kind": "image", "rank": 2, "elo": 1042, "vram": 12288,
        "steps": 20, "cfg": 1.0, "negative": False, "sampler": "euler",
        "sizes": IMAGE_SIZES, "multiple": 32, "side": [256, 2048], "max_pixels": 1536 * 1536,
    },
    # Ming-Image-0.1-Design (inclusionAI, 17/09/2026), Elo 999 ; MIT. Les GGUF faits pour
    # stable-diffusion.cpp par voltaire321 (cœur en 8 bits : en 4 bits, il perd le pixel art),
    # le décodeur de Comfy-Org, le découpage du texte d'inclusionAI
    "ming-image-0.1-design": {
        "label": "Ming-Image 0.1 Design",
        "license": "MIT",
        "about": "imaging_models.about_ming_image",
        "files": {
            "diffusion": _file("voltaire321/Ming-Image-0.1-Design-GGUF",
                               "6339edc85081dd6b8b248debd75f9e64dc6750d0",
                               "ming_image_0.1_design-Q8_0.gguf", 6541979808,
                               "260ded24503b35b80b6eb4a5949f664b26f6c00b46e8ec6e6d00942593eb120b"),
            "llm": _file("voltaire321/Ming-Image-0.1-Design-GGUF",
                         "6339edc85081dd6b8b248debd75f9e64dc6750d0",
                         "ming_ling_mini_2.0-Q4_K.gguf", 10518620096,
                         "70f7ea31d3e1ffa917e70b961e5d422b2a8b19327c3522033a381b1a4b766002"),
            "vae": _file("Comfy-Org/Ming-Image", "93804ff3747517a318969c5f61b25a57daefe09f",
                         "vae/ming_image_vae_bf16.safetensors", 253816696,
                         "7f5bed402dc8c77dc2e0ab1929a85d4df433b7cf7b599dfa8c353da98db0b90a"),
            "tokenizer": _file("inclusionAI/Ming-Image-0.1-Design",
                               "c57c49689d12ef7d4e24a170014f1dc43d3ce258",
                               "mllm/tokenizer.json", 12210709,
                               "e7ff01708d504f7bf4dbf7f5815adde57bab9a40e7f563ab6ad1acace4464917",
                               name="ming_image_0.1_tokenizer.json"),
        },
        # docs/ming_image.md : 12 pas, sans guidage
        "kind": "image", "rank": 3, "elo": 999, "vram": 12288,
        "steps": 12, "cfg": 1.0, "negative": False, "sampler": "euler",
        "sizes": IMAGE_SIZES, "multiple": 16, "side": [256, 2048], "max_pixels": 1536 * 1536,
    },
    # HiDream-O1-Image (HiDream, 08/05/2026), Elo 975 ; MIT. Un seul fichier, sans encodeur ni
    # décodeur à part (docs/hidream_o1_image.md) : en 16 bits (16,4 Go), la seule forme que
    # stable-diffusion.cpp lit telle quelle (son « fp8_scaled » demande une conversion)
    "hidream-o1-image": {
        "label": "HiDream-O1-Image",
        "license": "MIT",
        "about": "imaging_models.about_hidream_o1",
        "files": {
            "model": _file("Comfy-Org/HiDream-O1-Image", "7b75f4cd7e757d55d9bd1d30fb40375fb446f38c",
                           "checkpoints/hidream_o1_image_bf16.safetensors", 16365209824,
                           "0520134a47e5b2bacea25804b2a6021413eb95945344b5a953aa14170edff1aa"),
        },
        # La version complète (pas « Dev ») : guidage 5, décalage 3 (carte du modèle) ; 30 pas
        # au lieu de 50, à mesurer
        "kind": "image", "rank": 4, "elo": 975, "vram": 16384,
        "steps": 30, "cfg": 5.0, "negative": True, "sampler": "euler", "flow_shift": 3.0,
        "sizes": IMAGE_SIZES, "multiple": 32, "side": [256, 2048], "max_pixels": 1536 * 1536,
    },
    # Z-Image Turbo, Elo 936. Licences vérifiées le 17/10/2026 : Apache-2.0 pour les trois
    # fichiers
    "z-image-turbo": {
        "label": "Z-Image Turbo",
        "license": "Apache-2.0",
        "about": "imaging_models.about_z_image_turbo",
        "files": {
            "diffusion": _file("leejet/Z-Image-Turbo-GGUF",
                               "c61c0e422dc8b541b7548cf33a4ef8302b0f8085",
                               "z_image_turbo-Q4_K.gguf", 3864250304,
                               "14b375ab4f226bc5378f68f37e899ef3c2242b8541e61e2bc1aff40976086fbd"),
            "llm": _file("unsloth/Qwen3-4B-Instruct-2507-GGUF",
                         "a06e946bb6b655725eafa393f4a9745d460374c9",
                         "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2497281120,
                         "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597"),
            "vae": _file("Comfy-Org/z_image_turbo", "6fc90a3b1b653e935a0d175e260736de25b84df5",
                         "split_files/vae/ae.safetensors", 335304388,
                         "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38"),
        },
        # Modèle distillé : 8 pas, sans guidage (la consigne négative n'a pas d'effet). 6 Go de
        # carte suffisent (essai du 29/10/2026, master-1027 : 3,7 Go de poids sur la carte)
        "kind": "image", "rank": 5, "elo": 936, "vram": 6144, "steps": 8, "cfg": 1.0,
        "negative": False,
        "sizes": IMAGE_SIZES, "multiple": 64, "side": [256, 2048], "max_pixels": 1536 * 1536,
    },
    # FLUX.2 [klein] 4B (Black Forest Labs, 01/2026), Elo 837 ; Apache-2.0. Très rapide (4 pas).
    # GGUF de leejet, encodeur Qwen3-4B (docs/flux2.md), décodeur de FLUX.2
    "flux-2-klein-4b": {
        "label": "FLUX.2 [klein] 4B",
        "license": "Apache-2.0",
        "about": "imaging_models.about_flux_2_klein_4b",
        "files": {
            "diffusion": _file("leejet/FLUX.2-klein-4B-GGUF",
                               "3b1f5a9dc3abb32238b053aeb3d823c30afdacbd",
                               "flux-2-klein-4b-Q8_0.gguf", 4300629440,
                               "0bba6951258ec8f92d51114a8fa13e66828297bfff58a738f52729b3ef66fa28"),
            "llm": _file("unsloth/Qwen3-4B-GGUF", "22c9fc8a8c7700b76a1789366280a6a5a1ad1120",
                         "Qwen3-4B-Q4_K_M.gguf", 2497281312,
                         "f6f851777709861056efcdad3af01da38b31223a3ba26e61a4f8bf3a2195813a"),
            "vae": FLUX2_VAE,
        },
        "kind": "image", "rank": 6, "elo": 837, "vram": 8192,
        "steps": 4, "cfg": 1.0, "negative": False, "sampler": "euler",
        "sizes": IMAGE_SIZES, "multiple": 16, "side": [256, 2048], "max_pixels": 1536 * 1536,
    },
    # ------------------------------------------------------------------ vidéos
    # MiniMax H3 : vidéo ET son, Elo 1160 (classement des vidéos avec son), le premier des
    # modèles ouverts. Décision de l'utilisateur (17/10/2026), comme dans PANDORA
    # (core/h3_local.py), confirmée le 29/10/2026 : AIODOS le propose, affiche sa licence (qui
    # exclut l'Union européenne et interdit le service à des tiers), demande une confirmation
    # avant le téléchargement, et laisse chacun juge ; il peut être prêté, avec un
    # avertissement à qui le prête. Fichiers et réglages : ceux de stable-diffusion.cpp
    # (docs/minimax_h3.md), en version allégée pour une machine à 32 Go de mémoire vive. Pas
    # encore essayé ici (30 Go à télécharger).
    "minimax-h3": {
        "label": "MiniMax H3", "label_kind": "imaging_models.kind_video_sound",
        "license": "MiniMax H3 Community License",
        "notice": "imaging_models.notice_minimax_h3",
        "lend_notice": "imaging_models.lend_notice_minimax_h3",
        "about": "imaging_models.about_minimax_h3",
        "files": {
            "diffusion": _file("leejet/MiniMax-H3-GGUF", "d9c4c6312b4728a68a15a35626d84775a6523783",
                               "minimax_h3_fl2va_pruned-Q4_K_M.gguf", 11420663904,
                               "dd948e08ad0ba3c71bd42f368e283dd82e790f5122a63b276e22a3e0283d0c10"),
            "llm": _file("leejet/MiniMax-H3-GGUF", "d9c4c6312b4728a68a15a35626d84775a6523783",
                         "qwen3vl_32b_minimax_h3-Q2_K_M.gguf", 13102161024,
                         "a8ccadccd57ef34c838ffb8a7da8368bb554721b2760274a1d3b0df63960b997"),
            "vae": _file("Comfy-Org/MiniMax-H3", "e5eb578a89295337b8ff433a035929ce0279e0b6",
                         "vae/minimax_h3_video_vae_fp16.safetensors", 5207808496,
                         "7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522"),
            "audio_vae": _file("Comfy-Org/MiniMax-H3", "e5eb578a89295337b8ff433a035929ce0279e0b6",
                               "vae/minimax_h3_audio_vae_fp32.safetensors", 605254808,
                               "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48"),
        },
        # Sans guidage (cfg 1, comme les exemples) ; bruit tiré sur le processeur (--rng cpu)
        "kind": "video", "rank": 1, "elo": 1160, "vram": 16384,
        "steps": 20, "cfg": 1.0, "negative": False, "fps": 24,
        "server_args": ["--rng", "cpu"],
        "sizes": [[864, 480, "imaging_models.size_landscape"],
                  [480, 864, "imaging_models.size_portrait"],
                  [640, 640, "imaging_models.size_square"],
                  [1344, 768, "imaging_models.size_landscape_hd"]],
        "multiple": 32, "side": [256, 1344], "max_pixels": 1344 * 768,
        "frame_step": 17, "frame_base": 5, "max_frames": 260, "default_frames": 56,
        "durations": [[22, 0.9], [56, 2.3], [73, 3], [124, 5.2]],
    },
    # LTX-2.5 (Lightricks, 23/07/2026) : vidéo et son synchronisés, Elo 949 (« Fast », la
    # version distillée). Dépôt officiel à accès conditionné : le cœur distillé en GGUF de
    # vantagewithai (celui de docs/ltx2.md), l'encodeur Gemma 4 12B (avec sa projection) et
    # les décodeurs copiés tels quels par unsloth (mêmes tailles que chez Lightricks). Le
    # décodeur vidéo « conv » : celui par diffusion n'est pas pris en charge (docs/ltx2.md)
    "ltx-2.5": {
        "label": "LTX-2.5", "label_kind": "imaging_models.kind_video_sound",
        "license": "LTX-2.x Community License",
        "notice": "imaging_models.notice_ltx_2_5",
        "about": "imaging_models.about_ltx_2_5",
        "files": {
            "diffusion": _file("vantagewithai/LTX-2.5-GGUF",
                               "9a5c1bed3ff27b5e5d055fc04e3ae884381c2343",
                               "distilled/ltx-2.5-22b-distilled-transformer-Q4_K_M.gguf",
                               15687639424,
                               "8954332873a705cf1a980a188ace1e0fa015fc84422d2a4a4948df6a629977a7"),
            "llm": _file("unsloth/LTX-2.5-GGUF", "ac3c540c6025903784336e99b2dc179385c4c3b7",
                         "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
                         26263858182,
                         "ef7243612fdae7a75cb4d5cee9433e81380675fb6c213bd98ae74a9cd16561d1"),
            "vae": _file("unsloth/LTX-2.5-GGUF", "ac3c540c6025903784336e99b2dc179385c4c3b7",
                         "vae/ltx-2.5-video-vae-conv-bf16.safetensors", 1452269922,
                         "685b06ee3d9b2039647698fc4ea33175112462fc374e2777312c907897dfce8d"),
            "audio_vae": _file("unsloth/LTX-2.5-GGUF", "ac3c540c6025903784336e99b2dc179385c4c3b7",
                               "vae/ltx-2.5-audio-vae-bf16.safetensors", 364866540,
                               "c52733d37f6a7fb7949c3dc0fb468c6cb2169e4d836983a73babb9f0d54837a5"),
        },
        # Distillé : 8 pas, sans guidage (recette ltx23-distilled de sd-server) ; 8 k + 1
        # images, 24 par seconde
        "kind": "video", "rank": 2, "elo": 949, "vram": 24576,
        "steps": 8, "cfg": 1.0, "negative": False, "fps": 24, "sampler": "euler",
        "sizes": [[768, 512, "imaging_models.size_landscape"],
                  [512, 768, "imaging_models.size_portrait"],
                  [640, 640, "imaging_models.size_square"],
                  [1280, 704, "imaging_models.size_landscape_hd"]],
        "multiple": 32, "side": [256, 1920], "max_pixels": 1920 * 1088,
        "frame_step": 8, "frame_base": 1, "max_frames": 121, "default_frames": 49,
        "durations": [[25, 1], [49, 2], [73, 3], [121, 5]],
    },
    # Wan 2.2 T2V A14B (Alibaba, 07/2025) ; Apache-2.0. Deux experts de 14 milliards de
    # paramètres, en GGUF de QuantStack (docs/wan.md) : « bruit faible » (--diffusion-model) et
    # « bruit fort » (--high-noise-diffusion-model) ; l'encodeur et le décodeur de Wan 2.1. Pas
    # au classement lisible (celui des vidéos sans son ne s'affiche pas) : rang estimé
    "wan2.2-t2v-a14b": {
        "label": "Wan 2.2 A14B", "label_kind": "imaging_models.kind_video",
        "license": "Apache-2.0",
        "about": "imaging_models.about_wan2_2_a14b",
        "files": {
            "diffusion": _file("QuantStack/Wan2.2-T2V-A14B-GGUF",
                               "73eafba53a1a8f29254e4c77f92e74ea27d7cd6f",
                               "LowNoise/Wan2.2-T2V-A14B-LowNoise-Q4_K_M.gguf", 9650090496,
                               "091a5bae02e14aa016bc9b10a7892efda4c629346b81c5dcebbe30ea2ac8923a"),
            "high_noise": _file("QuantStack/Wan2.2-T2V-A14B-GGUF",
                                "73eafba53a1a8f29254e4c77f92e74ea27d7cd6f",
                                "HighNoise/Wan2.2-T2V-A14B-HighNoise-Q4_K_M.gguf", 9650090496,
                                "e0c490c6e316fd91ff52034e4ca66b825717e33ff11624585c0ccfcb5d410c59"),
            "t5xxl": UMT5,
            "vae": WAN21_VAE,
        },
        # La recette wan22-t2v de sd-server (d'après wan_t2v_A14B.py) : guidage 3 (4 pour
        # l'expert « bruit fort »), décalage 12, 20 pas partagés à la frontière 0,875
        "kind": "video", "rank": 3, "elo": None, "vram": 16384,
        "steps": 20, "cfg": 3.0, "negative": True, "fps": 16, "flow_shift": 12.0,
        "sampler": "euler",
        "high_noise": {"cfg": 4.0, "steps": -1, "sampler": "euler", "flow_shift": 12.0},
        "sizes": WAN_SIZES + [[1280, 720, "imaging_models.size_landscape_hd"]],
        "multiple": 16, "side": [256, 1280], "max_pixels": 1280 * 720,
        "frame_step": 4, "frame_base": 1, "max_frames": 81, "default_frames": 33,
        "durations": [[17, 1], [33, 2], [49, 3], [81, 5]],
        "default_negative": WAN_NEGATIVE,
    },
    # Wan 2.2 TI2V 5B (Alibaba, 07/2025) ; Apache-2.0. GGUF de QuantStack, encodeur de Wan 2.1,
    # son propre décodeur (wan2.2_vae, docs/wan.md) ; 24 images par seconde, jusqu'au 720p
    "wan2.2-ti2v-5b": {
        "label": "Wan 2.2 TI2V 5B", "label_kind": "imaging_models.kind_video",
        "license": "Apache-2.0",
        "about": "imaging_models.about_wan2_2_ti2v_5b",
        "files": {
            "diffusion": _file("QuantStack/Wan2.2-TI2V-5B-GGUF",
                               "57437632ddd08bdcbd1508c866aa22e126ed51d2",
                               "Wan2.2-TI2V-5B-Q8_0.gguf", 5400179040,
                               "57bece983817ab2f957546683bb670f13be7d99022d45674840cd999a050ea8f"),
            "t5xxl": UMT5,
            "vae": _file("Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
                         "ee6f4a40737a995bf5818954cfce6d59443b0f04",
                         "split_files/vae/wan2.2_vae.safetensors", 1409400960,
                         "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156"),
        },
        # La recette wan22-ti2v-5b de sd-server : guidage 5, décalage 5, 20 pas
        "kind": "video", "rank": 4, "elo": None, "vram": 12288,
        "steps": 20, "cfg": 5.0, "negative": True, "fps": 24, "flow_shift": 5.0,
        "sampler": "euler",
        "sizes": [[832, 480, "imaging_models.size_landscape"],
                  [480, 832, "imaging_models.size_portrait"],
                  [640, 640, "imaging_models.size_square"],
                  [1280, 704, "imaging_models.size_landscape_hd"]],
        "multiple": 32, "side": [256, 1280], "max_pixels": 1280 * 704,
        "frame_step": 4, "frame_base": 1, "max_frames": 121, "default_frames": 49,
        "durations": [[25, 1], [49, 2], [73, 3], [121, 5]],
        "default_negative": WAN_NEGATIVE,
    },
    # Wan 2.1 T2V 1.3B. Licences vérifiées le 17/10/2026 : Apache-2.0 pour les trois fichiers
    "wan2.1-t2v-1.3b": {
        "label": "Wan 2.1", "label_kind": "imaging_models.kind_video",
        "license": "Apache-2.0",
        "about": "imaging_models.about_wan2_1",
        "files": {
            "diffusion": _file("Comfy-Org/Wan_2.1_ComfyUI_repackaged",
                               "123acf1cc74bccbb9bfff8ac1ee72edc08c2341d",
                               "split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors",
                               2838303560,
                               "be531024cd9018cb5b48c40cfbb6a6191645b1c792eb8bf4f8c1c6e10f924dc5"),
            "t5xxl": UMT5,
            "vae": WAN21_VAE,
        },
        # Réglages de l'exemple de stable-diffusion.cpp (docs/wan.md)
        "kind": "video", "rank": 5, "elo": None, "vram": 8192,
        "steps": 20, "cfg": 6.0, "negative": True, "fps": 16,
        "flow_shift": 3.0, "sampler": "euler",
        "sizes": WAN_SIZES, "multiple": 16, "side": [256, 1280], "max_pixels": 1024 * 576,
        "frame_step": 4, "frame_base": 1, "max_frames": 161, "default_frames": 33,
        "durations": [[17, 1], [33, 2], [49, 3], [81, 5]],
        "default_negative": WAN_NEGATIVE,
    },
}


def is_video(model_id: str) -> bool:
    return MODELS.get(model_id, {}).get("kind") == "video"


_lock = threading.Lock()
_checking = set()                 # fichiers dont l'empreinte se calcule en ce moment


class ModelError(Exception):
    pass


def folder() -> Path:
    configured = str(settings.get("images_models_dir") or "").strip()
    return Path(configured) if configured else paths.data_dir() / "images" / "models"


def model(model_id: str) -> dict:
    found = MODELS.get(model_id)
    if found is None:
        raise ModelError(t("imaging.unknown_model_id", model=model_id))
    return found


def total_size(model_id: str) -> int:
    return sum(f["size"] for f in model(model_id)["files"].values())


def hashes(model_id: str) -> list:
    """Empreintes des fichiers du modèle : une machine qui les a toutes calcule pareil."""
    return sorted(f["sha256"] for f in model(model_id)["files"].values())


# ------------------------------------------------------------ textes affichés
def _text(value, **params):
    """Un texte d'un modèle dans la langue en cours : une clé des textes (imaging_models.json),
    traduite ; un texte tel quel sinon (modèle ajouté à la main, essais)."""
    return t(value, **params) if i18n.exists(value) else value


def about(model_id: str) -> str:
    return _text(model(model_id)["about"])


def notice(model_id: str) -> str | None:
    """L'avertissement d'une licence restrictive, lu et accepté avant le téléchargement."""
    found = model(model_id).get("notice")
    return _text(found) if found else None


def lend_notice(model_id: str) -> str | None:
    """L'avertissement à qui prête ce modèle à d'autres (sa licence l'encadre), ou None."""
    found = model(model_id).get("lend_notice")
    return _text(found) if found else None


def restricted(model_id: str) -> bool:
    """Sa licence (ni Apache ni MIT) demande d'être lue et acceptée avant le téléchargement."""
    return bool(MODELS.get(model_id, {}).get("notice"))


def accepted(model_id: str) -> bool:
    """Sa licence est-elle acceptée ici ? Vrai sans licence restrictive ; sinon, si elle l'a
    été (licenses_accepted : au téléchargement, ou en le choisissant pour une autre machine)."""
    if not restricted(model_id):
        return True
    from aiodos import settings
    return model_id in (settings.get("licenses_accepted") or [])


def accept(model_id: str):
    """Sa licence, lue et acceptée par l'utilisateur, notée ici (ModelError s'il n'est pas au
    catalogue)."""
    model(model_id)
    if not restricted(model_id) or accepted(model_id):
        return
    from aiodos import settings
    settings.set_value("licenses_accepted",
                       [*(settings.get("licenses_accepted") or []), model_id])


def rank(model_id: str) -> int:
    """Sa puissance dans son genre : 1 pour le plus puissant (cf. le début du module)."""
    return int(model(model_id).get("rank") or 999)


def vram(model_id: str) -> int:
    """La mémoire de carte graphique (Mio) qu'il demande (cf. le début du module)."""
    return int(model(model_id).get("vram") or 0)


def sizes(model_id: str) -> list:
    """[[largeur, hauteur, libellé]] des formats proposés : « Carré · 1024 × 1024 »."""
    return [[width, height, _text(label, width=width, height=height)]
            for width, height, label in model(model_id)["sizes"]]


def durations(model_id: str) -> list:
    """[[nombre d'images, libellé]] des durées proposées d'une vidéo : « 2,3 s » (« 2.3 s »
    en anglais). Un libellé déjà écrit reste tel quel."""
    return [[frames, seconds if isinstance(seconds, str) else _seconds(seconds)]
            for frames, seconds in model(model_id).get("durations") or []]


def _seconds(value) -> str:
    digits = 0 if float(value).is_integer() else 1
    return t("format.seconds", n=i18n.number(value, digits))


# ------------------------------------------------------------ vérification
def cache_file_path() -> Path:
    return paths.data_dir() / "images" / "verified.json"


def _load(cache_file: Path | None = None) -> dict:
    try:
        return json.loads((cache_file or cache_file_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _stamp(path: Path) -> list:
    info = path.stat()
    return [info.st_size, info.st_mtime_ns]


def record(path: Path, sha256: str, cache_file: Path | None = None):
    """Note l'empreinte d'un fichier (calculée au téléchargement ou après coup), dans
    `cache_file` : celui des données d'AIODOS au moment où le calcul a commencé."""
    target = cache_file or cache_file_path()
    with _lock:
        cache = _load(target)
        cache[str(path)] = {"stamp": _stamp(path), "sha256": sha256}
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".tmp")
        part.write_bytes(json.dumps(cache, indent=1).encode("utf-8"))
        os.replace(part, target)


def sha256_of(path: Path, stop=None) -> str | None:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 << 20), b""):
            if stop is not None and stop.is_set():
                return None
            digest.update(block)
    return digest.hexdigest()


def verify(path: Path, spec: dict, cache_file: Path | None = None) -> bool:
    """Calcule l'empreinte maintenant (quelques secondes par Go) et la note."""
    target = cache_file or cache_file_path()       # avant le calcul, qui dure
    digest = sha256_of(path)
    record(path, digest, target)
    if digest != spec["sha256"]:
        log.warning("Empreinte inattendue pour %s", path)
    return digest == spec["sha256"]


def _verify_later(path: Path, spec: dict):
    with _lock:
        if str(path) in _checking:
            return
        _checking.add(str(path))

    target = cache_file_path()        # fixé au lancement : le fil peut survivre à son contexte

    def run():
        try:
            verify(path, spec, target)
        except OSError as exc:
            log.warning("Vérification impossible de %s : %s", path, exc)
        finally:
            with _lock:
                _checking.discard(str(path))
    threading.Thread(target=run, daemon=True, name="aiodos-images-empreinte").start()


def file_state(spec: dict, check: bool = True) -> str:
    """ok | missing | partial (téléchargement commencé) | checking | bad.
    Un fichier présent mais pas encore vérifié l'est en arrière-plan (check)."""
    path = folder() / spec["name"]
    try:
        stamp = _stamp(path)
    except OSError:
        return "partial" if path.with_name(path.name + ".part").exists() else "missing"
    if stamp[0] != spec["size"]:
        return "bad"
    with _lock:
        known = _load().get(str(path))
        busy = str(path) in _checking
    if known and known.get("stamp") == stamp:
        return "ok" if known.get("sha256") == spec["sha256"] else "bad"
    if check and not busy:
        _verify_later(path, spec)
    return "checking"


def state(model_id: str, check: bool = True) -> dict:
    """Ce que cette machine a du modèle : « ready » s'il est complet et vérifié."""
    files = [{"part": part, "name": spec["name"], "size": spec["size"],
              "state": file_state(spec, check)}
             for part, spec in model(model_id)["files"].items()]
    states = {f["state"] for f in files}
    if states == {"ok"}:
        overall = "ready"
    elif "bad" in states:
        overall = "bad"
    elif "checking" in states:
        overall = "checking"
    else:
        overall = "missing"
    missing = sum(f["size"] for f in files if f["state"] != "ok")
    return {"state": overall, "files": files, "size": total_size(model_id),
            "missing_bytes": missing}


def label(model_id: str) -> str:
    """Le nom affiché d'un modèle : son nom propre, et ce qu'il crée quand ce n'est pas
    une image (« Wan 2.1 · vidéo »), dans la langue en cours."""
    spec = model(model_id)
    kind = spec.get("label_kind")
    return f"{spec['label']} · {t(kind)}" if kind else spec["label"]


def ready(model_id: str) -> bool:
    return model_id in MODELS and state(model_id)["state"] == "ready"


def files(model_id: str) -> dict:
    """{partie : chemin} d'un modèle complet et vérifié ; ModelError sinon."""
    if not ready(model_id):
        raise ModelError(t("imaging.model_incomplete", name=label(model_id)))
    return {part: folder() / spec["name"] for part, spec in model(model_id)["files"].items()}


def available() -> list:
    """Modèles complets et vérifiés ici (profil de la machine, unités qu'elle peut calculer)."""
    return [m for m in MODELS if ready(m)]
