"""Programme à part qui transcrit la parole pour services/media.py (faster-whisper).

Pourquoi à part : les fils de calcul de CTranslate2 sont créés par la bibliothèque, pas
par AIODOS ; ils ne passent pas par les threads de fond abaissés (node/priority.py) et
gardaient, sous Windows, la priorité normale. Ce programme, lui, est lancé en priorité
« inférieure à la normale » et en EcoQoS (nice 10 sous Linux et Android) : tous ses fils
cèdent la place aux programmes de l'utilisateur. Sa mémoire (~500 Mo pour « small ») est
rendue quand AIODOS le ferme, quelques minutes après la dernière demande (media.IDLE_S) ;
une panne de la bibliothèque n'arrête que lui.

Protocole, une ligne JSON par message (UTF-8) :
  AIODOS → programme : {"id", "path", "model", "threads", "download"} (une demande), puis
                       « next » après chaque segment reçu (le suivant est calculé) ou
                       « stop » ;
  programme → AIODOS : {"id", "language"}, puis {"id", "segment": texte} un à un, et
                       {"id", "end": true} ; {"id", "error": message} si la transcription
                       échoue, avec "missing": true si le modèle n'est pas sur cette
                       machine et que "download" ne permet pas de le télécharger. Chaque
                       réponse rappelle le numéro de sa demande.

Dernière défense (services/media.py vérifie déjà) : "model" doit être un des modèles de
faster-whisper (available_models). Le nom vient aussi d'une autre machine ; faster-whisper
prendrait sinon un chemin pour un dossier de modèle à charger, et « compte/dépôt » pour un
dépôt Hugging Face à télécharger. Le modèle est cherché dans le cache, et téléchargé
seulement si "download" est vrai ; il est ensuite chargé par son dossier dans ce cache,
jamais par son nom (un dossier « small » du dossier courant n'est pas pris).
Entre deux segments, le programme attend la réponse d'AIODOS : la cadence du travail de
fond (jobs/pacing.py : pause sur batterie, attente du secteur) s'applique avant le calcul
du segment suivant, comme quand la transcription se faisait dans AIODOS même. La fin de
l'entrée (AIODOS qui le ferme, ou qui s'arrête) le termine.

Le protocole a sa propre copie de la sortie standard ; la sortie standard elle-même
(celle de Python, du runtime C et de Windows) part vers la sortie d'erreur : ce qu'une
bibliothèque y écrit ne peut pas se mêler aux réponses. Un seul fil lit l'entrée, et
seulement quand rien d'autre ne tourne : sous Windows, une lecture bloquée sur le tuyau
d'entrée dans un fil bloque aussi, dans un autre, le lancement d'un programme qui en
hérite (ce que font certaines bibliothèques à leur chargement).
"""
import json
import os
import sys


class _Missing(Exception):
    """Le modèle n'est pas sur cette machine, et la demande ne permet pas de le
    télécharger."""


def _locate(name, download: bool) -> str:
    """Le dossier du modèle `name` de faster-whisper dans le cache (téléchargé d'abord si
    `download` le permet)."""
    import faster_whisper
    if not isinstance(name, str) or name not in faster_whisper.available_models():
        raise ValueError(f"modèle Whisper inconnu : « {str(name)[:60]} »")
    try:
        return faster_whisper.download_model(name, local_files_only=not download)
    except Exception as exc:
        if not download:
            raise _Missing(name) from exc
        raise


def _load(where: str, threads: int):
    import faster_whisper
    return faster_whisper.WhisperModel(where, device="cpu", compute_type="int8",
                                       cpu_threads=threads)


def serve(read, send):
    """Répond aux demandes lues par read() (une ligne, "" à la fin de l'entrée)."""
    models = {}
    while True:
        line = read()
        if not line:
            return
        line = line.strip()
        if not line or line in ("next", "stop"):
            continue                    # réponse en retard à une transcription finie
        rid = None
        try:
            request = json.loads(line)
            rid = request.get("id")
            key = (request["model"], int(request.get("threads") or 0))
            model = models.get(key)
            if model is None:
                # Le modèle en mémoire n'est rendu qu'une fois le nouveau trouvé : un nom
                # inconnu ou un modèle absent (qu'un ami peut demander) ne le fait pas
                # recharger à la demande suivante
                where = _locate(key[0], request.get("download") is True)
                models.clear()          # un seul modèle en mémoire
                model = models[key] = _load(where, key[1])
            segments, info = model.transcribe(str(request["path"]), vad_filter=True)
            send({"id": rid, "language": getattr(info, "language", None) or "?"})
            for segment in segments:    # générateur : un segment calculé à la fois
                send({"id": rid, "segment": segment.text})
                answer = read()
                if not answer:
                    return
                if answer.strip() != "next":
                    break
            send({"id": rid, "end": True})
        except _Missing:
            send({"id": rid, "error": "modèle absent de cette machine", "missing": True})
        except Exception as exc:
            send({"id": rid, "error": f"{exc.__class__.__name__} : {exc}"[:500]})


def _protocol_channel():
    """Le canal des réponses (une copie de la sortie standard), la sortie standard
    renvoyée vers la sortie d'erreur."""
    channel = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    sys.stdout.flush()
    os.dup2(2, 1)
    if sys.platform == "win32":     # le code natif qui écrit par la poignée de Windows
        import ctypes
        import msvcrt
        from ctypes import wintypes
        set_handle = ctypes.windll.kernel32.SetStdHandle
        set_handle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        set_handle(wintypes.DWORD(-11 & 0xFFFFFFFF), msvcrt.get_osfhandle(1))  # STD_OUTPUT_HANDLE
    sys.stdout = sys.stderr
    return channel


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    channel = _protocol_channel()

    def send(message: dict):
        channel.write(json.dumps(message, ensure_ascii=False) + "\n")
        channel.flush()
    serve(sys.stdin.readline, send)


if __name__ == "__main__":
    main()
