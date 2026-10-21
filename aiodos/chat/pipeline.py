"""Préparation et génération d'une réponse.

Ordre des messages envoyés au modèle :
  prompt système → sommaire des connaissances → historique →
  [résultats web] → [extraits pertinents] → règle anti-invention → question.
La règle anti-invention est placée juste avant la question : c'est la position
la plus forte, celle qui l'emporte sur d'anciens échanges mémorisés (leçon v1).
Les extraits peuvent venir aussi des bibliothèques que mes machines et mes amis
m'ouvrent (network/remote_search.py), si la conversation nomme ces machines : à part
des miens, dans un bloc marqué non vérifié, et seulement pour un modèle autorisé
(peers_allowed).

Quand la réponse est calculée par une autre machine (modèle prêté, choisi à la main ou
par le mode automatique, cf. network/placement.py), le sommaire et les extraits ne
viennent que des bibliothèques qui peuvent aller chez elle (visible_libraries) : rien
d'une bibliothèque gardée sur ce PC, ni de la mémoire des conversations.
"""
import logging
import re

from aiodos import settings
from aiodos.chat import conversations, creations
from aiodos.cluster import group as cluster
from aiodos.i18n import t
from aiodos.knowledge import search, store
from aiodos.network import placement, remote_models, remote_search, sharing
from aiodos.services import cloud, ollama, web

log = logging.getLogger("aiodos.chat")

# Tournures qui suggèrent une recherche web. On matche des radicaux :
# « cherch\w* » couvre cherche, chercher, recherche, rechercher… (bug v1).
WEB_INTENT = re.compile(          # i18n: tel quel (motif de détection, français et anglais)
    r"(cherch\w*|recherch\w*|trouv\w*|google|navigu\w*|sur le web|sur internet|"
    r"en ligne|actualit[ée]s?\w*|site officiel|documentation (en ligne|web|à jour)|"
    r"derni[èe]re?s? (version|nouvelle|info)|m[ée]t[ée]o|qui a gagn\w*|"
    r"\bnews\b|taux de change|cours (de l'action|du bitcoin|de bourse)|"
    r"\bsearch\w*|\blook\w* (it |this |that )?up\b|\bbrowse\b|on the web|on the internet|"
    r"\bonline\b|\bheadlines\b|official (web)?site|(online|web|up-to-date) documentation|"
    r"\blatest (version|release|news|info\w*)|\bweather\b|\bforecast\b|\bwho won\b|"
    r"exchange rate|(stock|share|bitcoin) price)",
    re.IGNORECASE)

# Les consignes données au modèle sont dans chat_prompts.json, dans la langue de la question :
# le sommaire (manifest), les résultats web, les extraits, ceux d'autres machines (rag_peers :
# à part, chacun entre <<< et >>>, marqué non vérifié) et la règle anti-invention (rule_…).
REMOTE_MAX = 6        # passages venus des autres machines, au plus, dans une réponse
# Dans le texte d'un passage d'ailleurs : de quoi imiter une source ou fermer son bloc (les
# en-têtes chat_prompts.knowledge_source et peer_source commencent ainsi dans chaque langue)
_FORGED = (("<<<", "‹‹‹"), (">>>", "›››"), ("[Source", "(Source"), ("[Passage", "(Passage"))


def preflight(text: str, model: str, conversation_id=None, replace_from=None) -> dict:
    """Ce que l'interface doit faire valider avant l'envoi (web, cloud). replace_from : le
    message que celui-ci remplacera (« Modifier »)."""
    provider = cloud.provider_of(model)
    return {
        "remote": remote_models.peer_name(model),     # machine appairée qui répondra
        "web_intent": bool(WEB_INTENT.search(text or "")),
        "cloud": provider,
        "cloud_host": cloud.PROVIDERS[provider]["host"] if provider else None,
        "cloud_has_key": cloud.has_key(provider) if provider else None,
        "cloud_confirm": bool(settings.get("cloud_confirm")),
        "consult": _consult_targets(),                 # chez qui partirait la question
        "consult_allowed": peers_allowed(model),       # et si ce modèle peut s'en servir
        "create": _creation(text, conversation_id, replace_from),   # image ou vidéo à créer
    }


def _creation(text: str, conversation_id=None, before=None) -> dict | None:
    """La demande d'image ou de vidéo du message, ou la retouche de la dernière création
    (chat/creations.py) ; jamais d'erreur ici."""
    try:
        return creations.preflight(text, conversation_id, before)
    except Exception as exc:
        log.warning("Demande de création illisible : %s", exc)
        return None


def _consult_targets() -> list:
    """Machines qui m'ouvrent des bibliothèques ; jamais d'erreur ici (un profil reçu mal
    formé ne doit pas empêcher d'envoyer un message)."""
    try:
        return remote_search.describe()
    except Exception as exc:
        log.warning("Machines à consulter illisibles : %s", exc)
        return []


def runs_here(model) -> bool:
    """Le modèle tourne-t-il sur ce PC (et occupe-t-il son GPU) ? Pour le mode
    automatique, on ne le sait qu'une fois la machine choisie (placement.run) ; un gros
    modèle réparti ne tourne pas seulement ici."""
    return not (cloud.provider_of(model) or remote_models.parse(model)
                or placement.parse(model) or cluster.parse(model))


def peers_allowed(model) -> bool:
    """Les passages trouvés chez mes machines et mes amis (et les réponses qui s'en servent)
    peuvent-ils aller à ce modèle ? Ils ne vont pas plus loin que mes machines : le
    propriétaire les a ouverts à moi, pas à des tiers. Liste d'autorisations, tout le reste
    est refusé (cloud, modèle prêté par un ami, toute nouvelle façon de répondre ailleurs) :
    - un modèle installé ici (Ollama) ;
    - un gros modèle réparti : il tourne ici, avec les cartes de mes seules machines
      (cluster/group.py) ;
    - un modèle prêté par une de mes machines.
    En mode automatique (« auto: »), chaque machine essayée est jugée à son tour (routes/
    chat.py) : ici, vrai si au moins une de celles qui peuvent répondre est autorisée."""
    if placement.parse(model):
        try:
            return any(peers_allowed(m) for m in placement.plan(model))
        except placement.NoMachine:
            return False
    lent = remote_models.parse(model)
    if lent:
        return sharing.level(lent[0]) == sharing.MINE
    if cluster.parse(model):
        return True
    try:
        return str(model or "") in {m["name"] for m in ollama.list_models()}
    except Exception:
        return False                      # Ollama injoignable : dans le doute, non


def available_models() -> list:
    """Modèles de conversation : en mode automatique (une fois chacun), locaux (Ollama),
    ceux de mes machines, les gros modèles répartis entre elles, puis cloud."""
    models = []
    try:
        installed = ollama.list_models()
        all_caps = ollama.capabilities_many([m["name"] for m in installed])
        for m in installed:
            caps = all_caps[m["name"]]
            if caps and "completion" not in caps:      # modèle d'embedding seul
                continue
            models.append({"id": m["name"], "label": m["name"], "group": "local",
                           "size": m["size"], "caps": caps})
    except Exception as exc:
        log.warning("Ollama injoignable : %s", exc)
    try:
        models.extend(remote_models.available())
    except Exception as exc:
        log.warning("Modèles des machines appairées indisponibles : %s", exc)
    try:
        models.extend(cluster.available())             # gros modèles répartis
    except Exception as exc:
        log.warning("Modèles répartis indisponibles : %s", exc)
    models[:0] = placement.auto_entries(models)
    for name, spec in cloud.PROVIDERS.items():
        models.append({"id": cloud.MODEL_PREFIX + name, "label": spec["label"],
                       "group": "cloud", "caps": [], "has_key": cloud.has_key(name)})
    return models


def _stopped(stop) -> bool:
    return stop is not None and stop.is_set()


def _web_context(question, max_results, sources, stop=None):
    """Les pages des premiers résultats, lues l'une après l'autre. stop (Stop pendant la
    préparation) va à la recherche comme à chaque lecture : elle s'interrompt aussitôt, au
    lieu d'aller jusqu'à son délai total (services/web.py : 20 s pour la recherche, 30 s par
    page, qu'une page qui distille ses octets atteint), et les pages suivantes ne sont pas
    lues."""
    blocks = []
    if _stopped(stop):
        return blocks
    try:
        for res in web.web_search(question, max_results, stop=stop):
            if _stopped(stop):
                break
            try:
                title, text = web.fetch_url_text(res["url"], stop=stop)
            except web.FetchStopped:
                break
            except Exception:
                continue
            blocks.append(t("chat_prompts.web_source", title=title, url=res["url"],
                            text=text[:3000]))
            sources.append({"type": "web", "label": web.host_of(res["url"]),
                            "url": res["url"], "title": title})
    except web.FetchStopped:
        pass                          # arrêtée pendant la recherche : rien à lire
    except Exception as exc:
        log.warning("Recherche web impossible : %s", exc)
    return blocks


def web_context(question, stop=None) -> tuple:
    """Recherche web pour la question : (blocs pour le modèle, sources citées). Faite une
    fois par message, même si la réponse change de machine en route (placement.run).
    stop : l'arrêt de la réponse (bouton Stop, client parti) ; levé, plus rien n'est lu."""
    sources = []
    return _web_context(question, settings.get("web_results") or 3, sources, stop), sources


def visible_libraries(destination) -> set | None:
    """Bibliothèques dont le sommaire et les extraits peuvent accompagner une conversation,
    selon la machine qui répond (placement.destination) : None, cette machine : toutes ;
    « mine », une de mes machines, et « cloud », un modèle du cloud après validation
    (décision de l'auteur, 18/10/2026) : toutes sauf celles gardées sur ce PC ; « friend »,
    la machine d'un ami : celles partagées avec mes amis. La mémoire des conversations ne
    va qu'à cette machine (build_messages)."""
    if destination is None:
        return None
    if destination in ("mine", "cloud"):
        return {c["id"] for c in store.list_collections() if c["scope"] != "local"}
    return store.shareable_ids()


def history_filter(destination, peers_ok: bool):
    """Ce que l'historique peut emporter vers la machine qui répond (keep de
    conversations.as_history) : une réponse appuyée sur la mémoire des conversations ne
    quitte pas cette machine, ni une réponse appuyée sur une bibliothèque qui ne peut pas
    aller là (visible_libraries) ; une réponse appuyée sur des passages d'autres machines ne
    va qu'à un modèle autorisé (peers_ok). Une source d'avant l'identifiant de bibliothèque
    est jugée sur son nom, et refusée si aucune bibliothèque visible ne le porte plus. Une
    réponse qui cite des noms du sommaire (source « summary ») suit la même règle que leurs
    bibliothèques."""
    only = visible_libraries(destination)
    names = None if only is None else {
        name for cid, name in store.collection_names().items() if cid in only}

    def keep(sources) -> bool:
        for s in sources:
            kind = s.get("type")
            if kind == "remote" and not peers_ok:
                return False
            if destination is not None and kind == "conversation":
                return False
            if only is not None and kind == "knowledge":
                known = s.get("collection_id")
                if known is not None and known not in only:
                    return False
                if known is None and s.get("collection") not in names:
                    return False
            if only is not None and kind == "summary":
                if any(i not in only for i in s.get("collection_ids") or []):
                    return False
            if kind == "inherited":           # reçu d'une réponse précédente (inherited)
                if s.get("remote") and not peers_ok:
                    return False
                if s.get("memory") and destination is not None:
                    return False
                if only is not None and any(i not in only for i in s.get("collection_ids") or []):
                    return False
        return True
    return keep


def inherited(rows, keep=None) -> dict:
    """Ce qu'une réponse doit aux réponses de son historique (elle a pu les reprendre mot
    pour mot) : leurs bibliothèques, la mémoire, les passages d'autres machines ; seulement
    celles que keep laisse passer (les autres ne lui ont pas été montrées). → {"collection_ids",
    "memory", "remote"}, vide si rien ne restreint. Une source d'avant l'identifiant de
    bibliothèque se retrouve par son nom ; introuvable, elle compte pour une bibliothèque
    que personne d'autre ne voit (-1)."""
    names = {name: cid for cid, name in store.collection_names().items()}
    ids, memory, remote = set(), False, False
    for r in rows:
        if r["role"] != "assistant" or conversations.is_error(r):
            continue
        sources = conversations.sources_of(r["sources"])
        if keep is not None and not keep(sources):
            continue
        for s in sources:
            kind = s.get("type")
            if kind == "knowledge":
                known = s.get("collection_id")
                ids.add(int(known) if known is not None else names.get(s.get("collection"), -1))
            elif kind in ("summary", "inherited"):
                ids.update(int(i) for i in s.get("collection_ids") or [])
                memory = memory or bool(s.get("memory"))
                remote = remote or bool(s.get("remote"))
            elif kind == "conversation":
                memory = True
            elif kind == "remote":
                remote = True
    if not (ids or memory or remote):
        return {}
    return {"collection_ids": sorted(ids), "memory": memory, "remote": remote}


def summary_mentioned(shown: dict, content: str) -> list:
    """Les bibliothèques du sommaire (build_messages, summary) dont la réponse cite un
    élément, en mots entiers (_name_forms). Pas le nom des bibliothèques (« Général » se
    lirait dans trop de réponses)."""
    text = str(content or "").casefold()
    found = []
    for collection_id, names in (shown or {}).items():
        if any(_cites(text, form) for name in names for form in _name_forms(name)):
            found.append(int(collection_id))
    return found


def _cites(text: str, form: str) -> bool:
    """Le nom figure dans le texte en mots entiers (« tarte » ne se lit pas dans
    « tartelette »)."""
    return re.search(r"(?<!\w)" + re.escape(form) + r"(?!\w)", text) is not None


def _name_forms(name: str) -> set:
    """Les formes sous lesquelles une réponse citerait un nom du sommaire : le titre d'une
    conversation de la mémoire (« titre · #12 ») s'il a au moins deux mots (« Bonjour » se
    lirait partout) ; un fichier tel quel, sans son dossier, et sans son extension s'il
    mêle lettres et chiffres ou tirets (pas un mot ni un nombre courants : « tarte »,
    « 2025 »). Rien de moins de quatre caractères."""
    name = str(name or "").strip()
    if " · #" in name:
        title = name.split(" · #")[0].strip()
        return {title.casefold()} if len(title.split()) >= 2 and len(title) >= 4 else set()
    base = name.replace(chr(92), "/").rsplit("/", 1)[-1]
    forms = {name, base}
    stem = base.rsplit(".", 1)[0]
    if stem != base and any(ch.isalpha() for ch in stem) and any(not ch.isalpha() for ch in stem):
        forms.add(stem)
    return {f.casefold() for f in forms if len(f.strip()) >= 4}


def select_relevant(hits, min_score, limit=4) -> list:
    """Ne garde que les extraits vraiment liés à la question.

    Sans seuil, la recherche renvoie toujours quelque chose et l'IA cite des
    sources sans rapport. Le seuil dépend du modèle d'embedding, chacun ayant
    son échelle de scores (settings.RAG_THRESHOLDS, docs/embeddings.md) : en
    français, avec embeddinggemma, les bons passages dépassent 0,45 et le bruit
    de fond reste sous 0,32 dans 99 % des cas (nomic-embed-text : 0,55 à 0,75).

    Pas d'écart maximal avec le meilleur extrait : la mémoire peut contenir la
    même question mot pour mot (0,89), et un tel écart écarterait alors les
    vraies sources (0,52) au profit de ce doublon.
    """
    return [h for h in hits if h["score"] >= min_score][:limit]


def _knowledge_context(question, libraries, conversations, sources, conversation_id=None,
                       only=None):
    try:
        vector = ollama.embed([question])[0]
        hits = search.search(vector, k=8, libraries=libraries, conversations=conversations,
                             exclude_conversation=conversation_id, only=only)
    except Exception as exc:
        log.warning("Recherche dans les connaissances impossible : %s", exc)
        return []
    hits = select_relevant(hits, float(settings.get("rag_min_score") or 0))
    seen = set()
    for h in hits:
        if (h["collection"], h["source"]) in seen:
            continue
        seen.add((h["collection"], h["source"]))
        sources.append({"type": "conversation" if h["kind"] == "conversation" else "knowledge",
                        "label": h["source"], "collection": h["collection"],
                        "collection_id": h.get("collection_id"),
                        "score": round(h["score"], 3)})
    return [t("chat_prompts.knowledge_source", source=h["source"], text=h["text"]) for h in hits]


def _unforged(text: str) -> str:
    for forged, harmless in _FORGED:
        text = text.replace(forged, harmless)
    return text


def _peer_context(question, sources, accepted):
    """Passages trouvés dans les bibliothèques ouvertes des machines acceptées (`accepted`) :
    le meilleur de chacune d'abord, REMOTE_MAX au plus ; chaque citation dit chez qui. Les
    noms arrivent nettoyés (remote_search.name) ; le texte ne peut ni fermer son bloc ni
    imiter une source d'ici."""
    try:
        found, _machines = remote_search.search(question, accepted)
    except Exception as exc:
        log.warning("Recherche chez les autres machines impossible : %s", exc)
        return []
    found = sorted(found, key=lambda p: (p["rank"], -p["score"]))[:REMOTE_MAX]
    seen = set()
    for p in found:
        if (p["node_id"], p["library"], p["document"]) in seen:
            continue
        seen.add((p["node_id"], p["library"], p["document"]))
        sources.append({"type": "remote", "label": p["document"], "collection": p["library"],
                        "peer": p["peer"], "score": round(p["score"], 3)})
    return [t("chat_prompts.peer_source", document=p["document"], library=p["library"],
              peer=p["peer"], text=_unforged(p["text"])) for p in found]


def _can_create() -> str | None:
    """AIODOS sait créer des images : le modèle de conversation doit le savoir."""
    try:
        return creations.capability_note()
    except Exception as exc:
        log.warning("Modèles d'images illisibles : %s", exc)
        return None


def build_messages(history, question, *, libraries, conversations, web_search,
                   conversation_id=None, destination=None, web=None, peers=None,
                   summary=None, stop=None):
    """Construit les messages pour le modèle. → (messages, sources citées)

    summary : un dict à remplir avec les noms montrés au modèle dans le sommaire des
    bibliothèques, {id de bibliothèque : [noms de ses éléments montrés]} ; une réponse
    qui les cite s'en est servie (summary_mentioned).

    destination : machine qui répondra (cf. visible_libraries) ; web : résultats déjà
    obtenus par web_context, sinon la recherche est faite ici si web_search, et stop
    (l'arrêt de la réponse) interrompt la lecture de ses pages.
    peers : identifiants des machines où chercher aussi, parmi celles qui m'ouvrent des
    bibliothèques (la question part chez elles) : celles que l'utilisateur a acceptées.
    Aucune par défaut."""
    only = visible_libraries(destination)
    conversations = conversations and destination is None    # la mémoire reste ici
    messages = [{"role": "system", "content": settings.get("system_prompt")}]
    note = _can_create()
    if note:
        messages.append({"role": "system", "content": note})
    sources = []
    if libraries or conversations:
        parts = search.manifest_parts(libraries, conversations, only=only)
        if parts:
            messages.append({"role": "system", "content": t(
                "chat_prompts.manifest", content=search.manifest_text(parts))})
            if summary is not None:               # les noms montrés (summary_mentioned) ;
                own = f" · #{conversation_id}"    # pas la mémoire de cette conversation-ci
                summary.update({cid: [n for n in shown if not n.endswith(own)]
                                for cid, _name, _total, shown in parts})
    messages.extend(history)

    web_ok = False
    if web_search:
        blocks, found = web if web is not None else web_context(question, stop)
        sources.extend(found)
        web_ok = bool(blocks)
        messages.append({"role": "system", "content": t(
            "chat_prompts.web_results", results="\n\n".join(blocks)) if web_ok
            else t("chat_prompts.web_empty")})
    if libraries or conversations:
        extracts = _knowledge_context(question, libraries, conversations, sources,
                                      conversation_id, only)
        if extracts:
            messages.append({"role": "system", "content": t(
                "chat_prompts.rag", excerpts="\n\n".join(extracts))})
    remote = _peer_context(question, sources, peers) if peers else []
    if remote:                            # à part, après les miens : jamais mêlés
        messages.append({"role": "system", "content": t(
            "chat_prompts.rag_peers", passages="\n\n".join(remote))})
    # Web permis (puce « Web ») mais la question ne semblait pas le demander : pas de
    # recherche (rule_not_searched)
    rule = ("chat_prompts.rule_web" if web_ok else "chat_prompts.rule_web_nothing" if web_search
            else "chat_prompts.rule_not_searched" if settings.get("web_search_enabled")
            else "chat_prompts.rule_local")
    messages.append({"role": "system", "content": t(rule)})
    messages.append({"role": "user", "content": question})
    return messages, sources


def generate(model, messages, think=True, stop=None):
    """Couples (genre, texte) depuis le modèle local, d'une machine appairée, ou cloud."""
    provider = cloud.provider_of(model)
    if provider:
        yield from cloud.chat_stream(provider, messages, stop=stop)
    elif remote_models.parse(model):
        yield from remote_models.chat_stream(model, messages, think, stop)
    elif cluster.parse(model):
        yield from cluster.chat_stream(model, messages, think, stop)
    else:
        yield from ollama.chat_stream(model, messages, think=None if think else False,
                                      stop=stop)
