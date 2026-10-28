# aiodos-net — le compagnon réseau d'AIODOS

Petit programme Rust (Apache-2.0) qui relie **par Internet** les machines appairées
d'AIODOS, avec [Iroh](https://www.iroh.computer) 1.3.0 (version épinglée). C'est
l'étape 2.2 de [docs/phase2-internet.md](../docs/phase2-internet.md). Le nœud Python le
lance, le surveille et lui confie ses requêtes quand le réseau local ne répond pas ;
il n'est **jamais obligatoire** : sans lui, AIODOS fonctionne comme avant, en local.

```
 Machine A                                          Machine B
 nœud Python                                        nœud Python
   client.call ──► 127.0.0.1:<port>/peer/<clé B>/p2p/v1/…    p2p_app (127.0.0.1:47701)
                        │                                          ▲
                        ▼                                          │  + X-Aiodos-Peer: <clé A>
 aiodos-net (Iroh) ══ une connexion QUIC « aiodos/2 » ══► aiodos-net (Iroh)
   liste blanche       en direct, ou par un relais       liste blanche
                       (qui ne voit que du chiffré)
```

- **Identité** : une clé Ed25519 dans `<dossier de données>/aiodos-net.key`, créée au
  premier lancement. Sa moitié publique (64 caractères hexadécimaux) est l'adresse de
  la machine : on compose une clé, pas une adresse IP. La clé secrète n'apparaît
  jamais dans le journal ni dans l'API.
- **Liste blanche** : seules les clés des machines appairées entrent ; une autre clé est
  refusée dès la fin de la poignée de main TLS, et le compagnon ne compose pas de clé
  inconnue. AIODOS la tient à jour (les machines appairées s'échangent leurs clés par un
  message scellé, l'action `net/info`).
- **Entrant** : chaque flux d'une machine autorisée porte un échange HTTP/1.1 de l'API
  `/p2p/…`, relayé vers le serveur réseau local d'AIODOS avec la clé authentifiée de
  l'appelant (`X-Aiodos-Peer`) ; AIODOS vérifie que c'est bien celle de la machine qui a
  scellé l'enveloppe. Rien d'autre que `/p2p/…` ne passe.
- **Sortant** : un mandataire HTTP local. `POST /peer/<clé>/p2p/v1/work/lease` part vers
  la machine `<clé>`. **Une seule connexion QUIC par machine**, réutilisée par toutes les
  requêtes (un flux chacune) : c'est l'enseignement de l'étape 2.0, où dumbpipe ouvrait
  une connexion par requête (5 à 10 s chacune en 4G). La connexion ouverte par l'autre
  machine sert aussi dans l'autre sens : un téléphone injoignable en 4G ouvre la
  connexion, et le PC s'en sert pour ses propres requêtes. Les réponses en flux
  (`/p2p/v1/stream/…`, conversations) passent morceau par morceau, et un client qui s'en
  va (Stop) coupe le calcul de l'autre côté.
- **Relais et annuaires** (décisions de l'étape 2.1) : relais publics de n0 par défaut,
  remplaçables ; annuaire DNS de n0 et DHT Mainline (pkarr) en secours, interrogés
  ensemble. Seule l'adresse du relais de la machine y est publiée, pas ses adresses IP.
  Ouverture de port automatique sur la box (UPnP, NAT-PMP, PCP) si Iroh y parvient.
- **Robustesse** : changement de réseau (Wi-Fi ↔ 4G) détecté toutes les 5 s, y compris
  sous Android où Iroh n'en est pas prévenu ; une connexion qui meurt est remplacée à la
  requête suivante ; silence toléré 60 s sur une connexion (liens mobiles) ; arrêt
  propre (les autres machines sont prévenues), y compris quand AIODOS est tué : le
  compagnon s'arrête quand son entrée standard se ferme.

## Construire

### Windows x64

Il faut Rust 1.91 ou plus récent (cible MSVC) et les outils C++ de Visual Studio
(« Développement Desktop en C++ », pour la bibliothèque cryptographique *ring*).

```powershell
cd compagnon
cargo build --release
```

Le programme est `compagnon\target\release\aiodos-net.exe` (environ 17 Mo) : AIODOS le
trouve là tout seul. Lancer `cargo` depuis PowerShell ou l'invite de commandes : sous Git
Bash, le `link.exe` de Git a déjà masqué celui de Visual Studio (le 16/10/2026, en
revanche, la construction depuis Git Bash a réussi).

### Programme livré avec les versions

**On ne le copie plus à la main** : `tools/publish.py release` construit le compagnon de
chaque plateforme à partir du commit publié et le joint à la version. Chaque machine
reçoit celui de sa plateforme (cf.
[docs/mises-a-jour.md](../docs/mises-a-jour.md#le-compagnon-internet-livré-avec-les-versions)).
L'outil utilise le profil `distribution` de `Cargo.toml` :

- optimisé pour la taille (`opt-level = "s"`, `lto = "fat"`, `codegen-units = 1`) ;
- toujours sans `panic = "abort"` (cf. Android plus bas) ;
- 10,6 Mo sous Windows au lieu de 17,5, et 9,3 Mo pour Android, mesurés le 16/10/2026 ;
- environ 100 s et 150 s de construction une fois les dépendances compilées.

Le commit d'AIODOS, donné à la construction par la variable `AIODOS_NET_COMMIT`, est
écrit dans le programme. L'outil de publication s'en sert pour vérifier qu'un binaire
correspond bien à la version publiée. La ligne « ready » le donne (`"commit"`).
Construction à la main du même programme :

```powershell
$env:AIODOS_NET_COMMIT = (git rev-parse HEAD)
cargo build --profile distribution --target x86_64-pc-windows-msvc --locked
```

### Android arm64, sous Termux

Rust est empaqueté dans Termux ; la compilation se fait sur le téléphone lui-même.

```sh
pkg install rust resolv-conf
cd ~/aiodos/compagnon            # le dossier compagnon/ du dépôt, copié sur le téléphone
cargo build --release
cp target/release/aiodos-net $PREFIX/bin/
```

- **Résolution DNS** : sous Android, Iroh lit les serveurs DNS par l'environnement Java
  (JNI), absent pour un programme lancé dans Termux. La recette Termux de dumbpipe
  contourne le problème en modifiant les bibliothèques (`rustls-platform-verifier`,
  `hickory-resolver`, `netdev`) pour qu'elles se comportent comme sous Linux et lisent
  `$PREFIX/etc/resolv.conf`. Le compagnon n'a besoin d'aucune modification : sous
  Android, il donne lui-même à Iroh les serveurs de `$PREFIX/etc/resolv.conf` (paquet
  `resolv-conf`), avec des résolveurs publics en secours (`src/dns.rs`). Les certificats
  des relais sont vérifiés avec les racines de Mozilla intégrées au programme, sans JNI.
- **Ne pas compiler avec `panic = "abort"`** (ni `CARGO_PROFILE_RELEASE_PANIC=abort`) :
  sous Android, des bibliothèques réseau interceptent une panique quand l'environnement
  Java manque ; la recette de dumbpipe force elle aussi `unwind`. Le profil `release` du
  projet le respecte.
- Comme pour le nœud, activer l'option développeur « Désactiver les limites pour les
  processus enfants » (cf. [docs/android.md](../docs/android.md)).
- L'ouverture de port automatique ne marche plus depuis Android 12, et un téléphone en
  4G/5G reste injoignable de l'extérieur : c'est lui qui ouvre la connexion, puis le PC
  s'en sert (cf. plus haut).

**Ou compiler depuis le PC** avec le NDK Android, puis copier le binaire dans Termux.
L'outil de publication le fait de lui-même (NDK trouvé par `ANDROID_NDK_HOME`,
`ANDROID_NDK_ROOT` ou `ANDROID_HOME/ndk/<version>`). Compilation vérifiée le
16/10/2026 avec le NDK 28.2 : exécutable ARM64 pour Android 7 et plus, 17 Mo en
`release`, 9,3 Mo en `distribution` ; pas encore lancé sur un téléphone. À la main :

```powershell
rustup target add aarch64-linux-android
$ndk = "C:\dev\android-sdk\ndk\28.2.13676358\toolchains\llvm\prebuilt\windows-x86_64\bin"
$env:CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER = "$ndk\aarch64-linux-android24-clang.cmd"
$env:CC_aarch64_linux_android = "$ndk\aarch64-linux-android24-clang.cmd"
$env:AR_aarch64_linux_android = "$ndk\llvm-ar.exe"
cargo build --release --target aarch64-linux-android
adb push target\aarch64-linux-android\release\aiodos-net /data/local/tmp/
# puis, dans Termux : cp /data/local/tmp/aiodos-net $PREFIX/bin/ && chmod +x $PREFIX/bin/aiodos-net
```

## Lancer

**Normalement, AIODOS s'en charge** : réglage `internet_enabled` (route
`POST /api/network/internet`), ou option `python -m aiodos --internet`. AIODOS choisit
le programme dans cet ordre (`aiodos/network/companion.py`) :

1. `AIODOS_NET_BIN`, s'il est donné (et alors rien d'autre) ;
2. dans une copie de développement, `compagnon/target/release/` (ou `debug/`) ;
3. celui livré avec la version qui tourne, à défaut avec une version plus ancienne,
   copié dans `data/updates/bin/` ;
4. celui compilé dans le dossier de la version, puis dans celui de la version d'origine ;
5. le `PATH`.

Un programme qui ne démarre pas, ou qui ne peut pas être mis en place, laisse place au
suivant ; quand tous ont échoué, la liste reprend du début. L'état du réseau
(`internet.program`) et le journal disent lequel tourne. Le
réglage `p2p_route` choisit le chemin vers les machines appairées : `auto` (réseau local
s'il répond, Internet sinon), `lan` ou `internet` (pour essayer le compagnon seul).
`--internet-local` lance un compagnon d'essai sans relais, sans annuaire et sans box
(deux nœuds sur une même machine, comme dans les tests). `--internet-relay-only <URL>`
lance un compagnon d'essai qui ne passe que par ce relais, jamais en direct (cf.
[docs/relais.md](../docs/relais.md#essai-sur-une-seule-machine)).

À la main :

```powershell
$env:AIODOS_NET_TOKEN = "un-jeton-au-hasard"
aiodos-net --data-dir ..\data --listen 127.0.0.1:47800 --forward 127.0.0.1:47701
```

| Option | Défaut | Rôle |
|---|---|---|
| `--data-dir` | (obligatoire) | dossier de données d'AIODOS : la clé y est gardée |
| `--listen` | `127.0.0.1:47800` | API locale et mandataire, sur la boucle locale seulement (`:0` : port libre) |
| `--forward` | `127.0.0.1:47701` | serveur réseau local d'AIODOS (`p2p_app`) |
| `--relay` | `n0` | `n0` (relais publics), `none`, ou `https://…` (répétable ; `n0` + adresses : les deux) |
| `--directory` | `n0-dns,dht` | annuaires : `n0-dns`, `dht`, ou `none` |
| `--bind` | toutes les interfaces | sockets UDP d'Iroh (`127.0.0.1:0` pour un essai local) |
| `--no-portmap` | — | pas d'ouverture de port automatique sur la box |
| `--relay-only` | — | essais : relais seulement, jamais en direct (aucune socket UDP, ni ouverture de port) ; demande un relais et ne se combine pas avec `--bind` |
| `--dial-timeout` | `20` | délai pour joindre une machine (secondes ; AIODOS attend 30 s au moins une réponse par Internet) |
| `--supervised` | — | lancé par AIODOS : s'arrête quand son entrée standard se ferme |

Le jeton de l'API locale vient de la variable `AIODOS_NET_TOKEN` (jamais de la ligne de
commande, visible des autres programmes) ; sans elle, il est tiré au hasard et écrit
dans `<data-dir>/aiodos-net.token`. Au démarrage, une ligne JSON sur la sortie standard
annonce que le compagnon est prêt (`{"event": "ready", "id": …, "listen": …, "version":
…, "commit": …, "features": […]}`, le commit étant `null` hors d'une construction de
publication, et `features` ce que ce compagnon sait faire, cf. « Capacités ») ; le
journal va sur la sortie d'erreur (niveau réglable par `AIODOS_NET_LOG`, par exemple
`aiodos_net=debug,iroh=info`). Windows peut demander l'autorisation du pare-feu au
premier lancement : l'accepter pour les réseaux privés suffit en local, mais une
connexion directe par Internet demande aussi les réseaux publics (Iroh sait alors passer
par un relais).

## API locale

Toutes les requêtes portent l'en-tête `X-Aiodos-Net-Token`.

| Requête | Effet |
|---|---|
| `GET /net/v1/status` | clé publique, capacités (`features`), relais (connectés ou non, refus du relais : `denied`), relais seulement ou non, adresses, liste blanche, connexions refusées, changements de réseau, et pour chaque machine : connectée, chemin `direct` ou `relay`, adresse distante, latence (`rtt_ms`), sens de la connexion, compteurs ; services exposés et tunnels ouverts (`tunnels`, cf. plus bas) |
| `PUT /net/v1/peers` | remplace la liste blanche : `{"peers": [{"id": "<clé>", "addrs": ["ip:port"], "relays": ["https://…"]}, {"ticket": "endpoint…"}]}` ; les connexions des clés retirées sont fermées |
| `GET /net/v1/ticket` | ticket Iroh à donner à une autre machine (clé, relais, adresses) ; il ne contient aucun secret et ne fait pas entrer dans la liste blanche à lui seul. Juste après le démarrage, il attend que le relais soit joint (4 s au plus après le démarrage) |
| `POST /net/v1/shutdown` | arrêt propre |
| `GET /net/v1/public` | réseau public (phase 3.0) : `enabled`, `key` (la clé publique du nœud, distincte de celle de la machine), `neighbors`, `nodes` (les nœuds publics vus depuis moins de 5 minutes : clé, version, modèles, prête ou non, `free` et `tps` ou `null`, `seen_s`), `served` (conversations servies aux inconnus : `in_flight`, `relayed`, `refused`), `rendezvous` (dernière inscription dans la DHT, clés trouvées, erreur) |
| `PUT /net/v1/public` | rejoint ou quitte le réseau public, avec le profil à diffuser : `{"enabled": true, "version": "…", "models": ["…"], "lends": false, "free": 1, "tps": 30}` (`free` : places de conversation libres, 0-255 ; `tps` : jetons par seconde, 0-65535 ; facultatifs, ramenés dans leurs bornes) ; désactivé tant qu'AIODOS ne le demande pas (docs/phase3-reseau-public.md). Le profil est diffusé chaque minute, et dans les 5 s quand il change (`public.rs` `CHANGE_GAP`). Tant qu'il tourne et que `lends` est vrai, le nœud public relaie à AIODOS les conversations des inconnus (`POST /public/v1/…`, en-tête `X-Aiodos-Public`, cf. plus bas) |
| `POST /net/v1/public/ask/<clé>/public/v1/…` | conversation confiée au nœud public `<clé>` (hexadécimal), avec l'en-tête `X-Aiodos-Conversation` (16 à 64 caractères `A-Za-z0-9_-`) : une clé jetable par conversation, en relais seulement, oubliée 15 min après sa dernière requête (32 au plus). Réponse du prêteur en flux. Refus du compagnon : `{"error", "detail"}` avec 400 `conversation` ou `bad_key`, 404 `not_found`, 405 `method`, 413 `too_large`, 502 `unreachable` ou `interrupted`, 504 `timeout` ; du prêteur : 503 `busy` ou `not_lending` (docs/conception/reseau-public-calcul.md) |
| `PUT /net/v1/exposed` | remplace la liste des services TCP locaux (127.0.0.1) ouverts à des machines nommées : `{"services": [{"name": "rpc-…", "port": 50123, "peers": ["<clé>"], "hold": true}]}`. 64 services au plus ; nom en minuscules, chiffres et tirets, 64 caractères au plus. `"hold": true` : le compagnon **tient** le service (cf. plus bas) ; refusé (400) si ce système ne dit pas quel programme se connecte. Tout est vérifié avant de changer quoi que ce soit. Réponse `{"ok": true, "exposed": <nombre>}` |
| `POST /net/v1/tunnels` | port local (127.0.0.1) relié au service `name` de la machine `peer` : `{"peer": "<clé>", "name": "rpc-…", "await_owner": true, "max_connections": 4}`, ou `"owner_pid": <numéro>` si le programme est déjà connu. Avec l'un ou l'autre, le tunnel est **gardé** (cf. plus bas). `max_connections` : de 1 à 8 (8 par défaut). Réponse `{"port", "guarded", "max_connections"}`. Refus : 400 (clé, nom, `max_connections` ou `owner_pid` invalide), 403 (machine absente de la liste blanche), 501 (tunnel gardé demandé là où le système ne dit pas quel programme se connecte) |
| `PUT /net/v1/tunnels/<port>` | `{"owner_pid": <numéro>}` : le seul programme dont ce tunnel gardé relaie les connexions, une fois pour toutes (le même numéro peut être redonné). 409 si le tunnel n'est pas gardé, ou s'il est déjà réservé à un autre programme ; 404 sans tunnel sur ce port |
| `DELETE /net/v1/tunnels/<port>` | ferme le tunnel : plus aucune connexion acceptée sur ce port, celles en cours coupées |
| `* /peer/<clé>/p2p/…` | requête relayée vers cette machine |

Sans le bon jeton, la réponse est un 401 et la connexion est fermée. Les en-têtes d'une
requête doivent arriver en 10 s, et 512 connexions au plus attendent de montrer le jeton :
au-delà, la plus ancienne est fermée. Un programme de la machine qui ouvrirait des milliers
de connexions muettes ne prive donc le compagnon ni de ses réponses à AIODOS, ni des
sockets d'Iroh.

Entre compagnons, le protocole `aiodos/2` (ALPN) est simple : un échange HTTP/1.1 par
flux QUIC bidirectionnel. Le contenu reste l'enveloppe scellée d'`aiodos/1`, que le
compagnon ne lit pas. Codes de fermeture d'une connexion : 0 arrêt, 1 doublon (ou
connexion de trop : la plus ancienne est fermée), 403 clé refusée ou retirée.

### Tunnels et services (gros modèles répartis)

Premier usage : le serveur RPC de llama.cpp, que la machine qui prête sa carte ouvre à la
machine qui emprunte ([docs/gros-modeles.md](../docs/gros-modeles.md)). Ce serveur n'a ni
mot de passe ni chiffrement : tout programme de la machine (un autre compte du PC) qui le
joindrait s'en servirait. D'où deux gardes, une de chaque côté (audit du 20/10/2026,
[docs/securite.md](../docs/securite.md)).

- **Tunnel gardé**, chez la machine qui emprunte. Ouvert avec `"await_owner": true`, il ne
  relaie que le programme qu'AIODOS nomme ensuite (`PUT /net/v1/tunnels/<port>`) : le
  llama-server qu'il vient de lancer. Pour chaque connexion, le compagnon cherche dans la
  table des connexions TCP du système quel programme est au bout (`src/owner.rs` :
  `GetExtendedTcpTable` sous Windows ; `/proc/net/tcp` puis `/proc/<pid>/fd` sous Linux),
  et ferme les autres. Une connexion arrivée avant que le programme soit nommé attend,
  30 s au plus. Les connexions en attente sont contrôlées ensemble, d'une seule lecture de
  la table : un intrus qui en ouvre des milliers ne fait pas écarter celle de
  llama-server. 512 attendent au plus ; au-delà, la plus ancienne en attente est écartée,
  jamais une dont le contrôle a commencé. L'état compte les refus (`refused`), et le
  journal dit le premier.
- **Service tenu**, chez la machine qui prête. Exposé avec `"hold": true`, le service est
  tenu par le compagnon (`src/hold.rs`). Le serveur RPC ne sert qu'un client à la fois,
  dans l'ordre d'arrivée : le compagnon y garde toujours deux connexions à lui, la servie
  et la suivante, et chaque flux du tunnel prend la plus ancienne libre. Avant de rendre
  une connexion, il vérifie dans la table qu'aucun autre programme n'est connecté au
  service, et qu'une des siennes passera ensuite. Sinon il la garde ouverte (le serveur
  reste occupé, l'intrus n'est jamais servi), refuse les flux suivants (503) et le dit
  dans l'état (`blocked`) ; AIODOS arrête alors le prêt. La table est aussi relue chaque
  seconde. Reste ouvert : l'instant entre le démarrage du serveur et la première connexion
  du compagnon.
- Un compagnon plus ancien ne répond pas `"guarded": true` : AIODOS referme alors le tunnel
  aussitôt, et refuse d'emprunter.

**Capacités** (`features`, dans la ligne « ready » et dans l'état) :

| Capacité | Sens | Annoncée |
|---|---|---|
| `service-usage` | l'état dit les flux en cours de chaque service exposé | toujours |
| `body-pace` | corps reçus d'une autre machine coupés sous un débit minimal, réponses jamais lues coupées | toujours |
| `tunnel-owner` | tunnels gardés | si le système dit quel programme se connecte : Windows, Linux (pas macOS, ni un Android qui ferme `/proc/net` aux applis) |
| `service-hold` | services tenus | comme `tunnel-owner` |
| `public-ask` | conversations du réseau public : servies aux inconnus (`aiodos/public/1`), demandées par une clé jetable (`/net/v1/public/ask/…`) ; `free` et `tps` dans le profil public | toujours |

AIODOS refuse d'emprunter une carte sans `tunnel-owner`, et d'en prêter une sans
`service-usage` et `service-hold` : il ne demande jamais à un compagnon plus ancien ce
qu'il ferait sans protection.

**État** (`GET /net/v1/status`, clé `tunnels`) :
- `exposed`, pour chaque service : `name`, `port`, `peers` (nombre de machines), `active`
  et `total` (flux en cours, et depuis l'ouverture), `idle_s` (secondes depuis la fin du
  dernier flux, quand il n'y en a plus), `held` ; pour un service tenu, `holding`
  (connexions du compagnon établies), `foreign` (connexions d'autres programmes vues) et
  `blocked` (pourquoi plus aucun flux ne passe) ;
- `tunnels`, pour chaque tunnel : `port`, `peer`, `name`, `connections`, `total`,
  `refused`, `max_connections`, `guarded`, `owner_pid`.

## Bornes et délais

Même une machine autorisée (un ami appairé) est bornée, et les autres programmes de la
machine ne gardent rien ouvert. Les bornes des corps relayés ne doivent jamais être plus
basses que celles d'AIODOS (`aiodos/network/body_limit.py`).

| Quoi | Valeur | Où |
|---|---|---|
| En-têtes d'une requête de l'API locale | 10 s | `control.rs` `HEADER_TIMEOUT` |
| Connexions de l'API locale qui n'ont pas montré le jeton | 512, la plus ancienne fermée au-delà | `admission.rs` `MAX_WAITING` |
| Corps d'une requête de l'API locale | 1 Mio | `control.rs` `MAX_PEERS_BODY` |
| Connexions QUIC ouvertes avec une même machine | 4, la plus ancienne fermée au-delà | `pool.rs` `MAX_CONNECTIONS_PER_PEER` |
| Flux bidirectionnels par connexion | 64 | `endpoint.rs` `MAX_STREAMS` |
| En-têtes d'un flux venu d'une autre machine | 60 s | `inbound.rs` `HEADER_TIMEOUT` |
| Requêtes relayées à la fois vers AIODOS, par machine | 32 conversations (`/p2p/v1/stream/`) et 32 autres requêtes, à part | `pool.rs` `MAX_STREAMS_PER_PEER`, `MAX_REQUESTS_PER_PEER` |
| Attente d'une place | 30 s, puis 503 | `pool.rs` `SLOT_WAIT` |
| Corps relayé | 64 Mio ; 1 Gio pour `/p2p/v1/work/result` ; 413 au-delà (une taille annoncée trop grande est refusée sans rien lire) | `relay_body.rs` `MAX_BODY`, `MAX_RESULT_BODY` |
| Silence pendant un corps | 60 s | `relay_body.rs` `BODY_IDLE` |
| Débit minimal d'un corps | 120 s de grâce, puis 16 Kio/s en moyenne ; 408 « Envoi trop lent » | `relay_body.rs` `PACE` |
| Réponse que l'autre machine ne lit plus | 60 s, puis flux coupé (sa place est rendue) | `relay_body.rs` `RESPONSE_STALL` |
| Services exposés | 64 | `tunnel.rs` `MAX_SERVICES` |
| Flux d'une machine vers un même service | 8 | `tunnel.rs` `MAX_SERVICE_STREAMS` |
| Connexions relayées à la fois par un tunnel | 8 (AIODOS en demande 4) | `tunnel.rs` `MAX_CONNECTIONS` |
| Connexions en attente du contrôle de leur programme | 512, contrôlées par lots de 1024 au plus | `tunnel.rs` `MAX_CHECKING`, `MAX_BATCH` |
| Attente du programme nommé | 30 s | `tunnel.rs` `OWNER_WAIT` |
| Service tenu : connexions gardées par le compagnon ; table relue | 2 ; chaque seconde | `hold.rs` `KEPT`, `CHECK_EVERY` |
| Réseau public, requêtes d'une même clé | 1 à la fois, 30 par heure ; 503 `busy` au-delà ; clé oubliée après 10 min sans requête | `public_serve.rs` `PER_KEY_IN_FLIGHT`, `PER_KEY_PER_HOUR`, `FORGET_KEY_AFTER` |
| Réseau public, requêtes de tout le public | 2 à la fois, 120 par minute ; 503 `busy` au-delà | `public_serve.rs` `GLOBAL_IN_FLIGHT`, `GLOBAL_PER_MINUTE` |
| Réseau public, requête d'un inconnu | `POST /public/v1/…` seulement (404 sinon) ; en-têtes en 10 s (16 Kio, 32 en-têtes) ; corps de 256 Kio lu en 30 s (8 lus à la fois) | `public_serve.rs` |
| Réseau public, connexions d'inconnus | 32 (la plus anciennement inactive fermée au-delà), fermées après 2 min sans flux ; 2 flux à la fois chacune | `public_serve.rs` `MAX_CONNECTIONS`, `CONNECTION_IDLE`, `MAX_STREAMS_PER_CONNECTION` |
| Réseau public, demandes de cette machine | une clé jetable par conversation, 32 au plus, oubliée après 15 min ; début de réponse attendu 120 s | `public_ask.rs` |

## Sécurité

- L'API locale n'écoute que sur la boucle locale et exige le jeton : une page web ouverte
  dans le navigateur ne peut ni lire l'état, ni changer la liste blanche, ni se servir
  du tunnel. Le jeton n'est jamais relayé à l'autre machine.
- Un autre programme de la machine (un autre compte du PC, une autre appli du téléphone)
  peut quand même se connecter à 127.0.0.1. L'API locale ne le laisse pas l'épuiser
  (en-têtes en 10 s, connexion fermée après un refus, 512 connexions sans jeton au plus) ;
  un tunnel gardé ne relaie que le programme nommé par AIODOS, et un service tenu ne sert
  que le compagnon (cf. « Tunnels et services »).
- Même une machine autorisée (un ami appairé) est bornée : connexions, flux, places,
  taille et débit des corps, réponses qu'elle ne lit pas (cf. « Bornes et délais »).
- Le contenu des échanges est chiffré deux fois : par QUIC (TLS 1.3, clés éphémères) et
  par l'enveloppe d'AIODOS (clé de la paire). Un relais voit qui parle à qui, quand et
  combien, pas le contenu.
- Réseau public : le nœud public sert toute clé, mais seulement `POST /public/v1/…`, dans
  les bornes plus haut, et AIODOS ne reçoit de l'appelant que le chemin, le corps,
  `Content-Type`, `Accept` et sa vraie clé (`X-Aiodos-Public`) ; jamais `X-Aiodos-Peer`.
  Pour demander, une clé jetable par conversation, en relais seulement, jamais écrite sur
  disque : le prêteur lit la conversation, mais ne voit ni la clé ni l'adresse IP de la
  machine qui demande. Pas d'enveloppe d'AIODOS ici : seul TLS d'Iroh protège le contenu
  en route.
- Reste à faire (cf. docs/phase2-internet.md et docs/securite.md) : protéger la clé de la
  machine (DPAPI sous Windows, étape 2.3) ; réserver des places à mes machines (le
  compagnon ne connaît pas le niveau de confiance des machines) ; une clé de compagnon qui
  change doit être réapprise par le réseau local.

## Tests

```powershell
cargo test                     # 57 essais unitaires ; à deux compagnons, relais coupés :
                               # tests/tunnel.rs (8), tests/services.rs (3) et
                               # tests/bornes.rs (14 : tunnel gardé sous une avalanche de
                               # connexions, service tenu, corps et places bornés, réponses
                               # non lues, API locale) ; tests/public.rs (2, réseau public) ;
                               # tests/public_ask.rs (2 : conversation confiée à un inconnu,
                               # en direct puis par un relais, et refusée par un autre) ;
                               # tests/relay_only.rs (3, par un relais lancé dans l'essai)
```

Côté Python (le compagnon doit être compilé, sinon ces tests sont ignorés) :
`tests/test_companion.py` (compagnons lancés et surveillés par AIODOS),
`tests/test_network_internet.py` (deux nœuds qui se partagent une indexation par leurs
compagnons ; il faut Ollama et le modèle d'embedding),
`tests/test_network_relay.py` (la même chose par un relais `iroh-relay` auto-hébergé,
compagnons en relais seulement ; il faut aussi `iroh-relay`, cf.
[docs/relais.md](../docs/relais.md)),
`tests/test_updates_companion_end_to_end.py` (une version reçue va chercher son
compagnon livré, le vérifie et le lance), `tests/test_securite_tunnels.py` (tunnel gardé
et serveur RPC tenu, avec de vrais compagnons), et sans compagnon `tests/test_route.py`,
`tests/test_netinfo.py`, `tests/test_updates_binaries.py` (choix du programme).

## Mesures du 16/10/2026 (PC principal, deux compagnons sur la même machine)

| Configuration | Première requête (connexion comprise) | Requêtes suivantes | Chemin |
|---|---|---|---|
| Relais et annuaires coupés (tests) | quelques ms | quelques ms | direct, 127.0.0.1 |
| Relais et DNS de n0, joint par la clé seule | 0,30 s | 10 à 30 ms | direct, IPv6 |
| Relais de n0 et DHT Mainline seule, joint par la clé seule | 2,6 s | 20 à 30 ms | direct, IPv6 |
| `iroh-relay --dev` sur la même machine, `--relay-only` (17/10) | 20 ms | 1 à 4 ms | relais |

Reste à mesurer sur le terrain : le téléphone en 4G par le compagnon (le cas mesuré
avec dumbpipe à l'étape 2.0), une machine derrière une autre box, et la tenue sur
plusieurs heures avec des changements de réseau.
