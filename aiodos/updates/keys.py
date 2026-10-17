"""Clés publiques de publication : seule une version signée par l'une d'elles s'installe.

La clé privée correspondante reste chez l'auteur, hors du dépôt (tools/publish.py).
Changer de clé : publier, signée par l'ancienne, une version qui ajoute la nouvelle
ici ; retirer l'ancienne dans une version suivante, signée par la nouvelle.
"""

# identifiant (16 premiers chiffres hexadécimaux du SHA-256 de la clé) : clé Ed25519 en base64
TRUSTED_KEYS = {
    "3eab3940710a965e": "x3+Zw7VibMmPeiUian5y5fz3XMKH9Zm8Gbqk9IVv0Bw=",
}

# Appareils d'approbation (deuxième signature, cf. approval.py et docs/mises-a-jour.md).
# Vide : la signature de l'auteur suffit. Sinon, une version ne s'installe et ne se
# transmet qu'approuvée par l'un d'eux. Pour en ajouter un : créer sa clé dans la vue
# Mises à jour de l'appareil (le téléphone), coller ici la ligne qu'elle donne, faire un
# commit, puis publier. En déclarer deux (téléphone et appareil de secours) : sans
# approbateur, plus aucune version ne peut partir.
APPROVER_KEYS = {
}
