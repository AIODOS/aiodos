# Héberger un relais pour son cercle

Quand deux machines ne peuvent pas se joindre en direct (box qui filtre, 4G, IPv4 d'un
côté et IPv6 de l'autre), leurs compagnons `aiodos-net` passent par un **relais**. Le
relais ne voit qu'un trafic chiffré de bout en bout, mais il voit quelles clés se parlent,
depuis quelles adresses IP, et combien.

Par défaut, AIODOS utilise les **relais publics de n0**. Ils sont gratuits, mais réservés
au développement et aux usages amateurs. Un membre du cercle peut héberger le sien :
- il ne dépend plus de personne ;
- les métadonnées de connexion restent chez lui ;
- il peut le réserver à ses machines et à celles de ses amis.

Les options ci-dessous sont celles d'`iroh-relay` 1.3.0, la version d'Iroh du compagnon,
vérifiées dans ses sources (`src/main.rs`). Le 17/10/2026, un essai automatique a fait
passer deux vrais nœuds AIODOS par ce relais, avec la liste blanche décrite plus bas (cf.
[Essai sur une seule machine](#essai-sur-une-seule-machine)).

## Ce qu'il faut

- Une machine joignable depuis Internet, allumée en permanence : un petit serveur loué
  (VPS), ou un PC de la maison avec les ports ouverts sur la box.
- Un nom de domaine qui pointe vers elle (ex. `relais.mondomaine.fr`), pour le certificat
  TLS de Let's Encrypt.
- Ces ports ouverts :
  - **80/TCP** : obtention du certificat ;
  - **443/TCP** : le relais lui-même ;
  - **7842/UDP**, si possible : il aide les machines à découvrir leur adresse publique, et
    donc à se joindre en direct.
- Rust (https://rustup.rs) sur la machine qui construit le relais.

## Installer

```
cargo install iroh-relay --version 1.3.0 --features server --locked
```

`--locked` reprend les versions de dépendances avec lesquelles n0 a publié le relais.
Sous Windows, il faut les mêmes outils que pour le compagnon (outils C++ de Visual
Studio, `cargo` lancé depuis PowerShell). Sur le PC principal, la construction prend
environ une minute, et le programme (9,7 Mo) arrive dans `~/.cargo/bin/`.

## Configurer

Un fichier `relais.toml` :

```toml
http_bind_addr = "[::]:80"
enable_relay = true
enable_quic_addr_discovery = true
# Statistiques : sinon servies sur le port 9090, sur toutes les interfaces
enable_metrics = false

# Seules ces machines peuvent passer par le relais : leurs clés Internet
# (vue Réseau, carte « Internet », « Copier la clé de cette machine »).
# AVANT la section [tls] : voir plus bas.
access.allowlist = [
    "clé-de-la-machine-1",
    "clé-de-la-machine-2",
]

[tls]
cert_mode = "LetsEncrypt"
hostname = "relais.mondomaine.fr"
contact = "toi@mondomaine.fr"
cert_dir = "/var/lib/iroh-relay"
```

**La liste blanche doit précéder `[tls]`.** En TOML, tout ce qui suit un en-tête de
section lui appartient : écrite après `[tls]`, elle devient `tls.access.allowlist`.
`iroh-relay` ignore cette clé sans rien dire, et sert alors **tout le monde**. Vérifié
le 17/10/2026 : après `[tls]`, le relais retient `AllowAll` ; avant, `AllowlistAccess`.
Pour le vérifier sur son propre relais, le lancer une fois avec
`RUST_LOG=iroh_relay=debug` : la configuration retenue s'affiche au démarrage, et la
ligne `access:` doit commencer par `AllowlistAccess`. Une clé mal recopiée, elle,
empêche le relais de démarrer (« data is not a valid public key »).

Autres contrôles d'accès possibles, au même endroit :
- `access = "everyone"` : relais ouvert à tous ;
- `access.denylist = ["…"]` : tout le monde sauf certaines clés ;
- `access.shared_token = ["…"]` : un jeton secret.

Le jeton est lu dans l'en-tête `Authorization: Bearer …`, ou à défaut dans l'adresse
(`?token=…`). La liste blanche de clés est la plus simple pour un cercle. Il faut alors y
ajouter chaque nouvelle machine.

## Lancer

```
iroh-relay --config-path relais.toml
```

Au premier lancement, le relais obtient son certificat auprès de Let's Encrypt.

Le relais est muet par défaut : il n'écrit que ses erreurs. Pour voir les connexions, et
celles qu'il refuse, le lancer avec `RUST_LOG=iroh_relay=info` (sous PowerShell :
`$env:RUST_LOG = "iroh_relay=info"` avant de le lancer). Une machine refusée y apparaît
avec son adresse IP, mais sans sa clé : `error accepting upgraded connection: The relay
denied our authentication (not authorized)`.

## Ajouter une machine

Le fichier de réglages n'est lu qu'au lancement : après y avoir ajouté une clé, relancer
le relais. Les machines déjà admises se reconnectent aussitôt. La nouvelle réessaie
seule, sans rien relancer chez elle, mais elle espace ses essais tant qu'on la refuse
(jusqu'à une trentaine de secondes). Mesuré le 17/10/2026 : servie 0,1 s après la
relance quand on la refusait depuis 5 s, 23 s après quand on la refusait depuis 30 s.

## Essai sur une seule machine

`iroh-relay --dev` écoute en HTTP, sans certificat, sur le port 3340 :
`http://localhost:3340`. Il ouvre aussi ses statistiques sur le port 9090, et les deux
sur toutes les interfaces. Avec `--config-path`, ce mode garde la liste blanche, ignore
la partie TLS, et `http_bind_addr` change le port. L'essai automatique
(`tests/test_network_relay.py`) lance ainsi son relais, sur un port libre :

```toml
http_bind_addr = "127.0.0.1:3340"
enable_metrics = false
access.allowlist = ["clé-de-la-machine-1", "clé-de-la-machine-2"]
```

Deux nœuds sur une même machine se joignent toujours en direct, même avec un relais.
Pour les obliger à passer par lui, AIODOS a une option réservée aux essais,
`python -m aiodos --internet-relay-only http://127.0.0.1:3340`. Elle lance le compagnon
avec ce relais seulement (`aiodos-net --relay-only`), sans annuaire ni ouverture de
port. Le compagnon n'ouvre alors aucune socket UDP : il ne peut ni joindre une machine
en direct, ni être joint en direct.

Ce qu'a montré l'essai du 17/10/2026 :
- deux nœuds qui ne se connaissent pas sur le réseau local s'appairent sur invitation,
  par le relais, puis se confient une indexation ;
- la vue Réseau dit des deux côtés que la connexion passe **par un relais** ;
- une troisième machine, absente de la liste blanche, est refusée par le relais. Même
  invitée, et admise par les deux compagnons, elle reste injoignable ;
- aucun annuaire n'est nécessaire : l'invitation, puis l'échange des clés entre machines
  appairées, donnent à chacune le relais de l'autre.

## Le donner à AIODOS

Réglages → Internet → **Relais**. Deux façons de faire :
- `n0, https://relais.mondomaine.fr` ajoute ce relais à ceux de n0 ;
- `https://relais.mondomaine.fr` seul n'utilise plus que lui.

Le compagnon redémarre avec le nouveau relais. Sur chaque machine de la vue Réseau, on
voit si la connexion est **en direct** ou **par un relais**, avec son délai. Une machine
que le relais refuse le dit dans sa carte « Internet » : « le relais … refuse cette
machine : sa clé doit être dans la liste blanche du relais ».

Toutes les machines du cercle doivent avoir ce relais parmi les leurs. Une machine
publie dans l'annuaire le relais par lequel on peut la joindre, et les autres doivent
pouvoir s'y connecter.
