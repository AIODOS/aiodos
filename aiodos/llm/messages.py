"""Ce que llama-server attend d'une conversation, à la place d'Ollama : les messages mis en
forme pour les modèles les plus stricts, et les réglages d'échantillonnage d'Ollama
traduits en champs de llama-server. Commun au moteur intégré (llm/integrated.py) et aux
gros modèles répartis (cluster/group.py)."""

PARAGRAPH = "\n\n"            # entre deux consignes réunies
# Réglages d'échantillonnage d'Ollama (lending.SAMPLING) → champs de llama-server
SAMPLING = {"num_predict": "max_tokens", "temperature": "temperature", "top_p": "top_p",
            "top_k": "top_k", "min_p": "min_p", "seed": "seed", "stop": "stop",
            "repeat_penalty": "repeat_penalty", "presence_penalty": "presence_penalty",
            "frequency_penalty": "frequency_penalty"}


def for_template(messages) -> list:
    """Messages acceptés par les modèles de conversation les plus stricts (Mistral exige
    l'alternance utilisateur / assistant, Ollama le cachait) : une seule consigne
    « system », en tête ; les consignes données plus loin (résultats web, rappel juste
    avant la question) rejoignent le message de l'utilisateur qui les suit ; deux messages
    de suite du même rôle sont réunis."""
    head, rest, pending = [], [], []
    for message in messages:
        role, content = message["role"], message.get("content") or ""
        if role == "system":
            (pending if rest else head).append(content)
            continue
        if pending and role == "user":
            content = PARAGRAPH.join(pending + [content])
            pending = []
        if rest and rest[-1]["role"] == role:
            rest[-1]["content"] += PARAGRAPH + content
        else:
            rest.append({"role": role, "content": content})
    if pending:
        if rest and rest[-1]["role"] == "user":
            rest[-1]["content"] += PARAGRAPH + PARAGRAPH.join(pending)
        else:
            head.extend(pending)
    return ([{"role": "system", "content": PARAGRAPH.join(head)}] if head else []) + rest
