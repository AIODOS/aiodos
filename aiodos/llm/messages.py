"""Ce que llama-server attend d'une conversation, à la place d'Ollama : les messages mis en
forme pour les modèles les plus stricts, et les réglages d'échantillonnage d'Ollama
traduits en champs de llama-server. Commun au moteur intégré (llm/integrated.py) et aux
gros modèles répartis (cluster/group.py)."""
import base64

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
        images = list(message.get("images") or [])          # base64, à la façon d'Ollama
        if role == "system":
            (pending if rest else head).append(content)
            continue
        if pending and role == "user":
            content = PARAGRAPH.join(pending + [content])
            pending = []
        if rest and rest[-1]["role"] == role:
            rest[-1]["content"] += PARAGRAPH + content
            rest[-1]["images"] = rest[-1].get("images", []) + images
        else:
            rest.append({"role": role, "content": content, "images": images})
    if pending:
        if rest and rest[-1]["role"] == "user":
            rest[-1]["content"] += PARAGRAPH + PARAGRAPH.join(pending)
        else:
            head.extend(pending)
    return ([{"role": "system", "content": PARAGRAPH.join(head)}] if head else []) + [
        _with_images(m) for m in rest]


def _with_images(message) -> dict:
    """Un message et ses images, en parties « text » et « image_url » comme llama-server les
    attend (un modèle qui a son projecteur les voit)."""
    images = message.pop("images", None)
    if not images:
        return message
    return {"role": message["role"], "content": [{"type": "text", "text": message["content"]}] + [
        {"type": "image_url", "image_url": {"url": f"data:{_mime(b64)};base64,{b64}"}}
        for b64 in images]}


def _mime(b64: str) -> str:
    """Le type d'une image d'après ses premiers octets (base64)."""
    head = base64.b64decode(str(b64)[:24] + "=" * (-len(str(b64)[:24]) % 4), validate=False)
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"
