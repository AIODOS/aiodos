"""Publier une version d'AIODOS : la signer avec la clé de publication, puis la confier
au nœud de cette machine, qui la transmettra à ses machines appairées.

    python tools/publish.py key        crée la clé de publication (une seule fois)
    python tools/publish.py show       identifiant et clé publique (jamais la clé privée)
    python tools/publish.py release [--notes "…"] [--data-dir data] [--out dossier]
                                    [--targets windows-x86_64,android-aarch64]
                                    [--no-companion] [--no-engine]

Une version publiée, c'est le code du commit courant (git archive : rien d'autre que
ce qui est dans le dépôt), au numéro écrit dans aiodos/__init__.py ; une étiquette
v<numéro> marque le commit. S'y joint le compagnon Internet aiodos-net de chaque
plateforme, construit à partir de ce même commit et vérifié (companion_build.py) :
par défaut, celles qu'on sait construire ici ; une plateforme sans ses outils est
signalée, et la version part sans son compagnon ; une construction qui échoue arrête
tout, avant que rien ne soit publié. S'y joignent aussi les moteurs : llama.cpp des gros
modèles répartis et stable-diffusion.cpp des images, chacun l'archive officielle d'une
version épinglée (engine_release.py) ; s'il ne peut être téléchargé, la version part
sans lui. La clé privée reste hors du dépôt, dans
~/.aiodos/publication/ (ou là où pointe AIODOS_RELEASE_KEY) : voir LISEZMOI.txt à côté.

Ce qui est signé vient du dépôt et des dossiers de travail de l'outil : un autre compte de
la machine qui pourrait y écrire ferait signer son code. L'outil refuse donc de tourner
depuis un dépôt que d'autres comptes peuvent modifier ou qui porte des fichiers non suivis
là où il cherche son code, et ce avant d'en importer quoi que ce soit (_amorce), puis de
publier avec une clé qu'ils peuvent lire (il ne change jamais les droits lui-même) ; il
travaille dans un dossier temporaire privé, et lance git sans ce que .git/config ou des
objets de remplacement pourraient lui faire exécuter ou publier (private.GIT_SAFE).

Le manifeste signé nomme les appareils d'approbation du commit publié (« approvers » :
APPROVER_KEYS de son keys.py) : toute machine qui le relaie sait ainsi quelles approbations
garder, même sans en déclarer elle-même (manifest.canonical).
"""
# os et sys sont gelés dans Python : jamais pris dans le dépôt, même avant le contrôle
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _amorce(root: str):
    """Lancé comme outil : le dépôt est contrôlé (private.require_private_repository) avant
    que rien n'en soit importé. Ni un module ni un bytecode plantés ne passent : le dossier
    du dépôt et celui des outils sont retirés de sys.path le temps du contrôle, private.py
    est chargé par son chemin, et aucun __pycache__ n'est lu ni écrit (pycache_prefix vers
    un dossier qui n'existe pas). Le contrôle refuse ensuite tout fichier non suivi là où
    les imports qui suivent chercheront."""
    sys.dont_write_bytecode = True
    sys.pycache_prefix = os.path.join(os.path.expanduser("~"), ".aiodos",
                                      f"pycache-{os.urandom(16).hex()}")
    mine = {os.path.normcase(os.path.realpath(p)) for p in (root, os.path.join(root, "tools"))}
    saved = list(sys.path)
    sys.path[:] = [p for p in sys.path if p and os.path.normcase(os.path.realpath(p)) not in mine]
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aiodos_amorce_private", os.path.join(root, "aiodos", "updates", "private.py"))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        try:
            guard.require_private_repository(root)
        except guard.NotPrivate as exc:
            for stream in (sys.stdout, sys.stderr):
                stream.reconfigure(encoding="utf-8", errors="replace")
            sys.exit(f"{exc}\nRien n'a été fait.")
    finally:
        sys.path[:] = saved
    # Le dossier courant n'est pas forcément le dépôt contrôlé ici : Windows y cherche un
    # programme (git.exe…) avant le PATH, et « python -m » y prend ses modules (pip.py).
    # Jamais un programme du dossier courant, et les « python -m » des outils partent du
    # dépôt (cwd=ROOT ; relecture finale du 21/10/2026)
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"


if __name__ == "__main__":
    _amorce(os.path.dirname(_HERE))

import argparse  # noqa: E402
import base64  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import companion_build  # noqa: E402
import engine_release  # noqa: E402
from aiodos.updates import manifest as manifests  # noqa: E402
from aiodos.updates import platforms  # noqa: E402
from aiodos.updates import private as privacy  # noqa: E402
from aiodos.updates import version as versions  # noqa: E402
from aiodos.updates.store import Store, sha256_of  # noqa: E402

# Ce qu'une machine fait tourner : ni les tests, ni la documentation, ni les sources du
# compagnon (il est joint à part, construit pour chaque plateforme)
RUNTIME = ["aiodos", "assets", "lancer.bat", "lancer-noeud.sh", "requirements.txt",
           "requirements-node.txt", "LICENSE", "NOTICE", "README.md"]
MIN_PYTHON = "3.11"
KEYS_FILE = ROOT / "aiodos" / "updates" / "keys.py"

README = """Clé de publication d'AIODOS
===========================

cle-de-publication.pem signe les versions d'AIODOS : une machine AIODOS n'installe
une mise à jour que si elle est signée par cette clé.

- Ne la donne à personne et ne la mets dans aucun dépôt : elle permet d'installer
  du code sur toutes tes machines et sur celles de tes amis.
- Sauvegarde-la à part (clé USB rangée ailleurs, gestionnaire de mots de passe) :
  si elle est perdue, les machines déjà installées ne pourront plus recevoir de
  mise à jour, il faudrait les réinstaller à la main.
- Si quelqu'un a pu la copier : crée une nouvelle clé, publie avec l'ancienne une
  version qui fait aussi confiance à la nouvelle (aiodos/updates/keys.py), puis
  retire l'ancienne dans la version suivante, signée par la nouvelle.
"""


def key_path() -> Path:
    return Path(os.environ.get("AIODOS_RELEASE_KEY")
                or Path.home() / ".aiodos" / "publication" / "cle-de-publication.pem")


def load_key() -> Ed25519PrivateKey:
    path = key_path()
    try:
        private = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except FileNotFoundError:
        sys.exit(f"Pas de clé de publication ({path}) : python tools/publish.py key")
    if not isinstance(private, Ed25519PrivateKey):
        sys.exit(f"{path} n'est pas une clé Ed25519.")
    return private


def public_text(private) -> tuple:
    raw = manifests.raw_public(private)
    return manifests.key_id(raw), base64.b64encode(raw).decode("ascii")


def git(*args) -> str:
    done = subprocess.run(companion_build.git_command(ROOT, *args), capture_output=True,
                          text=True, encoding="utf-8")
    if done.returncode != 0:
        sys.exit(f"git {' '.join(args)} : {done.stderr.strip()}")
    return done.stdout.strip()


def require_private_setup(key: Path):
    """Refuse de publier si d'autres comptes de cette machine peuvent modifier le dépôt,
    ou lire la clé de publication (rien n'est changé à leurs droits)."""
    try:
        companion_build.require_private_repository(ROOT)
    except companion_build.BuildError as exc:
        sys.exit(f"{exc}\nRien n'a été publié.")
    try:
        readers = privacy.foreign_readers(key)
    except FileNotFoundError:
        return                                  # dit par load_key
    except OSError as exc:
        sys.exit(f"Droits de la clé de publication illisibles ({key}) : {exc}")
    if readers:
        sys.exit(f"La clé de publication est lisible par d'autres comptes de cette machine "
                 f"({', '.join(readers)}) : {key}. Range-la dans un dossier à toi seul ; si "
                 "quelqu'un a pu la copier, crée-en une nouvelle (voir LISEZMOI.txt). Rien "
                 "n'a été publié.")


def committed_approvers(text: str) -> dict:
    """APPROVER_KEYS du keys.py publié (`text`, lu dans le commit, sans l'exécuter) : {}
    s'il n'en déclare pas. SystemExit s'il est illisible : la version nommerait de mauvais
    appareils."""
    import ast
    try:
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "APPROVER_KEYS" for t in node.targets):
                found = ast.literal_eval(node.value)
                break
        else:
            return {}
    except (SyntaxError, ValueError) as exc:
        sys.exit(f"APPROVER_KEYS illisible dans aiodos/updates/keys.py ({exc}).")
    if not isinstance(found, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in found.items()):
        sys.exit("APPROVER_KEYS, dans aiodos/updates/keys.py, doit associer des identifiants à "
                 "des clés publiques (texte).")
    listed = manifests.listed_approvers(json.dumps({"approvers": found}))
    if listed != found:
        sys.exit("APPROVER_KEYS, dans aiodos/updates/keys.py : un identifiant ne correspond pas "
                 "à sa clé publique (recopie la ligne que donne la vue Mises à jour), ou plus de "
                 f"{manifests.MAX_APPROVALS} appareils.")
    return found


def trust(kid: str, public: str):
    """Ajoute la clé publique à aiodos/updates/keys.py (à enregistrer par un commit)."""
    text = KEYS_FILE.read_text(encoding="utf-8")
    if kid in text:
        return
    start = text.index("TRUSTED_KEYS = {\n") + len("TRUSTED_KEYS = {\n")
    text = text[:start] + f'    "{kid}": "{public}",\n' + text[start:]
    KEYS_FILE.write_bytes(text.encode("utf-8"))


def cmd_key(args):
    path = key_path()
    if path.exists():
        sys.exit(f"Une clé de publication existe déjà : {path} (rien n'a changé).")
    try:                                   # un dossier à soi seul (créé tel s'il manque)
        privacy.private_dir(path.parent)
        readers = privacy.foreign_readers(path.parent)
    except (privacy.NotPrivate, OSError) as exc:
        sys.exit(f"{exc}\nLa clé de publication doit naître dans un dossier à toi seul.")
    if readers:
        sys.exit(f"Le dossier {path.parent} est lisible par d'autres comptes de cette machine "
                 f"({', '.join(readers)}) : la clé de publication doit naître dans un dossier "
                 "à toi seul.")
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(pem)
    (path.parent / "LISEZMOI.txt").write_text(README, encoding="utf-8")
    kid, public = public_text(private)
    trust(kid, public)
    print(f"Clé de publication créée : {path}")
    print(f"Identifiant {kid}, ajouté aux clés de confiance (aiodos/updates/keys.py).")
    print("Sauvegarde ce fichier à part et ne le donne à personne (voir LISEZMOI.txt).")


def cmd_show(args):
    kid, public = public_text(load_key())
    print(f"Clé de publication : {key_path()}")
    print(f"Identifiant : {kid}")
    print(f"Clé publique : {public}")
    print("Dans la version courante : " + ("oui" if kid in KEYS_FILE.read_text(encoding="utf-8")
                                            else "NON (python tools/publish.py key l'ajoute)"))


def companion_targets(args, commit: str) -> tuple:
    """(plateformes à construire, [(plateforme, raison)] laissées de côté)."""
    if args.no_companion:
        return [], []
    if not companion_build.has_sources(ROOT, commit):
        if args.targets:
            sys.exit("Le commit à publier n'a pas de dossier compagnon/ : pas de compagnon "
                     "à construire (--no-companion pour publier sans).")
        return [], [("toutes", "le commit n'a pas de dossier compagnon/")]
    if args.targets:
        wanted = [t.strip() for t in args.targets.split(",") if t.strip()]
        for target in wanted:
            reason = companion_build.missing(target)
            if reason:
                sys.exit(f"Compagnon {target} impossible à construire ici : {reason}.")
        return wanted, []
    targets, skipped = [], []
    for target in companion_build.default_targets():
        reason = companion_build.missing(target)
        if reason:
            skipped.append((target, reason))
        else:
            targets.append(target)
    return targets, skipped


def cmd_release(args):
    require_private_setup(key_path())
    private = load_key()
    kid, public = public_text(private)
    commit = git("rev-parse", "HEAD")
    declared = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""",
                         git("show", f"{commit}:aiodos/__init__.py"), re.M)
    number = declared.group(1) if declared else None
    if not versions.valid(number):
        sys.exit(f"Numéro de version illisible dans aiodos/__init__.py : {number!r}")
    if "-dev" in number and not args.dev:
        sys.exit(f"{number} est une version de développement : écris le numéro de la version "
                 "(ex. 1.0.1) dans aiodos/__init__.py et fais un commit, ou passe --dev.")
    committed_keys = git("show", f"{commit}:aiodos/updates/keys.py")
    if kid not in committed_keys:
        sys.exit("Le commit à publier ne fait pas confiance à cette clé : sa clé publique doit "
                 "figurer dans aiodos/updates/keys.py (python tools/publish.py key l'y met), "
                 "enregistrée par un commit.")
    changed = git("status", "--porcelain", "--untracked-files=no", "--", *RUNTIME)
    if changed:
        print("Attention, ces modifications ne sont pas dans un commit : elles ne seront pas "
              "publiées.\n" + changed)
    if args.data_dir:
        os.environ["AIODOS_DATA_DIR"] = str(Path(args.data_dir).resolve())
    store = Store()
    if store.signed(number):
        sys.exit(f"La version {number} est déjà publiée ici : change de numéro.")
    trusted = {kid: public}
    approvers = committed_approvers(committed_keys)
    targets, skipped = companion_targets(args, commit)
    # Dossier de travail privé : %TEMP% peut être ouvert à d'autres comptes (bacs à sable),
    # qui remplaceraient l'archive entre sa création et son empreinte
    with privacy.private_tempdir("aiodos-publication-") as work:
        archive = Path(work) / f"aiodos-{number}.zip"
        git("archive", "--format=zip", f"--output={archive}", commit, "--", *RUNTIME)
        files = [(archive, {"kind": manifests.SOURCE, "name": archive.name,
                            "size": archive.stat().st_size, "sha256": sha256_of(archive)})]
        if targets:
            print(f"Construction du compagnon à partir du commit {commit[:10]} : "
                  f"{', '.join(targets)} (plusieurs minutes)…", flush=True)
        for target in targets:
            try:
                built = companion_build.build(target, commit, number, Path(work) / "compagnon")
            except companion_build.BuildError as exc:
                sys.exit(f"Compagnon {target} : {exc}\nRien n'a été publié.")
            files.append((built, {"kind": platforms.kind(manifests.COMPANION, target),
                                  "name": built.name, "size": built.stat().st_size,
                                  "sha256": sha256_of(built)}))
        skipped_engines = []
        for name in ([] if args.no_engine else engine_release.ENGINES):
            for where in engine_release.targets(name):
                try:
                    engine = engine_release.fetch(where, engine=name)
                except engine_release.EngineError as exc:
                    skipped_engines.append((name, where, str(exc)))
                    continue
                files.append((engine, {"kind": platforms.kind(name, where),
                                       "name": engine.name, "size": engine.stat().st_size,
                                       "sha256": sha256_of(engine)}))
        signed = manifests.sign({
            "format": manifests.FORMAT, "app": manifests.APP, "version": number,
            "published": int(time.time()), "commit": commit, "min_python": MIN_PYTHON,
            "notes": args.notes or "", "approvers": approvers,
            "files": [item for _, item in files]}, private)
        manifests.verify(signed, trusted, approvers={})   # l'approbation viendra après
        for path, item in files:                        # le manifeste en dernier : le paquet
            store.save_file(number, item, path)         # n'est proposé qu'une fois complet
        store.add_manifest(signed, trusted, approvers={})
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            for path, _ in files:
                shutil.copyfile(path, out / path.name)
            (out / f"aiodos-{number}.manifest.json").write_bytes(
                (store.root / "packages" / number / "manifest.json").read_bytes())
    tag = f"v{number}"
    if not git("tag", "--list", tag):
        git("tag", "-a", tag, "-m", f"AIODOS {number}", commit)
    print(f"Version {number} publiée (commit {commit[:10]}, étiquette {tag}, "
          f"{files[0][1]['size'] / 1e6:.1f} Mo) dans {store.root}.")
    for path, item in files[1:]:
        what = "Moteur" if item["kind"].startswith(manifests.ENGINES) else "Compagnon"
        print(f"{what} joint : {item['kind']} ({item['size'] / 1e6:.1f} Mo)")
    for target, reason in skipped:
        print(f"ATTENTION : compagnon non joint ({target}) : {reason}. Les machines de cette "
              "plateforme garderont le compagnon qu'elles ont déjà.")
    for name, where, reason in skipped_engines:
        print(f"ATTENTION : moteur {name} non joint ({where}) : {reason}. Les machines de "
              "cette plateforme garderont celui qu'elles ont déjà.")
    if approvers:
        print(f"EN ATTENTE D'APPROBATION : ouvre la vue Mises à jour de ton appareil "
              f"d'approbation (ton téléphone) et approuve la version {number}. D'ici là, elle "
              "n'est proposée à personne.")
        # Ce que montre l'appareil d'approbation (updates/approval.digest) : sans elle, un
        # voleur de la clé de publication pourrait faire approuver son propre texte sous ce
        # numéro et ce commit, qu'il recopie (relecture finale du 21/10/2026)
        print(f"Empreinte du texte : {text_digest(signed)[:16]}… : vérifie qu'elle est la "
              "même sur ton appareil d'approbation avant d'approuver.")
    else:
        print("Le nœud de cette machine la propose à ses machines appairées, qui se la passent.")
    if args.out:
        print(f"Copie pour une installation à la main : {Path(args.out) / archive.name}")


def text_digest(signed: dict) -> str:
    """Empreinte du texte signé du manifeste, celle que montre l'appareil d'approbation
    (aiodos/updates/approval.digest) à côté de la version à approuver."""
    return hashlib.sha256(signed["manifest"].encode("utf-8")).hexdigest()


def main():
    for stream in (sys.stdout, sys.stderr):    # accents lisibles même dans un tube (Git Bash)
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="publish", description="Publier une version d'AIODOS")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("key", help="créer la clé de publication").set_defaults(run=cmd_key)
    commands.add_parser("show", help="afficher la clé publique").set_defaults(run=cmd_show)
    release = commands.add_parser("release", help="publier le commit courant")
    release.add_argument("--notes", default="", help="ce qui change, montré aux utilisateurs")
    release.add_argument("--data-dir", default=None, help="dossier de données du nœud (défaut : data)")
    release.add_argument("--out", default=None,
                         help="copier aussi l'archive, les compagnons et le manifeste ici")
    release.add_argument("--dev", action="store_true", help="accepter un numéro « -dev » (essais)")
    release.add_argument("--targets", default=None, metavar="PLATEFORMES",
                         help="compagnons à joindre, séparés par des virgules (défaut : ceux "
                              f"qu'on sait construire ici parmi {', '.join(companion_build.default_targets())})")
    release.add_argument("--no-companion", action="store_true",
                         help="publier sans le compagnon Internet")
    release.add_argument("--no-engine", action="store_true",
                         help="publier sans le moteur llama.cpp des gros modèles répartis")
    release.set_defaults(run=cmd_release)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
