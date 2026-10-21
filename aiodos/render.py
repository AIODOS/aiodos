"""Rendu Markdown → HTML sûr pour l'interface.

Le HTML brut écrit par un modèle est échappé (html=False). Ce qu'il écrit vient parfois d'un
ami (modèle prêté) ou d'une page, d'un document piégés (injection d'invite), donc :
  - un lien ne reste cliquable que vers le web ou un courriel (http, https, mailto) ;
    search-ms:, ms-word:, intent:… lanceraient une autre application sans confirmation,
    et une adresse relative viserait le nœud lui-même : leur texte reste, sans lien ;
  - un courriel ne garde que ses destinataires, son sujet et son texte (attach=… ferait
    joindre un fichier de la machine par certaines messageries) ;
  - une image n'est jamais chargée d'office : elle devient un lien à cliquer. Chargée,
    elle ferait sortir sans aucun clic ce que l'adresse contient (extraits, conversation).
    Seule une image écrite dans la réponse même (data:image/png;base64,…) s'affiche :
    elle ne va rien chercher ailleurs.
Le tout en une passe : une réponse se rend de nouveau à chaque morceau reçu, sur la boucle
qui sert aussi les autres machines (api/routes/chat.py), même longue de milliers de liens.
Les blocs de code sont colorés par Pygments côté serveur : aucune bibliothèque JS à embarquer.
"""
import html
import re

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

_FORMATTER = HtmlFormatter(nowrap=True)


def _code_block(code: str, lang: str) -> str:
    lexer = None
    if lang:
        try:
            lexer = get_lexer_by_name(lang)
        except ClassNotFound:
            lexer = None
    body = highlight(code, lexer, _FORMATTER) if lexer else html.escape(code)
    label = html.escape(lang or "texte")
    return ('<div class="code-block"><div class="code-head">'
            f'<span>{label}</span>'
            '<button class="code-copy" type="button">Copier</button></div>'
            f'<pre class="hl"><code>{body}</code></pre></div>\n')


def _fence(self, tokens, idx, options, env):
    info = (tokens[idx].info or "").strip().split()
    return _code_block(tokens[idx].content, info[0] if info else "")


def _indented_code(self, tokens, idx, options, env):
    return _code_block(tokens[idx].content, "")


OUTSIDE = ("http://", "https://", "mailto:")      # ce qu'un clic peut ouvrir hors d'AIODOS
MAIL_FIELDS = {"to", "cc", "subject", "body"}    # ce qu'un courriel garde (plain_mail)
# Une image écrite dans la réponse même : rien à aller chercher, rien ne sort
INLINE_IMAGE = re.compile(r"data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+", re.I)


def opens_outside(address: str) -> bool:
    """Lien qu'on peut ouvrir hors d'AIODOS : une page web ou un courriel, rien d'autre."""
    return (address or "").strip().lower().startswith(OUTSIDE)


def plain_mail(address: str) -> str:
    """Un lien mailto: réduit à ses destinataires, son sujet et son texte. Certaines
    messageries ont joint d'office le fichier nommé par attach=… ou attachment=… : un lien
    piégé ferait partir un fichier de la machine avec le courriel. Un nom de champ encodé
    (%61ttach) n'est pas reconnu, donc écarté. Le fragment (#…) tombe aussi : rien de ce
    qui suit l'adresse n'échappe au tri."""
    address = (address or "").partition("#")[0]
    head, ask, query = address.partition("?")
    if not ask:
        return address
    kept = [field for field in query.split("&")
            if field.partition("=")[0].strip().lower() in MAIL_FIELDS]
    return head + ("?" + "&".join(kept) if kept else "")


def _pair_links(state):
    """Règle du cœur, après « inline » : en une passe par bloc, chaque ouverture de lien
    connaît sa fermeture et chaque image sait si un lien l'entoure. Le chercher au rendu de
    chaque image (relire tout ce qui précède) rendait une réponse de 10 000 images en
    plusieurs secondes."""
    for block in state.tokens:
        opened = []
        for token in block.children or ():
            if token.type == "link_open":
                opened.append(token)
            elif token.type == "link_close" and opened:
                opened.pop().meta["closing"] = token
            elif token.type == "image":
                token.meta["in_link"] = bool(opened)


def _link_open(self, tokens, idx, options, env):
    token = tokens[idx]
    href = token.attrGet("href") or ""
    if opens_outside(href):
        if href.strip().lower().startswith("mailto:"):
            token.attrSet("href", plain_mail(href))
        token.attrSet("target", "_blank")
        token.attrSet("rel", "noopener noreferrer")
        return self.renderToken(tokens, idx, options, env)
    # Ni le web ni un courriel : le texte reste, l'adresse se lit au survol, rien ne s'ouvre
    token.tag = "span"
    token.attrs = {"class": "link-off", "title": f"Lien désactivé : {href}"}
    end = token.meta.get("closing")
    if end is not None:
        end.tag = "span"
    return self.renderToken(tokens, idx, options, env)


def _image(self, tokens, idx, options, env):
    """Une image devient son texte (« [image : …] »), cliquable vers son adresse web ; une
    image écrite dans la réponse même (data:image/…) s'affiche."""
    token = tokens[idx]
    src = (token.attrGet("src") or "").strip()
    alt = self.renderInlineAsText(token.children or [], options, env).strip()
    if INLINE_IMAGE.fullmatch(src):
        return f'<img class="inline-image" src="{html.escape(src)}" alt="{html.escape(alt)}">'
    label = html.escape(f"[image : {alt}]" if alt else "[image]")
    if token.meta.get("in_link"):            # le lien autour l'ouvre déjà
        return label
    if not src.lower().startswith(("http://", "https://")):
        title = html.escape("Image non affichée : " + src)
        return f'<span class="link-off" title="{title}">{label}</span>'
    return (f'<a class="image-link" href="{html.escape(src)}" target="_blank" '
            f'rel="noopener noreferrer" title="{html.escape("Image : " + src)}">{label}</a>')


_md = (MarkdownIt("commonmark", {"html": False, "breaks": True})
       .enable("table").enable("strikethrough"))
_md.add_render_rule("fence", _fence)
_md.add_render_rule("code_block", _indented_code)
_md.add_render_rule("link_open", _link_open)
_md.add_render_rule("image", _image)
_md.core.ruler.push("aiodos_links", _pair_links)


def to_html(text: str) -> str:
    return _md.render(text or "")


def pygments_css() -> str:
    try:
        formatter = HtmlFormatter(style="github-dark")
    except ClassNotFound:
        formatter = HtmlFormatter(style="monokai")
    return formatter.get_style_defs(".hl")
