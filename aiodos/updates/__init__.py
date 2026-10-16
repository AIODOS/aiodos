"""Mises à jour automatiques, sans serveur.

Une version publiée est une archive du code et son manifeste, signé par la clé de
publication de l'auteur (keys.py, tools/publish.py). Les machines appairées se la
passent de l'une à l'autre (p2p.py) ; chacune vérifie la signature, installe la
version à côté de la précédente (install.py), puis redémarre dessus quand elle est
libre (service.py, restart.py). Au démarrage, le lanceur essaie la nouvelle version
et revient à la précédente si elle ne démarre pas (launcher.py).
"""
