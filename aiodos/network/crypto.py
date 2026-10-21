"""Cryptographie du réseau entre nœuds.

Appairage par code : échange de clés X25519 avec engagement, comme la comparaison
numérique de l'appairage Bluetooth (SSP). La machine qui lance l'appairage s'engage
d'abord sur sa clé et un nonce (une empreinte, commitment) ; l'autre lui envoie alors
les siens ; la première révèle enfin sa clé et son nonce, que l'autre compare à
l'engagement. Le code à 6 chiffres et la clé de la paire sont tirés du secret commun ET
de toute la transcription (identifiants, clés, nonces) : transcript_secrets.

Sans engagement, un intermédiaire actif choisirait sa dernière clé après avoir vu les
deux autres et chercherait, parmi un million de codes, celui qui s'affiche des deux
côtés (quelques secondes de calcul). Avec lui, chaque clé est fixée avant que le code
d'en face soit connu : chaque essai n'a qu'une chance sur un million, et un échec se
voit (codes différents).

pair_secrets (sans transcription ni code utile) ne sert plus qu'à l'appairage par
groupe, où les clés publiques passent par des paires déjà vérifiées par un humain.

Échanges : chaque message est scellé (ChaCha20-Poly1305) avec la clé propre à la
paire de machines. Les données associées lient le message à son action, son
émetteur, son destinataire et son horodatage : ni rejeu, ni détournement.
"""
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from aiodos.i18n import t

NONCE = 12
TAG = 16
_SALT = b"aiodos/appairage/v1"

PAIRING = 2                        # version de l'appairage par code (2 : avec engagement)
PAIR_NONCE = 32                    # octets du nonce de chaque machine
COMMIT = 32                        # octets d'un engagement (SHA-256)
_COMMIT_TAG = b"aiodos/appairage/v2/engagement"


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
    """Clé de la paire (32 octets) et code (6 chiffres), identiques sur les deux machines.

    Seulement pour un échange relayé par des paires déjà vérifiées (appairage par groupe,
    group.py) : sans engagement, ce code ne protège pas d'un intermédiaire actif, et
    l'appairage par code passe par transcript_secrets. Inchangée : une machine plus
    ancienne tire la même clé d'une présentation."""
    shared = private.exchange(X25519PublicKey.from_public_bytes(peer_public))
    ids = "|".join(sorted([my_id, peer_id])).encode()
    key = _hkdf(shared, b"cle|" + ids, 32)
    code = int.from_bytes(_hkdf(shared, b"code|" + ids, 4), "big") % 1_000_000
    return key, f"{code:06d}"


def _public_bytes(private) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)


def _fields(*parts) -> bytes:
    """Morceaux mis bout à bout sans ambiguïté : chacun précédé de sa longueur."""
    out = b""
    for part in parts:
        raw = part.encode("utf-8") if isinstance(part, str) else bytes(part)
        out += len(raw).to_bytes(2, "big") + raw
    return out


def new_nonce() -> bytes:
    return os.urandom(PAIR_NONCE)


def commitment(initiator_id: str, responder_id: str, public: bytes, nonce: bytes) -> bytes:
    """Engagement de la machine qui lance l'appairage, envoyé avant sa clé : il la lie à
    sa clé publique, à son nonce et aux deux identifiants."""
    return hashlib.sha256(_COMMIT_TAG + _fields(initiator_id, responder_id, public,
                                                nonce)).digest()


def commitment_ok(commit: bytes, initiator_id: str, responder_id: str, public: bytes,
                  nonce: bytes) -> bool:
    """La clé et le nonce révélés sont-ils ceux de l'engagement ? (temps constant)"""
    return hmac.compare_digest(bytes(commit), commitment(initiator_id, responder_id,
                                                         public, nonce))


def transcript_secrets(private, initiator: tuple, responder: tuple):
    """Clé de la paire (32 octets) et code (6 chiffres) de l'appairage par code, tirés du
    secret commun et de toute la transcription. initiator et responder : (identifiant,
    clé publique, nonce) de la machine qui lance l'appairage et de celle qui répond ;
    `private` est la clé privée de l'une des deux. ValueError : clé illisible ou faible
    (secret commun nul)."""
    # Ces trois refus ne sont jamais montrés : pairing.py les remplace par le sien
    mine = _public_bytes(private)
    if mine == bytes(initiator[1]):
        theirs = responder[1]
    elif mine == bytes(responder[1]):
        theirs = initiator[1]
    else:
        # i18n: tel quel (jamais montré)
        raise ValueError("Cette clé privée n'est d'aucun des deux côtés.")
    for _, public, nonce in (initiator, responder):
        if len(public) != 32 or len(nonce) != PAIR_NONCE:
            # i18n: tel quel (jamais montré)
            raise ValueError("Clé ou nonce de longueur inattendue.")
    if bytes(initiator[1]) == bytes(responder[1]):
        # Sa propre clé renvoyée en miroir : ce n'est pas une autre machine
        # i18n: tel quel (jamais montré)
        raise ValueError("Les deux côtés présentent la même clé.")
    shared = private.exchange(X25519PublicKey.from_public_bytes(bytes(theirs)))
    transcript = _fields(b"v2", initiator[0], responder[0], initiator[1], responder[1],
                         initiator[2], responder[2])
    key = _hkdf(shared, b"cle|v2|" + transcript, 32)
    code = int.from_bytes(_hkdf(shared, b"code|v2|" + transcript, 4), "big") % 1_000_000
    return key, f"{code:06d}"


def seal(key: bytes, data: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, data, aad)


def open_sealed(key: bytes, box: bytes, aad: bytes) -> bytes:
    if len(box) < NONCE + TAG:
        raise SealError(t("p2p.truncated"))
    try:
        return ChaCha20Poly1305(key).decrypt(box[:NONCE], box[NONCE:], aad)
    except InvalidTag as exc:
        raise SealError(t("p2p.altered")) from exc
