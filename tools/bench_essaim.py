"""Mesures pour l'écriture en essaim (docs/conception/essaim.md).

Une réponse écrite d'un trait par un seul modèle, comparée à la même réponse écrite en
essaim. Deux façons d'écrire en essaim :

- « flux » (par défaut) : une machine écrit l'ouverture tout de suite, pendant qu'une autre
  écrit un plan minuscule en texte ; chaque partie part chez une machine dès que sa ligne
  du plan est écrite, sans attendre la fin du plan ;
- « contrat » (première mesure) : un plan-contrat en JSON (les parties, et ce que toutes
  doivent respecter : fil directeur, termes, faits, ton), puis toutes les parties en même
  temps.

Les parties sont assemblées dans l'ordre.

Ici, les machines sont les emplacements parallèles d'un même llama-server (--parallel N) :
sur une seule carte graphique, elles se partagent la carte et se ralentissent un peu ; de
vraies machines séparées iraient au moins aussi vite, à un aller-retour réseau près par
étage (plan, puis parties).

La qualité : le même modèle compare les deux réponses sans savoir laquelle est laquelle,
dans les deux ordres (pour annuler sa préférence de position). C'est un indice, pas un
verdict : un juge de 9 milliards de paramètres se trompe, et il se juge lui-même.

    llama-server -m qwen3.5-9b.gguf --parallel 9 --ctx-size 36864 --port 8090
    python tools/bench_essaim.py http://127.0.0.1:8090 --json tools/bench_essaim_results.json

Le script ne dépend pas du reste d'AIODOS.
"""
import argparse
import json
import re
import statistics
import sys
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
    "une partie que si la réponse en a besoin.\n"
    "Sois très bref : le plan doit s'écrire en quelques secondes (titres de quelques mots, "
    "contenus d'une ligne, 6 termes et 6 faits au plus).")

PART_SYSTEM = (
    "Tu écris UNE partie d'une réponse rédigée par plusieurs auteurs en même temps : les "
    "autres parties sont écrites ailleurs, au même moment. N'écris que la tienne. Respecte "
    "le contrat (fil, termes, faits, ton) : ne contredis aucun fait, n'invente pas d'autres "
    "chiffres pour les mêmes choses. Ne traite pas ce que couvrent les autres parties. "
    "Commence directement par le contenu, sans répéter le titre, sans introduction ni "
    "conclusion générale (sauf si c'est ta partie). En français, en Markdown.")

OPEN_SYSTEM = (
    "Tu écris l'ouverture d'une réponse rédigée par plusieurs auteurs en même temps : les "
    "parties détaillées qui suivront sont écrites ailleurs, au même moment. Écris seulement "
    "l'ouverture : l'essentiel de la réponse en quelques phrases (environ 100 mots), sans "
    "titre, sans détails, sans chiffres précis, sans annoncer de plan ni conclure. En "
    "français.")

FLOW_SYSTEM = (
    "Tu prépares une réponse rédigée par plusieurs auteurs en même temps, chacun écrivant "
    "une partie sans voir les autres ; une ouverture de quelques phrases est déjà en cours "
    "d'écriture. Écris seulement le plan de la suite, en texte, exactement dans ce format :\n"
    "FIL : l'idée directrice, 12 mots au plus\n"
    "FAITS : les chiffres et dates que toutes les parties doivent partager, séparés par "
    "des points-virgules, 15 mots au plus\n"
    "1. titre de 5 mots au plus | ce que couvre cette partie et seulement elle, 10 mots au "
    "plus | nombre de mots de la partie\n"
    "2. …\n"
    "Entre 3 et 6 parties, autant que la question en demande, dans l'ordre de lecture, "
    "sans introduction (l'ouverture la fait). Une réponse complète compte 1 000 à 1 300 "
    "mots en tout : répartis-les entre les parties. Rien d'autre, et ne rédige aucune "
    "partie.")

# La forme du plan en flux, imposée au modèle (grammaire GBNF de llama.cpp) : sans elle, le
# 28/10, qwen3.5:9b écrivait des lignes de 300 caractères, oubliait le nombre de mots, ou se
# mettait à rédiger la partie 1 au milieu du plan, jusqu'à 600 jetons (30 s seul, 80 s en
# partageant la carte). Les longueurs bornées font un plan de 300 jetons au plus.
# Deuxième essai du 28/10 : avec TERMES et des lignes jusqu'à 100 caractères, le modèle
# remplissait toujours 8 parties et butait sur les bornes (lignes coupées au milieu d'un mot),
# 400 jetons. D'où : pas de TERMES, 3 à 6 parties, et les bornes dites dans la consigne.
PLAN_GRAMMAR = r'''root ::= "FIL : " fil "\nFAITS : " facts "\n" part{3,6}
fil ::= [^\n|]{10,90}
facts ::= [^\n|]{3,110}
part ::= [1-6] ". " title " | " what " | " [1-9] [0-9] [0-9]? "\n"
title ::= [^\n|]{3,45}
what ::= [^\n|]{10,80}
'''

# Les lignes du plan en flux, lues pendant qu'il s'écrit (« **1.** », « - FIL : » passent)
FIELD_LINE = re.compile(r"^\W*(FIL|TERMES|FAITS)\W*:\s*(.+)$", re.IGNORECASE)
PART_LINE = re.compile(r"^\W*\d+\s*[.)]\W*(.+?)\s*\|\s*(.+?)\s*\|\D*(\d+)")

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
                "fil": {"type": "string", "maxLength": 160},
                "termes": {"type": "array", "maxItems": 6,
                           "items": {"type": "string", "maxLength": 60}},
                "faits": {"type": "array", "maxItems": 6,
                          "items": {"type": "string", "maxLength": 120}},
                "ton": {"type": "string", "maxLength": 40},
            },
            "required": ["fil", "termes", "faits", "ton"],
        },
        "parties": {
            "type": "array", "minItems": 2, "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {"titre": {"type": "string", "maxLength": 60},
                               "contenu": {"type": "string", "maxLength": 160},
                               "mots": {"type": "integer"}},
                "required": ["titre", "contenu", "mots"],
            },
        },
    },
    "required": ["contrat", "parties"],
}


def ask(url, system, user, max_tokens, schema=None, temperature=0.7, on_text=None,
        grammar=None) -> dict:
    """Une demande en flux : texte, jetons écrits, temps jusqu'au premier mot et en tout.

    on_text reçoit chaque morceau de texte dès qu'il arrive ; grammar : la forme imposée."""
    body = {"messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": max_tokens, "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False}}
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "plan", "schema": schema}}
    if grammar is not None:
        body["grammar"] = grammar
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
                    if on_text is not None:
                        on_text(piece)
    return {"text": "".join(text), "tokens": tokens, "first": first,
            "seconds": time.monotonic() - began}


def whole(url, question) -> dict:
    reply = ask(url, WHOLE_SYSTEM, question, max_tokens=1800)
    return {"answer": reply["text"], "tokens": reply["tokens"], "answer_tokens": reply["tokens"],
            "first": reply["first"], "seconds": reply["seconds"]}


def _outline(parts, mine=None) -> str:
    return "\n".join(f"{n}. {p['titre']} — {p['contenu']}" + ("   ← TA PARTIE" if n == mine else "")
                     for n, p in enumerate(parts, 1))


def swarm(url, question, workers) -> dict:
    """Le plan-contrat, puis toutes les parties en même temps, assemblées dans l'ordre."""
    began = time.monotonic()
    planned = ask(url, PLAN_SYSTEM, question, max_tokens=1500, schema=PLAN_SCHEMA, temperature=0.3)
    plan = json.loads(planned["text"])        # bornes du schéma : il ne peut plus déborder
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
            "answer_tokens": sum(w["tokens"] for w in written),
            "longest_part": max(w["tokens"] for w in written),
            # Premier mot lisible : la partie 1 s'affiche dès qu'elle commence
            "first": plan_seconds + (written[0]["first"] or 0.0),
            "seconds": time.monotonic() - began, "contract": contract,
            "outline": [p["titre"] for p in parts]}


def flow(url, question, workers) -> dict:
    """Le plan en flux : l'ouverture part tout de suite, chaque partie dès que sa ligne est
    écrite ; le plan n'est jamais attendu en entier."""
    began = time.monotonic()
    contract, parts, futures, pending = {}, [], [], [""]

    def write(n, known, terms):
        part = known[n - 1]
        user = (f"Question : {question}\n\nContrat :\n{terms}\n\n"
                f"Plan (d'autres parties peuvent suivre) :\n{_outline(known, n)}\n\n"
                f"Écris la partie {n}, « {part['titre']} », environ {part['mots']} mots.")
        reply = ask(url, PART_SYSTEM, user, max_tokens=min(1000, 60 + 2 * part["mots"]))
        reply["done_at"] = time.monotonic() - began
        return reply

    def line(text):
        if field := FIELD_LINE.match(text):
            contract[field[1].upper()] = field[2].strip()
        elif (part := PART_LINE.match(text)) and len(parts) < 8:
            parts.append({"titre": part[1].strip(" *"), "contenu": part[2].strip(),
                          "mots": max(50, min(400, int(part[3]))),
                          "dispatched": time.monotonic() - began})
            terms = "\n".join(f"{k} : {v}" for k, v in contract.items())
            futures.append(pool.submit(write, len(parts), list(parts), terms))

    def read(piece):
        pending[0] += piece
        *complete, pending[0] = pending[0].split("\n")
        for text in complete:
            line(text)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        opening = pool.submit(ask, url, OPEN_SYSTEM, question, 300)
        planned = ask(url, FLOW_SYSTEM, question, max_tokens=600, temperature=0.3, on_text=read,
                      grammar=PLAN_GRAMMAR)
        line(pending[0])
        plan_seconds = time.monotonic() - began
        opened = opening.result()
        written = [f.result() for f in futures]
    if not parts:
        raise ValueError("aucune partie dans le plan")
    answer = opened["text"].strip() + "\n\n" + "\n\n".join(
        f"## {p['titre']}\n\n{w['text'].strip()}" for p, w in zip(parts, written))
    return {"answer": answer, "parts": len(parts), "plan_seconds": plan_seconds,
            "dispatched": [round(p["dispatched"], 1) for p in parts],
            "tokens": planned["tokens"] + opened["tokens"] + sum(w["tokens"] for w in written),
            "answer_tokens": opened["tokens"] + sum(w["tokens"] for w in written),
            "longest_part": max(w["tokens"] for w in written),
            # L'ouverture part à l'instant zéro : son premier mot est celui de la réponse
            "first": opened["first"],
            "seconds": time.monotonic() - began, "contract": contract,
            "outline": [p["titre"] for p in parts]}


METHODS = {"flux": flow, "contrat": swarm}


def judge(url, question, a, b) -> str:
    user = f"Question : {question}\n\n=== Réponse A ===\n{a}\n\n=== Réponse B ===\n{b}"
    try:
        verdict = ask(url, JUDGE_SYSTEM, user, max_tokens=3, temperature=0.0)["text"].strip()
    except requests.HTTPError:          # deux réponses trop longues pour une place : noté
        return "?"
    return verdict[:1] if verdict[:1] in ("A", "B", "=") else "?"


def run(url, question, workers, method=flow) -> dict | None:
    alone = whole(url, question)
    try:
        shared = method(url, question, workers)
    except (ValueError, KeyError) as exc:      # plan illisible : noté, la mesure continue
        print(f"plan illisible ({exc}) | {question[:50]}", flush=True)
        return None
    # Deux ordres : l'essaim en B, puis en A ; +1 quand l'essaim est préféré
    first, second = judge(url, question, alone["answer"], shared["answer"]), \
        judge(url, question, shared["answer"], alone["answer"])
    score = {"B": 1, "A": -1}.get(first, 0) + {"A": 1, "B": -1}.get(second, 0)
    result = {"question": question, "whole": alone, "swarm": shared,
              "speedup": alone["seconds"] / shared["seconds"], "votes": [first, second],
              "swarm_score": score}
    print(f"{alone['seconds']:6.1f} s seul ({alone['answer_tokens']} jetons) | "
          f"{shared['seconds']:6.1f} s en essaim ({shared['parts']} parties, plan "
          f"{shared['plan_seconds']:.1f} s, {shared['answer_tokens']} jetons, premier mot "
          f"{shared['first'] or 0:.1f} s) | x{result['speedup']:.2f} | "
          f"juge {first}{second} → {score:+d} | {question[:50]}", flush=True)
    return result


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("url", help="llama-server, ex. http://127.0.0.1:8090")
    parser.add_argument("--machines", type=int, default=8, help="parties écrites en même temps")
    parser.add_argument("--json", help="fichier où ajouter les résultats")
    parser.add_argument("--court", action="store_true", help="seulement les questions courtes")
    parser.add_argument("--methode", choices=sorted(METHODS), default="flux",
                        help="façon d'écrire en essaim")
    args = parser.parse_args()
    # Sortie redirigée vers un fichier sous Windows : cp1252, qui n'a pas « → »
    sys.stdout.reconfigure(encoding="utf-8")
    url = args.url.rstrip("/")
    results = [r for q in (SHORT if args.court else QUESTIONS + SHORT)
               if (r := run(url, q, args.machines, METHODS[args.methode])) is not None]
    long = [r for r in results if r["question"] in QUESTIONS]
    summary = {"when": time.strftime("%Y-%m-%d %H:%M"), "machines": args.machines,
               "method": args.methode,
               "speedup_median_long": _median(r["speedup"] for r in long),
               "speedup_median_short": _median(r["speedup"] for r in results
                                               if r["question"] in SHORT),
               "first_word_whole": _median(r["whole"]["first"] for r in long),
               "first_word_swarm": _median(r["swarm"]["first"] for r in long),
               "tokens_whole": _median(r["whole"]["answer_tokens"] for r in long),
               "tokens_swarm": _median(r["swarm"]["answer_tokens"] for r in long),
               # Vitesse à longueur égale : jetons de la réponse par seconde
               "rate_whole": _median(r["whole"]["answer_tokens"] / r["whole"]["seconds"]
                                     for r in long),
               "rate_swarm": _median(r["swarm"]["answer_tokens"] / r["swarm"]["seconds"]
                                     for r in long),
               "plan_seconds": _median(r["swarm"]["plan_seconds"] for r in long),
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
