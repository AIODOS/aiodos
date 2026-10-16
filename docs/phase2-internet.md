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

### Ce que « à la façon de BitTorrent » veut dire

BitTorrent n'a pas de solution magique. Il empile : l'ouverture automatique d'un port
sur la box (UPnP, NAT-PMP, PCP), active par défaut dans libtorrent, qBittorrent et
Transmission ; le transport uTP sur UDP ; et le perçage de NAT de la BEP 55, où un pair
déjà connecté aux deux sert d'entremetteur. Ce perçage existe dans libtorrent (donc
qBittorrent) depuis 2013 environ, mais Transmission ne l'a reçu que le 23/08/2026, pas
encore dans une version publiée. Malgré cela, les mesures (anciennes) trouvent de 20 à
70 % de pairs injoignables, 39 % en moyenne dans une étude de 2010. L'essaim marche
quand même : les pairs injoignables se connectent *vers* les joignables, et un pair
qu'on ne joint pas est remplacé par un autre qui a les mêmes morceaux.

Pour Cœur IA :

- **en phase 2**, on veut joindre *une* machine précise, celle d'un ami : un échec
  n'est compensé par personne. Il faut donc faire mieux que BitTorrent, avec un perçage
  systématique et un relais de secours ;
- **en phase 3**, le modèle « pull » ressemble davantage à BitTorrent : une machine
  injoignable peut toujours calculer pour une machine joignable, puisque c'est elle qui
  ouvre la connexion ; l'inverse demande le perçage ou un relais.

## 2. Les candidats

*(recherche en cours : Iroh, autres pistes)*

### 2.2 libp2p : py-libp2p, ou rust-libp2p / go-libp2p en processus compagnon

libp2p est la boîte à outils réseau d'IPFS et d'Ethereum : identité par clé, DHT
Kademlia, GossipSub, relais, perçage de NAT (DCUtR).

- **py-libp2p 0.8.0** (26/09/2026, MIT + Apache-2.0) : une version tous les deux
  mois, une pile presque complète (TCP, QUIC, WebSocket, Noise, TLS, Yamux, Kademlia,
  GossipSub, relais v2, DCUtR, mDNS, réseau privé par clé partagée), interopérable avec
  go et rust sur TCP, WebSocket et QUIC. Mais :
  - classé « Beta », pas déclaré prêt pour la production ;
  - **ne s'installe pas sous Python 3.14** : `coincurve` est épinglé dans une version
    sans paquet pour 3.14, et sa compilation échoue (issue #1100, ouverte) ;
  - **bâti sur trio, pas sur asyncio** (les derniers usages d'asyncio ont été retirés
    en février 2026) : il faudrait faire cohabiter deux boucles d'événements avec
    FastAPI ;
  - perçage de NAT absent des tests d'interopérabilité ; AutoNAT en v1 seulement ;
  - une faille de déni de service (CVE-2026-73568, yamux) touchait les versions
    jusqu'à la 0.7.0.
- **Processus compagnon** : le démon officiel `go-libp2p-daemon` est presque en
  sommeil (v0.9.2 du 09/03/2026, bâtie sur un go-libp2p d'août 2024 et un quic-go
  vulnérables) ; hivemind, le réseau de Petals, reste sur un fork figé de 2023 et ne
  gère Windows que par WSL. Un petit compagnon maison sur **rust-libp2p** (0.57.1,
  15/10/2026, deux mainteneurs nommés) ou **go-libp2p** (0.50.0, 21/09/2026) serait la
  variante saine. L'avenir de go-libp2p est incertain : Shipyard, qui l'entretenait et
  exploitait les nœuds d'amorçage publics d'IPFS, a arrêté son travail IPFS le
  30/09/2026, Protocol Labs n'ayant pas renouvelé son financement ; aucun commit sur sa
  branche principale depuis le 21/09.
- **Traversée de NAT** : la meilleure mesure publique du domaine porte sur DCUtR
  (IPFS, 4,4 millions de tentatives dans 167 pays, déc. 2022 – janv. 2023, publiée en
  2025 et à IMC 2026) : **70 % ± 7,1 % de réussite**, TCP et QUIC à égalité, 97,6 % des
  réussites au premier essai, *une fois* la réservation de relais et la découverte de
  l'adresse publique réussies (29 % des cas sont écartés avant). Le relais v2 est
  **limité par conception** : par défaut, 2 minutes et 128 Kio par sens et par
  connexion. Il sert à coordonner le perçage, pas à transporter des unités de travail ;
  pour les échecs, il faut ses propres relais, sans ces limites.
- **Android** : un binaire go-libp2p tourne sous Termux (le paquet `kubo` y est à
  jour), mais go-libp2p plante à chaque connexion QUIC quand la table de routage est
  illisible, ce qui est le cas sur Android (#3537, correctifs non fusionnés). UPnP et
  NAT-PMP ne marchent plus depuis Android 12. rust-libp2p a deux bugs ouverts sous
  Android (WebSocket, DNS).
- **Licences** : MIT, Apache-2.0, BSD ; zeroconf (mDNS de py-libp2p) en LGPL-2.1+.
  Toutes compatibles avec la GPL-3.0.
- **Sécurité** : aucun audit tiers public trouvé ; des avis réguliers, surtout des
  dénis de service (côté rust : gossipsub et yamux en mars 2026, libp2p-quic en
  septembre 2026 ; quic-go de 2023 à 2026). La DHT publique d'IPFS reste attaquable :
  une attaque Sybil publiée en 2026 fait encore échouer environ 80 % des recherches sur
  Kubo.

**Verdict : la référence technique, la mieux mesurée, mais le chemin le plus long.**
py-libp2p n'est pas prêt pour nous (3.14, trio, bêta) ; un compagnon rust-libp2p
obligerait à assembler soi-même identité, relais sans limites, amorçage et découverte,
ce que d'autres fournissent tout faits.

### 2.3 Hyperswarm / HyperDHT (Holepunch)

La pile de Keet et de Pear : une DHT de type Kademlia où l'on s'annonce sous un
« sujet » de 32 octets, et des connexions chiffrées (Noise IK sur clés Ed25519)
ouvertes par perçage de NAT.

- **État** : hyperdht 6.34.1 (05/10/2026), hyperswarm 4.17.2 (22/09/2026), moteur
  JavaScript Bare 1.35.1 (13/10/2026). Éditeur : Holepunch, soutenu par Tether. Point
  fragile : un seul compte npm publie tout le cœur de la pile. À surveiller : le QVAC
  SDK de Tether (annoncé le 09/04/2026, Apache-2.0), bâti sur la pile Holepunch, qui
  fait de l'inférence déléguée entre appareils, très proche de Cœur IA.
- **Fonctionnement** : trois nœuds d'amorçage codés en dur (un chez Hetzner, deux chez
  DigitalOcean), remplaçables par option. Le NAT est classé (ouvert, cohérent,
  aléatoire) ; le perçage vise même un NAT « aléatoire » (rafales vers des ports au
  hasard pendant environ 35 s, ou 256 sockets façon « paradoxe des anniversaires »),
  mais **échoue par construction quand les deux côtés ont un NAT aléatoire**.
- **Relais** : `blind-relay` ne voit que du chiffré, mais **aucun relais public
  n'existe** : c'est à l'application d'en fournir un (la documentation de Pear conseille
  un serveur loué). Sans relais fourni, pas de repli.
- **Taux de réussite** : aucun chiffre publié, seulement des déclarations
  qualitatives. Des compteurs internes (`stats.punches`, `stats.relaying`)
  permettraient de mesurer chez nous.
- **Python** : rien de natif (un « KadePy » qui pilote un démon Node, un portage Rust
  qui se dit instable). Il faudrait un processus compagnon Node ou Bare avec un petit
  script JS : `bare.exe` pèse 45,9 Mo, `node.exe` 92,5 Mo ; les modules natifs existent
  précompilés pour Windows x64 et Android arm64 ; Node 24 et 26 existent sous Termux
  (non testé avec hyperswarm).
- **Licences** : MIT (hyperdht, hyperswarm, dht-rpc), Apache-2.0 (secret-stream,
  blind-relay, Bare), ISC : compatibles avec la GPL-3.0, en gardant les fichiers
  `NOTICE`. Holesail, un outil voisin, est en AGPL-3.0 : ne pas copier son code.
- **Sécurité** : on peut filtrer par clé avant toute réponse (`firewall`), mais
  l'adresse que reçoit ce filtre peut être falsifiée. L'IP est échangée avec les pairs
  (la FAQ de Pear conseille un VPN). Aucun avis de sécurité publié, pas de politique de
  sécurité, aucun audit public ; un rapport externe du 27/08/2026 (déni de service par
  inondation de poignées de main, réflexion UDP par les relais, contournement du filtre
  par IP, valeurs sans plafond de taille dans la DHT) **est resté sans réponse** et
  n'est pas corrigé dans la 6.34.1.

**Verdict : la meilleure ergonomie « à la BitTorrent »** (on s'annonce sous un sujet,
on se retrouve), **mais** un moteur JavaScript à embarquer à côté de Python, aucun
relais public, aucun taux mesuré, et des failles signalées restées sans réponse.

### 2.4 La DHT Mainline de BitTorrent et pkarr : un annuaire, pas un tuyau

La DHT Mainline est l'annuaire distribué de BitTorrent : des millions de nœuds, sans
serveur (15 à 27 millions par jour mesurés en 2011-2013 ; environ 10 millions selon
l'écosystème pkarr en 2025-2026, chiffre non vérifié de façon indépendante).
**pkarr** s'en sert pour publier, sous une clé publique Ed25519, un petit paquet DNS
signé (1000 octets au plus, élément mutable BEP 44), republié toutes les heures.
C'est exactement « où est la machine de Paul en ce moment ? », sans serveur.

- **État** : pkarr 8.1.0 (02/10/2026), licence MIT, très actif, mais son API casse
  souvent (v5 → v8 entre octobre 2025 et août 2026, deux versions retirées) ; le format,
  lui, est stable. Deux relais HTTP publics par défaut, pour qui ne peut pas parler à la
  DHT : `pkarr.pubky.app` et `pkarr.pubky.org`, du projet Pubky, à la même adresse IP.
  Le relais s'auto-héberge (`pkarr-relay` 2.1.0).
- **Durée de vie** : un enregistrement non republié disparaît en quelques heures. Une
  expérience de Pubky (2025) donne 39 % de survie à un jour pour une seule copie ;
  avec 20 copies, environ 100 % à un jour et 52 % à une semaine.
- **Python** : aucune liaison (Rust et JavaScript seulement). Les bibliothèques DHT en
  Python ne font que la BEP 5, pas la BEP 44 ; la liaison Python de libtorrent sait
  faire la BEP 44, mais PyPI n'en a pas pour Python 3.14 (Termux, si). Restent un petit
  processus compagnon, un relais HTTP, ou une implémentation maison de KRPC + BEP 44
  en asyncio (le protocole est simple). La bibliothèque Rust `mainline` ne parle plus
  qu'IPv4 depuis sa v5.
- **Ce n'est pas un transport** : pkarr dit où joindre une clé, pas comment traverser
  le NAT. Il faut l'associer à un transport ; c'est ce que fait Iroh, qui a intégré sa
  propre version du format (iroh 0.98, 17/04/2026) et sa propre copie de la DHT
  (`n0-mainline`).
- **Vie privée** : la DHT est un espace public et surveillé (environ 300 000 nœuds
  espions mesurés en 2011-2012 ; des services indexent qui télécharge quoi). Publier
  révèle son IP aux nœuds qui stockent l'enregistrement, et le contenu est lisible par
  quiconque connaît la clé. Ce qui trahirait « cette IP fait tourner Cœur IA » : une
  clé de rendez-vous commune et fixe, un format reconnaissable, une IP publiée en clair.
  Parades : une clé par nœud ou, entre amis, une clé dérivée chaque jour d'un secret de
  paire (seuls les amis savent où chercher) ; une valeur chiffrée ; publier l'adresse
  d'un relais plutôt que son IP. Iroh ne publie plus les IP par défaut depuis sa 0.97
  (16/03/2026).

**Verdict : le bon annuaire pour retrouver ses amis, à utiliser à travers le transport
qui l'intègre plutôt qu'en direct.** En phase 3, un annuaire public des nœuds Cœur IA
serait énumérable par construction : à n'ouvrir qu'avec l'accord explicite de chacun.

### 2.5 Pour comparaison : les réseaux privés virtuels type WireGuard

L'idée séduit : chaque machine reçoit une adresse IP virtuelle, et `coeur/1` marche
par-dessus sans changer une ligne. Elle se heurte au brief.

| Projet (version au 16/10/2026) | Point central | Windows | Android sans root | Inconnus | Licence |
|---|---|---|---|---|---|
| Headscale 0.29.5 (13/10/2026) + clients Tailscale 1.104.1 | **Oui** : serveur de contrôle public en HTTPS, un seul réseau ; relais DERP de Tailscale Inc. par défaut | pilote Wintun (droits admin) ; interface graphique propriétaire | app officielle (occupe le seul emplacement VPN du téléphone) ; démon sous Termux non fonctionnel sans root | non | Headscale, démon, app Android : BSD-3 ; serveur Tailscale et interface Windows : propriétaires |
| Nebula 1.11.2 (22/09/2026) | Pas de serveur de contrôle, mais une **autorité de certification** et des *lighthouses* à IP fixe | Wintun (admin) | app Mobile Nebula (dépôt sans fichier de licence) | non | MIT |
| NetBird 0.81.1 (16/10/2026) | **Oui** : serveurs de gestion, de signalisation et relais à héberger (VM publique + nom de domaine) | Wintun (admin) ; client Go embarquable sans admin | app (emplacement VPN) | non | client BSD-3 ; serveurs AGPL-3.0 depuis le 05/08/2025 |
| ZeroTier 1.16.2 (28/05/2026) | **Oui** : contrôleur + serveurs racines de ZeroTier Inc. | pilote TAP (admin) | app (emplacement VPN) ; Termux : root | non | agent MPL-2.0 ; contrôleur *source-available* non commercial depuis la 1.16 (2025) ; SDK libzt en BSL 1.1 |
| Yggdrasil 0.5.14 (19/06/2026) | Non : pairs publics remplaçables | Wintun, ou yggstack sans admin | yggstack (non vérifié sous Termux) | oui, mais **toutes les machines sont joignables** ; projet « alpha » | LGPL-3.0 + exception de liaison |

- **Un point central** : sauf Yggdrasil, il faut une autorité (serveur de contrôle,
  autorité de certification, contrôleur) que quelqu'un administre, et qui peut faire
  entrer n'importe quelle machine dans le réseau. Chaque cercle d'amis devient un réseau
  administré à part ; Headscale a refusé le partage de machines entre réseaux
  (29/11/2025).
- **Toute la machine exposée** : en mode interface réseau, tout ce qui écoute sur le PC
  (partage de fichiers, bureau à distance, Ollama s'il écoute sur `0.0.0.0`…) devient
  joignable par les autres membres. Headscale/Tailscale, NetBird et ZeroTier laissent
  tout passer par défaut ; Nebula bloque par défaut, mais contourne le pare-feu Windows
  depuis sa 1.11.
- **Installation lourde** : droits administrateur sous Windows (pilote réseau) ; sous
  Android, l'unique emplacement VPN du téléphone.
- **Pas pour des inconnus** : aucun n'est fait pour une adhésion libre (phase 3), sauf
  Yggdrasil, qui expose tout.
- **Ce qu'on en retient** : la traversée de NAT de Tailscale est la référence du
  domaine (sondes simultanées depuis le socket qui sert au trafic, relais DERP adressé
  par clé publique, relais par des pairs volontaires — *Peer Relays*, stables depuis le
  18/02/2026). Tailscale annonce des connexions directes dans « well north of 90% » des
  cas : chiffre interne, sans détail publié (billet du 15/10/2025). Les variantes
  embarquées en espace utilisateur (tsnet/libtailscale, NetBird `client/embed`,
  yggstack) n'exposent que les sockets de l'application et se passent de droits admin,
  mais elles sont en Go ; côté Python, seules les liaisons libtailscale 0.2.0 existent,
  à compiler soi-même, absentes de PyPI.

**Verdict : écarté comme socle.** Dépannage possible en attendant : un utilisateur qui a
déjà un réseau Tailscale ou Headscale devrait pouvoir ajouter une machine par son IP
virtuelle (« ajouter par adresse », `manual_peers`) et l'appairer avec le code à
6 chiffres lu au téléphone, sans rien coder (non testé). L'appairage et l'enveloppe
scellée restent alors indispensables : l'overlay est un tuyau, pas une preuve
d'identité.

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
