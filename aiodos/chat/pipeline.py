"""Préparation et génération d'une réponse.

Ordre des messages envoyés au modèle :
  prompt système → sommaire des connaissances → historique →
  [résultats web] → [extraits pertinents] → règle anti-invention → question.
La règle anti-invention est placée juste avant la question : c'est la position
la plus forte, celle qui l'emporte sur d'anciens échanges mémorisés (leçon v1).

Quand la réponse est calculée par une autre machine (modèle prêté, choisi à la main ou
par le mode automatique, cf. network/placement.py), le sommaire et les extraits ne
viennent que des bibliothèques qui peuvent aller chez elle (visible_libraries) : rien
d'une bibliothèque gardée sur ce PC, ni de la mémoire des conversations.
"""
import logging
import re

from aiodos import settings
from aiodos.knowledge import search, store
from aiodos.network import placement, remote_models
from aiodos.services import cloud, ollama, web

log = logging.getLogger("aiodos.chat")

# Tournures qui suggèrent une recherche web. On matche des radicaux :
# « cherch\w* » couvre cherche, chercher, recherche, rechercher… (bug v1).
WEB_INTENT = re.compile(
    r"(cherch\w*|recherch\w*|trouv\w*|google|navigu\w*|sur le web|sur internet|"
    r"en ligne|actualit[ée]s?\w*|site officiel|documentation (en ligne|web|à jour)|"
    r"derni[èe]re?s? (version|nouvelle|info))", re.IGNORECASE)

_MANIFEST = ("Tu disposes d'une base de connaissance locale. Contenu disponible :\n\n{}\n\n"
             "Si l'utilisateur demande ce que tu « vois » ou ce que tu as en mémoire, "
             "réfère-toi à cette liste. Des extraits pertinents te sont fournis selon "
             "la question.")
_WEB_RESULTS = ("Résultats de recherche web réels, récupérés à l'instant. Réponds en "
                "t'appuyant dessus et cite les URL utilisées :\n\n{}")
_WEB_EMPTY = ("La recherche web n'a renvoyé aucun résultat exploitable. Dis-le "
              "honnêtement et n'invente aucune source.")
_RAG = ("Extraits des connaissances de l'utilisateur. Sers-t'en s'ils sont pertinents, "
        "ignore-les sinon :\n\n{}")
_RULE_WEB = ("RAPPEL : les résultats web ci-dessus sont réels. Appuie-toi dessus et cite "
             "uniquement leurs URL ; n'invente aucune autre adresse.")
_RULE_LOCAL = ("RAPPEL IMPORTANT : ici tu fonctionnes en local, SANS accès à Internet. "
               "N'invente JAMAIS d'URL ni de résultat de recherche, et ne prétends pas "
               "avoir cherché sur le web. Si on te demande des informations en ligne, "
               "réponds : « Active la recherche web puis repose ta question. »")


def preflight(text: str, model: str) -> dict:
    """Ce que l'interface doit faire valider avant l'envoi (web, cloud)."""
    provider = cloud.provider_of(model)
    return {
        "remote": remote_models.peer_name(model),     # machine appairée qui répondra
        "web_intent": bool(WEB_INTENT.search(text or "")),
        "cloud": provider,
        "cloud_host": cloud.PROVIDERS[provider]["host"] if provider else None,
        "cloud_has_key": cloud.has_key(provider) if provider else None,
        "cloud_confirm": bool(settings.get("cloud_confirm")),
    }


def runs_here(model) -> bool:
    """Le modèle tourne-t-il sur ce PC (et occupe-t-il son GPU) ? Pour le mode
    automatique, on ne le sait qu'une fois la machine choisie (placement.run)."""
    return not (cloud.provider_of(model) or remote_models.parse(model)
                or placement.parse(model))


def available_models() -> list:
    """Modèles de conversation : en mode automatique (une fois chacun), locaux (Ollama),
    ceux de mes machines, puis cloud."""
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
    models[:0] = placement.auto_entries(models)
    for name, spec in cloud.PROVIDERS.items():
        models.append({"id": cloud.MODEL_PREFIX + name, "label": spec["label"],
                       "group": "cloud", "caps": [], "has_key": cloud.has_key(name)})
    return models


def _web_context(question, max_results, sources):
    blocks = []
    try:
        for res in web.web_search(question, max_results):
            try:
                title, text = web.fetch_url_text(res["url"])
            except Exception:
                continue
            blocks.append(f"[Web : {title} — {res['url']}]\n{text[:3000]}")
            sources.append({"type": "web", "label": web.host_of(res["url"]),
                            "url": res["url"], "title": title})
    except Exception as exc:
        log.warning("Recherche web impossible : %s", exc)
    return blocks


def web_context(question) -> tuple:
    """Recherche web pour la question : (blocs pour le modèle, sources citées). Faite une
    fois par message, même si la réponse change de machine en route (placement.run)."""
    sources = []
    return _web_context(question, settings.get("web_results") or 3, sources), sources


def visible_libraries(destination) -> set | None:
    """Bibliothèques dont le sommaire et les extraits peuvent accompagner une conversation,
    selon la machine qui répond (placement.destination) : None, cette machine (ou le
    cloud, après validation) : toutes ; « mine », une de mes machines : toutes sauf celles
    gardées sur ce PC ; « friend », la machine d'un ami : celles partagées avec mes amis."""
    if destination is None:
        return None
    if destination == "mine":
        return {c["id"] for c in store.list_collections() if c["scope"] != "local"}
    return store.shareable_ids()


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
                        "score": round(h["score"], 3)})
    return [f"[Source : {h['source']}]\n{h['text']}" for h in hits]


def build_messages(history, question, *, libraries, conversations, web_search,
                   conversation_id=None, destination=None, web=None):
    """Construit les messages pour le modèle. → (messages, sources citées)

    destination : machine qui répondra (cf. visible_libraries) ; web : résultats déjà
    obtenus par web_context, sinon la recherche est faite ici si web_search."""
    only = visible_libraries(destination)
    conversations = conversations and destination is None    # la mémoire reste ici
    messages = [{"role": "system", "content": settings.get("system_prompt")}]
    sources = []
    if libraries or conversations:
        summary = search.manifest(libraries, conversations, only=only)
        if summary:
            messages.append({"role": "system", "content": _MANIFEST.format(summary)})
    messages.extend(history)

    web_ok = False
    if web_search:
        blocks, found = web if web is not None else web_context(question)
        sources.extend(found)
        web_ok = bool(blocks)
        messages.append({"role": "system", "content": _WEB_RESULTS.format(
            "\n\n".join(blocks)) if web_ok else _WEB_EMPTY})
    if libraries or conversations:
        extracts = _knowledge_context(question, libraries, conversations, sources,
                                      conversation_id, only)
        if extracts:
            messages.append({"role": "system", "content": _RAG.format("\n\n".join(extracts))})
    messages.append({"role": "system", "content": _RULE_WEB if web_ok else _RULE_LOCAL})
    messages.append({"role": "user", "content": question})
    return messages, sources


def generate(model, messages, think=True, stop=None):
    """Couples (genre, texte) depuis le modèle local, d'une machine appairée, ou cloud."""
    provider = cloud.provider_of(model)
    if provider:
        yield from cloud.chat_stream(provider, messages, stop=stop)
    elif remote_models.parse(model):
        yield from remote_models.chat_stream(model, messages, think, stop)
    else:
        yield from ollama.chat_stream(model, messages, think=None if think else False,
                                      stop=stop)
