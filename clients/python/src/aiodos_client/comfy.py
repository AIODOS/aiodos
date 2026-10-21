# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""`ai.comfy` : les travaux ComfyUI confiés à AIODOS (livraison 2 ; conception « partager la
puissance », § 6.10).

AIODOS ne les sert pas encore : tant que `features` n'annonce pas « engines.comfyui », chaque
fonction lève NotSupported sans rien essayer. La surface est déjà là pour que le code de
l'appli n'ait pas à changer : son repli (le ComfyUI de ce PC) se branche sur NotSupported.
Si AIODOS les annonce déjà, c'est ce client (0.1.0) qui ne sait pas encore s'en servir :
NotSupported aussi, avec le conseil de mettre aiodos-client à jour.
"""

FEATURE = "engines.comfyui"


class Comfy:
    """Les travaux ComfyUI par AIODOS (L2)."""

    def __init__(self, client):
        self._client = client

    def _unserved(self):
        self._client._refuse_unserved(FEATURE)

    def plan(self, graph=None, graphs=None, recipe=None, items=None, repeat=None, inputs=None,
             models="auto", where=None, priority="normal", bounds=None, strict=False,
             sensitive=False):
        """Le pré-vol d'un travail : où il peut tourner, ce qui manque (rien n'est mis en file)."""
        self._unserved()

    def object_info(self, cls=None):
        """L'`object_info` du ComfyUI de ce PC (ou de la machine de référence)."""
        self._unserved()

    def template(self, name):
        """Un gabarit de ComfyUI, relayé par AIODOS."""
        self._unserved()

    def recipes(self):
        """Les recettes connues d'AIODOS (L4)."""
        self._unserved()

    def recipe_for(self, template):
        """La recette qui correspond à un gabarit, ou None (L4)."""
        self._unserved()

    def tokenize(self, graph, images):
        """Met les jetons d'entrée dans les nœuds LoadImage et LoadVideo donnés
        ({identifiant de nœud: octets}) ; rend (graphe, entrées)."""
        self._unserved()

    def submit(self, graph=None, graphs=None, recipe=None, items=None, repeat=None, inputs=None,
               models="auto", where=None, priority="normal", label=None, client_ref=None,
               bounds=None, strict=False, sensitive=False):
        """Envoie les entrées absentes, puis le travail : rend un Job. `sensitive=True`
        limite le lieu de calcul à ce PC et à mes machines."""
        self._unserved()

    def run(self, graph=None, graphs=None, recipe=None, items=None, repeat=None, inputs=None,
            models="auto", where=None, priority="normal", label=None, client_ref=None,
            bounds=None, strict=False, sensitive=False, dest_dir=None, stem=None, cancel=None):
        """submit(), puis wait(), puis download() : rend la liste des fichiers (Path)."""
        self._unserved()
