#!/bin/sh
# Aiodos : nœud de calcul sans fenêtre (téléphone Android sous Termux, Linux…).
#   sh lancer-noeud.sh              (les options en plus sont passées au nœud)
# Sous Termux : garde le processeur éveillé écran éteint, et limite le téléphone
# à une unité d'embeddings et une page web à la fois (ni vision, ni Whisper).
# Démarre Ollama s'il ne tourne pas. Pas à pas : docs/android.md.
cd "$(dirname "$0")" || exit 1

case "$PREFIX" in
    *com.termux*) termux=1 ;;
    *) termux= ;;
esac

if [ -n "$termux" ]; then
    termux-wake-lock
fi

if ! command -v ollama >/dev/null 2>&1; then
    echo "Ollama est introuvable (Termux : pkg install ollama). Voir docs/android.md." >&2
elif ! ollama list >/dev/null 2>&1; then
    echo "Démarrage d'Ollama (journal : $HOME/ollama.log)…"
    nohup ollama serve >"$HOME/ollama.log" 2>&1 &
    sleep 5
fi

if [ -n "$termux" ]; then
    set -- --slots gpu=0,cpu=0,embed=1,net=1 "$@"
fi
exec python3 -m aiodos --headless --network --port 8800 "$@"
