"""Dossiers et fichiers privés : personne d'autre que moi, le système et les
administrateurs ne peut y écrire (ni les lire, pour une clé).

Les outils de publication s'en servent (tools/publish.py, companion_build.py,
build_installer.py) : ce qu'ils signent ou livrent vient du dépôt et de leurs dossiers de
travail. Si un autre compte de la machine peut y écrire, il peut y glisser du code (un
« core.fsmonitor » dans .git/config, une dépendance compilée piégée, une source modifiée
entre l'export et la compilation) que la clé de publication signerait pour toutes les
machines. Un dossier sous C:\\ hérite par défaut de « Utilisateurs authentifiés :
modification » ; %TEMP% peut porter les droits d'un bac à sable (comptes CodexSandbox…).

- foreign_writers / foreign_readers : qui d'autre a ce droit (liste vide : privé) ;
- exposed / describe : ceux d'une liste de chemins qui ne sont pas privés, pour un refus
  lisible (« ce dépôt est modifiable par… ») ;
- private_dir : un dossier de travail privé, créé tel (liste d'accès protégée sous
  Windows, 0700 ailleurs), refusé s'il existait déjà ouvert à d'autres ;
- private_tempdir : de même, un dossier temporaire neuf, effacé à la sortie ;
- require_private_repository : le dépôt d'où l'on publie, tel que les outils le lisent et
  l'exécutent (ci-dessous).

Sous Windows, seules comptent les entrées qui accordent un droit : un SID d'application
ou de capacité (S-1-15-…, bacs à sable du même compte) ou un SID restreignant
(S-1-5-12, S-1-5-33) n'ouvre rien à un autre compte, puisque Windows exige en plus que le
compte lui-même soit autorisé.

Le contrôle du dépôt tourne avant que les outils n'importent quoi que ce soit du dépôt : ils
chargent ce fichier par son chemin, le dossier du dépôt et celui des outils retirés de
sys.path, sans lire aucun __pycache__ (amorce en tête de tools/publish.py et
tools/build_installer.py). Ce fichier ne dépend donc que de la bibliothèque standard. Il
refuse :
- un chemin du dépôt (.git compris) que d'autres comptes peuvent modifier, ou qu'un autre
  compte possède (il l'y aurait créé) ;
- un fichier non suivi par git là où Python cherche les modules des outils (aiodos/,
  tools/, la racine) : un paquet tools/engine_release/ planté passerait devant
  engine_release.py, un .pyd devant un .py ;
- une configuration git qui ferait lire ou exécuter ce que le dépôt ne montre pas
  (inclusions, filtres, attributs pris ailleurs, autre arbre de travail), ou qui trahit un
  dépôt piégé (programme de suivi, crochets, éditeur, programme de signature : neutralisés
  par GIT_SAFE dans les outils, mais lancés par le git de l'auteur) ;
- des objets de remplacement (refs/replace : git archive sortirait un autre fichier que
  celui du commit, git status ne montrerait rien) et des attributs dans .git/info.
"""
import functools
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Droits qui permettent de changer un fichier ou un dossier (écrire, ajouter, effacer,
# changer ses droits ou son propriétaire), ou de lire un fichier
_WRITE = (0x2 | 0x4 | 0x40            # données, ajout, enfants
          | 0x10000 | 0x40000 | 0x80000   # DELETE, droits (DAC), propriétaire
          | 0x40000000 | 0x10000000)      # GENERIC_WRITE, GENERIC_ALL
_READ = 0x1 | 0x80000000 | 0x10000000                       # données, GENERIC_READ, GENERIC_ALL
_TRUSTED = {"S-1-5-18",          # SYSTEM
            "S-1-5-32-544",      # Administrateurs
            "S-1-3-0",           # CREATOR OWNER (remplacé par celui qui crée)
            "S-1-3-4",           # OWNER RIGHTS
            "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}  # TrustedInstaller
_NOT_AN_ACCOUNT = ("S-1-15-",    # applications et capacités : bacs à sable du même compte
                   "S-1-5-12",   # code restreint
                   "S-1-5-33")   # écriture restreinte
_ALLOWED_TYPES = {0: 8, 9: 8}    # ACCESS_ALLOWED(_CALLBACK)_ACE : SID après l'en-tête et le masque
_ALLOWED_OBJECT_TYPES = (5, 11)  # ACCESS_ALLOWED(_CALLBACK)_OBJECT_ACE


class NotPrivate(Exception):
    """Dossier ou fichier ouvert à d'autres comptes (message lisible)."""


# ------------------------------------------------------------------ décision
def foreign(entries, owner: str | None, me: str, mask: int) -> list:
    """SID qui ont l'un des droits `mask`, hors moi, le système et les administrateurs,
    d'après les entrées [(SID, droits)] qui accordent ; le propriétaire, s'il est un autre,
    peut toujours changer les droits."""
    found = []

    def counts(sid):
        return sid != me and sid not in _TRUSTED and not sid.startswith(_NOT_AN_ACCOUNT)
    if owner and counts(owner):
        found.append(owner)
    for sid, rights in entries:
        if rights & mask and counts(sid) and sid not in found:
            found.append(sid)
    return found


# ------------------------------------------------------------------ Windows
@functools.cache
def _win():
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.LookupAccountSidW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)]
    advapi.LookupAccountSidW.restype = wintypes.BOOL
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    return ctypes, wintypes, advapi, kernel


def _sid_text(ctypes, advapi, kernel, sid) -> str:
    out = ctypes.c_void_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "SID illisible")
    try:
        return ctypes.wstring_at(out.value)
    finally:
        kernel.LocalFree(out)


@functools.cache
def my_sid() -> str:
    """SID du compte qui fait tourner ce programme (Windows ; lu une fois : le dépôt entier
    est examiné, chemin par chemin)."""
    ctypes, wintypes, advapi, kernel = _win()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "jeton du processus illisible")
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))     # TokenUser
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), "compte du processus illisible")
        sid = ctypes.c_void_p.from_buffer(buffer).value
        return _sid_text(ctypes, advapi, kernel, sid)
    finally:
        kernel.CloseHandle(token)


def _windows_entries(path: Path) -> tuple:
    """(propriétaire, [(SID, droits)] des entrées qui accordent) ; FileNotFoundError si le
    chemin n'existe pas, OSError si ses droits sont illisibles."""
    ctypes, wintypes, advapi, kernel = _win()
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    error = advapi.GetNamedSecurityInfoW(str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None,
                                         ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error in (2, 3):
        raise FileNotFoundError(str(path))
    if error:
        raise OSError(error, f"droits illisibles : {path}")
    try:
        owner_sid = _sid_text(ctypes, advapi, kernel, owner) if owner.value else None
        if not dacl.value:                        # aucune liste : tout le monde a tout
            return owner_sid, [("S-1-1-0", _WRITE | _READ)]
        count = ctypes.c_uint16.from_address(dacl.value + 4).value
        entries = []
        for index in range(count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise OSError(ctypes.get_last_error(), f"droits illisibles : {path}")
            kind = ctypes.c_uint8.from_address(ace.value).value
            rights = ctypes.c_uint32.from_address(ace.value + 4).value
            if kind in _ALLOWED_TYPES:
                offset = _ALLOWED_TYPES[kind]
            elif kind in _ALLOWED_OBJECT_TYPES:
                flags = ctypes.c_uint32.from_address(ace.value + 8).value
                offset = 12 + 16 * bool(flags & 1) + 16 * bool(flags & 2)
            else:
                continue                          # refus, audit : n'ouvrent rien
            entries.append((_sid_text(ctypes, advapi, kernel, ace.value + offset), rights))
        return owner_sid, entries
    finally:
        kernel.LocalFree(descriptor)


def account_name(sid: str) -> str:
    """« Utilisateurs authentifiés (S-1-5-11) », ou le SID seul s'il est inconnu ici."""
    if sys.platform != "win32" or not sid.startswith("S-"):
        return sid
    try:
        ctypes, wintypes, advapi, kernel = _win()
        raw = ctypes.c_void_p()
        if not advapi.ConvertStringSidToSidW(sid, ctypes.byref(raw)):
            return sid
        try:
            name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            size, dsize, use = wintypes.DWORD(256), wintypes.DWORD(256), ctypes.c_int()
            if not advapi.LookupAccountSidW(None, raw, name, ctypes.byref(size), domain,
                                            ctypes.byref(dsize), ctypes.byref(use)):
                return sid
            full = f"{domain.value}\\{name.value}" if domain.value else name.value
            return f"{full} ({sid})"
        finally:
            kernel.LocalFree(raw)
    except OSError:
        return sid


# -------------------------------------------------------------- Linux, macOS
def _group_has_others(gid: int, uid: int) -> bool:
    try:
        import grp
        import pwd
        me = pwd.getpwuid(uid).pw_name
        members = set(grp.getgrgid(gid).gr_mem)
        members |= {p.pw_name for p in pwd.getpwall() if p.pw_gid == gid}
    except (ImportError, KeyError, OSError):
        return True                               # inconnu : on suppose que oui
    return bool(members - {me})


def _posix(path: Path, write: bool) -> list:
    info = os.stat(path)
    uid = os.getuid()
    found = []
    if info.st_uid not in (uid, 0):
        found.append(f"propriétaire uid {info.st_uid}")
    other, group = (0o002, 0o020) if write else (0o004, 0o040)
    if info.st_mode & other:
        found.append("tous les comptes")
    if info.st_mode & group and _group_has_others(info.st_gid, uid):
        found.append(f"groupe {info.st_gid}")
    return found


# ------------------------------------------------------------------ public
def _others(path, write: bool) -> list:
    path = Path(path)
    if sys.platform != "win32":
        return _posix(path, write)
    owner, entries = _windows_entries(path)
    return [account_name(sid) for sid in foreign(entries, owner, my_sid(),
                                                 _WRITE if write else _READ)]


def foreign_writers(path) -> list:
    """Comptes autres que moi (le système et les administrateurs mis à part) qui peuvent
    modifier ce fichier ou ce dossier, ou y créer et effacer : vide s'il est privé.
    FileNotFoundError s'il n'existe pas, OSError si ses droits sont illisibles."""
    return _others(path, write=True)


def foreign_readers(path) -> list:
    """De même pour la lecture (une clé privée)."""
    return _others(path, write=False)


def _create(path: Path):
    """Crée le dossier `path`, privé dès sa naissance. FileExistsError s'il existe."""
    if sys.platform != "win32":
        os.mkdir(path, 0o700)
        return
    ctypes, wintypes, advapi, kernel = _win()
    # Liste protégée (rien d'hérité du parent) : moi, SYSTEM, Administrateurs ; héritée par
    # tout ce qui sera créé dedans
    sddl = f"D:P(A;OICI;FA;;;{my_sid()})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise OSError(ctypes.get_last_error(), "liste d'accès privée impossible à préparer")

    class Attributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]
    try:
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error == 183:                      # ERROR_ALREADY_EXISTS
                raise FileExistsError(str(path))
            raise OSError(error, f"dossier impossible à créer : {path}")
    finally:
        kernel.LocalFree(descriptor)


def require_private(path, what: str):
    """NotPrivate si d'autres comptes peuvent modifier `path` (dit ce qu'il contient : `what`)."""
    try:
        others = foreign_writers(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise NotPrivate(f"Droits de {what} illisibles ({path}) : {exc}") from exc
    if others:
        raise NotPrivate(f"{what[:1].upper() + what[1:]} est modifiable par d'autres comptes "
                         f"de cette machine ({', '.join(others)}) : {path}")


def exposed(paths) -> list:
    """[(chemin, [comptes])] : ceux de `paths` que d'autres comptes peuvent modifier (droits
    illisibles : signalés aussi, par prudence). Un chemin absent est ignoré."""
    found = []
    for path in paths:
        try:
            others = foreign_writers(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            others = [f"droits illisibles ({exc})"]
        if others:
            found.append((Path(path), others))
    return found


def describe(found: list) -> str:
    """Les chemins exposés (exposed), un par ligne, avec qui peut y écrire."""
    return "\n".join(f"  - {path} : {', '.join(others)}" for path, others in found)


def private_dir(path) -> Path:
    """Le dossier de travail `path`, créé privé s'il manque (ses parents, eux, ordinaires) ;
    NotPrivate s'il existait déjà ouvert à d'autres comptes (on ne change pas ses droits)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _create(path)
    except FileExistsError:
        require_private(path, "le dossier de travail")
    return path


@contextmanager
def private_tempdir(prefix: str, base=None):
    """Dossier temporaire neuf et privé (sous `base`, défaut : le dossier temporaire),
    effacé à la sortie. Même si d'autres comptes peuvent écrire dans `base`, ils ne
    peuvent ni écrire dans celui-ci, ni le remplacer (nom tiré au hasard, créé d'un coup)."""
    base = Path(base or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        path = base / f"{prefix}{secrets.token_hex(6)}"
        try:
            _create(path)
            break
        except FileExistsError:
            continue
    else:
        raise OSError(f"Aucun dossier temporaire libre dans {base}")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ------------------------------------------------------- dépôt d'où l'on publie
# git lancé sans ce que .git/config ou le dépôt pourraient lui faire exécuter ou substituer :
# programme de suivi des fichiers (git status), crochets (git tag : reference-transaction),
# signature GPG, objets de remplacement (git archive les appliquerait)
GIT_SAFE = ["--no-replace-objects", "-c", "core.fsmonitor=false",
            "-c", f"core.hooksPath={os.devnull}", "-c", "tag.gpgSign=false"]
# Dossiers du dépôt que les outils ne lisent pas, trop longs à parcourir (données d'essai,
# sorties de construction, copies de travail de Claude Code) : seul le dossier lui-même est
# contrôlé, pas son contenu
NOT_WALKED = {"data", "build", "dist", ".pytest_cache", ".claude", "compagnon/target",
              "android/build", "android/.gradle"}
# Là où Python cherche les modules des outils : tout y est suivi par git (hors __pycache__,
# que l'amorce ne lit pas) ; à la racine, seul ce qui s'importe ou se lance compte (un
# git.exe ou un cargo.exe y passeraient devant ceux du PATH : le dossier courant d'abord)
IMPORTED = ("aiodos", "tools")
_RUNNABLE = (".py", ".pyc", ".pyd", ".pyw", ".so", ".exe", ".bat", ".cmd", ".com")
# Réglages de la configuration locale qui feraient lire ou exécuter à git ce qu'elle ne
# montre pas : un fichier inclus, un filtre ou une conversion (git archive les exécute), des
# attributs pris ailleurs, un autre arbre de travail (git verrait d'autres fichiers)
_GIT_REFUSED = re.compile(r"include\.path|includeif\..+\.path|filter\..+|diff\..+\.textconv"
                          r"|core\.attributesfile|core\.worktree", re.I)
# Neutralisés dans les outils (GIT_SAFE, sortie lue), mais lancés par le git de l'auteur :
# dans .git/config, le signe d'un dépôt piégé
_GIT_SUSPECT = re.compile(r"core\.hookspath|core\.sshcommand|core\.pager|core\.editor"
                          r"|sequence\.editor|diff\.external|gpg\.program|gpg\..+\.program",
                          re.I)
_BOOLEAN = {"true", "false", "yes", "no", "on", "off", "1", "0", ""}


def git_command(repo, *args) -> list:
    """La commande git sur le dépôt `repo`, sans ce que sa configuration ferait exécuter ni
    ses objets de remplacement (GIT_SAFE)."""
    return ["git", *GIT_SAFE, "-C", str(repo), *args]


def _git(repo, *args) -> str:
    done = subprocess.run(git_command(repo, *args), capture_output=True)
    if done.returncode != 0:
        raise NotPrivate(f"git {' '.join(args)} : "
                         f"{done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout.decode("utf-8", "replace")


def repository_paths(repo) -> list:
    """Ce que les outils de publication lisent ou font exécuter du dépôt : le dépôt entier
    (.git, code importé et __pycache__ compris), hors du contenu de NOT_WALKED. Un fichier
    qu'un autre compte y aurait créé un jour (il en reste propriétaire) serait signé ou
    livré, ou exécuté par l'outil."""
    repo = Path(repo)
    found = [repo]
    for folder, dirs, files in os.walk(repo):
        here = Path(folder)
        relative = here.relative_to(repo).as_posix()
        walked = []
        for name in dirs:
            found.append(here / name)
            if (name if relative == "." else f"{relative}/{name}") not in NOT_WALKED:
                walked.append(name)
        dirs[:] = walked
        found += [here / name for name in files]
    return found


def git_config_problems(repo) -> list:
    """Ce qui, dans la configuration git que les outils liront, échappe au contrôle du
    dépôt ou trahit un dépôt piégé (_GIT_REFUSED, _GIT_SUSPECT, un programme de suivi des
    fichiers autre que celui de git), et tout fichier de configuration (inclus, global,
    système) que d'autres comptes peuvent modifier."""
    fields = _git(repo, "config", "--list", "--show-origin", "--show-scope", "-z").split("\0")
    problems, origins = [], {}
    for scope, origin, entry in zip(fields[0::3], fields[1::3], fields[2::3]):
        key, _, value = entry.partition("\n")
        where = origin[5:] if origin.startswith("file:") else origin
        if scope in ("local", "worktree") and (
                _GIT_REFUSED.fullmatch(key) or _GIT_SUSPECT.fullmatch(key)
                or (key.lower() == "core.fsmonitor" and value.lower() not in _BOOLEAN)):
            problems.append(f"  - {key} ({where})")
        if origin.startswith("file:"):
            path = Path(origin[5:])
            origins.setdefault(str(path), path if path.is_absolute() else Path(repo) / path)
    found = exposed(origins.values())
    return problems + ([describe(found)] if found else [])


def _git_dir(repo) -> Path:
    found = Path(_git(repo, "rev-parse", "--absolute-git-dir").strip())
    return found if found.is_absolute() else Path(repo) / found


def git_state_problems(repo) -> list:
    """Ce que git ferait entrer dans une version sans que le dépôt le montre, ou qui
    s'exécuterait : objets de remplacement, attributs de .git/info, crochets actifs."""
    problems = []
    replaced = _git(repo, "for-each-ref", "--format=%(refname)", "refs/replace").split()
    if replaced:
        problems.append(f"  - objets de remplacement ({', '.join(replaced[:5])}) : git "
                        "archive sortirait d'autres fichiers que ceux du commit "
                        "(git replace -d …, ou git update-ref -d …)")
    git_dir = _git_dir(repo)
    try:
        lines = (git_dir / "info" / "attributes").read_text("utf-8", "replace").splitlines()
    except OSError:
        lines = []
    if any(line.strip() and not line.lstrip().startswith("#") for line in lines):
        problems.append(f"  - {git_dir / 'info' / 'attributes'} : des attributs (export-ignore, "
                        "export-subst, filtres) changeraient ce que git archive publie ; "
                        "vide-le")
    try:
        hooks = sorted(p.name for p in (git_dir / "hooks").iterdir()
                       if p.is_file() and not p.name.endswith(".sample"))
    except OSError:
        hooks = []
    if hooks:
        problems.append(f"  - crochets actifs dans {git_dir / 'hooks'} ({', '.join(hooks)}) : "
                        "vérifie-les et retire-les")
    return problems


def untracked_problems(repo) -> list:
    """Fichiers non suivis par git là où Python cherche les modules des outils : un module
    planté passerait devant le vrai (paquet du même nom, .pyd devant .py) et s'exécuterait
    sous le compte de l'auteur, la clé de publication à portée ; de même d'un programme
    posé à la racine. Les __pycache__ ne comptent pas : l'amorce des outils ne les lit
    pas."""
    inside = _git(repo, "ls-files", "--others", "--directory", "-z", "--", *IMPORTED)
    found = [p for p in inside.split("\0") if p and "__pycache__" not in p.split("/")]
    top = _git(repo, "ls-files", "--others", "--directory", "-z")
    for entry in (p for p in top.split("\0") if p):
        name = entry.rstrip("/")
        if "/" in name:
            continue
        if entry.endswith("/"):
            try:
                package = any(child.startswith("__init__.")
                              for child in os.listdir(Path(repo) / name))
            except OSError:
                package = False
            if package:
                found.append(entry)
        elif name.lower().endswith(_RUNNABLE):
            found.append(entry)
    shown = found[:10] + ([f"… et {len(found) - 10} autres"] if len(found) > 10 else [])
    return [f"  - {p}" for p in shown]


def repository_problems(repo, extra=()) -> list:
    """[message] : pourquoi les outils ne doivent rien signer ni livrer depuis `repo`
    (`extra` : dossiers de travail à contrôler aussi). Vide : rien à redire. Rien n'est
    jamais changé aux droits ni au dépôt : c'est à l'auteur de corriger. Un dépôt ouvert à
    d'autres comptes est refusé avant que git n'y soit lancé."""
    found = exposed([*repository_paths(repo), *extra])
    if found:
        shown = found[:20] + ([(f"… et {len(found) - 20} autres", ["idem"])]
                              if len(found) > 20 else [])
        return [
            "Le dépôt est modifiable par d'autres comptes de cette machine :\n"
            f"{describe(shown)}\n"
            "Ce qu'ils y auraient glissé (un réglage de .git/config, un crochet, du code) serait "
            "signé ou livré à toutes les machines. Rends ce dossier privé (toi, Système et "
            "Administrateurs seulement) ou déplace le dépôt dans ton dossier personnel, "
            "vérifie .git/config et .git/hooks, puis recommence."]
    messages = []
    problems = git_config_problems(repo)
    if problems:
        messages.append(
            "La configuration de git ferait lire ou exécuter ce que le dépôt ne montre pas :\n"
            + "\n".join(problems) + "\n"
            "Retire ces réglages de .git/config (git config --local --unset …), ou rends ces "
            "fichiers privés, puis recommence.")
    problems = git_state_problems(repo)
    if problems:
        messages.append("Le dépôt git contient de quoi publier autre chose que le commit :\n"
                        + "\n".join(problems))
    problems = untracked_problems(repo)
    if problems:
        messages.append(
            "Fichiers non suivis par git là où les outils cherchent leur code :\n"
            + "\n".join(problems) + "\n"
            "Un module planté là s'exécuterait avec la clé de publication à portée. Commite-les "
            "s'ils sont à toi, retire-les sinon, puis recommence.")
    return messages


def require_private_repository(repo, extra=()):
    """NotPrivate (message lisible) si les outils ne doivent rien signer ni livrer depuis
    `repo` (repository_problems)."""
    messages = repository_problems(repo, extra)
    if messages:
        raise NotPrivate("\n\n".join(messages))
