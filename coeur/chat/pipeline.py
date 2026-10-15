"""Préparation et génération d'une réponse.

Ordre des messages envoyés au modèle :
  prompt système → sommaire des connaissances → historique →
  [résultats web] → [extraits pertinents] → règle anti-invention → question.
La règle anti-invention est placée juste avant la question : c'est la position
la plus forte, celle qui l'emporte sur d'anciens échanges mémorisés (leçon v1).
"""
import logging
import re

from coeur import settings
from coeur.knowledge import search
from coeur.services import cloud, ollama, web

log = logging.getLogger("coeur.chat")

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
        "web_intent": bool(WEB_INTENT.search(text or "")),
        "cloud": provider,
        "cloud_host": cloud.PROVIDERS[provider]["host"] if provider else None,
        "cloud_has_key": cloud.has_key(provider) if provider else None,
        "cloud_confirm": bool(settings.get("cloud_confirm")),
    }


def available_models() -> list:
    """Modèles de conversation : locaux (Ollama) puis cloud."""
    models = []
    try:
        for m in ollama.list_models():
            caps = ollama.capabilities(m["name"])
            if caps and "completion" not in caps:      # modèle d'embedding seul
                continue
            models.append({"id": m["name"], "label": m["name"], "group": "local",
                           "size": m["size"], "caps": caps})
    except Exception as exc:
        log.warning("Ollama injoignable : %s", exc)
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


def _knowledge_context(question, libraries, conversations, sources):
    try:
        vector = ollama.embed([question])[0]
        hits = search.search(vector, k=4, libraries=libraries, conversations=conversations)
    except Exception as exc:
        log.warning("Recherche dans les connaissances impossible : %s", exc)
        return []
    seen = set()
    for h in hits:
        if (h["collection"], h["source"]) in seen:
            continue
        seen.add((h["collection"], h["source"]))
        sources.append({"type": "conversation" if h["kind"] == "conversation" else "knowledge",
                        "label": h["source"], "collection": h["collection"],
                        "score": round(h["score"], 3)})
    return [f"[Source : {h['source']}]\n{h['text']}" for h in hits]


def build_messages(history, question, *, libraries, conversations, web_search):
    """Construit les messages pour le modèle. → (messages, sources citées)"""
    messages = [{"role": "system", "content": settings.get("system_prompt")}]
    sources = []
    if libraries or conversations:
        summary = search.manifest(libraries, conversations)
        if summary:
            messages.append({"role": "system", "content": _MANIFEST.format(summary)})
    messages.extend(history)

    web_ok = False
    if web_search:
        blocks = _web_context(question, settings.get("web_results") or 3, sources)
        web_ok = bool(blocks)
        messages.append({"role": "system", "content": _WEB_RESULTS.format(
            "\n\n".join(blocks)) if web_ok else _WEB_EMPTY})
    if libraries or conversations:
        extracts = _knowledge_context(question, libraries, conversations, sources)
        if extracts:
            messages.append({"role": "system", "content": _RAG.format("\n\n".join(extracts))})
    messages.append({"role": "system", "content": _RULE_WEB if web_ok else _RULE_LOCAL})
    messages.append({"role": "user", "content": question})
    return messages, sources


def generate(model, messages, think=True, stop=None):
    """Couples (genre, texte) depuis le modèle local ou cloud."""
    provider = cloud.provider_of(model)
    if provider:
        yield from cloud.chat_stream(provider, messages, stop=stop)
    else:
        yield from ollama.chat_stream(model, messages, think=None if think else False,
                                      stop=stop)
