"""Les modèles que le moteur intégré sait télécharger lui-même, sans Ollama : des fichiers
GGUF de Hugging Face, épinglés (révision du dépôt, taille, empreinte SHA-256) pour qu'un
fichier ne change jamais sous nos pieds. Relevés le 20/10/2026 (API de Hugging Face) :
dépôts publics, sans accès restreint ; licences libres pour la conversation (Apache-2.0
pour Qwen, Mistral et Gemma 4, MIT pour Phi), conditions de Gemma pour embeddinggemma, le
modèle d'embedding déjà utilisé (docs/embeddings.md). Gemma 4 et Qwen 3.6 ajoutés le
21/10/2026.

Mêmes noms que chez Ollama : réglages et conversations n'ont pas à changer. Les Qwen 3.5
voient les images (projecteur « mmproj ») : ils remplacent qwen2.5vl pour la lecture des
images, qu'Ollama a converti dans un format que llama.cpp ne lit pas.
"""

HF = "https://huggingface.co"


def _qwen(size, revision, weights, weights_sha, weights_bytes, mmproj_sha, mmproj_bytes):
    repo = f"unsloth/Qwen3.5-{size}-GGUF"
    return {"repo": repo, "revision": revision, "license": "Apache-2.0", "kind": "chat",
            "family": "qwen35", "parameters": size, "quantization": "Q4_K_M",
            "files": {"model": (weights, weights_sha, weights_bytes),
                      "mmproj": ("mmproj-F16.gguf", mmproj_sha, mmproj_bytes)}}


MODELS = {
    "qwen3.5:0.8b": _qwen("0.8B", "6ab461498e2023f6e3c1baea90a8f0fe38ab64d0",
                          "Qwen3.5-0.8B-Q4_K_M.gguf",
                          "bd258782e35f7f458f8aced1adc053e6e92e89bc735ba3be89d38a06121dc517",
                          532517120,
                          "56e4c6cfe73b0c82e3e82bc518d7591997e61d81f723fc41a586f4fa69ea2453",
                          204987232),
    "qwen3.5:2b": _qwen("2B", "f6d5376be1edb4d416d56da11e5397a961aca8ae",
                        "Qwen3.5-2B-Q4_K_M.gguf",
                        "aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223",
                        1280835840,
                        "7035e9cb8d7c6a9681d07eef9a364783e86ea4cd73faab2eabb4f43a101830c7",
                        668227264),
    "qwen3.5:4b": _qwen("4B", "e87f176479d0855a907a41277aca2f8ee7a09523",
                        "Qwen3.5-4B-Q4_K_M.gguf",
                        "00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4",
                        2740937888,
                        "cd88edcf8d031894960bb0c9c5b9b7e1fea6ebee02b9f7ce925a00d12891f864",
                        672423616),
    "qwen3.5:9b": _qwen("9B", "3885219b6810b007914f3a7950a8d1b469d598a5",
                        "Qwen3.5-9B-Q4_K_M.gguf",
                        "03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8",
                        5680522464,
                        "f70dc3509053962b0d0d3ee8a7eacebf5d60aa560cad78254ae8698516ae029f",
                        918166080),
    "mistral:7b": {
        "repo": "bartowski/Mistral-7B-Instruct-v0.3-GGUF",
        "revision": "61fd4167fff3ab01ee1cfe0da183fa27a944db48", "license": "Apache-2.0",
        "kind": "chat", "family": "llama", "parameters": "7B", "quantization": "Q4_K_M",
        "files": {"model": ("Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
                            "1270d22c0fbb3d092fb725d4d96c457b7b687a5f5a715abe1e818da303e562b6",
                            4372812000)}},
    "phi4-mini:latest": {
        "repo": "unsloth/Phi-4-mini-instruct-GGUF",
        "revision": "78eb92a46fc37e6b524df991ed9aca9bc6aa7b80", "license": "MIT",
        "kind": "chat", "family": "phi3", "parameters": "3.8B", "quantization": "Q4_K_M",
        "files": {"model": ("Phi-4-mini-instruct-Q4_K_M.gguf",
                            "88c00229914083cd112853aab84ed51b87bdf6b9ce42f532d8c85c7c63b1730a",
                            2491874272)}},
    # Ajoutés le 21/10/2026 (veille des sorties, accord de l'auteur) ; architectures gemma4
    # et qwen35moe connues de llama.cpp b11707 (src/llama-arch.cpp). Google, sous
    # Apache-2.0 depuis Gemma 4 : voit les images, tient dans une carte de 8 Go
    "gemma4:e4b": {
        "repo": "unsloth/gemma-4-E4B-it-GGUF",
        "revision": "bfc15c382204943c3a8fff0c750b94ae2364d7a3", "license": "Apache-2.0",
        "kind": "chat", "family": "gemma4", "parameters": "E4B", "quantization": "Q4_K_M",
        "files": {"model": ("gemma-4-E4B-it-Q4_K_M.gguf",
                            "85a896a047553e842f25297ee5b031d64ff30147d9c4af17b1e4b394cd1fab87",
                            4977171584),
                  "mmproj": ("mmproj-F16.gguf",
                             "ddf46c21d7078e95338cfc22306b19b276a29a5ad089023449dd54d4b6170a51",
                             990372672)}},
    # Alibaba, mixture d'experts : 35 milliards de paramètres, dont 3 travaillent à chaque
    # mot ; tourne en partie sur le processeur (--fit), avec 32 Go de mémoire vive
    "qwen3.6:35b-a3b": {
        "repo": "unsloth/Qwen3.6-35B-A3B-GGUF",
        "revision": "a483e9e6cbd595906af30beda3187c2663a1118c", "license": "Apache-2.0",
        "kind": "chat", "family": "qwen35moe", "parameters": "35B-A3B",
        "quantization": "UD-Q4_K_M",
        "files": {"model": ("Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
                            "ac0e2c1189e055faa36eff361580e79c5bd6f8e76bffb4ce547f167d53e31a61",
                            22134528992),
                  "mmproj": ("mmproj-F16.gguf",
                             "8971ee4f331ff0a4c609374f32984b3d4e6dc086c0aa35f1d637fad1829e887f",
                             899283680)}},
    "embeddinggemma:latest": {
        "repo": "ggml-org/embeddinggemma-300M-GGUF",
        "revision": "0f741b5a6585bd53aeb15cd1372c56f2a0f65e12",
        "license": "Gemma (conditions d'utilisation de Google)", "kind": "embedding",
        "family": "gemma-embedding", "parameters": "300M", "quantization": "Q8_0",
        "files": {"model": ("embeddinggemma-300M-Q8_0.gguf",
                            "b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63",
                            333590944)}},
}


def entry(name: str) -> dict | None:
    name = str(name or "").strip()
    return MODELS.get(name if ":" in name else f"{name}:latest")


def total_size(spec: dict) -> int:
    return sum(size for _file, _sha, size in spec["files"].values())


def url(spec: dict, file: str) -> str:
    return f"{HF}/{spec['repo']}/resolve/{spec['revision']}/{file}"
