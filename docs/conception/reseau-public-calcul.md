# Le réseau public qui calcule : des inconnus répondent aux conversations

*Conception du 27/10/2026 au soir, à coder le 28/10. Suite de
[phase3-reseau-public.md](../phase3-reseau-public.md) (l'annuaire, 3.0) et de
[essaim.md](essaim.md) (étape 4 du § 13).*

## 1. Pourquoi, et ce qui est décidé

Le 27/10, sur le S23 en 4G, la conversation tournait sur le téléphone seul (qwen3.5:0.8b),
alors que le PC de l'auteur (qwen3.5:9b, RTX 4070) était dans l'annuaire public. Le réseau
public n'est aujourd'hui qu'un annuaire. Or **AIODOS sert d'abord à relier par Internet
les machines de personnes qui ne se connaissent pas**.

Décisions de l'auteur (27/10) :

1. Les conversations peuvent être **calculées par la machine d'un inconnu**, avec un
   **demandeur anonyme** : celui qui calcule lit la conversation, mais ne sait pas qui la
   demande. Cela révise la phase 3, § 4 (« Conversations : Non au début »).
2. **Prêter aux inconnus est activé par défaut**, après mes machines et mes amis, jamais
   sur batterie, dans la limite des places (60 % de la puissance, `node/places.py`).
3. La réponse d'un inconnu **s'affiche tout de suite**, marquée « réseau public » ; des
   **contrôles au hasard** la comparent à celle d'une autre machine, et une machine qui
   triche est mise de côté.

## 2. Ce qui existe, et ce qui manque

| | Aujourd'hui | À faire |
|---|---|---|
| Compagnon, nœud public | clé à part (`aiodos-public.key`), relais seulement, **gossip seul** : `PublicGate` refuse tout autre ALPN, à l'entrée comme à la sortie (public.rs) | ALPN de calcul `aiodos/public/1`, accepté de toute clé, avec des bornes |
| Compagnon, demandeur | aucun moyen de joindre un nœud public par sa clé | point de terminaison **jetable** par conversation, et une route locale qui le compose |
| Profil public | `{version, models, lends: false}` (codé en dur, public.py) | `lends` vrai si le prêt aux inconnus est permis, places libres, vitesse |
| Python, côté prêteur | toute conversation exige une machine appairée (`peers.get`, clé de la paire) ; `lending.chat` ne regarde ni `sharing_state` ni les places | une route publique, sans appairage, avec ses vérifications et ses limites |
| Python, côté demandeur | placement entre machines appairées ; cercle « public » non servi | machines publiques candidates, cercle « public », marque dans la conversation |
| Contrôles | réputation prévue (phase 3, § 5), rien de codé | contrôles au hasard, réputation par clé, mise de côté |

## 3. Une conversation, de bout en bout

1. **Choisir.** Le demandeur lit l'annuaire (profils reçus chaque minute, et dans les 5 s
   quand ils changent ; relu au compagnon s'il date de plus de 3 s) : machines qui
   prêtent, ont le modèle voulu (§ 6) et une place libre. Il les classe par vitesse mesurée
   (sa propre table, § 8), puis latence. Mes machines et mes amis d'abord, à vitesse
   comparable ; une machine publique bien plus rapide passe devant ce téléphone.
   **Relecture du 28/10** : seule la vitesse **mesurée ici** (au moins une réponse servie,
   depuis l'envoi de la demande jusqu'au dernier mot, 1 000 jetons/s au plus) fait passer une
   machine publique devant cette machine ; celle qu'elle annonce (« tps ») ne sert qu'à
   départager les machines publiques entre elles. Une machine jamais mesurée sert quand cette
   machine n'a pas le modèle, est occupée, ou pour un modèle plus gros accepté.
   **Contre la collusion (29/10 au soir)** : il faut en plus qu'elle soit **établie** ici
   (§ 8) ; à modèle égal, les machines établies passent devant les autres. « Connue depuis
   quelques minutes » (`known_s`) ne suffit pas : une identité coûte ~2 s de calcul (§ 10),
   et quelques minutes d'attente rien ; passer devant cette machine, libre et qui a le modèle,
   c'est recevoir toutes ses conversations de ce modèle. Coût accepté : sur un réseau encore
   petit, sans aucune de mes machines, ce téléphone répond lui-même, plus lentement, tant que
   la machine publique rapide n'est pas établie (elle sert toujours quand il est occupé, n'a
   pas le modèle, ou pour un modèle plus gros accepté).
2. **Se cacher.** Pour chaque conversation, le compagnon du demandeur crée une **clé
   jetable** et un point de terminaison Iroh **en relais seulement** : le prêteur voit la
   clé jetable et l'adresse du relais, jamais l'adresse ni la clé du demandeur. La clé
   est oubliée à la fin de la conversation.
3. **Envoyer.** Une connexion QUIC chiffrée de bout en bout (TLS d'Iroh, avec les deux
   clés) vers la clé publique du prêteur, ALPN `aiodos/public/1`. Le relais ne voit que du
   chiffré. La demande : le modèle, les messages, les options (température, longueur
   maximale), et les passages de mes bibliothèques seulement avec mon accord (§ 7) ;
   jamais de fichier.
4. **Admettre.** Le compagnon du prêteur borne avant tout (§ 5), puis relaie vers sa route
   locale `POST /public/v1/chat`, en disant la clé de l'appelant. Le Python vérifie : prêt
   aux inconnus permis, `sharing_state` actif, une place de conversation libre, modèle
   installé, demande dans les bornes. Rang dans la file des conversations prêtées : mes
   machines, puis mes amis, puis le public.
5. **Calculer et rendre.** Le moteur intégré calcule ; les morceaux repartent en flux par
   la même connexion. Rien n'est gardé chez le prêteur : ni la conversation, ni la clé de
   l'appelant au-delà des compteurs de limites (en mémoire, quelques minutes), et le
   journal ne dit que « une conversation pour le réseau public, 412 jetons, 9 s ».
6. **Afficher.** Le demandeur affiche la réponse dès le premier mot, marquée « Calculée par
   une machine du réseau public » avec le modèle. Si rien n'arrive dans le délai (premier
   mot), il passe à la machine suivante, comme aujourd'hui (`placement.run`). Tant que son
   moteur charge le modèle ou lit la demande, le prêteur envoie toutes les 5 s un signe de
   vie (`{"type": "wait", "stage": "loading" | "reading"}`, ignoré par une version d'avant) :
   le demandeur repousse alors son délai, 180 s au plus, et l'interface dit « La machine du
   réseau public charge le modèle… » (essai réel du 28/10 : 45 à 53 s de chargement, pour
   60 s d'attente). Toute la réponse, 10 min au plus depuis la demande ; au-delà, elle est
   coupée, le début gardé. Un échec du prêteur (des signes de vie puis rien, une réponse
   lente, coupée, illisible, trop longue, son moteur arrêté) l'écarte 10 min, en mémoire :
   ce n'est pas une preuve de triche, mais il a pu lire la conversation sans y répondre.

## 4. Ce que chacun voit

| Qui | Voit | Ne voit pas |
|---|---|---|
| Le prêteur | le texte de la conversation, le modèle, une clé jetable, l'adresse d'un relais | qui demande, son adresse, ses autres conversations |
| Le relais (n0 ou un relais choisi) | les adresses des deux côtés, la taille et le rythme des échanges | le contenu |
| Un observateur de l'annuaire | que tel nœud prête, ses modèles, ses places | qui demande quoi |

Limite dite à l'utilisateur : **le texte lui-même peut révéler qui l'écrit** (un nom, une
adresse dans la question). D'où un avertissement la première fois, et un interrupteur pour garder
une conversation sur mes machines (§ 7).

## 5. Les bornes du prêteur

Une clé jetable par conversation rend une limite par clé contournable : il faut aussi des
plafonds pour tout le public.

- **Par clé** (compagnon) : une conversation à la fois, 30 demandes par heure.
- **Pour tout le public** (compagnon et Python) : au plus une place de conversation sur
  celles de la machine, jamais plus que les places (60 %) ; au plus 120 demandes par minute
  en tout (seau de `gateway/limits.py`) ; au-delà, refus immédiat, que le demandeur
  traite comme « machine occupée » et passe à la suivante.
- **Par demande** : 256 Kio de requête au plus ; contexte borné (8 192 jetons), réponse
  bornée (2 048 jetons) ; texte seulement (ni image, ni fichier) dans cette première
  version.
- **Puissance prêtée** : après t s de calcul (depuis que le modèle est chargé, **lecture de
  la demande comprise** ; le chargement du modèle ne compte pas, à 5 s près, en trop plutôt
  qu'en moins), pause de t × (100 − X) / X. Relecture du 28/10 : comptée du premier jeton au
  dernier, elle laissait de côté la lecture, et ~8 000 jetons envoyés avec `max_tokens` = 1
  faisaient calculer la machine sans pause. Le profil (« free ») est
  redit au compagnon au début et à la fin de chaque conversation publique, et à la fin de
  la pause, sans attendre la synchronisation toutes les 15 s.
- **Le compagnon seul** : la route locale `/public/v1/chat` n'est ouverte qu'à une requête
  venue de cette machine, avec la clé jetable (`X-Aiodos-Public`) et un `Host` qui est une
  adresse IP en chiffres (boucle locale, ou l'adresse où elle arrive), comme le pose le
  compagnon : une page web qui se fait servir sous un nom résolu vers 127.0.0.1 (« DNS
  rebinding ») ne l'atteint pas.
- **Jamais** sur batterie ; un téléphone seulement en charge et en Wi-Fi (comme pour un
  ami) ; une conversation de l'utilisateur sur cette machine passe devant et arrête le prêt
  en cours, comme aujourd'hui.
- **Cadences du compagnon** (29/10, f1a0119, `public_serve.rs`, `cadence.rs`) : un flot de
  connexions ou de flux ne rend pas la machine indisponible à peu de frais.
  - **Nouvelles connexions** qui demandent `aiodos/public/1` : 20 d'un coup puis 10 par
    seconde, pour tous ; au-delà, **refusées avant la poignée de main** (le protocole est lu
    dans le premier paquet ; le groupe de discussion de l'annuaire ne compte pas). 32
    connexions au plus : une nouvelle prend la place d'une connexion jamais servie, puis de
    la plus anciennement servie, jamais d'une dont une requête avance.
  - **Flux cadencés** : ceux des connexions jamais servies, 40 d'un coup puis 20 par seconde,
    pour tous ; au-delà, 503 `busy` aussitôt, réponse toute faite, sans rien lire.
  - **Une conversation en cours est protégée** (une connexion déjà servie) : ses flux ont
    leur propre cadence (10 d'un coup puis 2 par seconde), jamais prise sur celle de tous ;
    une des requêtes à la fois et une part des relais de la minute lui restent (les
    connexions jamais servies, toutes ensemble, n'en ont qu'une, et 480 relayées par
    minute) ; le corps qu'elle envoie garde sa place de lecture face aux nouveaux venus ; elle
    ne cède sa place de connexion qu'après toutes celles qui n'ont jamais été servies.

## 6. Le modèle demandé

- **Un modèle choisi** par l'utilisateur : seules les machines qui ont ce modèle (nom
  exact) sont candidates.
- **Mode automatique** (décision de l'auteur, 27/10 au soir) : parmi les machines qui ont
  une place libre, **le meilleur modèle de conversation** (qwen3.5:9b plutôt que 0.8b),
  tant que le premier mot est attendu en quelques secondes ; sinon cette machine.
  - **Indiqué dans la conversation** : « Répondu par qwen3.5:9b, sur une machine du réseau
    public ».
  - **Accord demandé une fois** : la première fois qu'un modèle plus gros d'une autre
    machine pourrait répondre, une carte dans la conversation le propose (« Une machine du
    réseau public a un modèle plus gros. L'utiliser pour tes conversations ? » [Oui]
    [Non]). La réponse est gardée (réglage `public_bigger_model` : non demandé, oui, non)
    et se change dans les Réglages ; on ne redemande pas.
  - **Seulement un modèle contrôlable** (relecture du 28/10) : présent, au nom exact, chez
    au moins deux machines publiques distinctes qui prêtent, ou chez une de mes machines.
    Un nœud qui annonçait « qwen3.5:999b » passait toujours premier et, seul à porter ce nom,
    n'était jamais contrôlé. Une taille lue dans un nom au-delà de 1 000 milliards de
    paramètres est ignorée (aucune machine d'aujourd'hui n'en fait tourner davantage).
    **Depuis le 29/10 au soir** (collusion, § 8) : deux machines ne suffisent plus, deux nœuds
    neufs d'un même menteur rendaient leur modèle « contrôlable » l'un par l'autre. Il faut des
    machines qui prêtent de **trois groupes indépendants** au moins (`sightings.groups` : les
    machines apparues ensemble, de proche en proche, font un seul groupe ; trois groupes, c'est
    le prêteur et deux machines de contrôle indépendantes de lui et l'une de l'autre, quel que
    soit le prêteur), ou **une machine établie** qui l'a, ou une de mes machines.
  - **« Automatique · réseau public »** (`auto:*`, un téléphone sans modèle) : refusé, avec
    un message, si cette machine ou mes machines ont un modèle de conversation (une autre
    application pouvait l'envoyer et prendre le plus gros modèle public sans l'accord). Le
    meilleur modèle contrôlable d'abord, puis, à modèle égal, les machines établies. **Repli**
    quand rien n'est contrôlable (un réseau encore minuscule ; décision du 28/10, revue le
    29/10 au soir) : n'importe quel modèle, une machine **établie** d'abord, puis une qui a
    déjà répondu ici sans faute, puis la **plus ancienne** (connue depuis le plus longtemps :
    `known_s` du compagnon ou la première vue gardée ici, à un facteur deux près à partir de
    10 min : une identité neuve passe après), puis le plus gros modèle. Avant, la taille venait
    juste après « a déjà répondu » : un nœud neuf qui annonçait un gros modèle passait premier.

## 7. Ce qui part, ou non, chez un inconnu

- Les **passages de mes bibliothèques** (documents, mémoire des conversations) : **avec
  mon accord** (décision de l'auteur, 27/10 au soir). La première fois qu'une conversation
  qui s'appuie sur mes documents pourrait partir chez un inconnu, une carte le demande
  (« Cette réponse s'appuie sur tes documents. Envoyer les passages utiles à une machine du
  réseau public ? » [Oui] [Non]). La réponse est gardée (réglage `public_documents` : non
  demandé, oui, non), modifiable dans les Réglages. Sans accord, une telle conversation est
  calculée par mes machines (ou mes amis, comme aujourd'hui pour MINE seulement).
- **Sur mes machines seulement**, par conversation (décision du 27/10 au soir) : un
  interrupteur dans la conversation, désactivé par défaut ; activé, rien de cette
  conversation ne part chez un inconnu. L'avertissement de la première fois le signale.
- Jamais, dans cette version : les **pièces jointes**, les images, les fichiers.

## 8. Contrôles au hasard et réputation

- **Au hasard** (5 % des réponses publiques, et toujours la première fois qu'une machine
  sert ce demandeur), le demandeur envoie en arrière-plan la même demande à une autre
  machine (une des miennes si elle a le modèle, sinon d'autres machines publiques, qui
  votent : cf. « Collusion » plus bas), avec la même graine et une température de 0 pour
  les deux. Le prêteur ne peut pas savoir quelles demandes sont contrôlées.
- **Comparer** : les mêmes modèle, graine et réglages donnent presque le même texte, mais
  des cartes différentes (CUDA, Vulkan, processeur) divergent après quelques dizaines de
  jetons. On compare donc le début (les premiers jetons identiques) et le sens (similarité
  des embeddings de deux réponses entières), avec des seuils **mesurés** sur des paires
  honnêtes avant d'en faire une règle. Mesuré le 28/10 (`tools/bench_controle.py`) : le
  début commun ne prouve rien ; comparer les textes attrape une réponse sans rapport, un
  lien ou un numéro ajouté, une réponse coupée, mais ni **un autre modèle** ni une
  publicité sans lien.
- **Lire** (branché le 28/10) : la machine de contrôle, après sa réponse B, **lit A et B
  sous le même modèle** (`llm/reading.py` : une grammaire GBNF impose le texte, température
  0, log-probabilités de chaque jeton avant échantillonnage, puis celle de la fin de texte ;
  pas en flux, seule la réponse entière donne la fin de texte). Elle rend un résumé fait de
  nombres (jetons lus, moyenne, pire fenêtre de 16 jetons, fin de texte), lu par
  `spot_check.reading_verdict` : coupée, un autre modèle (A bien moins probable que B), un
  passage ajouté. Mesure : aucune erreur sur 40 paires honnêtes et 120 tricheuses lues ;
  essai réel sur qwen3.5:9b (moteur intégré) conforme. Coût : à peu près une génération
  par texte lu (pas moins : la grammaire d'un long texte pèse sur le processeur), en fond,
  jamais avant la réponse affichée. Chez mes machines, l'action « read » (mes machines
  seulement, `network/lend_reading.py`) ; chez une machine publique, `POST /public/v1/read`
  (même admission que `/public/v1/chat` : prêt permis, partage actif, une place, la pause
  de la puissance prêtée, rien de gardé ni au journal ; réponse en NDJSON avec signes de
  vie, le compagnon n'attendant le début d'une réponse que 120 s), redemandée toutes les 20 s
  si la machine est en pause après sa réponse, 15 min au plus. A et B partent dans un ordre
  tiré au hasard, sous l'identifiant de conversation de la réponse de contrôle (jamais
  celui de la conversation contrôlée), chez la même machine de contrôle (donc jamais chez
  un inconnu si la demande porte mes documents). Seul le moteur intégré sait lire : une
  machine sur Ollama refuse (`no_reading`), une version d'avant répond 404 ; le contrôle
  compare alors les textes seuls. Quand des machines de contrôle sont en désaccord, la
  suivante lit A, sa réponse et celles des machines d'avant.
- **Réputation** : une table locale par clé de prêteur (vitesse mesurée, contrôles réussis,
  ratés ou sans conclusion). Un contrôle raté met la machine de côté 7 jours ; deux, pour de
  bon. Rien n'est partagé avec les autres dans cette version (pas de liste noire commune,
  qu'un menteur pourrait remplir).
- **Branché** (28/10, `network/public_controls.py`) : chaque demande publique porte une
  graine tirée au hasard et la longueur maximale du prêteur (2 048 jetons), gardées pour
  le contrôle ; la température reste celle de la demande (aucune n'est imposée : les
  seuils ont été mesurés aussi sur des paires d'autres graines). À la fin d'une réponse
  complète et affichée : vitesse notée, puis contrôle en arrière-plan, chez mes machines
  d'abord (pas celles de mes amis, ni celle-ci, où l'utilisateur converse), puis chez les
  machines publiques qui ont le modèle (rangées depuis le 29/10 : « Collusion »), quatre
  demandées au plus ; une demande qui porte mes
  documents n'est contrôlée que par mes machines. Sans machine de contrôle, rien n'est
  lancé : la prochaine réponse de ce prêteur est encore « la première ». Une machine mise
  de côté n'est plus choisie ; la vitesse mesurée ici passe avant celle qu'elle annonce.
- **Relecture du 28/10** : la vitesse mesurée va de l'envoi de la demande au dernier mot (le
  temps total vu d'ici : un prêteur qui garde sa réponse pour l'envoyer d'un coup ne paraît
  pas plus rapide), bornée à 1 000 jetons/s et à la longueur demandée ; elle seule fait passer
  une machine publique devant cette machine (§ 3). Un échec sans preuve de triche écarte le
  prêteur 10 min, en mémoire (§ 3, étape 6), ni candidat ni machine de contrôle.
- **Refus après lecture** (28/10, `network/public_refusals.py`) : le prêteur reçoit la
  conversation avant de répondre ; un menteur pouvait la lire puis refuser « avant calcul »
  (`busy`, `not_lending`, `no_model`, `too_big`, `bad_request`), sans jamais être écarté. Le
  demandeur note chaque machine que son choix propose (son profil annonce le prêt, une place,
  le modèle) ; un tel refus reçu d'elle après l'envoi de la demande, dans les 2 min qui
  suivent ce choix, est compté (`too_big` et `bad_request` aussi : nos bornes, plus strictes,
  acceptaient la demande). Trois en 10 min : écartée 10 min comme après un échec. Un refus
  isolé (la place vient d'être prise) n'écarte rien ; la conversation passe toujours à la
  machine suivante.
- **Collusion** (29/10, `network/sightings.py`, `spot_check.py`, `reputation.py`,
  `public_controls.py`, migration 29) : une clé de nœud ne coûte rien ; un menteur qui tenait
  **deux nœuds** annonçant le même modèle se contrôlait lui-même (le modèle devenait
  « contrôlable », l'un répondait, l'autre lui donnait raison : le contrôle passait
  toujours). Plus largement, une machine de contrôle publique peut mentir dans le sens du
  prêteur, ou contre lui. Ce qui change :
  - **Premières vues** : chaque clé de l'annuaire est notée la première fois qu'on la voit,
    avec l'empreinte de sa liste de modèles : depuis le 29/10 au soir à **chaque lecture de
    l'annuaire** au compagnon (`public.py` : la synchronisation toutes les 15 s, la
    relecture du choix des machines), à 15 s près ; avant, seulement après une réponse
    publique complète et au choix des machines de contrôle (une machine là depuis des jours
    paraissait neuve à la première réponse). Le compagnon dit aussi depuis quand il connaît
    chaque nœud (`known_s`, gardé par `public.py`). Deux machines **apparues ensemble** :
    premières vues à moins de 6 h ; elles **se ressemblent** si, en plus, leurs listes de
    modèles sont identiques. Une clé jamais vue passe pour vue à l'instant.
  - **Établie** (`reputation.established`) : vue depuis un jour au moins, au moins un
    contrôle réussi compté par les règles d'aujourd'hui (une de mes machines, ou deux machines
    indépendantes), aucun raté, pas de côté. Les contrôles réussis d'avant le 29/10 (une seule
    machine publique suffisait) ne comptent pas.
  - **Choix des machines de contrôle** : mes machines d'abord ; puis les machines publiques
    établies ; puis celles qui ont réussi le plus de contrôles et sont vues depuis le plus de
    jours ; celles apparues en même temps que le prêteur en dernier ; la plus rapide à
    égalité. Une machine qui ressemble à une machine qui a déjà répondu passe après les
    autres (diversité).
  - **Qui conclut** : une de mes machines, crue seule (comme avant). Sinon les machines
    publiques votent (quatre demandées au plus) : **réussi** si deux machines d'accord avec
    le prêteur sont indépendantes de lui (pas apparues avec lui) et l'une de l'autre (elles
    ne se ressemblent pas), et plus nombreuses que celles qui le contredisent ; **raté** si
    deux machines qui ne se ressemblent pas lui donnent tort, plus nombreuses que celles qui
    lui donnent raison. Une seule machine d'accord : **sans conclusion** (ni réussi, ni
    raté). Une **faute flagrante** (sans rapport, lien ajouté, tronquée, un autre modèle à la
    lecture) compte sur l'avis d'une seule machine, à condition qu'elle soit établie et pas
    apparue avec le prêteur, et qu'aucune machine établie ne lui donne raison : une machine
    sans passé pourrait accuser un honnête. Une machine établie n'est jamais mise en minorité
    par des machines sans passé. Une machine de contrôle **seule contre deux** (la minorité,
    quand une majorité conclut) est prise en défaut si sa propre réponse ne ressemble à
    aucune des leurs (lue par la dernière machine, qui lit en cas de désaccord A, sa réponse
    et celles d'avant) ; deux machines en désaccord sans troisième : sans conclusion,
    personne n'est accusé (avant, la seconde tranchait seule : c'était justement la place
    d'un jumeau du prêteur).
  - « La première fois » dure jusqu'à un contrôle conclu, trois contrôles sans conclusion au
    plus (sans machine qui puisse conclure, chaque réponse serait refaite ailleurs pour rien).
  - **Ce que cela ne règle pas** : la collusion n'est **pas résolue** par ces mesures seules.
    Un menteur patient qui espace la création de ses nœuds (au-delà de 6 h, des listes de
    modèles différentes) les fait paraître indépendants ; pour être établi, il lui faut
    encore réussir un contrôle qu'il ne peut pas reconnaître, ce qui l'oblige à répondre
    honnêtement presque toujours. À la première observation d'ici (l'installation), toutes
    les machines déjà là paraissent apparues ensemble : elles ne se blanchissent pas entre
    elles ; seules mes machines, ou des machines arrivées ensuite, le peuvent. Sans aucune de
    mes machines (un téléphone seul), rien ne devient établi par le seul réseau public tant
    que deux machines indépendantes n'ont pas été d'accord. L'annuaire noyé de clés neuves
    remplit aussi la table des premières vues (20 000 lignes ; pleine, elle n'efface pas les
    anciennes). La parade de fond est ailleurs : le **compagnon** rend les identités publiques
    coûteuses (preuve de travail sur la clé, faite le 29/10 : § 10), et le choix des machines
    s'en sert (ci-dessous).
  - **Le choix des machines** (29/10 au soir, `placement.py`) : un modèle n'est
    « contrôlable » que chez trois groupes indépendants, une machine établie ou une de mes
    machines (§ 6) ; à modèle égal, les machines établies d'abord ; seule une machine établie
    passe devant cette machine (§ 3) ; le repli « rien de contrôlable » prend une machine
    établie, puis une qui a bien servi, puis la plus ancienne, avant la taille (§ 6).

## 9. Ce qu'on ajoute, fichier par fichier

**Compagnon** (`compagnon/src/`, cargo depuis PowerShell) :
- `public.rs` : l'ALPN `aiodos/public/1` dans `.alpns` et `PublicGate` (toute clé), un
  `.accept(ALPN_PUBLIC, …)` au Router ; l'`Endpoint` gardé dans `Session`.
- `public_serve.rs` (nouveau) : relais d'une connexion entrante vers la route locale, avec
  la clé de l'appelant, bornes par clé et globales (sur le modèle de `pool.rs`,
  `relay_body.rs`).
- `public_ask.rs` (nouveau) : point de terminaison jetable par conversation (relais
  seulement), route locale `POST /net/v1/public/ask/<clé>` qui relaie la demande et le
  flux de la réponse.

**Python** :
- `aiodos/network/public.py` : `lends` d'après le réglage `lend_public` ; places libres et
  vitesse dans le profil ; l'annuaire gardé comme source de candidats.
- `aiodos/network/public_lending.py` (nouveau) : la route `/public/v1/chat` (compagnon
  seulement, jeton local), ses vérifications, son rang dans la file.
- `aiodos/network/public_chat.py` (nouveau) : `chat_stream` vers une machine publique par
  le compagnon.
- `aiodos/network/placement.py` : candidats publics (identifiant `public:<clé>`), ordre du
  § 3.
- `aiodos/network/spot_check.py` (nouveau) et `aiodos/network/reputation.py` (nouveau,
  table SQLite) : § 8 ; `aiodos/network/public_refusals.py` (nouveau, en mémoire) : les
  refus « avant calcul » qui contredisent le profil, § 8.
- `aiodos/llm/reading.py` (nouveau, la lecture imposée), `aiodos/network/lend_reading.py`
  (nouveau : l'action « read » entre mes machines et `POST /public/v1/read`),
  `public_chat.read`, `remote_models.read`, `public_controls.read` : la vraisemblance, § 8.
- `aiodos/gateway/scopes.py`, `chat/answer.py` : le cercle « public » servi et affiché.
- Réglages : `lend_public` (prêter mes conversations aux inconnus, **activé**),
  `use_public` (faire calculer mes conversations par des inconnus, **activé**),
  `public_bigger_model` et `public_documents` (non demandé, oui, non : § 6, § 7) ;
  l'interrupteur « Sur mes machines seulement » par conversation ; les cartes d'accord ;
  textes fr et en ; avertissement la première fois.
- `aiodos/legal` : charte et conditions **version 3**.

**Essais** : unités Python et Rust ; de bout en bout, deux nœuds sur ce PC (dossiers de
données séparés), réseau public seul, l'un demande, l'autre répond, contrôle au hasard
forcé.

## 10. Hors de cette version

Vidéos pour les inconnus (les images : § 11) ; pièces jointes ; réputation partagée ;
paiement ou crédit (phase3-incitation.md).

**Fait le 29/10 : la preuve de travail sur la clé du nœud public** (compagnon,
`public_identity.rs`, f1a0119). SHA-256(« aiodos/public/pow » ‖ clé publique) doit commencer
par **19 bits nuls** ; le compagnon la cherche une fois, sur tous les cœurs : ~2 s sur un PC
(300 000 à 360 000 essais par seconde mesurés), **7 à 15 s estimées sur un téléphone** (1 fois
sur 20, de 20 à 45 s), une seule fois (la clé est gardée ; une clé gardée sans preuve est
remplacée). Pendant ce temps, l'état dit `preparing` et la vue Réseau « Préparation de
l'identité publique… (une seule fois) » ; le nœud rejoint le réseau de lui-même ensuite. Une
clé sans preuve n'entre ni dans l'annuaire ni parmi les voisins ; l'annuaire admet 30 nouveaux
venus par minute et ne chasse personne. **1 000 identités coûtent ~26 min d'un PC** à pleine
charge : un flot d'identités devient coûteux, pas impossible.

**Ce qui reste** :
- **Une carte graphique** génère et hache des clés bien plus vite qu'un processeur
  (probablement des centaines de fois, comme les générateurs d'adresses personnalisées : à
  mesurer) ; la difficulté ne peut pas monter beaucoup sans pénaliser les téléphones. La
  preuve borne le flot, elle ne le rend pas impossible.
- **La collusion d'un menteur patient** n'est pas résolue (§ 8, « Ce que cela ne règle
  pas ») : des nœuds créés à plus de 6 h d'écart, avec des listes de modèles différentes,
  paraissent indépendants ; ils peuvent rendre un modèle contrôlable (trois groupes) et se
  donner raison dans un vote. Pour être établi, chacun doit encore réussir un contrôle qu'il
  ne reconnaît pas, donc répondre honnêtement presque toujours ; et seule une machine établie
  passe devant cette machine.

## 11. Images (29/10/2026)

**Pourquoi.** L'auteur a installé AIODOS sur son PC (Z-Image Turbo) et sur son téléphone
(aucun modèle d'images), sans les appairer : ils ne se voient que par le réseau public. Le
téléphone répondait « Aucun modèle d'images ne peut calculer en ce moment, ni ici ni chez tes
machines en ligne ». **Décision de l'auteur** : les images passent aussi par le réseau public,
comme les conversations, avec un **accord demandé une fois** ; « Sur mes machines seulement »
vaut aussi pour elles. C'est la phase 3, § 4 (demandeur anonyme, la machine qui calcule lit la
description et voit l'image), sauf l'accord : une fois, et non à chaque création.

**Côté prêteur** (`network/public_imaging.py`) : `POST /public/v1/image`, relayée par le
compagnon comme une conversation (aucun changement du compagnon). Réglage à part,
`lend_images_public` (« Prêter ma carte graphique aux images d'inconnus », **activé** par
défaut), en plus de `lend_public`, mêmes conditions : partage actif, jamais sur batterie, un
téléphone seulement en charge et en Wi-Fi ; **une seule place pour tout le public**
(conversation, lecture ou image) ; après l'utilisateur, mes machines et mes amis (qui
arrêtent le prêt en cours) ; pause de la puissance prêtée (le chargement du modèle ne compte
pas). Bornes : un modèle d'images du catalogue (pas une vidéo, pas de licence restrictive),
complet ici ; description de 2 000 caractères au plus ; côtés au multiple et dans les bornes
du modèle, **1 024 × 1 024 pixels au plus** ; de 1 à deux fois les pas du modèle (16 pour
Z-Image Turbo) ; graine entière ; **pas d'image de départ** dans cette version ; le guidage est
celui du modèle. Refus avant tout calcul, sans rien prendre : `not_lending` (503), `no_model`
(404), `too_big` (413), `bad_request` (400), `busy` (503). Réponse en NDJSON : `meta`, signes de
vie toutes les 5 s (`"stage": "loading"` puis `"computing"`), puis `{"type": "image",
"format": "png", "bytes", "width", "height", "seconds"}`, le PNG en morceaux base64 de
192 Kio (`part`), et `done` ; ou `error` (`busy`, `not_lending`, `engine`). PNG plutôt que
WebP : la galerie, les vignettes et les retouches attendent un PNG, et Pillow n'est pas
toujours là chez le demandeur (1 à 2 Mo par image, 6 Mio au plus).

**Rien de gardé** : sd-server rend l'image en mémoire (base64) ; rien n'est écrit sur le
disque par AIODOS ; ni la description, ni l'image, ni la clé ne restent après la réponse ; le
journal dit le modèle, la taille, les pas et les durées. Hors d'AIODOS : sd-server garde sa
tâche finie en mémoire jusqu'à son arrêt (1 min 30 après la dernière image), et son journal
(`data/logs/sd-server.log`, niveau « info ») dit la taille et la graine, pas la description
(vérifié sur le journal de ce PC).

**Le profil public** : le profil signé du compagnon n'a que `version`, `models`, `lends`,
`free`, `tps` (un champ inconnu ne passerait pas) : les modèles d'images prêtés vont dans
`models`, marqués `image:` (`image:z-image-turbo`), en dernier, seulement si
`lend_images_public` est permis (un modèle annoncé puis refusé compterait comme un refus
suspect). `image:` rejoint les préfixes réservés (`placement.RESERVED`) : jamais pris pour un
modèle de conversation. Un nœud d'avant le 29/10 pourrait, en « Automatique · réseau public »,
en choisir un s'il n'en voit pas d'autre chez cette machine (refus `no_model`).

**Côté demandeur** : quand ni cette machine ni mes machines (ni mes amis, si je les y
autorise) ne peuvent calculer l'image (`chat/creations.py`), `chat/public_creations.py`
regarde le réseau public : les règles de `public_consent.closed` (« Faire calculer mes
conversations par des inconnus », réseau public rejoint, pas « Sur mes machines seulement »,
pas d'image jointe), une machine qui prête ce modèle avec une place, et l'accord
`public_images` (non demandé, oui, non). **Non demandé** : la création s'arrête sur une carte
dans la conversation (« La description de ton image part chez la machine d'un inconnu, qui
voit aussi l'image créée, sans savoir qui tu es. Créer tes images par le réseau public ? »
[Oui] [Non]) ; Oui la relance ; la réponse est gardée et se change dans Réglages → Cette
machine → Partage de puissance. Contrairement aux cartes des conversations, elle précède la
création : rien d'autre ne pourrait la faire en attendant. Des amis qui le proposeraient,
sans mon autorisation, passent d'abord (le message d'avant, avec son bouton), sauf si j'ai déjà
dit oui au réseau public. Sinon, le message « Aucun modèle d'images… » dit ce qui l'en empêche
quand une machine du réseau public pourrait la créer (réglage, accord refusé, « Sur mes
machines seulement », image jointe), ou qu'il n'y en a pas non plus.

La création devient un travail de la file comme les autres, marqué `public` : une unité
`generate_image_public` par image (`jobs/kinds/public_images.py`), **de cette machine
seulement** (jamais confiée à une de mes machines, qui n'a ni l'accord ni ce compagnon), sur
la ressource « net ». Avant chaque envoi, l'accord est relu (réglages, « Sur mes machines
seulement » de la conversation). Les machines candidates (`network/public_image_ask.py`) :
celles qui prêtent, ont une place (moins ce que je viens de leur envoyer) et ce modèle, pas
mises de côté ni écartées ; celles qui ont déjà servi ici sans contrôle raté d'abord, puis
la plus rapide qu'elles annoncent, la plus récemment vue ; trois au plus, la suivante si la
première échoue. **Une clé jetable par création** (un identifiant tiré au hasard pour le
lot). Ce qui part : modèle, description, consigne négative, taille, pas, graine ; jamais
d'image de départ (une retouche se refait depuis sa description), ni rien de la conversation.
La description est **enrichie ici** (`chat/enrich.py` : le modèle de conversation de cette
machine), jamais chez un inconnu ; sans modèle ici, elle part telle quelle.

**Vérifier ce qui revient** : le modèle de `meta`, l'en-tête (PNG, 6 Mio au plus, la taille
demandée), les morceaux (jamais plus que les octets annoncés), `done` avec exactement ces
octets, puis le PNG (signature, en-tête, taille, fin `IEND` ; avec Pillow, décodé en entier,
**pas d'une seule couleur**, réécrit sans ses métadonnées). Délais : 60 s pour le premier
événement, chaque signe de vie repousse l'échéance de 20 s, 10 min en tout. **Pas de
contrôle au hasard des images dans cette version** (la phase 3, § 5 le prévoit : refaire une
image ailleurs et comparer, SSIM ≥ 0,60) : une image autre que celle demandée, sur le bon
sujet, n'est donc pas vue. Mais un prêteur qui renvoie autre chose qu'une image valide, trop
grosse, lente, coupée, ou dont le moteur s'arrête, est écarté 10 min (`reputation.bench`),
comme pour une conversation ; ses refus « avant calcul » répétés après avoir annoncé une place
l'écartent de même (`public_refusals`) ; une image valide compte comme une réponse servie.

**Dans la conversation** : l'image est gardée comme une création d'ici, sa machine marquée
« Créée par une machine du réseau public » (galerie, visionneuse, sous l'image) ; pendant le
calcul, « Création par une machine du réseau public ». Le mode « Créer » propose le modèle
« · réseau public » quand une machine le prête (sauf « Sur mes machines seulement », ou
l'accord refusé).

**Hors de cette version** : les vidéos ; une image de départ (retouche, image jointe) ; le
contrôle au hasard des images ; un modèle à licence restrictive.

**Essais** : `tests/test_public_imaging.py` (le prêteur derrière le vrai serveur entre
machines avec un faux moteur d'images ; le demandeur par un faux compagnon qui relaie au
prêteur ou répond ce qu'on lui dicte ; l'accord, la création et son unité de bout en bout).
