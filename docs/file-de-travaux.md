# La file de travaux, comme un torrent

Livraison L2a de l'intégration aux applications (conception :
[conception/partage-puissance.md](conception/partage-puissance.md) § 2.5 et 2.6), le
21/10/2026. Elle profite à tout ce qui passe par la file : l'indexation des bibliothèques,
les images et vidéos d'AIODOS, et bientôt les travaux des applications (L2b : ComfyUI de
PANDORA réparti entre mes machines).

## Ce qui change pour l'utilisateur

- **Pause « après l'unité en cours »** (par défaut) : plus rien ne part ; ce qui calcule en
  ce moment, ici ou sur une autre machine, va au bout et son résultat arrive. Rien ne se perd.
- **Pause « tout de suite »** : le calcul en cours s'arrête, sur ce PC et chez les autres
  machines (elles l'apprennent à leur prochain renouvellement, 30 s au plus), et l'unité est
  rendue **sans compter d'essai**. Seul le calcul déjà fait est perdu.
- **Tout mettre en pause** (vue Tâches) : toute la file, « après » ou « tout de suite » ;
  « Tout reprendre » la relance. Pendant la pause, ce PC peut prêter sa puissance à mes
  machines.
- **Reprendre** : le travail retrouve son rang (sa date de création ne change pas).
- **Annuler** atteint aussi les autres machines : elles arrêtent leur calcul, et leur
  résultat, s'il arrive quand même, est refusé. Les résultats déjà rendus restent.
- **Quitter AIODOS** (menu de l'icône) pendant des calculs : une question d'abord. Les unités
  en cours sont rendues à la file sans compter d'essai et reprennent au prochain lancement.
- **Un redémarrage ne coûte rien** : les unités qui calculaient ici reviennent en file sans
  perdre d'essai ; celles confiées à une autre machine **gardent leur machine** pendant
  15 minutes (le temps qu'elle se raccroche, ou livre son résultat), au lieu d'être refaites.

## Les compteurs d'une unité

Seul un vrai échec du calcul compte un essai (`attempts`, 2 au plus). Le reste a ses propres
compteurs (`jobs/models.py`) :

| Compteur | Monte quand | Effet |
|---|---|---|
| `interrupts` | AIODOS s'arrête pendant son calcul ici | au-delà de 5, l'essai compte : une unité qui fait planter AIODOS finit en échec |
| `lost` | le bail d'une de mes machines échoit sans nouvelles | elle se raccroche si personne d'autre ne l'a prise ; à 2, plus jamais chez un ami ni un inconnu ; à 5, elle échoue |
| `handouts` | chaque remise, ici ou ailleurs ; jamais rendu | à 8, elle échoue : « Aucune machine n'a pu la finir » |

**Chez un ami, rien ne change** (règles de l'audit du 20/10) : un bail qu'il laisse échoir
compte l'essai, l'unité ne lui revient pas, et le dernier essai ne part jamais chez un ami.

## L'ordre de passage

Priorité du travail, puis **l'appli qui a le moins d'unités en cours** (un lot de 500 images
d'une appli ne bloque ni les autres, ni l'indexation d'AIODOS), puis l'ordre d'arrivée. Les
conversations passent toujours devant (la carte graphique leur est réservée pendant la
réponse).

## Les cercles d'une unité

Une unité peut porter les cercles où elle a le droit de se calculer (`units.reach` : `here`,
`mine`, `friends`, `public`), venus de l'accord de l'appli qui l'a déposée (L2b). Sans eux,
les règles d'avant. `["mine"]` sans `here` épargne ce PC.

## Le protocole entre machines

`work/renew` répond désormais `state` : `running`, `resumed` (le bail avait échu, la machine
se raccroche), `paused`, `cancelled` ou `reassigned`, en plus de `ok`, que lisent les
machines d'une version précédente (elles arrêtent sur `ok: false`, comme avant).

## Où c'est dans le code

- `aiodos/jobs/queue.py` : pauses (`pause_job`, `pause_all`), annulation, reprise au
  démarrage (`recover`), arrêt d'AIODOS (`interrupt_units`), équité (`fair_order`), cercles
  (`in_circle`).
- `aiodos/jobs/leases.py` : `renew` et ses états, `expire` et les disparitions, `take_back`
  qui refuse un travail annulé.
- `aiodos/jobs/executor.py` : arrêt des unités d'ici en pause « tout de suite », unités
  rendues à l'arrêt.
- `aiodos/network/work.py`, `worker.py` : `state` du renouvellement.
- `aiodos/ui/static/js/views/jobs.js` : pause « après » ou « tout de suite », tout mettre en
  pause ; `aiodos/ui/window.py` : la question à la fermeture.
- Migration 21 (`aiodos/db.py`) ; essais : `tests/test_jobs_torrent.py`.

## Ce qui viendra avec L2b

Les travaux de moteur des applis (ComfyUI), leur avancement estimé (`jobs/estimates.py`), la
boîte d'envoi chez le prêteur (un résultat attend que le propriétaire revienne), les délais
par cercle et la carte du lot.
