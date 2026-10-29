"""Une vidéo de la conversation créée par une machine du réseau public (docs/conception/
reseau-public-calcul.md § 11 ; l'auteur, le 29/10/2026 : « il faut que ça soit pareil pour
les vidéos ») : l'unité PUBLIC_VIDEO_TASK d'un travail « images » que chat/creations.py a
confié au réseau public, avec l'accord une fois (chat/public_creations.py : le même que pour les
images), parce que ni cette machine ni mes machines ne pouvaient la faire.

Le parcours d'une image (jobs/kinds/public_images.create) : une unité de cette machine
seulement, sur la ressource « net » (elle attend le réseau, plusieurs minutes, sans occuper la
carte d'ici) ; l'accord relu avant chaque envoi ; la base du filtre ; les machines qui prêtent
ce modèle de vidéos (network/public_video_ask.lenders), la suivante si la première échoue,
aucune autre si son filtre refuse la description ; une clé jetable par création. La vidéo,
vérifiée et réécrite (imaging/webm.py), est gardée comme une création d'ici
(imaging/gallery.py), avec son nombre d'images et la cadence du modèle, sa machine marquée
« Créée par une machine du réseau public ».
"""
from aiodos.i18n import t
from aiodos.imaging import gallery
from aiodos.jobs.kinds import images as kind
from aiodos.jobs.kinds import public_images
from aiodos.jobs.registry import register_local
from aiodos.network import public_video_ask

TASK = kind.PUBLIC_VIDEO_TASK


def _keep(params: dict, made: dict) -> dict:
    return gallery.add(params, made["webm"], machine=t("public_images.machine"),
                       seconds=made["seconds"], frames=made["frames"], fps=made["fps"])


def compute(payload, file, options, stop=None):
    return public_images.create(payload, stop, public_video_ask, public_video_ask.video, _keep,
                                "public_videos")


register_local(TASK, compute)
