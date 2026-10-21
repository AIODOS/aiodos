# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""Outils communs des essais : le contrat partagé, un port fermé, une attente."""

import json
import socket
import time
from pathlib import Path

CONTRACT = Path(__file__).resolve().parents[2] / "contract"
SOURCE = Path(__file__).resolve().parents[1] / "src" / "aiodos_client"

APP = {"app": "PANDORA", "app_id": "arkane.pandora", "version": "3.2.0"}


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
    JSON, récursivement (pour une liste, son premier élément quand les deux en ont)."""
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
