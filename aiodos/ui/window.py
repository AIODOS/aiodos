"""Fenêtre native (WebView2 / Edge via pywebview) qui affiche l'interface du nœud.

Elle s'ouvre aussitôt, sur un écran d'attente, pendant que le nœud démarre : sur un PC
modeste, au premier lancement surtout, ce démarrage prend de longues secondes, et rien ne
montrait qu'AIODOS se lançait.

Sous Windows, une icône dans la zone de notification (ui/tray.py) accompagne la fenêtre :
la fermer la cache seulement (réglage close_to_tray), le nœud continue de tourner ;
« Quitter AIODOS », dans le menu de l'icône, arrête tout. L'arrêt de Windows, ou celui
demandé par le gestionnaire des tâches, ferme vraiment la fenêtre.
"""
import base64
import html
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


def waiting_page(problem: str | None = None) -> str:
    """L'écran d'attente : le logo, « AIODOS démarre… », ou ce qui l'a empêché."""
    try:
        logo = base64.b64encode((paths.ASSETS_DIR / "logo_128.png").read_bytes()).decode()
        image = f'<img src="data:image/png;base64,{logo}" alt="">'
    except OSError:
        image = ""
    text = (f'<p class="err">{html.escape(problem)}</p><p>Détails : {html.escape(str(paths.log_path()))}</p>'
            if problem else '<p>AIODOS démarre<span class="dots"></span></p>')
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><style>
html, body {{ margin: 0; height: 100%; background: #080c1a; color: #e8ecfb;
  font: 15px "Segoe UI", system-ui, sans-serif; display: grid; place-items: center; }}
main {{ text-align: center; max-width: 520px; padding: 24px; }}
img {{ width: 96px; height: 96px; border-radius: 22px; }}
p {{ color: #8e99c0; margin: 16px 0 0; }}
.err {{ color: #ff9aa8; }}
.dots::after {{ content: ""; animation: dots 1.4s steps(4) infinite; }}
@keyframes dots {{ 0% {{ content: ""; }} 25% {{ content: "."; }} 50% {{ content: ".."; }} 75% {{ content: "..."; }} }}
</style></head><body><main>{image}{text}</main></body></html>"""


def run(start, on_closed, quit_on_error: bool = False):
    """Ouvre la fenêtre aussitôt, sur l'écran d'attente, puis l'interface : start(), appelé
    hors du fil graphique, démarre le nœud et rend l'adresse de son interface. S'il échoue,
    l'écran le dit ; ou, quit_on_error, la fenêtre se ferme (premier démarrage d'une nouvelle
    version : le lanceur rend alors la main à la précédente). Bloque jusqu'à la fermeture."""
    bridge = Bridge()
    window = webview.create_window(
        "AIODOS", html=waiting_page(), js_api=bridge, width=1320, height=860,
        min_size=(980, 640), background_color="#080c1a", text_select=True)
    bridge._window = window
    _open.append(window)
    window.events.closed += on_closed
    window.events.shown += lambda: _on_shown(window)
    instance.on_show(show)               # AIODOS relancé : on montre celle-ci

    def load():
        try:
            url = start()
        except Exception as exc:
            log.exception("Démarrage du nœud impossible")
            if quit_on_error:
                close()
            else:
                window.load_html(waiting_page(f"AIODOS n'a pas pu démarrer : {exc}"))
            return
        window.load_url(url)

    storage = paths.data_dir() / "webview"
    storage.mkdir(exist_ok=True)
    webview.start(load, icon=str(paths.LOGO_ICO), private_mode=False, storage_path=str(storage))
