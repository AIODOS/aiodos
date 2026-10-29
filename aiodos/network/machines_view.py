"""« Machines connectées » en une seule liste (demandé par l'auteur le 29/10/2026) : toutes
les machines que cette machine voit, quel que soit le chemin, chacune une seule fois, pour le
bloc unique de la vue Réseau (js/views/connected.js) et la barre du bas.

Les sources, toutes dans l'état du réseau (NetworkManager.snapshot) : mes machines et celles
de mes amis (appairées, peers.py, avec leur activité en direct, activity.py), les machines
détectées sur le réseau local (balises, discovery.py), les appairages en cours (pairing.py),
les présentations à accepter (group.py) et les nœuds du réseau public (public.py). Une
machine vue à plusieurs titres n'apparaît qu'une fois, sous sa sorte la plus proche (mes
machines > amis > réseau public > réseau local), ses autres chemins dans « paths » :
- appairée et vue sur le réseau local (sa balise, ou le chemin direct) : « lan » ;
- appairée et dans l'annuaire du réseau public : reconnue par la clé publique qu'elle donne
  à mes machines seulement (activity.field, « public_key » ; jamais à un ami : sa clé
  publique ne le relie pas à sa machine appairée, docs/conception/reseau-public-calcul.md) ;
- un appairage en cours rejoint la machine détectée ou appairée qu'il concerne.

Un nœud du réseau public est un inconnu : jamais nommé, il est dit par les SHORT premiers
caractères de sa clé, jamais plus. Rien d'autre de confidentiel ne sort d'ici : ni clé de
paire, ni clé Internet (net_id), ni clé publique entière, ni texte de conversation.

Le tri (l'auteur : AIODOS relie d'abord des inconnus par Internet) : ce qui attend une
réponse (un code à vérifier, une présentation, une machine mise de côté), puis ce qui est en
activité avec cette machine, puis les machines en ligne, puis celles hors ligne ; dans
chaque groupe, le réseau public, mes machines, les amis, le réseau local (KINDS) ; parmi les
nœuds publics, ceux qui prêtent et ont une place libre d'abord.

Chaque entrée dit ce que l'interface montre sur la ligne (sorte, nom ou abréviation, état,
ce qu'elle fait, modèles, chemins, charge d'un téléphone) et ce que ses actions demandent
(node_id, la demande d'appairage, la présentation) ; le détail d'une machine appairée
(profil, échanges) est lu dans « peers » par l'interface (js/views/machine-row.js).
"""
import math

from aiodos.network import placement, public_chat

KINDS = ("public", "mine", "friend", "local")    # ordre des filtres et du tri
SHORT = 8                     # caractères d'une clé publique montrés, au plus
MAX_MODELS = 24               # noms de modèles gardés par machine (la ligne en montre deux)
MODEL_TEXT = 64               # longueur d'un nom de modèle, au plus
NAME_TEXT = 60                # comme profile.NAME
# Un modèle d'images prêté aux inconnus, dans le profil public d'un nœud (« image:<nom> »,
# network/public_imaging.MARKER, placement.RESERVED) : montré à part, sans la marque
IMAGE_MARKER = "image:"

# Ce qu'une machine fait avec celle-ci, dans l'ordre de la ligne (« doing ») : elle calcule
# pour moi, double mes unités en fin de partie, répond à ma conversation ; je calcule pour
# elle, je réponds à la sienne ; un nœud public répond à ma conversation (public_for_me)
DOING = ("for_me", "copies_for_me", "chat_for_me", "public_for_me", "for_it", "chat_for_it")
ACTIVE = frozenset(DOING)


def short(key) -> str:
    """Les SHORT premiers caractères d'une clé publique : ce qu'on en montre, au plus."""
    return key[:SHORT] if isinstance(key, str) else ""


def _count(value) -> int:
    """Un entier positif reçu d'ailleurs, sinon 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _number(value):
    return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value)) else None


def _text(value, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _models(names) -> tuple:
    """(noms gardés, nombre en tout) : sans doublon, bornés."""
    found = list(dict.fromkeys(n[:MODEL_TEXT] for n in names if isinstance(n, str) and n))
    return found[:MAX_MODELS], len(found)


def public_asking() -> dict:
    """{clé publique : {modèle : réponses en cours}} : mes conversations qu'un nœud du réseau
    public calcule en ce moment (placement.sending : le modèle exact de chaque envoi ; lu,
    jamais changé). Le nombre seulement, jamais le texte."""
    asking = {}
    for target, models in placement.sending().items():
        if not (isinstance(target, str) and target.startswith(public_chat.PREFIX)):
            continue
        for model, n in models.items():
            parsed = public_chat.parse(model)
            if parsed is not None and _count(n):
                key, name = parsed
                per_key = asking.setdefault(key, {})
                per_key[name] = per_key.get(name, 0) + n
    return asking


# ------------------------------------------------------------ machines appairées
def _units(items) -> int:
    return sum(_count(item.get("units")) for item in items or [] if isinstance(item, dict))


def _doing(live: dict) -> list:
    """Ce qu'une machine appairée fait avec celle-ci en ce moment (activity.annotate)."""
    doing = []
    for what, n in (("for_me", _units(live.get("for_me"))),
                    ("copies_for_me", _units(live.get("copies_for_me"))),
                    ("chat_for_me", _count(live.get("chats_for_me"))),
                    ("for_it", len(live.get("for_it") or [])),
                    ("chat_for_it", _count(live.get("chats_for_it")))):
        if n:
            doing.append({"what": what, "n": n})
    return doing


def _public_doing(models: dict | None) -> list:
    """Mes conversations qu'un nœud public calcule : par modèle (public_asking)."""
    return [{"what": "public_for_me", "n": n, "model": _text(model, MODEL_TEXT)}
            for model, n in sorted((models or {}).items())]


def _full(places) -> bool:
    """Toutes ses places prises (places_seen : les places dites en demandant du travail)."""
    if not isinstance(places, dict):
        return False
    total = places.get("places") if isinstance(places.get("places"), dict) else {}
    busy = places.get("busy") if isinstance(places.get("busy"), dict) else {}
    counted = [(n, _count(busy.get(name))) for name, n in total.items() if _count(n)]
    return bool(counted) and all(taken >= n for n, taken in counted)


def _power(profile: dict) -> dict | None:
    """La charge d'un téléphone (en charge, niveau), ou un PC sur batterie ; None sinon."""
    power = profile.get("power") if isinstance(profile.get("power"), dict) else {}
    if power.get("mobile") is True:
        plugged = power.get("plugged")
        return {"mobile": True, "plugged": plugged if isinstance(plugged, bool) else None,
                "level": _number(power.get("level"))}
    if profile.get("on_ac_power") is False:
        return {"mobile": False, "plugged": False, "level": None}
    return None


def _paired_state(peer: dict, live: dict, doing: list, lends: bool) -> str:
    """offline, set_aside, busy (en activité avec moi, ou toutes ses places prises), paused
    (son partage en pause), free (elle prête quelque chose), online (elle ne prête rien)."""
    if peer.get("set_aside_at"):
        return "set_aside"
    if not peer.get("online"):
        return "offline"
    if doing or _full(live.get("places")):
        return "busy"
    sharing = live.get("sharing") if isinstance(live.get("sharing"), dict) else {}
    if sharing.get("enabled") is True and sharing.get("active") is False:
        return "paused"
    return "free" if lends else "online"


def _paired(peer: dict, lan: dict, public_keys: dict, by_key: dict, asking: dict) -> dict:
    node_id = peer["node_id"]
    live = peer.get("live") if isinstance(peer.get("live"), dict) else {}
    profile = peer.get("profile") if isinstance(peer.get("profile"), dict) else {}
    ollama = profile.get("ollama") if isinstance(profile.get("ollama"), dict) else {}
    listed = ollama.get("models") if isinstance(ollama.get("models"), list) else []
    models, models_n = _models(m.get("name") for m in listed if isinstance(m, dict))
    sharing = live.get("sharing") if isinstance(live.get("sharing"), dict) else None
    images = profile.get("images") if isinstance(profile.get("images"), dict) else {}
    lends = bool((sharing or {}).get("enabled") if sharing is not None else images.get("shares")) \
        or profile.get("lends_models") is True \
        or (isinstance(profile.get("cluster"), dict) and profile["cluster"].get("lends") is True)
    doing = _doing(live)
    paths = []
    route = live.get("route")
    if route == "lan" or node_id in lan:
        paths.append("lan")
    inet = peer.get("internet") if isinstance(peer.get("internet"), dict) else {}
    if route in ("direct", "relay", "internet") or inet.get("connected"):
        paths.append("internet")
    public = None
    node = by_key.get(public_keys.get(node_id))
    if node is not None:                       # aussi dans l'annuaire du réseau public
        paths.append("public")
        public = _public_facts(node)
        doing += _public_doing(asking.get(node["key"]))
    state = _paired_state(peer, live, doing, lends)
    seen_lan = lan.get(node_id) or {}
    rtt = _number(live.get("rtt_ms"))
    if rtt is None:
        rtt = _number(live.get("internet_rtt_ms"))
    actions = ["refresh", "options", "unpair"]
    if peer.get("set_aside_at"):
        actions.insert(0, "restore")
    return {
        "id": f"peer:{node_id}", "kind": "friend" if peer.get("trust") == "friend" else "mine",
        "name": _text(peer.get("name"), NAME_TEXT), "short": "", "node_id": node_id,
        "online": bool(peer.get("online")), "state": state,
        "active": any(d["what"] in ACTIVE for d in doing), "attention": state == "set_aside",
        "doing": doing, "models": models, "models_n": models_n, "lends": lends,
        "power": _power(profile), "paths": paths,
        "route": route if isinstance(route, str) else None, "rtt_ms": rtt,
        "seen": _number(peer.get("last_seen")),
        "address": _text(seen_lan.get("address") or peer.get("address"), 64),
        "port": _count(seen_lan.get("port") or peer.get("port")) or None,
        "version": _text(profile.get("version"), 40),
        "public": public, "pairing": None, "intro": None, "actions": actions,
    }


# -------------------------------------------------------------- réseau public
def _public_facts(node: dict) -> dict:
    """Ce qu'un nœud public dit de lui (public._node, déjà typé), sa clé abrégée."""
    free = node.get("free")
    tps = _number(node.get("tps"))
    return {"short": short(node.get("key")), "lends": node.get("lends") is True,
            "free": _count(free) if isinstance(free, int) else None,
            "tps": tps if tps is not None and tps > 0 else None}


def _public(node: dict, asking: dict) -> dict:
    facts = _public_facts(node)
    announced = [n for n in node.get("models") or [] if isinstance(n, str)]
    models, models_n = _models(n for n in announced if not n.startswith(IMAGE_MARKER))
    facts["images"] = _models(n[len(IMAGE_MARKER):] for n in announced
                              if n.startswith(IMAGE_MARKER))[0]
    doing = _public_doing(asking.get(node["key"]))
    # Libre : elle prête et a une place ; occupée : elle répond à ma conversation, ou prête
    # sans place libre ; en ligne : elle ne prête pas aux inconnus (l'annuaire seulement)
    if doing or (facts["lends"] and facts["free"] == 0):
        state = "busy"
    else:
        state = "free" if facts["lends"] and facts["free"] else "online"
    return {
        "id": f"public:{facts['short']}", "kind": "public", "name": "", "short": facts["short"],
        "node_id": None, "online": True, "state": state, "active": bool(doing),
        "attention": False, "doing": doing, "models": models, "models_n": models_n,
        "lends": facts["lends"], "power": None, "paths": ["public"], "route": None,
        "rtt_ms": None, "seen_s": _count(node.get("seen_s")),
        "address": "", "port": None, "version": _text(node.get("version"), 40),
        "public": facts, "pairing": None, "intro": None, "actions": [],
    }


# ------------------------------------------------- réseau local, appairages, présentations
def _shown_pairing(p: dict) -> dict:
    """Une demande d'appairage telle que la fenêtre de vérification la demande (jamais la
    clé : pairing._shown n'en donne pas)."""
    return {"node_id": p.get("node_id"), "name": _text(p.get("name"), NAME_TEXT),
            "code": _text(p.get("code"), 12), "ref": p.get("ref"),
            "address": _text(p.get("address"), 64), "internet": bool(p.get("internet")),
            "known": p.get("known"), "local_ok": bool(p.get("local_ok"))}


def _with_pairing(entry: dict, pairing: dict) -> dict:
    """Un appairage en cours, posé sur la machine qu'il concerne : à confirmer ici, ou en
    attente de l'autre machine."""
    shown = _shown_pairing(pairing)
    entry["pairing"] = shown
    if not shown["local_ok"]:
        entry["state"], entry["attention"] = "confirm", True
        entry["actions"] = ["check_code"] + [a for a in entry["actions"] if a != "pair"]
    elif entry["state"] in ("detected", "online", "free"):
        entry["state"] = "pairing"
        entry["actions"] = [a for a in entry["actions"] if a != "pair"]
    return entry


def _local(seen: dict) -> dict:
    return {
        "id": f"local:{seen['node_id']}", "kind": "local",
        "name": _text(seen.get("name"), NAME_TEXT), "short": "", "node_id": seen["node_id"],
        "online": True, "state": "detected", "active": False, "attention": False, "doing": [],
        "models": [], "models_n": 0, "lends": False, "power": None, "paths": ["lan"],
        "route": None, "rtt_ms": None, "seen": _number(seen.get("last_seen")),
        "address": _text(seen.get("address"), 64), "port": _count(seen.get("port")) or None,
        "version": "", "public": None, "pairing": None, "intro": None, "actions": ["pair"],
    }


def _pairing_only(p: dict) -> dict:
    """Un appairage en cours avec une machine qui n'est ni détectée ni appairée : sur
    invitation, par Internet (« Inviter un ami » : rangé avec les amis), ou à une adresse
    saisie (réseau local)."""
    entry = _local({"node_id": p.get("node_id") or "", "name": p.get("name"),
                    "address": p.get("address"), "port": None})
    entry["id"] = f"pairing:{p.get('ref') or p.get('node_id')}"
    if p.get("internet"):
        entry.update(kind="friend", paths=["internet"], address="")
    entry["actions"] = []
    return _with_pairing(entry, p)


def _intro(offer: dict, trust_of: dict) -> dict:
    """Une présentation à accepter (appairage par groupe) : la machine présentée aura le
    niveau de confiance de celle qui la présente."""
    trust = trust_of.get(offer.get("presenter_id"))
    return {
        "id": f"intro:{offer.get('intro')}", "kind": "mine" if trust == "mine" else "friend",
        "name": _text(offer.get("machine"), NAME_TEXT), "short": "", "node_id": None,
        "online": False, "state": "introduced", "active": False, "attention": True,
        "doing": [], "models": [], "models_n": 0, "lends": False, "power": None, "paths": [],
        "route": None, "rtt_ms": None, "seen": _number(offer.get("created")), "address": "",
        "port": None, "version": "", "public": None, "pairing": None,
        "intro": {"intro": offer.get("intro"), "machine": _text(offer.get("machine"), NAME_TEXT),
                  "presenter": _text(offer.get("presenter"), NAME_TEXT),
                  "created": _number(offer.get("created"))},
        "actions": ["answer_intro"],
    }


# ---------------------------------------------------------------------- liste
def _rank(entry: dict) -> tuple:
    band = 0 if entry["attention"] else 1 if entry["active"] else 2 if entry["online"] else 3
    if entry["kind"] == "public":
        facts = entry.get("public") or {}
        lender = 0 if facts.get("free") else 1 if facts.get("lends") else 2
        return band, KINDS.index("public"), lender, entry.get("seen_s", 0), entry["short"]
    return band, KINDS.index(entry["kind"]), 0, 0, entry["name"].casefold()


def build(snap: dict, lan: dict | None = None, public_keys: dict | None = None,
          asking: dict | None = None) -> list:
    """La liste unique des machines (cf. le début du module). snap : l'état du réseau
    (NetworkManager.snapshot, sans « machines ») ; lan : {machine appairée : {"address",
    "port"}} vue sur le réseau local (ses balises) ; public_keys : {machine appairée : sa clé
    du réseau public} (activity.public_keys) ; asking : public_asking()."""
    lan, public_keys, asking = lan or {}, public_keys or {}, asking or {}
    public = snap.get("public") if isinstance(snap.get("public"), dict) else {}
    nodes = [n for n in public.get("nodes") or [] if isinstance(n, dict)
             and isinstance(n.get("key"), str) and n["key"]]
    by_key = {n["key"]: n for n in nodes}
    peers = [p for p in snap.get("peers") or [] if isinstance(p, dict) and p.get("node_id")]
    entries = [_paired(p, lan, public_keys, by_key, asking) for p in peers]
    merged = {public_keys.get(p["node_id"]) for p in peers} - {None}
    by_node = {e["node_id"]: e for e in entries}
    for seen in snap.get("visible") or []:
        if isinstance(seen, dict) and seen.get("node_id") and seen["node_id"] not in by_node:
            by_node[seen["node_id"]] = _local(seen)
            entries.append(by_node[seen["node_id"]])
    for p in snap.get("pairing") or []:
        if not isinstance(p, dict):
            continue
        if p.get("node_id") in by_node:
            _with_pairing(by_node[p["node_id"]], p)
        else:
            entries.append(_pairing_only(p))
    trust_of = {p["node_id"]: p.get("trust") for p in peers}
    entries += [_intro(o, trust_of) for o in snap.get("introductions") or []
                if isinstance(o, dict)]
    taken = set()
    for node in nodes:
        if node["key"] in merged:
            continue                          # une de mes machines, déjà sur sa ligne
        entry = _public(node, asking)
        while entry["id"] in taken:           # deux clés au même début : une ligne chacune
            entry["id"] += "+"
        taken.add(entry["id"])
        entries.append(entry)
    return sorted(entries, key=_rank)
