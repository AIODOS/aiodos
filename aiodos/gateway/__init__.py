"""AIODOS ouvert aux autres outils : API compatible OpenAI et Ollama, serveur MCP.

Un second serveur, à côté de celui de l'interface, sur 127.0.0.1 et un port fixe
(réglage gateway_port), désactivé par défaut (gateway_enabled), protégé par une clé
d'accès stable (access.py). Open WebUI, AnythingLLM, Continue, un éditeur de code ou un
assistant MCP y trouvent les modèles de cette machine et ceux que prêtent mes machines
appairées, avec le même choix automatique de la machine qui répond que dans les
conversations (network/placement.py). Mode d'emploi : docs/autres-outils.md.
"""
