"""Réponses arrêtées en cours d'enregistrement (api/routes/chat.py : après Stop, la réponse
partielle s'enregistre hors de la boucle du serveur). La question suivante, une création,
« Modifier » ou « Régénérer » attendent qu'elle soit en place : la conversation garde son
ordre, et l'historique la contient."""
import threading

WAIT_S = 20.0                     # au-delà, on n'attend plus (base très occupée)
_pending = {}                     # conversation → threading.Event, levé une fois enregistrée
_lock = threading.Lock()


def begin(conv_id) -> threading.Event:
    done = threading.Event()
    with _lock:
        _pending[conv_id] = done
    return done


def end(conv_id, done: threading.Event):
    done.set()
    with _lock:
        if _pending.get(conv_id) is done:
            del _pending[conv_id]


def wait(conv_id, timeout: float = WAIT_S) -> bool:
    """Attend l'enregistrement en cours dans cette conversation, s'il y en a un."""
    with _lock:
        done = _pending.get(conv_id)
    return done.wait(timeout) if done is not None else True
