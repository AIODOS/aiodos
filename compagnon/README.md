# aiodos-net — le compagnon réseau d'AIODOS

Petit programme Rust (GPL-3.0) qui relie **par Internet** les machines appairées
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
Bash, le `link.exe` de Git peut masquer celui de Visual Studio.

Pour un binaire à distribuer, plus petit (10,6 Mo au lieu de 17,5 mesurés le 16/10/2026)
mais plusieurs minutes de compilation de plus :

```powershell
$env:CARGO_PROFILE_RELEASE_OPT_LEVEL = "s"
$env:CARGO_PROFILE_RELEASE_LTO = "fat"
$env:CARGO_PROFILE_RELEASE_CODEGEN_UNITS = "1"
cargo build --release
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

**Ou compiler depuis le PC** avec le NDK Android, puis copier le binaire dans Termux
(compilation vérifiée le 16/10/2026 avec le NDK 28.2 : exécutable ARM64 pour Android 7
et plus, 17 Mo ; pas encore lancé sur un téléphone) :

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
`POST /api/network/internet`), ou option `python -m aiodos --internet`. AIODOS cherche le
programme dans `AIODOS_NET_BIN`, puis `compagnon/target/release/`, puis le `PATH`. Le
réglage `p2p_route` choisit le chemin vers les machines appairées : `auto` (réseau local
s'il répond, Internet sinon), `lan` ou `internet` (pour essayer le compagnon seul).
`--internet-local` lance un compagnon d'essai sans relais, sans annuaire et sans box
(deux nœuds sur une même machine, comme dans les tests).

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
| `--dial-timeout` | `20` | délai pour joindre une machine (secondes ; AIODOS attend 30 s au moins une réponse par Internet) |
| `--supervised` | — | lancé par AIODOS : s'arrête quand son entrée standard se ferme |

Le jeton de l'API locale vient de la variable `AIODOS_NET_TOKEN` (jamais de la ligne de
commande, visible des autres programmes) ; sans elle, il est tiré au hasard et écrit
dans `<data-dir>/aiodos-net.token`. Au démarrage, une ligne JSON sur la sortie standard
annonce que le compagnon est prêt (`{"event": "ready", "id": …, "listen": …}`) ; le
journal va sur la sortie d'erreur (niveau réglable par `AIODOS_NET_LOG`, par exemple
`aiodos_net=debug,iroh=info`). Windows peut demander l'autorisation du pare-feu au
premier lancement : l'accepter pour les réseaux privés suffit en local, mais une
connexion directe par Internet demande aussi les réseaux publics (Iroh sait alors passer
par un relais).

## API locale

Toutes les requêtes portent l'en-tête `X-Aiodos-Net-Token`.

| Requête | Effet |
|---|---|
| `GET /net/v1/status` | clé publique, relais (connectés ou non), adresses, liste blanche, connexions refusées, changements de réseau, et pour chaque machine : connectée, chemin `direct` ou `relay`, adresse distante, latence (`rtt_ms`), sens de la connexion, compteurs |
| `PUT /net/v1/peers` | remplace la liste blanche : `{"peers": [{"id": "<clé>", "addrs": ["ip:port"], "relays": ["https://…"]}, {"ticket": "endpoint…"}]}` ; les connexions des clés retirées sont fermées |
| `GET /net/v1/ticket` | ticket Iroh à donner à une autre machine (clé, relais, adresses) ; il ne contient aucun secret et ne fait pas entrer dans la liste blanche à lui seul |
| `POST /net/v1/shutdown` | arrêt propre |
| `* /peer/<clé>/p2p/…` | requête relayée vers cette machine |

Entre compagnons, le protocole `aiodos/2` (ALPN) est simple : un échange HTTP/1.1 par
flux QUIC bidirectionnel. Le contenu reste l'enveloppe scellée d'`aiodos/1`, que le
compagnon ne lit pas. Codes de fermeture d'une connexion : 0 arrêt, 1 doublon, 403 clé
refusée ou retirée.

## Sécurité

- L'API locale n'écoute que sur la boucle locale et exige le jeton : une page web ouverte
  dans le navigateur ne peut ni lire l'état, ni changer la liste blanche, ni se servir
  du tunnel. Le jeton n'est jamais relayé à l'autre machine.
- Le contenu des échanges est chiffré deux fois : par QUIC (TLS 1.3, clés éphémères) et
  par l'enveloppe d'AIODOS (clé de la paire). Un relais voit qui parle à qui, quand et
  combien, pas le contenu.
- Reste à faire (cf. docs/phase2-internet.md) : protéger la clé de la machine (DPAPI sous
  Windows, étape 2.3) ; invitations à distance (2.4 : l'appairage passe encore par le
  réseau local) ; une clé de compagnon qui change doit être réapprise par le réseau local.

## Tests

```powershell
cargo test                     # 15 tests unitaires + 8 tests à deux compagnons (relais coupés)
```

Côté Python (le compagnon doit être compilé, sinon ces tests sont ignorés) :
`tests/test_companion.py` (compagnons lancés et surveillés par AIODOS),
`tests/test_network_internet.py` (deux nœuds qui se partagent une indexation par leurs
compagnons ; il faut Ollama et le modèle d'embedding), et sans compagnon
`tests/test_route.py`, `tests/test_netinfo.py`.

## Mesures du 16/10/2026 (PC principal, deux compagnons sur la même machine)

| Configuration | Première requête (connexion comprise) | Requêtes suivantes | Chemin |
|---|---|---|---|
| Relais et annuaires coupés (tests) | quelques ms | quelques ms | direct, 127.0.0.1 |
| Relais et DNS de n0, joint par la clé seule | 0,30 s | 10 à 30 ms | direct, IPv6 |
| Relais de n0 et DHT Mainline seule, joint par la clé seule | 2,6 s | 20 à 30 ms | direct, IPv6 |

Reste à mesurer sur le terrain : le téléphone en 4G par le compagnon (le cas mesuré
avec dumbpipe à l'étape 2.0), une machine derrière une autre box, et la tenue sur
plusieurs heures avec des changements de réseau.
