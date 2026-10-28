"""Les contrôles au hasard branchés sur mes conversations (docs/conception/reseau-public-calcul.md
§ 8 ; network/spot_check.py, network/reputation.py) : chaque réponse d'une machine du réseau
public passe par stream (chat/pipeline.generate).

Ce qui part : la demande porte une graine tirée au hasard (seed) et la longueur maximale du
prêteur (public_lending.MAX_TOKENS), gardées pour le contrôle : la machine de contrôle reçoit
exactement la même demande (modèle, messages en texte, prompt système et passages compris,
options), sous un autre identifiant de conversation (spot_check.control_request). La même
longueur maximale des deux côtés : sans elle, une réponse honnête arrêtée par la limite du
prêteur paraîtrait tronquée à côté d'une machine qui ne s'arrête pas.

À la fin d'une réponse complète (« done » reçu) et affichée (ni Stop, ni client parti, ni
erreur) : sa vitesse notée sous la clé du nœud prêteur, jamais une clé jetable
(reputation.note_served : les jetons qu'il dit, bornés par une estimation prudente du texte
reçu et par la longueur maximale demandée ; depuis l'envoi de la demande jusqu'au dernier
mot, mesuré ici), puis, si spot_check.should_check le tire, le contrôle en arrière-plan
(spot_check.run_check), qui rend la main tout de suite. Rien de tout cela ne peut faire
échouer la réponse.

Pourquoi depuis l'envoi de la demande (relecture du 28/10/2026) : mesurée du premier mot au
dernier, la vitesse se trichait vers le haut (un prêteur qui garde sa réponse puis l'envoie
d'un coup paraissait très rapide), et c'est elle qui fait passer une machine publique devant
celle-ci (placement.py). Le temps total vu d'ici, chargement et lecture de la demande compris,
est prudent : un prêteur ne peut que la faire paraître plus lente.

Les machines de contrôle (controls), spot_check.MAX_CONTROLS au plus : d'abord mes machines
(pas celles de mes amis) qui prêtent ce modèle, crues seules ; puis les machines du réseau
public qui l'ont et une place (placement.public_with : sans les machines mises de côté), la
plus rapide d'abord ; jamais le prêteur contrôlé. Une demande qui porte mes documents (des
passages, leur sommaire, ou une réponse d'avant qui s'en servait) n'a que mes machines pour
contrôle : un second inconnu ne les lit pas. Cette machine-ci n'en est pas : le contrôle ne
prend pas la carte graphique à l'utilisateur, qui converse ici. Aucune machine de contrôle :
rien n'est lancé, et la prochaine réponse de ce prêteur reste « la première » (spot_check).

Une machine prise en faute : rien ne change dans la conversation déjà affichée ; une ligne au
journal (reputation : « Machine du réseau public mise de côté : contrôle raté (raison) », la
clé abrégée à reputation.SHORT caractères) et elle n'est plus choisie (placement).
"""
import logging
import secrets
import threading
import time

from aiodos import settings
from aiodos.network import (peers, placement, public_chat, public_lending, remote_models,
                            reputation, sharing, spot_check)
from aiodos.services import ollama

log = logging.getLogger("aiodos.net")

SEED_MAX = 2 ** 31 - 1          # dans les bornes du prêteur (public_lending.SEED_RANGE)
# Réponse d'une machine de contrôle attendue au plus (ask) : chargement du modèle compris
# (public_chat.LOADING_MAX_S), puis public_lending.MAX_TOKENS jetons même lents. Une machine
# publique est coupée avant, à public_chat.TOTAL_S (et écartée quelques minutes) ; ce délai
# borne surtout mes machines
CONTROL_S = 900


def seed() -> int:
    """Une graine tirée au hasard (secrets : le prêteur ne peut pas la prévoir)."""
    return secrets.randbelow(SEED_MAX + 1)


def options() -> dict:
    """Les options d'une demande publique : une graine neuve, la longueur maximale du prêteur."""
    return {"seed": seed(), "num_predict": public_lending.MAX_TOKENS}


def _text(messages) -> list:
    """Les messages tels qu'ils sont partis (public_chat.body : rôle et contenu seulement)."""
    return [{"role": m.get("role"), "content": m.get("content")}
            for m in messages or [] if isinstance(m, dict)]


def documents_in(sources, shown, inherited) -> bool:
    """La demande porte-t-elle mes documents ? Des passages (sources « knowledge »), le
    sommaire de mes bibliothèques (shown, pipeline.build_messages), ou une réponse d'avant
    qui s'en servait (inherited, pipeline.inherited)."""
    return bool(shown) or bool((inherited or {}).get("collection_ids")) or any(
        isinstance(s, dict) and s.get("type") in ("knowledge", "summary") for s in sources or [])


def stream(model, messages, stop=None, conversation=None, documents=True):
    """Couples (genre, texte) d'une machine du réseau public (« public:<clé>:<modèle> »), comme
    public_chat.chat_stream, avec la graine et la longueur des options() ; la réponse finie
    et affichée, sa vitesse notée et, de temps en temps, contrôlée (answered). documents :
    la demande porte mes documents (cf. documents_in ; dans le doute, vrai)."""
    key, name = public_chat.parse(model)
    sent = options()
    pieces, last, done = [], None, None
    asked = time.monotonic()                # la demande part au premier tour de la boucle
    for kind, value in public_chat.chat_stream(model, messages, False, stop, options=sent,
                                               stats=True, conversation=conversation):
        if kind == "stats":                 # « done » reçu : la réponse est complète
            done = value
            continue
        if kind == "content":
            last = time.monotonic()
            pieces.append(value)
        yield kind, value
    if done is None or (stop is not None and stop.is_set()):
        return
    request = {"model": name, "messages": _text(messages), "options": sent,
               "conversation": conversation}
    try:
        answered(key, request, "".join(pieces), done.get("completion_tokens"),
                 last - asked if last is not None else 0.0, documents)
    except Exception:  # noqa: BLE001 — la réponse est là : un contrôle manqué ne la gâche pas
        log.warning("Contrôle du réseau public : impossible de le préparer", exc_info=True)


def _tokens(reported, text: str) -> int:
    """Les jetons de la réponse : ceux que dit le prêteur, jamais plus qu'une estimation
    prudente du texte reçu (public_lending.estimate_tokens), ni que la longueur maximale
    demandée (public_lending.MAX_TOKENS, options) : il ne gonfle pas sa vitesse."""
    bound = min(public_lending.estimate_tokens(text), public_lending.MAX_TOKENS)
    if isinstance(reported, int) and not isinstance(reported, bool) and reported > 0:
        return min(reported, bound)
    return bound


def answered(key, request, text, tokens, seconds, documents=True):
    """Une réponse complète du prêteur `key` (clé de son nœud) à `request` ({"model",
    "messages", "options", "conversation"}), en `seconds` secondes depuis l'envoi de la
    demande : vitesse notée, puis le contrôle s'il est tiré et qu'une machine peut le faire.
    → le fil du contrôle, ou None."""
    reputation.note_served(key, _tokens(tokens, text), seconds)
    if not spot_check.should_check(key):
        return None
    machines = controls(request["model"], key, documents)
    if not machines:
        log.debug("Contrôle du réseau public : %s, aucune machine de contrôle pour l'instant",
                  reputation.short(key))     # i18n: tel quel (journal)
        return None
    return spot_check.run_check(key, request, text, ask, machines, embed=_embed())


def controls(name: str, lender_key, documents=True) -> list:
    """Les machines de contrôle candidates, dans l'ordre, en couples (identifiant concret,
    clé) : clé None pour une de mes machines (crue seule), celle du nœud pour une machine
    publique (cf. le début du module)."""
    found = []
    for peer in peers.list_peers():
        if not placement.eligible(peer) or sharing.level(peer["node_id"]) != sharing.MINE:
            continue
        model = next((m["name"] for m in remote_models.chat_models(peer.get("profile"))
                      if placement.same_model(m["name"], name)
                      and not placement.reserved(m["name"])), None)
        if model is not None:
            found.append((remote_models.model_id(peer["node_id"], model), None))
    if not documents:
        found += [(public_chat.model_id(key, model), key)
                  for key, model in placement.public_with(name) if key != lender_key]
    return found[:spot_check.MAX_CONTROLS]


def ask(machine, request) -> str:
    """La réponse entière d'une machine de contrôle à `request` (spot_check.run_check) :
    une machine publique par le compagnon, sous l'identifiant de conversation du contrôle ;
    une des miennes comme d'habitude (remote_models). Lève en cas d'échec : la suivante.

    CONTROL_S secondes au plus (relecture du 28/10/2026) : une machine de contrôle qui
    distille sa réponse sans fin tenait le contrôle ouvert pour toujours, et avec lui sa place
    (spot_check.MAX_RUNNING : deux ainsi, plus aucun contrôle jusqu'au redémarrage). Coupée à
    l'échéance, sa réponse partielle ne sert pas (elle ferait accuser le prêteur) : TimeoutError."""
    stop = threading.Event()
    timer = threading.Timer(CONTROL_S, stop.set)
    timer.daemon = True
    timer.start()
    try:
        if public_chat.parse(machine):
            pieces = public_chat.chat_stream(machine, request["messages"], think=False,
                                             stop=stop, options=request["options"],
                                             conversation=request["conversation"])
        else:
            pieces = remote_models.chat_stream(machine, request["messages"], think=False,
                                               stop=stop, options=request["options"])
        text = "".join(text for kind, text in pieces if kind == "content")
    finally:
        timer.cancel()
    if stop.is_set():
        raise TimeoutError("contrôle trop long")        # i18n: tel quel (jamais montré)
    return text


def _embed():
    """« local » si le modèle d'embedding de cette machine est là (réglage embed_model),
    sinon None : les mots communs (spot_check.compare)."""
    try:
        wanted = settings.get("embed_model")
        installed = [m["name"] for m in ollama.list_models()]
    except Exception:  # noqa: BLE001 — moteur arrêté : les mots communs
        return None
    return "local" if wanted and any(placement.same_model(n, wanted) for n in installed) \
        else None
