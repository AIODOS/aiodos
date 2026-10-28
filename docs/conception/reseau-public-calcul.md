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
   mot), il passe à la machine suivante, comme aujourd'hui (`placement.run`).

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
- **Jamais** sur batterie ; un téléphone seulement en charge et en Wi-Fi (comme pour un
  ami) ; une conversation de l'utilisateur sur cette machine passe devant et arrête le prêt
  en cours, comme aujourd'hui.

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
  machine (une des miennes si elle a le modèle, sinon une autre machine publique), avec la
  même graine et une température de 0 pour les deux. Le prêteur ne peut pas savoir quelles
  demandes sont contrôlées.
- **Comparer** : les mêmes modèle, graine et réglages donnent presque le même texte, mais
  des cartes différentes (CUDA, Vulkan, processeur) divergent après quelques dizaines de
  jetons. On compare donc le début (les premiers jetons identiques) et le sens (similarité
  des embeddings de deux réponses entières), avec des seuils **mesurés** sur des paires
  honnêtes avant d'en faire une règle. Piste plus forte, à mesurer : faire calculer par la
  seconde machine la **vraisemblance** de la réponse du prêteur (une lecture, bien moins
  coûteuse qu'une génération) ; une réponse fausse, d'un autre modèle ou piégée y est
  très improbable.
- **Réputation** : une table locale par clé de prêteur (vitesse mesurée, contrôles réussis
  ou non). Un contrôle raté met la machine de côté 7 jours ; deux, pour de bon. Rien n'est
  partagé avec les autres dans cette version (pas de liste noire commune, qu'un menteur
  pourrait remplir).

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
  table SQLite) : § 8.
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

Images et vidéos pour les inconnus ; pièces jointes ; réputation partagée ; preuve de
travail contre les fausses identités (à mesurer si le plafond global ne suffit pas) ;
paiement ou crédit (phase3-incitation.md).
