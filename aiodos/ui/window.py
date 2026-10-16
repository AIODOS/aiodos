"""Fenêtre native (WebView2 / Edge via pywebview) qui affiche l'interface du nœud."""
import webview

from aiodos import paths


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


def run(url: str, on_closed):
    """Ouvre la fenêtre et bloque jusqu'à sa fermeture (boucle graphique)."""
    bridge = Bridge()
    window = webview.create_window(
        "Aiodos", url, js_api=bridge, width=1320, height=860, min_size=(980, 640),
        background_color="#0a0f1f", text_select=True)
    bridge._window = window
    window.events.closed += on_closed
    storage = paths.data_dir() / "webview"
    storage.mkdir(exist_ok=True)
    webview.start(icon=str(paths.LOGO_ICO), private_mode=False, storage_path=str(storage))
