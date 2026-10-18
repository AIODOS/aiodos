"""Fichiers de modèles déjà sur ce PC (imaging/local_copies.py) : un fichier identique
trouvé dans le dossier de ComfyUI (que PANDORA utilise) ou dans un dossier du réglage est
repris au lieu d'être téléchargé, une fois son empreinte vérifiée."""
import os

import pytest

from aiodos import settings
from aiodos.imaging import catalog, download, local_copies
from tests.test_images import ready, tiny_model, wait_for  # noqa: F401  (tiny_model : fixture)


@pytest.fixture
def comfy(tmp_path, monkeypatch, tiny_model):
    """Le dossier de modèles de ComfyUI, sous un LOCALAPPDATA d'essai ; le modèle
    « essai » n'a plus aucun fichier dans le dossier d'AIODOS."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    root = tmp_path / "local" / "Comfy-Desktop" / "ComfyUI-Shared" / "models"
    for name in tiny_model["contents"]:
        (tiny_model["folder"] / name).unlink()
    # aucun téléchargement : un appel à Hugging Face fait échouer l'essai
    monkeypatch.setattr(download, "_resume", lambda spec, part: pytest.fail("téléchargement"))
    return root


def _put(folder, name, content):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(content)
    return folder / name


def test_files_found_in_comfyui_are_taken_without_downloading(comfy, tiny_model):
    for name, content in tiny_model["contents"].items():
        _put(comfy / ("vae" if "vae" in name else "diffusion_models"), name, content)
    found = {f["name"]: f["from"] for f in local_copies.available("essai")}
    assert found == {name: "ComfyUI" for name in tiny_model["contents"]}
    download.start("essai")
    ready()
    target = tiny_model["folder"] / "vae.gguf"
    assert target.read_bytes() == tiny_model["contents"]["vae.gguf"]
    assert os.stat(target).st_nlink == 2                    # un lien : aucune place en plus
    assert local_copies.available("essai") == []           # plus rien ne manque


def test_a_same_named_file_with_another_content_is_not_taken(comfy, tiny_model, monkeypatch):
    wrong = b"x" * len(tiny_model["contents"]["vae.gguf"])  # même taille, autre contenu
    _put(comfy / "vae", "vae.gguf", wrong)
    for name, content in tiny_model["contents"].items():
        if name != "vae.gguf":
            _put(comfy, name, content)

    def fetch(spec, part):                                  # seul le VAE se télécharge
        assert spec["name"] == "vae.gguf"
        part.write_bytes(tiny_model["contents"]["vae.gguf"])
    monkeypatch.setattr(download, "_resume", fetch)
    download.start("essai")
    ready()
    assert (comfy / "vae" / "vae.gguf").read_bytes() == wrong         # l'original intact
    assert (tiny_model["folder"] / "vae.gguf").read_bytes() == tiny_model["contents"]["vae.gguf"]


def test_folders_from_the_setting_are_searched_first(comfy, tiny_model, tmp_path):
    other = tmp_path / "Mes modèles, avec espaces"
    for name, content in tiny_model["contents"].items():
        _put(other / "sous-dossier", name, content)
    _put(comfy, "vae.gguf", tiny_model["contents"]["vae.gguf"])
    settings.set_value("images_other_dirs", f"{tmp_path / 'absent'}\n{other}")
    found = {f["name"]: f["from"] for f in local_copies.available("essai")}
    assert set(found.values()) == {str(other)}
    assert [name for _, name in local_copies.folders()][:2] == [str(other), "ComfyUI"]


def test_aiodos_own_folder_is_not_a_source(comfy, tiny_model):
    settings.set_value("images_other_dirs", str(tiny_model["folder"]))
    assert all(os.path.normcase(str(f.resolve())) != os.path.normcase(str(tiny_model["folder"].resolve()))
               for f, _ in local_copies.folders())
    assert catalog.state("essai")["state"] == "missing"


def test_a_different_copy_is_hashed_once_and_no_longer_offered(comfy, tiny_model, monkeypatch):
    spec = tiny_model["files"]["vae"]
    _put(comfy / "vae", spec["name"], b"x" * spec["size"])          # même nom, même taille
    assert [f["name"] for f in local_copies.available("essai")] == [spec["name"]]
    hashed = []
    real = catalog.sha256_of
    monkeypatch.setattr(catalog, "sha256_of", lambda path, stop=None: hashed.append(path) or real(path, stop))
    target = tiny_model["folder"] / spec["name"]
    assert local_copies.bring(spec, target) is None and not target.exists()
    assert local_copies.bring(spec, target) is None and len(hashed) == 1   # pas rehachée
    assert local_copies.available("essai") == []                            # plus proposée


def test_an_unreadable_copy_lets_the_download_go_on(comfy, tiny_model, monkeypatch):
    spec = tiny_model["files"]["vae"]
    _put(comfy / "vae", spec["name"], tiny_model["contents"][spec["name"]])

    def locked(*args, **kwargs):
        raise PermissionError("fichier verrouillé")
    monkeypatch.setattr(local_copies.os, "link", locked)
    monkeypatch.setattr(local_copies, "_copy", locked)
    target = tiny_model["folder"] / spec["name"]
    assert local_copies.bring(spec, target) is None                  # pas d'exception
    assert not target.with_name(target.name + ".part").exists()
