# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Outils communs des essais : le contrat partagé, un port fermé, une attente, le graphe des
travaux ComfyUI du contrat."""

import copy
import json
import socket
import time
from pathlib import Path

CONTRACT = Path(__file__).resolve().parents[2] / "contract"
SOURCE = Path(__file__).resolve().parents[1] / "src" / "aiodos_client"

APP = {"app": "PANDORA", "app_id": "arkane.pandora", "version": "3.2.0"}
COMFY = ("chat", "embed", "engines:comfyui")


def load_contract(name):
    return json.loads((CONTRACT / name).read_text(encoding="utf-8"))


def interaction(name, title):
    for item in load_contract(name)["interactions"]:
        if item["name"] == title:
            return item
    raise KeyError(f"{name} : pas d'interaction « {title} »")


def closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout=5.0, step=0.02):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition jamais remplie")
        time.sleep(step)


def json_type(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def assert_shape(example, actual, where="réponse"):
    """La règle du contrat : chaque clé de l'exemple existe dans la réponse, avec le même type
    JSON, récursivement (pour une liste, son premier élément quand les deux en ont) ; null
    vaut pour tout type, d'un côté comme de l'autre (comme tests/test_gateway_apps._shape,
    côté serveur : « mine » vaut null ou un nombre selon les cercles servis)."""
    if example is None or actual is None:
        return
    assert json_type(actual) == json_type(example), f"{where} : {actual!r} au lieu de {example!r}"
    if isinstance(example, dict):
        for key, value in example.items():
            assert key in actual, f"{where} : « {key} » manque"
            assert_shape(value, actual[key], f"{where}.{key}")
    elif isinstance(example, list) and example and actual:
        assert_shape(example[0], actual[0], f"{where}[0]")


def at_path(value, dotted):
    for part in dotted.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def job_request():
    """Le corps de « travail déposé » (clients/contract/jobs.json), {sha256} à remplir."""
    return copy.deepcopy(interaction("jobs.json", "travail déposé")["request"]["json"])


def graph():
    """Le graphe au format API du contrat (LoadImage « @in:ref0 », SaveImage « 9 »)."""
    return job_request()["graph"]


def items():
    return job_request()["items"]


def fill(value, **found):
    """Les {sha256}, {job_id}, {file_id}… du contrat remplacés, partout."""
    if isinstance(value, str):
        for name, real in found.items():
            value = value.replace("{" + name + "}", str(real))
        return value
    if isinstance(value, list):
        return [fill(v, **found) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, **found) for k, v in value.items()}
    return value
