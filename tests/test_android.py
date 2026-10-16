"""Android (Termux) : reconnaître le système, nommer le nœud d'après le téléphone
(son nom de machine vaut « localhost »). Propriétés du système simulées."""
import pytest

from aiodos import settings
from aiodos.node import android

PIXEL = {"ro.product.manufacturer": "Google", "ro.product.model": "Pixel 7",
         "ro.build.version.release": "14"}


@pytest.fixture
def phone(monkeypatch):
    def use(props):
        monkeypatch.setattr(android, "prop", lambda name: props.get(name, ""))
        android.model.cache_clear()
        android.system.cache_clear()
    yield use
    android.model.cache_clear()
    android.system.cache_clear()


def test_android_is_recognised_from_termux_environment(monkeypatch):
    for name in ("ANDROID_ROOT", "TERMUX_VERSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(android.sys, "platform", "linux")
    assert android.is_android() is hasattr(android.sys, "getandroidapilevel")
    monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
    assert android.is_android()


@pytest.mark.parametrize("maker, model, expected", [
    ("Google", "Pixel 7", "Google Pixel 7"),
    ("samsung", "SM-A546B", "Samsung SM-A546B"),
    ("OnePlus", "OnePlus 11", "OnePlus 11"),            # marque déjà dans le modèle
    ("", "", ""),
])
def test_phone_model(phone, maker, model, expected):
    phone({"ro.product.manufacturer": maker, "ro.product.model": model})
    assert android.model() == expected


def test_android_version_for_the_profile(phone):
    phone(PIXEL)
    assert android.system() == "Android 14"


def test_phone_node_is_named_after_its_model(phone, monkeypatch):
    phone(PIXEL)
    monkeypatch.setattr(settings.socket, "gethostname", lambda: "localhost")
    monkeypatch.setattr(android, "is_android", lambda: True)
    assert settings.get("node_name") == "Google Pixel 7"
    assert settings.get_all()["node_name"] == "Google Pixel 7"
    settings.set_value("node_name", "Téléphone de Léa")              # renommé par l'utilisateur
    assert settings.get("node_name") == "Téléphone de Léa"


def test_pc_keeps_its_machine_name(monkeypatch):
    monkeypatch.setattr(settings.socket, "gethostname", lambda: "localhost")
    monkeypatch.setattr(android, "is_android", lambda: False)
    assert settings.machine_name() == "localhost"


def test_without_getprop_properties_are_empty(monkeypatch):
    monkeypatch.setattr(android.os.path, "exists", lambda path: False)
    monkeypatch.setattr(android.shutil, "which", lambda name: None)
    assert android.prop("ro.product.model") == ""
