"""Les contrôles au hasard du réseau public (docs/conception/reseau-public-calcul.md § 8 ;
décisions de l'auteur du 27/10/2026) : la réponse d'une machine du réseau public s'affiche
tout de suite, marquée « réseau public » ; de temps en temps, la même demande part en
arrière-plan chez une autre machine, et les deux réponses sont comparées. Une machine qui
triche est mise de côté (network/reputation.py) : 7 jours, puis pour de bon.

Quand (should_check) : toujours tant que la machine n'a jamais été contrôlée d'ici avec une
conclusion (la première fois qu'elle sert cette machine, vue d'ici : on ne retient pas qui
d'autre elle sert), FIRST_TRIES fois au plus sans conclusion, puis RATE des réponses, tirées
par random.SystemRandom : le prêteur ne peut pas deviner lesquelles. Au plus MAX_RUNNING
contrôles à la fois, un seul par prêteur.

Comment (run_check) : après la réponse affichée, dans un fil à part, sans jamais la
retarder. La même demande (modèle, messages, options : température, longueur, graine) part
chez une machine de contrôle, dans l'ordre donné par l'appelant (les miennes d'abord, puis
les machines du réseau public établies, network/public_controls.controls ; ni le prêteur, ni
une machine de côté ; une machine qui ressemble à une autre qui a déjà répondu passe après
les autres, _diverse), sous un **autre identifiant de conversation**, tiré ici (donc une
autre clé jetable) : le prêteur ne peut pas relier le contrôle à la conversation contrôlée,
et la machine de contrôle ne sait pas qu'elle contrôle.

Qui conclut (contre la collusion, 29/10/2026) : une de mes machines est crue seule. Une
machine du réseau public peut mentir, dans un sens comme dans l'autre, et une clé de nœud ne
coûte rien : deux nœuds d'un même menteur annonçant le même modèle se contrôlaient l'un
l'autre, et le contrôle passait toujours. Les machines publiques votent donc (_decide), au
plus MAX_CONTROLS demandées :
- réussi : deux machines d'accord avec le prêteur, indépendantes de lui (pas apparues avec
  lui, network/sightings.together) et l'une de l'autre (elles ne se ressemblent pas,
  sightings.alike), et plus nombreuses que celles qui le contredisent. Une seule d'accord :
  sans conclusion ;
- raté : deux machines qui lui donnent tort et ne se ressemblent pas, plus nombreuses que
  celles qui lui donnent raison ; ou, faute d'autre machine, une faute flagrante (FLAGRANT :
  sans rapport, lien ajouté, tronquée, un autre modèle à la lecture) vue par une machine
  établie (reputation.established) et indépendante du prêteur, sans machine établie pour lui
  donner raison. Une machine sans passé ici ne fait jamais compter seule une faute : elle
  pourrait accuser un honnête ;
- une machine établie n'est jamais mise en minorité par des machines sans passé : la
  majorité doit en compter une aussi (sinon : sans conclusion) ;
- sinon, sans conclusion : ni réussi, ni raté (reputation.note_open).
Une machine de contrôle seule contre deux (la minorité, quand une majorité conclut) est prise
en défaut si sa propre réponse ne ressemble à aucune des leurs (lue par la dernière machine,
qui lit, en cas de désaccord, A, sa réponse et celles d'avant) : une machine honnête qui a
seulement jugé A autrement n'est pas accusée.

Ce que cela ne règle pas (conception, § 8) : un menteur patient qui espace la création de
ses nœuds, ou qui les fait d'abord passer pour honnêtes, n'est pas reconnu par ces règles
seules ; le compagnon rend les identités publiques coûteuses (preuve de travail sur la clé).

Comparer (compare) : les règles et leurs seuils viennent d'une mesure du 28/10/2026
(tools/bench_controle.py, résultats dans tools/bench_controle_results.json) : qwen3.5:9b,
vingt questions en français, 400 jetons au plus, 80 paires honnêtes (même graine sur la même
carte, sur une autre façon de calculer, autre graine, température 0) et 160 tricheuses
(qwen2.5:7b et mistral:7b à la place du modèle demandé, réponse tronquée, réponse à une autre
question, publicité ajoutée avec ou sans lien). Ce qu'elle a montré :

- même modèle, même graine, même carte : les deux textes divergent déjà après 1 à 1 111
  caractères (les calculs groupés de llama.cpp ne sont pas reproductibles) ; avec une autre
  graine, après 0 à 128. Le préfixe commun ne prouve donc rien, ni dans un sens ni dans
  l'autre ;
- comparer les deux textes (ce que fait run_check) n'attrape que trois tricheries, mais
  toutes, sans aucun faux « tricheur » sur 80 paires honnêtes : une réponse sans rapport (20
  sur 20 avec les embeddings, 17 sur 20 avec les mots communs), un lien, un courriel ou un
  numéro ajouté (20 sur 20), une réponse coupée au milieu d'une phrase (20 sur 20) ;
- un autre modèle, ou une publicité sans lien, ne se voient pas en comparant deux textes
  (similarité et longueurs dans les mêmes plages que les paires honnêtes : 0 à 5 pris sur
  80 selon le modèle d'embedding, 0 sur 20) ; la vraisemblance les voit (reading_verdict) :
  lecture de A et de B plus les règles du texte, aucune erreur sur 40 paires honnêtes et 120
  tricheuses lues.

Lire (read, depuis le 28/10/2026) : la machine de contrôle qui a répondu B lit ensuite A et B
sous le même modèle (llm/reading.py chez elle : une de mes machines par l'action « read »,
une machine publique par POST /public/v1/read), dans un ordre tiré au hasard, sous
l'identifiant de conversation de sa réponse ; compare reçoit les deux lectures. Une machine
qui ne sait pas lire (version d'avant : 404 ; Ollama au lieu du moteur intégré) : la
comparaison des textes seule, comme avant. Quand des machines de contrôle sont en désaccord,
la suivante lit A, sa réponse et celles des machines d'avant.

Le journal ne dit que la clé du prêteur (abrégée), le verdict et ses mesures (des nombres) :
jamais le contenu des conversations, ni l'identifiant de conversation du contrôle (il mène à
sa clé jetable).
"""
import logging
import math
import random
import re
import secrets
import threading
import unicodedata
from dataclasses import dataclass, field

from aiodos.llm import reading as _reading
from aiodos.network import reputation, sightings

log = logging.getLogger("aiodos.net")

RATE = 0.05                    # part des réponses publiques contrôlées (décision du 27/10)
MAX_RUNNING = 2                # contrôles en cours à la fois, au plus
MAX_CONTROLS = 4               # machines de contrôle essayées par contrôle, au plus
MAX_CANDIDATES = 8             # machines de contrôle proposées (public_controls), au plus
# « La première fois » dure jusqu'à un contrôle conclu, FIRST_TRIES contrôles sans conclusion
# au plus : sans machine qui puisse conclure (aucune des miennes n'a le modèle, une seule
# machine publique indépendante), chaque réponse serait refaite ailleurs, pour rien
FIRST_TRIES = 3
# Fautes flagrantes : comptées sur l'avis d'une seule machine de contrôle établie (_decide)
FLAGRANT = frozenset({"unrelated", "link", "truncated", "unlikely"})

# --- Seuils mesurés le 28/10/2026 (tools/bench_controle.py ; cf. plus haut) ---------------
# Sans rapport : similarité cosinus des embeddings des deux réponses entières. Chaque modèle
# d'embedding a sa propre échelle (comme settings.RAG_THRESHOLDS) : paires honnêtes au plus
# bas / réponses à une autre question au plus haut, sur 80 et 20 paires :
#   qwen3-embedding 0,75 / 0,40 ; bge-m3 0,78 / 0,56 ; nomic-embed-text 0,81 / 0,66.
# Le seuil : le milieu de l'écart. embeddinggemma, le modèle par défaut d'AIODOS, n'a pas
# pu être mesuré (le fichier d'Ollama ne se lit pas dans llama.cpp, rien n'a été
# téléchargé) : son échelle est proche de celle de qwen3-embedding (seuils de pertinence 0,40
# et 0,42, settings.py), d'où UNRELATED_COS_DEFAULT, au-dessous du plus bas honnête mesuré.
UNRELATED_COS = {"qwen3-embedding": 0.58, "bge-m3": 0.67, "nomic-embed-text": 0.735}
UNRELATED_COS_DEFAULT = 0.6
# Sans embeddings (aucun modèle ici, ou en échec) : part des mots de A (3 lettres et plus,
# sans accents) retrouvés dans B ou la question. Honnêtes au plus bas 0,25 ; sans rapport
# 0,00 à 0,24 (17 sur 20 sous 0,15). Seuil prudent : en dessous de la plage honnête.
UNRELATED_WORDS = 0.15
# Tronquée : A fait moins de TRUNCATED_RATIO fois la longueur de B (en caractères) et
# s'arrête au milieu d'une phrase. Honnêtes au plus bas 0,53 (et alors finies par un point) ;
# réponses coupées à 35 % : 0,24 à 0,46, toutes au milieu d'une phrase (l'une sur
# « ma_chaine = », d'où « = »). Une réponse honnête coupée par la longueur maximale l'est
# aussi chez B : les longueurs se suivent (0,71 à 1,35).
TRUNCATED_RATIO = 0.5
# Lien ajouté : un lien, un domaine, un courriel ou un numéro de téléphone dans A dont le
# domaine (ou le numéro) n'est ni dans B ni dans la question. 20 publicités avec lien sur 20,
# aucune paire honnête (mais aucune question ne demandait de site : cf. ce qui reste fragile).

# Vraisemblance (reading) : la réponse A lue sous le modèle demandé par une machine qui l'a :
# une lecture forcée (une grammaire GBNF impose le texte, temperature 0, logprobs : la
# log-probabilité de chaque jeton avant échantillonnage, puis celle de la fin de texte
# imposée), mesurée avec llama-server (sa route OpenAI refuse « echo » : pas de probabilités
# des jetons de la demande). Elle coûte autant qu'une génération, et même plus (sur la RTX
# 4070, 50 jetons/s pour quatre lectures à la fois, contre 95 en génération : la grammaire
# d'un long texte pèse sur le processeur). Demandée à la machine de contrôle après sa réponse
# (run_check, read : llm/reading.py chez elle), qui rend un résumé fait des mêmes bornes.
READ_CLIP = _reading.CLIP      # log-probabilité d'un jeton bornée (découpage inhabituel)
READ_WINDOW = _reading.WINDOW  # fenêtre glissante, en jetons
# Coupée : une réponse honnête arrêtée d'elle-même finit à -1,72 au plus bas (40 réponses) ;
# une réponse coupée à 35 %, à -13,1 au plus haut (20). Une réponse de LIMIT_SHARE de la
# longueur maximale ou plus a pu être coupée par la limite : pas de test.
EOS_MIN = -7.0
LIMIT_SHARE = 0.9
# Un autre modèle : moyenne de A moins celle de B, lues de la même façon. Honnêtes -0,21 au
# plus bas (40) ; qwen2.5:7b -0,34 et mistral:7b -0,75 au plus haut (20 chacun). Marge mince.
GAP_MIN = -0.28
# Sans lecture de B, la moyenne de A seule : honnêtes -1,22 au plus bas (un poème) ; autres
# modèles de -2,31 à -0,77 : seulement 25 sur 40 en dessous du seuil.
UNLIKELY_MEAN = -1.3
# Un passage ajouté : la pire fenêtre de READ_WINDOW jetons. Honnêtes -3,13 au plus bas ;
# publicités ajoutées, avec ou sans lien, -4,79 au plus haut (40).
WINDOW_MIN = -4.0

_rng = random.SystemRandom()
_lock = threading.Lock()
_pending = set()               # prêteurs en cours de contrôle


@dataclass(frozen=True)
class Verdict:
    """Le résultat d'une comparaison : ok (A ressemble à une réponse honnête), la raison
    (agree, empty, link, truncated, unrelated, unlikely, inserted) et les mesures (des
    nombres seulement, jamais du texte : elles vont au journal)."""
    ok: bool
    reason: str
    measures: dict = field(default_factory=dict)


# ------------------------------------------------------------------- quand
def should_check(lender_key, rng=None) -> bool:
    """Contrôler cette réponse de ce prêteur ? Toujours s'il n'a jamais été contrôlé d'ici
    avec une conclusion, FIRST_TRIES fois au plus sans conclusion (et qu'aucun contrôle de
    lui n'est en cours), sinon au hasard RATE des réponses."""
    if not reputation.valid_key(lender_key):
        return False
    with _lock:
        pending = lender_key in _pending
    if not pending and reputation.checked(lender_key) == 0 \
            and reputation.inconclusive(lender_key) < FIRST_TRIES:
        return True
    return (rng or _rng).random() < RATE


# ---------------------------------------------------------------- mesures
# Les textes comparés viennent d'inconnus (le prêteur, une machine de contrôle) : chaque motif
# se lit en temps linéaire (relecture du 28/10/2026). « [\w.+-]+@ » ou « (?:[a-z0-9-]+\.)+ »,
# essayés à chaque position d'une longue suite de lettres (« a.a.a.a… »), prenaient un temps
# quadratique : 4 s pour 20 000 caractères, des minutes pour une réponse de 130 000, sans
# rendre la main aux autres fils (le module re garde le GIL) : AIODOS entier figé. Bornés
# comme le sont les vraies adresses (64 signes avant « @ », 63 par étiquette d'un domaine,
# 16 étiquettes au plus avant la fin), ils trouvent les mêmes domaines.
_WORD = re.compile(r"\w+")
# i18n: tel quel (motif : un lien s'arrête aux guillemets)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"'«»]+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]{1,64}@([\w-]+(?:\.[\w-]+)+)")
# Un domaine nu, en minuscules seulement (« mot.De plus », sans espace, n'en est pas un)
_DOMAIN = re.compile(r"\b((?:[a-z0-9-]{1,63}\.){1,16}(?:com|fr|net|org|io|ly|me|co|info|biz|"
                     r"shop|xyz|app|eu|be|ch|ca|de|uk|us|tv|gg|link|click|online|site|store|"
                     r"top|example))\b")
_PHONE = re.compile(r"(?:\+\d{2}[\s.-]?|\b0)[1-9](?:[\s.-]?\d{2}){4}\b")
_SECOND_LEVEL = {"co", "com", "org", "net", "gouv", "gov", "ac", "edu"}   # bbc.co.uk
# Fin d'une phrase coupée : une lettre, un chiffre, une virgule, deux-points, « = » (du
# code)…, ou une puce (« * », « + » ou « - » seuls sur leur ligne) ; i18n: tel quel (motif)
_UNFINISHED_END = re.compile(r"[\w,;:(\[{=«\-–—]")
_BULLETS = "*+-"


def _fold(text: str) -> str:
    """Minuscules, sans accents, blancs réduits."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(ch for ch in text if not unicodedata.combining(ch)).split())


def _words(text: str) -> set:
    return {w for w in _WORD.findall(_fold(text)) if len(w) >= 3}


def contained(a: str, b: str, question: str) -> float:
    """Part des mots de A (3 lettres et plus) retrouvés dans B ou dans la question."""
    mine = _words(a)
    return len(mine & (_words(b) | _words(question))) / len(mine) if mine else 1.0


def _domain(address: str) -> str:
    """Le domaine enregistrable d'une adresse (« https://fr.wikipedia.org/wiki/x » →
    « wikipedia.org », « news.bbc.co.uk » → « bbc.co.uk ») : deux réponses honnêtes citent
    souvent le même site par deux pages ou deux langues."""
    host = re.sub(r"^(?:https?://)?", "", address.casefold())
    host = re.split(r"[/?#:]", host)[0].rstrip(".,;:!?")
    labels = [label for label in host.split(".") if label]
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def links(text: str) -> set:
    """Les domaines (liens, adresses, courriels) et les numéros de téléphone d'un texte."""
    found = {_domain(m.group()) for m in _URL.finditer(text)}
    found |= {_domain(m.group(1)) for m in _EMAIL.finditer(text)}
    found |= {_domain(m.group(1)) for m in _DOMAIN.finditer(text)}
    found |= {re.sub(r"\D", "", m.group())[-9:] for m in _PHONE.finditer(text)}
    return {f for f in found if f}


def new_links(a: str, b: str, question: str) -> int:
    """Domaines et numéros de A qui ne sont ni dans B ni dans la question."""
    return len(links(a) - links(b) - links(question))


def unfinished(text: str) -> bool:
    """Le texte s'arrête-t-il au milieu d'une phrase (une lettre, une virgule, une puce) ?
    Son dernier signe seulement, et pour une puce ce qui la précède sur sa ligne : lu en
    temps linéaire (cf. plus haut)."""
    text = text.rstrip()
    if not text:
        return False
    if _UNFINISHED_END.fullmatch(text[-1]):
        return True
    if text[-1] not in _BULLETS:
        return False
    before = text[:-1]
    kept = before.rstrip()                       # la puce seule sur sa ligne (ou en tête)
    return not kept or "\n" in before[len(kept):]


def cosine(a, b) -> float | None:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb or len(a) != len(b):
        return None
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def unrelated_threshold(embed_model) -> float:
    base = str(embed_model or "").split(":")[0].casefold()
    return UNRELATED_COS.get(base, UNRELATED_COS_DEFAULT)


# ---------------------------------------------------------------- comparer
def compare(question, answer_a, answer_b, *, embed=None, embed_model=None,
            reading=None, reading_b=None) -> Verdict:
    """A (la réponse du prêteur) ressemble-t-elle à B (celle de la machine de contrôle) ?
    embed : textes → vecteurs (le modèle d'embedding de cette machine, nommé embed_model ;
    sans lui, ou en échec, les mots communs) ; reading : la lecture de A sous le modèle
    demandé, si une machine a pu la faire ({"lp": log-probabilités des jetons, "eos" : celle
    de la fin de texte, "max_tokens" : la longueur maximale de la demande}), et reading_b
    celle de B (cf. reading_verdict). Règles et seuils : cf. plus haut."""
    question = question if isinstance(question, str) else ""
    a, b = (answer_a or "").strip(), (answer_b or "").strip()
    measures = {"len_a": len(a), "len_b": len(b)}
    if not a:
        return Verdict(not b, "agree" if not b else "empty", measures)
    if not b:
        return Verdict(True, "agree", measures)       # rien à opposer à A
    measures["new_links"] = added = new_links(a, b, question)
    if added:
        return Verdict(False, "link", measures)
    measures["len_ratio"] = ratio = round(len(a) / len(b), 3)
    measures["unfinished"] = cut = unfinished(a)
    if ratio < TRUNCATED_RATIO and cut:
        return Verdict(False, "truncated", measures)
    similarity = None
    if embed is not None:
        try:
            va, vb = embed([a, b])
            similarity = cosine(va, vb)
        except Exception as exc:  # noqa: BLE001 — sans embeddings, les mots communs
            # i18n: tel quel (journal) ; le type seulement : le message pourrait citer le texte
            log.debug("Contrôle du réseau public : embeddings en échec (%s)",
                      type(exc).__name__)
    if similarity is not None:
        measures["cos"] = round(similarity, 3)
        if similarity < unrelated_threshold(embed_model):
            return Verdict(False, "unrelated", measures)
    else:
        measures["contained"] = share = round(contained(a, b, question), 3)
        if share < UNRELATED_WORDS:
            return Verdict(False, "unrelated", measures)
    if reading is not None:
        found = reading_verdict(reading, reading_b)
        measures.update(found.measures)
        if not found.ok:
            return Verdict(False, found.reason, measures)
    return Verdict(True, "agree", measures)


def _log_probs(reading) -> list:
    raw = reading.get("lp") if isinstance(reading, dict) else None
    return [max(READ_CLIP, float(x)) for x in raw if isinstance(x, (int, float))
            and not isinstance(x, bool) and math.isfinite(x)] if isinstance(raw, list) else []


def reading_measures(reading) -> dict:
    """Jetons lus, moyenne des log-probabilités (bornées à READ_CLIP), pire fenêtre de
    READ_WINDOW jetons, et log-probabilité de la fin de texte (None : inconnue). La lecture
    est soit entière ({"lp": […], "eos"}), soit déjà résumée par la machine qui l'a faite
    ({"n", "mean", "window", "eos"} : llm/reading.summarize, mêmes bornes et même fenêtre ;
    hors de sa forme, ignorée)."""
    if isinstance(reading, dict) and "lp" not in reading:
        summary = _reading.clean(reading)
        if summary is None:
            return {}
        return {"ll_n": summary["n"], "ll_mean": round(summary["mean"], 3),
                "ll_window": round(summary["window"], 3),
                "ll_eos": round(summary["eos"], 3) if summary["eos"] is not None else None}
    lp = _log_probs(reading)
    if not lp:
        return {}
    size = min(READ_WINDOW, len(lp))
    worst = min(sum(lp[i:i + size]) / size for i in range(len(lp) - size + 1))
    eos = reading.get("eos")
    eos = float(eos) if isinstance(eos, (int, float)) and math.isfinite(eos) else None
    return {"ll_n": len(lp), "ll_mean": round(sum(lp) / len(lp), 3),
            "ll_window": round(worst, 3), "ll_eos": round(eos, 3) if eos is not None else None}


def _mean(reading) -> float | None:
    """La moyenne d'une lecture, entière ou résumée (None : pas de lecture)."""
    found = reading_measures(reading) if reading is not None else {}
    return found.get("ll_mean")


def reading_verdict(reading, reading_b=None) -> Verdict:
    """La lecture de A sous le modèle demandé ({"lp", "eos", "max_tokens"} : la longueur
    maximale de la demande, None si inconnue) et, si elle est là, celle de B lue de la même
    façon (reading_b). Dans l'ordre : coupée (le modèle ne se serait pas arrêté là, alors
    que A s'est arrêtée avant la limite), un autre modèle (A bien moins probable que B, ou
    à défaut trop improbable seule), un passage ajouté (une fenêtre trop improbable)."""
    measures = reading_measures(reading)
    if not measures:
        return Verdict(True, "agree", measures)
    limit = reading.get("max_tokens")
    if measures["ll_eos"] is not None and isinstance(limit, int) and limit > 0 \
            and measures["ll_n"] < LIMIT_SHARE * limit and measures["ll_eos"] < EOS_MIN:
        return Verdict(False, "truncated", measures)
    reference = _mean(reading_b)
    if reference is not None:
        measures["ll_gap"] = round(measures["ll_mean"] - reference, 3)
        if measures["ll_gap"] < GAP_MIN:
            return Verdict(False, "unlikely", measures)
    elif measures["ll_mean"] < UNLIKELY_MEAN:
        return Verdict(False, "unlikely", measures)
    if measures["ll_window"] < WINDOW_MIN:
        return Verdict(False, "inserted", measures)
    return Verdict(True, "agree", measures)


# ------------------------------------------------------------------ contrôler
def _question(request) -> str:
    """Le dernier message de l'utilisateur de la demande."""
    for message in reversed((request or {}).get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user" \
                and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def control_request(request) -> dict:
    """La demande du contrôle : la même (modèle, messages, options), sous un nouvel
    identifiant de conversation tiré ici (« conversation » : comme public_chat.tag, 32
    caractères [A-Za-z0-9_-]), jamais celui de la conversation contrôlée."""
    copy = {k: v for k, v in dict(request or {}).items() if k != "conversation"}
    copy["conversation"] = secrets.token_urlsafe(24)
    return copy


def _local_embed():
    """Le modèle d'embedding de cette machine (réglage embed_model), ou (None, None)."""
    try:
        from aiodos import settings
        from aiodos.services import ollama
        model = settings.get("embed_model")
    except Exception:  # noqa: BLE001 — sans embeddings, les mots communs
        return None, None
    return (lambda texts: ollama.embed(texts, model=model, timeout=60)), model


def _ask(ask, candidates, request, used: list):
    """La réponse de la première machine de contrôle qui répond : (machine, clé, texte,
    demande partie, avec son identifiant de conversation), ou None. Une machine qui échoue
    est passée."""
    while candidates and len(used) < MAX_CONTROLS:
        machine, key = candidates.pop(0)
        used.append(key)
        sent = control_request(request)
        try:
            text = ask(machine, sent)
        except Exception as exc:  # noqa: BLE001 — la suivante
            # i18n: tel quel (journal) ; le type seulement : le message pourrait citer la demande
            log.info("Contrôle du réseau public : machine de contrôle en échec (%s)",
                     type(exc).__name__)
            continue
        if isinstance(text, str) and text.strip():
            return machine, key, text, sent
    return None


def _limit(request):
    """La longueur maximale de la demande (options « num_predict »), None si inconnue."""
    wanted = ((request or {}).get("options") or {}).get("num_predict")
    return wanted if isinstance(wanted, int) and not isinstance(wanted, bool) \
        and wanted > 0 else None


def _readings(read, control, texts: list) -> list:
    """Les lectures de `texts` par la machine de contrôle `control` (le résultat de _ask),
    sous le modèle demandé, chacune avec la longueur maximale de la demande
    (« max_tokens ») : [lecture ou None], dans l'ordre de `texts`. Envoyés dans un ordre
    tiré au hasard (elle ne sait pas lequel est la réponse contrôlée), sous l'identifiant de
    conversation de sa réponse (jamais celui de la conversation contrôlée). Une machine qui
    ne sait pas lire (version d'avant, Ollama…) ou qui échoue : aucune lecture, la
    comparaison des textes reste."""
    if read is None:
        return [None] * len(texts)
    machine, _key, _text, sent = control
    order = list(range(len(texts)))
    _rng.shuffle(order)
    try:
        found = read(machine, sent, [texts[i] for i in order])
    except Exception as exc:  # noqa: BLE001 — sans lecture, les textes seuls
        # i18n: tel quel (journal) ; le type seulement : le message pourrait citer la demande
        log.info("Contrôle du réseau public : lecture absente (%s)", type(exc).__name__)
        return [None] * len(texts)
    if not isinstance(found, list) or len(found) != len(texts):
        return [None] * len(texts)
    limit, out = _limit(sent), [None] * len(texts)
    for place, index in enumerate(order):
        if isinstance(found[place], dict):
            out[index] = {**found[place], "max_tokens": limit}
    return out


def _note(key, ok, verdict, role="prêteur"):        # i18n: tel quel (journal)
    reputation.note_check(key, ok, verdict.reason)
    # i18n: tel quel (journal) ; des nombres seulement
    log.info("Contrôle du réseau public : %s %s %s (%s, %s)", role, reputation.short(key),
             "réussi" if ok else "raté", verdict.reason, verdict.measures)


@dataclass
class _Vote:
    """L'avis d'une machine de contrôle publique sur la réponse du prêteur (_check)."""
    key: str                   # la clé de son nœud
    text: str                  # sa réponse
    verdict: Verdict           # la réponse du prêteur comparée à la sienne
    established: bool          # reputation.established
    free: bool                 # pas apparue avec le prêteur (sightings.together)
    reads: dict                # rang d'un avis (le sien compris) -> sa réponse lue par elle

    @property
    def ok(self) -> bool:
        return self.verdict.ok


def _alike_any(key, votes) -> bool:
    return key is not None and any(sightings.alike(key, v.key) for v in votes)


def _diverse(candidates: list, votes: list):
    """Les machines de contrôle qui ressemblent à une machine qui a déjà voté
    (sightings.alike) passent après les autres, dans le même ordre sinon (mes machines
    restent devant)."""
    if votes:
        candidates.sort(key=lambda pair: _alike_any(pair[1], votes))


def _pair(votes) -> bool:
    """Deux de ces avis viennent-ils de machines indépendantes du prêteur (free) qui ne se
    ressemblent pas ?"""
    free = [v for v in votes if v.free]
    return any(not sightings.alike(a.key, b.key)
               for i, a in enumerate(free) for b in free[i + 1:])


def _apart(votes) -> bool:
    """Deux de ces avis viennent-ils de machines qui ne se ressemblent pas ?"""
    return any(not sightings.alike(a.key, b.key)
               for i, a in enumerate(votes) for b in votes[i + 1:])


def _outranks(majority, minority) -> bool:
    """Une machine établie n'est mise en minorité que par une majorité qui en compte une."""
    return any(v.established for v in majority) or not any(v.established for v in minority)


def _decide(votes: list, more: bool):
    """Les avis des machines de contrôle publiques (cf. le début du module, « Qui
    conclut ») : (« ok » ou « fail », majorité, minorité), (« open », [], []) sans conclusion,
    ou None si d'autres machines peuvent encore être demandées (more)."""
    oks = [v for v in votes if v.ok]
    fails = [v for v in votes if not v.ok]
    if len(oks) > len(fails) and _pair(oks) and _outranks(oks, fails):
        return "ok", oks, fails
    if len(fails) > len(oks) and _apart(fails) and _outranks(fails, oks):
        return "fail", fails, oks
    if more:
        return None
    strong = [v for v in fails if v.established and v.free and v.verdict.reason in FLAGRANT]
    if strong and not any(v.established for v in oks):
        return "fail", strong[:1], []
    return "open", [], []


def _check(lender_key, request, answer, ask, controls, embed, embed_model, read=None):
    question = _question(request)
    candidates = [(machine, key) for machine, key in controls
                  if key != lender_key and not (key is not None and
                                                reputation.is_set_aside(key))]

    def judge(a, b, reading=None, reading_b=None):
        return compare(question, a, b, embed=embed, embed_model=embed_model,
                       reading=reading, reading_b=reading_b)
    used, votes, decided = [], [], None
    while decided is None:
        _diverse(candidates, votes)
        control = _ask(ask, candidates, request, used)
        if control is None:
            break
        _machine, key, text, _sent = control
        # A et B lus par la machine de contrôle, sous le même modèle (si elle sait lire) ;
        # en cas de désaccord, les réponses des machines d'avant aussi : de quoi les juger
        disputed = len({v.ok for v in votes}) > 1
        texts = [answer, text] + ([v.text for v in votes] if disputed else [])
        found = _readings(read, control, texts)
        verdict = judge(answer, text, found[0], found[1])
        if key is None:                         # une de mes machines : crue seule
            _note(lender_key, verdict.ok, verdict)
            return verdict
        reads = {len(votes): found[1]}
        if disputed:
            reads.update({i: found[2 + i] for i in range(len(votes))})
        votes.append(_Vote(key, text, verdict, reputation.established(key),
                           not sightings.together(key, lender_key), reads))
        decided = _decide(votes, more=bool(candidates) and len(used) < MAX_CONTROLS)
    if not votes:
        log.info("Contrôle du réseau public : %s, aucune machine de contrôle",  # i18n: tel quel
                 reputation.short(lender_key))
        return None
    outcome, majority, minority = decided or _decide(votes, more=False)
    if outcome == "open":
        reputation.note_open(lender_key)
        # i18n: tel quel (journal) ; les raisons seulement
        log.info("Contrôle du réseau public : %s, sans conclusion (%s)",
                 reputation.short(lender_key), ", ".join(v.verdict.reason for v in votes))
        return None
    # Seule contre deux : la dernière machine a lu toutes les réponses (désaccord avant elle)
    reads, index = votes[-1].reads, {id(v): i for i, v in enumerate(votes)}
    for alone in minority:
        against = [judge(alone.text, v.text, reads.get(index[id(alone)]),
                         reads.get(index[id(v)])) for v in majority]
        if against and not any(found.ok for found in against):
            _note(alone.key, False, against[0], role="contrôleur")  # i18n: tel quel (journal)
    verdict = majority[-1].verdict
    _note(lender_key, outcome == "ok", verdict)
    return verdict


def run_check(lender_key, request, answer, ask, controls, *, embed="local", embed_model=None,
              on_done=None, read=None) -> threading.Thread | None:
    """Contrôler en arrière-plan la réponse `answer` du prêteur `lender_key` (la clé publique
    de son nœud) à `request` (la demande telle qu'elle est partie : « model », « messages »,
    « options »). Ne bloque jamais : le contrôle tourne dans un fil à part, qui est rendu
    (None si rien n'est lancé : clé invalide, prêteur déjà en contrôle, MAX_RUNNING
    contrôles en cours).

    ask(machine, demande) → texte : la réponse d'une machine de contrôle, en entier (lève en
    cas d'échec) ; la demande porte son propre identifiant de conversation (« conversation »),
    à passer tel quel (public_chat.chat_stream(conversation=…)). controls : les machines de
    contrôle candidates, dans l'ordre de préférence, en couples (machine, clé) : clé None
    pour une de mes machines (crue seule), la clé publique du nœud sinon (les machines
    publiques votent : cf. le début du module, « Qui conclut »). embed : textes →
    vecteurs (« local » : le modèle d'embedding de cette machine ; None : les mots communs).
    read(machine, demande, textes) → [lecture ou None] : les textes lus par cette machine de
    contrôle sous le modèle demandé (llm/reading.py), la demande étant celle qui lui est
    partie (son identifiant de conversation compris) ; lève si elle ne sait pas lire. None :
    aucune lecture, les textes seuls. on_done(verdict ou None : sans conclusion, ou aucune
    machine de contrôle) à la fin, dans le fil du contrôle."""
    if not reputation.valid_key(lender_key):
        return None
    with _lock:
        if lender_key in _pending or len(_pending) >= MAX_RUNNING:
            return None
        _pending.add(lender_key)
    if embed == "local":
        embed, embed_model = _local_embed()
    controls = list(controls or [])

    def work():
        verdict = None
        try:
            verdict = _check(lender_key, request, answer, ask, controls, embed, embed_model,
                             read)
        except Exception as exc:  # noqa: BLE001 — un échec de notre fait n'accuse personne
            # i18n: tel quel (journal) ; le type seulement : le message pourrait citer la demande
            log.warning("Contrôle du réseau public en échec (%s)", type(exc).__name__)
        finally:
            with _lock:
                _pending.discard(lender_key)
            if on_done is not None:
                on_done(verdict)

    thread = threading.Thread(target=work, daemon=True, name="aiodos-controle-public")
    thread.start()
    return thread


def forget():
    """Plus aucun contrôle en cours (essais)."""
    with _lock:
        _pending.clear()
