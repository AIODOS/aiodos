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

Les machines de contrôle (controls), spot_check.MAX_CANDIDATES au plus : d'abord mes machines
(pas celles de mes amis) qui prêtent ce modèle, crues seules ; puis les machines du réseau
public qui l'ont et une place (placement.public_with : sans les machines mises de côté) ;
jamais le prêteur contrôlé. Une demande qui porte mes documents (des passages, leur
sommaire, ou une réponse d'avant qui s'en servait) n'a que mes machines pour contrôle : un
second inconnu ne les lit pas. Cette machine-ci n'en est pas : le contrôle ne prend pas la
carte graphique à l'utilisateur, qui converse ici. Aucune machine de contrôle : rien n'est
lancé, et la prochaine réponse de ce prêteur reste « la première » (spot_check).

Contre la collusion (29/10/2026) : deux nœuds d'un même menteur (une clé ne coûte rien) qui
annoncent le même modèle se contrôlaient l'un l'autre. Les machines publiques sont donc
rangées (_standing) : celles qui ont un passé établi ici (reputation.established : vues depuis
un jour, contrôlées avec succès, jamais prises en faute) d'abord ; puis les autres, celles
qui ont réussi le plus de contrôles et sont vues depuis le plus de jours d'abord ; celles
apparues en même temps que le prêteur (network/sightings.together) en dernier ; la plus
rapide à égalité. Ce qui conclut (deux machines indépendantes, ou une des miennes), et la
diversité des machines qui votent : spot_check, « Qui conclut ». Pour savoir qui est apparu
quand, l'annuaire est noté (sightings.sighted) à chaque lecture au compagnon (network/public.py,
depuis le 29/10/2026 au soir) ; et ici encore, sans coût, l'annuaire gardé (public.seen) après
chaque réponse publique complète (jamais avant le premier mot) et à chaque choix des machines
de contrôle.

La lecture (read, depuis le 28/10/2026) : après sa réponse B, la machine de contrôle lit A et
B sous le même modèle (llm/reading.py chez elle), et spot_check.compare reçoit les deux
lectures : elles seules voient un autre modèle ou une publicité sans lien (mesure du 28/10).
Toujours en fond, après la réponse affichée ; chez la même machine de contrôle (donc jamais
chez un inconnu si la demande porte mes documents) ; une machine qui ne sait pas lire : les
textes seuls.

Une machine prise en faute : rien ne change dans la conversation déjà affichée ; une ligne au
journal (reputation : « Machine du réseau public mise de côté : contrôle raté (raison) », la
clé abrégée à reputation.SHORT caractères) et elle n'est plus choisie (placement).
"""
import logging
import secrets
import threading
import time

from aiodos import db, settings
from aiodos.network import (peers, placement, public, public_chat, public_lending,
                            remote_models, reputation, sharing, sightings, spot_check)
from aiodos.services import ollama

log = logging.getLogger("aiodos.net")

SEED_MAX = 2 ** 31 - 1          # dans les bornes du prêteur (public_lending.SEED_RANGE)
# Réponse d'une machine de contrôle attendue au plus (ask) : chargement du modèle compris
# (public_chat.LOADING_MAX_S), puis public_lending.MAX_TOKENS jetons même lents. Une machine
# publique est coupée avant, à public_chat.TOTAL_S (et écartée quelques minutes) ; ce délai
# borne surtout mes machines
CONTROL_S = 900
# Lecture de A et de B par la machine de contrôle (read), au plus, attente de sa pause
# comprise : une lecture coûte à peu près une génération par texte
READ_S = 900
READ_RETRY_S = 20               # une machine publique en pause (« busy ») : redemandée après


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
    demande : l'annuaire noté (_sight), la vitesse notée, puis le contrôle s'il est tiré et
    qu'une machine peut le faire. → le fil du contrôle, ou None."""
    _sight()
    reputation.note_served(key, _tokens(tokens, text), seconds)
    if not spot_check.should_check(key):
        return None
    machines = controls(request["model"], key, documents)
    if not machines:
        log.debug("Contrôle du réseau public : %s, aucune machine de contrôle pour l'instant",
                  reputation.short(key))     # i18n: tel quel (journal)
        return None
    return spot_check.run_check(key, request, text, ask, machines, embed=_embed(), read=read)


def controls(name: str, lender_key, documents=True) -> list:
    """Les machines de contrôle candidates, dans l'ordre, en couples (identifiant concret,
    clé) : clé None pour une de mes machines (crue seule), celle du nœud pour une machine
    publique, rangées par _standing (cf. le début du module)."""
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
        _sight()
        now = db.now()
        publics = [(key, model) for key, model in placement.public_with(name)
                   if key != lender_key]                 # la plus rapide d'abord
        publics.sort(key=lambda pair: _standing(pair[0], lender_key, now))   # stable
        found += [(public_chat.model_id(key, model), key) for key, model in publics]
    return found[:spot_check.MAX_CANDIDATES]


def _standing(key, lender_key, now: float) -> tuple:
    """Le rang d'une machine publique comme machine de contrôle de ce prêteur (le plus petit
    d'abord) : apparue avec lui en dernier, établie en premier ; puis les contrôles réussis
    ici, les jours depuis sa première vue (reputation.first_seen)."""
    if sightings.together(key, lender_key, now):
        group = 2
    else:
        group = 0 if reputation.established(key) else 1
    row = reputation.get(key)
    first = reputation.first_seen(key)
    days = int((now - first) // sightings.DAY_S) if first is not None else 0
    return group, -(row["checks_sure"] if row else 0), -days


def _sight():
    """L'annuaire gardé (public.seen, sans le relire au compagnon) : la première vue de
    chaque machine (sightings.sighted). Jamais d'échec : une machine non notée passe pour vue
    à l'instant, ce qui ne lui donne rien."""
    try:
        sightings.sighted(public.seen()[1])
    except Exception:  # noqa: BLE001 — base occupée… : la prochaine fois
        log.debug("Réseau public : premières vues non notées", exc_info=True)  # i18n: tel quel


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


def read(machine, request, texts) -> list:
    """Les textes lus par la machine de contrôle qui vient de répondre (spot_check.run_check,
    read), sous le modèle demandé, après les mêmes messages : [résumé ou None]. Une machine
    publique par le compagnon (public_chat.read), sous l'identifiant de conversation de sa
    réponse (jamais celui de la conversation contrôlée) ; une des miennes par l'action
    « read » (remote_models.read). Lève si elle ne sait pas lire (version d'avant : 404 ;
    Ollama au lieu du moteur intégré) ou échoue : le contrôle compare alors les textes seuls.

    Une machine publique vient de calculer : elle peut être en pause (public_lending : la
    puissance prêtée) et refuser « busy » ; la demande est refaite toutes les READ_RETRY_S
    secondes. READ_S secondes au plus en tout (TimeoutError)."""
    stop = threading.Event()
    timer = threading.Timer(READ_S, stop.set)
    timer.daemon = True
    timer.start()
    try:
        if not public_chat.parse(machine):
            return remote_models.read(machine, request["messages"], texts, stop=stop)
        while True:
            try:
                return public_chat.read(machine, request["messages"], texts, stop=stop,
                                        conversation=request["conversation"])
            except public_chat.PublicError as exc:
                if exc.code != "busy" or stop.wait(READ_RETRY_S):
                    raise
    finally:
        timer.cancel()
        if stop.is_set():
            raise TimeoutError("lecture trop longue")       # i18n: tel quel (jamais montré)


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
