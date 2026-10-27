"""La charte et les conditions d'utilisation (aiodos/legal) : lues dans l'appli, acceptées à
la première ouverture quand l'installeur ne l'a pas fait (appli Android, copie lancée depuis
les sources), et de nouveau à chaque nouvelle version des textes."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aiodos import i18n, legal, render

router = APIRouter(prefix="/api")


class AcceptIn(BaseModel):
    version: int


@router.get("/legal")
def read_legal():
    """Les deux textes dans la langue de l'interface, rendus en HTML, et où en est l'accord."""
    lang = i18n.current()
    docs = []
    for doc in legal.DOCS:
        title, body = legal.split(doc, lang)
        docs.append({"id": doc, "title": title, "html": render.to_html(legal.unwrap(body))})
    return {"version": legal.VERSION, "accepted": legal.accepted(), "docs": docs}


@router.post("/legal/accept")
def accept_legal(body: AcceptIn):
    try:
        legal.accept(body.version)
    except ValueError:
        # Les textes ont changé pendant qu'on les lisait (une mise à jour) : à relire
        raise HTTPException(409, i18n.t("legal.outdated"))
    return {"accepted": legal.VERSION}
