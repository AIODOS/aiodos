# Cœur IA

Atelier d'IA **100 % local** : un chatbot type Claude qui tourne sur ta machine
avec des modèles open source (Mistral, Qwen, et d'autres à venir) via **Ollama**.
Conçu pour coder, discuter et travailler — sans envoyer tes données dans le cloud.

## Palier 1 (actuel) — Chat + base de connaissance

- 💬 **Chat multi-modèles** en streaming (réponse au fil de l'eau)
- 🔀 **Sélecteur de modèle** — bascule entre tous les modèles installés dans Ollama
- 📚 **Base de connaissance (RAG)** — ajoute des fichiers texte/code, l'IA s'en sert
- 🗂 **Conversations sauvegardées** localement (reprise possible)
- 🌙 Interface sombre PyQt6

## Prérequis

1. **Ollama** installé et lancé — https://ollama.com
2. Les modèles téléchargés :
   ```powershell
   ollama pull qwen2.5-coder:7b
   ollama pull mistral:7b
   ollama pull qwen2.5:7b
   ollama pull nomic-embed-text
   ```
   (`nomic-embed-text` est requis pour la base de connaissance.)
3. Python 3.14 + dépendances : `pip install -r requirements.txt`

## Lancer

Double-clic sur `lancer.bat`, ou :

```powershell
C:\Users\22eme\AppData\Local\Python\pythoncore-3.14-64\python.exe main.py
```

## Architecture

```
Cœur IA/
├── main.py                  # point d'entrée QApplication
├── core/
│   ├── config.py            # data/config.json (modèle par défaut, prompt système…)
│   ├── ollama_client.py     # appels à l'API Ollama (chat streaming + embeddings)
│   ├── chat_session.py      # sessions de conversation (persistance JSON)
│   └── knowledge.py         # base de connaissance RAG (chunks + cosinus numpy)
├── api/
│   └── workers.py           # QThread : ChatWorker, ModelListWorker, IndexWorker
├── ui/
│   ├── styles.py            # palette + feuille de style sombre
│   ├── message_widget.py    # bulle de message (rendu markdown)
│   └── chat_window.py       # fenêtre principale
└── data/                    # généré : config, sessions, base de connaissance
```

**Convention** : tout appel réseau/IA tourne dans un `QThread` (jamais dans l'UI),
communication par signaux PyQt6 — comme PANDORA.

## Paliers suivants (prévus)

- 🤝 **Orchestration multi-agents** : plusieurs modèles qui collaborent / se corrigent
- 🛠 **Outils système** : lecture/écriture de fichiers, tri de sécurité (avec bac à sable + confirmation)
- 🔁 **Amélioration de logiciels** : l'IA propose des patchs → tests → validation humaine
- ☁️ **Modèles cloud optionnels** pour le vrai parallèle massif
