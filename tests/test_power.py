"""Alimentation hors Windows : faux /sys/class/power_supply (Linux, Android) et
fausse commande termux-battery-status (Termux:API). Aucun vrai téléphone ici."""
import json
import os
import subprocess

import pytest

from aiodos.node import identity, power

# Sortie de termux-battery-status (application Termux:API) sur un téléphone en charge
TERMUX_CHARGING = {
    "present": True, "technology": "Li-ion", "health": "GOOD", "plugged": "PLUGGED_AC",
    "status": "CHARGING", "temperature": 30.700000762939453, "voltage": 4290,
    "current": 1200000, "current_average": None, "percentage": 84, "level": 84,
    "scale": 100, "charge_counter": 3500000, "energy": None, "cycle": 120,
}


def supply(root, name, **attrs):
    """Un dossier de /sys/class/power_supply, un fichier par attribut."""
    folder = root / name
    folder.mkdir(parents=True)
    for key, value in attrs.items():
        (folder / key).write_text(f"{value}\n", encoding="ascii")
    return folder


@pytest.fixture
def sys_root(tmp_path):
    root = tmp_path / "power_supply"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setitem(power._termux, "until", 0.0)
    monkeypatch.setitem(power._termux, "value", None)
    monkeypatch.setitem(power._termux, "good_at", 0.0)


@pytest.fixture
def termux(monkeypatch):
    """termux-battery-status simulée : sortie JSON (ou None) et nombre d'appels."""
    fake = {"output": None, "calls": 0}

    def run():
        fake["calls"] += 1
        return fake["output"]
    monkeypatch.setattr(power, "_run_termux", run)
    return fake


# ---------------------------------------------------------------- téléphone
def test_phone_charging_over_usb(sys_root):
    supply(sys_root, "battery", type="Battery", status="Charging", capacity=84, temp=312,
           present=1, health="Good")
    supply(sys_root, "usb", type="USB", online=1)
    supply(sys_root, "ac", type="Mains", online=0)
    assert power._linux(sys_root, mobile=True) == {
        "plugged": True, "level": 84, "temperature": 31.2, "mobile": True, "source": "sysfs"}


def test_phone_unplugged(sys_root):
    supply(sys_root, "battery", type="Battery", status="Discharging", capacity=63, temp=287)
    supply(sys_root, "usb", type="USB", online=0)
    supply(sys_root, "wireless", type="Wireless", online=0)
    status = power._linux(sys_root, mobile=True)
    assert status["plugged"] is False and status["level"] == 63


def test_old_kernel_charger_types_count_as_chargers(sys_root):
    supply(sys_root, "battery", type="Battery", status="Unknown", capacity=90, temp=300)
    supply(sys_root, "usb_pd", type="USB_PD", online=1)
    assert power._linux(sys_root, mobile=True)["plugged"] is True


def test_battery_status_alone_tells_if_the_phone_is_plugged(sys_root):
    supply(sys_root, "battery", type="Battery", status="Full", capacity=100, temp=301)
    assert power._linux(sys_root, mobile=True)["plugged"] is True


def test_closed_sysfs_falls_back_to_termux_api(sys_root, termux):
    # SELinux : les dossiers sont listés, mais rien n'est lisible
    (sys_root / "battery").mkdir()
    (sys_root / "usb").mkdir()
    termux["output"] = TERMUX_CHARGING
    assert power._linux(sys_root, mobile=True) == {
        "plugged": True, "level": 84, "temperature": 30.7, "mobile": True,
        "source": "termux-api"}


def test_missing_temperature_is_completed_by_termux_api(sys_root, termux):
    supply(sys_root, "battery", type="Battery", status="Charging", capacity=84)
    termux["output"] = TERMUX_CHARGING
    assert power._linux(sys_root, mobile=True)["temperature"] == 30.7
    termux["output"] = None                                # Termux:API muet depuis toujours
    power._termux.update(until=0.0, value=None, good_at=0.0)
    partial = power._linux(sys_root, mobile=True)          # mieux que rien
    assert (partial["plugged"], partial["level"], partial["temperature"]) == (True, 84, None)


def test_phone_with_nothing_readable_is_unknown_not_on_mains(tmp_path, termux):
    status = power._linux(tmp_path / "absent", mobile=True)
    assert status["plugged"] is None and status["mobile"] is True
    empty = tmp_path / "vide"
    empty.mkdir()                                     # dossier vide : pas « de bureau »
    power._termux["until"] = 0.0
    assert power._linux(empty, mobile=True)["plugged"] is None


# --------------------------------------------------------------- Linux (PC)
def test_laptop_on_mains_ignores_the_mouse_battery(sys_root):
    supply(sys_root, "BAT0", type="Battery", status="Not charging", capacity=95,
           present=1, scope="System")
    supply(sys_root, "AC", type="Mains", online=1)
    supply(sys_root, "hidpp_battery_0", type="Battery", status="Discharging",
           capacity=10, scope="Device")
    assert power._linux(sys_root, mobile=False) == {
        "plugged": True, "level": 95, "temperature": None, "mobile": False, "source": "sysfs"}


def test_laptop_on_battery(sys_root):
    supply(sys_root, "BAT0", type="Battery", status="Discharging", capacity=60)
    supply(sys_root, "AC", type="Mains", online=0)
    status = power._linux(sys_root, mobile=False)
    assert status["plugged"] is False and status["level"] == 60


def test_removed_battery_is_ignored(sys_root):
    supply(sys_root, "BAT1", type="Battery", status="Discharging", capacity=5, present=0)
    supply(sys_root, "AC", type="Mains", online=1)
    status = power._linux(sys_root, mobile=False)
    assert status["plugged"] is True and status["level"] is None


def test_desktop_without_battery_is_on_mains(sys_root, termux):
    assert power._linux(sys_root, mobile=False)["plugged"] is True
    supply(sys_root, "hid-mouse-battery", type="Battery", scope="Device", capacity=40)
    assert power._linux(sys_root, mobile=False)["plugged"] is True
    assert termux["calls"] == 0                  # Termux:API : seulement sur Android


def test_no_sysfs_outside_android_is_unknown(tmp_path):
    assert power._linux(tmp_path / "absent", mobile=False) == power._status()


def test_memory_is_read_from_proc_meminfo(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        7854320 kB\nMemFree:          123456 kB\n"
                       "MemAvailable:    3927160 kB\n", encoding="ascii")
    assert identity._meminfo(meminfo) == {"total": 7854320 * 1024,
                                          "available": 3927160 * 1024}
    assert identity._meminfo(tmp_path / "absent") == {"total": 0, "available": 0}


# --------------------------------------------------------------- Termux:API
def test_termux_output_is_read():
    assert power.from_termux(TERMUX_CHARGING)["level"] == 84
    unplugged = dict(TERMUX_CHARGING, plugged="UNPLUGGED", status="DISCHARGING")
    assert power.from_termux(unplugged)["plugged"] is False
    wireless = dict(TERMUX_CHARGING, plugged="PLUGGED_WIRELESS")
    assert power.from_termux(wireless)["plugged"] is True
    no_plug = {"status": "CHARGING", "percentage": 50}
    assert power.from_termux(no_plug)["plugged"] is True


@pytest.mark.parametrize("output", [None, [], {}, {"health": "GOOD"}, "texte"])
def test_unusable_termux_output_is_ignored(output):
    assert power.from_termux(output) is None


def test_implausible_termux_values_are_dropped():
    odd = dict(TERMUX_CHARGING, percentage=180, temperature=-273.0)
    status = power.from_termux(odd)
    assert status["level"] is None and status["temperature"] is None
    missing = dict(TERMUX_CHARGING, temperature=-214748364.8)     # Integer.MIN_VALUE / 10
    assert power.from_termux(missing)["temperature"] is None


def test_termux_output_variants():
    # Termux:API 0.51 : température en texte, selon la langue du téléphone
    assert power.from_termux(dict(TERMUX_CHARGING, temperature="25,5"))["temperature"] == 25.5
    assert power.from_termux(dict(TERMUX_CHARGING, temperature="?"))["temperature"] is None
    # Pourcentage absent : niveau / échelle
    no_percent = {k: v for k, v in TERMUX_CHARGING.items() if k != "percentage"}
    assert power.from_termux(dict(no_percent, level=42, scale=50))["level"] == 84
    # Termux de Google Play : valeurs manquantes à null, statut inconnu numéroté
    play = dict(TERMUX_CHARGING, plugged=None, status="UNKNOWN_7", percentage=None)
    assert power.from_termux(dict(play, level=None)) is None


def test_termux_is_asked_at_most_every_few_seconds(termux):
    termux["output"] = TERMUX_CHARGING
    for _ in range(5):
        assert power._termux_status()["level"] == 84
    assert termux["calls"] == 1
    power._termux["until"] = 0.0                   # délai écoulé
    power._termux_status()
    assert termux["calls"] == 2


def test_silent_termux_is_not_asked_again_at_once(termux):
    assert power._termux_status() is None
    assert power._termux_status() is None
    assert termux["calls"] == 1


def test_one_missed_reading_keeps_the_last_one_for_a_while(termux):
    """La commande se bloque parfois au hasard : un raté ne doit pas mettre le
    téléphone en pause, mais une mesure trop ancienne ne vaut plus rien."""
    termux["output"] = TERMUX_CHARGING
    assert power._termux_status()["level"] == 84
    termux["output"] = None
    power._termux["until"] = 0.0
    assert power._termux_status()["level"] == 84                # raté isolé
    power._termux["until"] = 0.0
    power._termux["good_at"] -= power.TERMUX_STALE + 1          # plus de nouvelles depuis 2 min
    assert power._termux_status() is None
    assert termux["calls"] == 3


# ------------------------------------- la commande elle-même (sans Termux)
class FakeProcess:
    pid = 4242

    def __init__(self, out=None, hang=False):
        self.out, self.hang, self.killed = out, hang, False

    def communicate(self, timeout=None):
        if self.hang:
            raise subprocess.TimeoutExpired("termux-battery-status", timeout)
        return self.out, None

    def kill(self):
        self.killed = True

    def wait(self):
        return -9


@pytest.fixture
def command(monkeypatch):
    monkeypatch.setattr(power.shutil, "which", lambda name: "/usr/bin/" + name)

    def use(process):
        monkeypatch.setattr(power.subprocess, "Popen", lambda *args, **kwargs: process)
        return process
    return use


def test_termux_command_output_is_parsed(command):
    command(FakeProcess(out=json.dumps(TERMUX_CHARGING)))
    assert power._run_termux()["percentage"] == 84
    command(FakeProcess(out="Termux:API n'est pas installé"))
    assert power._run_termux() is None


def test_hanging_termux_command_is_killed(command, monkeypatch):
    # Sans l'application Termux:API, la commande attend une réponse qui ne vient jamais
    killed = []
    if hasattr(os, "killpg"):
        monkeypatch.setattr(power.os, "killpg", lambda pid, sig: killed.append(pid))
    process = command(FakeProcess(hang=True))
    assert power._run_termux() is None
    assert process.killed or killed == [4242]


def test_without_termux_api_nothing_is_run(monkeypatch):
    monkeypatch.setattr(power.shutil, "which", lambda name: None)
    monkeypatch.setattr(power.subprocess, "Popen", lambda *a, **k: pytest.fail("lancée"))
    assert power._run_termux() is None
