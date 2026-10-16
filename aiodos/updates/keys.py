"""Clés publiques de publication : seule une version signée par l'une d'elles s'installe.

La clé privée correspondante reste chez l'auteur, hors du dépôt (tools/publish.py).
Changer de clé : publier, signée par l'ancienne, une version qui ajoute la nouvelle
ici ; retirer l'ancienne dans une version suivante, signée par la nouvelle.
"""

# identifiant (16 premiers chiffres hexadécimaux du SHA-256 de la clé) : clé Ed25519 en base64
TRUSTED_KEYS = {
}
