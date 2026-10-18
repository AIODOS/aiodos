# Appairage par groupe : un ami joint toutes mes machines

Décision de l'auteur, le 18/10/2026, après l'essai avec la machine d'un ami
(EarthlingVisual) : « Oui, par groupe ». Un ami appairé avec une de mes machines doit
pouvoir joindre aussi mes autres machines, sans refaire l'appairage avec chacune. À
concevoir avec soin, parce que c'est la sécurité de l'appairage qui est en jeu : la
confiance est transmise par une de mes machines, et l'ami doit être d'accord.

**Conception relue et tranchée le 18/10/2026 (§ 9), puis codée** : `aiodos/network/group.py`,
essayée avec trois et quatre vrais nœuds (`tests/test_group_pairing.py`).

## 1. Aujourd'hui

- On appaire **deux machines à la fois** : elles échangent des clés éphémères X25519,
  affichent le même code à 6 chiffres, et l'utilisateur confirme sur **chacune** que le code
  est le même. Chacune dit alors ce qu'est l'autre pour elle : une de ses machines (MINE)
  ou celle d'un ami (FRIEND).
- La clé de la paire (HKDF du secret commun) scelle ensuite tous leurs messages
  (ChaCha20-Poly1305, horodatés, liés à l'action, à l'émetteur et au destinataire).
- Par Internet, il faut d'abord une invitation acceptée des deux côtés : chaque compagnon
  n'admet que les clés de sa liste blanche.

Conséquence : un ami appairé avec mon portable ne voit pas mon PC fixe. Pour qu'il le
voie, il faut refaire l'appairage avec chaque machine, et par Internet une invitation par
machine. Avec trois machines de chaque côté, cela fait neuf appairages.

## 2. Ce que « groupe » veut dire ici

- **Mon groupe** : mes machines, qui sont « mes machines » les unes pour les autres, de
  proche en proche (le portable connaît le PC fixe, qui connaît le téléphone).
- **Un ami du groupe** : la machine d'un ami appairée avec une de mes machines, qui
  accepte d'être présentée aux autres.
- **Dans les deux sens** : l'ami a peut-être lui aussi plusieurs machines ; elles peuvent
  rejoindre les miennes de la même façon, avec mon accord.
- **Pas plus loin** : on ne présente que ses propres machines. Un ami ne peut pas me
  présenter la machine d'un de ses amis : les amis d'amis, c'est la phase 3 (réseau public).

## 3. Le protocole proposé : une présentation, par une machine de confiance

Exemple : mon portable P est appairé avec la machine A de mon ami (A est un ami pour P,
P est un ami pour A). Mon PC fixe F est une de mes machines pour P. P présente F à A.

1. **P propose la présentation** à F et à A, chacun par sa propre paire (P–F, P–A), donc
   dans des messages scellés que personne d'autre ne peut lire ni fabriquer.
2. **Chacun dit oui ou non.** Côté A, l'utilisateur voit la première fois : « Portable
   propose de te présenter les autres machines de la même personne : PC fixe,
   téléphone. » Il peut accepter celle-ci, accepter toutes celles de ce groupe à
   l'avenir, ou refuser. Côté F, rien à demander : P et F sont à moi, et j'ai déjà choisi
   l'ami (§ 9, question 1).
3. **F et A font leur échange de clés, à travers P.** Chacun crée une clé éphémère X25519 ;
   la clé publique de A va à F en passant par P (A→P scellé avec la paire A–P, puis P→F
   scellé avec la paire P–F), et celle de F va à A par le même chemin. F et A en tirent la
   clé de leur nouvelle paire avec la fonction de l'appairage actuel (`pair_secrets`).
   **P ne voit que des clés publiques : il ne connaît pas la clé de F–A.** Il n'y a pas
   de code à 6 chiffres à comparer : ce qui garantit qu'il n'y a personne au milieu, c'est
   que les deux trajets passent par des paires déjà vérifiées par un humain.
4. **Le niveau de confiance ne monte jamais.** F reçoit A au niveau que P a donné à A
   (un ami) ; A reçoit F au niveau qu'il a donné à P (un ami, ou une de ses machines s'il
   avait dit que P était à lui). Une présentation ne peut rien donner de plus que ce que la
   machine qui présente avait elle-même. « Mes machines » ne passe jamais par un lien
   d'ami.
5. **Par Internet**, P donne aussi à F la clé Internet de A et de quoi la joindre, et
   inversement ; chaque compagnon les ajoute à sa liste blanche. Pas besoin d'invitation :
   la présentation arrive par un chemin déjà authentifié.
6. **Chacun garde la trace** : la machine présentée est notée « présentée par Portable »
   (colonne `introduced_by`), visible dans la vue Réseau.

Une présentation expire au bout de 3 minutes, comme une demande d'appairage, et il ne
peut y en avoir que quelques-unes en attente à la fois.

**Dans le code**, les messages sont `group/introduce` (P→F : F prépare sa clé éphémère),
`group/propose` (P→A : A accepte d'office ou demande à son utilisateur), `group/key` (P→F :
F termine), et `group/answer` (A→P, quand l'utilisateur de A a répondu).

**Rien à moitié.** Si une étape échoue (machine injoignable, refus), P le dit à celle qui
attendait (`group/cancel`) : une paire gardée trop tôt est défaite. La réponse de A
attend que F ait terminé, et A ne garde la paire que si F l'a gardée. Une paire déjà
connue n'est jamais remplacée (ce serait une clé différente de l'autre côté).

**De proche en proche.** Toute machine qui gagne la machine d'un ami (appairée, choisie
comme amie, ou présentée) lui présente à son tour ses propres machines. Ainsi, après un
seul appairage entre mon portable et le PC de Léa, et une réponse de chaque côté, mon PC
fixe rencontre aussi le téléphone de Léa. « Toujours accepter ce groupe », dit sur une de
mes machines, vaut pour mes autres machines (`group/ok`), y compris pour les questions
déjà affichées.

**Présentations croisées.** De proche en proche, la même paire peut être présentée des
deux côtés à la fois (mon portable présente mon PC fixe au téléphone de Léa pendant que le
PC de Léa présente son téléphone à mon PC fixe). Les deux machines de la paire appliquent
la même règle : la présentation de la machine au plus petit identifiant continue, l'autre
s'arrête. Une seule aboutit, avec une seule clé.

**Une machine qui arrive plus tard dans mon groupe** (un nouveau PC appairé comme une de
mes machines) est présentée de la même façon aux amis qui ont accepté « toutes celles de
ce groupe » ; les autres reçoivent une demande.

## 4. Retirer, mettre de côté

- **Retirer un ami** depuis une de mes machines le retire de **toutes** (proposition) :
  la machine où je le retire prévient mes autres machines, qui le retirent à leur tour et
  le lui disent. Une case permet de ne le retirer que de cette machine.
- **Une de mes machines hors ligne** au moment d'un retrait ou d'une mise de côté
  l'apprend à son retour (`group_outbox`, redit pendant 30 jours) ; la fenêtre de retrait
  dit lesquelles n'ont pas pu être prévenues tout de suite.
- **Mettre de côté** une machine d'ami après un contrôle échoué (un résultat faux) : la
  machine qui l'a vu prévient les autres, qui la mettent de côté aussi (proposition). Un
  résultat faux ne dépend pas de la machine à qui il a été rendu.
- **Côté ami**, la même règle : retirer une de mes machines la retire de toutes les
  siennes, sauf « seulement de celle-ci ». Retirer d'un geste toutes les machines d'un même
  groupe reste à faire.
- **Si une de mes machines est volée**, je la retire de mes autres machines comme
  aujourd'hui. Les paires qu'elle a présentées gardent leur clé, qu'elle n'a jamais
  connue ; elles restent valables, ce qui est voulu (ce sont mes autres machines et mes
  amis).

## 5. Menaces, et réponses

| Menace | Réponse |
|---|---|
| Une machine inconnue se fait passer pour une des miennes, pour être présentée à un ami | Elle devrait être une de mes machines pour P, donc appairée avec un code confirmé par moi |
| Une machine qui présente donne plus de droits qu'elle n'en a | Niveau hérité, jamais plus haut ; « mes machines » ne passe jamais par un ami |
| Amis d'amis de proche en proche | On ne présente que ses propres machines (MINE pour celle qui présente) |
| La machine qui présente garde la clé de la nouvelle paire | Échange X25519 relayé : elle ne voit que les clés publiques |
| Une présentation rejouée ou modifiée en route | Messages scellés et horodatés de l'appairage actuel ; présentation à usage unique, 3 minutes |
| Avalanche de demandes | Quelques présentations en attente au plus, et l'accord de l'ami |
| Une de mes machines compromise présente des machines pirates à mes amis | Le même risque existe déjà : elle a mes clés. L'ami voit « présentée par … » et peut tout retirer d'un geste |
| Une de mes machines volée fait entrer des machines pirates chez moi (elle joue aussi le côté de l'ami) | En la retirant, je retire aussi, de toutes mes machines, les machines d'amis qu'elle a présentées (case cochée dans la fenêtre de retrait) |
| Un identifiant ou une adresse piégés dans une présentation (chemin, URL) | Seul un identifiant de machine (32 caractères hexadécimaux) et une adresse IP passent ; l'interface encode les identifiants dans ses adresses. L'adresse sur le réseau local n'est donnée que si la machine qui présente joint les deux par lui |
| « Toujours accepter » détourné pour faire entrer des machines sans fin | Au plus 8 machines par jour d'une même machine, ensuite on demande ; 3 questions en attente au plus par machine. Le compagnon écarte seule une clé Internet illisible, sans rejeter les autres |
| Une présentation ramène une machine refusée ou retirée (de proche en proche) | Refus et retraits sont gardés (`group_declined`) : aucune présentation ne les ramène, seul un appairage par code |
| Une question acceptée par mégarde (Entrée ou Espace tapés ailleurs) | Les questions venues d'une autre machine ne s'acceptent qu'au clic |

## 6. Interface

- **Une présentation reçue** ouvre une question, quelle que soit la vue : accepter, refuser,
  avec la case « Accepter d'office les prochaines machines de ce groupe ». La fermer sans
  répondre (Échap, clic à côté) ne refuse rien : elle attend dans « Machines appairées »
  (« attend ta réponse », bouton « Répondre ») jusqu'à son expiration.
- **Machines appairées** : une machine présentée porte « présentée par … » ; le menu ⋯ de
  la machine d'un ami propose « Présenter mes autres machines » (le résultat dit qui a été
  appairée, qui connaissait déjà, qui est injoignable, qui a refusé).
- **Retirer la machine d'un ami** : retirée de toutes mes machines, sauf si la case
  « Seulement de cette machine » est cochée.
- **Machines connectées** : une machine présentée y apparaît comme les autres, avec
  « présentée par … » en info-bulle.

## 7. Données

- Migration : `peers.introduced_by` (la machine qui a présenté, vide pour un appairage
  direct) et `peers.group_ok` (j'accepte toutes les machines du groupe de celle-ci).
- Les présentations en attente restent en mémoire, comme les appairages en attente
  (`network/pairing.py`), et disparaissent au redémarrage.
- Nouvelles actions entre nœuds, scellées : `group/introduce`, `group/propose`,
  `group/key` (le relais des clés publiques), `group/answer`, `group/cancel`,
  `group/remove`, `group/set_aside`, `group/ok`.

## 8. Étapes

1. Fait : le protocole sur le réseau local, avec ses tests à trois et quatre nœuds :
   présentation, accord, niveau hérité, rien de plus haut, P sans la clé, de proche en
   proche, présentations croisées, rien à moitié.
2. Codé : par Internet, la présentation donne la clé du compagnon et ses indices, et la
   liste blanche est mise à jour sans invitation. Pas encore essayé entre deux réseaux.
3. Fait : retrait, mise de côté et « toujours accepter ce groupe » propagés à toutes mes
   machines.
4. Fait : l'interface (§ 6), essayée sur trois nœuds dans le navigateur.
5. À faire : côté ami, retirer d'un geste toutes les machines d'un même groupe.

## 9. Décisions de l'auteur (18/10/2026)

Les cinq recommandations sont retenues :

1. **De mon côté, présentation d'office** : un ami choisi sur une de mes machines est
   présenté à toutes mes autres machines.
2. **Côté ami, l'accord la première fois**, avec « toujours accepter ce groupe ».
3. **Retirer un ami** le retire de toutes mes machines, sauf « seulement de celle-ci ».
4. **Mise de côté** propagée à toutes mes machines.
5. **Le téléphone est présenté aussi**, mais ne calcule pour un ami qu'en charge et en
   Wi-Fi (`network/worker.py`, `node/android.py`).
