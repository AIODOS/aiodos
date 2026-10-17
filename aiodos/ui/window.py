"""Fenêtre native (WebView2 / Edge via pywebview) qui affiche l'interface du nœud.

Sous Windows, une icône dans la zone de notification (ui/tray.py) accompagne la fenêtre :
la fermer la cache seulement (réglage close_to_tray), le nœud continue de tourner ;
« Quitter AIODOS », dans le menu de l'icône, arrête tout. L'arrêt de Windows, ou celui
demandé par le gestionnaire des tâches, ferme vraiment la fenêtre.
"""
import logging

import webview

from aiodos import instance, paths, settings
from aiodos.ui import tray

log = logging.getLogger("aiodos.window")

HIDDEN_NOTICE = ("AIODOS continue en arrière-plan. Clic sur l'icône pour rouvrir la "
                 "fenêtre, clic droit pour quitter.")
_open = []                         # fenêtres ouvertes (cf. close)
_state = {"quitting": False, "noticed": False, "tray": None}


class Bridge:
    """Fonctions Python appelables depuis l'interface : window.pywebview.api.*

    Les boîtes de dialogue de fichiers passent par ici : un navigateur ne
    donne jamais le chemin complet d'un fichier choisi, la fenêtre native si.
    """

    def __init__(self):
        self._window = None        # préfixe _ : non exposé au JavaScript

    def pick_files(self):
        chosen = self._window.create_file_dialog(webview.FileDialog.OPEN,
                                                 allow_multiple=True)
        return list(chosen or [])

    def pick_folder(self):
        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return list(chosen or [])


def close():
    """Ferme la fenêtre et arrête AIODOS, depuis n'importe quel thread (redémarrage pour
    mise à jour, « Quitter AIODOS »)."""
    _state["quitting"] = True
    for window in list(_open):
        window.destroy()


def show() -> bool:
    """Ramène la fenêtre (cachée ou réduite) au premier plan, depuis n'importe quel thread."""
    shown = False
    for window in list(_open):
        form = window.native
        if form is None:
            continue
        from System import Action
        form.Invoke(Action(lambda form=form: _bring_back(form)))
        shown = True
    return shown


def _bring_back(form):
    from System.Windows.Forms import FormWindowState
    form.Show()
    if form.WindowState == FormWindowState.Minimized:
        form.WindowState = FormWindowState.Normal
    form.Activate()


def _keep_running(form):
    """Fil graphique : l'icône, puis la fermeture qui cache la fenêtre."""
    from System.Windows.Forms import CloseReason
    icon = tray.Tray(paths.LOGO_ICO, on_open=show, on_quit=close)
    try:
        icon.create()
    except Exception:              # pas d'icône : la fenêtre se ferme comme avant
        log.exception("Icône de la zone de notification impossible")
        return
    _state["tray"] = icon
    log.info("Icône dans la zone de notification : fermer la fenêtre la cache "
             "(réglage close_to_tray)")

    def closing(sender, args):
        if (_state["quitting"] or args.CloseReason != CloseReason.UserClosing
                or not settings.get("close_to_tray")):
            if not args.Cancel:
                icon.remove()
            return
        args.Cancel = True
        sender.Hide()
        if not _state["noticed"]:
            _state["noticed"] = True
            icon.notify(HIDDEN_NOTICE)
    form.FormClosing += closing


def _on_shown(window):
    if not tray.available() or window.native is None or _state["tray"] is not None:
        return
    from System import Action
    try:
        window.native.Invoke(Action(lambda: _keep_running(window.native)))
    except Exception:
        log.exception("Fenêtre : fermeture vers la zone de notification impossible")


def run(url: str, on_closed):
    """Ouvre la fenêtre et bloque jusqu'à sa fermeture (boucle graphique)."""
    bridge = Bridge()
    window = webview.create_window(
        "AIODOS", url, js_api=bridge, width=1320, height=860, min_size=(980, 640),
        background_color="#0a0f1f", text_select=True)
    bridge._window = window
    _open.append(window)
    window.events.closed += on_closed
    window.events.shown += lambda: _on_shown(window)
    instance.on_show(show)               # AIODOS relancé : on montre celle-ci
    storage = paths.data_dir() / "webview"
    storage.mkdir(exist_ok=True)
    webview.start(icon=str(paths.LOGO_ICO), private_mode=False, storage_path=str(storage))
