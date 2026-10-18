# Phase 3 : le réseau public, plan technique

Écrit dans la nuit du 18/10/2026, à la demande de l'auteur (« tu peux enchaîner sur la
phase 3 »). Il complète [phase3-incitation.md](phase3-incitation.md), qui étudie déjà les
récompenses, les précédents et le cadre légal. Ici : comment des machines qui ne se
connaissent pas se trouvent sans serveur central, ce qu'on peut leur confier, comment
vérifier leur travail, et dans quel ordre le construire.

**Rien n'est codé de la phase 3 avant ton accord** : plusieurs choix sont de la
gouvernance (§ 9). Ce qui est déjà en place et resservira est signalé.

## 1. Ce que « public » voudrait dire

- **Un choix explicite**, désactivé par défaut, comme « Prêter ma puissance » : « Rejoindre
  le réseau public ». Sans lui, rien ne change : mes machines et mes amis seulement.
- **Deux sens séparés**, chacun avec son interrupteur :
  - *calculer pour des inconnus* (donner) ;
  - *faire calculer par des inconnus* (recevoir).
  On peut donner sans jamais rien confier.
- **Ce qui part chez un inconnu est marqué « public » exprès** : une bibliothèque ouverte
  à tous (le niveau `everyone`, déjà prévu par la migration 7), une image dont la
  description peut être lue. Jamais une conversation, jamais une bibliothèque gardée sur ce
  PC ou partagée avec mes amis, jamais un morceau de modèle découpé.

## 2. Se trouver sans serveur central

Il faut un annuaire que personne ne possède. Le plus grand qui existe est la **DHT
Mainline de BitTorrent** : des millions de nœuds, sans propriétaire, utilisée par les
clients BitTorrent pour trouver des pairs sans tracker. Proposition en deux étages :

1. **Le rendez-vous par la DHT.** Chaque nœud public s'annonce (BEP 5, `announce_peer`)
   sous une clé de sujet fixe, par exemple le SHA-1 de « aiodos/public/1 », et demande
   les autres (`get_peers`). La DHT ne rend que des adresses IP et des ports, et chaque
   nœud n'en rend qu'une partie au hasard : assez pour amorcer, pas pour tout connaître.
2. **Le groupe de discussion du compagnon.** Une fois quelques pairs trouvés, le
   compagnon (Iroh) rejoint un sujet de gossip (`iroh-gossip`, arbres de diffusion
   HyParView et PlumTree) : chaque nœud y annonce son profil public (sa clé, ce qu'il sait
   calculer, s'il prête en ce moment), et l'annonce se propage. Un sujet unique tient
   « quelques milliers de pairs » selon la documentation d'Iroh ; au-delà, des sujets par
   modèle ou par région.

Pour joindre un nœud trouvé, son adresse ne suffit pas : Iroh se connecte à une **clé**
(l'EndpointId, Ed25519). Le profil publié dans le gossip donne cette clé ; et Iroh sait
résoudre une clé par la même DHT (« DHT address lookup », option `discovery-pkarr-dht`,
désactivée par défaut), sans passer par les serveurs DNS de n0. Les relais restent ceux
de n0 par défaut, ou un relais auto-hébergé ([relais.md](relais.md)) : un relais ne voit
que des paquets chiffrés.

**À vérifier avant d'écrire du code** (règle du brief : vérifier les bibliothèques P2P) :
le crate `mainline` (MIT, version 8.0.1, mainteneurs de Pubky) gère `get_peers`,
`announce_peer` et les éléments signés (BEP 44) ; l'API synchrone est dépréciée au profit
de l'API asynchrone, ce qui convient au compagnon (tokio). Il faudra mesurer le temps
d'un premier rendez-vous (quelques secondes pour la DHT, d'après les mesures de 2.2 :
2,6 s pour résoudre une clé) et le comportement derrière un NAT de box ou en 4G.

**Ce qui existe déjà** : le compagnon et sa clé Ed25519, ses relais, la DHT pour résoudre
les clés (mesurée en 2.2), le protocole scellé entre nœuds.

## 3. L'identité d'un inconnu

- **La clé du compagnon est l'identité** : c'est elle que le transport authentifie. Le
  `node_id` actuel reste un nom interne, lié à la clé dans le profil.
- **Un profil public signé** par cette clé : nom choisi (jamais le nom de la machine par
  défaut), version, modèles qu'il sait calculer (avec leurs empreintes), s'il prête en ce
  moment. Rien du matériel au-delà du nécessaire, rien de l'alimentation ni de l'activité
  (comme le profil allégé des amis).
- **Pas d'appairage, pas de code à 6 chiffres** : un inconnu n'entre pas dans la table
  des machines appairées. Il a sa propre table (clé, première vue, réputation), et le
  compagnon l'accepte sur un protocole à part (ALPN `aiodos/public/1`), limité en débit,
  qui n'offre que les actions publiques.

## 4. Ce qu'on peut confier à un inconnu, et calculer pour lui

| Travail | À un inconnu | Pour un inconnu | Pourquoi |
|---|---|---|---|
| Indexation d'une bibliothèque publique (passages → vecteurs) | Oui | Oui | Unités indépendantes, contenu public, résultat vérifiable (§ 5) |
| Images et vidéos à description publique | Oui, avec l'accord de chaque création | Oui (réglage à part) | Unités indépendantes ; la description est lue par la machine qui calcule |
| Conversations (modèle prêté) | Non | Non au début | Le modèle lit toute la conversation ; une réponse ne se vérifie pas à bas prix |
| Gros modèle découpé | Non | Non | Chaque tranche peut fausser le résultat sans que personne le voie |
| Recherche dans une bibliothèque consultable par tous | — | Oui (si ouverte à tous) | Seuls des passages publics sortent |

Calculer pour un inconnu se fait dans les mêmes conditions que pour un ami (inactivité,
secteur, température, part du GPU), en dernier dans la file : mes machines, puis mes
amis, puis le public.

## 5. Vérifier le travail d'un inconnu

- **Indexation** : le résultat est presque déterministe pour un même modèle (même
  empreinte). Mesuré : 0,9999 de similarité entre le PC (carte graphique) et le S9
  (processeur) ; le contrôle actuel des amis exige 0,999 (`jobs/kinds/index.py`). Pour un
  inconnu :
  - **nouveau venu** : chaque unité confiée à deux machines (la sienne et une machine
    sûre, ou deux inconnus de réputation différente), comme BOINC ; un désaccord, et ses
    résultats sont jetés ;
  - **une fois connu** : un échantillon recalculé (5 %, puis 1 %), comme pour les amis.
- **Images** : mesuré le 18/10 ([phase3-verification-images.md](phase3-verification-images.md)).
  Le même moteur refait une image au pixel près ; deux moteurs différents (Vulkan et
  CUDA, ou la carte et le processeur) donnent la même scène, pas les mêmes pixels. D'où :
  la taille, le format et le contenu toujours (une image unie ou du bruit se voient sans
  rien recalculer) ; pour un nouveau venu, une image sur cinq refaite ailleurs, égale au
  pixel près si le moteur est le même, sinon proche (SSIM ≥ 0,60 : honnête ≥ 0,73,
  tricheries grossières ≤ 0,42). Une image faite en 4 pas au lieu de 8 ne se voit
  qu'avec le même moteur.
- **Réputation** : un score par clé, calculé ici (jamais reçu d'ailleurs) : unités
  rendues, unités vérifiées, écarts. Plus il est haut, moins on vérifie ; un écart le
  remet à zéro et met la clé de côté ; plusieurs écarts l'excluent. On peut le préparer
  dès maintenant pour les amis (fiabilité affichée dans « Machines connectées »).

## 6. Fausses identités (attaque Sybil)

Rien n'empêche de créer mille clés. Les réponses, de la plus simple à la plus lourde :

- **Un inconnu ne pèse rien au début** : il ne reçoit que des unités doublées (§ 5), et
  peu à la fois. Mille clés neuves ne rapportent que mille fois rien.
- **Ce qu'il reçoit dépend de ce qu'il a donné** : la réciprocité souple (déjà en place
  entre amis, `network/lend_queue.py`) devient la règle avec le public, avec une petite
  avance pour démarrer.
- **La confiance de mes amis** : une clé que mes amis ont déjà vue travailler bien
  pourrait démarrer plus haut. Cela suppose d'échanger des avis entre amis : à décider
  (§ 9), et à garder simple.

## 7. Incitation

Voir [phase3-incitation.md](phase3-incitation.md) : des **crédits visibles et non
monnayables** qui donnent la priorité, d'abord. Le compte des échanges existe (temps de
calcul et conversations donnés et reçus, par machine) et sert déjà à l'ordre de passage.
Pour le public, il faudrait des **reçus** : quand un inconnu me rend une unité vérifiée,
je lui signe un reçu (la clé de l'unité, le temps de calcul, la date). Ces reçus ne
servent à rien tant qu'ils restent chez lui ; ils deviennent utiles si quelqu'un d'autre
peut les vérifier, par exemple pour lui donner la priorité. C'est la seule pièce qui
pourrait un jour aller vers un registre partagé, ou une blockchain (principe n° 8, « à
revoir en phase 3 ») : à comparer avec des reçus échangés entre pairs, sans registre.

## 8. Étapes proposées

1. **3.0 Annuaire, sans rien confier.** Le compagnon s'annonce dans la DHT et rejoint le
   gossip public ; la vue Réseau montre combien de nœuds publics sont en ligne et ce
   qu'ils savent calculer. *Fini quand* : deux AIODOS sur deux réseaux différents se
   voient sans invitation, et un nœud sans « Rejoindre le réseau public » n'annonce rien.
2. **3.1 Donner.** Calculer pour des inconnus : indexation de bibliothèques publiques, avec
   les conditions du prêt, en dernier dans la file. Rien n'est confié aux inconnus.
3. **3.2 Recevoir, avec vérification.** Confier à des inconnus l'indexation d'une
   bibliothèque publique : unités doublées pour les nouveaux, échantillons ensuite,
   réputation.
4. **3.3 Crédits et reçus** : affichés, donnant la priorité.
5. **3.4 Images publiques**, après des mesures de vérification (§ 5).
6. **Plus tard** : registre partagé ou blockchain, si les reçus seuls ne suffisent pas.

Ce qui peut se faire dès maintenant sans rien décider : la **fiabilité** de chaque
machine (unités rendues, vérifiées, écarts), utile aussi entre amis.

## 9. Questions pour toi

1. **Le nom affiché** d'un nœud public : un pseudonyme choisi à l'activation (recommandé),
   ou rien du tout (une suite de lettres tirée de la clé) ?
2. **Images pour les inconnus** : seulement à description publique, création par
   création (recommandé), ou jamais ?
3. **Les avis entre amis** sur des clés inconnues (§ 6) : à garder pour plus tard
   (recommandé), ou dès le début ?
4. **Les relais** : ceux de n0 par défaut pour le public aussi (simple), ou un relais
   AIODOS auto-hébergé à prévoir (plus indépendant, mais il faut l'héberger) ?
5. **Blockchain** : comparer d'abord les reçus signés entre pairs, sans registre
   (recommandé), avant toute chaîne ?

## Sources

- iroh-gossip : [documentation d'Iroh, gossip](https://docs.iroh.computer/connecting/gossip/)
  (amorçage par des pairs connus, « scales to a few thousand peers »),
  [docs.rs](https://docs.rs/iroh-gossip)
- Résolution des clés par la DHT : [DHT discovery](https://docs.iroh.computer/connecting/dht-discovery)
  (option `discovery-pkarr-dht`, désactivée par défaut),
  [address lookup](https://docs.iroh.computer/concepts/address-lookup)
- DHT Mainline : [crate `mainline`](https://docs.rs/mainline/latest/mainline/) (MIT,
  8.0.1 ; BEP 5 et BEP 44), [méthodes de `Dht`](https://docs.rs/mainline/latest/mainline/struct.Dht.html)
- Vérification et précédents : [phase3-incitation.md](phase3-incitation.md)
