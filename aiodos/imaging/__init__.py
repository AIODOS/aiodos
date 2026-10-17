"""Génération d'images, répartie entre mes machines (docs/generation-repartie.md).

Chaque image demandée est une unité indépendante de la file de travail : un modèle, une
consigne, une graine et des réglages, qui rend un PNG. Mes machines qui ont le même
modèle (mêmes empreintes) en calculent leur part ; l'image revient et n'est gardée que
chez celle qui l'a demandée.

- catalog.py   modèles connus : fichiers épinglés, vérification des empreintes
- download.py  téléchargement d'un modèle (Hugging Face, adresse épinglée, reprise)
- engine.py    stable-diffusion.cpp : sd-server, un modèle chargé à la fois
- generate.py  une image : demande à sd-server, suivi, arrêt
- gallery.py   images gardées : base et fichiers PNG
"""
