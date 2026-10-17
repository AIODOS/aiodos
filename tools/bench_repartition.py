"""Mesures pour l'étude des gros modèles répartis entre machines (docs/gros-modeles.md).

Vitesse d'un modèle servi par Ollama ou par llama-server (llama.cpp) : lecture de la
question (jetons/s) et écriture de la réponse (jetons/s), médiane de plusieurs essais.
Le relais simule la distance entre deux machines : placé entre llama-server et un
rpc-server, il retarde chaque paquet et peut plafonner le débit.

    python tools/bench_repartition.py ollama qwen3.6
    python tools/bench_repartition.py serveur http://127.0.0.1:8080 --nom "PC seul"
    python tools/bench_repartition.py relais 50052 127.0.0.1:50053 --delai-ms 20 --mbps 50

Avec --json, chaque résultat s'ajoute au fichier donné. Le script ne dépend pas du reste
d'AIODOS : pas besoin de lancer le nœud.
"""
import argparse
import json
import queue
import socket
import statistics
import threading
import time
from pathlib import Path

import requests

# Texte lu par le modèle avant de répondre (~600 jetons) : un texte quelconque, en français
PARAGRAPH = (
    "Le réseau relie des machines ordinaires : un ordinateur portable, un vieux PC de "
    "bureau, un téléphone. Chacune prête ce qu'elle a, de la mémoire ou du calcul, et le "
    "travail avance plus vite que sur une seule d'entre elles. Les données restent chez "
    "leur propriétaire, et chaque machine peut quitter le groupe à tout moment. ")
QUESTION = "\n\nRésume ce texte en une dizaine de phrases, puis donne trois idées pour l'améliorer."


def prompt(run: int, repeat: int) -> str:
    """Un début différent à chaque essai : sinon le serveur réutilise sa lecture précédente."""
    return f"Essai n° {run}.\n" + PARAGRAPH * repeat + QUESTION


def summary(name, runs, extra=None):
    result = {
        "nom": name, "essais": len(runs),
        "lecture_jetons": runs[-1]["prompt_n"],
        "lecture_jetons_s": round(statistics.median(r["prompt_tps"] for r in runs), 1),
        "ecriture_jetons_s": round(statistics.median(r["gen_tps"] for r in runs), 2),
        "premier_mot_s": round(statistics.median(r["first_s"] for r in runs), 2),
        "date": time.strftime("%Y-%m-%d %H:%M"),
    }
    result.update(extra or {})
    return result


def options(pairs) -> dict:
    """« num_gpu=16 » → {"num_gpu": 16} : options d'Ollama passées telles quelles."""
    found = {}
    for pair in pairs:
        key, _, text = pair.partition("=")
        for kind in (int, float):
            try:
                found[key] = kind(text)
                break
            except ValueError:
                found[key] = text
    return found


def bench_ollama(args):
    url = args.url.rstrip("/")
    runs = []
    for run in range(args.essais + 1):                   # le premier essai charge le modèle
        body = {"model": args.modele, "prompt": prompt(run, args.repete), "stream": False,
                "think": False, "keep_alive": "10m",
                "options": {"num_predict": args.mots, "temperature": 0, "seed": 1,
                            "num_ctx": args.contexte, **options(args.option)}}
        reply = requests.post(f"{url}/api/generate", json=body, timeout=1800).json()
        if "error" in reply:
            raise SystemExit(f"Ollama : {reply['error']}")
        prompt_s = reply["prompt_eval_duration"] / 1e9
        if run:
            runs.append({"prompt_n": reply["prompt_eval_count"],
                         "prompt_tps": reply["prompt_eval_count"] / prompt_s,
                         "gen_tps": reply["eval_count"] / (reply["eval_duration"] / 1e9),
                         "first_s": prompt_s})
            print(f"  essai {run} : lecture {runs[-1]['prompt_tps']:.0f} j/s, "
                  f"écriture {runs[-1]['gen_tps']:.1f} j/s", flush=True)
    placed = next((m for m in requests.get(f"{url}/api/ps", timeout=10).json()["models"]
                   if m["name"].startswith(args.modele)), {})
    extra = {"moteur": "ollama", "modele": args.modele, **options(args.option)}
    if placed:
        extra["memoire_go"] = round(placed["size"] / 1e9, 1)
        extra["sur_gpu_go"] = round(placed["size_vram"] / 1e9, 1)
    return summary(args.nom or f"Ollama {args.modele}", runs, extra)


def bench_server(args):
    url = args.url.rstrip("/")
    runs = []
    for run in range(args.essais + 1):
        body = {"prompt": prompt(run, args.repete), "n_predict": args.mots, "temperature": 0,
                "seed": 1, "cache_prompt": False}
        timings = requests.post(f"{url}/completion", json=body, timeout=1800).json()["timings"]
        if run:
            runs.append({"prompt_n": timings["prompt_n"],
                         "prompt_tps": timings["prompt_n"] / (timings["prompt_ms"] / 1000),
                         "gen_tps": timings["predicted_n"] / (timings["predicted_ms"] / 1000),
                         "first_s": timings["prompt_ms"] / 1000})
            print(f"  essai {run} : lecture {runs[-1]['prompt_tps']:.0f} j/s, "
                  f"écriture {runs[-1]['gen_tps']:.1f} j/s", flush=True)
    return summary(args.nom or url, runs, {"moteur": "llama-server"})


# ------------------------------------------------------------------------------- relais
class Link:
    """Un sens du relais : ce qui est lu d'un côté ressort de l'autre après le délai d'aller
    simple, et pas plus vite que le débit permis (temps de passage sur le « fil »)."""

    def __init__(self, source, target, one_way, rate):
        self.source, self.target = source, target
        self.one_way, self.rate = one_way, rate
        self.pending = queue.Queue()

    def receive(self):
        wire_free = 0.0
        try:
            while data := self.source.recv(65536):
                now = time.perf_counter()
                if self.rate:
                    wire_free = max(wire_free, now) + len(data) / self.rate
                    now = wire_free
                self.pending.put((now + self.one_way, data))
        except OSError:
            pass
        self.pending.put((0.0, b""))

    def send(self):
        try:
            while True:
                due, data = self.pending.get()
                if not data:
                    break
                wait = due - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)                      # précis à la milliseconde (3.11+)
                self.target.sendall(data)
        except OSError:
            pass
        for sock in (self.target, self.source):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def relay(args):
    host, port = args.vers.rsplit(":", 1)
    one_way, rate = args.delai_ms / 2000, args.mbps * 125_000 if args.mbps else 0
    server = socket.create_server(("127.0.0.1", args.port))
    print(f"Relais 127.0.0.1:{args.port} -> {args.vers}, aller-retour {args.delai_ms} ms, "
          f"débit {args.mbps or 'illimité'} Mbit/s", flush=True)
    while True:
        client, _ = server.accept()
        upstream = socket.create_connection((host, int(port)))
        for sock in (client, upstream):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for link in (Link(client, upstream, one_way, rate), Link(upstream, client, one_way, rate)):
            threading.Thread(target=link.receive, daemon=True).start()
            threading.Thread(target=link.send, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode, target in (("ollama", "modele"), ("serveur", "url")):
        p = sub.add_parser(mode)
        p.add_argument(target)
        if mode == "ollama":
            p.add_argument("--url", default="http://127.0.0.1:11434")
            p.add_argument("--contexte", type=int, default=4096)
            p.add_argument("--option", action="append", default=[],
                           help="option d'Ollama, ex. num_gpu=16 (couches sur le GPU)")
        p.add_argument("--nom", help="nom de la mesure dans les résultats")
        p.add_argument("--essais", type=int, default=3)
        p.add_argument("--mots", type=int, default=128, help="jetons écrits par essai")
        p.add_argument("--repete", type=int, default=8, help="longueur du texte lu")
        p.add_argument("--json", type=Path, help="fichier où ajouter le résultat")
    p = sub.add_parser("relais")
    p.add_argument("port", type=int)
    p.add_argument("vers", help="hôte:port du rpc-server")
    p.add_argument("--delai-ms", type=float, default=0.0, help="aller-retour ajouté")
    p.add_argument("--mbps", type=float, default=0.0, help="débit plafonné (0 = illimité)")
    args = parser.parse_args()
    if args.mode == "relais":
        return relay(args)
    result = bench_ollama(args) if args.mode == "ollama" else bench_server(args)
    print(json.dumps(result, ensure_ascii=False))
    if args.json:
        done = json.loads(args.json.read_text(encoding="utf-8")) if args.json.exists() else []
        args.json.write_text(json.dumps(done + [result], ensure_ascii=False, indent=1),
                             encoding="utf-8")


if __name__ == "__main__":
    main()
