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
vérifiées dans ses sources (`src/main.rs`).

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
cargo install iroh-relay --version 1.3.0 --features server
```

## Configurer

Un fichier `relais.toml` :

```toml
http_bind_addr = "[::]:80"
enable_relay = true
enable_quic_addr_discovery = true

[tls]
cert_mode = "LetsEncrypt"
hostname = "relais.mondomaine.fr"
contact = "toi@mondomaine.fr"
cert_dir = "/var/lib/iroh-relay"

# Seules ces machines peuvent passer par le relais : leurs clés Internet
# (vue Réseau, carte « Internet », « Copier la clé de cette machine »)
access.allowlist = [
    "clé-de-la-machine-1",
    "clé-de-la-machine-2",
]
```

Autres contrôles d'accès possibles :
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

Au premier lancement, le relais obtient son certificat auprès de Let's Encrypt. Pour un
essai sur une seule machine, sans certificat ni ports : `iroh-relay --dev`. Il écoute
alors en HTTP sur le port 3340, et son adresse est `http://localhost:3340`.

## Le donner à AIODOS

Réglages → Internet → **Relais**. Deux façons de faire :
- `n0, https://relais.mondomaine.fr` ajoute ce relais à ceux de n0 ;
- `https://relais.mondomaine.fr` seul n'utilise plus que lui.

Le compagnon redémarre avec le nouveau relais. Sur chaque machine de la vue Réseau, on
voit si la connexion est **en direct** ou **par un relais**, avec son délai.

Toutes les machines du cercle doivent avoir ce relais parmi les leurs. Une machine
publie dans l'annuaire le relais par lequel on peut la joindre, et les autres doivent
pouvoir s'y connecter.
