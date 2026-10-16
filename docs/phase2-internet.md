# Relier des nœuds Cœur IA par Internet (étude préalable aux phases 2 et 3)

> Étude du 16/10/2026, **en cours de rédaction**. Rien n'est implémenté : ce document
> prépare des choix, dont plusieurs reviennent à l'utilisateur (cf.
> [§ 6](#6-questions-à-trancher-gouvernance-et-confidentialité)).

## En bref

*(à compléter quand la comparaison sera terminée)*

## 1. Le problème

En phase 1, deux nœuds se trouvent par une annonce UDP diffusée sur le réseau local
([`discovery.py`](../coeur/network/discovery.py)), puis se parlent en HTTP direct
(`http://ip:port/p2p/v1/…`, [`client.py`](../coeur/network/client.py)). Sur Internet,
rien de cela ne marche :

- **pas de diffusion** : une annonce UDP ne franchit pas la box ;
- **pas d'adresse joignable** : chez soi, le PC est derrière la box (NAT en IPv4,
  pare-feu en IPv6), qui refuse les connexions entrantes ; sur un téléphone en 4G/5G,
  l'opérateur ajoute souvent un second NAT (CGNAT) que personne ne peut ouvrir ;
- **des adresses qui changent** : IP de la box, du téléphone, d'un portable qui passe
  du Wi-Fi au partage de connexion.

Il faut donc quatre briques, que les solutions étudiées assemblent plus ou moins :

1. **Une identité indépendante de l'adresse** : la clé publique du nœud. C'est elle
   qu'on appelle, et c'est elle que le chiffrement authentifie.
2. **Une découverte** : retrouver les adresses actuelles d'une clé (« où est la machine
   de Paul en ce moment ? ») ; en phase 3, trouver aussi des machines inconnues qui
   offrent du calcul.
3. **La traversée de NAT** (*hole punching*) : les deux machines s'envoient des paquets
   au même moment, ce qui ouvre un passage dans chaque box. Ça marche souvent, pas
   toujours (cf. § 3).
4. **Un relais en dernier recours** : une machine joignable qui fait passer les paquets,
   chiffrés de bout en bout, quand le passage direct échoue.

*(§ « à la façon de BitTorrent » : à compléter)*

## 2. Les candidats

*(recherche en cours : Iroh, libp2p, Hyperswarm/HyperDHT, DHT Mainline + pkarr,
réseaux privés type WireGuard, autres pistes)*

## 3. Traversée de NAT : ce que disent les mesures

*(recherche en cours)*

## 4. Recommandation

*(à rédiger)*

## 5. Partir de l'existant : ce qu'on garde, ce qui change

Le protocole « coeur/1 » a été pensé pour le réseau local, mais plusieurs de ses choix
sont déjà ceux qu'il faut sur Internet. Le principe directeur : **le transport change,
le protocole de travail reste**. Les routes `/p2p/v1/…`, l'enveloppe scellée, les baux
et les tâches portables passent telles quelles dans un tuyau qui sait traverser les NAT.

### Ce qu'on garde

| Élément | Où | Pourquoi il reste |
|---|---|---|
| **Enveloppe scellée** : ChaCha20-Poly1305 avec la clé de la paire ; données associées liant protocole, action, émetteur, destinataire et horodatage ; anti-rejeu | [`envelope.py`](../coeur/network/envelope.py), [`crypto.py`](../coeur/network/crypto.py) | Deuxième couche, indépendante du transport : un relais, un tunnel ou une bibliothèque réseau mal configurée ne voit jamais un contenu en clair. |
| **Appairage avec un geste humain des deux côtés** | [`pairing.py`](../coeur/network/pairing.py) | La confiance ne s'établit jamais automatiquement. Le *canal* change (cf. plus bas), pas le principe. |
| **Modèle « pull » avec baux** de 90 s renouvelés toutes les ~30 s ; unité rendue si l'utilisateur revient ; résultat en retard accepté tant que l'unité n'a pas été redonnée | [`leases.py`](../coeur/jobs/leases.py), [`work.py`](../coeur/network/work.py), [`worker.py`](../coeur/network/worker.py) | C'est exactement ce qu'il faut sur Internet : les coupures et les machines qui disparaissent sont déjà prévues, et c'est la machine qui calcule qui ouvre la connexion. |
| **Tâches portables prédéfinies** : calcul n'importe où, intégration chez le propriétaire ; aucun code venu d'ailleurs | [`registry.py`](../coeur/jobs/registry.py) | Première protection d'une machine qui calcule pour d'autres (BOINC, lui, télécharge les programmes des projets). À ne jamais abandonner. |
| **Exigence du même modèle à l'empreinte près** (digest Ollama) | `work.can_serve` | Indispensable pour que des vecteurs calculés ailleurs soient comparables. |
| **Bibliothèque « gardée sur ce PC »** | `store.local_only_ids`, `units.local_only` | Devient le premier cran d'une portée à plusieurs niveaux (cf. § 6). |
| **Versions explicites** : `coeur/1`, `WORK_API` | `envelope.PROTOCOL`, `work.WORK_API` | Un protocole ouvert et versionné : on passe à `coeur/2`, on ne bricole pas `coeur/1`. |

### Ce qui change

| Aujourd'hui | Où | Sur Internet |
|---|---|---|
| Identité `node_id = uuid4().hex`, tirée au hasard | [`identity.py`](../coeur/node/identity.py) | **Identité = clé publique Ed25519** : c'est l'adresse du nœud sur Internet, et le transport l'authentifie. La clé privée devient le secret le plus précieux du nœud (à protéger, par exemple avec DPAPI sous Windows). Migration : les paires existantes échangent leurs nouvelles clés par un message scellé avec leur clé de paire actuelle, sans ré-appairage. |
| Machine jointe à `peers.address:port`, mise à jour par les annonces UDP | [`peers.py`](../coeur/network/peers.py), [`manager.py`](../coeur/network/manager.py) | **On joint une clé, pas une adresse.** IP directes et relais deviennent des indices mis en cache, retrouvés par la découverte. |
| Découverte par diffusion UDP (`255.255.255.255:47700`) | [`discovery.py`](../coeur/network/discovery.py) | **Gardée pour le réseau local** (ou remplacée par la découverte locale du transport). Sur Internet, pas de découverte d'inconnus en phase 2 : on ajoute ses amis par invitation, puis on retrouve leurs adresses par une publication signée (cf. § 2). |
| Appairage : code à 6 chiffres comparé sur les deux écrans ; `pair/start` ouvert à toute machine du réseau local (3 demandes en attente au plus) | [`pairing.py`](../coeur/network/pairing.py), [`p2p_app.py`](../coeur/network/p2p_app.py) | Les deux écrans ne sont plus côte à côte. **Invitation** : un lien ou un QR code contenant la clé publique de l'invitant, un secret à usage unique et une date d'expiration, transmis par un canal que les deux amis jugent sûr (en personne, messagerie chiffrée). `pair/start` exige ce secret : un inconnu qui connaît la clé ne peut pas déclencher de demande. Le code à 6 chiffres reste possible par téléphone ou en visio ; il faudra alors le dériver aussi des deux clés publiques (liaison au canal), pour qu'un intermédiaire ne puisse pas relayer l'appairage. |
| Serveur entre nœuds FastAPI sur `0.0.0.0:47701`, en HTTP (contenus scellés) | [`p2p_app.py`](../coeur/network/p2p_app.py) | **Jamais exposé tel quel sur Internet** (pas de redirection de port ni d'UPnP vers lui) : il passe derrière le transport P2P, qui ne laisse entrer que des clés connues (liste blanche) et les porteurs d'une invitation valide. Le code des routes ne change pas. |
| Appels `requests.post("http://ip:port/p2p/v1/…")`, délai 10 s | [`client.py`](../coeur/network/client.py) | Même requête, autre chemin (tunnel local ou flux du transport). Délais à revoir : un passage par relais ajoute de la latence. |
| Clé de paire statique : qui la vole peut déchiffrer tout trafic enregistré | `crypto.pair_secrets` | Sur Internet, le trafic peut être enregistré en route. La **confidentialité persistante** (*forward secrecy*) viendra du transport (TLS 1.3 ou Noise, à clés éphémères) ; prévoir aussi une rotation de la clé de paire. |
| Fichier de l'unité envoyé en base64 dans du JSON, par morceaux de 2 Mio, jusqu'à 512 Mio, contrôlé par SHA-256 | `work.blob`, `worker._download` | **Flux binaire** (le base64 coûte un tiers de volume en plus), reprise après coupure, plafond par défaut bien plus bas : le débit montant d'une box et un forfait mobile ne sont pas ceux d'un réseau local. |
| Contrôle des sorties : forme, valeurs finies, dimension | `index.check_embedded` | Suffisant entre ses propres machines ; **insuffisant face à un tricheur** : un vecteur plausible mais faux passe. Cf. § 6.3. |
| Réciprocité : secondes de calcul **déclarées par le calculateur** (plafonnées à 24 h par unité) | `work.result`, `peers.account` | Acceptable entre amis ; un inconnu peut gonfler son compte pour être servi en premier. Cf. § 6.4. |
| Ce qui part avec une unité : le fichier, mais aussi son **nom**, son **chemin dans la bibliothèque** (`source`) et l'identifiant de la bibliothèque | `work.lease` (seul `path` est retiré) | Métadonnées à réduire hors de ses propres machines (nom neutre, extension seule). |
| Profil échangé : **nom de la machine** (nom d'hôte Windows par défaut), GPU, modèles installés avec empreintes, **temps d'inactivité** | `identity.profile`, `worker.capabilities` | Le nom d'hôte contient souvent un prénom ; l'inactivité dit quand on est devant son PC. À réduire selon le niveau de confiance (cf. § 6.2). |
| Nouvelles de chaque machine appairée toutes les 15 s (profil toutes les 60 s) | `manager._refresh_loop` | Suffisant pour quelques amis ; un réseau public demandera des abonnements plutôt qu'un sondage. |

## 6. Questions à trancher (gouvernance et confidentialité)

Ces questions sont posées, **pas tranchées**. Pour chacune : ce qui existe aujourd'hui,
les options, ce qu'elles coûtent. Les questions marquées **[phase 2]** bloquent
l'ouverture aux amis ; les autres peuvent attendre la phase 3.

### 6.1 Qui peut confier quoi, à qui ?

Aujourd'hui, être appairé, c'est la confiance totale : la machine appairée reçoit les
fichiers en clair, peut demander du travail et voit le profil complet. C'était fait pour
*mes* machines ; un ami n'est pas mon deuxième PC.

- **Q1 [phase 2]** — Faut-il des niveaux de confiance distincts ? Par exemple *mes
  machines* / *amis* / *inconnus*, avec des droits différents : recevoir mes fichiers,
  me confier du travail, utiliser mes modèles de chat, voir mon profil.
- **Q2 [phase 2]** — Portée par défaut d'une bibliothèque. Aujourd'hui tout est
  confiable aux machines appairées sauf ce qui est « gardé sur ce PC » (cadenas). Chez un
  ami, faut-il inverser le défaut (rien ne part sans accord explicite, par bibliothèque
  ou par travail) ?
- **Q3 [phase 2]** — Dans l'autre sens : qu'accepte-t-on de calculer pour un ami, pour
  un inconnu ? Types de tâches (embeddings, lecture d'images, transcription, chat),
  plafonds (heures par jour, Go par jour, part du GPU — ce dernier existe déjà),
  conditions (inactivité et secteur existent déjà).
- **Q4** — Le chat servi par une autre machine (étape C de la phase 1) : la machine qui
  sert le modèle lit les questions et les réponses. Entre amis ? Jamais avec des
  inconnus ?

### 6.2 Quelles données peuvent partir ?

Aujourd'hui, la machine qui calcule **voit le fichier de l'unité en clair** : il est
chiffré pendant le voyage, mais il faut bien le déchiffrer pour en extraire le texte et
calculer les vecteurs. Elle voit aussi son nom et son chemin dans la bibliothèque. Elle
l'efface après le calcul, mais rien ne permet de le vérifier.

- **Q5 [phase 2]** — Quelles catégories de données peuvent partir chez un ami ? Chez
  un inconnu ? Une piste pour les inconnus : seulement les pages web publiques, que le
  calculateur télécharge lui-même ; seule l'adresse part, ce qui révèle quand même ce
  qui vous intéresse.
- **Q6 [phase 2]** — Métadonnées : nom de fichier, chemin, nom de la machine, modèles
  installés, temps d'inactivité. Que montre-t-on à un ami, à un inconnu ?
- **Q7** — Faut-il un journal consultable de « ce qui est parti, chez qui, quand » ?
- **Q8** — Se connecter directement à quelqu'un lui révèle son adresse IP (donc une
  localisation approximative). Pour les inconnus, faut-il passer toujours par un relais,
  au prix du débit ?

### 6.3 Vérifier les résultats

Aujourd'hui, une sortie venue d'ailleurs est contrôlée (forme, valeurs finies,
dimension). Un calculateur malveillant peut quand même renvoyer des vecteurs plausibles
mais faux, ou un texte extrait falsifié : la bibliothèque est alors empoisonnée, et le
chat citera de mauvais passages.

- **Q9 [phase 2]** — Entre amis : confiance (contrôles actuels) ou contrôle par
  échantillon (le propriétaire recalcule lui-même un passage tiré au hasard) ?
- **Q10** — Avec des inconnus : quel surcoût accepter (calculer chaque unité deux fois,
  façon BOINC) ? Que faire d'une machine prise en faute : l'exclure chez soi seulement,
  ou prévenir les autres (c'est déjà un système de réputation, et il s'attaque) ?
- **Q11** — Les tâches difficiles à vérifier (transcription, description d'image,
  réponse de chat) : exclues pour les inconnus ?

### 6.4 Crédits et réciprocité

Aujourd'hui, chaque machine tient son propre compte (temps donné et reçu, par machine)
et sert d'abord celles qui ont le plus calculé pour elle. Rien n'est global ; le temps
est déclaré par celui qui a calculé.

- **Q12** — La réciprocité reste-t-elle strictement entre deux machines (comme le
  donnant-donnant de BitTorrent), ou se transmet-elle (amis d'amis, reçus signés
  présentables à d'autres) ? La transmission aide les nouveaux venus mais ouvre la porte
  aux fausses identités en nombre (attaque Sybil).
- **Q13** — Faut-il une part de générosité : servir un nouveau venu qui n'a encore rien
  donné, et dans quelle proportion ?
- **Q14** — Unité de compte : temps déclaré, unités validées, ou coût estimé par le
  propriétaire (nombre de passages, durée d'audio…) ?
- Rappel du brief : **pas de jeton ni de blockchain** dans le cœur.

### 6.5 Ouverture du réseau

- **Q15** — Phase 3 : réseau ouvert à tous (comme un torrent public), ou cercles sur
  invitation (amis d'amis, communautés) ? Qui peut exclure qui ?
- **Q16 [phase 2]** — Infrastructure : accepte-t-on par défaut des relais publics gérés
  par un tiers (remplaçables, mais gérés par quelqu'un d'autre) ? Un relais hébergé par
  le projet ? Par un membre du cercle qui a une IP publique ? Aucun relais (connexion
  directe ou rien) ?
- **Q17** — Responsabilité : calculer pour un inconnu, c'est lire ses fichiers (peut-être
  illicites, ou contenant des données personnelles de tiers, ce qui relève du RGPD) et
  générer du texte sur sa machine. Faut-il une charte, des tâches exclues ?
- **Q18** — Sécurité de la machine qui calcule : ouvrir les fichiers d'un inconnu expose
  aux failles des lecteurs (PDF, Office, images, audio) et du moteur de modèles. Bac à
  sable obligatoire avant toute ouverture au public ?
- **Q19** — Téléphone : peut-il prêter sa puissance sur données mobiles, sur batterie ?

## 7. Plan par étapes

*(à rédiger après la recommandation)*

## Sources

*(à compléter)*
