# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Le coffre de la clé d'accès de l'appli : get(), set(clé), delete().

- `Keystore.dpapi(chemin)` (Windows) : la clé chiffrée par DPAPI pour le compte de
  l'utilisateur (CryptProtectData), avec une entropie propre à l'appli
  (b"aiodos-client/" + app_id) : copiée sur une autre machine, lue depuis un autre compte ou
  par une autre appli qui se sert aussi de ce client, elle ne se déchiffre pas. Adapté de
  aiodos/node/vault.py (Apache-2.0).
- `Keystore.file(chemin)` (hors de Windows) : un fichier en 0600, dans un dossier en 0700.
  Il ne protège que des autres comptes de la machine.
- `Keystore.memory()` : le temps du programme (essais).

Une appli peut aussi donner son propre objet, pourvu qu'il ait ces trois méthodes. La clé
n'apparaît jamais dans repr() ni str(), ni dans le journal. Une clé illisible (autre compte,
autre machine, fichier abîmé) se lit comme une clé absente : l'appli redemandera l'accès.
"""

import ctypes
import logging
import os
import sys
import tempfile
import threading

log = logging.getLogger("aiodos_client")

WINDOWS = sys.platform == "win32"
MAGIC = b"aiodos-client:dpapi1:"
ENTROPY = b"aiodos-client/"
MAX_FILE = 64 * 1024
_UI_FORBIDDEN = 0x01

if WINDOWS:
    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _Protect = _crypt32.CryptProtectData
    _Protect.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p, ctypes.POINTER(_Blob),
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                         ctypes.POINTER(_Blob)]
    _Protect.restype = ctypes.c_int
    _Unprotect = _crypt32.CryptUnprotectData
    _Unprotect.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                           ctypes.POINTER(_Blob)]
    _Unprotect.restype = ctypes.c_int
    _LocalFree = _kernel32.LocalFree
    _LocalFree.argtypes = [ctypes.c_void_p]
    _LocalFree.restype = ctypes.c_void_p


def _dpapi(protect: bool, data: bytes, entropy: bytes) -> bytes:
    source_buffer = ctypes.create_string_buffer(data, len(data))
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_char)))
    entropy_buffer = ctypes.create_string_buffer(entropy, len(entropy))
    extra = _Blob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    try:
        if protect:
            ok = _Protect(ctypes.byref(source), "aiodos-client", ctypes.byref(extra), None, None,
                          _UI_FORBIDDEN, ctypes.byref(out))
        else:
            ok = _Unprotect(ctypes.byref(source), None, ctypes.byref(extra), None, None,
                            _UI_FORBIDDEN, ctypes.byref(out))
        if not ok:
            raise OSError(ctypes.get_last_error(), "DPAPI a refusé")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
    finally:
        ctypes.memset(source_buffer, 0, len(data))      # la copie en clair, au plus tôt


def _expand(path) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(os.fspath(path))))


def _write_private(path: str, data: bytes) -> None:
    """Écriture atomique d'un fichier lisible par ce compte seulement (0600 hors de Windows)."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".aiodos-key-", dir=folder)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if not WINDOWS:
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


class Keystore:
    """Le coffre de la clé d'accès. À construire par Keystore.dpapi(), .file() ou .memory()."""

    def __init__(self, kind, path=None, app_id=None, key=None):
        self._kind = kind
        self._path = path
        self._app_id = app_id
        self._value = key
        self._lock = threading.Lock()

    @classmethod
    def dpapi(cls, path, app_id=None):
        """La clé chiffrée par DPAPI dans `path` (variables comme %LOCALAPPDATA% développées).

        `app_id` est fixé par Aiodos(app_id=…) s'il n'est pas donné ici.
        """
        if not WINDOWS:
            raise OSError("DPAPI n'existe que sous Windows : prends Keystore.file().")
        return cls("dpapi", _expand(path), app_id)

    @classmethod
    def file(cls, path):
        """La clé dans un fichier en 0600 (hors de Windows)."""
        return cls("file", _expand(path))

    @classmethod
    def memory(cls, key=None):
        """La clé en mémoire, le temps du programme."""
        return cls("memory", key=key or None)

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def path(self):
        return self._path

    def bind(self, app_id: str) -> None:
        """Fixe l'identifiant de l'appli (l'entropie de DPAPI) ; Aiodos l'appelle."""
        if self._kind != "dpapi":
            return
        with self._lock:
            if self._app_id is None:
                self._app_id = app_id
            elif self._app_id != app_id:
                raise ValueError("Ce coffre appartient à une autre application "
                                 f"(« {self._app_id} »).")

    def _entropy(self) -> bytes:
        if not self._app_id:
            raise ValueError("Keystore.dpapi : l'identifiant de l'appli (app_id) manque.")
        return ENTROPY + self._app_id.encode("utf-8")

    def get(self):
        """La clé, ou None (absente ou illisible)."""
        with self._lock:
            if self._kind == "memory":
                return self._value
            entropy = self._entropy() if self._kind == "dpapi" else None
            try:
                with open(self._path, "rb") as handle:
                    raw = handle.read(MAX_FILE + 1)
            except FileNotFoundError:
                return None
            except OSError as exc:
                log.warning("Coffre de la clé illisible (%s) : clé ignorée.", type(exc).__name__)
                return None
            if self._kind == "file":
                text = raw[:MAX_FILE].decode("utf-8", "replace").strip()
                return text or None
            if not raw.startswith(MAGIC):
                log.warning("Coffre de la clé abîmé : clé ignorée.")
                return None
            try:
                clear = _dpapi(False, raw[len(MAGIC):MAX_FILE], entropy)
            except OSError:
                log.warning("Clé illisible ici (autre compte, autre machine ou autre appli) : "
                            "l'accès est à redemander.")
                return None
            return clear.decode("utf-8", "replace").strip() or None

    def set(self, key: str) -> None:
        """Range la clé (écriture atomique)."""
        if not isinstance(key, str) or not key.strip():
            raise ValueError("clé vide")
        key = key.strip()
        with self._lock:
            if self._kind == "memory":
                self._value = key
                return
            if self._kind == "file":
                data = key.encode("utf-8") + b"\n"
            else:
                data = MAGIC + _dpapi(True, key.encode("utf-8"), self._entropy())
            _write_private(self._path, data)

    def delete(self) -> None:
        """Efface la clé."""
        with self._lock:
            if self._kind == "memory":
                self._value = None
                return
            try:
                os.remove(self._path)
            except FileNotFoundError:
                pass

    def __repr__(self):
        if self._kind == "memory":
            return f"Keystore.memory({'pleine' if self._value else 'vide'})"
        return f"Keystore.{self._kind}({self._path!r})"

    __str__ = __repr__
