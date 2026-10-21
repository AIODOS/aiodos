# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""La preuve d'identité d'AIODOS (docs/api-applications.md § 5.1) et la forme des clés."""

import re

from aiodos_client import _proof
from aiodos_client.fake import new_key
from support import load_contract


def test_contract_vector():
    contract = load_contract("proof.json")
    for vector in contract["vectors"]:
        assert _proof.compute(vector["key"], vector["challenge"]) == vector["proof"]
        assert _proof.verify(vector["key"], vector["challenge"], vector["proof"])
        assert _proof.key_id(vector["key"]) == vector["key_id"]


def test_key_format_matches_contract():
    rule = load_contract("proof.json")["key_format"]
    pattern = re.compile(rule["pattern"])
    for _ in range(20):
        key = new_key()
        assert pattern.match(key)
        assert _proof.key_id(key) == key[rule["id_offset"]:rule["id_offset"] + rule["id_length"]]
    for wrong in ("aiodos_app_k3j9m2qa_" + "A" * 43,       # 9 : hors de base32
                  "aiodos_app_k3j7m2qa_" + "A" * 42,
                  "aiodos-" + "A" * 43, "", None, 42):
        assert _proof.key_id(wrong) is None


def test_challenge_is_fresh_base64url():
    seen = {_proof.challenge() for _ in range(50)}
    assert len(seen) == 50
    for challenge in seen:
        assert len(challenge) == 43
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge)


def test_wrong_proofs_are_refused():
    vector = load_contract("proof.json")["vectors"][0]
    key, challenge, proof = vector["key"], vector["challenge"], vector["proof"]
    assert not _proof.verify(key, challenge, proof[:-1] + ("A" if proof[-1] != "A" else "B"))
    assert not _proof.verify(key, _proof.challenge(), proof)
    assert not _proof.verify(key, challenge, None)
    assert not _proof.verify(key, challenge, "é" * 43)
    assert not _proof.verify(new_key(), challenge, proof)
