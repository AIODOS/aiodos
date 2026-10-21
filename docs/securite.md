# Sécurité d'AIODOS

Pour l'auteur et pour qui lit le code. Ce document décrit l'audit de sécurité du
20/10/2026, le lot de corrections qui l'a suivi (20 et 21/10/2026, pour la version 2.0.2),
les bornes qu'il pose, et ce qui reste ouvert.

## En bref

- L'audit a confirmé **37 failles** : 1 critique, 10 hautes, 16 moyennes, 10 basses.
  36 sont corrigées dans ce lot. La dernière (n° 3, le pont MCP de la passerelle « Autres
  outils ») attend la passerelle, réécrite sur une autre branche (§ 7).
- La plus grave (n° 0) : une machine appairée pouvait glisser du HTML dans la vue Réseau
  par son profil, voler le jeton de l'API locale, puis faire lancer un programme par la
  machine. Le profil reçu est désormais typé et borné, l'interface n'en insère que des
  nombres, des oui/non et du texte échappé, et une politique de contenu (CSP) empêche tout
  script étranger.
- Ce qui change pour l'usage :
  - **l'appairage par code passe au protocole 2** (avec engagement, comme le Bluetooth) :
    il faut mettre à jour toutes les machines, téléphone compris, avant d'en appairer de
    nouvelles. Les paires existantes continuent de se parler (§ 5) ;
  - **un ami ne reçoit plus que ce que ma machine sait contrôler** : des textes et des
    documents qu'elle relit elle-même ; jamais une page web, une image, un son ou une vidéo
    à indexer ;
  - **le compagnon publié doit être celui de ce lot** : sans lui, AIODOS refuse de prêter
    ou d'emprunter une carte graphique ;
  - une réponse de modèle ne charge plus d'image toute seule, et ses liens ne s'ouvrent que
    vers le web ou un courriel ;
  - les outils de publication refusent un dépôt ouvert aux autres comptes, ou des fichiers
    non suivis : tout commiter avant de publier ou de construire.
- Tout ce qui vient d'une autre machine est borné : taille des requêtes et des réponses,
  nombre de connexions, durée, débit minimal (§ 4).
- Ce qui reste ouvert est listé au § 6.

## 1. Le modèle de menace

AIODOS tourne sur les machines de l'utilisateur, mais il parle à d'autres machines, lit des
pages et des documents venus d'ailleurs, et reçoit ses mises à jour du réseau. L'audit, les
rapports et le code désignent les attaquants par une lettre :

| | Qui | Ce qu'il peut tenter | Ce qui l'arrête |
|---|---|---|---|
| A | Une page web ouverte dans un navigateur de la machine | joindre 127.0.0.1 (requête forgée, *DNS rebinding*), charger une image | API locale : en-tête `Host` vérifié, jeton, pas de CORS. Ports de sd-server refusés par les navigateurs. Clé de llama-server |
| B | Un autre programme, un autre compte du PC, une autre appli du téléphone | se connecter à 127.0.0.1, lire ce que les droits des fichiers laissent lire, lire la ligne de commande des programmes | tunnels gardés, serveur RPC tenu par le compagnon, clés passées par l'environnement, données en 0700 et 0600 hors Windows, DPAPI sous Windows |
| C | Un appareil du réseau local, non appairé | envoyer des balises UDP et des requêtes au port 47701, se placer au milieu d'un appairage | appairage avec engagement, balises jamais crues, bornes du serveur entre machines |
| D | Un ami appairé, ou une de mes machines compromise | sceller n'importe quel JSON, confier du travail, en calculer | profil typé, ordres vérifiés par la machine qui calcule, résultats contrôlés, bornes et limites de fréquence |
| E | Un inconnu du réseau public (phase 3.0) | joindre l'annuaire public, rien de plus | rien de changé dans ce lot |
| F | Un pair qui envoie des données piégées | NaN, objet à la place d'une liste, réponse énorme, bombe gzip | JSON strict, réponses lues en flux et bornées, schéma du profil |
| G | La chaîne des mises à jour | clé de publication volée, relais malhonnête, dépôt ou outils de publication modifiés, paquets PyPI | signature, approbation par empreinte, enveloppe canonique, dépôt privé, paquets figés par empreinte |
| H | Un contenu malveillant | page web, document, réponse de modèle (injection d'invite) | images non chargées, liens filtrés, documents bornés, CSP |

## 2. L'audit du 20/10/2026

### Méthode

- **Un auditeur par surface** : API locale, interface (XSS), protocole entre machines,
  services entre machines, compagnon (Rust), contenus, moteurs et modèles, mises à jour,
  stockage et secrets, appli Android. Chacun lit le code, cherche la garde qui manque et,
  quand il le peut, fait un essai sur des nœuds jouets (jamais sur l'AIODOS installé).
- **Une critique de complétude** cherche ce que les auditeurs n'ont pas regardé. Elle a
  ajouté deux failles (n° 16 et 17).
- **Une contre-vérification par des sceptiques** : chaque faille passe devant deux ou
  trois vérificateurs, chargés de la réfuter (une garde oubliée, un chemin impossible) ou
  d'en juger l'impact.
- **Bilan** : 58 failles proposées au premier tour, 60 après la critique, **53** une fois
  les doublons réunis. **37 confirmées**, **10 partagées** (les vérificateurs ne
  s'accordaient pas) et **6 réfutées**.
- **Les corrections** ont été faites par groupes de fichiers (interface, contenus,
  appairage, réseau, compagnon et tunnels, moteurs locaux, mises à jour, stockage, puis
  amis et approbation, arrêts, client, travail, divers). Chaque groupe a été relu par un
  relecteur indépendant, et l'ensemble une dernière fois le 21/10. Chaque nouvel essai
  échoue sans sa correction : c'est vérifié sur des copies remises dans l'état d'avant.

Les failles sont numérotées de 0 à 36, comme dans les rapports et les commentaires du code
(« faille 20 de l'audit du 20/10/2026 »).

### Les 37 failles confirmées

| N° | Gravité | Surface | Qui | Faille | Ce qui est corrigé | Où |
|---|---|---|---|---|---|---|
| 0 | critique | interface | D, F | XSS sans clic par le profil d'une machine appairée (vue Réseau) : vol du jeton d'API, puis programme lancé par un réglage | profil typé et borné à la réception et à la relecture ; l'interface n'en insère que des nombres, des oui/non et du texte échappé ; CSP | `network/profile.py`, `network/peers.py`, `network/manager.py`, `ui/static/js/dom.js`, `views/network.js` |
| 1 | haute | interface | D, F, H | aucune politique de contenu (CSP), ni `nosniff`, ni `Referrer-Policy` | CSP stricte pour l'interface, `sandbox` pour l'API, `nosniff` et `no-referrer` partout | `api/security.py`, `api/server.py` |
| 2 | haute | moteurs | B, D | ggml-rpc-server joignable sans authentification par tout programme local, des deux côtés du tunnel | chez l'emprunteur, tunnel gardé (n° 9) ; chez le prêteur, serveur tenu par le compagnon, prêt arrêté s'il voit un autre programme ; cache plafonné ; pas de prêt depuis un téléphone | `compagnon/src/hold.rs`, `owner.rs`, `cluster/helper.py`, `cluster/group.py`, `node/identity.py` |
| 3 | haute | stockage | H, B | pont MCP stdio : `python -m` exécute le code du dossier courant du client MCP | **non corrigé** : la passerelle est réécrite ailleurs (§ 7) | `gateway/server.py` |
| 4 | haute | services | D, F | un ami fait lire au PC ou au téléphone une adresse de son réseau local ou 127.0.0.1 (unité « page web ») | garde des adresses (Internet public seulement, redirections revérifiées) ; pages réservées à mes machines | `services/addresses.py`, `services/web.py`, `jobs/kinds/index.py`, `network/work.py`, `network/worker.py` |
| 5 | haute | contenus | D | la même, vue par l'auditeur des contenus, avec le sondage des ports par les messages d'erreur | idem ; un message d'erreur unique, sans hôte ni port | `services/addresses.py` |
| 6 | haute | protocole | C | code à 6 chiffres sans engagement : un intermédiaire actif choisit sa clé après coup | protocole 2 : engagement, puis code et clé tirés de toute la transcription | `network/crypto.py`, `network/pairing.py` |
| 7 | haute | protocole | C (A) | demande d'appairage remplaçable sans authentification ; réponse de l'utilisateur non liée au code vu | demande jamais remplacée ; réponse liée à la demande montrée (`ref`) ; une machine déjà appairée n'est remplacée que sur un choix explicite | `network/pairing.py`, `api/routes/network.py`, `views/network.js` |
| 8 | haute | API locale | H | images Markdown chargées d'office : fuite de données par injection d'invite | une image devient un lien « [image : …] » ; seule une image `data:` s'affiche ; CSP | `render.py`, `api/security.py` |
| 9 | haute | compagnon | B (D, F) | port local d'un tunnel ouvert à tout programme de la machine | tunnel gardé : seul le llama-server nommé par AIODOS est relayé ; connexions bornées ; tunnel fermé avec lui | `compagnon/src/tunnel.rs`, `owner.rs`, `network/companion.py`, `cluster/group.py` |
| 10 | haute | mises à jour | B, puis G | dépôt de publication modifiable par tout compte Windows : publication piégée, clé volée | contrôle du dépôt avant tout import, git neutralisé, dossiers de travail privés ; installeur forcé dans le profil ; droits du dossier corrigés par l'auteur le 21/10 | `updates/private.py`, `tools/publish.py`, `tools/build_installer.py`, `tools/companion_build.py`, `tools/build_android.py`, `tools/installer/aiodos.iss` |
| 11 | moyenne | stockage | C, A | journal sans limite : un appareil du réseau local, ou une page web, remplit le disque | journaux bornés, alertes répétées filtrées ; une requête malformée ne lève plus d'erreur imprévue | `__main__.py`, `logfiles.py`, `network/envelope.py`, `api/security.py` |
| 12 | moyenne | services | D, H | un ami remplace le texte de mes bibliothèques (contrôle par échantillon incomplet) | le document est relu et redécoupé ici : les passages doivent être exactement ce découpage ; 3 vecteurs recalculés | `jobs/kinds/index.py`, `knowledge/chunking.py` |
| 13 | moyenne | contenus | D, F | le contrôle ne vérifie pas que les passages viennent du document ; un résultat « sauté » n'est jamais contrôlé | idem ; « sauté » revérifié ; ce qui ne se relit pas ici ne part plus chez un ami | `jobs/kinds/index.py`, `jobs/registry.py`, `network/work.py` |
| 14 | moyenne | moteurs | A, B | les llama-server enfants du routeur écoutent sans clé, avec CORS ouvert | clé par l'environnement (`LLAMA_API_KEY`), héritée par les enfants ; 401 exigé une fois prêt | `llm/engine.py`, `cluster/engine.py` |
| 15 | moyenne | protocole | C | une balise UDP réécrit l'adresse, le port et le nom d'une machine appairée | une balise n'est qu'un indice : la nouvelle adresse n'est retenue qu'après un ping scellé qui dit où il est arrivé | `network/beacons.py`, `network/manager.py`, `network/p2p_app.py` |
| 16 | moyenne | complétude | D (F) | profil non validé : un NaN ou un champ mal typé fait tomber la vue Réseau et la liste des modèles | profil typé ; JSON strict entre machines (ni NaN ni infini) ; défense en profondeur dans le choix des modèles | `network/profile.py`, `network/envelope.py`, `network/remote_models.py`, `network/placement.py` |
| 17 | moyenne | complétude | C (B, D) | serveur entre machines : corps sans limite, lu avant de savoir qui l'envoie | taille bornée avant lecture, selon l'action et le chemin ; connexions bornées ; en-têtes en 10 s | `network/body_limit.py`, `network/connections.py`, `network/p2p_app.py` |
| 18 | moyenne | services | D | `work/blob` donne à un ami l'image de départ d'une retouche | refusé à un ami ; décalage demandé vérifié | `network/work.py` |
| 19 | moyenne | services | D, F | la machine qui calcule télécharge un fichier de la taille que choisit l'autre | taille et morceaux bornés, place sur le disque vérifiée ; réponses du propriétaire bornées | `network/worker.py`, `network/client.py` |
| 20 | moyenne | stockage | B (D) | Linux et macOS : base, journaux et `instance.json` lisibles par les autres comptes ; clé de llama-server en ligne de commande | umask 077, dossier 0700, fichiers 0600, droits relus et signalés ; clé par l'environnement | `paths.py`, `__main__.py`, `node/vault.py`, `llm/engine.py` |
| 21 | moyenne | services | F, H | archives piégées (odt, docx, pptx, xlsx) : décompression sans borne | archive contrôlée avant tout lecteur : entrées, taille décompressée, parties XML, feuilles | `services/archives.py`, `services/extract.py`, `services/epub.py` |
| 22 | moyenne | interface | H, D, F | liens à protocole dangereux (`search-ms:`, `ms-word:`, `intent:`…) ouverts hors du navigateur | seuls http, https et mailto (nettoyé) restent des liens, dans la page, la fenêtre et l'appli | `render.py`, `dom.js`, `views/chat.js`, `ui/window.py`, `MainActivity.java` |
| 23 | moyenne | mises à jour | D, F, G | un pair monopolise le fil des mises à jour (filet d'octets, versions fictives) | échéance par appel, débit minimal par fichier, machine mise de côté 10 min, réception dans un fil à part | `updates/p2p.py`, `updates/service.py`, `updates/store.py` |
| 24 | moyenne | mises à jour | D, F | un profil d'ami mal formé arrête le service de mise à jour | profil lu sans rien supposer de sa forme ; chaque étape du tour isolée | `updates/p2p.py`, `updates/service.py` |
| 25 | moyenne | mises à jour | D, F, G | enveloppe signée gardée et relayée telle quelle (approbations bidon, champs géants) | enveloppe canonique, texte borné, approbations vérifiées et bornées ; le texte signé nomme les appareils d'approbation | `updates/manifest.py`, `updates/store.py`, `tools/publish.py` |
| 26 | moyenne | compagnon | D | aucune borne sur les connexions, les flux et les corps d'un pair autorisé | connexions, flux, places, taille, silence et débit bornés ; réponse non lue coupée | `compagnon/src/pool.rs`, `endpoint.rs`, `inbound.rs`, `relay_body.rs` |
| 27 | basse | moteurs | B | modèle d'images repris par lien physique, modifiable depuis le dossier d'origine | lien gardé seulement si le fichier est privé ; sinon copie vérifiée avant chaque lancement | `imaging/local_copies.py` |
| 28 | basse | contenus | D | indexation d'un dossier : dossiers cachés de Windows et données d'AIODOS non exclus | fichiers cachés ou système, fichiers de clés, AppData, dossiers d'AIODOS et liens jamais parcourus | `jobs/kinds/index.py` |
| 29 | basse | services | D | un ami fait échouer mes travaux (bail puis échec, en boucle) | unités détenues, demandes et durée bornées ; son échec ne coûte pas d'essai à une unité que mes machines savent calculer | `network/work.py` |
| 30 | basse | contenus | H, D | lecture de page web sans taille maximale ni délai total | 5 Mio et 30 s au plus, jeux de caractères du Web seulement ; recherche web bornée de même | `services/web.py` |
| 31 | basse | services | D, F | nom du modèle Whisper imposé par l'autre machine | seuls les noms de faster-whisper ; rien de téléchargé pour un ami | `services/media.py`, `services/whisper_child.py`, `network/worker.py` |
| 32 | basse | contenus | D | la même : téléchargement de n'importe quel dépôt Hugging Face | idem ; modèle chargé par son dossier dans le cache, jamais par son nom | `services/whisper_child.py` |
| 33 | basse | API locale | H | pont pywebview : l'objet fenêtre atteignable depuis le JavaScript | quatre fonctions exposées une à une, appel vérifié (page de l'interface) ; la fenêtre ne quitte pas l'interface | `ui/window.py`, `ui/static/js/app.js` |
| 34 | basse | stockage | D | retour à une version d'origine 2.0.x : niveaux de confiance ignorés là où les clés sont en clair | ami refusé sur ces nœuds ; clés des amis marquées `k2:` hors DPAPI (migration 18) | `updates/launcher.py`, `network/pairing.py`, `network/group.py`, `node/vault.py`, `network/peers.py`, `db.py` |
| 35 | basse | compagnon | B | API locale et ports de tunnel : ni délai d'en-têtes, ni limite de connexions | en-têtes en 10 s, connexion fermée après un refus, 512 connexions sans jeton au plus | `compagnon/src/control.rs`, `admission.rs`, `tunnel.rs` |
| 36 | basse | mises à jour | G | installeur : paquets pip épinglés par numéro, sans empreinte | paquets figés par empreinte (`--require-hashes`) | `tools/build_installer.py`, `tools/installer/paquets.txt` |

### Les 10 failles partagées

| Faille | Suite donnée |
|---|---|
| Présentation par groupe : un ami pourrait faire appairer d'office des identités arbitraires (`network/group.py`) | pas de changement dans ce lot |
| Un ami peut monopoliser un modèle prêté, sans limite de longueur ni de durée (`network/lending.py`) | pas de changement dans ce lot |
| Réseau public : un ami peut relier la clé publique d'un nœud à sa machine, par la DHT (`compagnon/src/rendezvous.rs`) | pas de changement dans ce lot |
| Annuaire public saturable par des clés jetables (`compagnon/src/public.rs`) | pas de changement dans ce lot |
| Retour silencieux à la version d'origine, et refus définitif d'un correctif qui plante deux fois (`updates/launcher.py`) | en partie : le retour est signalé (avis « rollback ») |
| L'appareil d'approbation signe ce qu'on lui renvoie au clic, pas ce qu'il a montré (`updates/approval.py`) | corrigé : le clic porte l'empreinte du texte montré (§ 3.7) |
| sd-server sans clé d'accès et avec CORS (`imaging/engine.py`) | en partie : ports refusés par les navigateurs, port revérifié à chaque demande ; un programme local peut encore lui parler |
| Traversée de chemin par un nom « reparti:<modèle> » sous Windows (`cluster/weights.py`) | corrigé : nom aux règles d'Ollama, empreinte vérifiée, noms réservés de Windows refusés |
| Dossier d'un moteur (`sdcpp_dir`, `llamacpp_dir`) changé par l'API au jeton : programme choisi lancé (`imaging/engine.py`) | pas de changement : il faut le jeton, et son vol (n° 0) est fermé |
| WebView Android : un lien forgé ouvre une intention de schéma quelconque (`MainActivity.java`) | corrigé avec le n° 22 |

### Les 6 failles réfutées

Les vérificateurs les ont écartées : la réponse de `pair/start` côté initiateur, non
assainie ; les indices Internet d'un pair (relais en `http://`, adresses quelconques) ; le
jeton du compagnon écrit sans droits restreints (lancement à la main) ; la clé de
publication gardée en clair, l'approbation non activée ; le jeton d'API rangé dans
`sessionStorage`, lisible par tout script de la page ; le récepteur de démarrage de l'appli
Android, exporté (`QUICKBOOT_POWERON`).

## 3. Ce qui change, par surface

### 3.1 Interface et API locale

**En-têtes** (`api/security.py`). Toute réponse de l'interface et de l'API locale, refus
compris (400, 401), porte `X-Content-Type-Options: nosniff` et
`Referrer-Policy: no-referrer`. L'interface reçoit cette politique de contenu :

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:;
connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none';
frame-ancestors 'none'
```

Seuls les scripts du nœud s'exécutent, et aucune requête ne part vers un autre hôte. Les
styles en ligne restent permis : les vues en posent beaucoup, et un style ne lance rien.
L'API reçoit `sandbox; frame-ancestors 'none'` : elle n'est jamais exécutée comme une page.
Seuls les images, les vidéos et les sons en sont dispensés : ils ne lancent rien, et
s'ouvrent ainsi dans un onglet.
`api/server.py` impose le type des fichiers `.js`, `.css` et `.html` : sous `nosniff`, un
registre Windows qui annonce `text/plain` laisserait sinon l'interface blanche. Le jeton
est comparé en octets : un jeton accentué reçoit un 401, pas une erreur 500.

**Rendu des réponses** (`render.py`).
- Une image n'est jamais chargée d'office. Elle devient le texte « [image : …] »,
  cliquable vers son adresse si elle est en http(s), du texte seul sinon. Seule une image
  écrite dans la réponse même s'affiche : `data:image` png, jpeg, gif ou webp en base64.
- Un lien ne reste cliquable que vers le web ou un courriel (http, https, mailto). Les
  autres (`search-ms:`, `ms-word:`, `intent:`, adresses relatives…) restent du texte, avec
  l'adresse au survol.
- Un lien mailto ne garde que `to`, `cc`, `subject` et `body` : `attach=…`, `bcc` et le
  fragment `#…` tombent.
- Le rendu se fait en une passe, même pour une réponse de milliers de liens.

**Clics** (`dom.js`, `views/chat.js`). `linkAction` décide pour chaque clic, clic du
milieu compris : rien ne s'ouvre hors du web et du courriel. Une source « web » d'une
réponse n'est un lien que si son adresse est en http(s). Du profil d'une autre machine,
l'interface n'insère que des nombres finis, des oui/non et du texte échappé.

**Fenêtre Windows** (`ui/window.py`).
- Le pont vers Python n'est plus un objet : quatre fonctions sont exposées une à une
  (`pick_files`, `pick_folder`, `save_image`, `show_image_file`). Chacune vérifie que
  l'appel vient de l'interface du nœud, et qu'un numéro d'image est un entier positif.
  L'objet `js_api` ne mène nulle part, même par `__class__`.
- La fenêtre refuse toute navigation hors de l'interface, et répond 403, sans rien
  envoyer, à toute requête vers un autre hôte. Annuler la navigation ne suffisait pas :
  WebView2 envoyait la requête quand même (essai du 20/10).
- Un lien `target=_blank` ne s'ouvre dehors que vers http, https ou mailto (nettoyé).
- Un fichier ou un lien lâché sur la fenêtre est ignoré hors des vraies zones de texte
  (`app.js`, `keepDrops`).

**Appli Android** (`MainActivity.java`). Un lien ne sort que vers http, https ou mailto
(nettoyé de même), avec `CATEGORY_BROWSABLE`. Sinon, le message « Lien non ouvert »
s'affiche.

**Vue « Cette machine ».** Elle dit ce qui reste lisible par d'autres comptes de la
machine (Linux, macOS, Android : § 3.8). Cette liste ne part jamais dans le profil envoyé
aux autres.

### 3.2 Contenus

**Garde des adresses** (`services/addresses.py`). Une page lue pour quelqu'un d'autre que
l'utilisateur de cette machine (une unité « page web », un résultat de recherche) ne joint
que l'Internet public. Sont refusés : la boucle locale, le réseau privé, le lien local (dont
les métadonnées cloud, 169.254.169.254), le CGNAT, le multicast et tout ce qui est
réservé ; les IPv6 qui enveloppent une IPv4 sont ouvertes et jugées sur celle-ci.
- Le nom est résolu une fois, chaque adresse vérifiée, puis la connexion part vers
  l'adresse vérifiée elle-même : pas de seconde résolution (*DNS rebinding*). Le nom reste
  celui de la requête (en-tête `Host`, SNI, certificat).
- Chaque redirection (5 au plus) repasse par la garde. Ni mandataire, ni `.netrc`.
- Le message d'erreur est toujours le même, sans hôte ni port : un ami ne sonde pas les
  ports de mon réseau par les échecs.
- La garde passe par des fonctions internes d'urllib3. Si une version les change, la
  lecture est refusée (`GUARD_BYPASSED`) plutôt que faite sans garde. D'où les versions
  figées dans l'installeur et l'appli Android : requests 2.33.1, urllib3 2.6.3.

**Pages publiques, pages locales** (`jobs/kinds/index.py`).
- Une page publique (`index_url`) est réservée à mes machines, dans les deux sens : elle
  n'est jamais confiée à un ami ni lue pour lui. Elle ne part qu'à une de mes machines à
  jour (révision 2 du contrat, § 3.4).
- Une page du réseau local (`index_url_here`) se lit seulement sur la machine où on la
  demande. Elle se reconnaît sans résolution de nom : une adresse IP privée écrite telle
  quelle, ou un nom local (sans point, ou en `.local`, `.lan`, `.home`, `.internal`…). Un
  intranet au nom DNS public (`intranet.entreprise.fr`) n'est pas lu comme page locale.

**Lecture d'une page** (`services/web.py`) : 5 Mio et 30 s au plus, redirections comprises
(connexion 10 s, lecture 15 s). Les types binaires sont ignorés sans être lus. Seuls les
jeux de caractères du Web sont suivis : punycode se décodait en temps plus que quadratique,
pendant des heures. Stop interrompt la page en cours, et les suivantes ne sont pas lues. La
**recherche web** est bornée de même : 2 Mio et 20 s, sans mandataire, interruptible, et
seuls ses résultats en http(s) sont rendus.

**Documents** (`services/archives.py`, `extract.py`, `epub.py`). Un document qui est une
archive zip (docx, pptx, xlsx, xlsm, odt, epub) est contrôlé avant tout lecteur :
- 10 000 entrées au plus, 200 Mio une fois décompressé ;
- 16 Mio par partie XML, 40 Mio de XML en tout. Une partie XML se reconnaît à son début,
  pas seulement à son nom (un `document.xml` renommé `document.dat` passait) ;
- ni entrée chiffrée, ni compression autre que deflate ;
- les feuilles d'un classeur, lues en flux, sont dispensées des bornes XML mais vérifiées
  dans leur forme : 1 200 000 éléments hors des lignes, 200 000 par ligne, numéros de ligne
  jusqu'à 1 048 576. Les dimensions annoncées par la feuille sont ignorées ;
- EPUB : toute entrée que le manifeste déclare en HTML ou en XML compte comme balisage ; un
  chapitre de plus de 16 Mio est sauté, la lecture s'arrête à 40 Mio, chaque chapitre est
  lu une fois.

Un HTML ou un RTF de plus de 16 Mio n'est pas lu, ni un RTF imbriqué sur plus de 1 000
niveaux. Le texte d'un .odt se tire en temps linéaire. Une extension inconnue qui n'est pas
du texte ne devient pas une unité. La raison d'un document non lu s'affiche avec l'unité
sautée.

**Parcours d'un dossier** (`jobs/kinds/index.py`). Un parcours ne ramasse jamais :
- les fichiers et dossiers cachés (nom en « . », attribut caché ou système de Windows) ;
- les fichiers de clés et de mots de passe (`.key`, `.pem`, `.p12`, `.pfx`, `.kdbx`,
  `id_rsa`, `credentials`, `client_secret*.json`…) ;
- les données des applications de Windows (AppData) ;
- les dossiers d'AIODOS (données de la fenêtre, journaux, fichiers reçus, moteurs,
  modèles, copie installée), même choisis directement ;
- les liens symboliques et les jonctions.

Un fichier choisi nommément est indexé.

**Contrôle des résultats d'un ami** (`jobs/kinds/index.py`, `network/work.py`).
- Seul part chez un ami ce dont le résultat se contrôle ici : un fichier texte, une
  extension inconnue lue comme du texte, un document que cette machine sait relire. Jamais
  une image, un son, une vidéo, une page web, ni un format sans lecteur ici : mes machines
  les calculent. Une tâche ajoutée plus tard sans crochet de contrôle ne part pas non plus
  (`registry.friend_checkable`).
- Au retour, le document est relu et redécoupé ici. Les passages rendus doivent être
  exactement ce découpage : ni morceau choisi, ni oubli, ni répétition. Puis 3 vecteurs
  sont recalculés ici (cosinus d'au moins 0,999).
- Un écart sur un fichier texte inchangé met l'ami de côté. Un écart sur un document (deux
  versions d'un lecteur peuvent lire un peu autrement), ou sur un fichier modifié depuis
  qu'il lui a été confié, est refusé sans reproche, et l'unité est recalculée chez moi.
- Un document « sauté » par l'ami alors qu'il a du texte ici est un écart. Un saut fondé
  garde la raison de ma machine, pas un texte de l'ami.
- Le découpage fait partie du contrat entre machines : changer `knowledge/chunking.py`
  impose d'incrémenter `network/work.WORK_API`.

**Annuler arrête vraiment** (`jobs/executor.py`). Annuler ou supprimer un travail arrête
en quelques secondes ses unités en cours ici : lecture d'une page, transcription, image ou
vidéo. Ce qu'elles calculaient n'entre ni dans les bibliothèques ni dans la galerie, et
l'unité ne compte pas d'échec. Fermer AIODOS coupe aussi une page en cours de lecture.

### 3.3 Appairage

**Protocole 2, avec engagement** (`network/crypto.py`, `network/pairing.py`), comme la
comparaison numérique du Bluetooth :
1. la machine qui lance l'appairage envoie un engagement : l'empreinte SHA-256 de sa clé
   publique X25519, d'un nonce de 32 octets et des deux identifiants. Pas sa clé ;
2. l'autre répond avec sa clé et son nonce ;
3. la première révèle sa clé et son nonce, que l'autre compare à l'engagement.

Le code à 6 chiffres et la clé de la paire sont tirés du secret commun **et** de toute la
transcription (identifiants, clés, nonces). Un intermédiaire ne peut plus choisir sa clé
après avoir vu les autres : chaque essai n'a qu'une chance sur un million, et un échec se
voit (codes différents). Un nœud qui répond accepte au plus 10 demandes par période de
3 minutes, quelle qu'en soit l'issue, et en garde 3 en attente.

**La demande vue est celle qu'on accepte.**
- Une demande en attente n'est jamais remplacée ; une autre pour la même machine est
  refusée jusqu'à son issue ou son expiration (3 minutes).
- Une révélation fausse garde la place jusqu'à l'expiration. La révélation doit venir du
  même appelant que l'engagement : même adresse sur le réseau local, même invitation par
  Internet.
- La réponse de l'utilisateur porte l'identifiant de la demande montrée (`ref`) : une
  demande arrivée entre-temps est refusée.
- Une machine déjà appairée n'est remplacée que si l'utilisateur choisit « remplacer sa
  clé ». C'est revérifié au moment d'accepter, puis au moment de conclure.
- La machine qui répond à l'adresse doit être celle choisie dans la liste. La fenêtre
  montre l'adresse jointe, prévient si la machine est déjà appairée, ou si une autre
  demande est ouverte en même temps.

**Versions d'avant.** Une machine sans engagement (2.0.1 et avant) reçoit un refus qui dit
de mettre AIODOS à jour, dans un sens comme dans l'autre. Jamais l'ancien échange, plus
faible.

**Appairage par groupe** (`network/group.py`). Son échange de clés ne change pas
(`pair_secrets`) : les clés passent par des paires déjà vérifiées par un humain, et une
version d'avant tire la même clé d'une présentation. Un nœud dont la version d'origine
ignore les amis les refuse, par code comme par présentation (§ 3.7).

### 3.4 Réseau entre machines

**Connexions au serveur entre machines** (port 47701, `network/connections.py`).
- Les en-têtes d'une requête doivent arriver en 10 s.
- Un corps doit arriver à 8 Kio/s en moyenne après 10 s, sauf depuis cette machine (le
  compagnon a ses propres bornes).
- 32 connexions à la fois par adresse inconnue, 256 pour toutes les adresses inconnues
  réunies. L'adresse connue d'une de mes machines ou d'un ami en a 128, cette machine 512.
  Un appareil inconnu ne prive donc pas mes machines du serveur.
- L'en-tête `X-Aiodos-Peer` (clé Internet de l'appelant) n'est cru que d'une connexion
  venue de cette machine : seul le compagnon l'ajoute.

**Taille des requêtes**, bornée avant d'être lue (`network/body_limit.py`) :

| Requête | Au plus |
|---|---|
| `hello` | 4 Kio |
| début d'appairage, action inconnue, machine non appairée | 64 Kio |
| message scellé d'une machine appairée arrivée par son chemin connu (son adresse, ou sa clé Internet par le compagnon) | 16 Mio ; 64 Kio par un autre chemin |
| résultat (`work/result`) d'une image | environ 58 Mio |
| résultat d'une vidéo | environ 911 Mio |
| index d'un document rendu par une de mes machines | 1 Gio |
| index rendu par un ami | 16 Mio |

« from », en tête de l'enveloppe, ne dit que qui prétend envoyer : les identifiants
circulent en clair dans les balises. D'où le chemin connu. Une machine qui vient de changer
d'adresse n'envoie de gros messages qu'une fois reconnue, après son prochain ping (toutes
les 15 s). Au-delà de 64 Kio, 2 corps sont lus à la fois par machine et 8 en tout ; au-delà
de 16 Mio, 1 et 2. Les suivants attendent 45 s au plus, puis reçoivent un 503. Une taille
annoncée trop grande reçoit un 413 sans rien lire. Un corps de plus de 64 Kio n'est analysé
que s'il a la forme d'une enveloppe (quelques valeurs simples), hors de la boucle du nœud :
quelques Mio de `[{},{},…]` deviendraient sinon des centaines de Mio d'objets.

**JSON strict** (`network/envelope.py`). Un message scellé qui contient NaN, Infinity ou
1e999 est refusé (403). Une machine à jour envoie une valeur non finie en `null`. Une
enveloppe mal formée est refusée, au lieu de lever une erreur 500.

**Balises** (`network/beacons.py`, `network/manager.py`). Une balise UDP n'est plus qu'un
indice. Si elle annonce une nouvelle adresse pour une machine appairée, un ping scellé part
vers cette adresse. La machine y dit, dans sa réponse scellée, l'adresse et le port où la
requête lui est arrivée (`where`) : la nouvelle adresse n'est retenue que s'ils sont bien
ceux annoncés. Un appareil qui relaierait la connexion vers la vraie machine est donc
refusé. Le nom ne vient jamais d'une balise. Une machine d'une version d'avant ne dit pas
`where` : elle n'est plus suivie sur balise, et le journal le dit une fois. La liste des
machines visibles garde 64 inconnues au plus, sans compter les machines appairées. Chaque
machine appairée est rafraîchie à part : une machine injoignable ne retarde pas les autres.

**Profil** (`network/profile.py`). Le profil reçu d'une machine (réponses à `profile` et
`ping`) est typé et borné avant d'être gardé, affiché ou utilisé, puis de nouveau à la
relecture. Chaque clé a son type et ses bornes ; ce qui ne les respecte pas est abandonné,
et ce qui n'est pas décrit ne passe pas. Les chaînes sont coupées (60 caractères pour un
nom de machine) ; caractères de contrôle, caractères invisibles (U+202E, U+200B…) et
chevrons y deviennent des espaces. 300 modèles au plus. Une clé ajoutée au profil doit être
décrite dans `profile.SCHEMA`, sinon les autres machines ne la voient pas.

**Réponses reçues** (`network/client.py`). Toute réponse d'une autre machine est lue en
flux, jamais décompressée (une réponse compressée est refusée), et la lecture s'arrête au
plafond de l'action (§ 4).
- Échéance d'un appel : la connexion de chaque chemin, plus l'envoi du corps à 8 Kio/s,
  plus 4 délais de lecture du plus long chemin. Soit environ 50 s pour un appel ordinaire
  sur le réseau local.
- Un appel qui dépasse son échéance est abandonné et sa connexion coupée. Tant qu'il n'a pas
  fini, rien d'autre n'est demandé à cette machine : elle ne retient pas les fils un à un.
- Au-delà de 64 Kio, une réponse n'est reconnue qu'à sa forme stricte `{"box": "…"}`.
- Un refus est lu sur 64 Kio au plus, et montré sur une ligne, en 500 caractères au plus.
- Une conversation (réponse en flux) : chaque morceau fait 1 Mio au plus, et pas plus de
  16 conversations à la fois vers une même machine. Les suivantes attendent leur tour, 9
  minutes au plus ; Stop interrompt l'attente.

**Travail confié à un ami** (`network/work.py`, chez le propriétaire).
- Il détient 4 unités à la fois au plus. Ses demandes restées sans unité, et les unités
  qu'il rend ou rend en échec, sont comptées : 60 par minute au plus.
- Une unité reste chez lui 1 h au plus, 30 min pour une image, 2 h pour une vidéo ; au-delà,
  elle lui est reprise comme un échec.
- Le dernier essai d'une unité que cette machine, ou une de mes machines en ligne, sait
  calculer ne part jamais chez un ami. Son échec la remet en file sans compter d'essai, et
  elle ne lui revient pas.
- Une unité que seuls mes amis savent calculer (une image, si je l'accepte) : après un
  échec, un autre ami passe d'abord pendant 5 min ; après 2 essais, elle échoue avec un
  message qui le dit.
- 10 échecs à la suite, sans un résultat accepté, le mettent de côté.
- L'ordre dit la taille de résultat acceptée (`result_max`). Un index trop gros n'est pas
  envoyé : il est rendu en échec (`too_big`), sans reproche, et l'unité revient chez moi.
  Pour une image ou une vidéo, ce n'est pas une excuse.
- L'image de départ d'une retouche ne part jamais chez un ami, même s'il la demande par
  `work/blob`.

**Calculer pour une autre machine** (`network/worker.py`).
- Le fichier d'une unité fait 512 Mio au plus, et il doit rester 1 Gio libre sur le disque.
  Chaque morceau est vérifié : jamais plus long que demandé.
- Les réponses du propriétaire sont bornées : 4 Mio pour un ordre, environ 3,6 Mio pour un
  morceau de fichier, 256 Kio pour le reste.
- La ressource « net » (lire le Web) n'est jamais proposée à une machine qui n'est pas à
  moi, et une image, un son ou une vidéo à indexer venant d'un ami est rendu sans être
  téléchargé ni décodé.
- Une unité rendue parce que je ne la calcule pas pour cette machine, ou faute de place,
  met cette machine au repos 10 min : plus d'allers-retours sans fin avec une version
  d'avant. Un modèle Whisper absent : plus de son ni de vidéo de cette machine pendant 1 h.
- Mes machines sont interrogées avant les amis. Un propriétaire qui laisse une demande
  aller jusqu'à l'échéance n'est redemandé qu'après 5 min.
- Un ami n'apprend d'un échec que la sorte d'erreur, ou le refus de son ordre ; ni chemin,
  ni nom de compte, ni disque plein. Mes machines lisent le message complet.

**Durée d'une image** (`imaging/gallery.py`, migration 19). La durée de calcul dite par la
machine qui a fait l'image n'est gardée que finie, entre 0 et 7 jours. Un « inf » envoyé
par un ami faisait tomber la galerie et la conversation qui l'avait demandée.

### 3.5 Compagnon

Le détail de l'API et des bornes est dans [compagnon/README.md](../compagnon/README.md).
- **API locale** : en-têtes en 10 s ; une connexion refusée faute de jeton est fermée ;
  512 connexions sans jeton au plus, la plus ancienne cédant sa place.
- **Machine autorisée** (un ami appairé) : 4 connexions QUIC à la fois, 64 flux par
  connexion ; 32 conversations et 32 autres requêtes relayées à la fois, les suivantes
  attendant 30 s avant un 503 ; corps de 64 Mio au plus (1 Gio pour un résultat de calcul),
  60 s de silence au plus, débit minimal de 16 Kio/s après 120 s ; réponse qu'elle ne lit
  plus coupée au bout de 60 s, ce qui rend sa place.
- **Tunnel gardé** (machine qui emprunte) : il ne relaie que le programme nommé par AIODOS
  (le llama-server qu'il vient de lancer), reconnu par la table des connexions du système.
- **Service tenu** (machine qui prête) : le compagnon garde lui-même ses connexions au
  serveur RPC, et aucun autre programme de la machine n'y est servi. S'il en voit un, il le
  dit (`blocked`), et AIODOS arrête le prêt.
- **Capacités** annoncées dans la ligne « ready » et dans l'état : `service-usage`,
  `body-pace`, et, là où la table des connexions se lit (Windows, Linux), `tunnel-owner` et
  `service-hold`. AIODOS refuse ce qu'un compagnon plus ancien ferait sans protection.

### 3.6 Moteurs

**llama-server** (`llm/engine.py`, `cluster/engine.py`). Sa clé passe par l'environnement
(`LLAMA_API_KEY`), jamais par la ligne de commande, que tout compte lit (`ps`,
`/proc/<pid>/cmdline`). Le routeur la donne à ses llama-server enfants, un par modèle, qui
écoutaient avant sans clé. Une fois le programme prêt, AIODOS vérifie qu'il répond 401 sans
la clé ; sinon il l'arrête et dit de mettre le moteur à jour.

**Journaux des moteurs** (`logfiles.py`). La sortie de llama-server, de ggml-rpc-server et
de sd-server passe par un tuyau vers un journal de 4 Mio, puis `.1`. Seuls les 4 plus
récents journaux de chaque rôle (`llama-rpc-*`, `llama-server-*`) restent, plus ceux des
programmes en marche.

**Prêter sa carte** (`cluster/helper.py`, `node/identity.py`).
- Le compagnon tient le serveur RPC (§ 3.5). AIODOS refuse de prêter si son compagnon
  n'annonce pas `service-usage` et `service-hold`, et si la machine qui emprunte ne dit pas
  que son tunnel est gardé.
- Le prêt s'arrête quand la connexion du tunnel tombe (2 s une fois le modèle chargé, 30 s
  avant), et quand le compagnon s'arrête, ne tient plus le serveur ou y voit un autre
  programme. Il s'arrête toujours aussi faute de nouvelles pendant 90 s.
- Le cache des poids reçus est ramené sous 48 Gio au début de chaque prêt, puis toutes les
  60 s : le client du serveur RPC y écrit ce qu'il veut.
- Le téléphone ne prête ni n'emprunte de carte : il ne peut pas vérifier quel programme se
  connecte. Une machine n'annonce sa carte que si le prêt serait accepté ; sinon, le
  journal dit une fois pourquoi.
- Le réglage « Prêter ma carte graphique » reste activé par défaut, pour mes machines
  seulement, jamais pour un ami.

**Emprunter une carte** (`cluster/group.py`). Les tunnels s'ouvrent juste avant
llama-server, réservés à lui (son numéro de programme est donné au compagnon dès le
lancement), avec 4 connexions au plus, et se ferment dès qu'il s'arrête. Il faut un
compagnon qui annonce `tunnel-owner`, donc Windows ou Linux. Le nom d'un modèle « réparti »
ne devient un chemin que s'il suit les règles de nom d'Ollama (`cluster/weights.py`).

**sd-server** (`imaging/engine.py`, `imaging/generate.py`). Il n'a pas de clé et répond à
toute page web (CORS ouvert). Il n'écoute donc que sur 127.0.0.1, sur l'un de 15 ports que
les navigateurs refusent de joindre : 1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000,
6665 à 6669, 6697. S'ils sont tous pris, il n'est pas lancé. À chaque demande, AIODOS
vérifie que c'est bien lui, seul, qui écoute sur son port : avant d'envoyer la demande
(donc l'image de départ d'une retouche), puis à chaque réponse, avant de croire un
« terminé ». Sinon l'image est abandonnée, et à la demande suivante sd-server est arrêté
puis relancé sur un autre port. Sous macOS et Android 10 et après, le système ne dit pas
qui écoute : seul compte que sd-server tourne encore.

**Whisper** (`services/media.py`, `services/whisper_child.py`). Seuls les noms de modèles
de faster-whisper sont acceptés : un autre nom serait pris pour un dossier de ce PC ou un
dépôt Hugging Face. Pour une unité d'une de mes machines, un modèle connu qui manque se
télécharge ; pour un ami, rien. Le programme de transcription refait la vérification, et
charge le modèle par son dossier dans le cache, jamais par son nom.

**Modèles d'images trouvés sur le PC** (`imaging/local_copies.py` ; ComfyUI, PANDORA, un
autre AIODOS). Un fichier n'est relié (lien physique) que s'il est privé et que ses droits
se lisent en entier ; sinon il est copié (toujours sous macOS). Avant chaque lancement de
sd-server, un fichier que d'autres comptes peuvent modifier est remplacé par une copie
vérifiée. sd-server n'est pas lancé tant que ce n'est pas fait. Une copie ne lit jamais plus
que la taille attendue.

### 3.7 Mises à jour et publication

**Outils de publication** (`tools/publish.py`, `build_installer.py`, `build_android.py`,
`aiodos/updates/private.py`). Avant d'importer quoi que ce soit du dépôt, ils le
contrôlent : aucun `__pycache__` lu, dépôt et dossier des outils retirés de `sys.path`,
`private.py` chargé par son chemin. Ils refusent :
- un dépôt (`.git` compris) que d'autres comptes peuvent modifier, ou qui a un fichier d'un
  autre compte ;
- un fichier non suivi dans `aiodos/` ou `tools/`, un module ou un programme non suivi à la
  racine ;
- les réglages git qui feraient lire ou exécuter ce que le dépôt ne montre pas (`include`,
  filtres, `textconv`, `attributesFile`, `worktree`) et les signes d'un dépôt piégé
  (`fsmonitor` qui n'est pas un booléen, `hooksPath`, `sshCommand`, `pager`, `editor`,
  `diff.external`, programme de signature, crochet actif, `refs/replace`,
  `.git/info/attributes`) ;
- une clé de publication que d'autres comptes peuvent lire ;
- un SDK Android, un JDK, un Python 3.13, cargo, le NDK ou Inno Setup modifiables par
  d'autres comptes ;
- pour l'appli Android : des fichiers non commités dans `aiodos/` ou `compagnon/`.

Git tourne sans ce que sa configuration ferait exécuter (`--no-replace-objects`,
`core.fsmonitor=false`, crochets coupés). Le compagnon se construit dans un dossier privé
(`%LOCALAPPDATA%\aiodos-build`), et une configuration `.cargo` posée dans un dossier
parent est refusée. Chaque refus dit quoi corriger.

**Installeur** (`tools/installer/aiodos.iss`, `tools/build_installer.py`).
- Le dossier est imposé : `%LOCALAPPDATA%\Programs\AIODOS`. Un dossier hors du profil
  (`/DIR=C:\AIODOS`) est refusé : sous `C:\` ou sur un autre disque, il hériterait de
  « Utilisateurs authentifiés : modification ».
- Une ancienne installation faite ailleurs est signalée, et le raccourci du démarrage avec
  Windows est refait sur la nouvelle.
- Les paquets Python embarqués sont figés par empreinte (`tools/installer/paquets.txt`,
  `--require-hashes`, roues seulement) ; `--lock` les relève.

**Manifeste et relais** (`updates/manifest.py`, `updates/store.py`). La signature ne
couvre que le texte du manifeste. L'enveloppe se garde et se retransmet donc sous sa forme
canonique : texte, clé, signature, et 8 approbations bien formées au plus. Le texte fait
256 Ki caractères au plus. Le texte signé nomme les appareils d'approbation du commit
publié (`approvers`, lu dans son `keys.py`) : toute machine qui relaie ne garde que leurs
approbations valides, même si elle n'en déclare aucun. Une version ne s'installe qu'avec
l'approbation d'un appareil que la machine déclare elle-même.

**Transferts entre machines** (`updates/p2p.py`, `updates/service.py`).
- Chaque appel dure 150 s au plus. Chaque morceau doit faire exactement la taille demandée.
  Chaque fichier doit arriver à 32 Kio/s en moyenne, plus 120 s.
- Une machine qui n'a pas fourni ce qu'elle annonçait est mise de côté 10 min, quel que
  soit le numéro qu'elle annonce ensuite. La suivante est essayée aussitôt, 3 au plus par
  tour : mes machines d'abord, puis les amis, puis les machines mises de côté.
- Un téléchargement interrompu ne reprend que chez la même machine (un fichier `.part` par
  machine, 3 au plus par fichier) : une empreinte fausse accuse ainsi la machine qui a tout
  envoyé.
- La réception tourne dans un fil à part : une machine lente ne retient ni l'installation
  d'une version déjà reçue, ni le redémarrage. Chaque étape du tour est isolée, et son
  erreur s'affiche.

**Approbation** (`updates/approval.py`, `views/approval.js`). Chaque version en attente
montre l'empreinte de son texte (16 caractères, l'empreinte entière au survol), et le clic
l'envoie : seul ce texte est signé. Une machine qui montre deux textes sous un même numéro
reste suspecte jusqu'au redémarrage de l'appareil d'approbation : l'avertissement s'affiche
en rouge, et l'approbation demande une confirmation. `publish.py` affiche « Empreinte du
texte : … » : la comparer avec celle de l'appareil avant d'approuver.

**Retour à une version 2.0.x** (`updates/launcher.py`). Un nœud Termux ou Linux installé
depuis une archive 2.0.0 ou 2.0.1 peut rendre la main à cette version d'origine, qui ignore
les amis. Il refuse donc les amis, par code comme par présentation, et le dit dans Réglages
→ Mises à jour : il faut le réinstaller depuis une version récente. Windows (DPAPI) et
l'appli Android ne sont pas concernés par ce refus. Partout où DPAPI manque, la clé d'un
ami ou d'une machine mise de côté est en plus précédée de `k2:` (35 octets ; migration 18
pour les paires existantes) : une 2.0.x ne sait pas s'en servir, et continue de servir mes
machines. Ce n'est pas un chiffrement.

### 3.8 Stockage

**Journaux d'AIODOS** (`__main__.py`). `aiodos.log` fait 10 Mio au plus, plus 3
sauvegardes ; `erreurs.log` 2 Mio, plus `.1`. Une même alerte n'est écrite que 10 fois par
minute. Si la rotation est impossible (fichier ouvert ailleurs, en lecture seule), le
journal le dit une fois, réessaie une fois par minute et se tait au double de sa taille.

**Linux, macOS, Android** (`paths.py`). AIODOS pose l'umask 077 au démarrage, avant
d'écrire quoi que ce soit : son dossier de données est en 0700, ses fichiers en 0600 (base
et fichiers `-wal` et `-shm`, journaux, `instance.json`). Ce qui existait déjà est fermé au
démarrage. Après `chmod`, les droits sont relus : un partage CIFS, un disque Windows vu par
WSL ou une carte FAT acceptent `chmod` sans rien changer. Ce qui reste ouvert est écrit au
journal et montré dans la vue « Cette machine ». `aiodos.log` donne à chaque démarrage les
droits réels du dossier et de la base. Ce qu'AIODOS écrit pour l'utilisateur hors de ses
données (« Enregistrer sous… », une galerie dans un dossier choisi) garde les droits
habituels de l'utilisateur.

Il n'y a pas de coffre hors de Windows : les clés des paires et du cloud restent en clair
dans la base, et seuls ces droits les ferment aux autres comptes. Pas à un programme lancé
sous le même compte. Les versions d'avant le 20/10/2026 laissaient la base lisible par
tous (0644), et fermer les droits après coup ne reprend pas un secret déjà lu. Sur une
machine partagée, après la mise à jour : refaire les appairages, et changer les clés du
cloud (Mistral, Qwen) et celle de la passerelle.

**Windows.** Rien ne change : les droits du profil (`%LOCALAPPDATA%`) et DPAPI. Une copie
lancée depuis les sources hors du profil garde les droits du dossier où elle est posée :
c'était le cas du dépôt de l'auteur, corrigé le 21/10 (§ 8).

## 4. Les bornes

| Domaine | Quoi | Valeur | Où |
|---|---|---|---|
| Serveur entre machines | en-têtes d'une requête | 10 s | `network/connections.py` `HEADER_S` |
| | débit minimal d'un corps (sauf depuis cette machine) | 8 Kio/s en moyenne, après 10 s | `MIN_RATE`, `BODY_GRACE` |
| | connexions d'une adresse inconnue | 32 | `PER_ADDRESS` |
| | connexions de toutes les adresses inconnues | 256 | `MAX_STRANGERS` |
| | connexions d'une adresse connue (mes machines, amis) | 128 | `PER_KNOWN` |
| | connexions depuis cette machine | 512 | `MAX_LOOPBACK` |
| | `hello` | 4 Kio | `network/body_limit.py` `HELLO_BODY` |
| | début d'appairage, inconnu, chemin non reconnu | 64 Kio | `OPEN_BODY` |
| | message scellé | 16 Mio | `SEALED_BODY` |
| | `work/result` | 1 Gio au plus (image ≈ 58 Mio, vidéo ≈ 911 Mio, index d'un ami 16 Mio) | `HUGE_BODY`, `network/work.result_limit`, `MINE_RESULT` |
| | gros corps lus à la fois (> 64 Kio) | 2 par machine, 8 en tout | `BIG_PER_SENDER`, `BIG_TOTAL` |
| | très gros corps (> 16 Mio) | 1 par machine, 2 en tout | `HUGE_PER_SENDER`, `HUGE_TOTAL` |
| | attente d'un tour | 45 s, puis 503 | `SLOT_WAIT` |
| Réponses reçues | `update/chunk` | ≈ 1,8 Mio | `network/client.py` `REPLY_LIMITS` |
| | `update/manifest` | ≈ 1,4 Mio | `REPLY_LIMITS` |
| | `update/pending` | ≈ 5,5 Mio | `REPLY_LIMITS` |
| | `images/file` | ≈ 14,3 Mio | `REPLY_LIMITS` |
| | `work/blob` | ≈ 3,6 Mio | `REPLY_LIMITS` |
| | `work/lease`, `profile` | 4 Mio | `REPLY_LIMITS` |
| | autres actions | 1 Mio | `DEFAULT_REPLY` |
| | `hello`, début d'appairage, refus | 64 Kio (refus montré en 500 caractères) | `PUBLIC_REPLY`, `ERROR_REPLY`, `DETAIL` |
| | JSON analysé sans sa forme stricte | 64 Kio | `SMALL_JSON` |
| | morceau d'une réponse en flux | 1 Mio | `MAX_FRAME` |
| | échéance d'un appel | connexion + envoi à 8 Kio/s + 4 délais de lecture | `DEADLINE_FACTOR`, `SEND_RATE` |
| | conversations à la fois vers une machine | 16, attente de 540 s au plus | `STREAMS_PER_PEER`, `STREAM_WAIT` |
| Travail confié | unités détenues par un ami | 4 | `network/work.py` `FRIEND_MAX_HELD` |
| | demandes comptées d'un ami | 60 par minute | `FRIEND_LEASES_PER_MINUTE` |
| | durée chez un ami | 1 h ; 30 min pour une image, 2 h pour une vidéo | `FRIEND_LEASE_MAX`, `_IMAGE`, `_VIDEO` |
| | échecs à la suite avant mise de côté | 10 | `FRIEND_FAILURES_ASIDE` |
| | priorité aux autres amis après un échec | 5 min | `FRIEND_RETRY_OTHERS` |
| | essais d'une unité | 2 | `jobs/models.py` `MAX_ATTEMPTS` |
| | fichier confié | 512 Mio, en morceaux de 2 Mio (512 Kio par Internet) | `MAX_BLOB`, `BLOB_CHUNK`, `INTERNET_CHUNK` |
| | vecteurs recalculés, cosinus minimal | 3, 0,999 | `jobs/kinds/index.py` `SAMPLE_COUNT`, `SAMPLE_MIN` |
| | longueur d'un passage reçu | 1 800 caractères | `MAX_PIECE` |
| Calcul pour les autres | place laissée libre sur le disque | 1 Gio | `network/worker.py` `DISK_MARGIN` |
| | réponse à `work/lease` ; aux autres `work/*` | 4 Mio ; 256 Kio | `LEASE_REPLY`, `SMALL_REPLY` |
| | repos après une unité refusée ou faute de place | 10 min | `REFUSED_RETRY`, `NO_ROOM_RETRY` |
| | repos après un modèle Whisper absent | 1 h | `WHISPER_RETRY` |
| | propriétaire trop lent | redemandé après 5 min | `SLOW_RETRY` |
| | résultat non remis | nouveaux essais après 3, 10 et 20 s | `RESULT_RETRY_DELAYS` |
| Appairage | demande en attente | 3 min | `network/pairing.py` `PENDING_TTL` |
| | demandes reçues en attente | 3 | `MAX_PENDING` |
| | demandes reçues acceptées | 10 par 3 min | `MAX_STARTS` |
| | nonce ; engagement | 32 octets ; SHA-256 | `network/crypto.py` `PAIR_NONCE`, `COMMIT` |
| Balises | vérification d'une adresse annoncée | une fois par 30 s, 4 à la fois, 8 s en tout, réponse de 64 Kio | `network/beacons.py` `CHECK_EVERY`, `MAX_RUNNING`, `DEADLINE`, `MAX_REPLY` |
| | machines inconnues montrées | 64 | `network/manager.py` `MAX_VISIBLE` |
| Profil | nom d'une machine ; modèles | 60 caractères ; 300 | `network/profile.py` `NAME`, `MAX_MODELS` |
| Pages web | corps d'une page | 5 Mio | `services/web.py` `MAX_BYTES` |
| | durée totale ; connexion ; lecture | 30 s ; 10 s ; 15 s | `TOTAL_TIMEOUT`, `CONNECT_TIMEOUT`, `READ_TIMEOUT` |
| | redirections | 5 | `MAX_REDIRECTS` |
| | recherche web | 2 Mio, 20 s | `SEARCH_MAX_BYTES`, `SEARCH_TIMEOUT` |
| Documents | fichier lu | 100 Mio | `services/extract.py` `MAX_TEXT_BYTES` |
| | archive zip | 10 000 entrées, répertoire central de 4 Mio, 200 Mio décompressés | `services/archives.py` `MAX_ENTRIES`, `MAX_DIRECTORY`, `MAX_UNZIPPED` |
| | parties XML | 16 Mio chacune, 40 Mio en tout | `MAX_XML_PART`, `MAX_XML_TOTAL` |
| | feuille d'un classeur | 1 200 000 éléments hors des lignes, 200 000 par ligne, ligne 1 048 576 au plus | `MAX_SHEET_OUTSIDE`, `MAX_ROW_ELEMENTS`, `EXCEL_ROWS` |
| | HTML et RTF ; profondeur d'un RTF | 16 Mio ; 1 000 niveaux | `extract.MAX_MARKUP_BYTES`, `MAX_RTF_DEPTH` |
| Compagnon | en-têtes de l'API locale | 10 s | `compagnon/src/control.rs` `HEADER_TIMEOUT` |
| | connexions sans jeton | 512 | `admission.rs` `MAX_WAITING` |
| | connexions QUIC par machine ; flux par connexion | 4 ; 64 | `pool.rs` `MAX_CONNECTIONS_PER_PEER`, `endpoint.rs` `MAX_STREAMS` |
| | requêtes relayées à la fois par machine | 32 conversations et 32 autres, attente de 30 s puis 503 | `pool.rs` `MAX_STREAMS_PER_PEER`, `MAX_REQUESTS_PER_PEER`, `SLOT_WAIT` |
| | corps relayé | 64 Mio (1 Gio pour `work/result`) | `relay_body.rs` `MAX_BODY`, `MAX_RESULT_BODY` |
| | silence ; débit minimal d'un corps | 60 s ; 16 Kio/s après 120 s (408) | `BODY_IDLE`, `PACE` |
| | réponse non lue | 60 s, puis flux coupé | `RESPONSE_STALL` |
| | connexions d'un tunnel ; en attente de contrôle | 8 (AIODOS en demande 4) ; 512 | `tunnel.rs` `MAX_CONNECTIONS`, `MAX_CHECKING` |
| | attente du programme nommé ; flux par service | 30 s ; 8 | `OWNER_WAIT`, `MAX_SERVICE_STREAMS` |
| Moteurs | journal d'un programme | 4 Mio, puis `.1` ; 4 anciens par rôle | `cluster/engine.py` `ENGINE_LOG_BYTES`, `KEEP_LOGS` |
| | cache des poids prêtés | 48 Gio, ramené toutes les 60 s | `cluster/helper.py` `CACHE_MAX_BYTES`, `CACHE_TRIM_S` |
| | connexion du tunnel tombée ; sans nouvelles | 2 s (modèle chargé) ou 30 s ; 90 s | `DROP_LOADED_S`, `DROP_S`, `LEASE_S` |
| | connexions d'un tunnel d'emprunt | 4 | `cluster/group.py` `TUNNEL_CONNECTIONS` |
| | ports de sd-server | 15 ports refusés par les navigateurs | `imaging/engine.py` `BROWSER_BLOCKED_PORTS` |
| Mises à jour | appel ; lecture | 150 s ; 60 s | `updates/p2p.py` `CALL_LIMIT`, `TIMEOUT` |
| | débit minimal d'un fichier | 32 Kio/s, plus 120 s | `MIN_RATE`, `FILE_MARGIN` |
| | machine qui n'a pas fourni ; machines par tour | 10 min de côté ; 3 | `updates/service.py` `PEER_RETRY`, `MAX_SOURCES` |
| | débuts d'un fichier gardés | 3 (un par machine) | `updates/store.py` `KEEP_PARTIALS` |
| | texte d'un manifeste ; approbations gardées ; examinées | 256 Ki caractères ; 8 ; 32 | `updates/manifest.py` `MAX_TEXT`, `MAX_APPROVALS`, `MAX_SCAN` |
| | `manifest.json` gardé | 16 Mio (au-delà, retiré) | `updates/store.py` `MAX_KEPT` |
| Journaux | `aiodos.log` | 10 Mio, plus 3 sauvegardes | `__main__.py` `LOG_BYTES`, `LOG_BACKUPS` |
| | `erreurs.log` | 2 Mio, plus `.1` | `ERRORS_BYTES` |
| | même alerte | 10 fois par minute | `_Repeats.KEPT` |
| Images | durée de calcul gardée | 0 à 7 jours | `imaging/gallery.py` `MAX_SECONDS` |

## 5. Compatibilité et mise à jour

**L'appairage par code passe au protocole 2.** Une machine à jour et une machine d'une
version d'avant (2.0.1 et avant) ne peuvent plus s'appairer par code, dans un sens comme
dans l'autre : chacune reçoit un refus qui dit de mettre AIODOS à jour. **Mettre à jour
toutes les machines, téléphone compris, avant d'en appairer de nouvelles.** Le téléphone se
met à jour par une nouvelle appli (`python tools/build_android.py --install`), pas par le
réseau.

**Les paires existantes continuent de se parler** : mêmes clés, même enveloppe
(`aiodos/1`), même contrat de calcul (`WORK_API` reste à 1 ; la révision du contrat est
annoncée à part, `work_revision` = 2). La présentation par groupe marche encore avec une
version d'avant.

Ce qu'une machine d'une version d'avant ne peut plus faire avec une machine à jour :

| Elle… | Désormais |
|---|---|
| s'appaire par code | non, dans les deux sens (« mets AIODOS à jour ») |
| change d'adresse | elle n'est plus suivie sur balise ; seuls ses propres messages scellés font connaître sa nouvelle adresse, pas un changement de port |
| reçoit des pages web à lire (une de mes machines) | non : refus qui dit de la mettre à jour ; les pages se lisent sur la machine qui les a demandées |
| emprunte une carte à une machine à jour | non : « mets AIODOS à jour » |
| prête sa carte | oui, mais sans la tenue du compagnon : son serveur RPC reste ouvert aux autres programmes de sa machine |
| envoie un nombre non fini (NaN) | son message est refusé |
| (un ami) envoie un index de plus de 16 Mio | refusé ; l'unité revient chez son propriétaire quand le bail expire (90 s) |
| (un ami) confie encore des pages, des images, des sons ou des vidéos à indexer | rendus sans être calculés ; cette machine est laissée 10 min au repos, sans va-et-vient |

**Le compagnon publié doit être celui de ce lot.** AIODOS lit dans la ligne « ready » ce
que son compagnon sait faire. Sans `tunnel-owner`, il n'emprunte pas de carte ; sans
`service-usage` et `service-hold`, il n'en prête pas. `tools/publish.py release` construit
le compagnon à partir du commit publié : il suffit de publier après avoir tout commité.
Dans une copie de développement, AIODOS lance `compagnon/target/release/aiodos-net.exe` :
le reconstruire (`cargo build --release`) après chaque changement de `compagnon/`.

**Deux cas particuliers.**
- Un nœud Termux ou Linux installé depuis une archive 2.0.0 ou 2.0.1 refuse les amis, même
  mis à jour, tant qu'il n'est pas réinstallé depuis une version récente (§ 3.7).
- Une copie de développement « 2.0.2-dev » construite entre le commit 804bd7c et ce lot
  connaît les amis, mais pas la marque `k2:`. Si elle reprenait la main, ses paires d'amis
  échoueraient (clé de 35 octets). Aucune version publiée n'est concernée ; au besoin,
  refaire l'appairage.

**Dépendance.** urllib3 2.3 ou plus récent (lecture bornée et coupure à l'échéance).

## 6. Ce qui reste ouvert

**Appairage**
- L'API locale accepte encore une réponse sans `ref` : l'interface l'envoie toujours,
  mais le lien entre la réponse et la demande montrée ne tient que si le client le donne.
- Le serveur entre machines ne vérifie pas l'en-tête `Host`. Une page web, par *DNS
  rebinding*, peut atteindre `/p2p/v1/pair/start` ; elle n'obtient que du bruit
  (engagements jamais révélés qui occupent les places), pas un appairage.
- Un appareil du réseau local peut envoyer à une machine un engagement « au nom » d'une
  autre, sans jamais le révéler, et le renouveler toutes les 3 minutes : l'appairage entre
  les deux est alors bloqué, avec un message qui dit à tort « termine-le ».
- Deux balises qui annoncent le même identifiant depuis deux adresses ne sont pas
  signalées.

**Réseau et amis**
- Le compagnon ne connaît pas le niveau de confiance des machines : il n'y a pas de places
  réservées à mes machines. Un ami peut occuper les siennes (32 et 32), mais le débit
  minimal et la coupure des réponses non lues le lui font payer en bande passante.
- Un ami lent peut encore retenir la boucle du prêt de puissance environ 150 s, une fois
  toutes les 5 min. Mes machines passent avant lui.
- Sous Windows, couper la connexion ne réveille pas une lecture sur une connexion muette :
  l'appel abandonné finit au prochain octet ou à son délai de lecture. D'ici là, plus rien
  n'est demandé à cette machine.
- La taille d'une conversation prêtée n'est bornée que par le bouton Stop.
- Une machine dont les nouvelles échouent (un NaN d'une version d'avant…) passe pour hors
  ligne, sans que la raison soit dite.
- La route qui change le niveau de confiance (`/api/network/peers/<id>/trust`) ne refuse
  pas le passage en « ami » sur un nœud d'origine 2.0.x. La clé est quand même marquée.

**Moteurs et compagnon**
- Prêt de carte : il reste l'instant entre le démarrage du serveur RPC et la première
  connexion du compagnon (quelques centaines de ms). Un intrus encore connecté ensuite est
  vu, et le prêt arrêté ; un intrus qui frappe et repart aussitôt ne l'est pas. Restent
  aussi les failles du serveur RPC face au llama-server d'une de mes machines, et
  ggml-rpc-server ne tourne pas avec des droits réduits. C'est pourquoi le prêt reste
  réservé à mes machines.
- Sous macOS (et Android récent), le compagnon ne lit pas la table des connexions : ni
  tunnel gardé, ni service tenu, donc pas de modèle réparti.
- sd-server n'a pas de clé : pendant qu'il tourne, un autre programme de la machine peut
  lui parler, lire ses tâches et leurs images, ou le faire tomber. Il reste aussi un court
  instant entre la vérification du port et l'envoi.
- Une copie de développement lance encore `compagnon/target/…` et un dossier de moteur
  posé à la main sans vérifier qui peut y écrire, et un moteur déplié n'est pas revérifié
  par son empreinte avant chaque lancement. Seuls les droits du dossier protègent.
- Un dossier de modèles d'images commun (réglage `images_models_dir`) n'est signalé qu'au
  journal.
- Sous Linux et macOS, `updates/private.py` ne lit que les bits de mode, pas les listes de
  droits (ACL). Seules les copies de modèles d'images en tiennent compte.

**Contenus**
- Un intranet au nom DNS public n'est pas lu comme page locale. Il faudrait un réglage de
  suffixes de domaines internes.
- Ne sont plus lus : un HTML ou un RTF de plus de 16 Mio (images incluses), un RTF imbriqué
  sur plus de 1 000 niveaux, une archive de plus de 200 Mio décompressés. La raison
  s'affiche.
- Le contrôle de forme d'une grande feuille confiée par un ami peut prendre une à deux
  minutes.
- `requirements.txt` et `requirements-node.txt` (Linux, Termux) laissent la version de
  requests libre. Avec une autre version, la garde refuse de lire les pages si les
  fonctions internes d'urllib3 ont changé : c'est sûr, mais la lecture ne marche plus.

**Interface**
- Sous Linux et macOS, les gardes natives de la fenêtre (navigations, requêtes) n'existent
  pas encore : elles sont celles de WebView2.
- L'appli Android ouvre aussi les liens mailto (nettoyés), comme la fenêtre Windows. C'est
  un choix à confirmer.

**Mises à jour**
- Aucun appareil d'approbation n'est déclaré (`APPROVER_KEYS` est vide) : une clé de
  publication volée suffit encore à pousser une version (§ 8).
- Pour un manifeste publié avant ce lot (sans `approvers`), 8 identifiants bidon placés
  devant peuvent écarter la vraie approbation. Cela revient à un relais qui la retire, ce
  que tout relais peut faire.
- Une future version d'origine devrait refuser le réseau quand la base est plus récente
  que son code (`PRAGMA user_version` plus grand que `SCHEMA_VERSION`).
- `tools/android_wheels.py`, lancé seul, ne contrôle ni le dépôt ni ses outils (il l'est
  quand `build_android.py` le lance).
- La note de version 2.0.2 reste à écrire : elle doit dire le protocole 2 de l'appairage.

**Stockage**
- Les deux essais des vrais droits Unix (`tests/test_securite_stockage.py`) n'ont pas
  encore tourné sur un vrai Linux ou macOS : ils sont sautés sous Windows. En attendant,
  `aiodos.log` écrit les droits réels à chaque démarrage.

**Robustesse** (sans effet sur la sécurité)
- La mise à jour de la mémoire des conversations et la réindexation d'une entrée vont au
  bout après une annulation.
- Un travail supprimé pendant qu'une unité finit ici fait échouer son enregistrement (une
  fenêtre de 2 s).

## 7. La passerelle « Autres outils »

La passerelle (`aiodos/gateway/`) n'a pas été auditée : elle était hors du périmètre des
auditeurs. Un auditeur y a trouvé en passant la faille n° 3 : la configuration donnée à
copier dans les assistants lance `python -m aiodos.gateway.mcp_stdio`, et `-m` place le
dossier courant du client MCP en tête de `sys.path` (un `requests.py` du projet ouvert
serait importé). La correction proposée : `-P` dans les arguments et `PYTHONSAFEPATH=1`
dans l'environnement, avec un essai. Elle n'est pas faite ici, parce que la passerelle est
réécrite sur une autre branche (`claude/distracted-satoshi-342231` : « Passerelle : servie
par le moteur intégré, sans Ollama »). Une fois cette branche fusionnée, il faudra corriger
le n° 3 et faire auditer la passerelle.

## 8. Ce que l'auteur a fait, ou peut faire

- **Fait le 21/10/2026 : droits du dossier `C:\_22eme ARKANE`.** Il héritait de la racine
  de `C:` « Utilisateurs authentifiés : modification » : tout autre compte du PC pouvait
  modifier le dépôt de publication (n° 10). L'héritage est retiré ; seuls l'auteur, le
  Système et les Administrateurs y ont des droits (vérifié avec `icacls` le 21/10). La
  commande, pour un autre poste (invite de commandes) :
  ```
  icacls "C:\_22eme ARKANE" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"
  ```
- **Recommandé : déclarer un appareil d'approbation des versions**, et un second de
  secours ([mises-a-jour.md](mises-a-jour.md), « Approbation sur un deuxième appareil »).
  Tant qu'aucun n'est déclaré, une clé de publication volée suffit à pousser une version
  chez les amis. Ensuite, comparer l'empreinte qu'affiche `publish.py` avec celle de
  l'appareil avant d'approuver.
- **À faire pour construire l'appli Android** : le 21/10, `C:\dev\android-sdk` est
  modifiable par « Utilisateurs authentifiés », et `build_android.py` refuse de construire
  tant qu'il l'est. La même commande `icacls`, appliquée à ce dossier, ou un SDK installé
  dans `%LOCALAPPDATA%\Android\Sdk`.
- **Conseillé** : créer une fois un dossier `C:\.cargo` vide et privé (`mkdir C:\.cargo`,
  puis la même commande). Sinon, chaque construction du compagnon avertit qu'un autre
  compte pourrait y poser une configuration de cargo.
- **Avant de publier** : commiter tout le lot (les outils refusent les fichiers non
  suivis), publier la 2.0.2 avec sa note de version, reconstruire et installer l'appli du
  téléphone, puis mettre à jour toutes les machines avant tout nouvel appairage.
- **Sur une machine Linux ou macOS partagée** qui a fait tourner une version d'avant :
  refaire les appairages, et changer les clés du cloud et de la passerelle (§ 3.8).
- **À trancher** : les liens mailto dans l'appli Android (§ 6) ; le prêt de carte, activé
  par défaut pour mes machines (§ 3.6).
- **À lancer une fois** sur un vrai Linux ou macOS, dans `/tmp` et pas dans `/mnt/c` :
  `python3 -m pytest tests/test_securite_stockage.py -k "unix or deleted_while_open"`.

## 9. Essais

- **Python** : 13 fichiers `tests/test_securite_*.py`, 542 essais (amis, appairage, arrêts,
  client, contenus, divers, interface, mises à jour, moteurs, réseau, stockage, travail,
  tunnels). Certains lancent de vrais compagnons, de vrais nœuds, ou un vrai serveur local
  qui joue la machine d'en face. Suite complète du 21/10 : 1 349 essais réussis, 20 sautés,
  aucun échec.
- **Compagnon** (`cargo test`) : 40 essais unitaires, et 14 dans `tests/bornes.rs` (tunnel
  gardé sous une avalanche de connexions, service tenu, corps et places bornés, réponses
  non lues, API locale), plus `tests/tunnel.rs` (8), `services.rs` (3), `relay_only.rs` (3)
  et `public.rs` (2).
- Chaque nouvel essai échoue sans sa correction : c'est vérifié sur des copies remises
  dans l'état d'avant, ou par des greffons pytest qui rétablissent l'ancien comportement.
