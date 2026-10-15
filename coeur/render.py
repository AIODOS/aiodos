"""Rendu Markdown → HTML sûr pour l'interface.

Le HTML brut écrit par un modèle est échappé (html=False) et markdown-it
refuse les liens javascript:/data:. Les blocs de code sont colorés par
Pygments côté serveur : aucune bibliothèque JS à embarquer.
"""
import html

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


def _link_open(self, tokens, idx, options, env):
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


_md = (MarkdownIt("commonmark", {"html": False, "breaks": True})
       .enable("table").enable("strikethrough"))
_md.add_render_rule("fence", _fence)
_md.add_render_rule("code_block", _indented_code)
_md.add_render_rule("link_open", _link_open)


def to_html(text: str) -> str:
    return _md.render(text or "")


def pygments_css() -> str:
    try:
        formatter = HtmlFormatter(style="github-dark")
    except ClassNotFound:
        formatter = HtmlFormatter(style="monokai")
    return formatter.get_style_defs(".hl")
