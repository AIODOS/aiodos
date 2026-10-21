# aiodos-client : versions

<!-- SPDX-License-Identifier: Apache-2.0 -->

Les deux bibliothèques (Python, JavaScript) portent toujours le même numéro.

## 0.1.0 (21/10/2026)

Première version, pour AIODOS 2.1 (livraison L1 « branchable, texte ») :
- découverte d'AIODOS (fichier gateway.json, installation), états vus par l'appli ;
- accès par appli : demande (RFC 8628), code à 6 chiffres, relève de la clé, droits en plus,
  retrait ; connexion quand la passerelle est fermée, par le programme d'aide d'AIODOS ;
- preuve d'identité d'AIODOS avant d'envoyer la clé ; hôtes locaux seulement ;
- catalogue des modèles (avec le modèle qu'AIODOS prend lui-même, `default`, préféré sans
  autre choix), conversation en flux (outils, images, réflexion), embeddings (rendus avec le
  modèle qui les a calculés) ;
- erreurs à codes stables, messages en français et en anglais ;
- faux serveurs pour les essais des applis ;
- surface des travaux de moteur (ComfyUI…), de la file et des fichiers : présente, lève
  NotSupported jusqu'à la livraison L2.
