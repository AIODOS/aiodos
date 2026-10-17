# Images et vidéo réparties entre plusieurs machines

Étude du 17/10/2026, suite de [gros-modeles.md](gros-modeles.md). Le but reste celui de
l'utilisateur : faire tourner les plus gros moteurs en les répartissant, pour le texte
comme pour « tous les autres moteurs de génération ». Les chiffres de trafic sont des
calculs, pas des mesures.

## 1. Ce qui change par rapport aux modèles de texte

Un modèle de texte qui écrit est limité par la lecture de la mémoire, un mot à la fois. Le
couper en tranches ne fait voyager que l'état d'un mot (16 Kio) : c'est ce qui rend la
répartition de llama.cpp possible, même par Internet.

Un modèle d'images ou de vidéo est limité par le calcul. Chaque pas de débruitage traite
des milliers de jetons à la fois. Le couper en tranches fait voyager tout leur état à
chaque pas, 3 à 4 ordres de grandeur de plus qu'un modèle de texte :

| Ce qui traverse le réseau | Flux.1, 1024² | Wan-14B, 480p × 81 images |
|---|---|---|
| Texte encodé, une fois par demande | 8 Mo | 8 Mo |
| Latent, une fois par passage de main | 1 Mo | 8,4 Mo |
| État du cœur, par coupure et par pas | 57 Mo | 671 Mo (le double avec la CFG) |
| Résultat | PNG de 1 à 2 Mo | MP4 de quelques Mo |

Pour comparer : 1 Gbit/s fait environ 110 Mo/s, un envoi Internet de 10 à 50 Mbit/s
environ 1,2 à 6 Mo/s.

D'où trois règles :

- **Couper le cœur du modèle n'accélère jamais une image.** Cela n'apporte que de la
  mémoire, et seulement sur un réseau local filaire.
- **Sur ce PC, la mémoire vive suffit déjà.** Ces modèles calculent tant que garder leurs
  poids en mémoire vive coûte peu (stable-diffusion.cpp : `--offload-to-cpu`, « sans
  ralentir la génération »). Avec 8 Go de carte et 32 Go de mémoire vive, Flux, Qwen-Image
  ou Wan-14B tournent ici.
- **Ce qui vaut la peine de partir ailleurs** : des images entières (unités
  indépendantes), ou des morceaux qui n'échangent que le texte encodé ou un latent.

## 2. Les façons de répartir

| Façon | Ce qui voyage | Réseau | Gain |
|---|---|---|---|
| **Unités indépendantes** : graines, lots, tuiles d'agrandissement, tranches d'images pour l'agrandissement ou l'interpolation | La demande, puis le PNG ou le MP4 | Internet compris | Débit multiplié par le nombre de machines ; chacune a tout le modèle |
| **Encodeur de texte ailleurs** (T5-XXL, UMT5, Mistral-24B de Flux.2, Qwen3-VL de Qwen-Image-2.1) | 4 à 16 Mo par demande | Internet compris | Libère la carte d'ici : les encodeurs pèsent de 4 à 17 Go |
| **Décodeur (VAE) ailleurs** | Le latent | Internet compris | Peu de chose |
| **Wan 2.2 A14B, un expert par machine** (bruit fort, puis bruit faible) | Un latent de 8 Mo par vidéo | Internet compris | Débit doublé sur une file de vidéos |
| **CFG partagée** : passes avec et sans consigne sur deux machines | 8 à 17 Mo par pas | Réseau local | ×1,89 mesuré sur Wan-14B (deux cartes, une machine) ; rien pour les modèles distillés sans CFG (Flux dev, Turbo, Lightning) |
| **Cœur coupé en tranches** (ggml RPC, `diffusion=cuda0&RPC0`) | 57 à 1 300 Mo par pas | Local filaire seulement | De la mémoire, pas de vitesse |
| **Parallélisme à l'intérieur du cœur** (xDiT, PipeFusion, DistriFusion…) | Encore plus, à chaque couche | NVLink ou 100 Gbit/s | PyTorch et NCCL : Linux seulement ; écarté |

## 3. Les outils

- **stable-diffusion.cpp** (MIT, quasi quotidien, master-973 du 17/10/2026) est l'équivalent
  de llama.cpp pour les images et la vidéo, sur ggml lui aussi.
  - Versions Windows CPU, CUDA, Vulkan (cartes AMD et Intel) et ROCm.
  - Images : SD 1.x à 3.5, Flux.1, Flux.2, FLUX.3 Klein, Chroma, Qwen-Image, Z-Image.
  - Vidéo : Wan 2.1 et 2.2, LTX-2.x, HunyuanVideo 1.5.
  - Il sait placer chaque composant sur un appareil (`--backend te=…,diffusion=…,vae=…`),
    et utiliser des appareils distants par ggml RPC depuis juin 2026 (PR #1629).
  - **Réserve** : il lui faut un `rpc-server` construit à partir de son propre commit de ggml
    (`GGML_MAX_NAME=160`), pas celui de llama.cpp, et sans doute une construction à nous :
    les versions publiées n'ont peut-être pas le RPC.
- **LocalAI** (MIT) fait déjà ce chemin : son module stable-diffusion.cpp emploie les mêmes
  serveurs RPC que llama.cpp. C'est le précédent le plus proche d'AIODOS.
- **ComfyUI** est sous GPL-3.0 : seulement comme programme à part, jamais fusionné.
  ComfyUI-Distributed (Apache-2.0) répartit graines, lots et agrandissements, sur des
  cartes NVIDIA.
- **SwarmUI** (MIT) envoie des générations entières à des machines distantes.
- **AI Horde** est sous AGPL-3.0, avec un serveur central : écarté.

## 4. Les modèles

Taille du cœur seul, en Go :

| Modèle | 16 bits | Q8 | Q4 | Tient en 8 Go ? |
|---|---|---|---|---|
| SD 3.5 Large (8,1 milliards) | 16,3 | 8,8 | 4,8 | en Q4 |
| Flux.1 dev / schnell (12) | 23,8 | 12,7 | 6,8 | en Q4, juste |
| Flux.2-dev (32) | ≈64 | ≈34 | 18,7 | non, plus un encodeur Mistral-24B |
| FLUX.3 Klein (5,6) | ≈11 | — | — | oui, en 8 bits |
| Z-Image Turbo (6) | ≈12 | 7,2 | ≈4 | oui |
| Qwen-Image (20) | 40,9 | 21,8 | 13,1 | non |
| Wan 2.1 1.3B | ≈2,8 | — | — | oui |
| Wan-14B (chaque expert de Wan 2.2 A14B) | 29,1 | 15,9 | 10,1 | non |
| LTX-2.5 (22) | ≈44 | 22,7 | 11,5 | non, plus un encodeur Gemma-12B |

L'encodeur UMT5-XXL de Wan pèse 11,4 Go en 16 bits, 6,0 en Q8, 3,7 en Q4.

**Licences des modèles**, à vérifier avant d'en livrer un :

- sous Apache-2.0 : Wan, Qwen-Image, Z-Image, Flux.1-schnell, Flux.2-klein-4B ;
- non commerciaux : Flux.1-dev, Flux.2-dev ;
- HunyuanVideo exclut l'Union européenne.

## 5. Recommandation

1. **Livrer stable-diffusion.cpp avec les versions signées**, comme llama.cpp : la version
   Vulkan pour toutes les cartes, la version CUDA pour NVIDIA. AIODOS le pilote par son
   serveur HTTP (`sd-server`).
2. **D'abord, des unités indépendantes dans la file de travail actuelle.** Une unité, c'est
   un modèle (empreinte), une consigne, une graine et des réglages ; elle rend un PNG ou un
   MP4. Cela couvre les graines, les lots, les tuiles d'agrandissement, et marche par
   Internet comme sur les cartes AMD et Intel. Les modèles se partagent entre machines,
   empreinte vérifiée.
3. **Ensuite, l'encodeur de texte sur une autre machine**, par les tunnels du compagnon
   (`--rpc-servers 127.0.0.1:<tunnel> --backend te=RPC0,diffusion=cuda0,vae=cuda0`, cache
   `-c` pour n'envoyer l'encodeur qu'une fois). Le PC portable pourrait alors faire tourner
   Flux.2-dev ou LTX-2.5 avec l'encodeur chez une autre machine.
4. **Plus tard, les partages par latent** : un expert de Wan 2.2 par machine, la CFG sur
   deux machines. stable-diffusion.cpp ne sait pas encore s'arrêter et reprendre à un pas
   donné ; il faudra du code à nous.
5. **Écarté** : le parallélisme à l'intérieur du cœur (Linux, NVLink), et fusionner du code
   GPL ou AGPL.

## Sources

- stable-diffusion.cpp : [dépôt](https://github.com/leejet/stable-diffusion.cpp),
  [RPC](https://raw.githubusercontent.com/leejet/stable-diffusion.cpp/master/docs/rpc.md),
  [composants](https://raw.githubusercontent.com/leejet/stable-diffusion.cpp/master/docs/backend.md),
  [performances](https://raw.githubusercontent.com/leejet/stable-diffusion.cpp/master/docs/performance.md),
  [Wan](https://raw.githubusercontent.com/leejet/stable-diffusion.cpp/master/docs/wan.md),
  [PR #1629](https://github.com/leejet/stable-diffusion.cpp/pull/1629)
- LocalAI : [images](https://localai.io/docs/features/image-generation/),
  [répartition](https://localai.io/docs/features/distribute/)
- ComfyUI : [dépôt](https://github.com/Comfy-Org/ComfyUI),
  [CFG sur plusieurs cartes](https://github.com/Comfy-Org/ComfyUI/pull/7063) ;
  [ComfyUI-Distributed](https://github.com/robertvoy/ComfyUI-Distributed)
- [SwarmUI](https://github.com/mcmonkeyprojects/SwarmUI), [xDiT](https://github.com/xdit-project/xDiT),
  [article xDiT](https://arxiv.org/abs/2411.01738), [AI Horde](https://github.com/Haidra-Org/horde-worker-reGen)
- Modèles : [Flux.2](https://huggingface.co/blog/flux-2),
  [Wan 2.2](https://github.com/Wan-Video/Wan2.2),
  [Flux.1 GGUF](https://huggingface.co/city96/FLUX.1-dev-gguf),
  [Qwen-Image GGUF](https://huggingface.co/city96/Qwen-Image-gguf),
  [Wan GGUF](https://huggingface.co/city96/Wan2.1-T2V-14B-gguf),
  [UMT5 GGUF](https://huggingface.co/city96/umt5-xxl-encoder-gguf),
  [LTX-2.5](https://comfyui-wiki.com/en/news/2026-08-11-ltx-2-5-open-weights-release),
  [licence HunyuanVideo](https://canirun.ai/license/tencent-hunyuan-community/)
