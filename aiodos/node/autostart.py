"""Démarrer AIODOS avec l'appareil, en arrière-plan (demande de l'auteur, 20/10/2026).

Windows : un raccourci « AIODOS » dans le dossier Démarrage de l'utilisateur (sans droits
administrateur) lance AIODOS sans ouvrir sa fenêtre (--background) ; l'icône près de
l'horloge la rouvre. Il vise la version d'origine, comme le raccourci du menu Démarrer :
le lanceur y choisit ensuite la version à jour (updates/launcher.py).

Android : l'appli AIODOS démarre le nœud dans Termux quand le téléphone démarre
(android/…/BootReceiver.java), seulement si le fichier ~/.aiodos-autostart existe ; le
nœud tourne alors sans que l'appli s'ouvre.

L'état se lit sur le disque (le raccourci, le fichier) : il reste juste même si
l'utilisateur supprime le raccourci à la main.
"""
import os
import subprocess
import sys
from pathlib import Path

from aiodos import paths
from aiodos.node import android
from aiodos.updates import launcher

SHORTCUT = "AIODOS.lnk"
ANDROID_FLAG = ".aiodos-autostart"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _startup_folder() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if sys.platform != "win32" or not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _android_flag() -> Path | None:
    home = os.environ.get("HOME")
    return Path(home) / ANDROID_FLAG if android.is_android() and home else None


def state() -> dict:
    """{"supported", "enabled", "device"} : « device » nomme l'appareil pour l'interface."""
    folder = _startup_folder()
    if folder is not None:
        return {"supported": True, "enabled": (folder / SHORTCUT).is_file(),
                "device": "windows"}
    flag = _android_flag()
    if flag is not None:
        return {"supported": True, "enabled": flag.is_file(), "device": "android"}
    return {"supported": False, "enabled": False, "device": None}


def set_enabled(enabled: bool) -> dict:
    """Active ou retire le démarrage avec l'appareil. → state(). OSError si impossible."""
    folder = _startup_folder()
    if folder is not None:
        target = folder / SHORTCUT
        if enabled:
            folder.mkdir(parents=True, exist_ok=True)
            _windows_shortcut(target)
        else:
            target.unlink(missing_ok=True)
        return state()
    flag = _android_flag()
    if flag is not None:
        if enabled:
            flag.write_text("Démarrer le nœud AIODOS avec le téléphone (appli AIODOS).\n",
                            encoding="utf-8")
        else:
            flag.unlink(missing_ok=True)
        return state()
    raise OSError("Démarrer avec l'appareil : seulement sous Windows et sur Android.")


def command() -> tuple:
    """(programme, arguments, dossier) du démarrage en arrière-plan : pythonw (sans
    console) de la version d'origine, « -m aiodos --background », dans son dossier."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if windowless.is_file():
        exe = windowless
    return str(exe), "-m aiodos --background", str(launcher.base_dir())


def _ps(text: str) -> str:
    """Chaîne PowerShell entre apostrophes (une apostrophe s'y double)."""
    return "'" + str(text).replace("'", "''") + "'"


def _windows_shortcut(target: Path):
    exe, arguments, folder = command()
    icon = paths.LOGO_ICO if Path(paths.LOGO_ICO).is_file() else exe
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps(target)}); "
              f"$s.TargetPath = {_ps(exe)}; $s.Arguments = {_ps(arguments)}; "
              f"$s.WorkingDirectory = {_ps(folder)}; $s.IconLocation = {_ps(icon)}; "
              "$s.Description = 'AIODOS, en arrière-plan'; $s.WindowStyle = 7; $s.Save()")
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=30,
                              creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(f"raccourci de démarrage impossible : {exc}") from exc
    if done.returncode != 0 or not target.is_file():
        raise OSError(f"raccourci de démarrage impossible : {(done.stderr or '').strip()[:300]}")
