# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""`ai.inputs` : les entrées d'un travail envoyées à part, par empreinte (livraison 2 ;
conception « partager la puissance », § 6.6 et 6.10).

AIODOS ne les sert pas encore : tant que `features` n'annonce pas « inputs », put() lève
NotSupported sans rien essayer ; annoncées, c'est ce client (0.1.0) qui est à mettre à jour.
"""

FEATURE = "inputs"


class Inputs:
    """Les entrées par empreinte (L2)."""

    def __init__(self, client):
        self._client = client

    def put(self, data, type=None):
        """Envoie une entrée (octets) si AIODOS ne l'a pas déjà ; rend son SHA-256."""
        self._client._refuse_unserved(FEATURE)
