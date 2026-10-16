"""Compare des modèles d'embedding d'Ollama sur un jeu de test en français.

Pour chaque modèle : score des bons passages, « bruit de fond » des passages
sans rapport, écart entre les deux, seuil de pertinence conseillé
(rag_min_score), qualité du classement, vitesse et place occupée en VRAM.

    python tools/embedding_bench.py                         # modèles par défaut
    python tools/embedding_bench.py bge-m3 nomic-embed-text
    python tools/embedding_bench.py --prefixes              # + préfixes conseillés
    python tools/embedding_bench.py --json mesures.json     # résultats détaillés

Ollama doit tourner et les modèles être installés (ollama pull …). Le script
ne dépend pas du reste d'AIODOS : pas besoin de lancer le nœud.

Un seul modèle est chargé à la fois : tout modèle déjà en mémoire est déchargé
avant chaque mesure, et le modèle mesuré l'est juste après. Avec --json, chaque
résultat est enregistré dès qu'il est connu ; une nouvelle exécution saute les
mesures déjà faites (--force pour les refaire).
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import requests

DATASET = Path(__file__).with_name("embedding_bench_fr.json")
MODELS = ["nomic-embed-text", "bge-m3", "snowflake-arctic-embed2", "embeddinggemma",
          "qwen3-embedding:0.6b"]
# Préfixes recommandés par les auteurs des modèles (question, passage). AIODOS
# n'en met aucun : --prefixes mesure ce qu'ils apporteraient.
PREFIXES = {
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "snowflake-arctic-embed2": ("query: ", ""),
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
    "qwen3-embedding:0.6b": ("Instruct: Given a web search query, retrieve relevant "
                             "passages that answer the query\nQuery:", ""),
}
BATCH = 16                      # comme aiodos.services.ollama.embed
MEMORY_PREFIX = "Utilisateur : "  # forme d'un message dans la mémoire des conversations


# ------------------------------------------------------------------ Ollama
def embed(base, model, texts):
    vectors = []
    for start in range(0, len(texts), BATCH):
        r = requests.post(base + "/api/embed",
                          json={"model": model, "input": texts[start:start + BATCH]},
                          timeout=600)
        r.raise_for_status()
        vectors.extend(r.json()["embeddings"])
    v = np.asarray(vectors, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def running(base) -> list:
    return requests.get(base + "/api/ps", timeout=10).json().get("models", [])


def unload_all(base):
    """Décharge tous les modèles en mémoire (keep_alive 0, sans rien générer)."""
    for m in running(base):
        requests.post(base + "/api/generate", json={"model": m["name"], "keep_alive": 0},
                      timeout=120)
    deadline = time.monotonic() + 30
    while running(base) and time.monotonic() < deadline:
        time.sleep(0.5)


def loaded(base, model):
    for m in running(base):
        if m["name"].split(":")[0] == model.split(":")[0]:
            return m
    return {}


def disk_size(base, model):
    want = model if ":" in model else model + ":latest"
    for m in requests.get(base + "/api/tags", timeout=10).json().get("models", []):
        if m["name"] == want:
            return m["size"]
    return 0


# ------------------------------------------------------------------ mesures
def pct(values, q):
    return float(np.percentile(values, q)) if len(values) else float("nan")


def auc(pos, neg):
    """Probabilité qu'un bon passage score plus haut qu'un passage sans rapport :
    c'est exactement ce que demande un seuil unique pour toutes les questions."""
    pos, neg = np.asarray(pos)[:, None], np.asarray(neg)[None, :]
    return float(((pos > neg).sum() + 0.5 * (pos == neg).sum()) / (pos.size * neg.size))


def best_threshold(pos, neg):
    """Seuil qui sépare le mieux bons passages et bruit (indice de Youden).
    Quand les deux nuages ne se chevauchent pas, on prend le milieu du vide."""
    pos, neg = np.asarray(pos), np.asarray(neg)
    if pos.min() > neg.max():
        return float((pos.min() + neg.max()) / 2)
    grid = np.unique(np.concatenate([pos, neg]))
    scores = [(pos >= t).mean() - (neg >= t).mean() for t in grid]
    return float(grid[int(np.argmax(scores))])


def evaluate(data, P, Q, O, D):
    """P passages, Q questions avec réponse, O questions hors sujet,
    D « Utilisateur : <question> » (même question déjà posée, en mémoire)."""
    ids = list(data["passages"])
    index = {pid: i for i, pid in enumerate(ids)}
    S, T = Q @ P.T, O @ P.T
    rel, far, near = [], [], []
    rel_cases, far_cases = [], []          # (score, question, passage) : cas difficiles
    top1 = mrr = recall4 = 0
    best_rel, dup_gap, dup = [], [], []
    for i, item in enumerate(data["questions"]):
        good = {index[p] for p in item["relevant"]}
        close = {index[p] for p in item.get("near", [])}
        for j in range(len(ids)):
            (rel if j in good else near if j in close else far).append(float(S[i, j]))
            if j in good or j not in close:
                (rel_cases if j in good else far_cases).append((float(S[i, j]), item["q"], ids[j]))
        order = list(np.argsort(-S[i]))
        rank = min(order.index(j) for j in good) + 1
        top1 += rank == 1
        mrr += 1 / rank
        recall4 += rank <= 4
        best_rel.append(max(float(S[i, j]) for j in good))
        d = float(Q[i] @ D[i])
        dup.append(d)
        dup_gap.append(d - min(float(S[i, j]) for j in good))
    off_near = []
    for i, item in enumerate(data["off_topic"]):
        close = {index[p] for p in item.get("near", [])}
        for j in range(len(ids)):
            (off_near if j in close else far).append(float(T[i, j]))
            if j not in close:
                far_cases.append((float(T[i, j]), item["q"], ids[j]))
    noise = far + near + off_near
    threshold = best_threshold(rel, far)
    n = len(data["questions"])
    return {
        "rel": {"min": min(rel), "p10": pct(rel, 10), "median": pct(rel, 50),
                "max": max(rel)},
        "noise": {"median": pct(far, 50), "p90": pct(far, 90), "p99": pct(far, 99),
                  "max": max(far)},
        "near": {"median": pct(near + off_near, 50), "max": max(near + off_near)},
        "gap": min(rel) - max(far),               # > 0 : un seuil unique suffit
        "auc": auc(rel, far), "auc_all": auc(rel, noise),
        "threshold": threshold,
        "at_threshold": {
            "missed_questions": int(sum(b < threshold for b in best_rel)),
            "noise_above": int(sum(s >= threshold for s in far)),
            "near_above": int(sum(s >= threshold for s in near + off_near)),
            "off_topic_with_hit": int(sum(T[i].max() >= threshold
                                          for i in range(len(data["off_topic"])))),
        },
        "off_topic_max": {"median": pct(T.max(axis=1), 50), "max": float(T.max())},
        "top1": top1 / n, "mrr": mrr / n, "recall4": recall4 / n,
        "dup": {"median": pct(dup, 50), "min": min(dup),
                "gap_max": max(dup_gap), "gap_median": pct(dup_gap, 50)},
        "counts": {"rel": len(rel), "far": len(far), "near": len(near + off_near)},
        "hard": {"weakest_relevant": [[q, p, s] for s, q, p in sorted(rel_cases)[:5]],
                 "strongest_noise": [[q, p, s] for s, q, p in sorted(far_cases)[-5:][::-1]]},
        # Ce que donnerait chaque seuil : bons passages perdus, questions restées
        # sans réponse, passages sans rapport (et pièges proches) retenus.
        "sweep": [{"t": round(float(t), 2),
                   "rel_lost": int(sum(s < t for s in rel)),
                   "missed_questions": int(sum(b < t for b in best_rel)),
                   "noise_above": int(sum(s >= t for s in far)),
                   "near_above": int(sum(s >= t for s in near + off_near))}
                  for t in np.arange(0.20, 0.755, 0.01)],
    }


def measure(base, model, data, prefixes=("", "")):
    qp, dp = prefixes
    passages = [dp + t for t in data["passages"].values()]
    questions = [qp + item["q"] for item in data["questions"]]
    off = [qp + item["q"] for item in data["off_topic"]]
    dups = [dp + MEMORY_PREFIX + item["q"] for item in data["questions"]]

    unload_all(base)
    t0 = time.perf_counter()
    embed(base, model, ["Chargement du modèle."])
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    P = embed(base, model, passages)
    per_passage = (time.perf_counter() - t0) / len(passages)
    t0 = time.perf_counter()
    Q = embed(base, model, questions)
    per_question = (time.perf_counter() - t0) / len(questions)
    O = embed(base, model, off)
    D = embed(base, model, dups)
    ps = loaded(base, model)
    result = evaluate(data, P, Q, O, D)
    result.update(model=model, prefixes=bool(qp or dp), dim=int(P.shape[1]),
                  disk_mb=disk_size(base, model) / 2 ** 20,
                  vram_mb=ps.get("size_vram", 0) / 2 ** 20,
                  ram_mb=(ps.get("size", 0) - ps.get("size_vram", 0)) / 2 ** 20,
                  context=ps.get("context_length"),
                  cold_s=cold, ms_per_passage=1000 * per_passage,
                  ms_per_question=1000 * per_question)
    unload_all(base)
    return result


# ------------------------------------------------------------------ rapport
def report(results):
    head = ("| modèle | dim | disque | VRAM | bons passages min / méd. | bruit méd. / p99 / max "
            "| écart | AUC | seuil | top 1 | MRR | hors sujet max | chargement | ms/passage |")
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    for r in results:
        name = r["model"] + (" (préfixes)" if r["prefixes"] else "")
        lines.append(
            f"| {name} | {r['dim']} | {r['disk_mb']:.0f} Mo | {r['vram_mb']:.0f} Mo "
            f"| {r['rel']['min']:.3f} / {r['rel']['median']:.3f} "
            f"| {r['noise']['median']:.3f} / {r['noise']['p99']:.3f} / {r['noise']['max']:.3f} "
            f"| {r['gap']:+.3f} | {r['auc']:.4f} | {r['threshold']:.3f} "
            f"| {r['top1']:.0%} | {r['mrr']:.3f} | {r['off_topic_max']['max']:.3f} "
            f"| {r['cold_s']:.1f} s | {r['ms_per_passage']:.1f} |")
    lines.append("")
    for r in results:
        a = r["at_threshold"]
        name = r["model"] + (" (préfixes)" if r["prefixes"] else "")
        lines.append(
            f"- {name} : au seuil {r['threshold']:.3f}, {a['missed_questions']} question(s) "
            f"sans bon passage, {a['noise_above']} passage(s) sans rapport retenus "
            f"(+{a['near_above']} pièges proches), {a['off_topic_with_hit']} question(s) hors "
            f"sujet avec un résultat ; même question déjà en mémoire : "
            f"{r['dup']['median']:.3f} (écart avec le bon passage jusqu'à "
            f"{r['dup']['gap_max']:.3f}).")
    return "\n".join(lines)


def report_details(results):
    lines = []
    for r in results:
        lines.append(f"\n{r['model']}{' (préfixes)' if r['prefixes'] else ''}")
        lines.append("  bons passages les plus faibles :")
        lines += [f"    {s:.3f}  {q}  →  {p}" for q, p, s in r["hard"]["weakest_relevant"]]
        lines.append("  passages sans rapport les plus forts :")
        lines += [f"    {s:.3f}  {q}  →  {p}" for q, p, s in r["hard"]["strongest_noise"]]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("models", nargs="*", default=MODELS)
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--prefixes", action="store_true",
                        help="mesurer aussi avec les préfixes conseillés par chaque modèle")
    parser.add_argument("--json", type=Path, help="enregistrer les résultats détaillés")
    parser.add_argument("--force", action="store_true",
                        help="refaire les mesures déjà présentes dans le fichier --json")
    parser.add_argument("--details", action="store_true",
                        help="afficher les cas difficiles de chaque modèle")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    data = json.loads(DATASET.read_text(encoding="utf-8"))
    saved = []
    if args.json and args.json.exists():
        saved = json.loads(args.json.read_text(encoding="utf-8"))
    wanted = [(m, p) for m in args.models
              for p in ([("", "")] + ([PREFIXES[m]] if args.prefixes and m in PREFIXES else []))]
    for model, prefixes in wanted:
        key = (model, bool(prefixes[0] or prefixes[1]))
        if not args.force and any((r["model"], r["prefixes"]) == key for r in saved):
            continue
        print(f"… {model}{' (préfixes)' if key[1] else ''}", file=sys.stderr, flush=True)
        result = measure(args.ollama, model, data, prefixes)
        saved = [r for r in saved if (r["model"], r["prefixes"]) != key] + [result]
        if args.json:                     # tout de suite : un plantage ne perd rien
            args.json.write_text(json.dumps(saved, indent=1, ensure_ascii=False),
                                 encoding="utf-8", newline="\n")
    keys = {(m, bool(p[0] or p[1])) for m, p in wanted}
    shown = [r for r in saved if (r["model"], r["prefixes"]) in keys]
    print(report(shown))
    if args.details:
        print(report_details([r for r in shown if "hard" in r]))


if __name__ == "__main__":
    main()
