"""Mesures pour l'écriture en essaim (docs/conception/essaim.md).

Une réponse écrite d'un trait par un seul modèle, comparée à la même réponse écrite en
essaim : une machine rédige un plan-contrat (les parties, et ce que toutes doivent
respecter : fil directeur, termes, faits, ton), puis chaque partie est écrite en même temps
par une autre « machine », et les parties sont assemblées dans l'ordre.

Ici, les machines sont les emplacements parallèles d'un même llama-server (--parallel N) :
sur une seule carte graphique, elles se partagent la carte et se ralentissent un peu ; de
vraies machines séparées iraient au moins aussi vite, à un aller-retour réseau près par
étage (plan, puis parties).

La qualité : le même modèle compare les deux réponses sans savoir laquelle est laquelle,
dans les deux ordres (pour annuler sa préférence de position). C'est un indice, pas un
verdict : un juge de 9 milliards de paramètres se trompe, et il se juge lui-même.

    llama-server -m qwen3.5-9b.gguf --parallel 8 --ctx-size 32768 --port 8090
    python tools/bench_essaim.py http://127.0.0.1:8090 --json tools/bench_essaim_results.json

Le script ne dépend pas du reste d'AIODOS.
"""
import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

NO_PROXY = {"http": None, "https": None}

# Des questions qui appellent une longue réponse, puis trois courtes (l'essaim n'y gagne rien)
QUESTIONS = [
    "Explique comment fonctionne la photosynthèse, de la lumière jusqu'au sucre.",
    "Compare Python, Rust et Go pour écrire un serveur web : forces, faiblesses, cas d'usage.",
    "Donne un plan d'entraînement de 8 semaines pour courir un premier 10 km.",
    "Quelles sont les causes de la Révolution française ?",
    "Comment préparer un voyage de deux semaines au Japon avec un petit budget ?",
    "Explique le fonctionnement d'un réseau pair-à-pair comme BitTorrent.",
    "Rédige un guide pour bien démarrer un potager sur un balcon.",
    "Quels sont les avantages et les risques de l'énergie nucléaire ?",
    "Explique les bases de l'apprentissage automatique à un lycéen.",
    "Comment négocier une augmentation de salaire ?",
    "Raconte l'histoire d'Internet et explique comment il fonctionne.",
    "Quelles sont les étapes pour créer une petite entreprise en France ?",
]
SHORT = [
    "Quelle est la capitale de l'Australie ?",
    "Traduis en anglais : « Le chat dort sur le canapé. »",
    "Combien font 17 fois 23 ?",
]

WHOLE_SYSTEM = "Réponds en français, de façon complète, juste et bien structurée (Markdown)."

PLAN_SYSTEM = (
    "Tu prépares une réponse qui sera rédigée par plusieurs auteurs en même temps, chacun "
    "écrivant une partie sans voir les autres. Écris seulement le plan-contrat, en JSON :\n"
    "- « contrat » : ce que toutes les parties doivent respecter pour former une seule "
    "réponse cohérente : « fil » (l'idée directrice, une phrase), « termes » (les noms et "
    "définitions à employer partout), « faits » (chiffres, dates, décisions et "
    "recommandations communes, pour qu'aucune partie n'en contredise une autre), « ton » ;\n"
    "- « parties » : entre 2 et 8, dans l'ordre de lecture, chacune avec un « titre », son "
    "« contenu » (ce qu'elle couvre et seulement elle) et une longueur en « mots ». Une "
    "question simple appelle 2 parties courtes ; une introduction ou une conclusion n'est "
    "une partie que si la réponse en a besoin.")

PART_SYSTEM = (
    "Tu écris UNE partie d'une réponse rédigée par plusieurs auteurs en même temps : les "
    "autres parties sont écrites ailleurs, au même moment. N'écris que la tienne. Respecte "
    "le contrat (fil, termes, faits, ton) : ne contredis aucun fait, n'invente pas d'autres "
    "chiffres pour les mêmes choses. Ne traite pas ce que couvrent les autres parties. "
    "Commence directement par le contenu, sans répéter le titre, sans introduction ni "
    "conclusion générale (sauf si c'est ta partie). En français, en Markdown.")

JUDGE_SYSTEM = (
    "Tu compares deux réponses à la même question. Critères : exactitude, complétude, "
    "cohérence d'ensemble (ni répétition ni contradiction), clarté. Réponds par un seul "
    "caractère : A si la réponse A est meilleure, B si c'est la B, = si elles se valent.")

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "contrat": {
            "type": "object",
            "properties": {
                "fil": {"type": "string"},
                "termes": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                "faits": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                "ton": {"type": "string"},
            },
            "required": ["fil", "termes", "faits", "ton"],
        },
        "parties": {
            "type": "array", "minItems": 2, "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {"titre": {"type": "string"}, "contenu": {"type": "string"},
                               "mots": {"type": "integer"}},
                "required": ["titre", "contenu", "mots"],
            },
        },
    },
    "required": ["contrat", "parties"],
}


def ask(url, system, user, max_tokens, schema=None, temperature=0.7) -> dict:
    """Une demande en flux : texte, jetons écrits, temps jusqu'au premier mot et en tout."""
    body = {"messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": max_tokens, "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False}}
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "plan", "schema": schema}}
    began = time.monotonic()
    first, text, tokens = None, [], 0
    with requests.post(url + "/v1/chat/completions", json=body, stream=True, timeout=600,
                       proxies=NO_PROXY) as response:
        response.raise_for_status()
        response.encoding = "utf-8"
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("usage"):
                tokens = chunk["usage"].get("completion_tokens") or tokens
            for choice in chunk.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    first = first if first is not None else time.monotonic() - began
                    text.append(piece)
    return {"text": "".join(text), "tokens": tokens, "first": first,
            "seconds": time.monotonic() - began}


def whole(url, question) -> dict:
    reply = ask(url, WHOLE_SYSTEM, question, max_tokens=1800)
    return {"answer": reply["text"], "tokens": reply["tokens"], "first": reply["first"],
            "seconds": reply["seconds"]}


def _outline(parts, mine=None) -> str:
    return "\n".join(f"{n}. {p['titre']} — {p['contenu']}" + ("   ← TA PARTIE" if n == mine else "")
                     for n, p in enumerate(parts, 1))


def swarm(url, question, workers) -> dict:
    """Le plan-contrat, puis toutes les parties en même temps, assemblées dans l'ordre."""
    began = time.monotonic()
    planned = ask(url, PLAN_SYSTEM, question, max_tokens=900, schema=PLAN_SCHEMA, temperature=0.3)
    plan = json.loads(planned["text"])
    parts, contract = plan["parties"], plan["contrat"]
    plan_seconds = time.monotonic() - began

    def write(n):
        part = parts[n - 1]
        user = (f"Question : {question}\n\nContrat :\n"
                f"{json.dumps(contract, ensure_ascii=False, indent=1)}\n\n"
                f"Plan complet :\n{_outline(parts, n)}\n\n"
                f"Écris la partie {n}, « {part['titre']} », environ {part['mots']} mots.")
        reply = ask(url, PART_SYSTEM, user, max_tokens=min(1200, 40 + 3 * int(part["mots"])))
        reply["done_at"] = time.monotonic() - began
        return reply

    with ThreadPoolExecutor(max_workers=workers) as pool:
        written = list(pool.map(write, range(1, len(parts) + 1)))
    answer = "\n\n".join(f"## {p['titre']}\n\n{w['text'].strip()}" for p, w in zip(parts, written))
    return {"answer": answer, "parts": len(parts), "plan_seconds": plan_seconds,
            "tokens": planned["tokens"] + sum(w["tokens"] for w in written),
            "longest_part": max(w["tokens"] for w in written),
            # Premier mot lisible : la partie 1 s'affiche dès qu'elle commence
            "first": plan_seconds + (written[0]["first"] or 0.0),
            "seconds": time.monotonic() - began, "contract": contract,
            "outline": [p["titre"] for p in parts]}


def judge(url, question, a, b) -> str:
    user = f"Question : {question}\n\n=== Réponse A ===\n{a}\n\n=== Réponse B ===\n{b}"
    verdict = ask(url, JUDGE_SYSTEM, user, max_tokens=3, temperature=0.0)["text"].strip()
    return verdict[:1] if verdict[:1] in ("A", "B", "=") else "?"


def run(url, question, workers) -> dict:
    alone = whole(url, question)
    shared = swarm(url, question, workers)
    # Deux ordres : l'essaim en B, puis en A ; +1 quand l'essaim est préféré
    first, second = judge(url, question, alone["answer"], shared["answer"]), \
        judge(url, question, shared["answer"], alone["answer"])
    score = {"B": 1, "A": -1}.get(first, 0) + {"A": 1, "B": -1}.get(second, 0)
    result = {"question": question, "whole": alone, "swarm": shared,
              "speedup": alone["seconds"] / shared["seconds"], "votes": [first, second],
              "swarm_score": score}
    print(f"{alone['seconds']:6.1f} s seul ({alone['tokens']} jetons) | "
          f"{shared['seconds']:6.1f} s en essaim ({shared['parts']} parties, plan "
          f"{shared['plan_seconds']:.1f} s, {shared['tokens']} jetons) | "
          f"x{result['speedup']:.2f} | juge {first}{second} → {score:+d} | {question[:50]}",
          flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("url", help="llama-server, ex. http://127.0.0.1:8090")
    parser.add_argument("--machines", type=int, default=8, help="parties écrites en même temps")
    parser.add_argument("--json", help="fichier où ajouter les résultats")
    parser.add_argument("--court", action="store_true", help="seulement les questions courtes")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    results = [run(url, q, args.machines) for q in (SHORT if args.court else QUESTIONS + SHORT)]
    long = [r for r in results if r["question"] in QUESTIONS]
    summary = {"when": time.strftime("%Y-%m-%d %H:%M"), "machines": args.machines,
               "speedup_median_long": statistics.median(r["speedup"] for r in long) if long else None,
               "speedup_median_short": statistics.median(r["speedup"] for r in results
                                                          if r["question"] in SHORT),
               "first_word_whole": statistics.median(r["whole"]["first"] or 0 for r in long) if long else None,
               "first_word_swarm": statistics.median(r["swarm"]["first"] for r in long) if long else None,
               "swarm_better": sum(r["swarm_score"] > 0 for r in long),
               "equal": sum(r["swarm_score"] == 0 for r in long),
               "whole_better": sum(r["swarm_score"] < 0 for r in long)}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.json:
        path = Path(args.json)
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        old.append({"summary": summary, "results": results})
        path.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8",
                        newline="\n")


if __name__ == "__main__":
    main()
