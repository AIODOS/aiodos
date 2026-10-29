"""Génération d'images, répartie entre mes machines (docs/generation-repartie.md).

Chaque image demandée est une unité indépendante de la file de travail : un modèle, une
consigne, une graine et des réglages, qui rend un PNG. Mes machines qui ont le même
modèle (mêmes empreintes) en calculent leur part ; l'image revient et n'est gardée que
chez celle qui l'a demandée.

- catalog.py   modèles connus : fichiers épinglés, vérification des empreintes, rang et
               mémoire de carte graphique, licences
- ranking.py   « Automatique » : le plus puissant qu'une machine a, où qu'elle soit ; le menu,
               la suggestion du meilleur modèle pour ce PC
- lenders.py   qui peut calculer un modèle pour moi : ce PC, mes machines, mes amis
- download.py  téléchargement d'un modèle (Hugging Face, adresse épinglée, reprise)
- engine.py    stable-diffusion.cpp : sd-server, un modèle chargé à la fois
- generate.py  une image : demande à sd-server, suivi, arrêt
- gallery.py   images gardées : base et fichiers PNG
"""
