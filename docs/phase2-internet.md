# Relier des nœuds AIODOS par Internet (étude préalable aux phases 2 et 3)

> Étude du 16/10/2026. Ce document prépare des choix, dont plusieurs reviennent à
> l'utilisateur (cf. [§ 6](#6-questions-à-trancher-gouvernance-et-confidentialité)) ;
> leur mise en œuvre suit le plan du [§ 7](#7-plan-par-étapes) (étapes 2.0 à 2.7 faites,
> 2.3 en partie ; ce qui reste est noté à chaque étape).
> Les faits sur les projets étudiés datent de ce jour et vieilliront vite : les
> revérifier avant d'agir.

## En bref

- **Oui, c'est possible sans serveur central obligatoire.** Chaque machine est
  désignée par sa clé publique et se fait trouver par un annuaire distribué ; les deux
  machines percent leurs box pour se parler en direct. Pas toujours : pour 10 à 30 %
  des paires, et bien plus souvent avec un téléphone en 4G/5G, il faut un **relais**.
  Il ne voit que du chiffré et n'importe qui peut l'héberger, mais qui le fournit reste
  à décider (Q16).
- **Recommandation : Iroh 1.x** (n0, MIT/Apache, stable depuis juin 2026), **utilisé
  par un petit programme compagnon en Rust** que lance le nœud Python. Il fournit d'un
  bloc l'identité par clé, l'annuaire, le perçage de NAT et le repli par relais ; il
  tourne sous Windows et sous Termux ; il sert déjà à partager des GPU entre machines
  personnelles (Mesh LLM, Buzz de Block). Ses liaisons Python officielles existent,
  mais elles sont en retard (version vulnérable, pas d'Android) : pas pour l'instant.
- **Écartés** : py-libp2p (pas de Python 3.14, bâti sur trio, encore en bêta) ;
  Hyperswarm (moteur JavaScript à embarquer, aucun relais public, rapport de sécurité
  resté sans réponse) ; la DHT Mainline et pkarr, qui ne sont qu'un annuaire (Iroh sait
  s'en servir) ; les VPN type WireGuard (un serveur central, et toute la machine
  exposée) ; Reticulum, dont la licence interdit les usages liés à l'IA.
- **Le protocole actuel reste** : enveloppes chiffrées, appairage confirmé par un
  humain, modèle « pull » avec baux, tâches prédéfinies. Changent : l'identité (une clé
  publique), l'appairage à distance (une invitation), le transport, et la protection du
  serveur entre nœuds, qui n'est plus jamais exposé directement.
- **Un contexte favorable** : la France est première mondiale pour l'IPv6 (87 % des
  utilisateurs de Google), ce qui simplifie beaucoup les connexions directes. Un
  téléphone en 4G/5G reste injoignable de l'extérieur : c'est toujours lui qui doit
  ouvrir la connexion.
- **Ce qu'on ignore** : aucune mesure indépendante du taux de connexion directe
  d'Iroh, aucun audit de sécurité. D'où la première étape : **mesurer chez nous avec
  dumbpipe, sans écrire de code**.
- **À trancher par l'utilisateur** : 20 questions de gouvernance et de confidentialité
  (§ 6), dont 7 bloquent la phase 2. Fait central : en 2026, une machine ne peut pas
  calculer sur des données sans les voir. La confidentialité repose donc sur le choix
  des machines et sur ce qu'on accepte d'envoyer.

## 1. Le problème

En phase 1, deux nœuds se trouvent par une annonce UDP diffusée sur le réseau local
([`discovery.py`](../aiodos/network/discovery.py)), puis se parlent en HTTP direct
(`http://ip:port/p2p/v1/…`, [`client.py`](../aiodos/network/client.py)). Sur Internet,
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

Pour AIODOS :

- **en phase 2**, on veut joindre *une* machine précise, celle d'un ami : un échec
  n'est compensé par personne. Il faut donc faire mieux que BitTorrent, avec un perçage
  systématique et un relais de secours ;
- **en phase 3**, le modèle « pull » ressemble davantage à BitTorrent : une machine
  injoignable peut toujours calculer pour une machine joignable, puisque c'est elle qui
  ouvre la connexion ; l'inverse demande le perçage ou un relais.

## 2. Les candidats

Évalués le 16/10/2026 sur des sources primaires (dépôts, versions publiées, avis de
sécurité, documentation, articles) ; ces faits vieilliront vite.

### 2.1 Iroh (n0)

Iroh relie des *endpoints* identifiés par une clé publique Ed25519 : on compose une
clé, pas une adresse IP (« Dial keys, not IPs »). Dessous : QUIC (TLS 1.3 avec clés
publiques brutes), perçage de NAT, et des relais qui servent à la fois de point de
rendez-vous et de repli chiffré.

- **État** : 1.0 publiée le 15/06/2026 après 65 versions ; dernière version 1.3.0
  (28/09/2026). La série 1.x promet que tout endpoint 1.x parle à tout endpoint 1.x, et
  qu'une future 2.x restera compatible avec la 1.x sur le réseau ; la politique de
  support est écrite. Très actif (88 commits en trois mois). Porté par n0 (une dizaine
  de personnes, capital-risque et fondateurs), qui vit de la vente de relais hébergés
  (à partir de 19 $ par mois). Les protocoles annexes (iroh-blobs pour les fichiers,
  iroh-gossip pour la diffusion, iroh-docs) sont encore en 0.x.
- **Fonctionnement** : QUIC via noq, un fork de Quinn par n0 qui gère plusieurs chemins
  dans une même connexion. Le relais, l'IPv4 directe et l'IPv6 directe sont des
  chemins : la connexion démarre par le relais et bascule en direct dès que le perçage
  réussit, sans que l'application le voie. Relais en HTTPS/WebSocket (port 443).
  Ouverture de port automatique (UPnP, NAT-PMP, PCP) activée par défaut. Annuaire par
  défaut : un enregistrement signé façon pkarr (clé → relais du nœud, sans les IP)
  publié sur le serveur DNS de n0 (`dns.iroh.link`) ; en option, la DHT Mainline (sans
  serveur) et le mDNS pour le réseau local.
- **Traversée de NAT** : n0 annonce environ 9 connexions sur 10 en direct et 95 à 99 %
  du volume ; sa propre documentation qualifie ce chiffre d'« anecdotique », et aucune
  mesure indépendante n'existe. Un NAT « dur » côté serveur n'est géré que depuis mai
  2026 (1.0.0-rc.1), et seulement si un seul des deux côtés est dans ce cas.
- **Sans serveur central ?** Par défaut, Iroh s'appuie sur l'infrastructure de n0
  (quatre relais publics et le DNS). Mais tout s'auto-héberge, sous la même licence :
  `iroh-relay` (binaire Windows de 3,7 Mo, ou Docker ; il lui faut une IP publique et
  un nom de domaine, il gère seul ses certificats), `iroh-dns-server`, ou la DHT
  Mainline à la place du DNS. Les relais publics de n0 sont réservés au développement
  et aux usages amateurs (limites non publiées, appelées à se durcir).
- **Python** : les liaisons officielles sont revenues avec la 1.0 (paquet `iroh` 1.1.0
  du 16/07/2026, asyncio, roue Windows x64). Mais elles embarquent iroh 1.0.2, touché
  par un avis de sécurité « high » corrigé en 1.1.0 ; rien n'a été publié depuis
  juillet ; pas de paquet Android ; la CI ne teste que Python 3.12 ; l'API se limite
  aux flux et datagrammes (ni DHT, ni mDNS, ni annuaire personnalisé). Les outils en
  ligne de commande de n0 sont prêts, eux : `dumbpipe`, un « netcat » sur Iroh qui
  transporte une connexion TCP (3 Mo pour Windows), et `sendme` (transfert de
  fichiers), **tous deux empaquetés dans Termux**. Mais dumbpipe est câblé sur les
  relais et le DNS de n0, sans liste blanche de pairs. Pour les autres langages, la
  documentation de n0 suggère elle-même un petit programme compagnon qui expose Iroh
  en local.
- **Plateformes** : Windows x64 de première classe (les bugs Windows de 2026 sont
  corrigés). Android est géré en Rust, Kotlin et Node ; un binaire nu sous Termux
  demande des précautions de compilation (résolution DNS par JNI, restrictions
  netlink), que la recette Termux de dumbpipe applique déjà.
- **Licence** : MIT OR Apache-2.0, comme AIODOS. Point à surveiller :
  `webpki-roots` (CDLA-Permissive-2.0), licence dont la FSF ne parle pas.
- **Sécurité** : pas d'audit tiers. En 2026, des failles corrigées vite : déni de
  service du relais avant authentification (1.0.2), trois avis côté client (1.1.0),
  écriture non autorisée dans iroh-blobs (0.103.1). Un relais voit qui est connecté,
  qui parle à qui, quand, combien, et les adresses IP, pas le contenu. On peut refuser
  une clé avant la fin de la poignée de main ; un relais auto-hébergé accepte une liste
  blanche de clés ou un jeton. Pas de spécification formelle du protocole 1.0 (un
  brouillon IETF individuel sur la traversée de NAT, juillet 2026) : c'est le code qui
  fait foi.
- **Qui s'en sert pour notre cas** : Mesh LLM (Apache-2.0, février 2026) mutualise les
  GPU de machines personnelles derrière une API compatible OpenAI, sans serveur central
  sur le chemin des données ; Buzz, de Block (Apache-2.0), l'intègre pour partager la
  puissance inutilisée des membres d'une communauté. Aussi Holochain (depuis juin 2026)
  et Delta Chat. Tous l'utilisent en Rust.

**Verdict : le meilleur candidat**, à condition de l'utiliser depuis Rust (un programme
compagnon) et de configurer relais et annuaire pour ne dépendre de personne (cf. § 4).

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
- **Licences** : MIT, Apache-2.0, BSD ; zeroconf (mDNS de py-libp2p) en LGPL-2.1+,
  utilisable par AIODOS (Apache-2.0) comme bibliothèque à part, sans copier son code.
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
  fait de l'inférence déléguée entre appareils, très proche d'AIODOS.
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
  blind-relay, Bare), ISC : compatibles avec l'Apache-2.0 d'AIODOS, en gardant les
  fichiers `NOTICE`. Holesail, un outil voisin, est en AGPL-3.0 : ne pas copier son code.
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
  quiconque connaît la clé. Ce qui trahirait « cette IP fait tourner AIODOS » : une
  clé de rendez-vous commune et fixe, un format reconnaissable, une IP publiée en clair.
  Parades : une clé par nœud ou, entre amis, une clé dérivée chaque jour d'un secret de
  paire (seuls les amis savent où chercher) ; une valeur chiffrée ; publier l'adresse
  d'un relais plutôt que son IP. Iroh ne publie plus les IP par défaut depuis sa 0.97
  (16/03/2026).

**Verdict : le bon annuaire pour retrouver ses amis, à utiliser à travers le transport
qui l'intègre plutôt qu'en direct.** En phase 3, un annuaire public des nœuds AIODOS
serait énumérable par construction : à n'ouvrir qu'avec l'accord explicite de chacun.

### 2.5 Autres pistes

- **Faire soi-même en Python** : aioice 0.10.2 (ICE, avec STUN et TURN, en asyncio)
  et aioquic 1.6.1 (QUIC, qui laisse l'application fournir la socket : il peut tourner
  sur celle que l'ICE vient de percer). Licence BSD-3, maintenus, paquets binaires
  pour CPython 3.14 sous Windows. aiortc (WebRTC complet) impose PyAV et FFmpeg
  (environ 28 Mo) même pour de simples canaux de données. Le perçage lui-même tient en
  quelques centaines de lignes ; le difficile est ailleurs : classer les NAT, ouvrir les
  pare-feu IPv6 des box (UPnP avec async-upnp-client 0.49 ; aucune bibliothèque Python
  maintenue pour PCP), les messages de maintien, le relais. Il faut aussi des serveurs
  STUN (258 publics recensés, dont Google et Cloudflare) et TURN (de 0,05 à 0,40 $ par
  Go chez les fournisseurs, ou un coturn chez un ami). **C'est le plan de secours**, si
  le transport retenu échoue quelque part (Termux, par exemple).
- **Tor (services onion)** : le seul à joindre n'importe quelle machine derrière
  n'importe quel NAT sans relais à exploiter soi-même, en masquant les IP. Mais environ
  5 Mbit/s et 280 ms de latence médiane (Tor Metrics, août-septembre 2026) ;
  l'hébergement de services onion avec Arti 2.7.0 est encore annoncé « testing only »,
  et le côté Python est fragile (stem n'est plus maintenu, l'API RPC d'Arti est
  expérimentale). Une option de vie privée pour la phase 3, pas un socle.
- **Veilid** 0.5.7 (MPL-2.0) : traversée de NAT intégrée (signalisation, connexion
  inverse, relais), mais un démon `veilid-server` à faire tourner à côté, sans
  procédure Windows documentée.
- **I2P / i2pd** 2.61.0 (BSD-3) : son transport échoue face aux NAT symétriques, et la
  bibliothèque Python (i2plib) n'est plus maintenue depuis 2019. Yggdrasil : cf. § 2.6.
- **Reticulum : exclu.** Depuis le 15/04/2025, sa licence (passée du MIT à une
  « Reticulum License ») interdit tout usage qui contribue à l'entraînement ou au
  développement de modèles d'IA : ce n'est plus une licence libre, et elle vise
  directement un projet comme AIODOS.

### 2.6 Pour comparaison : les réseaux privés virtuels type WireGuard

L'idée séduit : chaque machine reçoit une adresse IP virtuelle, et `aiodos/1` marche
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

| Source | Méthode | Résultat |
|---|---|---|
| Ford, Srisuresh et Kegel (2005) | 380 NAT testés par des volontaires | 82 % se laissent percer en UDP, 64 % en TCP |
| Halkes et Pouwelse (Tribler, IFIP Networking 2011) | réseau P2P réel | plus de 80 % des tentatives réussies |
| Trautwein et al. (DCUtR dans IPFS ; arXiv 2025, IMC 2026) | 4,4 millions de tentatives, 167 pays, déc. 2022 – janv. 2023 | **70 % ± 7,1 %**, *une fois* le relais et l'adresse publique obtenus ; TCP et QUIC à égalité ; 97,6 % des réussites au premier essai |
| Tailscale (billet du 15/10/2025) ; n0 pour Iroh (documentation, 2025-2026) | métriques internes, méthode non publiée | environ 90 % de connexions directes ; n0 qualifie lui-même son chiffre d'« anecdotique » (affirmations d'éditeurs) |

Ce qui fait échouer, ce sont les NAT dont la correspondance de ports dépend de la
destination (dits « symétriques »), fréquents derrière les CGNAT et sur mobile. Quand
un seul côté est dans ce cas, deviner son port façon « paradoxe des anniversaires »
coûte peu (256 ports ouverts : 64 % de réussite ; 2 048 sondes : 99,9 %) ; quand les
deux le sont, c'est impraticable (de l'ordre de 28 minutes à 100 paquets par seconde).
**Un relais reste indispensable** pour 10 à 30 % des paires selon les mesures, sans
doute davantage avec un téléphone en 4G.

### Le contexte français

D'après le baromètre de l'Arcep publié en juillet 2026 (données de fin 2025) et les
statistiques de Google au 14/10/2026 :

- **l'IPv6 change la donne.** La France est première mondiale : 87,3 % des
  utilisateurs de Google, 75,6 % selon la médiane de l'Arcep (mars 2026). Fin 2025,
  94 % des clients fixes et 83 % des clients mobiles l'ont. Fixe : Free 99 %, Orange
  97 %, Bouygues 96 %, SFR 80 %. Mobile : Bouygues 97 %, Orange 92 %, SFR 86 %, Free
  Mobile 50 %. En IPv6, pas de traduction d'adresse : chaque côté connaît l'adresse
  exacte de l'autre, et le perçage à travers des pare-feu à état est trivial ;
- **l'IPv4 est partagée presque partout** : tout le mobile (CGNAT chez les quatre
  opérateurs, sans IPv4 dédiée possible) ; presque tout Free sur le fixe (MAP-E : seuls
  4 % des abonnés fibre ont une IPv4 entière, l'option « Full Stack » est gratuite) ;
  30 % de Bouygues sur le fixe ; une partie de SFR fibre, sans option ; désormais une
  partie d'Orange (DS-Lite, avec une case pour en sortir) ;
- **les box, en IPv6** : la Freebox laisse tout entrer (pare-feu désactivé par
  défaut : simple, mais risqué) ; la Livebox bloque, mais s'ouvre par UPnP ; la Bbox
  et la SFR Box bloquent, mais s'ouvrent par PCP. **Sur mobile, le pare-feu IPv6 des
  quatre opérateurs bloque toute connexion entrante et ne s'ouvre pas** : un téléphone
  en 4G/5G doit toujours ouvrir lui-même la connexion. Le perçage IPv6 depuis un
  mobile devrait marcher si ce pare-feu est un pare-feu à état classique, mais rien ne
  le documente.

Ce que ça implique :

- **IPv6 d'abord**, et plusieurs adresses candidates annoncées pour chaque nœud ;
- **qui ouvre la connexion ne doit pas décider qui fait les requêtes.** Avec HTTP, le
  client parle et le serveur répond ; avec une connexion QUIC entre pairs, les deux
  côtés ouvrent des flux dans la même connexion. Le téléphone, qu'on ne peut pas
  joindre directement, ouvre la connexion (vers le PC, ou vers le relais où il
  s'inscrit), et le PC peut ensuite lui envoyer des requêtes. C'est indispensable si le
  téléphone confie du travail au PC : dans le modèle « pull », c'est alors le PC qui
  vient demander les unités ;
- **un relais qui sait porter des unités de travail**, pas seulement coordonner le
  perçage ;
- **mesurer nous-mêmes** : aucune mesure récente du comportement des CGNAT mobiles ou
  des MAP-E, MAP-T et DS-Lite français n'a été trouvée.

## 4. Recommandation

**Iroh 1.x, utilisé par un petit programme compagnon en Rust (`aiodos-net`, Apache-2.0)
que le nœud Python lance et pilote en local. Le protocole de travail `aiodos/1` passe
dans ce tuyau presque sans changement. Relais et annuaire restent configurables, pour
qu'aucun ne soit obligatoire.** C'est une recommandation technique : le choix des
relais (Q16) et les autres questions du § 6 restent à trancher.

### Synthèse

| Critère | Iroh (compagnon Rust) | libp2p | Hyperswarm | Mainline + pkarr | VPN type WireGuard |
|---|---|---|---|---|---|
| Serveur central obligatoire | non, une fois relais et annuaire choisis (par défaut : ceux de n0) | non (amorçage et relais à fournir) | non (amorçage remplaçable, relais à fournir) | non | oui, sauf Yggdrasil |
| Traversée de NAT | ~90 % en direct annoncé, non mesuré ; repli par relais transparent | 70 % ± 7 mesuré ; relais publics bridés (128 Kio) | perçage poussé, aucun chiffre ; aucun relais public | annuaire seulement | > 90 % annoncé (Tailscale) |
| Santé (oct. 2026) | 1.3.0 ; stable depuis juin 2026 ; très actif | py 0.8.0 bêta ; rust actif ; go incertain | actif ; un seul publieur ; failles sans réponse | actif ; API instable | actifs |
| Licence | MIT / Apache-2.0 | MIT / Apache-2.0 | MIT / Apache-2.0 / ISC | MIT | variable (contrôleur ZeroTier non libre) |
| Windows | ✓ | py : pas de 3.14 ; rust ✓ | moteur JS de 46 à 92 Mo | ✓ (par un compagnon) | pilote réseau, droits admin |
| Android / Termux | dumbpipe et sendme empaquetés ; compagnon à compiler avec soin | plantages ouverts (go, rust) | Node sous Termux, non testé | libtorrent (Termux) | emplacement VPN, ou root |
| Python 3.14 + asyncio | compagnon (liaisons officielles en retard) | trio, pas de 3.14 | compagnon JS | rien de prêt | rien à coder (IP virtuelles) |
| Sécurité | pas d'audit ; correctifs rapides ; liste blanche par clé | pas d'audit ; dénis de service réguliers | pas d'audit ; rapport resté sans réponse | DHT surveillée | toute la machine exposée |

### Pourquoi Iroh

1. **Les quatre briques du § 1 d'un seul tenant** : identité par clé, annuaire (DNS,
   DHT Mainline ou mDNS), perçage de NAT, relais. Avec libp2p ou une solution maison,
   il faudrait les assembler soi-même.
2. **Le relais est un chemin de la même connexion** : une connexion qui démarre relayée
   passe en direct sans coupure, et l'inverse lors d'un changement de réseau (Wi-Fi →
   4G). Exactement ce qu'il faut pour des baux de 90 s.
3. **Des connexions dans les deux sens** : le téléphone, injoignable, ouvre la
   connexion, puis chacun peut faire des requêtes (cf. § 3).
4. **Stable depuis juin 2026**, sous licence permissive, Windows de première classe, et
   déjà présent dans Termux (dumbpipe, sendme).
5. **Déjà utilisé pour le même usage** : partager des GPU de machines personnelles
   (Mesh LLM, Buzz).
6. **Rien d'obligatoire chez n0** : relais et annuaire s'auto-hébergent sous la même
   licence, et la DHT Mainline peut remplacer le DNS de n0.

### Pourquoi un programme compagnon plutôt que les liaisons Python

- Les liaisons officielles sont en retard (iroh 1.0.2, vulnérable ; rien depuis
  juillet), absentes d'Android, et n'exposent ni la DHT, ni le mDNS, ni un annuaire
  personnalisé.
- Le compagnon donne toute l'API Rust (liste blanche de clés dès la poignée de main,
  relais auto-hébergés avec jeton, DHT, mDNS). Le même code se compile pour Windows et
  pour Termux (Rust y est disponible, et la recette de dumbpipe montre les précautions
  à prendre). Il isole aussi le réseau dans un processus à part : un plantage ne fait
  pas tomber le nœud, et une faille de la pile réseau reste confinée.
- Python reste simple : asyncio continue de parler HTTP en local.
- **Réversible** : si des liaisons Python à jour sortent (Android compris), elles
  pourront remplacer le compagnon sans toucher au reste, puisque le transport reste
  isolé derrière une interface.
- Coût : une chaîne de compilation Rust de plus dans le projet, et deux binaires à
  publier (Windows x64, Android arm64), de 3 à 5 Mo chacun d'après dumbpipe et
  iroh-relay.

### Architecture proposée (à confirmer par la mesure de l'étape 2.0)

```
 Machine A                                      Machine B (un ami)
 nœud Python                                    nœud Python
   client.call → 127.0.0.1:port de B              p2p_app sur 127.0.0.1
        │                                               ▲
        ▼                                               │
 aiodos-net (Rust + Iroh) ══ QUIC « aiodos/2 » ══► aiodos-net (Rust + Iroh)
   liste blanche de clés   direct, ou par un      liste blanche de clés
                           relais (chiffré)
```

- **Données** : pour chaque machine appairée, `aiodos-net` ouvre un port local qui
  mène, à travers Iroh, au serveur `p2p_app` de cette machine (comme dumbpipe). Dans
  l'autre sens, il n'accepte que les clés de sa liste blanche et transmet leurs
  requêtes à `p2p_app`, qui n'écoute plus que sur `127.0.0.1` pour ce chemin. Quand
  l'autre machine a déjà ouvert une connexion, le compagnon s'en sert aussi pour ses
  propres requêtes (cf. § 3). Les routes `/p2p/v1/…` et l'enveloppe ne changent pas.
- **Contrôle** : une petite API locale (ma clé, état de chaque connexion, directe ou
  relayée, liste blanche, relais, invitations), utilisée par un module Python dédié qui
  lance, surveille et relance le compagnon.
- **Identités liées** : le compagnon transmet à `p2p_app` la clé Iroh authentifiée de
  l'appelant, que `p2p_app` compare à l'émetteur de l'enveloppe. Une machine appairée ne
  peut pas se faire passer pour une autre, et un programme local qui utiliserait le
  tunnel se heurterait encore à l'enveloppe.
- **Réseau local inchangé** pendant la transition : la découverte UDP et le HTTP direct
  restent, et tout continue de marcher sans Internet. On ne fusionnera les deux chemins
  qu'après mesure (Iroh sait aussi se connecter en direct sur un réseau local, et
  trouver les machines par mDNS).
- **Relais et annuaire** : des listes configurables, sans valeur imposée. Relais :
  ceux de n0, un relais auto-hébergé (par un ami, par le projet) ou aucun (direct
  seulement : IPv6, Freebox, port ouvert). Annuaire : DHT Mainline (aucun serveur, mais
  un trafic de type BitTorrent visible) ou DNS (celui de n0, ou auto-hébergé). C'est la
  question Q16. Une invitation contient de quoi joindre l'invitant sans annuaire.

### Risques et parades

| Risque | Parade |
|---|---|
| n0 disparaît ou change de cap | Code MIT/Apache, réutilisable ; relais et annuaire auto-hébergés ; transport isolé dans le compagnon ; plans B : rust-libp2p, ou aioice + aioquic (§ 2.5). |
| Aucune mesure indépendante du taux de connexion directe | Étape 2.0 : mesurer sur nos propres réseaux avant tout code de production. |
| Pas d'audit, failles régulières en 2026 | Garder l'enveloppe et l'appairage ; épingler les versions ; suivre les avis de sécurité ; liste blanche dès la poignée de main. |
| Pas de spécification formelle du protocole Iroh | Notre protocole (`aiodos/2`) reste documenté par nous et indépendant du tuyau ; la compatibilité réseau de la série 1.x est promise par écrit. |
| Métadonnées visibles : le relais voit qui parle à qui ; le nom du protocole (ALPN) circule en clair | Choix des relais (Q16) ; plus tard, pour les inconnus, l'option Tor (Iroh a un transport Tor expérimental). |
| Android : arrière-plan, mise en veille, restrictions réseau | Mesure sous Termux dès l'étape 2.0 ; recette de compilation de dumbpipe. |
| Du Rust dans un projet Python | Binaires publiés pour Windows et Android ; un module court par fonctionnalité, comme côté Python. |

## 5. Partir de l'existant : ce qu'on garde, ce qui change

Le protocole « aiodos/1 » a été pensé pour le réseau local, mais plusieurs de ses choix
sont déjà ceux qu'il faut sur Internet. Le principe directeur : **le transport change,
le protocole de travail reste**. Les routes `/p2p/v1/…`, l'enveloppe scellée, les baux
et les tâches portables passent telles quelles dans un tuyau qui sait traverser les NAT.

### Ce qu'on garde

| Élément | Où | Pourquoi il reste |
|---|---|---|
| **Enveloppe scellée** : ChaCha20-Poly1305 avec la clé de la paire ; données associées liant protocole, action, émetteur, destinataire et horodatage ; anti-rejeu | [`envelope.py`](../aiodos/network/envelope.py), [`crypto.py`](../aiodos/network/crypto.py) | Deuxième couche, indépendante du transport : un relais, un tunnel ou une bibliothèque réseau mal configurée ne voit jamais un contenu en clair. |
| **Appairage avec un geste humain des deux côtés** | [`pairing.py`](../aiodos/network/pairing.py) | La confiance ne s'établit jamais automatiquement. Le *canal* change (cf. plus bas), pas le principe. |
| **Modèle « pull » avec baux** de 90 s renouvelés toutes les ~30 s ; unité rendue si l'utilisateur revient ; résultat en retard accepté tant que l'unité n'a pas été redonnée | [`leases.py`](../aiodos/jobs/leases.py), [`work.py`](../aiodos/network/work.py), [`worker.py`](../aiodos/network/worker.py) | C'est exactement ce qu'il faut sur Internet : les coupures et les machines qui disparaissent sont déjà prévues, et c'est la machine qui calcule qui ouvre la connexion. |
| **Tâches portables prédéfinies** : calcul n'importe où, intégration chez le propriétaire ; aucun code venu d'ailleurs | [`registry.py`](../aiodos/jobs/registry.py) | Première protection d'une machine qui calcule pour d'autres (BOINC, lui, télécharge les programmes des projets). À ne jamais abandonner. |
| **Exigence du même modèle à l'empreinte près** (digest Ollama) | `work.can_serve` | Indispensable pour que des vecteurs calculés ailleurs soient comparables. Et les modèles ne voyagent jamais entre nœuds : chaque machine les tient du registre officiel. À garder, car les lecteurs de fichiers de modèle (GGUF) ont des failles récurrentes. |
| **Bibliothèque « gardée sur ce PC »** | `store.local_only_ids`, `units.local_only` | Devient le premier cran d'une portée à plusieurs niveaux (cf. § 6). |
| **Versions explicites** : `aiodos/1`, `WORK_API` | `envelope.PROTOCOL`, `work.WORK_API` | Un protocole ouvert et versionné : on passe à `aiodos/2`, on ne bricole pas `aiodos/1`. |

### Ce qui change

| Aujourd'hui | Où | Sur Internet |
|---|---|---|
| Identité `node_id = uuid4().hex`, tirée au hasard | [`identity.py`](../aiodos/node/identity.py) | **Identité réseau = clé publique Ed25519** (l'EndpointId d'Iroh) : c'est l'adresse du nœud sur Internet, et le transport l'authentifie. L'identifiant actuel peut rester le nom du nœud dans le protocole, lié à sa clé lors de l'appairage. La clé privée devient le secret le plus précieux du nœud (à protéger, par exemple avec DPAPI sous Windows). Migration : les paires existantes échangent leurs clés par un message scellé avec leur clé de paire actuelle, sans nouvel appairage. |
| Machine jointe à `peers.address:port`, mise à jour par les annonces UDP | [`peers.py`](../aiodos/network/peers.py), [`manager.py`](../aiodos/network/manager.py) | **On joint une clé, pas une adresse.** IP directes et relais deviennent des indices mis en cache, retrouvés par la découverte. |
| Découverte par diffusion UDP (`255.255.255.255:47700`) | [`discovery.py`](../aiodos/network/discovery.py) | **Gardée pour le réseau local** (ou remplacée par la découverte locale du transport). Sur Internet, pas de découverte d'inconnus en phase 2 : on ajoute ses amis par invitation, puis on retrouve leurs adresses par une publication signée (cf. § 2). |
| Appairage : code à 6 chiffres comparé sur les deux écrans ; `pair/start` ouvert à toute machine du réseau local (3 demandes en attente au plus) | [`pairing.py`](../aiodos/network/pairing.py), [`p2p_app.py`](../aiodos/network/p2p_app.py) | Les deux écrans ne sont plus côte à côte. **Invitation** : un lien ou un QR code contenant la clé publique de l'invitant, un secret à usage unique et une date d'expiration, transmis par un canal que les deux amis jugent sûr (en personne, messagerie chiffrée). `pair/start` exige ce secret : un inconnu qui connaît la clé ne peut pas déclencher de demande. Le code à 6 chiffres reste possible par téléphone ou en visio ; il faudra alors le dériver aussi des deux clés publiques (liaison au canal), pour qu'un intermédiaire ne puisse pas relayer l'appairage. |
| Serveur entre nœuds FastAPI sur `0.0.0.0:47701`, en HTTP (contenus scellés) | [`p2p_app.py`](../aiodos/network/p2p_app.py) | **Jamais exposé tel quel sur Internet** (pas de redirection de port ni d'UPnP vers lui) : il passe derrière le transport P2P (le compagnon du § 4), qui ne laisse entrer que des clés connues (liste blanche) et les porteurs d'une invitation valide, et pour ce chemin il n'écoute plus que sur `127.0.0.1`. Comme l'adresse vue est alors locale, l'identité de l'appelant vient du transport et de l'enveloppe, plus de l'adresse IP. Le code des routes ne change pas. |
| Appels `requests.post("http://ip:port/p2p/v1/…")`, délai 10 s | [`client.py`](../aiodos/network/client.py) | Même requête, autre chemin : le port local que le compagnon ouvre vers chaque machine appairée. Délais à revoir : un passage par relais ajoute de la latence. |
| Clé de paire statique : qui la vole peut déchiffrer tout trafic enregistré | `crypto.pair_secrets` | Sur Internet, le trafic peut être enregistré en route. La **confidentialité persistante** (*forward secrecy*) viendra du transport (TLS 1.3 dans Iroh, à clés éphémères) ; prévoir aussi une rotation de la clé de paire. |
| Fichier de l'unité envoyé en base64 dans du JSON, par morceaux de 2 Mio, jusqu'à 512 Mio, contrôlé par SHA-256 | `work.blob`, `worker._download` | **Flux binaire** (le base64 coûte un tiers de volume en plus), reprise après coupure, plafond par défaut bien plus bas : le débit montant d'une box et un forfait mobile ne sont pas ceux d'un réseau local. |
| Contrôle des sorties : forme, valeurs finies, dimension | `index.check_embedded` | Suffisant entre ses propres machines ; **insuffisant face à un tricheur** : un vecteur plausible mais faux passe. Cf. § 6.3. |
| Réciprocité : secondes de calcul **déclarées par le calculateur** (plafonnées à 24 h par unité) | `work.result`, `peers.account` | Acceptable entre amis ; un inconnu peut gonfler son compte pour être servi en premier. Cf. § 6.4. |
| Ce qui part avec une unité : le fichier, mais aussi son **nom**, son **chemin dans la bibliothèque** (`source`) et l'identifiant de la bibliothèque | `work.lease` (seul `path` est retiré) | Métadonnées à réduire hors de ses propres machines (nom neutre, extension seule). |
| Profil échangé : **nom de la machine** (nom d'hôte Windows par défaut), GPU, modèles installés avec empreintes, **temps d'inactivité** | `identity.profile`, `worker.capabilities` | Le nom d'hôte contient souvent un prénom ; l'inactivité dit quand on est devant son PC. À réduire selon le niveau de confiance (cf. § 6.2). |
| Nouvelles de chaque machine appairée toutes les 15 s (profil toutes les 60 s) | `manager._refresh_loop` | Suffisant pour quelques amis ; un réseau public demandera des abonnements plutôt qu'un sondage. |

## 6. Questions à trancher (gouvernance et confidentialité)

Ces questions sont posées, **pas tranchées**. Pour chacune : ce qui existe aujourd'hui,
ce qu'on sait, les options. Les questions marquées **[phase 2]** bloquent l'ouverture
aux amis ; les autres peuvent attendre la phase 3.

Constat d'ensemble : aucun projet libre et mûr ne réunit aujourd'hui « pas de
blockchain », « inconnus », « vérification » et « incitations ». Petals, hivemind et exo
n'ont ni vérification ni incitation : ils supposent un essaim de confiance, et Petals
(sans activité depuis septembre 2024) déconseille d'y mettre des données sensibles. Ceux qui en
ont (Nous Psyche, Gensyn, Prime Intellect) s'appuient sur une blockchain pour la
coordination ou les paiements, même si leurs mécanismes de vérification n'en dépendent
pas. Il n'y a donc pas de modèle à recopier tel quel.

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

Ce qu'on sait : **en 2026, une machine grand public ne peut pas calculer sur des
données sans les voir.** Le chiffrement homomorphe coûte encore de l'ordre de 100 s
*par jeton* pour un modèle de 8 milliards de paramètres sur GPU (préprint de février
2026) ; le calcul multipartite, environ 5 minutes par jeton pour LLaMA-7B ; le calcul
confidentiel de NVIDIA n'existe que sur des GPU de centre de données (H100, H200,
B200/B300, RTX PRO 6000 Server Edition), aucune GeForce. Et ne renvoyer que des
vecteurs ne protège rien : on reconstruit exactement 92 % des textes de 32 jetons à
partir de leur embedding (EMNLP 2023). La confidentialité ne peut donc venir que du
**choix de la machine** (le cercle de confiance) et de la **minimisation** de ce qui
part.

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

Ce qu'on sait :

- **BOINC** ne fait jamais confiance à un résultat isolé : chaque tâche est calculée
  plusieurs fois, un validateur compare (avec une tolérance pour les nombres à virgule,
  ou en n'appariant que des machines « numériquement équivalentes »), et le crédit
  n'est accordé qu'après validation. Sa *réplication adaptative* fait confiance à une
  machine après 10 résultats valides consécutifs, ne revérifie plus qu'au hasard, et
  vise 5 à 10 % de calcul en plus au lieu d'au moins 50 %.
- **La triche apparaît dès qu'il y a un classement** : en 2001, moins de 1 % des unités
  de SETI@home étaient falsifiées, mais certains mois, la moitié des ressources du
  projet servait à débusquer les tricheurs, motivés par le classement public.
- **Recalculer ne redonne jamais exactement le même résultat** d'une machine à
  l'autre : 1 000 exécutions identiques d'un même modèle à température 0 ont donné
  80 réponses différentes (Thinking Machines, septembre 2025). Toute vérification par
  recalcul doit tolérer un écart. Pour les embeddings, le plus simple est un seuil de
  similarité cosinus sur des passages tirés au hasard et recalculés chez soi ; le seuil
  reste à mesurer sur nos machines (aucune étude publiée pour Ollama). Pour une réponse
  de modèle, la méthode légère la mieux documentée (TOPLOC, de Prime Intellect, licence
  MIT) a besoin des activations internes du modèle, qu'Ollama n'expose pas ; avec Ollama
  seul, il reste les tâches-témoins (rejouer une question connue et comparer les
  probabilités des jetons).

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

Ce qu'on sait :

- Le donnant-donnant de BitTorrent se contourne : des clients qui ne rendent jamais
  rien obtiennent quand même de bons débits (BitThief, 2006). Les trackers privés, eux,
  imposent un ratio et excluent ceux qui ne le tiennent pas.
- Les points de BOINC ou de Folding@home motivent, mais supposent une autorité qui
  valide : c'est un serveur central, exclu par le brief.
- Les comptabilités décentralisées par reçus et réputation (BarterCast, puis
  TrustChain et MeritRank à TU Delft, brouillon IETF de juin 2026) se passent de
  consensus global, mais aucune n'est totalement à l'abri des fausses identités : elles
  bornent ce que gagne un tricheur, sans l'annuler (Seuken et Parkes, 2011).
- Amélioration simple, sans blockchain : un reçu contresigné (celui qui a reçu le calcul
  signe le temps ou les unités reçus) supprime le mensonge unilatéral.

- **Q12** — La réciprocité reste-t-elle strictement entre deux machines (comme le
  donnant-donnant de BitTorrent), ou se transmet-elle (amis d'amis, reçus signés
  présentables à d'autres) ? La transmission aide les nouveaux venus mais ouvre la porte
  aux fausses identités en nombre (attaque Sybil).
- **Q13** — Faut-il une part de générosité : servir un nouveau venu qui n'a encore rien
  donné, et dans quelle proportion ?
- **Q14** — Unité de compte : temps déclaré, unités validées, ou coût estimé par le
  propriétaire (nombre de passages, durée d'audio…) ?
- **Q14 bis** — Le compte reste-t-il privé, ou devient-il visible (classement) ? Un
  classement motive, et motive aussi la triche.
- Rappel du brief : **pas de jeton ni de blockchain** dans le cœur.

### 6.5 Ouverture du réseau

Ce qu'on sait :

- **Sécurité** : calculer pour des inconnus, c'est faire lire des fichiers choisis par
  un attaquant à des bibliothèques qui publient des failles en continu. En 2026 : des
  dizaines d'avis pour pypdf (surtout des dénis de service), des écritures hors limites
  dans Pillow, une faille XXE active par défaut dans lxml (CVE-2026-41066, corrigée en
  6.1.0). Ollama n'a pas d'authentification et a connu une traversée de chemin critique
  menant à l'exécution de code (CVE-2026-103663, corrigée en 0.35.0) ; le mode RPC de
  llama.cpp permet l'exécution de code sans authentification (CVE-2026-34159), et son
  README interdit de l'exposer. L'injection de prompt n'est pas totalement évitable
  (OWASP). Windows Sandbox n'existe pas sous Windows Famille (le cas de la machine
  principale) ; AppContainer, si.
- **Droit** (repères, à faire relire par un juriste avant la phase 3) : d'après la
  CNIL, quand la machine d'un tiers traite des documents contenant des données
  personnelles, le propriétaire des documents est responsable du traitement et la
  machine qui calcule est sous-traitante, avec un contrat (article 28 du RGPD), sauf
  « exemption domestique », que la Cour de justice de l'UE lit strictement (arrêt
  Ryneš, C-212/13). L'AI Act exclut l'usage purement personnel et, en partie, le
  logiciel libre ; la nouvelle directive sur les produits défectueux (2024/2853, à
  transposer d'ici le 09/12/2026) exclut le logiciel libre fourni hors activité
  commerciale ; la directive sur la responsabilité de l'IA a été retirée en février
  2025.

- **Q15** — Phase 3 : réseau ouvert à tous (comme un torrent public), ou cercles sur
  invitation (amis d'amis, communautés) ? Qui peut exclure qui ?
- **Q16 [phase 2]** — Infrastructure : accepte-t-on par défaut des relais publics gérés
  par un tiers (remplaçables, mais gérés par quelqu'un d'autre) ? Un relais hébergé par
  le projet ? Par un membre du cercle qui a une IP publique ? Aucun relais (connexion
  directe ou rien) ? Repères : les relais publics de n0 sont gratuits, mais réservés au
  développement et aux usages amateurs, avec des limites non publiées ; n0 vend des
  relais à partir de 19 $ par mois ; un relais auto-hébergé demande une IP publique et
  un nom de domaine (un petit serveur loué suffit). Un relais ne voit jamais le contenu,
  mais il voit qui parle à qui, quand et combien. Même question pour l'annuaire : DHT
  Mainline (aucun serveur, trafic de type BitTorrent visible) ou DNS (celui de n0, ou
  auto-hébergé).
- **Q17** — Responsabilité : calculer pour un inconnu, c'est lire ses fichiers (peut-être
  illicites, ou contenant des données personnelles de tiers, ce qui relève du RGPD) et
  générer du texte sur sa machine. Faut-il une charte, des tâches exclues ?
- **Q18** — Sécurité de la machine qui calcule : ouvrir les fichiers d'un inconnu expose
  aux failles des lecteurs (PDF, Office, images, audio) et du moteur de modèles. Bac à
  sable obligatoire avant toute ouverture au public ?
- **Q19** — Téléphone : peut-il prêter sa puissance sur données mobiles, sur batterie ?

## 7. Plan par étapes

Pas de code de production avant que les questions **[phase 2]** soient tranchées
(étape 2.1). Chaque étape a son critère de fin.

### Phase 2 : le cercle de confiance

**2.0 Mesurer sur le terrain** (aucun code, montage jetable). Faire passer le protocole
actuel dans dumbpipe (archive de 3 Mo sous Windows, `pkg install dumbpipe` sous
Termux, qui a aussi Python 3.14), avec un tunnel dans chaque sens, entre le PC
principal et (a) le téléphone en 4G/5G, (b) une machine derrière une autre box, si
possible chez un ami. Relever, pour chaque cas : connexion directe ou relayée, en IPv4
ou en IPv6, délai d'établissement, débit, latence. Faire calculer de bout en bout
quelques unités d'indexation ; observer Termux en arrière-plan. dumbpipe passe par les
relais et l'annuaire de n0 : acceptable pour un essai, pas au-delà (le contenu reste
scellé par l'enveloppe). Quelques détails seront à contourner : à travers un tunnel,
l'adresse vue est `127.0.0.1`.

*Fini quand* : un tableau de mesures par cas réel, et la décision de continuer avec
Iroh, ou de passer au plan B (rust-libp2p, ou aioice + aioquic).

**Premières mesures, 16/10/2026 : cas (a), téléphone en 4G.** PC principal (nœud de
démonstration sans embeddings locaux, `--slots embed=0`) et Galaxy S23 (Android 16,
Termux, embeddinggemma), appairés puis reliés uniquement par deux tunnels dumbpipe 0.39
(un dans chaque sens, sur les ports déjà enregistrés à l'appairage).

| Point | Constat |
|---|---|
| Réseaux | PC en Ethernet derrière la box, **IPv4 seulement** (IPv6 désactivé sur la carte, alors que la box le fournit) ; Iroh signale une adresse IPv4 publique « qui varie selon la destination » (NAT strict). Téléphone en 4G Orange, **IPv6 seulement** avec IPv4 traduite (464XLAT, 192.0.0.8). |
| Chemin | **Relais** de n0 (euc1, Francfort) : aucune famille d'adresses commune pour une connexion directe. À refaire avec l'IPv6 activé sur le PC. |
| Établissement | Les deux sens fonctionnent, même avec un signal faible. dumbpipe ouvre **une connexion QUIC par connexion TCP**, donc par requête : 5 à 10 s par échange en 4G faible. |
| Calcul de bout en bout | **6 notes calculées par le téléphone pour le PC en 271 s, 0 échec** (57 requêtes : baux, fichiers, résultats, renouvellements, nouvelles). Vecteurs du téléphone identiques à ceux du PC (cosinus ≥ 0,9999, mesuré sur le S9). |
| Pannes trouvées | Demande de travail abandonnée à 8 s → porté à 30 s, échecs journalisés (69a668e). Propriétaire sans Ollama refusant en silence → raison donnée (51c4ed4). Tunnel d'écoute du PC arrêté en route (lancé depuis un shell : à détacher), tunnel d'écoute du téléphone arrêté au changement de réseau. |
| Android | Activer « Désactiver les limites pour les processus enfants » ; après la coupure du Wi-Fi, le téléphone ne reprend pas toujours les données mobiles seul (mode avion aller-retour) ; `sh relancer.sh` dans Termux recrée les tunnels. |

Enseignements pour **2.2** (le compagnon) :
- garder **une connexion par machine**, avec des flux multiplexés, au lieu d'une
  connexion par requête : c'est l'essentiel des 5 à 10 s mesurées ;
- des délais adaptés aux liens lents ;
- détecter les changements de réseau et se reconnecter seul, sans relancer le tunnel ;
- une identité stable par nœud (ici, la clé gardée dans `dp-listen.key`).

Reste à mesurer : le cas (b) (une machine derrière une autre box), une connexion
directe (IPv6 activé sur le PC), le débit sur un gros fichier, et la tenue sur plusieurs
heures.

**2.1 Trancher les questions [phase 2]** du § 6 : Q1, Q2, Q3, Q5, Q6, Q9 et Q16.

*Fini quand* : les réponses sont écrites dans ce document.

**Réponses de l'utilisateur, 16/10/2026** (étape 2.1 faite) :

| Question | Décision |
|---|---|
| **Q1** Niveaux de confiance | Deux niveaux en phase 2 : **mes machines** (confiance totale, comme aujourd'hui) et **amis** (droits plus étroits). Le niveau « inconnus » viendra en phase 3. |
| **Q2 / Q5** Données confiées à un ami | **Rien par défaut** : seules les bibliothèques marquées « partageable avec mes amis » peuvent partir chez un ami. Entre mes machines, la règle actuelle reste (tout sauf le cadenas). |
| **Q3** Ce que je calcule pour un ami | **Tous les types de travaux** : textes et documents, pages web, images et sons, conversations (modèles prêtés), dans les conditions déjà en place (inactivité, secteur, charge et température des téléphones, part du GPU). |
| **Q6** Métadonnées visibles par un ami | **Le minimum** : nom de fichier remplacé par un nom neutre (l'extension reste, elle choisit le lecteur), ni chemin, ni nom de bibliothèque, ni inactivité ; seulement ce qu'il faut pour calculer (modèles, formats lisibles). |
| **Q9** Vérification entre amis | **Par échantillon** : le propriétaire recalcule lui-même un passage tiré au hasard et compare (similarité cosinus, seuil à mesurer, ≥ 0,999 attendu d'après le S9) ; une machine prise en défaut est mise de côté et le résultat refusé. |
| **Q16** Relais | **Relais publics de n0 pour commencer**, remplaçables dans les réglages (relais du projet ou d'un membre du cercle plus tard). |
| **Q16 bis** Annuaire | **Les deux** : DNS de n0 d'abord, DHT Mainline (pkarr) en secours. |

**2.2 Le compagnon `aiodos-net`.** Programme Rust (Apache-2.0) sur Iroh 1.x, à version
épinglée : protocole `aiodos/2` (ALPN), liste blanche de clés, tunnels vers `p2p_app`,
API locale de contrôle, relais et annuaire configurables, ouverture de port
automatique. Compilé pour Windows x64 et Android arm64 (Termux). Côté Python, un
module dédié le lance et le surveille, et `client.call` choisit le chemin : réseau
local, ou compagnon.

*Fini quand* : deux nœuds sur une même machine (comme les démos A et B d'aujourd'hui)
font passer le travail par le compagnon, relais coupés, avec des tests automatiques.

**Fait le 16/10/2026** (dossier [`compagnon/`](../compagnon/README.md), Iroh 1.3.0) :

- **Le compagnon** : clé Ed25519 gardée dans le dossier de données, ALPN `aiodos/2` (un
  échange HTTP/1.1 de l'API `/p2p/…` par flux QUIC), relais de n0 et annuaire DNS de n0
  plus DHT Mainline par défaut, tous remplaçables ; liste blanche appliquée dès la
  poignée de main ; flux entrants relayés vers `p2p_app` avec la clé authentifiée de
  l'appelant ; mandataire local qui garde **une connexion par machine**, utilisable dans
  les deux sens ; API locale avec jeton (état, liste blanche, ticket, arrêt) ; changements
  de réseau détectés toutes les 5 s ; résolution DNS sans JNI sous Android.
- **Côté AIODOS** : `network/companion.py` le lance, le surveille et le relance ;
  `network/route.py` choisit le chemin (réseau local s'il répond, compagnon sinon ; réglage
  `p2p_route`) ; les machines appairées s'échangent leurs clés Internet par un message
  scellé (`net/info`, colonnes `peers.net_id`, `net_addrs`) ; `p2p_app` vérifie que la clé
  vue par le compagnon est celle de la machine qui a scellé l'enveloppe, et ne retient
  plus 127.0.0.1 comme adresse. Réglages `internet_*`, désactivés par défaut.
- **Tests** : 15 tests unitaires et 8 tests à deux compagnons en Rust ; côté Python, deux
  vrais nœuds se partagent une indexation par leurs seuls compagnons, relais coupés
  (`tests/test_network_internet.py`), plus les compagnons supervisés, le choix du chemin
  et l'échange des clés.
- **Premières mesures** (deux compagnons sur le PC, configuration par défaut, joints par
  la clé seule) : 0,30 s pour la première requête par le DNS de n0, 2,6 s par la DHT
  Mainline seule, puis 10 à 30 ms par requête sur la connexion gardée (directe, en IPv6).

Reste pour la suite : refaire la mesure de l'étape 2.0 (téléphone en 4G) par le
compagnon, construire et lancer le compagnon sous Termux sur le téléphone, et observer
la tenue sur plusieurs heures avec des changements de réseau.

**2.3 Identité et `aiodos/2`.** Une clé Ed25519 par nœud, rangée à l'abri (DPAPI sous
Windows) ; l'identifiant actuel reste le nom du nœud dans le protocole, lié à sa clé ;
table `peers` migrée (clé, relais, dernières adresses connues) ; contrôle que la clé
authentifiée par le transport est bien celle de l'émetteur de l'enveloppe ; code de
vérification dérivé aussi des deux clés. Les paires existantes passent à `aiodos/2` par
un message scellé avec leur clé actuelle, sans nouvel appairage.

*Fini quand* : deux nœuds déjà appairés migrent seuls, et un nœud `aiodos/1` reçoit un
message clair (« mets à jour AIODOS ») au lieu d'une erreur obscure.

**2.4 Invitation à distance.** Lien ou QR code contenant la clé de l'invitant, son
relais, un secret à usage unique et une date d'expiration ; `pair/start` exige ce
secret ; la confirmation sur les deux machines reste obligatoire.

*Fini quand* : un ami s'appaire sans être sur le même réseau, et une invitation déjà
utilisée ou périmée est refusée.

**Fait le 16/10/2026** (`aiodos/network/invitations.py`, migration 5), sans toucher au
compagnon. Celui-ci n'admet que les clés de sa liste blanche, dans les deux sens : les
deux machines se donnent donc d'abord leurs clés, par un canal que leurs utilisateurs
connaissent déjà (message, courriel). Il n'y a pas de QR code pour l'instant.

1. **L'invitant** crée une invitation : un code `AIODOS1-…` qui porte sa clé Internet, ses
   relais et adresses, et un secret à usage unique, valable 24 heures.
2. **L'ami** colle ce code dans son AIODOS : son compagnon admet l'invitant, et il reçoit
   une réponse à renvoyer (sa clé, et le même secret).
3. **L'invitant** colle la réponse : son compagnon admet l'ami, et l'appairage commence
   par les compagnons. Il se conclut comme sur le réseau local : même code à 6 chiffres
   des deux côtés, confirmation et niveau de confiance (2.5) choisis sur chaque machine.

Par le compagnon, `pair/start` n'est reçu que de la clé inscrite dans une invitation en
cours, avec son secret. `pair/confirm` doit venir de la clé de l'appairage en cours. Une
invitation qui a servi ou qui est périmée quitte la liste blanche et n'ouvre plus rien.

La vue Réseau a désormais une carte « Internet » :
- l'interrupteur du compagnon ;
- son état (clé, relais) ;
- les invitations en cours ;
- le chemin vers chaque machine (en direct ou par un relais, avec le délai).

Tests : 8 tests des codes et de l'admission (`tests/test_invitations.py`). Dans un essai
réel (`tests/test_network_invitation.py`), deux nœuds qui ne se connaissent pas sur le
réseau local s'appairent en amis par leurs seuls compagnons, et l'invitation ne resservira
pas.

Ce que 2.3 prévoyait est en grande partie assuré par le compagnon :
- clé Ed25519 par machine ;
- clé du transport comparée à celle de l'émetteur ;
- clés échangées sans nouvel appairage (`net/info`).

Il reste à protéger les clés (DPAPI sous Windows) et à lier le code à 6 chiffres aux clés
des compagnons.

**2.5 Politique de partage** (selon les réponses de 2.1) : niveaux de confiance,
portée des bibliothèques, métadonnées réduites (nom de fichier neutre, profil allégé),
plafonds de volume (données mobiles), journal de ce qui est parti et chez qui.

*Fini quand* : chaque réponse de 2.1 se retrouve dans un réglage ou une règle testée.

**Fait le 16/10/2026** (`aiodos/network/sharing.py`, migration 4) :

- **Q1, niveaux de confiance.** En confirmant l'appairage, chaque machine dit si l'autre est
  « une de mes machines » ou « la machine d'un ami ». Il n'y a pas de choix par défaut, et
  chacune juge l'autre. Le niveau se change ensuite dans la vue Réseau. Les machines déjà
  appairées restent « mes machines » (`peers.trust`).
- **Q2 et Q5, données confiées.** Une bibliothèque a trois portées : ce PC uniquement (le
  cadenas), mes machines (par défaut), ou mes machines et mes amis
  (`collections.share_friends`). Un ami ne reçoit que des unités de la troisième.
- **Q6, métadonnées.** Chez un ami, le fichier arrive sous un nom neutre
  (`fichier-<n>.<extension>`). Du payload ne partent que les clés que la tâche déclare
  nécessaires (`shared_keys` : l'adresse d'une page web) : ni chemin, ni nom de
  bibliothèque. Chez le propriétaire, le vrai nom remplace le nom neutre dans le texte
  calculé (« [Image : …] »). Un ami voit de cette machine un profil allégé : nom, version,
  mises à jour, et les modèles seulement s'ils lui sont prêtés. Ni matériel, ni
  alimentation, ni inactivité.
- **Q9, contrôle par échantillon.**
  - Un passage tiré au hasard est recalculé chez le propriétaire ; le cosinus doit être au
    moins de 0,999.
  - En cas d'écart, le résultat est refusé, l'unité est refaite ailleurs, et la machine est
    mise de côté : plus rien ne lui est confié jusqu'à ce que l'utilisateur la rétablisse.
  - Si le contrôle est impossible (Ollama arrêté), l'unité retourne en file sans compter
    d'essai.
  - Les résultats de mes machines ne sont pas contrôlés.
- **Q3, calculer pour un ami.** Pas de règle à part : les conditions du partage de
  puissance s'appliquent.
- **Journal.** Chaque fichier ou page confié à un ami est noté (`shared_log`). La vue Réseau
  en montre le total, et la liste.
- **Tests.** 11 tests (`tests/test_sharing.py`), plus un essai à deux vrais nœuds avec
  Ollama (`tests/test_network_work.py`). Dans cet essai, rien ne part avant le partage ;
  ensuite, l'ami calcule sans voir un seul vrai nom de fichier, et chacun montre à l'autre
  le profil qui convient.

Reste : les plafonds de volume pour les données mobiles. Ils ne correspondent à aucune
question de 2.1, et un téléphone ne sait pas encore dire s'il est en Wi-Fi ou en 4G.

**2.6 Transferts adaptés à Internet** : fichier de l'unité en flux binaire avec
reprise, plafond de taille par défaut plus bas, délais revus pour les chemins relayés.

*Fini quand* : une coupure en plein transfert reprend où elle s'était arrêtée.

**Fait le 16/10/2026**, sans changer le protocole :

- **Reprise.** Un morceau du fichier qui n'arrive pas (4G, relais, changement de réseau)
  est redemandé au même endroit, après des pauses de 2 à 30 s, pendant environ 1 min 40.
  Pendant ce temps, le bail reste renouvelé à côté. Rien n'est repris depuis le début.
- **Morceaux.** Quand le propriétaire n'est joignable que par Internet, les morceaux sont
  de 512 Kio au lieu de 2 Mio : chacun tient dans le délai d'une requête, même par un
  relais.
- **Plafond.** Une machine joignable seulement par Internet ne reçoit pas de fichier de plus
  de 64 Mo (réglage `internet_max_blob_mb`) : les gros fichiers restent aux machines du
  réseau local, ou à celle du propriétaire.
- **Délais.** Les délais des chemins par Internet viennent de 2.2 (`route.timeouts`) : au
  moins 30 s de lecture.
- **Tests** dans `tests/test_worker.py` et `tests/test_work_leases.py` :
  - un transfert coupé au milieu reprend à l'octet près ;
  - une coupure trop longue abandonne ;
  - les morceaux sont plus petits par Internet ;
  - les gros fichiers restent au réseau local.

Reste : le fichier passe encore en base64 dans des messages JSON scellés (un tiers de plus
à transférer) ; un flux binaire scellé l'éviterait.

**2.7 Relais** (selon Q16) : relais configurables dans l'interface, état direct ou
relayé visible pour chaque machine, guide d'auto-hébergement d'`iroh-relay` (liste
blanche de clés ou jeton).

*Fini quand* : un relais auto-hébergé sert deux machines qui ne peuvent pas se joindre
en direct.

**Fait le 16/10/2026, essai conclu le 17/10/2026** :

- **Réglages.** Réglages → Internet : relais (`n0`, ses propres adresses, ou `none`),
  annuaires (`n0-dns`, `dht`), ouverture de port sur la box, chemin préféré, et taille
  maximale d'un fichier confié par Internet. Changer une option du compagnon le relance
  avec elle.
- **État.** Pour chaque machine appairée, la vue Réseau montre si la connexion est en
  direct ou par un relais, avec son délai (depuis 2.4).
- **Guide.** [docs/relais.md](relais.md) explique comment héberger `iroh-relay` 1.3.0 :
  - installation ;
  - certificat Let's Encrypt et ports ;
  - accès réservé par une liste blanche de clés (le bouton « Copier la clé de cette
    machine » les donne) ou par un jeton ;
  - mode d'essai `--dev`.

**L'essai qui conclut l'étape** (17/10/2026, sur le PC principal) :

- **Relais seulement.** Deux compagnons sur une même machine se joignent toujours en
  direct. Le compagnon a donc une option réservée aux essais, `--relay-only` : Iroh n'y
  a aucun transport IP, donc aucune socket UDP. Il n'y a ni perçage de NAT, ni adresse
  directe annoncée, ni ouverture de port, dans un sens comme dans l'autre. C'est ainsi
  qu'Iroh 1.3.0 coupe le direct dans ses propres essais. Un sélecteur de chemin maison
  (API instable d'Iroh) n'aurait choisi que le chemin d'envoi d'une des deux machines.
  Côté nœud, l'option correspondante est `--internet-relay-only <adresse du relais>`.
- **Le relais.** `iroh-relay` 1.3.0, construit avec `cargo install iroh-relay --version
  1.3.0 --features server --locked` (environ une minute), lancé en `--dev` (HTTP) avec le
  fichier de réglages du guide et sa liste blanche de deux clés.
- **L'essai automatique** (`tests/test_network_relay.py`, 33 s) :
  - deux vrais nœuds qui ne se connaissent pas sur le réseau local s'appairent sur
    invitation, par le relais ;
  - ils se confient une indexation (3 notes en 2,6 s) ;
  - la vue Réseau montre `relay` comme chemin de chaque machine appairée ;
  - une troisième machine, absente de la liste blanche, est refusée par le relais. Même
    invitée, et admise par les deux compagnons, elle reste injoignable ; rien n'atteint
    même la poignée de main de l'autre compagnon.
- **Côté Rust** (`compagnon/tests/relay_only.rs`, relais iroh-relay lancé dans l'essai) :
  dix allers-retours restent par le relais ; une clé hors liste blanche est refusée. Un
  contrôle montre que les mêmes machines, sans l'option, passent en direct : l'essai
  verrait donc un chemin direct s'il y en avait un.
- **Mesures** (une seule machine) : première requête par le relais en 20 ms, puis 1 à
  4 ms.

L'essai a fait apparaître quatre défauts, corrigés :
- **L'exemple du guide ouvrait le relais à tous.** La liste blanche y suivait la section
  `[tls]` : en TOML, elle appartenait alors à cette section, et `iroh-relay` l'ignorait
  sans rien dire (vérifié : `AllowAll` au lieu d'`AllowlistAccess`). Le guide la place
  désormais avant, et dit comment le vérifier.
- **Une machine appairée sur invitation ne recevait jamais de travail.** La machine qui
  prête ne demandait du travail qu'aux machines dont elle connaît l'adresse sur le réseau
  local, comme en phase 1 (`worker.py`). Deux amis appairés à distance (2.4) ne se
  seraient donc rien prêté. Elle demande désormais aussi aux machines qu'elle sait
  joindre par Internet.
- **Un ticket donné juste après le démarrage n'avait pas de relais.** Une invitation
  acceptée dans la seconde où le compagnon démarre partait avec la seule clé ; sans
  annuaire, l'autre machine ne pouvait pas la joindre. Le compagnon attend désormais son
  relais avant de donner son ticket, 4 s au plus après le démarrage.
- **Une machine refusée par le relais semblait prête.** La carte « Internet » le dit
  maintenant : « le relais … refuse cette machine ».

Le guide ([docs/relais.md](relais.md)) dit aussi ce que l'essai a appris d'autre :
- `iroh-relay` ouvre ses statistiques sur le port 9090, sur toutes les interfaces ;
- il est muet par défaut (`RUST_LOG=iroh_relay=info` pour voir les refus) ;
- après l'ajout d'une clé, il faut le relancer, et la machine refusée réessaie seule ;
- aucun annuaire n'est nécessaire entre machines appairées.

Reste, hors de cette étape : le même essai entre deux vrais réseaux (2.8). La
conversation avec un modèle prêté et la prévenance au désappairage marchent aussi avec
une machine appairée sur invitation, qui n'a qu'une clé Internet (17/10/2026,
`tests/test_network_chat.py`, `tests/test_network_invitation.py`).

**2.8 Essai réel avec un ami**, puis documentation (README, ce document complété par
les mesures).

### Phase 3 : le réseau public

Après les décisions des § 6.3 à 6.5, et seulement si la phase 2 a tenu ses promesses :

- **découverte publique sur adhésion explicite** (diffusion par sujet avec
  iroh-gossip, ou annonces dans la DHT) ;
- **vérification** : contrôle par échantillon pour les embeddings, tâches-témoins,
  réplication adaptative façon BOINC pour les inconnus ;
- **réciprocité** : reçus contresignés, réputation calculée à partir de ses propres
  amis ;
- **bac à sable** pour les fichiers d'inconnus : processus séparé, limites de temps et
  de mémoire, sans réseau, AppContainer sous Windows ; jamais de fichier de modèle venu
  d'un pair ; Ollama 0.35.0 au minimum ;
- **relais communautaires**, et une option Tor pour cacher les adresses IP ;
- **relecture juridique** (RGPD, responsabilité) avant l'ouverture.

## Sources

Consultées le 16/10/2026 (sélection ; versions et dates relevées sur les dépôts, PyPI,
crates.io et npm).

**Iroh** —
[Iroh 1.0, « Dial keys, not IPs »](https://www.iroh.computer/blog/v1) ·
[politique de versions et de support](https://docs.iroh.computer/about/release-policy.md) ·
[versions publiées (crates.io)](https://crates.io/api/v1/crates/iroh/versions) ·
[traversée de NAT](https://docs.iroh.computer/concepts/nat-traversal.md) ·
[origine du chiffre de 90 %](https://github.com/n0-computer/docs.iroh.computer/pull/4) ·
[1.0.0-rc.1 et les NAT durs](https://www.iroh.computer/blog/iroh-1-0-0-rc-1) ·
[noq](https://www.iroh.computer/blog/noq-announcement) ·
[sécurité et vie privée](https://docs.iroh.computer/concepts/security-privacy.md) ·
[relais publics](https://docs.iroh.computer/iroh-services/relays/public.md) ·
[iroh-relay (contrôle d'accès)](https://github.com/n0-computer/iroh/tree/main/iroh-relay) ·
[correctifs de sécurité 1.1.0](https://www.iroh.computer/blog/iroh-1-1-0) ·
[avis GHSA-7cq4-mhxw-xw78](https://github.com/n0-computer/iroh/security/advisories/GHSA-7cq4-mhxw-xw78) ·
[faille iroh-blobs](https://www.iroh.computer/blog/iroh-blobs-push-fix) ·
[paquet Python `iroh`](https://pypi.org/project/iroh/) ·
[documentation Python](https://docs.iroh.computer/languages/python.md) ·
[dépendances figées d'iroh-ffi 1.1.0](https://github.com/n0-computer/iroh-ffi/blob/v1.1.0/Cargo.lock) ·
[dumbpipe 0.39.0](https://github.com/n0-computer/dumbpipe/releases/tag/v0.39.0) ·
[recette Termux de dumbpipe](https://github.com/termux/termux-packages/blob/master/packages/dumbpipe/build.sh) ·
[brouillon IETF sur la traversée de NAT](https://datatracker.ietf.org/doc/draft-bruynooghe-n0-quic-nat-traversal/) ·
[Mesh LLM](https://github.com/Mesh-LLM/mesh-llm) ·
[Buzz](https://github.com/block/buzz)

**libp2p** —
[py-libp2p](https://github.com/libp2p/py-libp2p) ·
[Python 3.14 (#1100)](https://github.com/libp2p/py-libp2p/issues/1100) ·
[passage à trio (#1240)](https://github.com/libp2p/py-libp2p/pull/1240) ·
[relais v2 (spécification)](https://github.com/libp2p/specs/blob/master/relay/circuit-v2.md) ·
[limites du relais dans go-libp2p](https://github.com/libp2p/go-libp2p/blob/v0.50.0/p2p/protocol/circuitv2/relay/resources.go) ·
[mesure DCUtR (arXiv 2510.27500)](https://arxiv.org/abs/2510.27500) ·
[version IMC 2026 (arXiv 2604.12484)](https://arxiv.org/abs/2604.12484) ·
[fin d'IPFS chez Shipyard](https://ipshipyard.com/blog/2026-the-end-of-ipfs-at-shipyard/) ·
[go-libp2p sous Android (#3537)](https://github.com/libp2p/go-libp2p/issues/3537)

**Hyperswarm** —
[hyperdht](https://github.com/holepunchto/hyperdht) ·
[perçage de NAT (code)](https://github.com/holepunchto/hyperdht/blob/fdcda937d8cf3ca2e494396c430218e2996e6b29/lib/holepuncher.js) ·
[relais aveugle (documentation Pear)](https://docs.pears.com/p2p/how-to/connect-to-peers/relay-connections-through-a-blind-relay/) ·
[rapport de sécurité du 27/08/2026](https://github.com/holepunchto/hyperdht/issues/294) ·
[FAQ Pear](https://github.com/holepunchto/pear-docs/blob/main/reference/faq.md) ·
[QVAC SDK (Tether)](https://tether.io/news/tether-launches-qvac-sdk-as-the-ai-universal-building-block-that-runs-trains-and-evolves-intelligence-across-any-device-and-platform/)

**DHT Mainline, pkarr, BitTorrent** —
[pkarr](https://github.com/pubky/pkarr) ·
[format pkarr](https://github.com/pubky/pkarr/blob/main/design/base.md) ·
[BEP 44](https://www.bittorrent.org/beps/bep_0044.html) ·
[BEP 55](https://www.bittorrent.org/beps/bep_0055.html) ·
[BEP 55 dans Transmission](https://github.com/transmission/transmission/pull/8884) ·
[pairs injoignables (Meulpolder et al., 2010)](https://www.usenix.org/legacy/event/iptps/tech/full_papers/Meulpolder.pdf) ·
[RFC 6092 (pare-feu IPv6 des box)](https://www.rfc-editor.org/rfc/rfc6092.html)

**Réseaux privés virtuels** —
[Headscale](https://github.com/juanfont/headscale) ·
[relais DERP de Headscale](https://headscale.net/stable/ref/derp/) ·
[Tailscale, « How NAT traversal works »](https://tailscale.com/blog/how-nat-traversal-works) ·
[Tailscale, « NAT traversal improvements, pt. 1 »](https://tailscale.com/blog/nat-traversal-improvements-pt-1) ·
[Peer Relays](https://tailscale.com/blog/peer-relays-ga) ·
[libtailscale](https://github.com/tailscale/libtailscale) ·
[Nebula](https://github.com/slackhq/nebula) ·
[NetBird](https://github.com/netbirdio/netbird) ·
[ZeroTier](https://github.com/zerotier/ZeroTierOne) ·
[Yggdrasil](https://yggdrasil-network.github.io/about.html) ·
[un seul VPN actif sous Android](https://developer.android.com/develop/connectivity/vpn)

**Traversée de NAT et contexte français** —
[Ford, Srisuresh et Kegel (2005)](https://bford.info/pub/net/p2pnat/) ·
[Halkes et Pouwelse (2011)](https://link.springer.com/doi/10.1007/978-3-642-20798-3_1) ·
[baromètre IPv6 de l'Arcep (2026)](https://www.arcep.fr/fileadmin/reprise/observatoire/ipv6/Arcep_Barometre_2026_de_la_transition_vers_IPv6.pdf) ·
[statistiques IPv6 de Google](https://www.google.com/intl/en/ipv6/statistics.html) ·
[aioice](https://github.com/aiortc/aioice) ·
[aioquic](https://github.com/aiortc/aioquic) ·
[Tor Metrics (débits des services onion)](https://metrics.torproject.org/onionperf-throughput.csv?start=2026-08-15&end=2026-10-14) ·
[Veilid (Python)](https://gitlab.com/veilid/veilid/-/blob/main/veilid-python/README.md) ·
[licence de Reticulum](https://github.com/markqvist/Reticulum/blob/master/LICENSE)

**Vérification, réciprocité, confidentialité** —
[BOINC, réplication adaptative](https://github.com/BOINC/boinc/wiki/Adaptive-Replication) ·
[BOINC, crédit](https://github.com/BOINC/boinc/wiki/CreditNew) ·
[Anderson, « BOINC: A Platform for Volunteer Computing » (2019)](https://arxiv.org/abs/1903.01699) ·
[triche à SETI@home (New York Times, 2001)](https://crypto.stanford.edu/~mironov/save/The%20Search%20for%20E_T_%20Yields%20Earthly%20Cheats.htm) ·
[non-déterminisme de l'inférence (Thinking Machines)](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/) ·
[TOPLOC](https://arxiv.org/abs/2501.16007) ·
[Petals : sécurité et vie privée](https://github.com/bigscience-workshop/petals/wiki/Security,-privacy,-and-AI-safety) ·
[Seuken et Parkes, mécanismes de comptabilité](https://dash.harvard.edu/bitstream/handle/1/11856150/Seuken_AccountingMechanisms.pdf) ·
[brouillon IETF TrustChain](https://datatracker.ietf.org/doc/html/draft-viftode-trustchain-trust-01) ·
[chiffrement homomorphe : Cachemir](https://arxiv.org/abs/2602.11470) ·
[NVIDIA, plateformes de calcul confidentiel](https://docs.nvidia.com/datacenter/cloud-native/confidential-containers/1.0.0/supported-platforms.html) ·
[inversion d'embeddings (EMNLP 2023)](https://arxiv.org/abs/2310.06816)

**Sécurité et droit** —
[lxml, GHSA-vfmq-68hx-4jfw](https://github.com/lxml/lxml/security/advisories/GHSA-vfmq-68hx-4jfw) ·
[Ollama, CVE-2026-103663](https://nvd.nist.gov/vuln/detail/CVE-2026-103663) ·
[llama.cpp, GHSA-j8rj-fmpv-wcxw](https://github.com/ggml-org/llama.cpp/security/advisories/GHSA-j8rj-fmpv-wcxw) ·
[OWASP, injection de prompt](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) ·
[Windows Sandbox](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/) ·
[AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation) ·
[CNIL, responsable de traitement ou sous-traitant](https://www.cnil.fr/fr/rgpd-comment-bien-identifier-son-role)
