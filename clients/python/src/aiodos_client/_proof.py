# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""La preuve d'identité d'AIODOS (docs/api-applications.md § 5.1).

AIODOS arrêté, n'importe quel programme pourrait écouter sur le port et recueillir les clés.
Avant d'envoyer la sienne, le client tire un défi (32 octets au hasard, en base64url sans
« = ») et demande `GET /aiodos/v1/info?key_id=<id>&challenge=<défi>`, sans la clé. AIODOS,
qui ne garde que SHA-256(clé), répond :

    proof = base64url_sans_égal(HMAC-SHA256(clé = SHA-256(clé d'accès en UTF-8),
                                            message = "aiodos-proof/1\\n" + défi))

Le client refait le calcul et compare en temps constant. Un imposteur, qui ne connaît pas la
clé, ne sait pas répondre. Le vecteur d'essai est dans clients/contract/proof.json.
"""

import base64
import hashlib
import hmac
import re
import secrets

PREFIX = b"aiodos-proof/1\n"
KEY_PATTERN = re.compile(r"^aiodos_app_([a-z2-7]{8})_[A-Za-z0-9_-]{43}$")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def challenge() -> str:
    """Un défi neuf : 32 octets au hasard, en base64url sans « = » (43 caractères)."""
    return _b64(secrets.token_bytes(32))


def key_id(key) -> str | None:
    """L'identifiant public d'une clé d'appli (`aiodos_app_<id>_<secret>`), ou None."""
    if not isinstance(key, str):
        return None
    found = KEY_PATTERN.match(key)
    return found.group(1) if found else None


def compute(key: str, challenge_text: str) -> str:
    """La preuve qu'AIODOS doit rendre pour cette clé et ce défi."""
    secret = hashlib.sha256(key.encode("utf-8")).digest()
    mac = hmac.new(secret, PREFIX + challenge_text.encode("ascii"), hashlib.sha256)
    return _b64(mac.digest())


def verify(key: str, challenge_text: str, proof) -> bool:
    """La preuve reçue est-elle juste ? (comparaison en temps constant)"""
    if not isinstance(proof, str) or not proof.isascii():
        return False
    return hmac.compare_digest(compute(key, challenge_text).encode("ascii"),
                               proof.encode("ascii"))


def fingerprint(key: str) -> str:
    """Une empreinte courte de la clé, pour savoir si elle a changé sans la garder."""
    return hashlib.sha256(b"aiodos-client/fingerprint\n" + key.encode("utf-8")).hexdigest()[:16]
