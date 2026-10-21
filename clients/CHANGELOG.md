# aiodos-client : versions

<!-- SPDX-License-Identifier: Apache-2.0 -->

Les deux bibliothèques (Python, JavaScript) portent toujours le même numéro.

## 0.2.0 (21/10/2026)

Pour AIODOS 2.1 avec les travaux ComfyUI des applications (livraison L2b) : la surface présente
depuis 0.1.0 sert maintenant, quand `features` annonce `engines`, `engines.comfyui`, `jobs`,
`jobs.pause`, `inputs` et `files` ; sinon `NotSupported`, sans rien essayer, comme avant. Les
noms sont ceux du client Python ; le client JavaScript a la même surface en camelCase, avec
des promesses et `signal` (AbortSignal) à la place de `cancel`.
- moteurs : `engines()`, le ComfyUI de ce PC et de mes machines, et leurs modèles ;
- entrées par empreinte : `inputs.put(data, type)` (`HEAD`, puis `PUT` si AIODOS ne l'a pas ;
  100 Mo au plus, vérifiés avant l'envoi : `InputTooLarge`) ;
- ComfyUI : `comfy.tokenize(graph, {nœud: octets})` (les jetons `@in:` dans `LoadImage`,
  `LoadImageMask`, `LoadVideo`, `LoadAudio`) ; `comfy.plan()` (le pré-vol) ;
  `comfy.submit()` (empreintes calculées, entrées manquantes envoyées, puis le travail ; le
  même `client_ref` — JS : `clientRef` — rend le même travail ; `where` par `X-Aiodos-Where` ;
  `sensitive` : ce PC et mes machines, dans ce que l'accord permet) ; `comfy.run()` (dépôt,
  attente, téléchargement) ;
- travaux : `jobs(active, client_ref, status)`, `job(id)` ; `Job` porte l'objet travail
  (`status`, `progress`, `units`, `files`…) et `refresh()`, `watch()` (événements `job`,
  `file`, `end`, et `unreachable` quand AIODOS ne répond plus ; reconnexion seule, l'état
  complet relu ; l'arrêt demandé par l'appli annule le travail dans AIODOS ; JS : itérateur
  asynchrone), `wait()`, `pause(when)`, `resume()`, `cancel()`, `retry_failed()` (JS :
  `retryFailed()`), `delete(files)`, `files(unfetched)`, `download(dest_dir, stem, on_file)`
  (JS : `download(destDir, {stem, onFile})`) ;
- fichiers : `File` (la fiche, `content()`, `save(path)` en flux, repris par `Range` après
  une coupure, l'empreinte SHA-256 vérifiée avant de renommer, `delete()`) ; un fichier servi
  en entier est récupéré pour AIODOS ;
- erreurs : `GraphInvalid` (`graph_invalid`, `graph_not_api`, `patch_invalid`),
  `ForbiddenValue`, `ForbiddenNode`, `InputMissing`, `InputHashMismatch`, `InputTooLarge`,
  `GraphTooLarge`, `JobNotFound`, `FileNotFound`, `InputNotFound`, `EngineNotFound`,
  `ModelUnavailable` (`models`, `machine`), `ModelUnresolved`, `ModelAmbiguous`,
  `JobNotCancellable`, `JobNotPausable`, `QuotaExceeded` ; `EngineUnavailable` vaut aussi
  pour 409 ; messages en français et en anglais ;
- faux serveurs : les routes des travaux, en mémoire (entrées par appli, travaux qui avancent
  seuls ou à la demande, flux SSE, fichiers par plages, commandes, idempotence, isolation
  entre applis, mes autres machines), réglables pour les essais (échec d'une unité, lenteur,
  flux ou téléchargement coupé) ; ils annoncent maintenant les travaux (`L1_FEATURES` pour
  imiter une version d'AIODOS qui ne les sert pas) ;
- pas encore servis, `NotSupported` : les recettes (L4), l'`object_info` et les gabarits
  relayés (L2c), les liens signés (`File.link()`), la priorité d'un travail déposé et les
  signalements.

## 0.1.0 (21/10/2026)

Première version, pour AIODOS 2.1 (livraison L1 « branchable, texte ») :
- découverte d'AIODOS (fichier gateway.json, installation), états vus par l'appli ;
- accès par appli : demande (RFC 8628), code à 6 chiffres, relève de la clé, droits en plus,
  retrait ; connexion quand la passerelle est fermée, par le programme d'aide d'AIODOS ;
- preuve d'identité d'AIODOS avant d'envoyer la clé ; hôtes locaux seulement ;
- catalogue des modèles (avec le modèle qu'AIODOS prend lui-même, `default`, préféré sans
  autre choix), conversation en flux (outils, images, réflexion), embeddings (rendus avec le
  modèle qui les a calculés) ;
- erreurs à codes stables, messages en français et en anglais ;
- faux serveurs pour les essais des applis ;
- surface des travaux de moteur (ComfyUI…), de la file et des fichiers : présente, lève
  NotSupported jusqu'à la livraison L2.
