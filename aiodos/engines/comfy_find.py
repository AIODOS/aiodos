"""Trouver ComfyUI sur ce PC (docs/conception/partage-puissance.md § 3.2) : son installation
et l'instance ouverte. Rien n'est lancé ni installé ici : AIODOS ne livre pas ComfyUI.

- L'installation, dans l'ordre : le réglage engines_comfyui_path ; Comfy Desktop
  (%LOCALAPPDATA%\\Comfy-Desktop\\ComfyUI-Installs\\<nom>\\ : ComfyUI\\ avec son .venv,
  standalone-env\\, manifest.json ; ses entrées et sorties dans ComfyUI-Shared\\input et
  \\output) ; la version portable (ComfyUI\\ à côté de python_embeded\\). La version est
  celle du code (ComfyUI\\comfyui_version.py) : Desktop met le code à jour sans changer son
  manifest.json (0.35.1 dans un environnement « v0.34.0-env1 », vu le 21/10/2026).
- L'instance ouverte : l'adresse réglée (engines_comfyui_url), puis 127.0.0.1:8188 (Desktop
  et la version portable), puis :8000. Une connexion TCP de 0,5 s, puis
  GET /system_stats en 8 s au plus (le premier appel réveille CUDA). Seules les adresses de
  ce PC sont essayées : un ComfyUI d'une autre machine n'est pas « celui de ce PC ».
- Le programme qui tient le port est noté (gateway/caller.holder_of) : autre chose que
  ComfyUI sur 8188 se voit.
"""
import json
import os
import re
import socket
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from urllib.parse import urlsplit

import requests

from aiodos import settings

PORTS = (8188, 8000)
CONNECT_TIMEOUT = 0.5
STATS_TIMEOUT = 8
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_VERSION = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)


@dataclass(frozen=True)
class Install:
    """Une installation de ComfyUI : le dossier du code (main.py), son Python, sa sorte."""
    comfyui: Path
    python: Path | None
    kind: str                   # setting, desktop, portable
    version: str | None
    name: str = ""              # le nom donné dans Desktop


@dataclass(frozen=True)
class Instance:
    """Un ComfyUI qui répond sur ce PC."""
    url: str
    version: str | None
    pytorch: str | None
    device: str | None
    vram_mib: int | None
    argv: tuple = field(default=())
    program: str | None = None  # le programme qui tient le port, s'il se lit


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def version_of(comfyui: Path) -> str | None:
    try:
        found = _VERSION.search((comfyui / "comfyui_version.py").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return found.group(1) if found else None


def _is_code(folder: Path) -> bool:
    return (folder / "main.py").is_file() and (folder / "comfy").is_dir()


def _from_folder(folder: Path, kind: str) -> Install | None:
    """Le dossier du code, ou celui qui le contient (Desktop, portable)."""
    for comfyui in (folder, folder / "ComfyUI"):
        if not _is_code(comfyui):
            continue
        base = comfyui.parent
        # Desktop lance ComfyUI par le Python de son .venv (standalone-env n'en est que la
        # base, sans torch : vu le 21/10/2026) ; la version portable, par python_embeded
        python = next((p for p in (comfyui / ".venv" / "Scripts" / "python.exe",
                                   base / "python_embeded" / "python.exe",
                                   base / "standalone-env" / "python.exe") if p.is_file()), None)
        if kind == "setting" or python is not None:
            return Install(comfyui, python, kind, version_of(comfyui), base.name)
    return None


def installs() -> list:
    """Les installations trouvées, la préférée d'abord : celle du réglage, puis celles de
    Desktop (la plus récemment utilisée d'abord), puis une version portable dans les
    dossiers habituels."""
    found = []
    chosen = str(settings.get("engines_comfyui_path") or "").strip()
    if chosen:
        one = _from_folder(Path(chosen), "setting")
        if one:
            found.append(one)
    desktop = _local_appdata() / "Comfy-Desktop" / "ComfyUI-Installs"
    try:
        folders = sorted((p for p in desktop.iterdir() if p.is_dir()),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        folders = []
    for folder in folders:
        one = _from_folder(folder, "desktop")
        if one and (folder / "manifest.json").is_file():
            found.append(one)
    for folder in (Path.home() / "ComfyUI_windows_portable", Path("C:/ComfyUI_windows_portable")):
        one = _from_folder(folder, "portable")
        if one:
            found.append(one)
    seen, unique = set(), []
    for one in found:
        key = os.path.normcase(str(one.comfyui))
        if key not in seen:
            seen.add(key)
            unique.append(one)
    return unique


def desktop_manifest(install: Install) -> dict:
    """Le manifest.json de Desktop (environnement, versions de torch) ; {} sinon."""
    try:
        data = json.loads((install.comfyui.parent / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def local_url(url) -> str | None:
    """« http://127.0.0.1:8188 » si l'adresse désigne ce PC, en http ; None sinon."""
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or (parts.hostname or "") not in LOOPBACK or not port:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"http://{host}:{port}"


def candidates() -> list:
    out = []
    for url in [settings.get("engines_comfyui_url")] + [f"http://127.0.0.1:{p}" for p in PORTS]:
        url = local_url(url)
        if url and url not in out:
            out.append(url)
    return out


def _open(url: str) -> bool:
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def probe(url: str) -> Instance | None:
    """Le ComfyUI qui répond à cette adresse de ce PC, ou None (rien n'écoute, ou ce n'est
    pas ComfyUI)."""
    url = local_url(url)
    if url is None or not _open(url):
        return None
    try:
        reply = requests.get(f"{url}/system_stats", timeout=(CONNECT_TIMEOUT, STATS_TIMEOUT),
                             proxies={"http": None, "https": None})
        data = reply.json() if reply.ok else None
    except (requests.RequestException, ValueError):
        return None
    system = data.get("system") if isinstance(data, dict) else None
    if not isinstance(system, dict) or "comfyui_version" not in system:
        return None
    devices = [d for d in data.get("devices") or [] if isinstance(d, dict)]
    first = devices[0] if devices else {}
    vram = first.get("vram_total")
    argv = system.get("argv")
    return Instance(url=url, version=_text(system.get("comfyui_version")),
                    pytorch=_text(system.get("pytorch_version")), device=_text(first.get("name")),
                    vram_mib=int(vram // 2 ** 20) if isinstance(vram, (int, float)) and vram > 0
                    else None,
                    argv=tuple(str(a) for a in argv) if isinstance(argv, list) else (),
                    program=_holder(url))


def _text(value) -> str | None:
    return str(value)[:200] if isinstance(value, (str, int, float)) and str(value) else None


def _holder(url: str) -> str | None:
    try:
        from aiodos.gateway import caller
        return caller.holder_of(urlsplit(url).port)
    except Exception:
        return None


def instance() -> Instance | None:
    """Le premier ComfyUI ouvert sur ce PC, dans l'ordre de candidates()."""
    for url in candidates():
        found = probe(url)
        if found is not None:
            return found
    return None
