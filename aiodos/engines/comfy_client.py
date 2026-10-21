"""Parler à un ComfyUI de ce PC par son API HTTP (docs/conception/partage-puissance.md § 3.6).
Vérifié le 21/10/2026 sur ComfyUI 0.35.1 (Desktop) :

- POST /prompt {"prompt", "prompt_id", "client_id"} : prompt_id imposé (UUID canonique en
  minuscules, sinon 400 invalid_prompt_id), client_id rangé dans extra_data ; un graphe
  refusé : 400 {"error", "node_errors"}. Jamais d'extra_data venu d'ailleurs.
- GET /api/jobs/{id} : {"status": pending | in_progress | completed | failed | cancelled,
  "execution_error"…} ; 404 tant que le travail n'est pas connu.
- POST /api/jobs/{id}/cancel : ce travail seulement (en cours : interrompu s'il est encore
  celui qui tourne ; en file : retiré), {"cancelled": bool}, sans effet sur les autres.
- POST /upload/image (multipart : image, subfolder, type=input, overwrite) : tout genre de
  fichier, rangé sous input/<subfolder>.
- GET /history/{id} : les sorties par nœud ; GET /view?filename&subfolder&type=output ;
  POST /history {"delete": [id]} ; POST /free.

Seules les adresses de ce PC sont jointes (comfy_find.local_url), sans proxy ni redirection.
"""
import uuid
from pathlib import Path
from urllib.parse import quote

import requests

from aiodos.engines import comfy_find

TIMEOUT = (2, 30)
UPLOAD_TIMEOUT = (2, 300)
VIEW_CHUNK = 1024 * 1024
NO_PROXY = {"http": None, "https": None}


class Unreachable(Exception):
    """Le ComfyUI ne répond pas (fermé, ou occupé à charger au-delà du délai)."""


class Refused(Exception):
    """Le graphe est refusé par ComfyUI : le message, et le nœud en cause s'il est dit."""

    def __init__(self, message: str, node: str | None = None, kind: str | None = None):
        super().__init__(message)
        self.node, self.kind = node, kind


class TooLarge(Exception):
    """Une sortie dépasse le poids permis."""


def new_run_id() -> str:
    """Un identifiant de calcul au format que ComfyUI accepte pour prompt_id."""
    return str(uuid.uuid4())


class Client:
    def __init__(self, url: str):
        found = comfy_find.local_url(url)
        if found is None:
            raise ValueError("ComfyUI : une adresse de ce PC en http est attendue.")
        self.url = found

    # ------------------------------------------------------------------ bas niveau
    def _call(self, method, path, timeout=TIMEOUT, **kw):
        try:
            return requests.request(method, self.url + path, timeout=timeout, proxies=NO_PROXY,
                                    allow_redirects=False, **kw)
        except requests.RequestException as exc:
            raise Unreachable(f"ComfyUI ne répond pas ({exc.__class__.__name__}).") from exc

    def _json(self, method, path, **kw):
        reply = self._call(method, path, **kw)
        if not reply.ok:
            raise Unreachable(f"ComfyUI : {path} a répondu {reply.status_code}.")
        try:
            return reply.json()
        except ValueError as exc:
            raise Unreachable(f"ComfyUI : réponse illisible pour {path}.") from exc

    # --------------------------------------------------------------- lecture
    def system_stats(self) -> dict:
        return self._json("GET", "/system_stats", timeout=(2, comfy_find.STATS_TIMEOUT))

    def object_info(self, cls: str | None = None) -> dict:
        path = "/object_info" if cls is None else f"/object_info/{quote(cls, safe='')}"
        data = self._json("GET", path, timeout=(2, 60))
        return data if isinstance(data, dict) else {}

    def model_folders(self) -> list:
        data = self._json("GET", "/models")
        return [str(f) for f in data if isinstance(f, str)] if isinstance(data, list) else []

    def models(self, folder: str) -> list:
        data = self._json("GET", f"/models/{quote(folder, safe='')}")
        return [str(m) for m in data if isinstance(m, str)] if isinstance(data, list) else []

    # --------------------------------------------------------------- un calcul
    def upload(self, data: bytes, name: str, subfolder: str) -> str:
        """Une entrée rangée sous input/<subfolder>/<name> : rend la valeur à donner au nœud
        (« <subfolder>/<name> »)."""
        reply = self._call("POST", "/upload/image", timeout=UPLOAD_TIMEOUT,
                           files={"image": (name, data, "application/octet-stream")},
                           data={"subfolder": subfolder, "type": "input", "overwrite": "true"})
        if not reply.ok:
            raise Unreachable(f"ComfyUI n'a pas pris l'entrée « {name} » ({reply.status_code}).")
        try:
            got = reply.json()
        except ValueError as exc:
            raise Unreachable("ComfyUI : réponse illisible à l'envoi d'une entrée.") from exc
        saved, folder = str(got.get("name") or name), str(got.get("subfolder") or subfolder)
        return f"{folder}/{saved}" if folder else saved

    def queue(self, graph: dict, run_id: str, client_id: str) -> None:
        """Met le graphe dans la file de ComfyUI sous l'identifiant run_id ; Refused si
        ComfyUI le refuse (nœud inconnu, valeur hors bornes…)."""
        reply = self._call("POST", "/prompt", json={"prompt": graph, "prompt_id": run_id,
                                                    "client_id": client_id})
        if reply.ok:
            return
        try:
            body = reply.json()
        except ValueError:
            body = {}
        if reply.status_code != 400 or not isinstance(body, dict):
            raise Unreachable(f"ComfyUI : la file a répondu {reply.status_code}.")
        raise _refusal(body)

    def job(self, run_id: str) -> dict | None:
        """L'état du calcul ({"status": pending, in_progress, completed, failed, cancelled},
        execution_error…), ou None s'il n'est pas (encore) connu."""
        reply = self._call("GET", f"/api/jobs/{run_id}")
        if reply.status_code == 404:
            return None
        if not reply.ok:
            raise Unreachable(f"ComfyUI : l'état du calcul a répondu {reply.status_code}.")
        try:
            data = reply.json()
        except ValueError as exc:
            raise Unreachable("ComfyUI : état du calcul illisible.") from exc
        return data if isinstance(data, dict) else None

    def cancel(self, run_id: str) -> bool:
        """Arrête ce calcul seulement (jamais ceux de l'utilisateur)."""
        try:
            reply = self._call("POST", f"/api/jobs/{run_id}/cancel")
            return bool(reply.ok and reply.json().get("cancelled"))
        except (Unreachable, ValueError, AttributeError):
            return False

    def history(self, run_id: str) -> dict | None:
        data = self._json("GET", f"/history/{run_id}")
        entry = data.get(run_id) if isinstance(data, dict) else None
        return entry if isinstance(entry, dict) else None

    def download(self, filename: str, subfolder: str, kind: str, dest: Path,
                 limit: int) -> int:
        """Une sortie, en flux vers dest, octets comptés ; TooLarge au-delà de limit."""
        reply = self._call("GET", "/view", timeout=(2, 120), stream=True,
                           params={"filename": filename, "subfolder": subfolder, "type": kind})
        with reply:
            if not reply.ok:
                raise Unreachable(f"ComfyUI : la sortie « {filename} » a répondu "
                                  f"{reply.status_code}.")
            size = 0
            with open(dest, "wb") as out:
                for chunk in reply.iter_content(VIEW_CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise TooLarge(f"Sortie « {filename} » trop lourde.")
                    out.write(chunk)
        return size

    def forget(self, run_id: str) -> None:
        """Efface ce calcul de l'historique de ComfyUI (ses sorties, son graphe)."""
        try:
            self._call("POST", "/history", json={"delete": [run_id]})
        except Unreachable:
            pass

    def free(self, unload_models=True, free_memory=False) -> None:
        try:
            self._call("POST", "/free", json={"unload_models": unload_models,
                                              "free_memory": free_memory})
        except Unreachable:
            pass


def _refusal(body: dict) -> Refused:
    """Le refus d'un graphe en clair : le message de ComfyUI et le premier nœud en cause."""
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    message = str(error.get("message") or "graphe refusé")[:300]
    nodes = body.get("node_errors") if isinstance(body.get("node_errors"), dict) else {}
    node, detail = None, ""
    for node_id, info in nodes.items():
        node = str(node_id)
        errors = info.get("errors") if isinstance(info, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            detail = str(errors[0].get("message") or errors[0].get("details") or "")[:300]
        break
    text = f"{message} ({detail})" if detail else message
    return Refused(text, node=node, kind=str(error.get("type") or "") or None)
