"""Les refus suspects d'une machine du réseau public, vus d'ici (côté demandeur ; relecture du
28/10/2026, docs/conception/reseau-public-calcul.md § 8). Un prêteur reçoit la conversation
entière avant de répondre : un menteur pouvait la lire, puis refuser « avant calcul » (occupée,
ne prête plus, modèle absent, demande trop grosse ou mal formée), récolter ainsi des
conversations sans jamais calculer, et rester candidat à chaque message : seuls les échecs de
public_chat.FAULTS (lent, coupé…) l'écartaient.

Une machine publique proposée par le choix des machines (network/placement.py : elle prête,
a une place, « free » moins les conversations que je viens de lui envoyer, et ce modèle au nom
exact) y est notée sous ce modèle (offered). Le premier essai qui la prend sous ce modèle dans
les OFFER_S secondes reprend cette note (promised : une note, un essai) ; un refus de SUSPECT
reçu d'elle ensuite, après l'envoi de la demande (le statut de sa réponse, ou une erreur dans
son flux), contredit ce qu'elle annonçait : il est compté (refused). too_big et bad_request
aussi : la demande est partie, donc nos bornes (public_chat.body, plus strictes que les
siennes) la jugeaient valide. LIMIT refus en WINDOW_S secondes : elle est écartée comme après
un échec (reputation.bench), et le journal le dit (la clé abrégée et les codes, jamais le
contenu). Un refus isolé, la course normale (sa place vient d'être prise par un autre, et son
profil ne le dit pas encore), ne l'écarte pas ; une réponse servie n'efface rien (un menteur
qui répondrait une fois sur trois récolterait les deux autres).

Rien ne change pour l'utilisateur : la conversation passe à la machine suivante, comme pour
tout refus (placement.run). En mémoire seulement, comme reputation.bench ; rien du contenu, et
rien de partagé avec d'autres machines.
"""
import logging
import threading
import time

from aiodos.network import reputation

log = logging.getLogger("aiodos.net")

# Refus « avant calcul » qui contredisent un profil (place libre, prêt, modèle annoncés)
SUSPECT = {"busy", "not_lending", "no_model", "too_big", "bad_request"}
# Refus comptés avant l'écart : trois en dix minutes. Une machine qui prête n'a qu'une place
# aujourd'hui (public.MAX_FREE) et redit son profil dès qu'elle la perd : la course entre ce
# profil et mon choix se paie d'un refus, rarement de deux ; trois de suite, c'est un choix
LIMIT = 3
WINDOW_S = reputation.BENCH_S
# Une note de choix vaut pour un essai commencé dans les OFFER_S secondes : au-delà (les
# machines d'avant ont tardé), le profil lu au choix ne l'engage plus
OFFER_S = 120
MAX_KEYS = 10_000              # machines suivies en mémoire, au plus (notes et refus)

_lock = threading.Lock()
_offers = {}                   # (clé, modèle) -> instant du choix (time.monotonic)
_refusals = {}                 # clé -> [(instant, code)…], les WINDOW_S dernières secondes


def _bounded(table: dict):
    """Les plus anciennes entrées oubliées au-delà de MAX_KEYS (ordre d'insertion)."""
    while len(table) > MAX_KEYS:
        del table[next(iter(table))]


def offered(key, name):
    """Le choix des machines propose cette machine publique sous ce modèle : son profil
    annonce le prêt, une place et ce modèle (cf. le début du module)."""
    if not reputation.valid_key(key) or not name:
        return
    now = time.monotonic()
    with _lock:
        for other in [k for k, at in _offers.items() if now - at > OFFER_S]:
            del _offers[other]
        _offers.pop((key, name), None)            # en dernier : la plus récente
        _offers[(key, name)] = now
        _bounded(_offers)


def promised(key, name) -> bool:
    """Un essai commence chez cette machine sous ce modèle : avait-elle été proposée dans les
    OFFER_S dernières secondes ? La note est reprise (une note, un essai)."""
    with _lock:
        at = _offers.pop((key, name), None)
    return at is not None and time.monotonic() - at <= OFFER_S


def refused(key, code) -> bool:
    """Un refus de SUSPECT reçu de cette machine après l'envoi de la demande, alors qu'elle
    avait été proposée (promised) : compté ; LIMIT en WINDOW_S secondes, elle est écartée
    (reputation.bench). → vrai si elle vient de l'être."""
    if code not in SUSPECT or not reputation.valid_key(key):
        return False
    now = time.monotonic()
    with _lock:
        seen = [(at, c) for at, c in _refusals.pop(key, ()) if now - at <= WINDOW_S]
        seen.append((now, code))
        if len(seen) < LIMIT:
            _refusals[key] = seen                 # en dernier : la plus récente
            _bounded(_refusals)
            return False
    codes = ", ".join(sorted({c for _, c in seen}))
    # i18n: tel quel (journal) ; la clé abrégée et les codes seulement
    log.warning("Réseau public : %s refuse %d fois en %d min après avoir annoncé une place "
                "et le modèle (%s)", reputation.short(key), len(seen), WINDOW_S // 60, codes)
    reputation.bench(key, "refus répétés")      # i18n: tel quel (journal)
    return True


def count(key) -> int:
    """Refus comptés pour cette machine, les WINDOW_S dernières secondes (essais, état)."""
    now = time.monotonic()
    with _lock:
        return sum(1 for at, _ in _refusals.get(key, ()) if now - at <= WINDOW_S)


def forget():
    """Tout oublier (essais)."""
    with _lock:
        _offers.clear()
        _refusals.clear()
