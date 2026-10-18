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


def test_wifi_asks_termux_first_and_never_counts_a_hotspot(monkeypatch):
    """Depuis Android 10, /sys/class/net n'est plus lisible par une appli : Termux:API
    d'abord ; à défaut, « wlan0 » seulement (wlan1 ou swlan0 servent au partage de
    connexion, qui passe par les données mobiles)."""
    calls = []
    monkeypatch.setattr(android, "_wifi_termux", lambda: calls.append("termux") or True)
    monkeypatch.setattr(android, "_wifi_interface", lambda: calls.append("wlan0") or False)
    android._wifi.update(at=None, value=None)
    assert android.on_wifi() is True and calls == ["termux"]
    android._wifi.update(at=None, value=None)
    monkeypatch.setattr(android, "_wifi_termux", lambda: None)
    assert android.on_wifi() is False
    android._wifi.update(at=None, value=None)
