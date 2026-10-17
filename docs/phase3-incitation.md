# Phase 3 : récompenser le calcul et vérifier les inconnus

Étude du 17/10/2026, faite d'abord pour Arkanyx (un autre projet, classé ce jour-là) et
reprise ici parce qu'elle répond à la ligne « Phase 3 — Réseau public : vérification des
résultats, incitation, gouvernance » du [README](../README.md). Ce n'est pas la priorité : la
direction principale d'AIODOS reste la répartition des gros modèles
([gros-modeles.md](gros-modeles.md)). Rien n'est décidé ici ; c'est une base pour le jour où
la phase 3 commencera.

## 1. Ce qu'on en tire, en bref

- Faire calculer des inconnus et les récompenser s'appelle aujourd'hui le « minage de travail
  utile ». AIODOS en a déjà la plomberie : unités indépendantes, baux, fichiers chiffrés,
  contrôle par échantillon, compte de réciprocité.
- Ce qui manque n'est pas technique : **une récompense ne vaut que ce que quelqu'un paie pour
  le calcul**. Les projets qui ont commencé par un jeton (une crypto-monnaie propre au projet)
  l'ont appris à leurs dépens.
- Première marche conseillée : des **crédits visibles, non monnayables**, qui donnent la
  priorité, comme les « kudos » d'AI Horde ([projets-voisins.md](projets-voisins.md), § 3,
  point 3).
- Le réseau public ne doit recevoir que des **unités indépendantes et vérifiables** : jamais
  des morceaux d'un modèle découpé, jamais des données privées.

## 2. Ce que rapporte une machine ordinaire (ordres de grandeur, octobre 2026)

Pour savoir ce qu'on peut promettre à quelqu'un qui prête sa machine : en argent, presque rien.

| Usage d'une machine ordinaire en France (électricité ~0,20 €/kWh) | Ce que ça rapporte |
|---|---|
| Miner du Bitcoin avec une machine dédiée | Une perte : le seuil de rentabilité est vers 0,10 €/kWh |
| Miner avec une carte graphique (ex. RTX 4070 Laptop) | Quelques dizaines de centimes par jour, moins que l'électricité |
| Louer sa carte graphique à des entreprises d'IA (Salad, Vast.ai) | Avec 8 Go de mémoire vidéo, à peu près ce que coûte l'électricité |
| Prêter son téléphone à un réseau de calcul (Acurast) | Quelques euros par mois, en points de valeur incertaine |

Ce qu'AIODOS peut offrir à un contributeur, c'est donc **de la priorité, l'accès à des modèles
qu'il ne pourrait pas faire tourner seul, et la réciprocité**, pas un revenu. Le dire
franchement évite d'attirer des gens venus pour l'argent.

## 3. Les précédents et ce qu'ils enseignent

| Projet | Récompense | Ce qui s'est passé | Leçon pour AIODOS |
|---|---|---|---|
| BOINC (SETI@home…) | Crédits non monnayables ; travail validé par redondance (la même unité confiée à deux machines) | Fonctionne depuis plus de vingt ans | La redondance marche pour les calculs qui donnent toujours le même résultat |
| Gridcoin | Une crypto-monnaie qui paie le calcul BOINC, depuis 2013 | ~4,3 M$ de valeur totale, ~7 000 $ échangés par jour (octobre 2026) | Un jeton posé sur du calcul bénévole, sans clients qui paient, reste marginal |
| BitTorrent Token (BTT) | Payer ceux qui partagent les fichiers | Énorme base d'utilisateurs, mais le jeton a chuté et l'incitation est devenue sans objet | Beaucoup d'utilisateurs ne font pas une demande pour un jeton |
| AI Horde | « Kudos » qui donnent la priorité, ni achetables ni vendables | Fonctionne, sans spéculation | La bonne première marche |
| Pearl (lancé le 27/04/2026) | Jeton miné par des multiplications de matrices, le calcul de base de l'IA | Le protocole vérifie que le calcul est fait, pas qu'il sert : l'essentiel est de l'inférence que personne n'a achetée | « Utile » doit venir d'une vraie demande (le travail d'un utilisateur), pas du protocole |
| Bittensor | Jetons par sous-réseau ; réponses notées par des validateurs | Selon une analyse critique, 22 à 40 $ de jetons émis pour 1 $ payé par de vrais clients | Des récompenses financées par la création de jetons, pas par des clients |
| Réseaux de calcul payants (Aethir, Akash, Render, io.net) | Jetons, contre de la puissance louée | De vrais clients (~128 M$ de revenus en 2025 pour Aethir, selon une source), mais des jetons valorisés 10 à 25 fois leurs revenus | La demande existe, surtout pour des GPU de centre de données |
| Acurast | Points par téléphone (250 par mois) | ~70 000 téléphones en test ; conversion en jeton non publiée | On peut embarquer beaucoup de téléphones ; chacun vaut peu |

## 4. Vérifier le travail d'un inconnu

Aujourd'hui, ce qu'un ami calcule est « vérifié par échantillon : en cas d'écart, sa machine
est mise de côté » (README). Avec des inconnus, il faut aller plus loin, et la méthode dépend
du type de travail.

- **Indexation (embeddings).** Le résultat est presque déterministe, et AIODOS exige déjà le
  même modèle, à l'empreinte près. On peut recalculer un échantillon et comparer les vecteurs
  avec une tolérance : deux GPU différents ne donnent pas exactement les mêmes nombres, la
  tolérance est à mesurer. Pour un nouveau venu, confier la même unité à deux machines, comme
  BOINC. Coût : la part recalculée (un échantillon de 5 % = 5 % de calcul en plus).
- **Génération de texte (prêt de modèles).** Recalculer coûte autant que générer. La méthode
  TOPLOC de Prime Intellect fait mieux : la machine qui génère publie une empreinte compacte
  de l'état interne du modèle (258 octets pour 32 mots), que le vérificateur contrôle environ
  100 fois plus vite que la génération. Elle détecte un autre modèle, un autre prompt ou une
  précision réduite. Limite pour nous : il faut lire l'état interne du modèle. Ollama ne le
  donne pas à ma connaissance ; llama.cpp, son moteur, peut sortir l'état de la dernière
  couche : à vérifier en pratique. D'autres méthodes (VeriLLM) demandent un tiers de confiance
  qui recalcule, ce qui recentralise.
- **Un gros modèle découpé entre machines** (la direction de
  [gros-modeles.md](gros-modeles.md)). Chaque machine de la chaîne peut fausser le résultat
  sans que les autres le voient. **À réserver au cercle de confiance** : ses machines et celles
  des amis. Le réseau public ne reçoit que des unités indépendantes.
- **Réputation.** Un score par machine (unités réussies, écarts relevés) : plus il est haut,
  moins on vérifie ; un écart fait repartir de zéro, ou exclut.
- **Fausses identités.** Rien n'empêche quelqu'un de lancer cent machines virtuelles pour
  gonfler ses crédits (on parle d'« attaque Sybil »). L'appairage sur invitation d'AIODOS est
  une bonne base : la confiance part des machines qu'on connaît, et un nouveau venu ne pèse rien
  tant qu'il n'a pas fait ses preuves.

## 5. Confidentialité : ce qui peut partir chez un inconnu

Le chiffrement protège le trajet, pas le calcul : la machine qui travaille doit lire l'unité
en clair. Donc :

- les bibliothèques « gardées sur ce PC » et « partagées avec mes amis » ne partent **jamais**
  sur le réseau public ;
- il faudrait un troisième niveau, « public », choisi exprès : pages web publiques, documents
  libres, jeux de données ouverts ;
- prêter un modèle à des inconnus leur montre les conversations : à limiter aux échanges
  marqués publics ;
- les zones de calcul protégées des processeurs de serveurs (« TEE ») n'existent presque pas
  sur les PC et téléphones ordinaires : on ne peut pas compter dessus.

## 6. Proposition : trois marches

1. **Crédits visibles, non monnayables.** Le compte de réciprocité existe déjà ; il suffit de
   l'afficher (donné / reçu) et d'en faire la priorité dans la file. Aucune question
   juridique, et on apprend si les gens contribuent.
2. **Vérification et réputation pour le public.** Échantillons, redondance pour les nouveaux
   venus, niveau « public » des bibliothèques, étude de TOPLOC pour le prêt de modèles.
3. **Paiement réel, seulement si des clients extérieurs paient** (des particuliers ou des
   entreprises qui achètent du calcul ou le service). Deux formes : en euros, comme Salad
   (pas de MiCA, mais des obligations fiscales et de paiement), ou en jeton (voir § 7). À
   décider le moment venu, avec un avocat.

## 7. Cadre légal (à faire valider par un avocat)

- Le règlement européen MiCA vise les actifs **transférables sur un registre distribué**. Des
  crédits tenus dans la base d'AIODOS, non transférables, n'en sont probablement pas : à
  confirmer.
- Pour un jeton : MiCA dispense de « livre blanc » un jeton créé automatiquement en récompense
  de la tenue du registre, c'est-à-dire le minage (article 4(3)(b)).
- Cette dispense tombe si on vise une cotation sur une plateforme européenne (article 4(4),
  questions-réponses 2671 de l'ESMA) ; une cotation sur une plateforme décentralisée peut
  aussi compter, au cas par cas.
- Un livre blanc publié volontairement fait appliquer tout le régime.
- En France, c'est l'AMF qui reçoit les notifications.
- **Vocabulaire.** « IA + minage » est le premier thème d'arnaque de 2026 (exemple :
  Bitgrokapp, sur la liste noire de l'AMF depuis le 3/09/2026). Parler de « crédits de
  calcul », jamais de « minage » ni de « rendement ».

## 8. Leçons d'ingénierie héritées d'Arkanyx

Arkanyx, un bot de trading en simulation classé le 17/10/2026, a appris ces leçons à ses
dépens ; elles valent pour la file de tâches et le réseau d'AIODOS.

- **Vérifier en conditions réelles, de bout en bout.** Trois vagues de bugs n'étaient visibles
  qu'en fonctionnement réel, jamais dans les tests nourris de données inventées. La pire : une
  échelle de score incompatible avec le seuil qui la filtrait (des scores vers 0,1 pour un
  seuil de 0,5) bloquait toute activité, sans aucun message d'erreur.
- **Calibrer chaque échelle contre le seuil qui la filtre.** Ici : la tolérance de comparaison
  des vecteurs et le seuil d'exclusion par réputation.
- **Une ligne de vie à chaque cycle, même quand il n'y a rien à faire.** Sinon « vivant mais
  inactif » et « mort » donnent le même écran vide. Journaliser chaque erreur au lieu de
  laisser un fil mourir en silence, et signaler la reprise après une veille (un trou anormal
  entre deux cycles).
- **Au redémarrage, réconcilier, ne jamais refaire.** Repartir de l'état enregistré, sans
  refaire un travail déjà fait ; les baux d'AIODOS vont déjà dans ce sens.
- **Séparer un mode d'essai rapide**, pour voir le système bouger, **d'un mode réel prudent.**

## Sources

- Vérification : [TOPLOC (Prime Intellect)](https://www.primeintellect.ai/blog/toploc),
  [VeriLLM](https://arxiv.org/pdf/2509.24257)
- Précédents : [Gridcoin (CryptoSlate)](https://cryptoslate.com/coins/gridcoin/),
  [BitTorrent Token (Decrypt)](https://decrypt.co/8128/the-bittorrent-tokens-flagging-price-is-making-it-unusable),
  [AI Horde](https://aihorde.net/mission)
- Pearl : [Hashrate Index](https://hashrateindex.com/blog/pearl-prl-ai-compute-cryptocurrency/),
  [« The Usefulness Gap in Proof-of-Useful-Work »](https://arxiv.org/pdf/2606.04819),
  [« Proofs of Useful Work from Arbitrary Matrix Multiplication »](https://arxiv.org/pdf/2504.09971)
- Bittensor : [analyse critique (Pine Analytics)](https://pineanalytics.substack.com/p/the-bear-case-for-bittensor-tao)
- Réseaux de calcul payants : [BlockEden](https://blockeden.xyz/blog/2026/04/12/depin-revenue-pivot-token-subsidies-ai-compute-akash-render-ionet/),
  [rzlt](https://www.rzlt.io/blog/7-depin-projects-generating-10m-revenue-(and-what-you-can-learn-from-them))
- Téléphones : [Acurast (Dealroom)](https://app.dealroom.co/companies/acurast),
  [Acurast (Gate)](https://www.gate.com/learn/articles/a-complete-guide-to-acurast/6875)
- Location de GPU : [Salad, RTX 3070](https://computeprices.com/providers/salad/gpus/rtx3070),
  [GPUnex](https://www.gpunex.com/blog/rent-out-your-gpu)
- Minage Bitcoin : [mexc (rentabilité, octobre 2026)](https://www.mexc.com/crypto-pulse/article/bitcoin-mining-profitability-calculator-171457),
  [Abundant Mines](https://abundantmines.com/hashprice-vs-profitability/) ;
  électricité : [tarif EDF au 1/08/2026](https://www.fournisseurs-electricite.com/guides/prix/kwh-electricite)
- MiCA : [Osborne Clarke](https://osborneclarke.com/insights/what-are-eus-white-paper-requirements-micar-and-do-they-apply-bitcoin),
  [ESMA, Q&R 2671](https://www.esma.europa.eu/publications-data/questions-answers/2671),
  [Cerha Hempel](https://www.cerhahempel.com/blog/fintech-ledger/when-is-a-white-paper-required-under-micar-applicability-and-exceptions)
- Arnaques : [Bitgrokapp sur la liste noire de l'AMF](https://consultation.avocat.fr/blog/jocelyn-ziegler/article-2983319-bitgrokapp.com-arnaque-ce-que-revele-l-inscription-sur-la-liste-noire-de-l-amf.html)
