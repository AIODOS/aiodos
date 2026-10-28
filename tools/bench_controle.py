"""Mesures pour les contrôles au hasard du réseau public (docs/conception/reseau-public-calcul.md
§ 8) : de quoi distinguer, en comparant deux réponses à la même demande, une machine honnête
d'une machine qui triche.

Paires **honnêtes** (le même modèle, qwen3.5:9b) :

- « rejeu » : la même demande, même graine, même température (0,7), même carte ;
- « autre carte » : même graine et même température, mais une autre façon de calculer (une
  partie des couches sur le processeur, --n-gpu-layers) : les nombres diffèrent un peu, et
  les deux textes divergent après quelques dizaines de jetons, comme entre deux cartes ;
- « graine » : une autre graine (pire cas : deux échantillons indépendants du même modèle) ;
- « glouton » : température 0, autre façon de calculer.

Paires **tricheuses** : un autre modèle (qwen2.5:7b, mistral:7b), la réponse tronquée (35 %
du texte, sans dire qu'elle est coupée), une réponse sans rapport (celle d'une autre question),
la réponse à laquelle on ajoute une publicité (avec un lien, un courriel ou un numéro, ou sans
rien de tout cela), à la fin ou au milieu.

Chaque paire compare la réponse A (celle du prêteur) à B (celle de la machine de contrôle,
« autre carte »). Mesures : préfixe commun, longueurs, mots de A retrouvés dans B, trigrammes,
liens nouveaux, similarité des embeddings (plusieurs modèles d'embedding : chacun a sa propre
échelle), et la **vraisemblance** de A sous le modèle : une lecture forcée (grammaire GBNF qui
impose le texte, probabilités des jetons avant échantillonnage ; llama-server refuse « echo »,
donc pas de probabilités des jetons de la demande), dont la dernière entrée est la fin de texte
imposée : sa probabilité dit si le modèle se serait arrêté là.

Le déroulé (un serveur à la fois sur une carte de 8 Go) :

    llama-server -m qwen3.5-9b.gguf --parallel 4 --ctx-size 24576 --port 8090
    python tools/bench_controle.py generer http://127.0.0.1:8090 -e ref:1:0.7 -e rejeu:1:0.7 \
        -e graine2:2:0.7 -e glouton:1:0 --travail travail.json
    (qwen2.5:7b, mistral:7b) generer … -e qwen25:1:0.7 / -e mistral:1:0.7
    (qwen3.5:9b, --n-gpu-layers 20) generer … -e autre_carte:1:0.7 -e glouton_autre:1:0
    (qwen3.5:9b) python tools/bench_controle.py lire http://127.0.0.1:8090 --travail travail.json
    (llama-server --embeddings) python tools/bench_controle.py embeddings URL --nom bge-m3 …
    python tools/bench_controle.py analyser --travail travail.json \
        --json tools/bench_controle_results.json

Le script ne dépend pas du reste d'AIODOS.
"""
import argparse
import json
import math
import re
import statistics
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

NO_PROXY = {"http": None, "https": None}
MAX_TOKENS = 400
SLOTS = 4

# Vingt questions variées, courtes et longues
QUESTIONS = [
    "Quelle est la capitale de l'Australie ?",
    "Combien font 17 fois 23 ?",
    "Traduis en anglais : « Le chat dort sur le canapé depuis ce matin. »",
    "Explique comment fonctionne la photosynthèse.",
    "Quelles sont les causes de la Révolution française ?",
    "Donne trois conseils pour mieux dormir.",
    "Écris un petit poème sur l'automne.",
    "Comment faire une pâte à crêpes ?",
    "Qu'est-ce qu'un trou noir ?",
    "Écris une fonction Python qui inverse une chaîne de caractères.",
    "Quelle est la différence entre un virus et une bactérie ?",
    "Résume l'histoire du Petit Chaperon rouge en quelques phrases.",
    "Comment fonctionne le chiffrement à clé publique ?",
    "Quels sont les avantages et les inconvénients du télétravail ?",
    "Qui a peint la Joconde, et quand ?",
    "Propose un menu végétarien pour un dîner entre amis.",
    "Explique la règle de trois avec un exemple.",
    "Pourquoi le ciel est-il bleu ?",
    "Rédige un court courriel pour décaler une réunion à jeudi.",
    "Comment préparer un entretien d'embauche ?",
]

# Publicités ajoutées (marques inventées) : avec un lien, un courriel ou un numéro, ou sans
ADS_LINK = [
    "👉 Pour aller plus loin, découvrez notre offre exclusive sur "
    "https://promo-express.example/offre?ref=ia : -50 % aujourd'hui seulement !",
    "Bon plan : les meilleurs prix de l'année sont sur www.meilleurs-prix.example, profitez-en.",
    "Besoin d'un crédit rapide ? Appelez le 06 12 34 56 78, réponse en 24 h.",
    "Pour recevoir nos astuces gratuites, écrivez à contact@bonplan-malin.example.",
]
ADS_PLAIN = [
    "Au fait, pour vos courses de la semaine, pensez aux supermarchés Prixdoux : des prix "
    "bas toute l'année, et la livraison offerte dès 30 euros.",
    "Ce conseil vous est offert par BonCrédit Express, le prêt rapide sans justificatif.",
    "Envie de vacances ? Les croisières Azur Horizon vous attendent cet été à prix réduit.",
    "Et pour vous détendre, rien ne vaut une boisson Fizzola bien fraîche !",
]
CUT = 0.35                     # part du texte gardée par la réponse tronquée
UNRELATED_SHIFT = 7            # la réponse « sans rapport » : celle de la question i + 7

DERIVED = ("tronquee", "sans_rapport", "pub_lien", "pub_sans_lien")

# (nom, genre, A : réponse du prêteur, B : réponse de la machine de contrôle)
PAIRS = [
    ("rejeu", "honnête", "rejeu", "ref"),
    ("autre carte", "honnête", "ref", "autre_carte"),
    ("graine", "honnête", "graine2", "autre_carte"),
    ("glouton, autre carte", "honnête", "glouton", "glouton_autre"),
    ("qwen2.5:7b", "tricheur", "qwen25", "autre_carte"),
    ("mistral:7b", "tricheur", "mistral", "autre_carte"),
    ("qwen2.5:7b glouton", "tricheur", "qwen25_glouton", "glouton_autre"),
    ("mistral:7b glouton", "tricheur", "mistral_glouton", "glouton_autre"),
    ("tronquée", "tricheur", "tronquee", "autre_carte"),
    ("sans rapport", "tricheur", "sans_rapport", "autre_carte"),
    ("pub avec lien", "tricheur", "pub_lien", "autre_carte"),
    ("pub sans lien", "tricheur", "pub_sans_lien", "autre_carte"),
]
# Textes lus sous le modèle (vraisemblance) : ce que A peut être, et B pour repère
READ_LABELS = ("ref", "graine2", "autre_carte", "qwen25", "mistral", *DERIVED)


# ------------------------------------------------------------------ travail
def load(path) -> dict:
    path = Path(path)
    work = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    work.setdefault("answers", {})
    work.setdefault("reads", {})
    work.setdefault("cos", {})
    derive(work)
    return work


def save(path, work):
    Path(path).write_text(json.dumps(work, ensure_ascii=False, indent=1), encoding="utf-8",
                          newline="\n")


def _cut(text, share) -> str:
    """Les premiers `share` du texte, coupés à un blanc."""
    end = int(len(text) * share)
    space = text.rfind(" ", 0, end)
    return text[:space if space > 0 else end].rstrip()


def _insert(text, ad, middle) -> str:
    """La publicité à la fin, ou au milieu (à la fin d'un paragraphe proche du milieu)."""
    if middle:
        cut = text.find("\n\n", len(text) // 2)
        if cut > 0:
            return text[:cut] + "\n\n" + ad + text[cut:]
    return text.rstrip() + "\n\n" + ad


def derive(work):
    """Les réponses tricheuses tirées de « ref » : tronquée, sans rapport, publicités."""
    ref = work["answers"].get("ref")
    if not ref or len(ref) != len(QUESTIONS):
        return
    n = len(QUESTIONS)
    work["answers"]["tronquee"] = [{"text": _cut(a["text"], CUT), "finish": "stop"} for a in ref]
    work["answers"]["sans_rapport"] = [dict(ref[(i + UNRELATED_SHIFT) % n]) for i in range(n)]
    work["answers"]["pub_lien"] = [
        {"text": _insert(a["text"], ADS_LINK[i % len(ADS_LINK)], i % 2 == 1),
         "finish": a["finish"]} for i, a in enumerate(ref)]
    work["answers"]["pub_sans_lien"] = [
        {"text": _insert(a["text"], ADS_PLAIN[i % len(ADS_PLAIN)], i % 2 == 0),
         "finish": a["finish"]} for i, a in enumerate(ref)]


# ------------------------------------------------------------------ générer
def ask(url, question, seed, temperature) -> dict:
    body = {"messages": [{"role": "user", "content": question}],
            "max_tokens": MAX_TOKENS, "temperature": temperature, "seed": seed,
            "chat_template_kwargs": {"enable_thinking": False}}
    began = time.monotonic()
    r = requests.post(url + "/v1/chat/completions", json=body, timeout=900, proxies=NO_PROXY)
    r.raise_for_status()
    data = r.json()
    choice = data["choices"][0]
    return {"text": choice["message"].get("content") or "",
            "finish": choice.get("finish_reason"),
            "tokens": (data.get("usage") or {}).get("completion_tokens"),
            "seconds": round(time.monotonic() - began, 2)}


def generate(args):
    work = load(args.travail)
    url = args.url.rstrip("/")
    for spec in args.etiquette:
        label, seed, temperature = spec.split(":")
        began = time.monotonic()
        with ThreadPoolExecutor(max_workers=SLOTS) as pool:
            answers = list(pool.map(lambda q: ask(url, q, int(seed), float(temperature)),
                                    QUESTIONS))
        work["answers"][label] = answers
        derive(work)
        save(args.travail, work)
        tokens = sum(a["tokens"] or 0 for a in answers)
        print(f"{label} : {len(answers)} réponses, {tokens} jetons en "
              f"{time.monotonic() - began:.0f} s", flush=True)


# --------------------------------------------------------------------- lire
def gbnf_literal(text) -> str:
    out = []
    for ch in text:
        if ch in '\\"':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def read_one(url, question, text) -> dict:
    """La lecture forcée du texte sous le modèle : log-probabilités de ses jetons (avant
    échantillonnage), et celle de la fin de texte imposée par la grammaire."""
    body = {"messages": [{"role": "user", "content": question}],
            "max_tokens": 3 * MAX_TOKENS, "temperature": 0.0,
            "grammar": "root ::= " + gbnf_literal(text),
            "logprobs": True, "top_logprobs": 1,
            "chat_template_kwargs": {"enable_thinking": False}}
    began = time.monotonic()
    r = requests.post(url + "/v1/chat/completions", json=body, timeout=900, proxies=NO_PROXY)
    r.raise_for_status()
    choice = r.json()["choices"][0]
    entries = (choice.get("logprobs") or {}).get("content") or []
    eos = None
    if entries and entries[-1]["token"] == "":
        eos = entries[-1]["logprob"]
        entries = entries[:-1]
    return {"lp": [round(e["logprob"], 3) for e in entries], "eos": eos,
            "same": (choice["message"].get("content") or "") == text,
            "seconds": round(time.monotonic() - began, 2)}


def read(args):
    work = load(args.travail)
    url = args.url.rstrip("/")
    labels = args.etiquettes.split(",") if args.etiquettes else READ_LABELS
    for label in labels:
        answers = work["answers"].get(label)
        if not answers:
            print(f"{label} : pas de réponses, sauté", flush=True)
            continue
        began = time.monotonic()
        with ThreadPoolExecutor(max_workers=SLOTS) as pool:
            reads = list(pool.map(lambda pair: read_one(url, pair[0], pair[1]["text"]),
                                  zip(QUESTIONS, answers)))
        work["reads"][label] = reads
        save(args.travail, work)
        print(f"{label} : lu en {time.monotonic() - began:.0f} s, "
              f"{sum(len(r['lp']) for r in reads)} jetons, "
              f"{sum(not r['same'] for r in reads)} textes rendus autrement", flush=True)


# --------------------------------------------------------------- embeddings
def embed_texts(url, texts) -> list:
    vectors = []
    for start in range(0, len(texts), 8):
        r = requests.post(url + "/v1/embeddings", json={"input": texts[start:start + 8]},
                          timeout=900, proxies=NO_PROXY)
        r.raise_for_status()
        vectors.extend(item["embedding"] for item in
                       sorted(r.json()["data"], key=lambda item: item["index"]))
    return vectors


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def embeddings(args):
    work = load(args.travail)
    url = args.url.rstrip("/")
    labels = sorted({p[2] for p in PAIRS} | {p[3] for p in PAIRS})
    labels = [label for label in labels if label in work["answers"]]
    began = time.monotonic()
    texts = [a["text"] or " " for label in labels for a in work["answers"][label]]
    flat = embed_texts(url, texts)
    vectors = {label: flat[k * len(QUESTIONS):(k + 1) * len(QUESTIONS)]
               for k, label in enumerate(labels)}
    found = work["cos"].setdefault(args.nom, {})
    for name, _kind, a, b in PAIRS:
        if a in vectors and b in vectors:
            found[name] = [round(cosine(x, y), 4) for x, y in zip(vectors[a], vectors[b])]
    save(args.travail, work)
    print(f"{args.nom} : {len(texts)} textes en {time.monotonic() - began:.0f} s", flush=True)


# ----------------------------------------------------------------- mesures
_WORD = re.compile(r"\w+")
# Liens, courriels, domaines nus (en minuscules) et numéros, comparés par domaine
# enregistrable (comme aiodos/network/spot_check.py)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"'«»]+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_DOMAIN = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|fr|net|org|io|ly|me|co|info|biz|shop|xyz|"
                     r"app|eu|be|ch|ca|de|uk|us|tv|gg|link|click|online|site|store|top|"
                     r"example))\b")
_PHONE = re.compile(r"(?:\+\d{2}[\s.-]?|\b0)[1-9](?:[\s.-]?\d{2}){4}\b")
_SECOND_LEVEL = {"co", "com", "org", "net", "gouv", "gov", "ac", "edu"}
_UNFINISHED = re.compile(r"(?:[\w,;:(\[{=«\-–—]|(?:^|\n)\s*[*+-])\s*$")


def fold(text) -> str:
    """Minuscules, sans accents, blancs réduits."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split())


def prefix_chars(a, b) -> int:
    a, b = " ".join(a.split()), " ".join(b.split())
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def words(text) -> set:
    return {w for w in _WORD.findall(fold(text)) if len(w) >= 3}


def contained(a, b, question) -> float:
    """Part des mots de A (3 lettres et plus) qui se retrouvent dans B ou la question."""
    mine = words(a)
    return len(mine & (words(b) | words(question))) / len(mine) if mine else 1.0


def trigrams(text) -> Counter:
    text = fold(text)
    return Counter(text[i:i + 3] for i in range(len(text) - 2))


def trigram_cosine(a, b) -> float:
    ta, tb = trigrams(a), trigrams(b)
    dot = sum(v * tb.get(k, 0) for k, v in ta.items())
    na = math.sqrt(sum(v * v for v in ta.values()))
    nb = math.sqrt(sum(v * v for v in tb.values()))
    return dot / (na * nb) if na and nb else 0.0


def _domain(address) -> str:
    host = re.sub(r"^(?:https?://)?", "", address.casefold())
    host = re.split(r"[/?#:]", host)[0].rstrip(".,;:!?")
    labels = [label for label in host.split(".") if label]
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def links(text) -> set:
    found = {_domain(m.group()) for m in _URL.finditer(text)}
    found |= {_domain(m.group(1)) for m in _EMAIL.finditer(text)}
    found |= {_domain(m.group(1)) for m in _DOMAIN.finditer(text)}
    found |= {re.sub(r"\D", "", m.group())[-9:] for m in _PHONE.finditer(text)}
    return {f for f in found if f}


def new_links(a, b, question) -> int:
    return len(links(a) - links(b) - links(question))


def unfinished(text) -> bool:
    return bool(_UNFINISHED.search(text.rstrip()))


CLIP = -10.0                   # une log-probabilité bornée : un découpage en jetons inhabituel
LOW = -6.0                     # un jeton « très surprenant » pour le modèle
WINDOW = 16                    # fenêtre glissante (jetons)


def read_measures(read) -> dict:
    lp = [max(CLIP, x) for x in read["lp"]] or [0.0]
    windows = [sum(lp[i:i + WINDOW]) / WINDOW for i in range(max(1, len(lp) - WINDOW + 1))] \
        if len(lp) >= WINDOW else [sum(lp) / len(lp)]
    return {"ll_mean": sum(lp) / len(lp), "ll_n": len(read["lp"]),
            "ll_low": sum(x < LOW for x in read["lp"]) / max(1, len(read["lp"])),
            "ll_window": min(windows),
            "ll_eos": read["eos"] if read["eos"] is not None else 0.0}


def measures(work, name, a_label, b_label) -> list:
    rows = []
    a_all, b_all = work["answers"].get(a_label), work["answers"].get(b_label)
    if not a_all or not b_all:
        return rows
    for i, (q, a, b) in enumerate(zip(QUESTIONS, a_all, b_all)):
        ta, tb = a["text"], b["text"]
        row = {"q": i, "prefix": prefix_chars(ta, tb),
               "prefix_words": len(_common_words(ta, tb)),
               "len_ratio": len(ta) / max(1, len(tb)),
               "contained": contained(ta, tb, q),
               "trigram": trigram_cosine(ta, tb),
               "new_links": new_links(ta, tb, q), "unfinished": int(unfinished(ta)),
               "a_finish": a.get("finish"), "b_finish": b.get("finish")}
        for model, pairs in work["cos"].items():
            if name in pairs:
                row["cos_" + model] = pairs[name][i]
        read = (work["reads"].get(a_label) or [None] * len(QUESTIONS))[i]
        if read is not None:
            row.update(read_measures(read))
            # A est-il plus improbable que B, sous le même modèle ?
            read_b = (work["reads"].get(b_label) or [None] * len(QUESTIONS))[i]
            if read_b is not None:
                row["ll_gap"] = row["ll_mean"] - read_measures(read_b)["ll_mean"]
        rows.append(row)
    return rows


def _common_words(a, b) -> list:
    out = []
    for x, y in zip(a.split(), b.split()):
        if x != y:
            break
        out.append(x)
    return out


# ----------------------------------------------------------------- analyser
NUMERIC = ("prefix", "prefix_words", "len_ratio", "contained", "trigram", "new_links",
           "unfinished", "ll_mean", "ll_low", "ll_window", "ll_eos", "ll_gap")


def best_threshold(honest, cheat, higher_is_honest=True):
    """Le seuil qui fait le moins d'erreurs (aucun faux tricheur d'abord) : → (seuil,
    faux tricheurs, tricheurs manqués)."""
    candidates = sorted(set(honest) | set(cheat))
    best = None
    for t in candidates:
        if higher_is_honest:
            fp = sum(v < t for v in honest)
            fn = sum(v >= t for v in cheat)
        else:
            fp = sum(v > t for v in honest)
            fn = sum(v <= t for v in cheat)
        key = (fp > 0, fp + fn, fp)
        if best is None or key < best[0]:
            best = (key, t, fp, fn)
    return best[1:] if best else (None, 0, 0)


def analyse(args):
    work = load(args.travail)
    table = {}
    for name, kind, a, b in PAIRS:
        rows = measures(work, name, a, b)
        if rows:
            table[name] = {"kind": kind, "rows": rows}
    keys = list(NUMERIC) + sorted({k for t in table.values() for r in t["rows"] for k in r
                                   if k.startswith("cos_")})
    summary = {}
    print(f"{'paire':24s} " + " ".join(f"{k[:11]:>23s}" for k in keys))
    for name, t in table.items():
        cells, stats = [], {}
        for k in keys:
            values = [r[k] for r in t["rows"] if r.get(k) is not None]
            if not values:
                cells.append(f"{'—':>23s}")
                continue
            stats[k] = {"min": round(min(values), 3), "med": round(statistics.median(values), 3),
                        "max": round(max(values), 3)}
            cells.append(f"{min(values):7.2f}/{statistics.median(values):7.2f}/"
                         f"{max(values):7.2f}")
        summary[name] = {"kind": t["kind"], **stats}
        print(f"{name[:24]:24s} " + " ".join(cells))
    # Chaque mesure seule : le meilleur seuil, tricheurs de toutes sortes confondus
    print("\nmesure seule : seuil, faux tricheurs / honnêtes, tricheurs manqués / tricheurs")
    single = {}
    for k in keys:
        honest = [r[k] for t in table.values() if t["kind"] == "honnête" for r in t["rows"]
                  if r.get(k) is not None]
        cheat = [r[k] for t in table.values() if t["kind"] == "tricheur" for r in t["rows"]
                 if r.get(k) is not None]
        if not honest or not cheat:
            continue
        higher = k not in ("ll_low", "new_links", "unfinished")
        t, fp, fn = best_threshold(honest, cheat, higher)
        single[k] = {"threshold": t, "false_cheaters": fp, "honest": len(honest),
                     "missed": fn, "cheaters": len(cheat)}
        print(f"  {k:28s} {t!s:>10.10s} {fp:3d}/{len(honest)}  {fn:3d}/{len(cheat)}")
    result = {"when": time.strftime("%Y-%m-%d %H:%M"), "max_tokens": MAX_TOKENS,
              "pairs": summary, "single": single, "rules": rules(table)}
    print(json.dumps(result["rules"], ensure_ascii=False, indent=1))
    if args.json:
        path = Path(args.json)
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        old.append({"summary": result, "rows": {n: t["rows"] for n, t in table.items()},
                    "answers": work["answers"]})
        path.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8",
                        newline="\n")


# Les règles candidates (celles d'aiodos/network/spot_check.py, recopiées : le banc ne dépend
# pas d'AIODOS). True : honnête.
TRUNCATED_RATIO = 0.5
UNRELATED_COS = {"qwen3-embedding": 0.58, "bge-m3": 0.67, "nomic-embed-text": 0.735}
UNRELATED_WORDS = 0.15
EOS_MIN = -7.0                 # fin de texte d'une réponse arrêtée d'elle-même, au plus bas
LIMIT_SHARE = 0.9              # au-delà de 90 % de MAX_TOKENS jetons : coupée par la limite
GAP_MIN = -0.28                # moyenne de A moins celle de B, lues de la même façon
UNLIKELY_MEAN = -1.3           # moyenne de A seule (sans lecture de B)
WINDOW_MIN = -4.0              # pire fenêtre de WINDOW jetons


def _text_rule(row, model):
    if row["new_links"] or (row["len_ratio"] < TRUNCATED_RATIO and row["unfinished"]):
        return False
    if model is None:
        return row["contained"] >= UNRELATED_WORDS
    return row["cos_" + model] >= UNRELATED_COS[model]


def _read_rule(row, gap=True):
    if "ll_mean" not in row:
        return None
    if row["ll_n"] < LIMIT_SHARE * MAX_TOKENS and row["ll_eos"] < EOS_MIN:
        return False
    if gap and "ll_gap" in row:
        if row["ll_gap"] < GAP_MIN:
            return False
    elif row["ll_mean"] < UNLIKELY_MEAN:
        return False
    return row["ll_window"] >= WINDOW_MIN


RULES = {
    "texte, mots communs": lambda r: _text_rule(r, None),
    **{f"texte, {m}": (lambda r, m=m: _text_rule(r, m) if "cos_" + m in r else None)
       for m in UNRELATED_COS},
    "lecture de A seule": lambda r: _read_rule(r, gap=False),
    "lecture de A et de B": _read_rule,
    "texte (qwen3-embedding) + lecture de A et de B": lambda r: None if _read_rule(r) is None
    else (_text_rule(r, "qwen3-embedding") and _read_rule(r)),
}


def rules(table) -> dict:
    """Les règles candidates, appliquées à toutes les paires : erreurs par paire (faux
    tricheurs pour une paire honnête, tricheurs manqués sinon)."""
    out = {}
    for rule_name, rule in RULES.items():
        per = {}
        for name, t in table.items():
            verdicts = [rule(r) for r in t["rows"]]
            wrong = sum(v != (t["kind"] == "honnête") for v in verdicts if v is not None)
            counted = sum(v is not None for v in verdicts)
            if counted:
                per[name] = f"{wrong}/{counted}"
        out[rule_name] = per
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    g = sub.add_parser("generer", help="réponses d'un modèle (étiquette:graine:température)")
    g.add_argument("url")
    g.add_argument("-e", "--etiquette", action="append", required=True)
    r = sub.add_parser("lire", help="vraisemblance des réponses sous le modèle du serveur")
    r.add_argument("url")
    r.add_argument("--etiquettes", help="séparées par des virgules (défaut : READ_LABELS)")
    e = sub.add_parser("embeddings", help="similarité des paires (serveur --embeddings)")
    e.add_argument("url")
    e.add_argument("--nom", required=True, help="nom du modèle d'embedding")
    a = sub.add_parser("analyser", help="tableau des mesures et règles candidates")
    a.add_argument("--json", help="fichier où ajouter les résultats")
    for p in (g, r, e, a):
        p.add_argument("--travail", required=True, help="fichier de travail (réponses, mesures)")
    args = parser.parse_args()
    # Sortie redirigée vers un fichier sous Windows : cp1252, qui n'a pas « → »
    sys.stdout.reconfigure(encoding="utf-8")
    {"generer": generate, "lire": read, "embeddings": embeddings, "analyser": analyse}[
        args.action](args)


if __name__ == "__main__":
    main()
