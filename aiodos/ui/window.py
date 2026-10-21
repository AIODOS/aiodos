"""Fenêtre native (WebView2 / Edge via pywebview) qui affiche l'interface du nœud.

Elle s'ouvre aussitôt, sur un écran d'attente, pendant que le nœud démarre : sur un PC
modeste, au premier lancement surtout, ce démarrage prend de longues secondes, et rien ne
montrait qu'AIODOS se lançait.

Sous Windows, une icône dans la zone de notification (ui/tray.py) accompagne la fenêtre :
la fermer la cache seulement (réglage close_to_tray), le nœud continue de tourner ;
« Quitter AIODOS », dans le menu de l'icône, arrête tout. L'arrêt de Windows, ou celui
demandé par le gestionnaire des tâches, ferme vraiment la fenêtre.

La fenêtre ne montre que l'interface du nœud : elle refuse d'aller ailleurs (un fichier ou
un lien lâché dessus) et rien de ce qu'elle demande ne part vers un autre hôte, son pont vers
Python ne répond qu'à cette interface, et ses liens ne s'ouvrent dehors que vers le web ou un
courriel. Ces deux gardes natives (navigations, requêtes) sont celles de WebView2, donc de
Windows ; sous Linux et macOS, cf. _on_shown.
"""
import base64
import html
import logging
import shutil
import subprocess
import sys
import webbrowser
from urllib.parse import urlsplit

import webview

from aiodos import i18n, instance, paths, settings
from aiodos.i18n import t
from aiodos.render import plain_mail
from aiodos.ui import tray

log = logging.getLogger("aiodos.window")

_open = []                         # fenêtres ouvertes (cf. close)
# window : la fenêtre principale ; origin : (schéma, hôte:port) de l'interface du nœud,
# connue une fois le nœud démarré ; guarded : ses navigations passent par la garde
_state = {"quitting": False, "noticed": False, "tray": None, "window": None, "origin": None,
          "guarded": False}
OUTSIDE = ("http", "https", "mailto")         # ce que la fenêtre laisse ouvrir dehors
# Les requêtes qui quittent la page, et l'origine à laquelle chacune se compare
NETWORK = {"http": "http", "https": "https", "ws": "http", "wss": "https", "ftp": "ftp",
           "file": "file"}


# --------------------------------------------------------------- pont vers l'interface
# Fonctions Python appelables depuis l'interface : window.pywebview.api.* Les boîtes de
# dialogue de fichiers passent par ici : un navigateur ne donne jamais le chemin complet
# d'un fichier choisi, la fenêtre native si.
#
# Des fonctions du module, données une à une à pywebview (window.expose), et non un objet
# (js_api) : pywebview suit un nom pointé reçu de la page (« _window.gui.os.system »)
# d'attribut en attribut, sans écarter ceux qui commencent par « _ ». Depuis un objet qui
# tenait la fenêtre, une page aurait atteint os.system. Chaque fonction vérifie en plus que
# l'appel vient de l'interface du nœud, pas d'une page étrangère.


class Sealed:
    """Le js_api donné à pywebview : un nom qu'il ne trouve pas parmi les fonctions exposées,
    il le cherche ici, attribut après attribut. Rien n'y mène : même « __class__ » rend None
    (depuis le None par défaut, « __class__.__base__… » menait encore aux types de Python)."""
    __slots__ = ()

    def __getattribute__(self, name):
        return None


SEALED = Sealed()

def _origin(url) -> tuple:
    parts = urlsplit(str(url or ""))
    return parts.scheme.lower(), parts.netloc.lower()


def _from_interface() -> bool:
    """La page affichée est-elle l'interface du nœud ? Sinon, le pont ne fait rien."""
    window, origin = _state["window"], _state["origin"]
    if window is None or origin is None:
        return False
    try:
        current = window.get_current_url()
    except Exception:
        log.exception("Pont de la fenêtre : adresse de la page illisible")
        return False
    if _origin(current) == origin:
        return True
    log.warning("Pont de la fenêtre : appel refusé depuis %s", str(current)[:200])
    return False


def _creation(image_id):
    """(gallery, ligne) de la création demandée : un numéro entier positif, sinon None."""
    if isinstance(image_id, bool) or not isinstance(image_id, (int, str)):
        return None
    if isinstance(image_id, str) and not (image_id.isascii() and image_id.isdigit()):
        return None
    number = int(image_id)
    if number <= 0:
        return None
    from aiodos.imaging import gallery
    row = gallery.get(number)
    return None if row is None else (gallery, row)


def pick_files():
    if not _from_interface():
        return []
    chosen = _state["window"].create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True)
    return list(chosen or [])


def pick_folder():
    if not _from_interface():
        return []
    chosen = _state["window"].create_file_dialog(webview.FileDialog.FOLDER)
    return list(chosen or [])


# Images et vidéos créées : un lien « télécharger » ne fait rien dans la fenêtre native
def save_image(image_id):
    """« Enregistrer sous… » : une copie, là où l'utilisateur le choisit."""
    found = _creation(image_id) if _from_interface() else None
    if found is None:
        return None
    gallery, row = found
    source = gallery.path_of(row)
    chosen = _state["window"].create_file_dialog(
        webview.FileDialog.SAVE, directory=str(source.parent), save_filename=source.name)
    if not chosen:
        return None
    target = chosen if isinstance(chosen, str) else chosen[0]
    try:
        shutil.copyfile(source, target)
    except shutil.SameFileError:              # le fichier lui-même : rien à copier
        pass
    # Les droits qu'il aurait eus sans AIODOS (0644 avec l'umask courant), pas le 0600 de
    # l'umask 077 que garde AIODOS pour ses données (paths.keep_private)
    paths.for_user(target)
    return str(target)


def show_image_file(image_id):
    """« Ouvrir le dossier » : l'Explorateur, le fichier sélectionné."""
    found = _creation(image_id) if _from_interface() else None
    if found is None:
        return False
    gallery, row = found
    path = gallery.path_of(row)
    if sys.platform == "win32":
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(path.parent)])
    return True


BRIDGE = (pick_files, pick_folder, save_image, show_image_file)


# ----------------------------------------------------- liens et pages hors de l'interface
class OutsideLinks:
    """À la place de webbrowser pour pywebview, qui y passe les liens target=_blank. Sous
    Windows, webbrowser.open lance le programme inscrit pour n'importe quel schéma
    (search-ms:, ms-word:…), sans la confirmation qu'un navigateur demanderait : seuls le
    web et les courriels sortent."""

    @staticmethod
    def open(url, new=0, autoraise=True):
        scheme = urlsplit(str(url or "")).scheme.lower()
        if scheme == "mailto":                 # sans attach=… : cf. render.plain_mail
            url = plain_mail(str(url))
        if scheme in OUTSIDE:
            return webbrowser.open(url, new, autoraise)
        log.warning("Lien non ouvert (ni web ni courriel) : %s", str(url)[:200])
        return False

    def __getattr__(self, name):
        return getattr(webbrowser, name)


ENGINES = ("edgechromium", "mshtml", "cef", "gtk", "cocoa")     # ceux de pywebview


def guard_new_windows():
    """Le moteur de pywebview, chargé par webview.start, ouvre ses liens par OutsideLinks."""
    for name in ENGINES:
        engine = sys.modules.get(f"webview.platforms.{name}")
        if engine is not None and not isinstance(getattr(engine, "webbrowser", None), OutsideLinks):
            engine.webbrowser = OutsideLinks()


def refused_navigation(uri) -> bool:
    """La fenêtre principale ne quitte pas l'interface du nœud : une page étrangère (un
    fichier .html ou un lien lâché sur la fenêtre) y recevrait le pont vers Python. Avant
    le démarrage du nœud (écran d'attente), rien n'est encore refusé."""
    origin = _state["origin"]
    return origin is not None and _origin(uri) != origin


def refused_request(uri) -> bool:
    """Une requête de la fenêtre principale vers un autre hôte que le nœud, une fois celui-ci
    démarré. Annuler la navigation ne suffit pas : WebView2 envoie quand même la requête
    (essai du 20/10 : l'hôte étranger reçoit le GET 2 ms après le refus), et la politique de
    contenu de l'interface ne couvre pas les navigations. Un contenu qui en lancerait une
    (script, meta refresh) garderait ainsi un canal de sortie. data: et blob: ne quittent
    pas la page."""
    origin = _state["origin"]
    if origin is None:
        return False
    scheme, netloc = _origin(uri)
    return scheme in NETWORK and (NETWORK[scheme], netloc) != origin


def _guard_requests(core):
    """Fil graphique, WebView2 prêt : ce que refused_request écarte reçoit un 403 sans
    quitter la machine."""
    try:
        from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
        core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All)
    except Exception:                  # pywebview pose déjà ce filtre (« * », toutes)
        log.exception("Fenêtre : filtre des requêtes non posé")

    def requested(sender, args):
        try:
            uri = str(args.Request.Uri)
            if refused_request(uri):
                args.Response = core.Environment.CreateWebResourceResponse(
                    None, 403, "Refused", "")
                log.warning("Fenêtre : requête refusée vers %s", uri[:200])
        except Exception:
            log.exception("Fenêtre : garde des requêtes")
    core.WebResourceRequested += requested


def _guard_navigation(window):
    """Fil graphique : chaque navigation de la fenêtre principale passe par la garde, et
    chacune de ses requêtes, dès que WebView2 est prêt."""
    control = getattr(getattr(window.native, "browser", None), "webview", None)
    if _state["guarded"]:
        return
    if control is None or not hasattr(control, "NavigationStarting"):
        log.warning("Fenêtre sans WebView2 : ses navigations ne sont pas gardées")
        return

    def starting(sender, args):
        if refused_navigation(args.Uri):
            args.Cancel = True
            log.warning("Fenêtre : navigation refusée vers %s", str(args.Uri)[:200])
    control.NavigationStarting += starting

    def ready(sender, args):
        if args.IsSuccess:
            _guard_requests(control.CoreWebView2)
    if control.CoreWebView2 is not None:
        _guard_requests(control.CoreWebView2)
    else:                              # WebView2 démarre encore (EnsureCoreWebView2Async)
        control.CoreWebView2InitializationCompleted += ready
    _state["guarded"] = True


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
            icon.notify(t("window.hidden_notice"))
    form.FormClosing += closing


def _on_shown(window):
    # Gardes natives et icône : WebView2, Windows seul. Sous Linux et macOS (moteurs gtk et
    # cocoa de pywebview), rien n'empêche la fenêtre d'aller sur une autre page : ne la
    # protègent que keepDrops (app.js), le pont scellé (SEALED) et _from_interface. À faire
    # si AIODOS y est un jour livré : decide-policy de WebKit2 (gtk), et
    # webView:decidePolicyForNavigationAction (cocoa), qui refuseraient refused_navigation.
    if window.native is None or sys.platform != "win32":
        return
    from System import Action
    try:
        window.native.Invoke(Action(lambda: _guard_navigation(window)))
    except Exception:
        log.exception("Fenêtre : garde des navigations impossible")
    if not tray.available() or _state["tray"] is not None:
        return
    try:
        window.native.Invoke(Action(lambda: _keep_running(window.native)))
    except Exception:
        log.exception("Fenêtre : fermeture vers la zone de notification impossible")


def waiting_page(problem: str | None = None) -> str:
    """L'écran d'attente : le logo, « AIODOS démarre… », ou ce qui l'a empêché, dans la
    langue du nœud (le réglage, sinon celle de Windows)."""
    try:
        logo = base64.b64encode((paths.ASSETS_DIR / "logo_128.png").read_bytes()).decode()
        image = f'<img src="data:image/png;base64,{logo}" alt="">'
    except OSError:
        image = ""
    details = html.escape(t("window.details", path=paths.log_path()))
    text = (f'<p class="err">{html.escape(problem)}</p><p>{details}</p>'
            if problem else f'<p>{html.escape(t("window.starting"))}<span class="dots"></span></p>')
    return f"""<!doctype html><html lang="{i18n.current()}"><head><meta charset="utf-8"><style>
html, body {{ margin: 0; height: 100%; background: #080c1a; color: #e8ecfb;
  font: 15px "Segoe UI", system-ui, sans-serif; display: grid; place-items: center; }}
main {{ text-align: center; max-width: 520px; padding: 24px; }}
img {{ width: 96px; height: 96px; border-radius: 22px; }}
p {{ color: #8e99c0; margin: 16px 0 0; }}
.err {{ color: #ff9aa8; }}
.dots::after {{ content: ""; animation: dots 1.4s steps(4) infinite; }}
@keyframes dots {{ 0% {{ content: ""; }} 25% {{ content: "."; }} 50% {{ content: ".."; }} 75% {{ content: "..."; }} }}
</style></head><body><main>{image}{text}</main></body></html>"""


def run(start, on_closed, quit_on_error: bool = False, hidden: bool = False):
    """Ouvre la fenêtre aussitôt, sur l'écran d'attente, puis l'interface : start(), appelé
    hors du fil graphique, démarre le nœud et rend l'adresse de son interface. S'il échoue,
    l'écran le dit ; ou, quit_on_error, la fenêtre se ferme (premier démarrage d'une nouvelle
    version : le lanceur rend alors la main à la précédente). Bloque jusqu'à la fermeture.
    hidden : la fenêtre reste cachée (démarrage avec Windows) ; l'icône près de l'horloge
    la montre (pywebview la crée tout de même, puis la cache : l'icône naît avec elle)."""
    window = webview.create_window(
        "AIODOS", html=waiting_page(), js_api=SEALED, width=1320, height=860,
        min_size=(980, 640), background_color="#080c1a", text_select=True, hidden=hidden)
    window.expose(*BRIDGE)               # des fonctions, pas un objet : cf. le pont, plus haut
    _state["window"] = window
    _open.append(window)
    window.events.closed += on_closed
    window.events.shown += lambda: _on_shown(window)
    instance.on_show(show)               # AIODOS relancé : on montre celle-ci

    def load():
        try:
            url = start()
        except Exception as exc:
            log.exception("Démarrage du nœud impossible")
            _state["quitting"] = True             # la croix ferme vraiment, sans repli
            if quit_on_error:
                close()
            else:
                window.load_html(waiting_page(t("window.start_failed", error=exc)))
            return
        guard_new_windows()
        _state["origin"] = _origin(url)           # la fenêtre ne quitte plus cette adresse
        window.load_url(url)

    storage = paths.data_dir() / "webview"
    storage.mkdir(exist_ok=True)
    webview.start(load, icon=str(paths.LOGO_ICO), private_mode=False, storage_path=str(storage))
