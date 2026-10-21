"""Les secrets de cette machine, à l'abri sur le disque (étape 2.3 de
docs/phase2-internet.md) : les clés des paires de machines appairées et les clés d'API
du cloud.

Sous Windows, ils sont chiffrés par DPAPI pour le compte de l'utilisateur
(CryptProtectData) : la base copiée sur une autre machine, ou lue depuis un autre compte,
ne les livre pas. Ailleurs (Linux, macOS, Android), il n'y a pas de coffre : ils restent
en clair dans la base, et seuls les droits des fichiers les gardent (dossier de données en
0700, base et journaux en 0600, cf. paths.keep_private ; sous Android, en plus, le dossier
privé de l'appli). Cela les ferme aux autres comptes de la machine, pas à un programme
lancé sous le même compte, ni à qui emporte une copie de la base. Fermer les droits après
coup ne reprend pas un secret déjà lu : la base des versions d'avant le 20/10/2026 était
lisible par tous (0644), et un descripteur ouvert avant le chmod le reste. Sur une machine
partagée, refaire les appairages et changer les clés du cloud et de la passerelle.

Un secret protégé commence par MARK (octets) ou TEXT_MARK (texte). Un secret enregistré par
cette version est protégé tout de suite. Une valeur sans marque (enregistrée avant) est
rendue telle quelle : elle est protégée plus tard (peers.protect_keys,
settings.protect_secrets), une fois la version en bonne santé, pour qu'une version
précédente relancée par les mises à jour sache encore lire les clés qu'elle avait
enregistrées (pas celles des appairages faits entre-temps). DPAPI indisponible : le
secret est gardé tel quel, et protégé au prochain démarrage. Un secret illisible (base
venue d'un autre compte ou d'une autre machine) lève Unreadable : la machine appairée est à
appairer de nouveau, la clé d'API à saisir de nouveau.

Une version d'origine 2.0.x peut reprendre la main sur ces données (updates/launcher.py :
version courante qui ne démarre pas). Elle ne connaît ni les amis ni les machines mises de
côté : elle se servirait de leur clé comme de celle d'une de mes machines. Leur clé est donc
gardée sous une forme qu'elle ne sait pas employer (hold) : scellée par DPAPI, sinon
précédée de HELD. Une 2.0.x lit alors une clé de 35 octets, que ChaCha20-Poly1305 refuse :
elle échoue au lieu de servir l'ami (sous Windows, DPAPI avait déjà cet effet). HELD n'est
pas un chiffrement : il ne protège que de ce retour en arrière, pas d'un programme lancé
sous le même compte. Une de mes machines garde sa clé comme avant (release) : hors DPAPI,
une 2.0.x relancée la lit encore.
"""
import base64
import binascii
import logging
import sys
import threading

log = logging.getLogger("aiodos")

MARK = b"dpapi1:"
TEXT_MARK = "dpapi1:"
# Clé d'un ami ou d'une machine mise de côté, là où DPAPI manque (hold). HELD n'est suivi
# que d'une clé de paire en clair, de PAIR_KEY octets (network/crypto.py) : une valeur de
# PAIR_KEY octets n'est donc jamais prise pour une clé marquée, même si elle commence par HELD.
HELD = b"k2:"
PAIR_KEY = 32
_ENTROPY = b"aiodos/coffre/1"           # propre à AIODOS : un autre programme ne lit pas ces secrets
_UI_FORBIDDEN = 0x01
_lock = threading.Lock()
_opened = {}                            # secret protégé → clair : DPAPI n'est appelé qu'une fois


class Unreadable(Exception):
    """Secret protégé illisible ici (autre compte, autre machine, données abîmées)."""


def available() -> bool:
    return sys.platform == "win32"


def _call(function, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw: bytes):
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    source, keep = blob(data)
    entropy, keep_entropy = blob(_ENTROPY)
    out = Blob()
    crypt32 = ctypes.windll.crypt32
    if function == "protect":
        ok = crypt32.CryptProtectData(ctypes.byref(source), "AIODOS", ctypes.byref(entropy),
                                      None, None, _UI_FORBIDDEN, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy),
                                        None, None, _UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise Unreadable(f"DPAPI a refusé ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)
        del keep, keep_entropy


def protect(data: bytes) -> bytes:
    """Le secret, protégé s'il peut l'être ici (déjà protégé : inchangé). Il sert à tous les
    secrets (clés d'API comprises) et ne connaît pas HELD : une clé de paire marquée se
    passe en clair (unprotect, release), sinon la marque serait scellée avec elle."""
    data = bytes(data)
    if not available() or data.startswith(MARK):
        return data
    try:
        sealed = MARK + _call("protect", data)
    except (Unreadable, OSError) as exc:       # DPAPI indisponible : gardé tel quel, protégé
        log.warning("Secret gardé sans protection pour l'instant : %s", exc)   # plus tard
        return data
    with _lock:
        _opened[sealed] = data
    return sealed


def held(data) -> bool:
    """Clé de paire marquée HELD (hold, sans DPAPI) ?"""
    data = bytes(data or b"")
    return len(data) == len(HELD) + PAIR_KEY and data.startswith(HELD)


def hold(data) -> bytes:
    """La clé d'une paire hors de portée d'une version d'origine 2.0.x (un ami, une machine
    mise de côté) : scellée par DPAPI si elle peut l'être ici, sinon précédée de HELD. Déjà
    scellée ou marquée, ou qui n'est pas une clé de paire en clair : inchangée."""
    data = bytes(data or b"")
    if len(data) != PAIR_KEY:
        return data
    sealed = protect(data)
    return sealed if sealed.startswith(MARK) else HELD + data


def release(data) -> bytes:
    """La clé d'une paire redevenue une de mes machines (plus un ami, ou rétablie) : sans
    la marque HELD, comme toute clé d'une de mes machines (scellée par DPAPI : inchangée)."""
    data = bytes(data or b"")
    return data[len(HELD):] if held(data) else data


def unprotect(data) -> bytes:
    """Le secret en clair ; une valeur sans marque est rendue telle quelle, une clé marquée
    HELD sans sa marque."""
    data = bytes(data or b"")
    if held(data):
        return data[len(HELD):]
    if not data.startswith(MARK):
        return data
    with _lock:
        known = _opened.get(data)
    if known is not None:
        return known
    if not available():
        raise Unreadable("secret protégé par DPAPI sur une autre machine (Windows)")
    clear = _call("unprotect", data[len(MARK):])
    with _lock:
        if len(_opened) > 1000:
            _opened.clear()
        _opened[data] = clear
    return clear


def verify(sealed: bytes, clear: bytes) -> bool:
    """Le secret protégé se relit-il vraiment (par DPAPI, pas de mémoire) ?"""
    if not sealed.startswith(MARK):
        return sealed == clear
    try:
        return _call("unprotect", sealed[len(MARK):]) == clear
    except Unreadable:
        return False


def verify_text(sealed: str, clear: str) -> bool:
    if not isinstance(sealed, str) or not sealed.startswith(TEXT_MARK):
        return sealed == clear
    try:
        raw = base64.b64decode(sealed[len(TEXT_MARK):], validate=True)
    except (binascii.Error, ValueError):
        return False
    return verify(MARK + raw, clear.encode("utf-8"))


def is_protected(data) -> bool:
    """Scellé par DPAPI ? Une clé marquée HELD ne l'est pas : peers.protect_keys la scelle
    dès que DPAPI le permet."""
    if isinstance(data, str):
        return data.startswith(TEXT_MARK)
    return bytes(data or b"").startswith(MARK)


def protect_text(text: str) -> str:
    if not text or not available() or text.startswith(TEXT_MARK):
        return text
    sealed = protect(text.encode("utf-8"))
    if not sealed.startswith(MARK):             # DPAPI indisponible : tel quel
        return text
    return TEXT_MARK + base64.b64encode(sealed[len(MARK):]).decode("ascii")


def unprotect_text(text: str) -> str:
    if not isinstance(text, str) or not text.startswith(TEXT_MARK):
        return text
    try:
        raw = base64.b64decode(text[len(TEXT_MARK):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Unreadable("secret protégé abîmé") from exc
    return unprotect(MARK + raw).decode("utf-8")
