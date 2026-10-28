"""Les contrôles au hasard du réseau public (docs/conception/reseau-public-calcul.md § 8 ;
décisions de l'auteur du 27/10/2026) : la réponse d'une machine du réseau public s'affiche
tout de suite, marquée « réseau public » ; de temps en temps, la même demande part en
arrière-plan chez une autre machine, et les deux réponses sont comparées. Une machine qui
triche est mise de côté (network/reputation.py) : 7 jours, puis pour de bon.

Quand (should_check) : toujours tant que la machine n'a jamais été contrôlée d'ici (la
première fois qu'elle sert cette machine, vue d'ici : on ne retient pas qui d'autre elle
sert), puis RATE des réponses, tirées par random.SystemRandom : le prêteur ne peut pas
deviner lesquelles. Au plus MAX_RUNNING contrôles à la fois, un seul par prêteur.

Comment (run_check) : après la réponse affichée, dans un fil à part, sans jamais la
retarder. La même demande (modèle, messages, options : température, longueur, graine) part
chez une machine de contrôle, dans l'ordre donné par l'appelant (une des miennes d'abord,
crue ; sinon une autre machine du réseau public, ni le prêteur, ni une machine de côté),
sous un **autre identifiant de conversation**, tiré ici (donc une autre clé jetable) : le
prêteur ne peut pas relier le contrôle à la conversation contrôlée, et la machine de
contrôle ne sait pas qu'elle contrôle. Le désaccord d'une machine du réseau public ne suffit
pas (elle peut mentir elle aussi) : une seconde machine de contrôle tranche. Si elle donne
tort au prêteur, il est pris en défaut ; si elle lui donne raison et tort à la première,
c'est la première qui l'est.

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
  lecture de A et de B plus
  les règles du texte, aucune erreur sur 40 paires honnêtes et 120 tricheuses lues. Aucune
  route ne demande encore cette lecture à une machine : compare la prend si on la lui
  donne.

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

from aiodos.network import reputation

log = logging.getLogger("aiodos.net")

RATE = 0.05                    # part des réponses publiques contrôlées (décision du 27/10)
MAX_RUNNING = 2                # contrôles en cours à la fois, au plus
MAX_CONTROLS = 4               # machines de contrôle essayées par contrôle, au plus

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
# d'un long texte pèse sur le processeur) ; aucune route ne la demande encore.
READ_CLIP = -10.0              # log-probabilité d'un jeton bornée (découpage inhabituel)
READ_WINDOW = 16               # fenêtre glissante, en jetons
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
    (et qu'aucun contrôle de lui n'est en cours), sinon au hasard RATE des réponses."""
    if not reputation.valid_key(lender_key):
        return False
    with _lock:
        pending = lender_key in _pending
    if not pending and reputation.checked(lender_key) == 0:
        return True
    return (rng or _rng).random() < RATE


# ---------------------------------------------------------------- mesures
_WORD = re.compile(r"\w+")
# i18n: tel quel (motif : un lien s'arrête aux guillemets)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"'«»]+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
# Un domaine nu, en minuscules seulement (« mot.De plus », sans espace, n'en est pas un)
_DOMAIN = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|fr|net|org|io|ly|me|co|info|biz|shop|xyz|"
                     r"app|eu|be|ch|ca|de|uk|us|tv|gg|link|click|online|site|store|top|"
                     r"example))\b")
_PHONE = re.compile(r"(?:\+\d{2}[\s.-]?|\b0)[1-9](?:[\s.-]?\d{2}){4}\b")
_SECOND_LEVEL = {"co", "com", "org", "net", "gouv", "gov", "ac", "edu"}   # bbc.co.uk
# Fin d'une phrase coupée : une lettre, un chiffre, une virgule, deux-points, « = » (du
# code)…, ou une puce ; i18n: tel quel (motif)
_UNFINISHED = re.compile(r"(?:[\w,;:(\[{=«\-–—]|(?:^|\n)\s*[*+-])\s*$")


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
    """Le texte s'arrête-t-il au milieu d'une phrase (une lettre, une virgule, une puce) ?"""
    return bool(_UNFINISHED.search(text.rstrip()))


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
    return [max(READ_CLIP, float(x)) for x in (reading or {}).get("lp") or []
            if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)]


def reading_measures(reading) -> dict:
    """Jetons lus, moyenne des log-probabilités (bornées à READ_CLIP), pire fenêtre de
    READ_WINDOW jetons, et log-probabilité de la fin de texte (None : inconnue)."""
    lp = _log_probs(reading)
    if not lp:
        return {}
    size = min(READ_WINDOW, len(lp))
    worst = min(sum(lp[i:i + size]) / size for i in range(len(lp) - size + 1))
    eos = reading.get("eos")
    eos = float(eos) if isinstance(eos, (int, float)) and math.isfinite(eos) else None
    return {"ll_n": len(lp), "ll_mean": round(sum(lp) / len(lp), 3),
            "ll_window": round(worst, 3), "ll_eos": round(eos, 3) if eos is not None else None}


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
    reference = _log_probs(reading_b)
    if reference:
        measures["ll_gap"] = round(measures["ll_mean"] - sum(reference) / len(reference), 3)
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
    """La réponse de la première machine de contrôle qui répond : (machine, clé, texte), ou
    None. Une machine qui échoue est passée."""
    while candidates and len(used) < MAX_CONTROLS:
        machine, key = candidates.pop(0)
        used.append(key)
        try:
            text = ask(machine, control_request(request))
        except Exception as exc:  # noqa: BLE001 — la suivante
            # i18n: tel quel (journal) ; le type seulement : le message pourrait citer la demande
            log.info("Contrôle du réseau public : machine de contrôle en échec (%s)",
                     type(exc).__name__)
            continue
        if isinstance(text, str) and text.strip():
            return machine, key, text
    return None


def _note(key, ok, verdict, role="prêteur"):        # i18n: tel quel (journal)
    reputation.note_check(key, ok, verdict.reason)
    # i18n: tel quel (journal) ; des nombres seulement
    log.info("Contrôle du réseau public : %s %s %s (%s, %s)", role, reputation.short(key),
             "réussi" if ok else "raté", verdict.reason, verdict.measures)


def _check(lender_key, request, answer, ask, controls, embed, embed_model):
    question = _question(request)
    candidates = [(machine, key) for machine, key in controls
                  if key != lender_key and not (key is not None and
                                                reputation.is_set_aside(key))]
    used = []
    first = _ask(ask, candidates, request, used)
    if first is None:
        log.info("Contrôle du réseau public : %s, aucune machine de contrôle",  # i18n: tel quel
                 reputation.short(lender_key))
        return None
    found = compare(question, answer, first[2], embed=embed, embed_model=embed_model)
    if found.ok or first[1] is None:            # accord, ou désaccord d'une de mes machines
        _note(lender_key, found.ok, found)
        return found
    second = _ask(ask, candidates, request, used)
    if second is None:
        log.info("Contrôle du réseau public : %s, sans conclusion (%s, une seule machine de "
                 "contrôle)", reputation.short(lender_key), found.reason)  # i18n: tel quel
        return None
    again = compare(question, answer, second[2], embed=embed, embed_model=embed_model)
    if not again.ok:
        _note(lender_key, False, again)
        return again
    # La seconde donne raison au prêteur : la première a-t-elle menti ?
    other = compare(question, first[2], second[2], embed=embed, embed_model=embed_model)
    if not other.ok:
        _note(first[1], False, other, role="contrôleur")    # i18n: tel quel (journal)
        _note(lender_key, True, again)
        return again
    log.info("Contrôle du réseau public : %s, sans conclusion (%s)",  # i18n: tel quel
             reputation.short(lender_key), found.reason)
    return None


def run_check(lender_key, request, answer, ask, controls, *, embed="local", embed_model=None,
              on_done=None) -> threading.Thread | None:
    """Contrôler en arrière-plan la réponse `answer` du prêteur `lender_key` (la clé publique
    de son nœud) à `request` (la demande telle qu'elle est partie : « model », « messages »,
    « options »). Ne bloque jamais : le contrôle tourne dans un fil à part, qui est rendu
    (None si rien n'est lancé : clé invalide, prêteur déjà en contrôle, MAX_RUNNING
    contrôles en cours).

    ask(machine, demande) → texte : la réponse d'une machine de contrôle, en entier (lève en
    cas d'échec) ; la demande porte son propre identifiant de conversation (« conversation »),
    à passer tel quel (public_chat.chat_stream(conversation=…)). controls : les machines de
    contrôle candidates, dans l'ordre de préférence, en couples (machine, clé) : clé None
    pour une de mes machines (crue seule), la clé publique du nœud sinon. embed : textes →
    vecteurs (« local » : le modèle d'embedding de cette machine ; None : les mots communs).
    on_done(verdict ou None) à la fin, dans le fil du contrôle."""
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
            verdict = _check(lender_key, request, answer, ask, controls, embed, embed_model)
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
