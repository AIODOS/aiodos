"""Créer de l'audio : voix, musique, effets sonores (docs/conception/audio.md).

Chaque création est une unité de la file de travail (jobs/kinds/audio.py) : un modèle, un
texte (ou une description, des paroles), une durée, qui rend un WAV. Mes machines qui ont le
même modèle (mêmes empreintes) peuvent la calculer ; le son revient et n'est gardé que chez
celle qui l'a demandé (imaging/gallery.py, avec les images et les vidéos).

- catalog.py   modèles connus, par genre : fichiers épinglés, rang, mémoire, langues, voix,
               licences
- files.py     les fichiers qu'en a cette machine : présence, empreintes vérifiées
- download.py  téléchargement d'un modèle (Hugging Face, adresse épinglée, reprise)
- engine.py    audio.cpp : où il est, sa version, la carte qui calcule, un calcul
- generate.py  une création : la commande d'audio.cpp pour ce modèle, le WAV contrôlé
- wav.py       lire un WAV : format, durée, silence
- lenders.py   qui peut calculer un modèle pour moi : ce PC, mes machines, mes amis
- ranking.py   « Automatique » pour chaque genre, la suggestion, le menu
"""
