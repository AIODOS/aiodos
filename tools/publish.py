"""Publier une version d'AIODOS : la signer avec la clé de publication, puis la confier
au nœud de cette machine, qui la transmettra à ses machines appairées.

    python tools/publish.py key        crée la clé de publication (une seule fois)
    python tools/publish.py show       identifiant et clé publique (jamais la clé privée)
    python tools/publish.py release [--notes "…"] [--data-dir data] [--out dossier]

Une version publiée, c'est le code du commit courant (git archive : rien d'autre que
ce qui est dans le dépôt), au numéro écrit dans aiodos/__init__.py ; une étiquette
v<numéro> marque le commit. La clé privée reste hors du dépôt, dans
~/.aiodos/publication/ (ou là où pointe AIODOS_RELEASE_KEY) : voir LISEZMOI.txt à côté.
"""
import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from aiodos.updates import manifest as manifests  # noqa: E402
from aiodos.updates import version as versions  # noqa: E402
from aiodos.updates.store import Store, sha256_of  # noqa: E402

# Ce qu'une machine fait tourner : ni les tests, ni la documentation, ni le compagnon à compiler
RUNTIME = ["aiodos", "assets", "lancer.bat", "lancer-noeud.sh", "requirements.txt",
           "requirements-node.txt", "LICENSE", "README.md"]
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
    done = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          encoding="utf-8")
    if done.returncode != 0:
        sys.exit(f"git {' '.join(args)} : {done.stderr.strip()}")
    return done.stdout.strip()


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
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True)
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


def cmd_release(args):
    private = load_key()
    kid, public = public_text(private)
    commit = git("rev-parse", "HEAD")
    declared = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""",
                         git("show", "HEAD:aiodos/__init__.py"), re.M)
    number = declared.group(1) if declared else None
    if not versions.valid(number):
        sys.exit(f"Numéro de version illisible dans aiodos/__init__.py : {number!r}")
    if "-dev" in number and not args.dev:
        sys.exit(f"{number} est une version de développement : écris le numéro de la version "
                 "(ex. 2.0.0) dans aiodos/__init__.py et fais un commit, ou passe --dev.")
    if kid not in git("show", "HEAD:aiodos/updates/keys.py"):
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
    with tempfile.TemporaryDirectory(prefix="aiodos-publication-") as work:
        archive = Path(work) / f"aiodos-{number}.zip"
        git("archive", "--format=zip", f"--output={archive}", commit, "--", *RUNTIME)
        item = {"kind": manifests.SOURCE, "name": archive.name,
                "size": archive.stat().st_size, "sha256": sha256_of(archive)}
        signed = manifests.sign({
            "format": manifests.FORMAT, "app": manifests.APP, "version": number,
            "published": int(time.time()), "commit": commit, "min_python": MIN_PYTHON,
            "notes": args.notes or "", "files": [item]}, private)
        manifests.verify(signed, trusted)
        store.save_file(number, item, archive)          # le manifeste en dernier : le paquet
        store.add_manifest(signed, trusted)             # n'est proposé qu'une fois complet
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, out / archive.name)
            (out / f"aiodos-{number}.manifest.json").write_bytes(
                (store.root / "packages" / number / "manifest.json").read_bytes())
    tag = f"v{number}"
    if not git("tag", "--list", tag):
        git("tag", "-a", tag, "-m", f"AIODOS {number}", commit)
    print(f"Version {number} publiée (commit {commit[:10]}, étiquette {tag}, "
          f"{item['size'] / 1e6:.1f} Mo) dans {store.root}.")
    print("Le nœud de cette machine la propose à ses machines appairées, qui se la passent.")
    if args.out:
        print(f"Copie pour une installation à la main : {Path(args.out) / archive.name}")


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
    release.add_argument("--out", default=None, help="copier aussi l'archive et son manifeste ici")
    release.add_argument("--dev", action="store_true", help="accepter un numéro « -dev » (essais)")
    release.set_defaults(run=cmd_release)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
