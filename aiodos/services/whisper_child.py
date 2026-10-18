"""Programme à part qui transcrit la parole pour services/media.py (faster-whisper).

Pourquoi à part : les fils de calcul de CTranslate2 sont créés par la bibliothèque, pas
par AIODOS ; ils ne passent pas par les threads de fond abaissés (node/priority.py) et
gardaient, sous Windows, la priorité normale. Ce programme, lui, est lancé en priorité
« inférieure à la normale » et en EcoQoS (nice 10 sous Linux et Android) : tous ses fils
cèdent la place aux programmes de l'utilisateur. Sa mémoire (~500 Mo pour « small ») est
rendue quand AIODOS le ferme, quelques minutes après la dernière demande (media.IDLE_S) ;
une panne de la bibliothèque n'arrête que lui.

Protocole, une ligne JSON par message (UTF-8) :
  AIODOS → programme : {"path", "model", "threads"} (une demande), puis « next » après
                       chaque segment reçu (le suivant est calculé) ou « stop » ;
  programme → AIODOS : {"language"}, puis {"segment": texte} un à un, et {"end": true} ;
                       {"error": message} si la transcription échoue.
Entre deux segments, le programme attend la réponse d'AIODOS : la cadence du travail de
fond (jobs/pacing.py : pause sur batterie, attente du secteur) s'applique avant le calcul
du segment suivant, comme quand la transcription se faisait dans AIODOS même. La fin de
l'entrée (AIODOS qui le ferme, ou qui s'arrête) le termine.

Un seul fil lit l'entrée, et seulement quand rien d'autre ne tourne : sous Windows, une
lecture bloquée sur le tuyau d'entrée dans un fil bloque aussi, dans un autre, le
lancement d'un programme qui en hérite (ce que font certaines bibliothèques à leur
chargement).
"""
import json
import sys


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
        try:
            request = json.loads(line)
            key = (str(request["model"]), int(request.get("threads") or 0))
            model = models.get(key)
            if model is None:
                from faster_whisper import WhisperModel
                models.clear()          # un seul modèle en mémoire
                model = WhisperModel(key[0], device="cpu", compute_type="int8",
                                     cpu_threads=key[1])
                models[key] = model
            segments, info = model.transcribe(str(request["path"]), vad_filter=True)
            send({"language": getattr(info, "language", None) or "?"})
            for segment in segments:    # générateur : un segment calculé à la fois
                send({"segment": segment.text})
                answer = read()
                if not answer:
                    return
                if answer.strip() != "next":
                    break
            send({"end": True})
        except Exception as exc:
            send({"error": f"{exc.__class__.__name__} : {exc}"[:500]})


def _send(message: dict):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    serve(sys.stdin.readline, _send)


if __name__ == "__main__":
    main()
