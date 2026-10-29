"""Choisir seul la machine qui répond à une conversation (mode « automatique »).

Un même modèle peut être installé sur cette machine et prêté par mes autres machines
(lending.py). En mode automatique, identifiant « auto:<modèle> », la conversation part
vers la mieux placée :

1. cette machine, si elle a le modèle et qu'elle est libre (aucune conversation en
   cours), sauf quand le modèle n'y tient pas en mémoire vidéo alors qu'une machine
   libre l'y fait tenir (qwen3.6, 22 Go, sur une carte de 8 Go : bien plus lent ici) ;
2. sinon une machine en ligne qui le prête et qui est libre, de préférence celle où le
   modèle tient en mémoire vidéo, puis celle qui a le moins d'unités en cours, le
   modèle déjà chargé, le GPU le moins occupé ;
3. si toutes sont occupées, celle qui a le moins de conversations en cours ; à
   égalité, cette machine : rien ne sort de la maison.

Seules mes machines servent en mode automatique : celles d'un ami seulement si le
réglage auto_route_friends le permet, car la conversation passe chez lui le temps de
la réponse. Une machine mise de côté après un contrôle échoué n'est jamais choisie
d'office. Le choix d'une machine précise (« peer:<machine>:<modèle> », ou le nom seul
pour cette machine) reste possible et passe tel quel, comme un gros modèle réparti entre
mes machines (« reparti:<modèle> », cluster/group.py).

Les machines du réseau public (network/public_chat.py, 28/10/2026), quand la conversation le
permet (public, cf. chat/public_consent.py) : celles qui prêtent (« lends »), ont une place
(« free », moins les conversations que je viens de leur envoyer) et le modèle, au nom exact,
ou, si l'utilisateur l'a accepté (bigger), un modèle de conversation plus gros (le plus grand
nombre de paramètres lu dans le nom, public_chat.params) et contrôlable (cf. plus bas ; sur un
petit réseau, même pas contrôlable, par la règle des petits réseaux) : « le meilleur modèle
libre ». Elles viennent en dernier, à vitesse comparable ; une machine publique
passe devant cette machine seulement si elle est **établie ici** (reputation.established : vue
depuis un jour, un contrôle réussi par une de mes machines ou deux machines indépendantes,
aucun raté ; sur un petit réseau, cf. plus bas, même non établie) et que sa vitesse **mesurée
ici** (reputation.speed : au moins une réponse servie et mesurée, depuis l'envoi de la demande
jusqu'au dernier mot) vaut au moins FASTER fois celle de cette machine (node/speed.py ;
inconnue sur un téléphone : toute vitesse mesurée), si elle répond avec un modèle plus gros
accepté, ou si cette machine est occupée (une conversation en cours ici : la machine publique,
libre, répond sans attendre) ; jamais devant une de mes machines libres (cette machine, libre,
passe alors après elles). Entre machines publiques, à modèle égal, les établies d'abord, puis
la plus rapide, la plus récemment vue.
La vitesse qu'une machine annonce (« tps », jusqu'à 100 000 dans un profil) ne sert qu'à
départager les machines publiques entre elles, jamais à passer devant cette machine ni devant
mes machines (relecture du 28/10/2026 : en annonçant une vitesse énorme, un menteur récoltait
des conversations ; l'amorce d'un téléphone, plus bas, ne s'y fie pas davantage). Une machine
jamais mesurée, ou pas établie, sert donc d'abord quand cette machine n'a pas le modèle, est
occupée, ou pour un modèle plus gros accepté (sur un petit réseau : cf. plus bas).
PUBLIC_TRIES au plus. Identifiant : « public:<clé>:<modèle> ».

Pourquoi « établie » et non « connue depuis quelques minutes » (known_s, 29/10/2026 au soir) :
passer devant cette machine, libre et qui a le modèle, c'est recevoir toutes ses conversations
de ce modèle, ce qu'un menteur cherche (les lire, y répondre mal). Une vitesse mesurée suppose
déjà une réponse servie ici ; quelques minutes d'attente de plus ne coûtent rien à une identité
qui coûte ~2 s de calcul (la preuve de travail du compagnon). Être établie, c'est un jour au
moins et un contrôle réussi qu'elle ne pouvait pas reconnaître.

La règle des petits réseaux (décision de l'auteur du 29/10/2026) : sur un réseau encore petit
(son PC et son téléphone), aucune machine n'est établie, et le PC ne répondait jamais aux
conversations du téléphone, ce qui contredit la raison d'être d'AIODOS. Tant qu'**aucune machine
publique établie ne prête ce modèle** (_held_models : celles qui prêtent, place libre ou non,
même écartées quelques minutes après un échec, reputation.bench), une machine publique qui a ce
modèle (nom exact), mesurée ici FASTER fois plus rapide que cette machine (même règle que plus
haut, téléphone compris), passe devant même non établie ; jamais une machine en faute (un
contrôle raté, même passé : _blameless ; de côté ou écartée, elle n'est déjà plus candidate).
Sa réponse est marquée « non vérifiable pour l'instant » (unverified : plan la dit à
l'appelant, routes/chat.py la garde avec le message). Dès qu'une machine établie prête ce
modèle, la règle stricte reprend : seules les établies passent devant.

Avec un **modèle plus gros** (décision de l'auteur du 29/10/2026 après-midi : le PC de l'auteur
n'a que qwen3.5:9b, son téléphone converse avec qwen3.5:0.8b) : l'accord « modèle plus gros »
donné (bigger, public_bigger_model, demandé une fois par la carte : bigger_public), la même
machine, aux mêmes conditions, peut répondre avec un modèle plus gros **pas contrôlable**
(_unchecked), que n'en prête aucune machine établie, et d'au plus SMALL_MAX_PARAMS milliards de
paramètres d'après son nom (ce qu'une machine d'aujourd'hui fait tourner ; au-delà,
« qwen3.5:999b » ou « 120b », seule une machine établie compte, comme avant). Son plus gros tel
modèle, s'il en a un, plutôt que ce modèle ; un modèle plus gros contrôlable passe avant tout
(sans marque). Marquée de même. Sans l'accord : ce modèle seulement.

L'amorce de la mesure (_trial) : jamais servie ici, une machine n'est mesurée par rien, et ne
passerait jamais devant. Sur un téléphone seulement (android.is_android : c'est lui la machine
lente ; un PC garde la règle stricte), sur un petit réseau (ci-dessus), libre et premier de son
plan, sans aucune machine déjà devant lui : une machine publique jamais mesurée ici, sans faute,
avec ce modèle (ou, l'accord donné, le modèle plus gros de la règle), est essayée avant lui, au
plus une fois par TRIAL_S toutes machines confondues (en mémoire : un redémarrage la rouvre).
La toute première conversation part ainsi vers le PC public. La vitesse annoncée (« tps ») n'y
suffit jamais seule : c'est le téléphone qui décide
d'essayer, une fois par heure, quel que soit le nombre d'identités d'un menteur ; elle ne sert
qu'à écarter une machine qui se dit elle-même trop lente (moins de FASTER fois la vitesse de ce
téléphone, si elle est connue) et à départager, après l'ancienneté (_seniority : une identité
neuve passe après une machine connue depuis longtemps). Ensuite, seule sa vitesse mesurée
compte (depuis l'envoi de la demande, donc chargement du modèle compris). Sa réponse est
marquée de même. Ce que cela coûte : un menteur rapide (un tout petit modèle, du texte sans
valeur) passe devant comme un honnête tant qu'aucune machine établie n'a ce modèle (ou, l'accord
donné, le modèle plus gros qu'il annonce, jusqu'à SMALL_MAX_PARAMS milliards), et un menteur qui
se dit rapide obtient au plus une conversation par heure de chaque téléphone ; la marque le dit
à l'utilisateur.

Ce que les contrôles au hasard ont appris (network/reputation.py, 28/10/2026) : une machine
publique mise de côté (contrôle raté), ou écartée quelques minutes après un échec (lente,
coupée, illisible… : reputation.bench), n'est plus candidate, ni comme machine de contrôle ;
la vitesse mesurée ici, quand elle est connue, passe avant celle qu'elle annonce. Chaque
machine publique proposée (candidate, ou machine de contrôle : public_with) est notée
(public_refusals.offered) : si elle refuse ensuite « avant calcul » ce que son profil
annonçait (une place, le modèle), ses refus sont comptés, et trop de refus l'écartent aussi.

Un modèle contrôlable (_controllable) : une autre machine peut refaire la demande et conclure
(network/public_controls.py, spot_check). Présent (nom exact) chez des machines publiques qui
prêtent de CHECKABLE_GROUPS groupes indépendants au moins (network/sightings.groups : celles
apparues ensemble, de proche en proche, font un seul groupe ; trois groupes : le prêteur et
deux machines de contrôle indépendantes de lui et l'une de l'autre), ou chez une machine
publique établie qui prête (reputation.established), ou prêté par une de mes machines. Seul un
tel modèle est pris pour le meilleur (best_public) ou pour un modèle plus gros (relecture du
28/10/2026 : un nœud qui annonçait « qwen3.5:999b » passait toujours premier, et, seul à porter
ce nom, n'avait jamais de machine de contrôle). Contre la collusion (29/10/2026 au soir) : deux
machines distinctes suffisaient, et deux nœuds neufs d'un même menteur rendaient leur modèle
« contrôlable » l'un par l'autre. Une taille invraisemblable lue dans un nom est ignorée
(public_chat.MAX_PARAMS). Seule exception, la règle des petits réseaux (cf. plus haut) : un
modèle plus gros pas contrôlable, de SMALL_MAX_PARAMS milliards au plus, chez une machine
mesurée bien plus rapide (ou essayée une fois par l'amorce), réponse marquée.

« Automatique · réseau public » (PUBLIC_BEST, 28/10/2026) : ni cette machine ni mes machines
n'ont de modèle de conversation (un téléphone neuf, sans modèle téléchargé), et la conversation
peut partir chez un inconnu. Seules les machines du réseau public répondent alors, chacune avec
son meilleur modèle de conversation contrôlable (le plus grand nombre de paramètres lu dans le
nom), le meilleur d'abord, puis les établies, la plus rapide (best_public) ; PUBLIC_TRIES au
plus. Aucun modèle contrôlable (un réseau encore minuscule) : n'importe lequel, en repli, les
machines établies d'abord, puis celles qui ont déjà répondu ici sans faute, puis les plus
anciennes (_seniority), puis le plus gros modèle. Il n'y a rien ici à quoi comparer : ni
« modèle plus gros », ni son accord. L'interface ne le propose que dans ce cas
(chat/public_consent.py, entry), et l'API le refuse sinon (public_consent.reach).

« Automatique » (BEST, « auto:best », 29/10/2026, docs/conception/modeles-auto.md) : le choix par
défaut d'une nouvelle conversation. Il prend **le modèle de conversation le plus puissant
disponible** (own_ranked) : parmi ceux de cette machine et de mes machines qui répondent d'office
(eligible : celles d'un ami seulement si auto_route_friends le permet), le plus grand nombre de
paramètres lu dans le nom (public_chat.params ; un nom qui n'en dit rien : estimé d'après la
taille de ses fichiers, power), puis, à égalité, celui qui tient dans la mémoire vidéo d'une
machine, déjà chargé quelque part, par nom. Pour ce modèle, les machines se choisissent comme
pour « auto:<modèle> » (candidates), le réseau public compris si la conversation le permet, avec
ses règles (même modèle, modèle plus gros accepté, règle des petits réseaux, amorce, marque « non
vérifiable pour l'instant ») ; puis, en repli, les machines des modèles plus modestes, chez cette
machine et mes machines seulement. Sans aucun modèle ici ni sur mes machines : comme
« Automatique · réseau public » (best_public), si la conversation peut partir chez un inconnu.
Les gros modèles répartis (« reparti: ») n'en font pas partie : ils demandent une mise en place.
« best » n'est le nom d'aucun modèle proposé : un modèle qui s'appellerait ainsi ne se choisit
qu'avec sa machine.

Tant qu'aucun mot de la réponse n'est arrivé, une machine qui échoue (injoignable, ne
prête plus, Ollama arrêté, machine publique occupée…) cède la place à la suivante (run).

Une machine qui prête ses modèles indique dans ses nouvelles (ping, profil) combien de
conversations et d'unités elle traite en ce moment (load_field) ; on y ajoute les
conversations qu'on vient de lui envoyer, que ses dernières nouvelles ne comptent pas
encore. Une machine qui ne prête pas ne dit rien de son activité.

Les modèles et la carte graphique d'une machine se lisent dans son profil, revérifié ici
(remote_models.chat_models, _gpu_load) : une entrée mal formée est ignorée au lieu de faire
échouer le choix pour toutes les machines (audit du 20/10/2026, faille 16).
"""
import logging
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from aiodos import db, settings
from aiodos.cluster import group as cluster
from aiodos.i18n import t
from aiodos.jobs import queue
from aiodos.network import (peers, public, public_chat, public_refusals, remote_models,
                            reputation, sharing, sightings)
from aiodos.node import android, identity, speed

log = logging.getLogger("aiodos.placement")

PREFIX = "auto:"
# « Automatique · réseau public » : le meilleur modèle d'une machine du réseau public, quand je
# n'en ai aucun (« * » n'est le nom d'aucun modèle ; parse le dit automatique)
PUBLIC_BEST = PREFIX + "*"
# « Automatique » : le modèle de conversation le plus puissant disponible (cf. le début du module)
BEST = PREFIX + "best"
# La puissance d'un modèle dont le nom ne dit pas la taille (power) : ses fichiers, à 4 bits
# environ, comptent ~0,7 Go par milliard de paramètres (qwen3.5:9b : 6,55 Go)
BYTES_PER_BILLION = 0.7e9
LOAD_TTL = 60               # nouvelles d'une machine plus vieilles : oubliées
LOAD_MAX = 1000             # valeurs annoncées par une autre machine : bornées
FASTER = 2                  # une machine publique passe devant celle-ci : deux fois plus rapide
PUBLIC_TRIES = 3            # machines publiques essayées au plus pour une réponse
PUBLIC_SEEN_MAX = 150       # nœud public vu il y a plus longtemps (profil toutes les 60 s) : parti
# Groupes indépendants de machines publiques (sightings.groups) qui ont un modèle : il est
# contrôlable (le prêteur et deux machines de contrôle)
CHECKABLE_GROUPS = 3
# L'ancienneté d'une machine publique (_seniority), comptée à partir de 10 min, à un facteur
# deux près : 10 min, 20 min, 40 min…
SENIOR_S = 600
# L'amorce de la mesure sur un petit réseau (_trial) : une machine publique jamais mesurée
# essayée avant ce téléphone, au plus une fois par heure, toutes machines confondues
TRIAL_S = 3600
# Un modèle plus gros par la règle des petits réseaux (_unchecked, décision de l'auteur du
# 29/10/2026 après-midi) : la taille lue dans son nom, au plus, en milliards de paramètres. Ce
# qu'une machine d'aujourd'hui fait tourner (72 milliards : ~40 Go en 4 bits, deux cartes de
# 24 Go ou un Mac de 64 Go) ; au-delà (« qwen3.5:999b », « 120b »), seule une machine établie
# compte, comme avant (public_chat.MAX_PARAMS borne le reste)
SMALL_MAX_PARAMS = 72

_lock = threading.Lock()
_loads = {}                 # machine -> (instant monotonic, {"chats": n, "units": n})
_sent = {}                  # machine -> conversations que je lui ai envoyées, en cours
# machine -> {identifiant concret : n} : le modèle exact de chacune (« Machines connectées »,
# activity.public_live : un prêteur qui annonce plusieurs modèles)
_sent_models = {}
_trial = {"at": None}       # la dernière amorce (_try_one), instant monotonic ; None : jamais


class NoMachine(Exception):
    """Aucune machine ne peut répondre avec ce modèle en ce moment (message lisible)."""


def auto_id(name: str) -> str:
    return PREFIX + name


def parse(model) -> str | None:
    """« auto:<modèle> » → le nom du modèle ; None pour un autre identifiant."""
    model = str(model or "")
    name = model[len(PREFIX):] if model.startswith(PREFIX) else ""
    return name or None


# Préfixes qui ont un sens pour AIODOS : un modèle d'Ollama qui en porterait un n'est jamais
# choisi d'office. « image: » marque, dans le profil d'une machine du réseau public, un modèle
# d'images qu'elle prête (network/public_imaging.MARKER) : jamais un modèle de conversation.
RESERVED = ("auto:", "peer:", "reparti:", "local:", "public:", "image:")


def reserved(name) -> bool:
    return str(name or "").startswith(RESERVED)


def same_model(installed: str, wanted: str) -> bool:
    """« llama3 » désigne « llama3:latest », comme pour Ollama."""
    if installed == wanted:
        return True
    tagged = ":" in wanted.rsplit("/", 1)[-1]
    return not tagged and installed == f"{wanted}:latest"


# ----------------------------------------------------------- charge des machines
def load_field(manager, sender) -> dict:
    """{"load": …} à joindre aux nouvelles de cette machine (ping, profil), seulement si
    elle prête ses modèles (c'est ce qui permet de lui envoyer une conversation), et pour
    une de mes machines : un ami ne voit pas l'activité de celle-ci (sharing.FRIEND_PROFILE)."""
    executor = getattr(manager, "executor", None)
    if executor is None or not settings.get("lend_models") \
            or sharing.level(sender) != sharing.MINE:
        return {}
    return {"load": {"chats": int(executor.interactive),
                     "units": int(sum(executor.busy.values()))}}


def note_load(node_id: str, load):
    """Charge annoncée par une machine appairée (None : elle n'en dit rien)."""
    with _lock:
        if not isinstance(load, dict):
            _loads.pop(node_id, None)
            return
        clean = {}
        for key in ("chats", "units"):
            try:
                clean[key] = min(LOAD_MAX, max(0, int(load.get(key) or 0)))
            except (TypeError, ValueError):
                clean[key] = 0
        _loads[node_id] = (time.monotonic(), clean)


def _reported(node_id) -> dict | None:
    with _lock:
        entry = _loads.get(node_id)
    if entry and time.monotonic() - entry[0] < LOAD_TTL:
        return entry[1]
    return None


def forget():
    """Tout oublier (tests)."""
    with _lock:
        _loads.clear()
        _sent.clear()
        _sent_models.clear()
        _trial["at"] = None


def sending() -> dict:
    """{machine : {identifiant concret : réponses en cours}} : les conversations que je fais
    calculer ailleurs en ce moment, avec le modèle exact de chacune (lu, jamais changé)."""
    with _lock:
        return {target: dict(models) for target, models in _sent_models.items()}


# --------------------------------------------------------------- candidates
@dataclass
class Candidate:
    model: str                  # identifiant concret : « qwen3.5:9b », « peer:<machine>:… »
    node_id: str | None         # None : cette machine ; « public:<clé> » : une machine publique
    machine: str                # nom de la machine
    trust: str                  # « local », « mine », « friend » ou « public »
    chats: int = 0              # conversations en cours
    units: int = 0              # unités de travail en cours
    fits: bool | None = None    # le modèle tient-il dans sa mémoire vidéo ?
    loaded: bool = False        # déjà chargé : pas d'attente de chargement
    gpu: int = 0                # occupation du GPU (%), si elle est connue
    # Machines publiques seulement : paramètres du modèle (milliards), vitesse annoncée,
    # depuis quand elle a été vue, si elle passe devant cette machine, si elle est établie ici
    # (reputation.established), depuis quand elle est connue (secondes, _known), et si elle
    # passe devant par la règle des petits réseaux (sa réponse : « non vérifiable pour
    # l'instant », cf. le début du module)
    params: float | None = None
    tps: float | None = None
    seen: int = 0
    ahead: bool = False
    established: bool = False
    known: int = 0
    unverified: bool = False

    @property
    def local(self) -> bool:
        return self.node_id is None

    def key(self) -> tuple:
        """Ordre de préférence (cf. le début du module) : libre, modèle en mémoire vidéo,
        le moins de conversations, cette machine, le moins d'unités, déjà chargé, GPU."""
        return (self.chats > 0, self.fits is False, self.chats, not self.local,
                self.units, not self.loaded, self.gpu)


def _local_profile() -> dict:
    """Profil de cette machine (modèles, mémoire vidéo), gardé quelques secondes."""
    try:
        return identity.profile()
    except Exception as exc:
        log.warning("Profil de cette machine illisible : %s", exc)
        return {}


def _gpu_load(profile) -> int:
    """Occupation (%) de la carte graphique la plus chargée ; une valeur qui n'est pas un
    nombre fini entre 0 et 100 n'est pas comptée."""
    gpus = profile.get("gpus") if isinstance(profile, dict) else None
    loads = [g.get("utilization") for g in (gpus if isinstance(gpus, list) else [])
             if isinstance(g, dict)]
    return max((int(v) for v in loads if isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and 0 <= v <= 100), default=0)


def _model(profile, name: str) -> dict | None:
    """Le modèle de conversation `name` dans ce profil (remote_models.chat_models)."""
    return next((m for m in remote_models.chat_models(profile)
                 if same_model(m["name"], name) and not reserved(m["name"])), None)


def _local(name: str, executor) -> Candidate | None:
    profile = _local_profile()
    model = _model(profile, name)
    if model is None:
        return None
    return Candidate(
        model=model["name"], node_id=None,
        machine=profile.get("name") or t("placement.this_machine"), trust="local",
        chats=int(executor.interactive) if executor else 0,
        units=int(sum(executor.busy.values())) if executor else 0,
        fits=model["fits_vram"], loaded=bool(model["loaded"]), gpu=_gpu_load(profile))


def eligible(peer: dict) -> bool:
    """Cette machine appairée peut-elle répondre d'office à mes conversations ?"""
    if not peer.get("online") or peer.get("set_aside_at"):
        return False
    if not (peer.get("address") or peer.get("net_id")):
        return False
    if not remote_models.lends(peer.get("profile")):
        return False
    return peer.get("trust") != "friend" or bool(settings.get("auto_route_friends"))


def _remote(name: str) -> list:
    out = []
    working = {}
    for counts in queue.remote_counts().values():   # unités qu'elles calculent pour moi
        for node_id, n in counts.items():
            working[node_id] = working.get(node_id, 0) + n
    for peer in peers.list_peers():
        if not eligible(peer):
            continue
        profile = peer.get("profile")
        model = _model(profile, name)
        if model is None:
            continue
        node_id = peer["node_id"]
        reported = _reported(node_id)
        with _lock:
            sent = _sent.get(node_id, 0)
        out.append(Candidate(
            model=remote_models.model_id(node_id, model["name"]), node_id=node_id,
            machine=peer["name"], trust=peer.get("trust") or "mine",
            chats=max(sent, reported["chats"] if reported else 0),
            units=reported["units"] if reported else working.get(node_id, 0),
            fits=model["fits_vram"], loaded=bool(model["loaded"]), gpu=_gpu_load(profile)))
    return out


def _lending(everyone: bool = False) -> list:
    """Les nœuds du réseau public qui prêtent en ce moment, avec ou sans place, d'après le
    dernier état lu au compagnon (public.seen, relu s'il date de plus de public.FRESH_S
    secondes), une fois chacun, sans ceux qu'un contrôle a mis de côté ou qu'un échec écarte
    (reputation.is_set_aside), sauf si everyone (_held) ; aucun si le compagnon ne sait pas
    leur confier une conversation."""
    if public_chat.ready() is None:
        return []
    mine, nodes = public.seen(fresh=True)
    found, keys = [], set()
    for node in nodes:
        key = node.get("key")
        if key == mine or key in keys or not public_chat.valid_key(key) \
                or not node.get("lends") or node.get("seen_s", 0) > PUBLIC_SEEN_MAX:
            continue
        if not everyone and reputation.is_set_aside(key):    # en dernier : une lecture de la base
            continue
        keys.add(key)
        found.append(node)
    return found


def _with_room(nodes) -> list:
    """Ceux de ces nœuds qui ont une place, moins les conversations que je viens de leur
    envoyer."""
    found = []
    for node in nodes:
        with _lock:
            sent = _sent.get(public_chat.PREFIX + node["key"], 0)
        if int(node.get("free") or 0) - sent >= 1:
            found.append(node)
    return found


def _public_nodes() -> list:
    """Les nœuds du réseau public qui prêtent en ce moment et ont une place (cf. _lending)."""
    return _with_room(_lending())


def _tagged(name) -> str:
    """Le nom d'un modèle avec son étiquette (« llama3 » → « llama3:latest », comme Ollama)."""
    name = str(name or "")
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _controllable(nodes) -> set:
    """Les modèles qu'une autre machine peut contrôler (cf. le début du module), avec leur
    étiquette (_tagged) : présents chez des nœuds de CHECKABLE_GROUPS groupes indépendants au
    moins (sightings.groups) parmi ceux-ci (_lending : ils prêtent, avec ou sans place), ou
    chez un de ces nœuds qui est établi (reputation.established_among), ou prêtés par une de
    mes machines (pas celles de mes amis, qui ne contrôlent pas : network/public_controls.py).
    Les premières vues ne sont lues que si un modèle est chez assez de nœuds."""
    holders = {}
    for node in nodes:
        for name in {_tagged(m) for m in node["models"]}:
            holders.setdefault(name, []).append(node["key"])
    trusted = reputation.established_among(node["key"] for node in nodes)
    many = [keys for keys in holders.values() if len(keys) >= CHECKABLE_GROUPS]
    known = sightings.firsts({key for keys in many for key in keys}) if many else {}
    now = db.now()
    found = {name for name, keys in holders.items()
             if trusted.intersection(keys) or (
                 len(keys) >= CHECKABLE_GROUPS
                 and len(sightings.groups(keys, now, known)) >= CHECKABLE_GROUPS)}
    for peer in peers.list_peers():
        if eligible(peer) and sharing.level(peer["node_id"]) == sharing.MINE:
            found |= {_tagged(m["name"]) for m in remote_models.chat_models(peer.get("profile"))}
    return found


def _measured(node) -> float | None:
    """La vitesse d'une machine publique mesurée ici (reputation.speed : au moins une réponse
    servie et mesurée), ou None : seule elle la fait passer devant cette machine."""
    measured = reputation.speed(node["key"])
    return round(min(measured, reputation.MAX_TPS), 1) if measured is not None else None


def _speed(node, measured=None) -> float | None:
    """La vitesse d'une machine publique : mesurée ici si on la connaît (measured : déjà lue
    par _measured), sinon celle qu'elle annonce (_told) ; celle-ci ne sert qu'à départager les
    machines publiques entre elles."""
    if measured is None:
        measured = _measured(node)
    return measured if measured is not None else _told(node)


def _told(node) -> float | None:
    """La vitesse qu'une machine publique annonce (« tps »), bornée à reputation.MAX_TPS."""
    told = node.get("tps")
    return min(told, reputation.MAX_TPS) if told is not None else None


def public_with(name: str) -> list:
    """(clé, modèle) des machines du réseau public qui prêtent, ont une place et ce modèle
    (nom exact), hors machines mises de côté, la plus rapide d'abord : les machines de
    contrôle possibles (network/public_controls.py)."""
    found = []
    for node in _public_nodes():
        chosen = next((m for m in node["models"] if same_model(m, name) and not reserved(m)),
                      None)
        if chosen is not None:
            found.append((-(_speed(node) or 0), node.get("seen_s", 0), node["key"], chosen))
    for _speed_, _seen, key, chosen in found:
        public_refusals.offered(key, chosen)        # proposées (cf. _offer)
    return [(key, chosen) for _speed_, _seen, key, chosen in sorted(found)]


def _larger(models, base, checkable) -> list:
    """(paramètres, nom) des modèles de conversation plus gros que `base` (milliards) et
    contrôlables (dans `checkable`, cf. _controllable)."""
    return [(size, m) for size, m in _bigger_than(models, base) if _tagged(m) in checkable]


def _unchecked(models, base, checkable, held) -> list:
    """(paramètres, nom) des modèles de conversation plus gros que `base` (milliards) que seule
    la règle des petits réseaux peut donner (cf. le début du module) : pas contrôlables (hors
    de `checkable`), prêtés par aucune machine établie (hors de `held`, _held_models), et de
    SMALL_MAX_PARAMS milliards au plus d'après leur nom."""
    return [(size, m) for size, m in _bigger_than(models, base)
            if size <= SMALL_MAX_PARAMS and _tagged(m) not in checkable
            and _tagged(m) not in held]


def _bigger_than(models, base) -> list:
    """(paramètres, nom) des modèles de conversation plus gros que `base` (milliards, lus dans
    le nom : public_chat.params, qui ignore une taille invraisemblable)."""
    if base is None:
        return []
    found = []
    for m in models:
        size = public_chat.params(m)
        if size is not None and size > base and public_chat.converses(m) and not reserved(m):
            found.append((size, m))
    return found


def _faster(measured, here, phone) -> bool:
    """Une vitesse mesurée ici (measured) vaut-elle FASTER fois celle de cette machine (here) ?
    Celle-ci inconnue : oui sur un téléphone, non sur un PC (cf. le début du module)."""
    return measured is not None and (measured >= FASTER * here if here is not None else phone)


def _public(name: str, bigger: bool, busy: bool = False, first: bool = False) -> list:
    """Machines du réseau public qui peuvent répondre (cf. le début du module) : chacune avec
    ce modèle (nom exact), ou, si bigger, avec son plus gros modèle de conversation plus gros
    que celui-ci et contrôlable, ou, par la règle des petits réseaux, pas contrôlable
    (_unchecked) ; celles qui passent devant cette machine d'abord (établie et mesurée FASTER
    fois plus rapide ; sur un petit réseau, même non établie, ou essayée pour l'amorce de la
    mesure ; modèle plus gros contrôlable ; ou cette machine occupée : busy), puis le plus gros
    modèle, les établies, la plus rapide, la plus récemment vue. PUBLIC_TRIES au plus.
    first : cette machine a le modèle, est libre et passe la première (candidates) ; seule
    elle permet l'amorce (_try_one)."""
    base = public_chat.params(name)
    here = speed.median()
    phone = android.is_android()
    lending = _lending()
    checkable = _controllable(lending) if bigger else set()
    room = _with_room(lending)
    trusted = reputation.established_among(node["key"] for node in room)
    held = _held_models()               # la règle des petits réseaux (cf. le début du module)
    small = _tagged(name) not in held
    found, fresh = [], []
    for node in room:
        key = node["key"]
        same = next((m for m in node["models"] if same_model(m, name) and not reserved(m)),
                    None)
        larger = max(_larger(node["models"], base, checkable), default=None) if bigger else None
        # Un modèle plus gros pas contrôlable, accepté : par la règle des petits réseaux seulement
        wild = max(_unchecked(node["models"], base, checkable, held), default=None) \
            if bigger and larger is None else None
        established = key in trusted
        # La règle des petits réseaux peut-elle servir à cette machine, et avec quel modèle ?
        # Non établie, sans faute (_blameless, lu plus bas), un modèle que personne d'établi ne
        # prête : le plus gros accepté (wild), sinon ce modèle
        rule = None
        if not established and larger is None:
            rule = wild[1] if wild is not None else same if small else None
        measured = _measured(node)
        faster = _faster(measured, here, phone)
        unverified = rule is not None and faster and _blameless(key)
        chosen = larger[1] if larger is not None else rule if unverified else same
        if chosen is not None:
            found.append(_candidate(
                node, chosen, measured, established=established, unverified=unverified,
                ahead=(faster and established) or unverified or larger is not None or busy))
        if rule is not None and measured is None:
            # Jamais mesurée : l'amorce, peut-être (avec ce modèle-là) ; sans candidate (un
            # modèle plus gros pas contrôlable seulement), elle ne sert pas autrement
            fresh.append((found[-1] if chosen is not None else None, node, rule))
    if first and phone and fresh and not any(c.ahead for c in found):
        tried = _try_one(fresh, here)
        if tried is not None:
            c, node, rule = tried
            found = [other for other in found if other is not c]
            found.append(_candidate(node, rule, None, established=False, unverified=True,
                                    ahead=True))
    found.sort(key=lambda c: (not c.ahead, -(c.params or 0), not c.established, -(c.tps or 0),
                              c.seen))
    return _offer(found[:PUBLIC_TRIES])


def _candidate(node, chosen, measured, **marks) -> Candidate:
    """Une machine publique candidate, avec ce modèle (marks : established, unverified,
    ahead)."""
    return Candidate(
        model=public_chat.model_id(node["key"], chosen),
        node_id=public_chat.PREFIX + node["key"], machine=t("placement.public_machine"),
        trust="public", params=public_chat.params(chosen), tps=_speed(node, measured),
        seen=int(node.get("seen_s") or 0), **marks)


def _held_models() -> set:
    """Les modèles (avec leur étiquette, _tagged) que prête une machine publique établie ici
    (reputation.established) : pour eux, la règle stricte. Celles qui prêtent, place libre ou
    non, même écartées quelques minutes après un échec (reputation.bench : établie le reste,
    et un menteur qui ferait refuser une machine établie, ou échouer, ne rouvrirait pas ainsi
    la règle des petits réseaux)."""
    nodes = _lending(everyone=True)
    trusted = reputation.established_among(node["key"] for node in nodes)
    return {_tagged(m) for node in nodes if node["key"] in trusted
            for m in node["models"] if not reserved(m)}


def _blameless(key) -> bool:
    """Cette machine publique n'a-t-elle jamais été prise en faute ici (aucun contrôle raté,
    même passé, ni mise de côté pour de bon) ? Seule une telle machine profite de la règle des
    petits réseaux."""
    row = reputation.get(key)
    return row is None or not (row["checks_failed"] or row["forever"])


def _try_one(fresh: list, here) -> tuple | None:
    """L'amorce de la mesure (cf. le début du module) : parmi ces machines publiques jamais
    mesurées ici ([(candidate ou None, nœud, modèle de la règle des petits réseaux)]), une
    seule, sans faute, qui ne se dit pas elle-même trop lente (sa vitesse annoncée, si celle de
    ce téléphone est connue : au moins FASTER fois celle-ci), passe devant, la plus ancienne
    (_seniority) d'abord, puis celle qui se dit la plus rapide, la plus récemment vue ; au plus
    une fois par TRIAL_S. → l'élément choisi, que _public met devant, marqué ; None : aucune."""
    fit = [entry for entry in fresh
           if here is None or (_told(entry[1]) or 0) >= FASTER * here]
    if not fit:
        return None
    with _lock:
        if _trial["at"] is not None and time.monotonic() - _trial["at"] < TRIAL_S:
            return None
    known = _known([node for _c, node, _rule in fit])
    fit = [entry for entry in fit if _blameless(entry[1]["key"])]
    if not fit:
        return None
    chosen = min(fit, key=lambda entry: (-_seniority(known[entry[1]["key"]]),
                                         -(_told(entry[1]) or 0),
                                         int(entry[1].get("seen_s") or 0)))
    with _lock:
        if _trial["at"] is not None and time.monotonic() - _trial["at"] < TRIAL_S:
            return None                             # une autre conversation l'a prise
        _trial["at"] = time.monotonic()
    # i18n: tel quel (journal) ; la clé abrégée et le modèle
    log.info("Réseau public : %s jamais mesurée, essayée d'abord avec %s (petit réseau)",
             reputation.short(chosen[1]["key"]), chosen[2])
    return chosen


def _offer(found: list) -> list:
    """Ces machines publiques sont proposées : leur profil annonce le prêt, une place (moins
    les conversations que je viens de leur envoyer) et ce modèle. Notées (public_refusals),
    pour qu'un refus qui le contredit compte. → found."""
    for c in found:
        public_refusals.offered(*public_chat.parse(c.model))
    return found


def best_public(offer: bool = True) -> list:
    """« Automatique · réseau public » (PUBLIC_BEST) : chaque machine du réseau public qui prête
    et a une place, avec son meilleur modèle de conversation contrôlable (_controllable ; le
    plus grand nombre de paramètres lu dans le nom, public_chat.params ; un nom qui n'en dit
    rien passe après), le meilleur d'abord, puis les établies, la plus rapide, la plus
    récemment vue. Aucun modèle contrôlable : n'importe lequel, en repli (ci-dessous).
    PUBLIC_TRIES au plus. offer faux : rien n'est noté (le menu des modèles dit seulement ce
    que prendrait « Automatique », chat/model_menu.py)."""
    lending = _lending()
    found = _best(lending, _controllable(lending))
    if not found:
        # Repli (décision du 28/10/2026, à revoir avec l'auteur) : sur un réseau encore
        # minuscule, aucun modèle n'est contrôlable (un seul PC qui prête qwen3.5:9b), et un
        # téléphone sans modèle n'aurait aucune réponse. Il prend alors un modèle que personne
        # ne peut contrôler : une machine établie d'abord (un passé ici), puis une qui a déjà
        # répondu ici sans faute, puis la plus ancienne (une identité neuve, celle d'un menteur
        # qui vient d'en créer, passe après), puis le plus gros modèle. Un menteur seul sur le
        # réseau peut encore être choisi ; un contrôle le prendra dès que d'autres machines
        # auront le même modèle (29/10/2026 au soir : établie et ancienneté, avant la taille)
        found = _best(lending, None)
        found.sort(key=lambda c: (not c.established,
                                  not _served_well(c.node_id.removeprefix(public_chat.PREFIX)),
                                  -_seniority(c.known), -(c.params or 0), -(c.tps or 0), c.seen))
    return _offer(found[:PUBLIC_TRIES]) if offer else found[:PUBLIC_TRIES]


def _best(lending, checkable) -> list:
    """Chaque machine qui prête et a une place, avec son plus gros modèle de conversation,
    contrôlable (dans `checkable`) ou n'importe lequel (None), le plus gros d'abord, puis les
    établies, la plus rapide, la plus récemment vue (cf. best_public)."""
    room = _with_room(lending)
    trusted = reputation.established_among(node["key"] for node in room)
    known = _known(room)
    found = []
    for node in room:
        talking = [m for m in node["models"] if public_chat.converses(m) and not reserved(m)
                   and (checkable is None or _tagged(m) in checkable)]
        if not talking:
            continue
        chosen = max(talking, key=lambda m: public_chat.params(m) or 0)
        found.append(Candidate(
            model=public_chat.model_id(node["key"], chosen),
            node_id=public_chat.PREFIX + node["key"], machine=t("placement.public_machine"),
            trust="public", params=public_chat.params(chosen), tps=_speed(node),
            seen=int(node.get("seen_s") or 0), ahead=True,
            established=node["key"] in trusted, known=known[node["key"]]))
    found.sort(key=lambda c: (-(c.params or 0), not c.established, -(c.tps or 0), c.seen))
    return found


def _known(nodes) -> dict:
    """{clé : secondes depuis lesquelles cette machine publique est connue} : la plus longue de
    deux durées vues d'ici, celle que dit le compagnon (« known_s » : présente sans
    interruption dans son annuaire, remise à zéro quand il redémarre ; un compagnon d'avant le
    29/10 ne la dit pas) et celle de sa première vue gardée en base (sightings.first_seen :
    survit aux redémarrages ; une clé jamais notée, à l'instant). Rien de ce que la machine
    annonce elle-même."""
    firsts = sightings.firsts(node["key"] for node in nodes)
    now = db.now()
    return {node["key"]: int(max(node.get("known_s") or 0,
                                 now - firsts.get(node["key"], now), 0)) for node in nodes}


def _seniority(known) -> int:
    """L'ancienneté d'une machine connue depuis `known` secondes (_known), à un facteur deux
    près : 0 avant SENIOR_S, puis 1, 2, 3… à SENIOR_S, 2 × SENIOR_S, 4 × SENIOR_S… Des machines
    arrivées dans la même période ne se distinguent pas (le plus gros modèle les départage) ;
    une identité neuve passe après une machine là depuis deux fois plus longtemps."""
    return 0 if known < SENIOR_S else 1 + int(math.log2(known / SENIOR_S))


def _served_well(key) -> bool:
    """Cette machine publique a-t-elle déjà répondu ici, sans contrôle raté ?"""
    row = reputation.get(key)
    return bool(row) and row["served"] > 0 and not row["checks_failed"]


def bigger_public(name: str) -> str | None:
    """Le plus gros modèle de conversation contrôlable qu'une machine du réseau public pourrait
    donner à la place de `name` (la carte d'accord le nomme), ou None. À défaut, sur un petit
    réseau, celui que la règle des petits réseaux donnerait une fois l'accord donné
    (_unchecked) : chez une machine non établie, sans faute, mesurée FASTER fois plus rapide
    que celle-ci, ou, sur un téléphone, jamais mesurée (l'amorce)."""
    base = public_chat.params(name)
    lending = _lending()
    checkable = _controllable(lending)
    room = _with_room(lending)
    found = [pair for node in room for pair in _larger(node["models"], base, checkable)]
    if found or base is None:
        return max(found)[1] if found else None
    held, here, phone = _held_models(), speed.median(), android.is_android()
    trusted = reputation.established_among(node["key"] for node in room)
    for node in room:
        wild = [] if node["key"] in trusted else _unchecked(node["models"], base, checkable, held)
        if not wild:
            continue
        measured = _measured(node)
        if (_faster(measured, here, phone) or (phone and measured is None)) \
                and _blameless(node["key"]):
            found += wild
    return max(found)[1] if found else None


# ------------------------------------------------- « Automatique » : le meilleur modèle
def power(name, size=0) -> float:
    """La puissance d'un modèle de conversation : ses milliards de paramètres lus dans le nom
    (public_chat.params), sinon estimés d'après la taille de ses fichiers (BYTES_PER_BILLION)."""
    found = public_chat.params(name)
    if found is not None:
        return found
    finite = isinstance(size, (int, float)) and not isinstance(size, bool) and math.isfinite(size)
    return size / BYTES_PER_BILLION if finite and size > 0 else 0.0


def own_ranked() -> list:
    """Les noms des modèles de conversation que cette machine et mes machines peuvent servir
    d'office (eligible : celles d'un ami si auto_route_friends le permet), chacun une fois :
    d'abord ceux qui tiennent dans la mémoire vidéo d'une machine (« une machine qui peut le
    faire tourner », demande de l'auteur du 29/10/2026 : un modèle de 35 milliards sur une
    carte de 8 Go répondrait au processeur, très lentement), le plus puissant d'abord (power),
    puis, à égalité, déjà chargé quelque part, par nom (cf. le début du module)."""
    found = {}
    profiles = [_local_profile()] + [peer.get("profile") for peer in peers.list_peers()
                                     if eligible(peer)]
    for profile in profiles:
        for m in remote_models.chat_models(profile):
            name = m["name"]
            if reserved(name) or not public_chat.converses(name):
                continue
            seen = found.setdefault(name, {"size": 0, "fits": False, "loaded": False})
            seen["size"] = max(seen["size"], m["size"])
            seen["fits"] = seen["fits"] or m["fits_vram"] is True
            seen["loaded"] = seen["loaded"] or m["loaded"] is True
    return sorted(found, key=lambda n: (not found[n]["fits"], -power(n, found[n]["size"]),
                                        not found[n]["loaded"], n))


def best_name() -> str | None:
    """Le modèle que prendrait « Automatique » parmi les miens (own_ranked), ou None : aucun
    modèle de conversation ici ni sur mes machines."""
    ranked = own_ranked()
    return ranked[0] if ranked else None


def public_offers() -> list:
    """Les modèles de conversation que prêtent en ce moment des machines du réseau public qui
    ont une place, et qu'une autre machine peut contrôler (_controllable : un nom seul ne fait
    pas un modèle), chacun une fois : ceux que le menu des modèles propose (chat/model_menu.py,
    « auto:<modèle> »). Rien n'est noté ni essayé."""
    lending = _lending()
    checkable = _controllable(lending)
    return sorted({m for node in _with_room(lending) for m in node["models"]
                   if public_chat.converses(m) and not reserved(m) and _tagged(m) in checkable})


def _automatic(executor=None, public=False, bigger=False) -> list:
    """« Automatique » (BEST) : les machines du plus puissant de mes modèles (candidates, le
    réseau public compris si public, avec ses règles), puis, en repli, celles des modèles plus
    modestes, chez cette machine et mes machines seulement. Aucun modèle à moi : le meilleur du
    réseau public (best_public), si public."""
    names = own_ranked()
    if not names:
        return best_public() if public else []
    ranked = candidates(names[0], executor, public, bigger)
    seen = {c.model for c in ranked}
    for name in names[1:]:
        for c in candidates(name, executor):
            if c.model not in seen:
                seen.add(c.model)
                ranked.append(c)
    return ranked


def candidates(name: str, executor=None, public=False, bigger=False) -> list:
    """Machines qui peuvent répondre avec ce modèle, la mieux placée d'abord. public : celles
    du réseau public aussi ; bigger : avec un modèle plus gros, s'il est accepté. Cette
    machine occupée : les machines publiques libres passent devant elle (cf. _public)."""
    found = _remote(name)
    here = _local(name, executor)
    if here:
        found.append(here)
    ranked = sorted(found, key=Candidate.key)
    if public:
        # first : cette machine, libre, serait la première servie (aucune autre de mes machines
        # libre : une machine publique ne passe jamais devant elles) ; une machine publique
        # essayée avant elle (l'amorce) serait donc bien servie
        alone = here is not None and here.chats == 0 \
            and not any(c.chats == 0 for c in ranked if c is not here)
        publics = _public(name, bigger, busy=here is not None and here.chats > 0, first=alone)
        ahead = [c for c in publics if c.ahead]
        if ahead and here is not None:
            # Devant cette machine, jamais devant une de mes machines libres (mes machines
            # d'abord) : libre, cette machine passe alors après elles ; occupée, elle garde sa
            # place parmi les machines occupées
            at = next(i for i, c in enumerate(ranked) if c is here)
            del ranked[at]
            if here.chats == 0:
                at = next((i for i, c in enumerate(ranked) if c.chats > 0), len(ranked))
            ranked[at:at] = [*ahead, here]
        else:
            ranked += ahead
        ranked += [c for c in publics if not c.ahead]        # à vitesse comparable : en dernier
    return ranked


def plan(model, executor=None, public=False, bigger=False, unverified=None) -> list:
    """Identifiants concrets à essayer, dans l'ordre. Un modèle précis (cette machine,
    une machine appairée) passe tel quel ; « auto:<modèle> » est réparti. public, bigger :
    cf. candidates (la conversation le permet, chat/public_consent.py) ; jamais pour un
    modèle précis. « Automatique · réseau public » (PUBLIC_BEST) : les machines du réseau
    public seules (best_public), si public. « Automatique » (BEST) : le plus puissant de mes
    modèles, puis les plus modestes (_automatic). unverified : un ensemble, s'il est donné, qui
    reçoit les identifiants des machines publiques passées devant par la règle des petits
    réseaux (cf. le début du module) : leur réponse est « non vérifiable pour l'instant ».

    Une machine du réseau public (« public:<clé>:<modèle> ») ne se désigne jamais à la main :
    seul ce choix la propose, quand la conversation le permet (relecture du 28/10/2026 : passé
    tel quel, un tel identifiant partait chez l'inconnu malgré use_public, « Sur mes machines
    seulement », les pièces jointes, l'accord d'un modèle plus gros et la mise de côté)."""
    name = parse(model)
    if name is None:
        if public_chat.parse(model):
            raise NoMachine(t("placement.public_by_hand"))
        return [model]
    if model == PUBLIC_BEST:
        ranked = best_public() if public else []
        if not ranked:
            raise NoMachine(t("placement.no_public" if public else "placement.public_closed"))
    elif model == BEST:
        ranked = _automatic(executor, public, bigger)
        if not ranked:
            raise NoMachine(t("placement.no_public" if public else "placement.no_model"))
    else:
        ranked = candidates(name, executor, public, bigger)
        if not ranked:
            raise NoMachine(t("placement.no_machine", model=name))
    if unverified is not None:
        unverified.update(c.model for c in ranked if c.unverified)
    log.info("Mode automatique, « %s » : %s", name,
             # i18n: tel quel (journal)
             ", ".join(f"{c.machine} ({c.chats} conv., {c.units} unités)" if c.trust != "public"
                       else f"{c.machine} ({public_chat.parse(c.model)[1]}, {c.tps} j/s"
                            f"{', non vérifiable' if c.unverified else ''})"  # i18n: tel quel
                       for c in ranked))
    return [c.model for c in ranked]


def machine_of(model) -> str | None:
    """Nom de la machine qui répond avec cet identifiant concret (None : cette machine)."""
    if cluster.parse(model):
        return t("placement.split")
    if public_chat.parse(model):
        return t("placement.public_machine")
    parsed = remote_models.parse(model)
    if not parsed:
        return None
    peer = peers.get(parsed[0])
    return peer["name"] if peer else t("placement.other_machine")


def destination(model) -> str | None:
    """Niveau de confiance de la machine qui répond : None (cette machine), « mine »,
    « friend » ou « public » (cf. pipeline.build_messages)."""
    if public_chat.parse(model):
        return "public"
    parsed = remote_models.parse(model)
    if not parsed:
        return None
    return peers.trust_of(parsed[0]) or "friend"


def _target(model) -> str | None:
    """La machine (autre que celle-ci) dont cette réponse compte dans la charge : une machine
    appairée, ou « public:<clé> »."""
    parsed = remote_models.parse(model)
    if parsed:
        return parsed[0]
    lent = public_chat.parse(model)
    return public_chat.PREFIX + lent[0] if lent else None


# ------------------------------------------------------------------ réponse
@contextmanager
def _occupy(model, executor, interactive: bool = True):
    """Pendant une réponse : ici, les unités GPU de fond attendent (l'interactif
    d'abord) ; ailleurs, la conversation compte dans la charge de cette machine. Un gros
    modèle réparti, qui ne tourne pas seulement ici, garde la règle de chat.py avant le
    mode automatique (pipeline.runs_here) : pas de pause. interactive faux (une appli en
    priorité basse, gateway/scopes.py) : rien n'est suspendu ici pour elle."""
    target = _target(model)
    local = (executor is not None and interactive and target is None
             and not cluster.parse(model))
    if local:
        executor.interactive_begin()
    elif target:
        with _lock:
            _sent[target] = _sent.get(target, 0) + 1
            models = _sent_models.setdefault(target, {})
            models[model] = models.get(model, 0) + 1
    try:
        yield
    finally:
        if local:
            executor.interactive_end()
        elif target:
            with _lock:
                left = _sent.get(target, 1) - 1
                if left > 0:
                    _sent[target] = left
                else:
                    _sent.pop(target, None)
                models = _sent_models.get(target, {})
                if models.get(model, 1) > 1:
                    models[model] -= 1
                else:
                    models.pop(model, None)
                if not models:
                    _sent_models.pop(target, None)


# N'engage pas encore la réponse (« notice » : une carte ou une note pour l'interface,
# chat/public_consent.py)
QUIET = {"route", "summary", "inherit", "sources", "status", "notice"}


def _hands_over(exc) -> bool:
    """Une autre machine peut-elle répondre à la place ? Oui si celle-ci est injoignable,
    refuse ou n'a plus le modèle (404), ou a failli (5xx) ; non si c'est la demande elle-même
    qui est refusée (400…) : ailleurs, elle le serait aussi."""
    status = getattr(exc, "status", None)
    return not (isinstance(status, int) and 400 <= status < 500
                and status not in (404, 408, 429))


def run(models: list, attempt, executor=None, stop=None, interactive: bool = True):
    """Couples (genre, valeur) de la réponse, par la première machine du plan qui répond.

    attempt(modèle) produit les couples d'un essai. Avant chacun vient ("route", modèle).
    Tant que l'essai n'a produit que des couples QUIET, un échec fait passer à la machine
    suivante (cf. _hands_over) ; après, ou au dernier essai, ou si la réponse a été
    arrêtée, il remonte."""
    for i, model in enumerate(models):
        yield "route", model
        started = False
        try:
            with _occupy(model, executor, interactive):
                for kind, value in attempt(model):
                    if kind not in QUIET:
                        started = True
                    yield kind, value
            return
        except Exception as exc:
            if (started or i + 1 == len(models) or (stop is not None and stop.is_set())
                    or not _hands_over(exc)):
                raise
            # Une machine publique : sa clé abrégée au journal, comme partout (reputation.short)
            shown, parsed = model, public_chat.parse(model)
            if parsed:
                shown = f"{public_chat.PREFIX}{reputation.short(parsed[0])}:{parsed[1]}"
            log.info("« %s » n'a pas pu répondre (%s) : essai de la machine suivante",
                     shown, exc)


# ------------------------------------------------------- choix proposé à l'interface
def auto_entries(models: list) -> list:
    """Modèles proposés en mode automatique, une fois chacun, à partir des modèles de
    cette machine et de ceux prêtés (pipeline.available_models) : un modèle n'y figure
    que si au moins une machine peut le servir d'office."""
    by_name = {}
    here = t("placement.this_machine")
    for m in models:
        if m.get("group") == "local":
            by_name.setdefault(m["label"], []).append((here, m))
        elif m.get("group") == "remote" and m.get("auto"):
            by_name.setdefault(m["label"], []).append((m["peer"], m))
    entries = []
    for name, found in by_name.items():
        first = found[0][1]
        entries.append({"id": auto_id(name), "label": name, "group": "auto",
                        "size": first.get("size", 0), "caps": first.get("caps") or [],
                        "machines": [machine for machine, _ in found]})
    return entries
