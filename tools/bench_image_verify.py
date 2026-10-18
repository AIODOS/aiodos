"""Vérifier une image calculée par une autre machine : mesure (docs/phase3-reseau-public.md § 5).

La même image (consigne, graine, taille, pas) est calculée par plusieurs moteurs : Vulkan et
CUDA sur la même carte graphique, et le processeur. Puis les « tricheries » d'une machine qui
voudrait s'épargner le calcul : moins de pas, une image plus petite agrandie, une autre graine,
une autre consigne, une image unie, du bruit. Pour chaque paire, plusieurs distances
(empreintes perceptuelles pHash et dHash, SSIM par blocs, PSNR) ; pour chaque image, des
mesures de contenu (écart type, compressibilité, part des hautes fréquences).

La question : un seuil sépare-t-il les écarts honnêtes (un autre moteur, une autre carte) des
tricheries ? Les images sont demandées à sd-server comme AIODOS le fait
(imaging/generate.request_body), avec le même modèle et les mêmes options (catalogue).

Usage (sd-server est lancé ici, un moteur après l'autre) :
  py -3.14 tools/bench_image_verify.py --vulkan C:/dev/stable-diffusion.cpp/master-973-vulkan
      --cuda C:/dev/stable-diffusion.cpp/master-973-cuda --models data/images/models
      --out <dossier de travail> [--cpu 2] [--size 512]
Résultats : docs/phase3-verification-images.json ; les images restent dans --out.
"""
import argparse
import base64
import io
import json
import socket
import subprocess
import sys
import time
import zlib
from itertools import combinations
from pathlib import Path

import numpy as np
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiodos.imaging import catalog, generate  # noqa: E402

MODEL = "z-image-turbo"
PROMPTS = [
    "une cabane en bois au bord d'un lac, au coucher du soleil, photo",
    "portrait d'un vieux marin à la barbe blanche, lumière douce",
    "un chat roux endormi sur le rebord d'une fenêtre, jour de pluie",
    "une rue de ville futuriste la nuit, néons et reflets sur le sol mouillé",
]
SEEDS = [42, 20261018]
REPEAT = 2                       # images recalculées une deuxième fois par le même moteur
NO_PROXY = {"http": None, "https": None}
OLLAMA = "http://127.0.0.1:11434"


# ------------------------------------------------------------------ sd-server
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _unload_ollama():
    """La carte pour sd-server : les modèles qu'Ollama garde chargés sont déchargés."""
    try:
        loaded = requests.get(f"{OLLAMA}/api/ps", timeout=5, proxies=NO_PROXY).json()
    except (requests.RequestException, ValueError):
        return
    for m in loaded.get("models") or []:
        endpoint = "embed" if "embed" in m["name"] else "generate"
        body = {"model": m["name"], "keep_alive": 0}
        if endpoint == "embed":
            body["input"] = ""
        try:
            requests.post(f"{OLLAMA}/api/{endpoint}", json=body, timeout=60, proxies=NO_PROXY)
        except requests.RequestException:
            pass


class Server:
    """Un sd-server lancé pour la mesure, avec les options d'AIODOS (imaging/engine.py)."""

    def __init__(self, name, sd_dir, models_dir, out, backend=None):
        self.name = name
        port = _free_port()
        self.base = f"http://127.0.0.1:{port}"
        args = [str(Path(sd_dir) / "sd-server.exe"), "--listen-ip", "127.0.0.1",
                "--listen-port", str(port), "--log-level", "info"]
        for part, spec in catalog.MODELS[MODEL]["files"].items():
            args += [catalog.FLAGS[part], str(Path(models_dir) / spec["name"])]
        args += list(catalog.MODELS[MODEL].get("server_args") or [])
        if backend:
            args += ["--backend", backend]
        self.log = open(Path(out) / f"sd-server-{name}.log", "w", encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        began = time.monotonic()
        self.process = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT,
                                        creationflags=flags)
        while time.monotonic() - began < 900:
            if self.process.poll() is not None:
                raise RuntimeError(f"sd-server ({name}) s'est arrêté : voir {self.log.name}")
            try:
                if requests.get(f"{self.base}/sdcpp/v1/capabilities", timeout=3,
                                proxies=NO_PROXY).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            self.stop()
            raise RuntimeError(f"sd-server ({name}) ne répond pas")
        self.load_s = round(time.monotonic() - began, 1)

    def image(self, prompt, seed, size, steps=None) -> tuple:
        params = {"model": MODEL, "prompt": prompt, "seed": seed, "width": size,
                  "height": size}
        if steps:
            params["steps"] = steps
        reply = requests.post(f"{self.base}/sdcpp/v1/img_gen",
                              json=generate.request_body(params), timeout=30, proxies=NO_PROXY)
        if reply.status_code != 202:
            raise RuntimeError(f"refus de sd-server ({reply.status_code}) : {reply.text[:200]}")
        job = reply.json()["id"]
        began = time.monotonic()
        while True:
            status = requests.get(f"{self.base}/sdcpp/v1/jobs/{job}", timeout=10,
                                  proxies=NO_PROXY).json()
            if status.get("status") == "completed":
                data = base64.b64decode(status["result"]["images"][0]["b64_json"])
                return Image.open(io.BytesIO(data)).convert("RGB"), time.monotonic() - began
            if status.get("status") in ("failed", "cancelled"):
                raise RuntimeError(f"image non calculée : {status.get('error')}")
            time.sleep(0.5)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(30)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.log.close()


# ------------------------------------------------------------------ distances
def _gray(img, size=None) -> np.ndarray:
    g = img.convert("L")
    if size:
        g = g.resize(size, Image.LANCZOS)
    return np.asarray(g, dtype=np.float64)


def _dct(n) -> np.ndarray:
    k, i = np.arange(n)[:, None], np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2 / n)
    m[0] /= np.sqrt(2)
    return m


_D32 = _dct(32)


def dhash(img) -> np.ndarray:
    a = _gray(img, (9, 8))
    return (a[:, 1:] > a[:, :-1]).flatten()


def phash(img) -> np.ndarray:
    """Basses fréquences (DCT 8 × 8 d'une réduction en 32 × 32, sans la composante
    continue) au-dessus de leur médiane : 63 bits."""
    a = _gray(img, (32, 32))
    low = (_D32 @ a @ _D32.T)[:8, :8].flatten()[1:]
    return low > np.median(low)


def ssim(img_a, img_b, block=8) -> float:
    """SSIM moyen sur des blocs de 8 × 8 (niveaux de gris), sans recouvrement."""
    a, b = _gray(img_a), _gray(img_b)
    h, w = (a.shape[0] // block) * block, (a.shape[1] // block) * block

    def blocks(x):
        return x[:h, :w].reshape(h // block, block, w // block, block).swapaxes(1, 2) \
            .reshape(-1, block * block)
    A, B = blocks(a), blocks(b)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = A.mean(1), B.mean(1)
    cov = ((A - ma[:, None]) * (B - mb[:, None])).mean(1)
    s = ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma ** 2 + mb ** 2 + c1) * (A.var(1) + B.var(1) + c2))
    return round(float(s.mean()), 4)


def psnr(img_a, img_b) -> float:
    a = np.asarray(img_a, dtype=np.float64)
    b = np.asarray(img_b, dtype=np.float64)
    mse = float(np.mean((a - b) ** 2))
    return 99.0 if mse == 0 else round(10 * np.log10(255 ** 2 / mse), 2)


def distances(img_a, img_b) -> dict:
    if img_a.size != img_b.size:
        img_b = img_b.resize(img_a.size, Image.LANCZOS)
    return {"phash": int(np.count_nonzero(phash(img_a) != phash(img_b))),
            "dhash": int(np.count_nonzero(dhash(img_a) != dhash(img_b))),
            "ssim": ssim(img_a, img_b), "psnr": psnr(img_a, img_b)}


def content(img) -> dict:
    """Une image vide ou du bruit se voit sans rien recalculer : écart type de la
    luminance, compressibilité (zlib, niveaux de gris 256 × 256), part des hautes
    fréquences (écart à un flou 3 × 3, rapporté à l'écart à la moyenne)."""
    a = _gray(img, (256, 256))
    raw = a.astype(np.uint8).tobytes()
    padded = np.pad(a, 1, mode="edge")
    blur = sum(padded[dy:dy + 256, dx:dx + 256] for dy in range(3) for dx in range(3)) / 9
    spread = float(np.mean(np.abs(a - a.mean())))
    return {"std": round(float(a.std()), 2),
            "compression": round(len(zlib.compress(raw, 9)) / len(raw), 3),
            "high_freq": round(float(np.mean(np.abs(a - blur))) / (spread + 1e-9), 3)}


# ------------------------------------------------------------------ mesure
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--vulkan", required=True, help="dossier de sd.cpp Vulkan")
    parser.add_argument("--cuda", help="dossier de sd.cpp CUDA (facultatif)")
    parser.add_argument("--models", required=True, help="dossier des modèles d'images")
    parser.add_argument("--out", required=True, help="dossier de travail (images, journaux)")
    parser.add_argument("--cpu", type=int, default=2,
                        help="images refaites par le processeur (lent), 0 pour aucune")
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--results", default=str(ROOT / "docs" / "phase3-verification-images.json"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    size = args.size
    cases = [(p, s) for p in range(len(PROMPTS)) for s in SEEDS]
    images, seconds, loads = {}, {}, {}

    def keep(key, img, took):
        images[key] = img
        seconds[key] = round(took, 1)
        img.save(out / ("_".join(str(k) for k in key) + ".png"))
        print(key, f"{took:.1f} s", flush=True)

    # 1. Les images honnêtes, et les tricheries qui demandent un calcul (Vulkan)
    _unload_ollama()
    server = Server("vulkan", args.vulkan, args.models, out)
    loads["vulkan"] = server.load_s
    try:
        for p, s in cases:
            keep(("vulkan", p, s), *server.image(PROMPTS[p], s, size))
        for p, s in cases[:REPEAT]:              # le même moteur, une deuxième fois
            keep(("vulkan2", p, s), *server.image(PROMPTS[p], s, size))
        for p, s in cases:
            for steps in (4, 2):
                keep((f"steps{steps}", p, s), *server.image(PROMPTS[p], s, size, steps))
            small, took = server.image(PROMPTS[p], s, size // 2)
            keep(("small", p, s), small.resize((size, size), Image.LANCZOS), took)
    finally:
        server.stop()

    # 2. Les mêmes, par d'autres moteurs
    others = []
    if args.cuda:
        others.append(("cuda", args.cuda, None, cases))
    if args.cpu:
        others.append(("cpu", args.vulkan, "cpu", cases[:args.cpu]))
    for name, sd_dir, backend, todo in others:
        _unload_ollama()
        server = Server(name, sd_dir, args.models, out, backend)
        loads[name] = server.load_s
        try:
            for p, s in todo:
                keep((name, p, s), *server.image(PROMPTS[p], s, size))
            if backend is None:
                for p, s in todo[:REPEAT]:
                    keep((f"{name}2", p, s), *server.image(PROMPTS[p], s, size))
        finally:
            server.stop()

    # 3. Distances : honnêtes (un autre moteur) et tricheries, par rapport à l'image Vulkan
    rng = np.random.default_rng(7)
    engines = ["vulkan"] + [name for name, *_ in others]
    pairs = []
    for p, s in cases:
        ref = images[("vulkan", p, s)]
        done = [e for e in engines if (e, p, s) in images]
        for a, b in combinations(done, 2):
            pairs.append({"case": [p, s], "kind": "honnête", "what": f"{a} / {b}",
                          **distances(images[(a, p, s)], images[(b, p, s)])})
        for e in done:
            if (f"{e}2", p, s) in images:
                same = np.array_equal(np.asarray(images[(e, p, s)]),
                                      np.asarray(images[(f"{e}2", p, s)]))
                pairs.append({"case": [p, s], "kind": "même moteur",
                              "what": f"{e}, deux fois", "identical": same,
                              **distances(images[(e, p, s)], images[(f"{e}2", p, s)])})
        other_seed = SEEDS[(SEEDS.index(s) + 1) % len(SEEDS)]
        other_prompt = (p + 1) % len(PROMPTS)
        mean = tuple(int(v) for v in np.asarray(ref).reshape(-1, 3).mean(0))
        cheats = {
            "4 pas au lieu de 8": images[("steps4", p, s)],
            "2 pas au lieu de 8": images[("steps2", p, s)],
            f"calculée en {size // 2} puis agrandie": images[("small", p, s)],
            "autre graine": images[("vulkan", p, other_seed)],
            "autre consigne": images[("vulkan", other_prompt, s)],
            "unie (couleur moyenne)": Image.new("RGB", (size, size), mean),
            "bruit": Image.fromarray(rng.integers(0, 256, (size, size, 3), dtype=np.uint8)),
            "floue (réduite à 64 puis agrandie)":
                ref.resize((64, 64), Image.LANCZOS).resize((size, size), Image.LANCZOS),
        }
        for what, img in cheats.items():
            pairs.append({"case": [p, s], "kind": "tricherie", "what": what,
                          **distances(ref, img)})

    summary = {}
    for metric, worse_is_higher in (("phash", True), ("dhash", True), ("ssim", False),
                                    ("psnr", False)):
        honest = [x[metric] for x in pairs if x["kind"] == "honnête"]
        cheat = {}
        for x in pairs:
            if x["kind"] == "tricherie":
                cheat.setdefault(x["what"], []).append(x[metric])
        pick_honest = max if worse_is_higher else min
        pick_cheat = min if worse_is_higher else max
        summary[metric] = {"pire honnête": pick_honest(honest) if honest else None,
                           "tricherie la plus proche": {k: pick_cheat(v) for k, v in cheat.items()}}
    repeats = [x for x in pairs if x["kind"] == "même moteur"]
    summary["même moteur"] = {"identiques au pixel près": sum(x["identical"] for x in repeats),
                              "sur": len(repeats)}
    contents = {"_".join(str(k) for k in key): content(img) for key, img in images.items()
                if key[0] in engines}
    contents["unie"] = content(Image.new("RGB", (size, size), (120, 110, 100)))
    contents["bruit"] = content(Image.fromarray(
        rng.integers(0, 256, (size, size, 3), dtype=np.uint8)))
    result = {"date": time.strftime("%Y-%m-%d %H:%M"), "model": MODEL, "size": size,
              "prompts": PROMPTS, "seeds": SEEDS, "load_s": loads,
              "seconds": {"_".join(str(k) for k in key): v for key, v in seconds.items()},
              "summary": summary, "pairs": pairs, "content": contents}
    Path(args.results).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
