"""Outils des tests de mise à jour : clé de publication jetable, faux code d'AIODOS,
paquets signés."""
import base64
import json
import os
import time
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aiodos.updates import manifest as manifests
from aiodos.updates.store import MARKER, Store, sha256_of

# Faux AIODOS : ce que fait la version une fois lancée (cf. launcher.py)
FAKE_MAIN = '''import json, os, pathlib, sys, time

if __name__ == "__main__":
    data = pathlib.Path(os.environ["AIODOS_DATA_DIR"])
    with open(data / "runs.txt", "a", encoding="utf-8") as f:
        f.write({version!r} + " " + " ".join(sys.argv[1:]) + "\\n")
    behavior = {behavior!r}
    if behavior == "crash":
        sys.exit(1)
    if behavior == "hang":
        time.sleep(60)
    ready = os.environ.get("AIODOS_UPDATE_READY")
    if ready:
        pathlib.Path(ready).write_text({version!r})
    if behavior == "restart":          # le service a installé 2.0.2 pendant qu'elle tournait
        path = data / "updates" / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["pending"] = {{"version": "2.0.2", "attempts": 0, "previous": {version!r}}}
        path.write_text(json.dumps(state), encoding="utf-8")
        sys.exit(75)
    sys.exit(0)
'''


def new_key():
    """(clé privée, clés de confiance) jetables."""
    private = Ed25519PrivateKey.generate()
    raw = manifests.raw_public(private)
    return private, {manifests.key_id(raw): base64.b64encode(raw).decode("ascii")}


def fake_code(folder: Path, version: str, behavior: str = "healthy", server: str = "") -> Path:
    """Un faux paquet aiodos : assez pour être essayé à blanc et lancé."""
    package = folder / "aiodos"
    (package / "api").mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (package / "__main__.py").write_text(FAKE_MAIN.format(version=version, behavior=behavior),
                                         encoding="utf-8")
    (package / "api" / "__init__.py").write_text("", encoding="utf-8")
    (package / "api" / "server.py").write_text(server, encoding="utf-8")
    return folder


def installed_fake(store: Store, version: str, behavior: str = "healthy") -> Path:
    """Une version déjà installée dans versions/ (sans passer par install.py)."""
    folder = fake_code(store.version_dir(version), version, behavior)
    (folder / MARKER).write_text(json.dumps({"version": version}), encoding="utf-8")
    return folder


def zip_folder(folder: Path, archive: Path) -> Path:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(folder).as_posix())
    return archive


def publish(store: Store, archive: Path, version: str, private, trusted: dict,
            companions: dict | None = None, engines: dict | None = None, approvers=None,
            image_engines: dict | None = None, **extra) -> dict:
    """Range dans `store` un paquet signé (comme tools/publish.py) ; le manifeste lu.
    companions : {plateforme : chemin du programme} à joindre (aiodos-net-<plateforme>) ;
    engines : {plateforme : archive} du moteur llama.cpp (llama.cpp-<plateforme>) ;
    image_engines : de même pour stable-diffusion.cpp (stable-diffusion.cpp-<plateforme>) ;
    approvers={} : gardé sans approbation (version qui attend la sienne)."""
    files = [(archive, {"kind": "source", "name": f"aiodos-{version}.zip",
                        "size": archive.stat().st_size, "sha256": sha256_of(archive)})]
    for where, path in (companions or {}).items():
        files.append((path, {"kind": f"aiodos-net-{where}", "name": f"aiodos-net-{version}-{where}",
                             "size": path.stat().st_size, "sha256": sha256_of(path)}))
    for engine, table in (("llama.cpp", engines), ("stable-diffusion.cpp", image_engines)):
        for where, path in (table or {}).items():
            files.append((path, {"kind": f"{engine}-{where}", "name": path.name,
                                 "size": path.stat().st_size, "sha256": sha256_of(path)}))
    data = {"format": 1, "app": "aiodos", "version": version, "published": int(time.time()),
            "commit": "0" * 40, "min_python": "3.11", "notes": "",
            "files": [item for _, item in files], **extra}
    signed = manifests.sign(data, private)
    for path, item in files:
        store.save_file(version, item, path)
    return store.add_manifest(signed, trusted, approvers)


def fake_program(path: Path, content: bytes = b"") -> Path:
    """Un faux compagnon (des octets au hasard, et `content`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(2000) + content)
    return path
