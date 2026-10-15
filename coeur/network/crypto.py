"""Cryptographie du réseau entre nœuds.

Appairage : échange de clés X25519. Les deux machines affichent un code à
6 chiffres dérivé du secret commun, et l'utilisateur vérifie qu'il est le même
des deux côtés (comparaison numérique, comme l'appairage Bluetooth) : un
intercepteur placé au milieu obtiendrait forcément des codes différents.

Échanges : chaque message est scellé (ChaCha20-Poly1305) avec la clé propre à la
paire de machines. Les données associées lient le message à son action, son
émetteur, son destinataire et son horodatage : ni rejeu, ni détournement.
"""
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE = 12
TAG = 16
_SALT = b"coeur-ia/appairage/v1"


class SealError(Exception):
    """Message illisible : altéré, rejoué, mauvaise clé ou mauvais destinataire."""


def new_exchange():
    """Paire de clés éphémère pour un appairage. → (clé privée, clé publique brute)"""
    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw,
                                               serialization.PublicFormat.Raw)
    return private, public


def _hkdf(material: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=_SALT, info=info).derive(material)


def pair_secrets(private, peer_public: bytes, my_id: str, peer_id: str):
    """Clé de la paire (32 octets) et code de vérification (6 chiffres),
    identiques sur les deux machines."""
    shared = private.exchange(X25519PublicKey.from_public_bytes(peer_public))
    ids = "|".join(sorted([my_id, peer_id])).encode()
    key = _hkdf(shared, b"cle|" + ids, 32)
    code = int.from_bytes(_hkdf(shared, b"code|" + ids, 4), "big") % 1_000_000
    return key, f"{code:06d}"


def seal(key: bytes, data: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, data, aad)


def open_sealed(key: bytes, box: bytes, aad: bytes) -> bytes:
    if len(box) < NONCE + TAG:
        raise SealError("Message tronqué.")
    try:
        return ChaCha20Poly1305(key).decrypt(box[:NONCE], box[NONCE:], aad)
    except InvalidTag as exc:
        raise SealError("Message altéré, ou clé incorrecte.") from exc
