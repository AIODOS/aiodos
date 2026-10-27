"""Les textes de l'interface, dans sa langue (aiodos/i18n, ui/static/js/i18n.js)."""
from fastapi import APIRouter

from aiodos import i18n, settings
from aiodos.i18n import language

router = APIRouter(prefix="/api")


@router.get("/i18n")
def texts(prefer: str = ""):
    """La langue de l'interface : celle des Réglages ; en automatique, celle de la machine
    (Windows, Android…), sinon la première que le navigateur préfère parmi le français et
    l'anglais (prefer : navigator.languages, séparées par des virgules), sinon l'anglais. Et
    tous ses textes ; « system » : la langue de la machine, que « Automatique » donnerait."""
    system = language.system()
    lang = (language.chosen() or system or language.first_supported(prefer.split(",")[:20])
            or i18n.FALLBACK)
    return {"lang": lang, "setting": settings.get("language"), "system": system,
            "languages": i18n.LANGUAGES, "texts": i18n.texts(lang)}
