"""Gros modèles répartis entre mes machines (docs/gros-modeles.md).

Un modèle trop gros pour la carte graphique d'une seule machine est découpé en tranches
de couches : chaque machine qui prête sa carte en garde une, et la machine qui pose la
question garde la dernière (le dernier étage, qui note tout le vocabulaire, ne traverse
jamais le réseau). Le moteur est llama.cpp : llama-server ici, ggml-rpc-server chez les
machines qui prêtent, joints par des tunnels chiffrés du compagnon aiodos-net.

- engine.py  : les programmes de llama.cpp (où, quelle version, quels appareils) ;
- weights.py : le fichier GGUF d'un modèle d'Ollama, et ce que dit son en-tête ;
- plan.py    : qui garde quelle tranche ;
- helper.py  : la machine qui prête sa carte (actions cluster/… du réseau) ;
- group.py   : la machine qui pose la question (modèles « réparti: » des conversations).
"""
