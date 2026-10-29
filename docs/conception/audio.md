# Créer de l'audio : voix, musique, effets sonores

*Conception du 29/10/2026, suite de [modeles-auto.md](modeles-auto.md) (§ 3 « Audio », § 5, et
les décisions de l'auteur du § 7 : l'audio, c'est la voix, la musique et les effets sonores ;
« Automatique » pour chaque genre ; les licences au choix de l'utilisateur, une licence
restrictive affichée et acceptée avant le téléchargement). Dépôts et pages lus le 29/10/2026.*

## 1. Les moteurs étudiés

| Moteur | Ce qu'il fait | Versions publiées | Binaires | Licence | Verdict |
|---|---|---|---|---|---|
| **audio.cpp** (0xShug0, C++/ggml) | 130 familles : voix (Qwen3-TTS, Voxtral, PocketTTS, Kokoro, Chatterbox…), musique (ACE-Step 1.5, MiniMax Music 3, Stable Audio 3, HeartMuLa…), effets (Stable Audio 3 Small SFX, MiDashengLM-Gen…), transcription | v0.9.0 (30/09), v0.9.2 (15/10), **v0.9.3 (26/10/2026)** : une par semaine ou deux, notes détaillées, 3 850 étoiles | Windows x64 (CPU, Vulkan, CUDA 12.4/13.3), Ubuntu x64 (CPU, Vulkan), macOS arm64 et x64 (Metal) ; **pas d'Android** | Apache-2.0 | **choisi** |
| acestep.cpp, qwentts.cpp (ServeurpersoCom) | musique (ACE-Step) ; voix (Qwen3-TTS) | **aucune version publiée** : à compiler soi-même | aucun | MIT | écartés (rien à épingler) |
| ComfyUI | ACE-Step 1.5, Stable Audio 3, MiniMax Music 3 dans son cœur ; pas de voix | — | — | GPL-3.0 | plus tard, en recettes (§ 6) : tout le monde ne l'a pas |
| sherpa-onnx (k2-fsa) | voix (Kokoro, Piper, PocketTTS, Supertonic…), sur téléphone | v2.0.1 (26/10/2026) | Android aarch64 (programmes statiques), Windows, Linux, macOS, iOS | Apache-2.0 | **pour le téléphone** (§ 6) |

**audio.cpp** couvre les trois genres avec un seul programme, comme stable-diffusion.cpp pour
les images : une archive officielle épinglée dans `tools/engine_release.py` (Windows, Vulkan,
« portable » : toutes les cartes graphiques, tous les processeurs, 82 Mo), livrée avec les
versions et l'installeur
(`updates/engines.py`, moteur « audio.cpp »). Ses modèles sont des GGUF publics (dépôt
`audio-cpp/audio.cpp-gguf` sur Hugging Face, sans compte), un fichier par modèle le plus souvent.

AIODOS appelle **`audiocpp_cli`**, un processus par création (voix, musique ou effet), qui écrit
un WAV et s'arrête. Pas `audiocpp_server` : un serveur garderait le modèle chargé, mais il
écouterait sur un port (la leçon de sd-server : n'importe quel programme de la machine peut lui
parler) ; une création audio dure de quelques secondes à une minute, le chargement en est une
petite part. La carte graphique : la meilleure qui n'est pas intégrée au processeur
(`--list-devices`), sinon le processeur ; réglage « audio_device ».

## 2. Le catalogue (`aiodos/audio/catalog.py`)

Classement : celui d'Artificial Analysis quand le modèle y est (voix : Speech Arena, poids
ouverts ; musique : Music Arena), sinon une estimation, à corriger par nos mesures. ⚠ : licence
affichée et acceptée avant le téléchargement. Tailles et empreintes relevées par l'API de
Hugging Face (dépôt `audio-cpp/audio.cpp-gguf` au commit `874fcb4`, `audio-cpp/MiniMax-Music3-GGUF`
au commit `46fe77f`). Mémoire : celle de la carte graphique, estimée (mesurée quand c'est dit).

### Voix (lire un texte)

| Rang | Modèle | Licence | Fichiers | Mémoire | Langues | Voix |
|---|---|---|---|---|---|---|
| 1 | Voxtral 4B TTS (Mistral, 03/2026), Elo 1090 | ⚠ CC BY-NC 4.0 (non commerciale) | 4,7 Go | 8 Go | fr, en, es, de, it, pt, nl, ar, hi | fr_female, fr_male, casual_female… |
| 2 | Qwen3-TTS 1.7B CustomVoice (Alibaba, 01/2026), pas au classement | Apache-2.0 | 2,8 Go | 6 Go | 10 dont fr | Vivian, Ryan, Serena, Eric… |
| 3 | PocketTTS français (Kyutai, 100 M), pas au classement | CC BY 4.0 (attribution) | 0,13 Go | 1 Go (0,7 mesuré), ou le processeur | fr | cosette, jean |
| 3 | PocketTTS anglais | CC BY 4.0 | 0,13 Go | 1 Go | en | alba, jean |

Écartés pour l'instant : Kokoro 82M (Elo 1062, Apache-2.0) parle français avec eSpeak-ng, que
l'archive Windows ne contient pas (bibliothèque GPL-3.0 à joindre à part) ; Chatterbox
Multilingual (MIT) n'a pas de voix intégrée dans audio.cpp (il lui faut une voix de référence :
le clonage viendra) ; Fish Audio Drama 3 (Elo 1278) : 35 milliards de paramètres, licence qui
interdit le service à des tiers.

### Musique

| Rang | Modèle | Licence | Fichiers | Mémoire | Note |
|---|---|---|---|---|---|
| 1 | MiniMax Music 3 (08/2026), le meilleur ouvert (Music Arena) | ⚠ MiniMax-Music3 Community (nom affiché ; > 20 M$ : autorisation) | 8,1 Go (Q4) | 10 Go | chansons avec paroles ; « expérimental » dans audio.cpp |
| 2 | ACE-Step 1.5 turbo (01/2026) | MIT | 10,1 Go (BF16) | 12 Go | chansons avec paroles, 50 langues ; la version 8 bits peut échouer (doc d'audio.cpp) |
| 3 | Stable Audio 3 Medium (05/2026) | ⚠ Stability AI Community (+ conditions de Gemma) | 3,6 Go | 6 Go | instrumental, jusqu'à 6 min |
| 4 | Stable Audio 3 Small Music | ⚠ idem | 1,7 Go | 4 Go | instrumental, jusqu'à 2 min |
| 5 | MiDashengLM-Gen (mispeech) | Apache-2.0 | 3,4 Go | 6 Go (3,8 mesurés) | son mêlé (parole, musique, effets, ambiance), 10 s au plus ; « expérimental » |

### Effets sonores

| Rang | Modèle | Licence | Fichiers | Mémoire |
|---|---|---|---|---|
| 1 | Stable Audio 3 Small SFX (05/2026) | ⚠ Stability AI Community (+ Gemma) | 1,7 Go | 4 Go |
| 2 | MiDashengLM-Gen | Apache-2.0 | 3,4 Go (le même fichier que pour la musique) | 6 Go |

## 3. Créer de l'audio

- **Le travail** : genre « generate_audio » de la file (`jobs/kinds/audio.py`), une unité par
  création, calculée ici, chez mes machines qui ont audio.cpp et le modèle aux mêmes empreintes,
  ou chez mes amis s'ils prêtent leur carte et que je l'accepte (les réglages des images :
  `images_from_friends`, `lend_images_friends` ; un son se range avec les images et vidéos).
  **Le réseau public attendra** : son contrôle par échantillon et ses bornes sont faits pour les
  images (network/public_imaging.py) ; un son demande les siens.
- **Le résultat** : un WAV dans le dossier des créations, une ligne de la galerie (table
  images, genre « audio », sa durée, ses paroles) ; dans la conversation, un lecteur audio, le
  texte lu ou la description et les paroles, le modèle, la durée.
- **Dans la conversation** : le menu « Créer » propose « Une voix », « Une musique », « Un effet
  sonore » ; une demande écrite est reconnue (« Lis ce texte : … », « Crée une musique… »,
  « Fais le bruit d'une porte qui grince ») ; la description d'une musique ou d'un effet est
  traduite en anglais par le modèle de conversation (`chat/enrich.py`, comme les images), un
  texte à lire jamais.
- **« Automatique »** : pour chaque genre, le mieux classé qu'une machine a et peut faire
  tourner (ce PC, mes machines, mes amis), parmi ceux qui parlent la langue du texte (la voix) ;
  une licence restrictive seulement si elle a été acceptée ici.
- **Le menu** : l'onglet Audio du menu des modèles, trois parties (Voix, Musique, Effets
  sonores), chacune avec son « Automatique » ; Réglages → Modèles, les choix par défaut des trois
  genres et le téléchargement des modèles audio.

## 4. Décisions prises ici (à revoir avec l'auteur)

1. **Licence CC BY 4.0** (PocketTTS) : comptée comme libre, sans avertissement (l'attribution :
   le nom du modèle et sa licence sont affichés avec chaque création). La règle des images (« ni
   Apache ni MIT → avertissement ») la marquerait.
2. **Amis** : l'audio suit les réglages des images (`images_from_friends`,
   `lend_images_friends`) plutôt que deux réglages de plus.
3. **Essais** : seuls des modèles libres ont été téléchargés (PocketTTS français 134 Mo,
   MiDashengLM-Gen 3,4 Go pour la musique et les effets) ; les modèles ⚠ n'ont pas été
   téléchargés (leur licence n'est pas à accepter par un agent).

## 5. Essais réels (29/10/2026, PC de l'auteur : Core i9-14900HX, RTX 4070 portable 8 Go)

- **Le moteur** : l'archive Windows « Vulkan » ordinaire s'arrête sur *Illegal instruction*
  dès que le processeur calcule (code compilé pour l'AVX-512, que ce Core i9 n'a pas) ;
  l'archive « vulkan-portable » (82 Mo) marche : c'est elle qui est épinglée. Version lue :
  `0.9.3 (41c1f8ff)` ; appareils vus : la carte Intel intégrée (écartée), la RTX 4070, le
  processeur.
- **Téléchargés par le code d'AIODOS** (audio/download.py, comme un clic), dans `data/` du
  dépôt : PocketTTS français, 140 Mo en 19 s ; MiDashengLM-Gen, 3,38 Go en 113 s (33 Mo/s) ;
  3,5 Go en tout, empreintes vérifiées.
- **Voix** (PocketTTS français, 24 kHz mono) : 9,9 s de parole en 22 s sur le processeur,
  9,7 s en 22 s sur la carte au premier calcul, puis 5 s en 4 s depuis l'interface ; ≈ 0,7 Go
  de carte. Transcrites par Whisper (small) : le texte est juste, à part « AIODOS » et
  « PocketTTS » (« Eudois », « Pocket ETS »).
- **Effet sonore** (MiDashengLM-Gen, 16 kHz mono) : 5 s en 51 s (premier chargement) ;
  ≈ 3,8 Go de carte. Description en anglais : un son structuré (platitude du spectre 0,20) ; la
  même demande en français, sans modèle de conversation pour la traduire : un son plus proche
  du bruit (0,56). La traduction (chat/enrich.py) compte.
- **Musique** (MiDashengLM-Gen) : 20 s demandées, 10,0 s rendues en 34 s (son plafond : 10 s) ;
  depuis l'interface, 10 s demandées, 6,4 s rendues en 27 s (il s'arrête quand le morceau est
  fini ; forcé à 10 s, ses deux dernières secondes ne sont qu'un souffle). Un son tonal
  (platitude 0,02 à 0,04), sans silence.
- **L'interface** (nœud d'essai, PC et téléphone 375 px) : l'onglet Audio du menu (Voix,
  Musique, Effets sonores, chacun son « Automatique »), le mode « Créer » (voix et sa liste de
  voix ; musique et sa durée), les trois demandes écrites reconnues, un lecteur par son
  (lisible : durée lue par le navigateur), la galerie et sa visionneuse, Réglages → Modèles
  (la licence de Voxtral s'affiche avant le téléchargement : annulé, rien accepté).

## 6. Ce qui reste

- **Le téléphone** : audio.cpp n'a pas d'archive Android. La voix y passerait par sherpa-onnx
  (programmes statiques Android aarch64 de la v2.0.1, rangés comme llama-server dans l'appli,
  `node/android_app.py`) avec PocketTTS ou Kokoro en ONNX : un moteur et un catalogue de plus.
- **Le réseau public** : bornes d'une création audio (durée, taille), contrôle par échantillon,
  annonce des modèles audio prêtés dans le profil public.
- **Linux et macOS** : les archives d'audio.cpp existent (Vulkan, Metal), pas encore jointes.
- Le clonage d'une voix (Qwen3-TTS Base, Chatterbox, PocketTTS savent le faire), les recettes
  ComfyUI de musique, Kokoro avec eSpeak-ng.
