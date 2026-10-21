"""Les textes d'AIODOS, en français et en anglais.

Chaque langue a son dossier (aiodos/i18n/fr, aiodos/i18n/en) et chaque fonctionnalité son
fichier de textes (chat.json, network.json…), lus par le nœud comme par l'interface
(GET /api/i18n, ui/static/js/i18n.js). Le français est la langue de départ.

    from aiodos.i18n import t
    raise HTTPException(404, t("chat.not_found"))
    t("images.ready", n=3)          # {"one": "{n} image prête", "other": "{n} images prêtes"}

t() ne s'appelle jamais au chargement d'un module : la langue n'est connue qu'au moment
d'afficher (celle de la requête en cours, sinon celle du nœud, cf. language.py).

Un texte gardé pour plus tard (titre d'une tâche en base) ou envoyé à une autre machine
part sous la forme lazy(clé, paramètres) : [clé, {paramètres}], que render() ou
l'interface (tt) traduisent dans la langue de celui qui le lit.
"""
from aiodos.i18n import catalog, language
from aiodos.i18n.catalog import FALLBACK, LANGUAGES, SOURCE
from aiodos.i18n.language import current, using

__all__ = ["FALLBACK", "LANGUAGES", "SOURCE", "current", "exists", "lazy", "number",
           "render", "t", "texts", "using"]


def t(key: str, /, **params) -> str:
    """Le texte de cette clé dans la langue en cours, paramètres posés."""
    lang = current()
    return catalog.fill(catalog.lookup(lang, key), params, lang)


def exists(key) -> bool:
    return isinstance(key, str) and catalog.exists(key)


def texts(lang: str) -> dict:
    """Tous les textes d'une langue (pour l'interface)."""
    return catalog.texts(lang if lang in LANGUAGES else FALLBACK)


def lazy(key: str, /, **params) -> list:
    """Un texte à traduire plus tard, par qui le lira : [clé, {paramètres}] (JSON)."""
    return [key, params]


def render(value) -> str:
    """Un texte reçu ou gardé : [clé, {paramètres}] (lazy) traduit, une chaîne telle quelle.
    Une clé inconnue (venue d'une autre version) ne s'affiche pas : chaîne vide."""
    if isinstance(value, (list, tuple)) and len(value) == 2 and exists(value[0]):
        params = value[1] if isinstance(value[1], dict) else {}
        safe = {str(k): v for k, v in params.items()
                if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
        return t(value[0], **safe)
    return value if isinstance(value, str) else ""


def number(value, digits: int = 0) -> str:
    """Un nombre à la façon de la langue en cours : 0,45 en français, 0.45 en anglais."""
    text = f"{value:.{digits}f}"
    return text.replace(".", ",") if current() == "fr" else text
