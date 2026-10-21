// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — les textes des états et des erreurs, en français (défaut) et en anglais.
//
// Les applis affichent ces textes tels quels, ou les traduisent d'après le `state` ou le
// `code` (jamais d'après le texte). Les textes des états sont ceux conseillés par
// docs/api-applications.md § 6.

/** Messages des états de status(). */
const STATES = {
  fr: {
    not_installed: "AIODOS n'est pas installé sur ce PC.",
    not_running: "AIODOS n'est pas lancé.",
    gateway_closed: "AIODOS tourne, mais n'est pas ouvert aux applications.",
    gateway_error: "La passerelle d'AIODOS est en erreur.",
    apps_closed: "AIODOS n'est ouvert qu'aux autres outils.",
    not_aiodos: 'Le port est pris par un autre programme.',
    too_old: 'Mets AIODOS à jour.',
    untrusted: "Le programme qui répond ne connaît pas la clé : elle n'a pas été envoyée.",
    headless: "AIODOS tourne sans fenêtre : ouvre-le pour autoriser l'appli.",
    requests_closed: "AIODOS n'accepte pas de nouvelle application en ce moment.",
    needs_access: "L'appli n'a pas encore accès à AIODOS.",
    pending: "Autorise l'appli dans la fenêtre d'AIODOS. Code : {code}",
    denied: "L'accès a été refusé dans AIODOS.",
    revoked: "L'accès a été retiré (ou a expiré) dans AIODOS.",
    other_account: 'AIODOS tourne sous un autre compte Windows.',
    ready: 'Connecté à AIODOS.',
  },
  en: {
    not_installed: 'AIODOS is not installed on this PC.',
    not_running: 'AIODOS is not running.',
    gateway_closed: 'AIODOS is running, but is not open to applications.',
    gateway_error: "AIODOS's gateway reports an error.",
    apps_closed: 'AIODOS is only open to other tools.',
    not_aiodos: 'The port is taken by another program.',
    too_old: 'Update AIODOS.',
    untrusted: 'The program that answers does not know the key: it was not sent.',
    headless: 'AIODOS is running without a window: open it to authorize the app.',
    requests_closed: 'AIODOS is not accepting new applications right now.',
    needs_access: 'The app does not have access to AIODOS yet.',
    pending: 'Authorize the app in the AIODOS window. Code: {code}',
    denied: 'Access was denied in AIODOS.',
    revoked: 'Access was removed (or has expired) in AIODOS.',
    other_account: 'AIODOS is running under another Windows account.',
    ready: 'Connected to AIODOS.',
  },
};

/**
 * Messages des codes d'erreur. Ceux du serveur ne servent que si sa réponse n'a pas de
 * message ; ceux du client (unreachable, cancelled…) sont toujours les nôtres.
 */
const CODES = {
  fr: {
    // propres au client
    unreachable: "Connexion refusée ({host}) : AIODOS n'est pas lancé, ou n'est pas ouvert aux applications.",
    connection_lost: 'Connexion refusée ou coupée ({host}) : AIODOS ne répond plus.',
    gateway_closed: "AIODOS tourne, mais n'est pas ouvert aux applications.",
    not_aiodos: "Le port {host} est pris par un autre programme qu'AIODOS.",
    too_old: "Cette version d'AIODOS est trop ancienne pour les applications : mets AIODOS à jour.",
    untrusted:
      "Le programme qui répond sur {host} ne connaît pas la clé de l'appli : elle n'a pas été envoyée. Vérifie qu'AIODOS est bien lancé.",
    non_local_url: "Adresse refusée ({url}) : AIODOS ne s'appelle que sur ce PC (127.0.0.1, localhost ou [::1]).",
    proxy:
      "Un proxy est réglé pour les requêtes de Node (NODE_USE_ENV_PROXY, --use-env-proxy ou un répartiteur à proxy) : la clé passerait par lui. Ajoute 127.0.0.1 à NO_PROXY.",
    redirect: "Réponse refusée : AIODOS ne redirige jamais, un autre programme répond sur {host}.",
    timeout: 'Délai dépassé : AIODOS ne répond pas.',
    cancelled: 'Arrêté',
    protocol: "Réponse incomprise d'AIODOS.",
    cut: "La réponse d'AIODOS s'est arrêtée avant la fin.",
    not_installed: "AIODOS n'est pas installé sur ce PC.",
    dev_copy: "Copie de développement d'AIODOS ({path}) sans interpréteur : donne-le par AIODOS_PYTHON.",
    program_missing: "Programme d'AIODOS introuvable : {path}",
    not_running: "AIODOS n'est pas lancé.",
    needs_access: "L'appli n'a pas encore accès à AIODOS : connecte-la d'abord.",
    bad_key: "La clé rangée n'a pas la forme d'une clé d'application d'AIODOS : elle n'a pas été envoyée.",
    access_denied: "L'accès a été refusé dans AIODOS.",
    expired_token: "La demande d'accès a expiré : recommence.",
    invalid_grant: "Demande d'accès inconnue, ou clé déjà relevée : recommence.",
    not_supported: "Cette version d'AIODOS ne sert pas encore « {feature} » : mets AIODOS à jour.",
    client_too_old:
      "AIODOS sert « {feature} », mais aiodos-client {version} ne sait pas encore s'en servir : mets à jour aiodos-client.",
    input_size: "Entrée trop lourde ({size} Mo) : 100 Mo au plus. Rien n'a été envoyé.",
    job_failed: 'Le travail a échoué : {reason}',
    job_cancelled: 'Le travail a été annulé dans AIODOS.',
    file_hash: "Le fichier reçu ne correspond pas à son empreinte : il n'a pas été gardé.",
    file_size: "AIODOS a envoyé plus d'octets que le fichier n'en a : il n'a pas été gardé.",
    // du serveur (messages de secours)
    host_refused: 'Hôte refusé par la passerelle.',
    invalid_request: 'Demande invalide.',
    context_length_exceeded: 'La demande dépasse le contexte du modèle.',
    tools_unavailable: 'Aucune machine permise ne reçoit les outils pour ce modèle.',
    vision_unsupported: 'Ce modèle ne voit pas les images.',
    local_only: "Cette demande ne se calcule que sur ce PC, qui n'a pas le modèle.",
    unsupported_parameter: "Paramètre qu'AIODOS ne traite pas.",
    invalid_api_key: "Clé d'accès d'AIODOS manquante ou invalide.",
    key_revoked: "L'accès de l'appli a été retiré dans AIODOS.",
    key_expired: "L'accès de l'appli a expiré dans AIODOS.",
    scope_denied: "L'appli n'a pas ce droit dans AIODOS.",
    where_denied: "Lieu de calcul plus large que l'accord.",
    apps_closed: "AIODOS n'est ouvert qu'aux autres outils.",
    requests_closed: "AIODOS n'accepte pas de nouvelle application en ce moment.",
    blocked_program: 'Ce programme est bloqué dans AIODOS.',
    other_account: 'AIODOS tourne sous un autre compte Windows.',
    program_mismatch: 'Cette clé est liée à un autre programme.',
    origin_refused: "La passerelle d'AIODOS ne répond pas aux pages web.",
    model_not_found: 'Modèle inconnu.',
    no_machine: "Aucune machine permise n'a ce modèle en ce moment.",
    file_too_large: 'Demande trop grosse.',
    unsupported_media_type: "Genre de contenu refusé (corps non JSON, ou entrée d'un genre non reconnu).",
    rate_limited: 'Trop de demandes : réessaie dans un moment.',
    busy: 'AIODOS est occupé : réessaie dans un moment.',
    peer_failed: "La machine choisie n'a pas pu répondre.",
    engine_failed: "Le moteur n'a pas pu répondre.",
    engine_unavailable: "Le moteur n'est pas prêt : en panne, en démarrage, ou sur aucune machine permise.",
    approval_unavailable: "AIODOS tourne sans fenêtre : ouvre-le pour autoriser l'appli.",
    graph_not_api: "Graphe au format de l'éditeur : il faut le format API (« Exporter (API) » dans ComfyUI).",
    graph_invalid: 'Graphe mal formé.',
    patch_invalid: 'Retouche invalide : elle remplace une valeur déjà présente, du même type, jamais un lien.',
    graph_too_large: 'Graphe trop gros (2 Mo par graphe, 8 Mo et 64 graphes par travail).',
    forbidden_value: 'Valeur refusée (chemin, adresse ou annotation de dossier).',
    forbidden_node: "Nœud refusé : il appelle un service en ligne.",
    input_missing: 'Entrée absente : jamais envoyée par cette appli, ou non déclarée.',
    input_hash_mismatch: "L'empreinte du fichier reçu ne correspond pas à celle de l'adresse.",
    input_too_large: 'Entrée trop lourde (100 Mo au plus).',
    engine_not_found: 'Moteur inconnu.',
    input_not_found: 'Entrée introuvable.',
    job_not_found: 'Travail introuvable.',
    file_not_found: 'Fichier introuvable.',
    model_unavailable: "Aucune machine permise n'a ces modèles.",
    model_unresolved: 'Modèle non résolu : déclare-le dans « models ».',
    model_ambiguous: 'Même nom de modèle dans plusieurs dossiers.',
    job_not_cancellable: 'Ce travail est déjà fini.',
    job_not_pausable: 'Ce travail est déjà fini.',
    quota_exceeded: 'Quota de cette application dépassé.',
    http: "Réponse inattendue d'AIODOS (HTTP {status}).",
  },
  en: {
    unreachable: 'Connection refused ({host}): AIODOS is not running, or is not open to applications.',
    connection_lost: 'Connection refused or lost ({host}): AIODOS no longer answers.',
    gateway_closed: 'AIODOS is running, but is not open to applications.',
    not_aiodos: 'Port {host} is taken by a program other than AIODOS.',
    too_old: 'This version of AIODOS is too old for applications: update AIODOS.',
    untrusted:
      "The program that answers on {host} does not know the app's key: it was not sent. Check that AIODOS is running.",
    non_local_url: 'Address refused ({url}): AIODOS is only called on this PC (127.0.0.1, localhost or [::1]).',
    proxy:
      "A proxy is set for Node's requests (NODE_USE_ENV_PROXY, --use-env-proxy or a proxy dispatcher): the key would go through it. Add 127.0.0.1 to NO_PROXY.",
    redirect: 'Response refused: AIODOS never redirects, another program answers on {host}.',
    timeout: 'Timed out: AIODOS does not answer.',
    cancelled: 'Stopped',
    protocol: 'Response from AIODOS not understood.',
    cut: "AIODOS's response stopped before the end.",
    not_installed: 'AIODOS is not installed on this PC.',
    dev_copy: 'Development copy of AIODOS ({path}) without an interpreter: set AIODOS_PYTHON.',
    program_missing: 'AIODOS program not found: {path}',
    not_running: 'AIODOS is not running.',
    needs_access: 'The app does not have access to AIODOS yet: connect it first.',
    bad_key: 'The stored key does not look like an AIODOS application key: it was not sent.',
    access_denied: 'Access was denied in AIODOS.',
    expired_token: 'The access request has expired: start again.',
    invalid_grant: 'Unknown access request, or key already collected: start again.',
    not_supported: 'This version of AIODOS does not serve “{feature}” yet: update AIODOS.',
    client_too_old: 'AIODOS serves “{feature}”, but aiodos-client {version} cannot use it yet: update aiodos-client.',
    input_size: 'Input too large ({size} MB): 100 MB at most. Nothing was sent.',
    job_failed: 'The job failed: {reason}',
    job_cancelled: 'The job was cancelled in AIODOS.',
    file_hash: 'The received file does not match its hash: it was not kept.',
    file_size: 'AIODOS sent more bytes than the file has: it was not kept.',
    host_refused: 'Host refused by the gateway.',
    invalid_request: 'Invalid request.',
    context_length_exceeded: "The request exceeds the model's context.",
    tools_unavailable: 'No allowed machine takes tools for this model.',
    vision_unsupported: 'This model does not see images.',
    local_only: 'This request is only computed on this PC, which does not have the model.',
    unsupported_parameter: 'Parameter not handled by AIODOS.',
    invalid_api_key: 'AIODOS access key missing or invalid.',
    key_revoked: "The app's access was removed in AIODOS.",
    key_expired: "The app's access has expired in AIODOS.",
    scope_denied: 'The app does not have this right in AIODOS.',
    where_denied: 'Place of computation wider than the agreement.',
    apps_closed: 'AIODOS is only open to other tools.',
    requests_closed: 'AIODOS is not accepting new applications right now.',
    blocked_program: 'This program is blocked in AIODOS.',
    other_account: 'AIODOS is running under another Windows account.',
    program_mismatch: 'This key is bound to another program.',
    origin_refused: "AIODOS's gateway does not answer web pages.",
    model_not_found: 'Unknown model.',
    no_machine: 'No allowed machine has this model right now.',
    file_too_large: 'Request too large.',
    unsupported_media_type: 'Content type refused (body not JSON, or input of an unknown kind).',
    rate_limited: 'Too many requests: try again in a moment.',
    busy: 'AIODOS is busy: try again in a moment.',
    peer_failed: 'The chosen machine could not answer.',
    engine_failed: 'The engine could not answer.',
    engine_unavailable: 'The engine is not ready: down, starting, or on no allowed machine.',
    approval_unavailable: 'AIODOS is running without a window: open it to authorize the app.',
    graph_not_api: 'Editor-format graph: the API format is required (“Export (API)” in ComfyUI).',
    graph_invalid: 'Malformed graph.',
    patch_invalid: 'Invalid patch: it replaces an existing value of the same type, never a link.',
    graph_too_large: 'Graph too large (2 MB per graph, 8 MB and 64 graphs per job).',
    forbidden_value: 'Value refused (path, address or folder annotation).',
    forbidden_node: 'Node refused: it calls an online service.',
    input_missing: 'Missing input: never uploaded by this app, or not declared.',
    input_hash_mismatch: "The received file's hash does not match the address.",
    input_too_large: 'Input too large (100 MB at most).',
    engine_not_found: 'Unknown engine.',
    input_not_found: 'Input not found.',
    job_not_found: 'Job not found.',
    file_not_found: 'File not found.',
    model_unavailable: 'No allowed machine has these models.',
    model_unresolved: 'Unresolved model: declare it in “models”.',
    model_ambiguous: 'Same model name in several folders.',
    job_not_cancellable: 'This job has already finished.',
    job_not_pausable: 'This job has already finished.',
    quota_exceeded: "This application's quota is exceeded.",
    http: 'Unexpected response from AIODOS (HTTP {status}).',
  },
};

/** Conseils donnés avec les erreurs propres au client. */
const HINTS = {
  fr: {
    unreachable: "Lance AIODOS, ou connecte l'appli : elle l'ouvrira aux applications.",
    connection_lost: "Vérifie qu'AIODOS tourne encore, puis réessaie.",
    gateway_closed: "Connecte l'appli, ou ouvre Réglages → Applications et autres outils dans AIODOS.",
    not_aiodos: "Libère le port, ou change celui d'AIODOS dans ses réglages.",
    too_old: 'Mets AIODOS à jour.',
    untrusted: "Vérifie qu'AIODOS est bien lancé, puis reconnecte l'appli si besoin.",
    non_local_url: "Donne l'adresse de la passerelle sur ce PC, par exemple http://127.0.0.1:11435.",
    proxy: 'Ajoute 127.0.0.1 à NO_PROXY, ou coupe le proxy de Node pour ce programme.',
    redirect: "Vérifie quel programme écoute sur ce port.",
    timeout: 'Réessaie, ou choisis un modèle plus léger.',
    not_installed: 'Installe AIODOS depuis sa page de projet.',
    dev_copy: "Donne l'interpréteur Python par AIODOS_PYTHON, ou lance AIODOS toi-même.",
    needs_access: "Connecte l'appli à AIODOS, après un clic de l'utilisateur.",
    bad_key: "Reconnecte l'appli à AIODOS.",
    access_denied: "Redemande l'accès, après un clic de l'utilisateur.",
    expired_token: "Redemande l'accès, après un clic de l'utilisateur.",
    invalid_grant: "Redemande l'accès, après un clic de l'utilisateur.",
    not_supported: 'Mets AIODOS à jour.',
    client_too_old: 'Mets à jour aiodos-client.',
    input_size: 'Envoie un fichier plus léger (un extrait de la vidéo, une image plus petite).',
    job_failed: "Regarde l'erreur de chaque unité (detail.units), puis relance-les (retryFailed).",
    job_cancelled: 'Redépose le travail si besoin.',
    file_hash: 'Réessaie le téléchargement.',
    file_size: 'Réessaie le téléchargement.',
  },
  en: {
    unreachable: 'Start AIODOS, or connect the app: it will open AIODOS to applications.',
    connection_lost: 'Check that AIODOS is still running, then try again.',
    gateway_closed: 'Connect the app, or open Settings → Applications and other tools in AIODOS.',
    not_aiodos: "Free the port, or change AIODOS's port in its settings.",
    too_old: 'Update AIODOS.',
    untrusted: 'Check that AIODOS is running, then reconnect the app if needed.',
    non_local_url: "Give the gateway's address on this PC, for example http://127.0.0.1:11435.",
    proxy: "Add 127.0.0.1 to NO_PROXY, or turn off Node's proxy for this program.",
    redirect: 'Check which program listens on this port.',
    timeout: 'Try again, or pick a lighter model.',
    not_installed: 'Install AIODOS from its project page.',
    dev_copy: 'Set the Python interpreter with AIODOS_PYTHON, or start AIODOS yourself.',
    needs_access: 'Connect the app to AIODOS, after a click from the user.',
    bad_key: 'Reconnect the app to AIODOS.',
    access_denied: 'Ask for access again, after a click from the user.',
    expired_token: 'Ask for access again, after a click from the user.',
    invalid_grant: 'Ask for access again, after a click from the user.',
    not_supported: 'Update AIODOS.',
    client_too_old: 'Update aiodos-client.',
    input_size: 'Send a lighter file (a clip of the video, a smaller image).',
    job_failed: "Look at each unit's error (detail.units), then retry them (retryFailed).",
    job_cancelled: 'Submit the job again if needed.',
    file_hash: 'Try the download again.',
    file_size: 'Try the download again.',
  },
};

/** « fr » ou « en » (tout ce qui commence par « en » est l'anglais ; le reste, le français). */
export function normalizeLang(lang) {
  return typeof lang === 'string' && /^en\b/i.test(lang.trim()) ? 'en' : 'fr';
}

function fill(template, vars) {
  if (typeof template !== 'string') return null;
  return template.replace(/\{(\w+)\}/g, (_, name) => (vars && vars[name] != null ? String(vars[name]) : ''));
}

/** Le message d'un état de status(). */
export function stateText(state, lang = 'fr', vars = {}) {
  const l = normalizeLang(lang);
  return fill(STATES[l][state], vars) ?? fill(STATES.fr[state], vars) ?? String(state);
}

/** Le message d'un code d'erreur (ceux du client, ou un message de secours pour ceux du serveur). */
export function codeText(code, lang = 'fr', vars = {}) {
  const l = normalizeLang(lang);
  return fill(CODES[l][code], vars) ?? fill(CODES.fr[code], vars) ?? null;
}

/** Le conseil donné avec un code d'erreur propre au client, ou null. */
export function hintText(code, lang = 'fr', vars = {}) {
  const l = normalizeLang(lang);
  return fill(HINTS[l][code], vars) ?? null;
}

for (const table of [STATES, CODES, HINTS]) {
  for (const l of Object.keys(table)) Object.freeze(table[l]);
  Object.freeze(table);
}

/** Tous les textes, pour les applis qui les inscrivent dans leurs propres traductions. */
export const texts = Object.freeze({
  states: STATES,
  codes: CODES,
  hints: HINTS,
  state: stateText,
  code: codeText,
  hint: hintText,
  lang: normalizeLang,
});
