"""Ce que cette machine calcule pour d'autres (network/lent_history.py), pour la vue Tâches,
« Pour d'autres » : l'historique par pages, les plus récents d'abord ; avec la première page,
les totaux (aujourd'hui, les 7 derniers jours : nombre et temps de calcul, par cercle) et ce
qui se calcule ici pour d'autres en ce moment. Jamais de contenu, ni rien qui identifie un
demandeur du réseau public : la base n'en a pas."""
from fastapi import APIRouter, Query

from aiodos.network import lent_history

router = APIRouter(prefix="/api")


@router.get("/lent")
def lent(before: int | None = Query(None, ge=1),
         limit: int = Query(lent_history.PAGE_SIZE, ge=1, le=lent_history.MAX_PAGE)):
    """before : l'identifiant de la dernière ligne de la page précédente (« Voir plus »)."""
    found = lent_history.page(before, limit)
    if before is None:
        found["totals"] = lent_history.totals()
        found["live"] = lent_history.live()
    return found
