# Conditions d'utilisation d'AIODOS

*Version 5, 29 octobre 2026*

## 1. Objet

Ces conditions encadrent l'utilisation d'AIODOS : le programme pour Windows, l'appli
Android, leurs mises à jour, et le réseau que forment les machines qui s'en servent. En
installant ou en utilisant AIODOS, tu acceptes ces conditions et la charte d'utilisation. Si
tu ne les acceptes pas, n'installe pas AIODOS, ou désinstalle-le.

## 2. Éditeur et licence

AIODOS est édité par 22ème Arkane et les contributeurs du projet. C'est un logiciel libre et
gratuit, distribué sous la licence Apache, version 2.0 (fichier LICENSE, installé avec
AIODOS). Cette licence régit tes droits sur le code : l'utiliser, le copier, le modifier, le
redistribuer. Ces conditions et la charte encadrent l'usage du logiciel et du réseau ; elles
ne retirent aucun des droits que la licence te donne sur le code. Pour ce qui touche au code,
la licence l'emporte.

## 3. Pas de compte, pas de serveur central

AIODOS ne demande ni compte ni abonnement. L'éditeur n'exploite aucun serveur qui recevrait
tes conversations, tes documents ou tes créations, et AIODOS n'envoie aucune statistique
d'utilisation. Tes données sont enregistrées sur tes appareils, dans le dossier d'AIODOS de
ton profil ; c'est à toi d'en faire des sauvegardes.

## 4. Services de tiers

Selon tes réglages, AIODOS contacte des services qui n'appartiennent pas à l'éditeur et qui
ont leurs propres conditions :

- des **relais Internet** (par défaut, les relais publics de n0, ou celui que tu choisis) et
  des **annuaires** (DNS de n0, DHT Mainline), pour relier tes machines à celles de tes amis
  et du réseau public. Un relais ne voit qu'un trafic chiffré, mais il voit quelles machines
  se parlent, depuis quelles adresses IP, et combien ;
- un **moteur de recherche** (DuckDuckGo) et les pages trouvées, quand la recherche web est
  activée et que tu valides une recherche ;
- les sites d'où viennent les **modèles et les moteurs** (Hugging Face, Ollama…), seulement
  quand tu demandes un téléchargement.

## 5. Le partage de puissance

- Une partie de tes demandes peut être calculée par d'autres machines : les tiennes, celles
  de tes amis, celles du réseau public. Elles voyagent chiffrées, mais la machine qui calcule
  doit les lire pour les traiter. AIODOS est conçu pour ne rien garder chez celle qui
  calcule ; l'éditeur ne peut pas garantir que le programme d'une autre personne n'a pas été
  modifié.
- Dès la première ouverture, et jusqu'à ce que tu le coupes : ta machine rejoint l'annuaire
  du réseau public (elle y annonce sa version d'AIODOS, la liste de ses modèles, si elle
  prête, ses places libres et sa vitesse, rien de personnel), tes conversations peuvent être
  calculées par des inconnus et ta machine calcule les leurs, ta carte graphique crée leurs
  images et leurs vidéos (voir ci-dessous) ainsi que les images que lui demandent les amis
  que tu as ajoutés, tes propres images peuvent être créées par ceux de ces amis qui le
  proposent quand tes machines ne le peuvent pas (leur machine lit alors ta description et
  voit l'image), et les applications de cette machine peuvent te demander l'accès à
  AIODOS (chacune doit être acceptée par toi).
- **Tes conversations chez un inconnu** (« Faire calculer mes conversations par des
  inconnus ») : la machine d'un inconnu du réseau public peut calculer tes conversations.
  Elle lit la conversation (ton texte, le prompt système), mais ne sait pas qui la demande :
  chaque conversation a sa clé jetable, et la demande passe par un relais. AIODOS n'y garde
  rien. Ton texte lui-même peut pourtant révéler qui l'écrit (un nom, une adresse dans ta
  question). Pour garder une conversation chez toi, coupe ce réglage, ou active « Sur mes
  machines seulement » dans cette conversation. Faire répondre un modèle plus gros d'une
  machine du réseau public, ou envoyer à un inconnu les passages de tes documents (tes
  bibliothèques, la mémoire de tes conversations), attend ton accord, demandé une fois et
  modifiable dans les Réglages. Tes pièces jointes, tes fichiers et tes images et vidéos
  déjà créées ne partent jamais chez un inconnu.
- **Tes images et tes vidéos chez un inconnu** : quand ni ta machine ni tes autres machines
  ne peuvent les créer, la machine d'un inconnu du réseau public peut le faire, avec ton
  accord, demandé une fois et modifiable dans les Réglages. Elle lit ta description (jamais
  une image jointe, ni le reste de la conversation) et voit l'image ou la vidéo créée, mais
  ne sait pas qui la demande : chaque création a sa clé jetable, et la demande passe par un
  relais. AIODOS n'y garde rien. Ta description elle-même peut pourtant révéler qui tu es.
  « Sur mes machines seulement » vaut aussi pour les images et les vidéos d'une
  conversation.
- **Les conversations d'inconnus sur ta machine** (« Prêter ma puissance aux conversations
  d'inconnus ») : ta machine les calcule dans la limite de la « Puissance prêtée » (60 % par
  défaut), jamais sur batterie, un téléphone seulement en charge et en Wi-Fi, et après tes
  propres travaux, tes machines et tes amis. Elle ne garde rien, ni la conversation ni qui
  la demande, et ne te la montre pas : tu ne choisis pas et tu ne vois pas ce qu'elle
  calcule. Un inconnu pourrait donc lui demander un texte illicite : ta machine ne ferait
  que faire tourner un modèle public dessus, sans rien en garder. Chacun reste responsable
  de ce qu'il demande (partie 6). Si cela ne te convient pas, coupe ce prêt dans les
  Réglages.
- **Les images et les vidéos d'inconnus sur ta carte graphique** : ce prêt est activé par
  défaut. Ta carte graphique les crée dans les mêmes limites que leurs conversations (la
  « Puissance prêtée », jamais sur batterie, un téléphone seulement en charge et en Wi-Fi,
  après tes propres travaux, tes machines et tes amis) ; une vidéo prend bien plus de temps
  et de place qu'une image. Avant tout calcul, un filtre refuse les descriptions interdites par la
  charte (partie 2), notamment toute image ou vidéo sexuelle impliquant un mineur, réel ou
  fictif, et les images ou vidéos intimes ou sexuelles d'une personne réelle sans son
  accord. Un filtre n'est pas infaillible : une description interdite peut lui échapper. Ta
  machine ne garde ni la description ni ce qu'elle a créé, et ne te les montre pas. Celui
  qui demande reste responsable de ce qu'il demande (partie 6). Tu peux couper ce prêt à
  tout moment dans les Réglages.
- Prêter ta puissance utilise ton processeur, ta carte graphique, ta mémoire, ta batterie et
  ta connexion : de l'électricité, des données, de la chaleur, de l'usure. AIODOS ménage ce
  prêt (priorité basse, pas sur batterie, téléphone seulement en charge) et tu peux l'arrêter
  à tout moment dans les Réglages.
- Le prêt est bénévole. Les comptes d'échanges que montre AIODOS n'ont aucune valeur
  monétaire.

## 6. Les contenus créés

- Les réponses, les images et les vidéos des modèles peuvent être fausses, incomplètes,
  biaisées ou choquantes. Elles ne remplacent pas l'avis d'un professionnel (médecin, avocat,
  conseiller financier…) : vérifie avant de t'y fier.
- Une réponse calculée par la machine d'un inconnu peut être fausse, par erreur ou parce que
  cette machine triche. Elle est marquée « réseau public » dans la conversation ; des
  contrôles au hasard la comparent à celle d'une autre machine, et une machine qui triche
  est mise de côté. Ces contrôles ne portent que sur une partie des réponses.
- Une image ou une vidéo créée par la machine d'un inconnu est marquée comme telle, mais
  elle n'est pas contrôlée ainsi pour l'instant : elle peut ne pas correspondre à ta
  description.
- Tu es seul responsable de ce que tu demandes, y compris aux machines des autres, de ce que
  tu crées et de ce que tu en fais, notamment de sa diffusion. Qu'une machine accepte ta
  demande, après son filtre, ne la rend pas permise.
- L'éditeur ne revendique aucun droit sur ce que tu crées avec AIODOS. Les droits sur une
  création dépendent de la loi qui s'applique et de la licence du modèle utilisé.

## 7. Modèles et logiciels de tiers

Les modèles d'IA ne sont pas fournis par l'éditeur : d'autres les publient, sous leurs
propres licences, dont certaines limitent l'usage commercial ou certains usages. AIODOS
affiche la licence d'un modèle quand elle est restrictive et te demande de la confirmer ; il
t'appartient de la respecter. Il en va de même des moteurs dont AIODOS peut se servir
(llama.cpp, stable-diffusion.cpp, Ollama, ComfyUI…).

## 8. Mises à jour

AIODOS peut se mettre à jour tout seul ; cela se désactive dans les Réglages. Chaque version
est signée par l'éditeur et vérifiée avant d'être installée ; elle peut te parvenir par tes
machines appairées.

## 9. Mineurs

Si tu as moins de 15 ans, ou moins que l'âge fixé par la loi de ton pays pour utiliser seul
un tel service, utilise AIODOS avec l'accord et sous la responsabilité d'un parent ou d'un
responsable légal.

## 10. Sans garantie

AIODOS est fourni « en l'état », sans garantie d'aucune sorte, comme le prévoit sa licence.
C'est un logiciel en développement : il peut contenir des erreurs, perdre des données ou
s'arrêter. Le réseau dépend des machines de chacun et de services de tiers : il peut ne pas
être disponible. Garde des sauvegardes de ce qui compte.

## 11. Responsabilité

Dans les limites permises par la loi, l'éditeur et les contributeurs ne sont pas
responsables des dommages qui résulteraient de l'utilisation d'AIODOS ou de l'impossibilité
de l'utiliser, des contenus créés, du comportement des autres participants ou des services
de tiers. Ces conditions ne limitent ni une responsabilité que la loi interdit de limiter,
ni les droits que la loi te donne si tu es consommateur. Tu réponds de l'usage que tu fais
d'AIODOS, y compris de ce que font les applications que tu y relies.

## 12. Arrêter

Tu peux cesser d'utiliser AIODOS à tout moment : le désinstaller (en gardant tes données ou
en les supprimant), quitter le réseau public, retirer tes amis. Les autres participants
peuvent eux aussi cesser de travailler avec ta machine, notamment si tu ne respectes pas la
charte.

## 13. Modifications

Ces conditions et la charte peuvent changer. Chaque nouvelle version porte un numéro et une
date ; AIODOS te la montre et te demande de l'accepter pour continuer à s'en servir.

## 14. Droit applicable

Ces conditions sont soumises au droit français. En cas de litige, une solution amiable est
cherchée d'abord ; à défaut, les tribunaux français sont compétents, sous réserve des règles
qui te protègent dans ton pays si tu es consommateur. La version française de ces textes
fait foi.

## 15. Contact

Questions, signalements, failles de sécurité : l'équipe du projet, sur son dépôt :
github.com/AIODOS/aiodos.
