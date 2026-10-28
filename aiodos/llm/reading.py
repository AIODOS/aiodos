"""Lire un texte sous un modèle du moteur intégré : la vraisemblance d'une réponse, pour les
contrôles au hasard du réseau public (docs/conception/reseau-public-calcul.md § 8 ;
network/spot_check.reading_verdict ; mesure du 28/10/2026, tools/bench_controle.py lire).

Une lecture imposée : la même conversation (messages, sans réflexion), puis une grammaire GBNF
qui n'admet que le texte, température 0 et logprobs : llama-server rend la log-probabilité de
chaque jeton du texte avant échantillonnage, puis celle de la fin de texte, imposée elle
aussi (la dernière entrée, de jeton vide). Sa route OpenAI refuse « echo » (pas de
probabilités des jetons de la demande) : d'où la grammaire. Pas en flux : en flux,
llama-server ne donne pas la probabilité de la fin de texte (essai du 28/10/2026, llama.cpp
b11707) ; la demande part donc en entier, et un arrêt (stop, échéance) ferme la connexion,
ce qui fait abandonner le calcul à llama-server.

Rendu, pour chaque texte, un résumé fait de nombres seulement : {"n": jetons lus, "mean":
moyenne des log-probabilités bornées à CLIP, "window": pire moyenne de WINDOW jetons de
suite, "eos": log-probabilité de la fin de texte, None si inconnue (texte de plus de
MAX_TOKENS jetons : lecture partielle)}. C'est la forme que lit spot_check.reading_measures ;
le demandeur y ajoute la longueur maximale de sa demande (« max_tokens »).

Bornes : un texte de MAX_CHARS caractères au plus, sans caractère nul ni demi-paire de
substitution (refusé : None à sa place) ; MAX_TOKENS jetons lus au plus par texte ; READ_S
secondes au plus pour tous ; MAX_REPLY octets de réponse de llama-server. Ollama ne sait pas
lire ainsi (ni grammaire libre, ni probabilités avant échantillonnage) : seul le moteur
intégré lit (Unavailable sinon). Rien n'est gardé ni écrit au journal du texte lu.
"""
import http.client
import json
import logging
import math
import socket
import threading
import time
from urllib.parse import urlsplit

from aiodos.llm.messages import for_template

log = logging.getLogger("aiodos.llm")

CLIP = -10.0                   # log-probabilité d'un jeton bornée (découpage inhabituel)
WINDOW = 16                    # fenêtre glissante, en jetons
MAX_TOKENS = 4096              # jetons lus au plus par texte (une réponse publique : 2 048)
MAX_CHARS = 32768              # caractères d'un texte, au plus
MAX_REPLY = 8 * 1024 ** 2      # réponse de llama-server lue au plus (~150 octets par jeton)
READ_S = 600                   # pour tous les textes d'une demande, chargement compris
WATCH_S = 0.25
PATH = "/v1/chat/completions"
SUMMARY = ("n", "mean", "window", "eos")


class Unavailable(RuntimeError):
    """Cette machine ne lit pas (Ollama à la place du moteur intégré, llama.cpp absent)."""


class ReadingError(RuntimeError):
    """Lecture impossible : moteur en échec, arrêtée, réponse illisible ou autre texte rendu.
    Le message ne cite jamais le texte."""


# --------------------------------------------------------------------- résumé
def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def summarize(log_probs, eos=None) -> dict | None:
    """Le résumé d'une lecture (cf. plus haut) ; None si aucun jeton."""
    lp = [max(CLIP, float(x)) for x in log_probs if _finite(x)]
    if not lp:
        return None
    size = min(WINDOW, len(lp))
    total = worst = sum(lp[:size])
    for i in range(size, len(lp)):          # fenêtre glissante, en temps linéaire
        total += lp[i] - lp[i - size]
        worst = min(worst, total)
    return {"n": len(lp), "mean": round(sum(lp) / len(lp), 4), "window": round(worst / size, 4),
            "eos": round(float(eos), 4) if _finite(eos) else None}


def clean(value) -> dict | None:
    """Un résumé reçu d'une autre machine, réduit à sa forme (des nombres seulement, dans
    leurs bornes : n entier de 1 à MAX_TOKENS, moyenne et fenêtre entre CLIP et 0, fin de
    texte négative ou None) ; None s'il ne l'a pas."""
    if not isinstance(value, dict):
        return None
    n, mean, window, eos = (value.get(key) for key in SUMMARY)
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_TOKENS:
        return None
    if not all(_finite(x) and CLIP <= x <= 0 for x in (mean, window)):
        return None
    if eos is not None and not (_finite(eos) and eos <= 0):
        return None
    return {"n": n, "mean": float(mean), "window": float(window),
            "eos": float(eos) if eos is not None else None}


# ------------------------------------------------------------------ grammaire
def literal(text: str) -> str:
    """Le texte en littéral GBNF (entre guillemets, « \\ » et « " » échappés, les
    caractères de commande en \\xNN)."""
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


def readable(text) -> bool:
    """Un texte qui peut être lu : non vide, MAX_CHARS caractères au plus, sans caractère nul
    (la grammaire de llama.cpp est une chaîne C) ni demi-paire de substitution."""
    if not isinstance(text, str) or not text or len(text) > MAX_CHARS or "\x00" in text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# -------------------------------------------------------------------- demande
def _close(connection):
    """Ferme la connexion d'un autre fil : llama-server voit le client parti et abandonne."""
    sock = connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def _post(url: str, headers: dict, body: dict, stop, deadline: float) -> dict:
    """La demande entière (pas en flux), échangée dans un fil à part ; un arrêt ou l'échéance
    ferme la connexion et rend la main tout de suite (ReadingError) : sous Windows, fermer le
    socket ne réveille pas un fil qui attend la réponse, qui s'arrête de lui-même ensuite."""
    left = deadline - time.monotonic()
    if left <= 0 or (stop is not None and stop.is_set()):
        raise ReadingError("stopped")                       # i18n: tel quel (jamais montré)
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=left)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    finished, outcome = threading.Event(), {}

    def exchange():
        try:
            connection.request("POST", parts.path.rstrip("/") + PATH, body=data,
                               headers={**headers, "Content-Type": "application/json"})
            response = connection.getresponse()
            outcome["raw"], outcome["status"] = response.read(MAX_REPLY + 1), response.status
        except Exception as exc:  # noqa: BLE001 — rendu à l'appelant : le type seulement
            outcome["error"] = type(exc).__name__
        finally:
            finished.set()

    threading.Thread(target=exchange, daemon=True, name="aiodos-lecture").start()
    try:
        while not finished.wait(WATCH_S):
            if (stop is not None and stop.is_set()) or time.monotonic() > deadline:
                raise ReadingError("stopped")               # i18n: tel quel (jamais montré)
    finally:
        _close(connection)
    if "error" in outcome:
        raise ReadingError(outcome["error"])
    raw = outcome["raw"]
    if len(raw) > MAX_REPLY:
        raise ReadingError("too_long")                      # i18n: tel quel
    if outcome["status"] >= 400:
        raise ReadingError(f"HTTP {outcome['status']}")     # i18n: tel quel
    try:
        found = json.loads(raw)
    except (ValueError, RecursionError):
        raise ReadingError("unreadable") from None          # i18n: tel quel
    if not isinstance(found, dict):
        raise ReadingError("unreadable")                    # i18n: tel quel
    return found


def body(model_id: str, messages, text: str) -> dict:
    """La demande d'une lecture imposée de `text` après `messages`."""
    return {"model": model_id, "messages": for_template(messages), "stream": False,
            # chaque jeton fait au moins un octet : de quoi lire tout le texte, puis la fin
            "max_tokens": min(MAX_TOKENS, len(text.encode("utf-8"))) + 1,
            "temperature": 0.0, "grammar": "root ::= " + literal(text),
            "logprobs": True, "top_logprobs": 1,
            "chat_template_kwargs": {"enable_thinking": False}}


def parse(found: dict, text: str) -> dict:
    """Le résumé d'une réponse de llama-server à body(…) ; ReadingError si elle ne lit pas ce
    texte (autre texte rendu, rien lu)."""
    choice = (found.get("choices") or [None])[0]
    if not isinstance(choice, dict):
        raise ReadingError("unreadable")                    # i18n: tel quel
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content") or ""
    entries = (choice.get("logprobs") or {}).get("content") if isinstance(
        choice.get("logprobs"), dict) else None
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ReadingError("unreadable")                    # i18n: tel quel
    eos = None
    if choice.get("finish_reason") == "stop" and entries and entries[-1].get("token") == "":
        eos = entries[-1].get("logprob")
        entries = entries[:-1]
    # Lu en entier, ou le début seulement (MAX_TOKENS atteint) ; jamais un autre texte.
    # llama-server rend le contenu sans ses blancs de tête (essai réel du 28/10/2026 : « La
    # capitale… » pour « ␣La capitale… », lu en entier) : comparés sans les blancs des bords
    whole = content.strip() == text.strip()
    if not whole and not (eos is None and content.strip()
                          and text.lstrip().startswith(content.strip())):
        raise ReadingError("other_text")                    # i18n: tel quel
    # Une lecture partielle a pu lire MAX_TOKENS + 1 jetons (la place de la fin de texte)
    summary = summarize([e.get("logprob") for e in entries[:MAX_TOKENS]],
                        eos if whole else None)
    if summary is None:
        raise ReadingError("empty")                         # i18n: tel quel
    return summary


def read_at(url: str, headers: dict, model_id: str, messages, texts, stop=None,
            timeout: float = READ_S) -> list:
    """[résumé, ou None pour un texte illisible (readable)] de chaque texte lu après
    `messages` par le llama-server (ou le routeur) d'adresse `url`, sous `model_id` ;
    `timeout` secondes au plus pour tous. ReadingError au premier échec."""
    deadline = time.monotonic() + timeout
    found = []
    for text in texts:
        if not readable(text):
            found.append(None)
            continue
        found.append(parse(_post(url, headers, body(model_id, messages, text), stop, deadline),
                           text))
    return found


def read(model: str, messages, texts, stop=None, timeout: float = READ_S) -> list:
    """read_at par le moteur intégré de cette machine, pour le modèle `model` (son nom, comme
    chez Ollama). Unavailable si cette machine ne lit pas ; ReadingError sinon."""
    from aiodos.llm import integrated
    from aiodos.services import ollama
    if not ollama.use_integrated():
        raise Unavailable("ollama")                         # i18n: tel quel (jamais montré)
    try:
        url, headers, model_id = integrated.endpoint(model)
    except Exception as exc:  # noqa: BLE001 — modèle absent, moteur arrêté…
        raise Unavailable(type(exc).__name__) from None     # i18n: tel quel (jamais montré)
    began = time.monotonic()
    try:
        return read_at(url, headers, model_id, messages, texts, stop, timeout)
    finally:
        integrated.touch(model_id)
        # i18n: tel quel (journal) ; des nombres seulement, jamais le texte
        log.debug("Lecture de %d texte(s) sous « %s » en %.1f s", len(texts), model,
                  time.monotonic() - began)
