// Réglages : formulaire construit à partir d'une description des champs.
import { put } from '../api.js';
import { $, h, esc } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { toast, errorToast } from '../ui.js';
import { bindGateway, gatewayCard, loadGateway } from './gateway.js';
import { appsCard, bindApps } from './gateway-apps.js';
import { bindEngines, enginesCard, loadEngines } from './engines.js';
import { asDefault, catalog, defaultChoice, modelLabel } from './models.js';
import { bindUpdates, loadUpdates, updatesStatus } from './updates.js';
import { renderApproval } from './approval.js';

const SECTIONS = [
  { title: 'Conversation', icon: 'chat', fields: [
    { key: 'default_model', label: 'Modèle par défaut', type: 'model' },
    { key: 'auto_route_friends', label: 'Mode automatique : faire aussi répondre les machines de mes amis', type: 'switch',
      help: 'Sinon, seules cette machine et tes machines répondent d\'office ; celles de tes amis restent au choix, '
        + 'à côté du modèle. Chez un ami, ta conversation passe par sa machine le temps de la réponse, avec les seuls '
        + 'extraits des bibliothèques partagées avec tes amis.' },
    { key: 'think', label: 'Laisser les modèles réfléchir avant de répondre', type: 'switch',
      help: 'Qwen3 et compagnie : de meilleures réponses, mais plus lentes à démarrer.' },
    { key: 'chat_context', label: 'Contexte des conversations (moteur intégré)', type: 'select', options: [
      ['0', 'Selon la carte graphique (16 384 jetons à partir de 8 Go)'], ['8192', '8 192 jetons'],
      ['16384', '16 384 jetons'], ['32768', '32 768 jetons (plus de mémoire vidéo)']],
      help: 'Ce qu\'un modèle lit d\'un coup : la conversation, les extraits et la réponse. Plus grand, il prend '
        + 'plus de mémoire vidéo ; le moteur repart avec le nouveau réglage. Les applications ne le changent jamais.' },
    { key: 'system_prompt', label: 'Consignes générales données à l\'IA', type: 'textarea' },
  ] },
  { title: 'Connaissances', icon: 'book', fields: [
    { key: 'knowledge_enabled', label: 'Utiliser les bibliothèques par défaut', type: 'switch' },
    { key: 'conversations_enabled', label: 'Mémoire entre les conversations', type: 'switch',
      help: 'L\'IA peut s\'appuyer sur tes autres conversations. Désactive-la pour des échanges isolés.' },
    { key: 'peer_libraries_enabled', label: 'Chercher aussi chez mes machines et mes amis', type: 'switch',
      help: 'Dans les bibliothèques qu\'ils t\'ouvrent (option « Chez les autres » de la conversation) : ta question '
        + 'part chez eux, avec ton accord pour tes amis, et seuls les passages trouvés reviennent.' },
    { key: 'embed_model', label: 'Modèle d\'embedding', type: 'text',
      help: 'embeddinggemma conseillé (à installer : ollama pull embeddinggemma). Changer de modèle relance '
        + 'automatiquement la réindexation des bibliothèques et de la mémoire (voir Tâches).' },
    { key: 'rag_min_score', label: 'Seuil de pertinence des extraits (0 à 1)', type: 'number',
      min: 0, max: 1, step: 0.01,
      help: 'Plus haut : moins d\'extraits mais mieux ciblés. Chaque modèle a son échelle : 0,40 pour '
        + 'embeddinggemma, 0,45 pour bge-m3, 0,70 pour nomic-embed-text. Revient à la valeur conseillée '
        + 'quand tu changes de modèle.' },
  ] },
  { title: 'Recherche web', icon: 'globe', fields: [
    { key: 'web_search_enabled', label: 'Recherche web activée par défaut', type: 'switch',
      help: 'AIODOS cherche quand ta question le demande (« cherche… », actualités, météo…). Une validation '
        + 'est demandée avant d\'envoyer une requête, ou une fois pour la session.' },
    { key: 'web_results', label: 'Pages lues par recherche', type: 'number', min: 1, max: 8 },
  ] },
  { title: 'Cloud', icon: 'cloud', fields: [
    { key: 'cloud_confirm', label: 'Demander une validation avant chaque envoi vers le cloud', type: 'switch',
      help: 'Recommandé : rien ne quitte ta machine sans ton accord.' },
    { key: 'mistral_key', label: 'Clé API Mistral', type: 'secret' },
    { key: 'mistral_cloud_model', label: 'Modèle Mistral', type: 'text' },
    { key: 'qwen_key', label: 'Clé API Qwen (DashScope)', type: 'secret' },
    { key: 'qwen_cloud_model', label: 'Modèle Qwen', type: 'text' },
  ] },
  { title: 'Images, audio et vidéo', icon: 'image', fields: [
    { key: 'media_vision', label: 'Lire les images (texte + description)', type: 'switch' },
    { key: 'media_vision_mode', label: 'Ce qu\'on garde des images', type: 'select', options: [
      ['both', 'Texte + description'], ['ocr', 'Texte seulement (OCR)'], ['describe', 'Description seulement']] },
    { key: 'vision_model', label: 'Modèle de vision', type: 'text' },
    { key: 'media_transcribe', label: 'Transcrire l\'audio et la vidéo (Whisper)', type: 'switch' },
    { key: 'whisper_model', label: 'Modèle Whisper', type: 'select', options: [
      ['tiny', 'tiny (rapide)'], ['base', 'base'], ['small', 'small (recommandé)'],
      ['medium', 'medium'], ['large-v3', 'large-v3 (précis, lent)']] },
  ] },
  { title: 'Internet (compagnon aiodos-net)', icon: 'globe', fields: [
    { key: 'internet_relays', label: 'Relais', type: 'list',
      help: '« n0 » : relais publics de n0, par défaut. Ajoute l\'adresse https://… de ton propre relais, ou de celui '
        + 'd\'un ami (guide : docs/relais.md), avec ou sans « n0 ». « none » : aucun relais, connexion directe seulement.' },
    { key: 'internet_directory', label: 'Annuaires', type: 'list',
      help: '« n0-dns » : DNS de n0 ; « dht » : DHT Mainline de BitTorrent, sans serveur ; « none » : aucun '
        + '(les machines se joignent par les adresses échangées à l\'appairage).' },
    { key: 'internet_portmap', label: 'Ouvrir un port sur la box automatiquement (UPnP, PCP)', type: 'switch',
      help: 'Facilite les connexions directes, sans relais. Sans effet si la box ne le permet pas.' },
    { key: 'p2p_route', label: 'Chemin vers mes machines', type: 'select', options: [
      ['auto', 'Réseau local s\'il répond, Internet sinon'], ['lan', 'Réseau local seulement'],
      ['internet', 'Internet seulement (pour l\'essayer)']] },
    { key: 'internet_max_blob_mb', label: 'Taille maximale d\'un fichier confié par Internet (Mo)', type: 'number',
      min: 1, max: 512, help: 'Au-delà, le fichier reste aux machines du réseau local.' },
  ] },
  { title: 'Gros modèles répartis (llama.cpp)', icon: 'network', fields: [
    { key: 'llamacpp_dir', label: 'Dossier de llama.cpp', type: 'folder',
      help: 'Le dossier qui contient llama-server et ggml-rpc-server, de la même version sur toutes '
        + 'tes machines. Vide : celui livré avec les mises à jour. Le réglage « Prêter ma carte graphique » est '
        + 'dans la vue « Cette machine ».' },
    { key: 'lend_gpu_device', label: 'Carte prêtée', type: 'text',
      help: 'Vide : la carte qui a le plus de mémoire libre. Sinon son nom (« Vulkan1 », « CUDA0 »), '
        + 'ou « CPU » pour prêter la mémoire vive (bien plus lent).' },
  ] },
  { title: 'Images (stable-diffusion.cpp)', icon: 'image', fields: [
    { key: 'sdcpp_dir', label: 'Dossier de stable-diffusion.cpp', type: 'folder',
      help: 'Le dossier qui contient sd-server. Vide : data/engines/stable-diffusion.cpp (il sera bientôt '
        + 'livré avec les mises à jour, comme llama.cpp).' },
    { key: 'images_models_dir', label: 'Dossier des modèles d\'images', type: 'folder',
      help: 'Vide : data/images/models. Les modèles se téléchargent depuis la vue Images.' },
    { key: 'images_other_dirs', label: 'Autres dossiers de modèles sur ce PC', type: 'textarea',
      help: 'Un dossier par ligne. Toujours regardés : celui de ComfyUI (que PANDORA utilise aussi) et celui de '
        + 'minimax-h3-local. Un fichier identique, vérifié par son empreinte, y est repris au lieu d\'être '
        + 'téléchargé, sans prendre de place en plus sur le même disque.' },
    { key: 'images_out_dir', label: 'Dossier des images et vidéos créées', type: 'folder',
      help: 'Vide : Images\\AIODOS (le dossier Images de Windows) pour AIODOS installé. Les images déjà créées '
        + 'restent où elles sont et s’affichent toujours.' },
    { key: 'images_from_friends', label: 'Faire calculer mes images par mes amis qui le proposent', type: 'switch',
      help: 'Quand cette machine ne peut pas calculer un modèle, un ami qui prête sa carte graphique pour les images '
        + 'le fait : ta description et le résultat passent chez lui le temps du calcul, sans y être gardés.' },
    { key: 'images_device', label: 'Carte qui calcule les images', type: 'text',
      help: 'Vide : la meilleure carte graphique (jamais celle intégrée au processeur s\'il y en a une autre). '
        + 'Sinon son nom (« Vulkan1 », « CUDA0 »), ou « CPU ».' },
  ] },
  { title: 'Mises à jour', icon: 'retry', fields: [
    { key: 'auto_update', label: 'Installer automatiquement les nouvelles versions', type: 'switch',
      help: 'Seules les versions signées par l\'auteur d\'AIODOS sont acceptées. Elles arrivent par tes '
        + 'machines appairées, s\'installent à côté de la version actuelle, et AIODOS redémarre dessus quand '
        + 'la machine est libre. Si la nouvelle version ne démarre pas, la précédente reprend la main.' },
    { type: 'updates' },
    { type: 'approval' },
  ] },
  { title: 'Applications et autres outils', icon: 'plug', fields: [
    { type: 'gateway' },
  ] },
  { title: 'Moteurs des applications', icon: 'image', fields: [
    { type: 'engines' },
    { key: 'engines_comfyui_enabled', label: 'Mettre ComfyUI au service de mes applications', type: 'switch',
      help: 'AIODOS enverra au ComfyUI ouvert de ce PC les travaux des applications que tu autorises (PANDORA…), '
        + 'venus de ce PC ou de tes autres machines, '
        + 'en plus des tiens : ils s’ajoutent à sa file sous le nom « aiodos », et seul ce calcul-là est arrêté '
        + 'quand une appli annule. Rien n’est installé ni téléchargé.' },
    { key: 'engines_comfyui_path', label: 'Dossier de ComfyUI', type: 'folder',
      help: 'Vide : trouvé seul (Comfy Desktop, version portable).' },
    { key: 'keep_awake_jobs', label: 'Pas de mise en veille pendant un travail d’application (sur secteur)',
      type: 'switch', help: 'Tant qu’une unité ComfyUI calcule, ici ou ailleurs pour ce PC, Windows ne met pas le PC '
        + 'en veille (l’écran peut s’éteindre). Sur batterie, rien ne change.' },
    { key: 'engines_comfyui_url', label: 'Adresse de ComfyUI sur ce PC', type: 'text',
      help: 'Vide : http://127.0.0.1:8188, puis :8000 (celle de Comfy Desktop). Seule une adresse de ce PC est prise.' },
  ] },
  { title: 'Avancé', icon: 'settings', fields: [
    { key: 'ollama_url', label: 'Adresse d\'Ollama', type: 'text' },
  ] },
];

let root;
let draft = {};

export function mountSettings(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head"><div><h1>Réglages</h1>
        <div class="sub">Enregistrés sur cette machine. Les clés API ne sont jamais réaffichées en clair.</div></div></div>
      <div class="settings" id="settings-form"></div>
    </div></div>`);
  container.append(root);
  // On ne réaffiche pas le formulaire par-dessus des modifications non enregistrées
  const refresh = () => { if (state.view !== 'settings' || !Object.keys(draft).length) render(); };
  on('settings:loaded', refresh);
  on('models:loaded', refresh);
  window.addEventListener('pywebviewready', refresh);     // les boutons « Choisir… » des dossiers
  on('view:shown', view => { if (view === 'settings') { render(); loadUpdates(); loadGateway(); loadEngines(); } });
  on('updates:loaded', () => {
    const box = root && $('#updates-status', root);
    if (box) { box.innerHTML = updatesStatus(); bindUpdates(box); }
  });
  on('gateway:loaded', () => {
    const box = root && $('#gateway-box', root);
    if (box) { box.innerHTML = gatewayCard(); bindGateway(box); }
  });
  on('engines:loaded', () => {
    const box = root && $('#engines-box', root);
    if (box) { box.innerHTML = enginesCard(); bindEngines(box); }
  });
  on('gateway-apps:loaded', () => {
    const box = root && $('#gateway-apps-box', root);
    if (box) { box.innerHTML = appsCard(); bindApps(box); }
  });
}

function field(f) {
  const s = state.settings;
  if (f.type === 'updates') return `<div id="updates-status">${updatesStatus()}</div>`;
  if (f.type === 'gateway') return `<div id="gateway-box">${gatewayCard()}</div>`;
  if (f.type === 'approval') return '<div id="approval-box"></div>';
  if (f.type === 'engines') return `<div id="engines-box">${enginesCard()}</div>`;
  const id = `set-${f.key}`;
  const help = f.help ? `<div class="help">${esc(f.help)}</div>` : '';
  if (f.type === 'switch') {
    return `<div class="setting-row"><div class="txt"><b>${esc(f.label)}</b>${f.help ? `<small>${esc(f.help)}</small>` : ''}</div>
      <label class="switch"><input type="checkbox" id="${id}" data-key="${f.key}" ${s[f.key] ? 'checked' : ''}><span></span></label></div>`;
  }
  let control;
  if (f.type === 'textarea') {
    control = `<textarea class="textarea" id="${id}" data-key="${f.key}" rows="4">${esc(s[f.key])}</textarea>`;
  } else if (f.type === 'select' || f.type === 'model') {
    const current = f.type === 'model' ? asDefault(s[f.key]) : s[f.key];
    const options = f.type === 'model' ? modelOptions(current) : f.options;
    control = `<select class="select" id="${id}" data-key="${f.key}">${options.map(([v, l]) =>
      `<option value="${esc(v)}" ${v === current ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
  } else if (f.type === 'secret') {
    const placeholder = s[`${f.key}_set`] ? `Clé enregistrée (${s[`${f.key}_hint`] || '••••'}) — laisser vide pour la garder` : 'Aucune clé';
    control = `<div style="display:flex;gap:8px"><input class="input" type="password" id="${id}" data-key="${f.key}"
      autocomplete="off" placeholder="${esc(placeholder)}">${s[`${f.key}_set`]
        ? `<button class="btn" data-clear="${f.key}" type="button">Effacer</button>` : ''}</div>`;
  } else if (f.type === 'folder') {       // un dossier : tapé, ou choisi dans la fenêtre d'AIODOS
    control = `<div class="folder-field"><input class="input" type="text" id="${id}" data-key="${f.key}"
      value="${esc(s[f.key])}">${window.pywebview?.api?.pick_folder
        ? `<button class="btn" type="button" data-pick="${id}">${icon('folder')}Choisir…</button>` : ''}</div>`;
  } else if (f.type === 'list') {         // une liste : les valeurs séparées par des virgules
    control = `<input class="input" type="text" id="${id}" data-key="${f.key}" data-list="1"
      value="${esc((s[f.key] || []).join(', '))}">`;
  } else {
    const type = f.type === 'number' ? 'number' : 'text';
    const limits = f.type === 'number' ? `min="${f.min}" max="${f.max}" step="${f.step || 1}"` : '';
    control = `<input class="input" type="${type}" ${limits} id="${id}" data-key="${f.key}" value="${esc(s[f.key])}">`;
  }
  return `<div class="field"><label for="${id}">${esc(f.label)}</label>${control}${help}</div>`;
}

/** Modèles proposés par défaut : chacun une fois, en mode automatique (la machine qui
 *  répond se choisit seule) ; le réglage actuel reste, même s'il désigne une machine. */
function modelOptions(current) {
  const options = catalog().map(e => [defaultChoice(e), e.name]);
  if (current && !options.some(([v]) => v === current)) options.unshift([current, modelLabel(current)]);
  return options;
}

function render() {
  if (!root || !state.settings) return;
  draft = {};
  const form = $('#settings-form', root);
  form.innerHTML = SECTIONS.map(sec => `
    <div class="card"><h2>${icon(sec.icon)}${esc(sec.title)}</h2>${sec.fields.map(field).join('')}</div>`).join('') +
    // N'apparaît qu'avec des modifications à enregistrer : toujours affichée, elle cachait
    // le réglage du dessous en faisant défiler la page
    `<div class="save-bar" hidden><div class="save-box"><span>Modifications non enregistrées</span>
      <button class="btn ghost sm" id="settings-cancel">Annuler</button>
      <button class="btn primary sm" id="settings-save">${icon('check')}Enregistrer</button></div></div>`;
  const bar = $('.save-bar', form);
  form.querySelectorAll('[data-key]').forEach(input => {
    const key = input.dataset.key;
    const read = () => {
      if (input.type === 'checkbox') return input.checked;
      if (input.type === 'number') return Number(input.value);
      if (input.dataset.list) return input.value.split(/[\s,]+/).filter(Boolean);
      return input.value;
    };
    input.addEventListener(input.type === 'checkbox' || input.tagName === 'SELECT' ? 'change' : 'input',
      () => { draft[key] = read(); bar.hidden = false; });
  });
  form.querySelectorAll('[data-clear]').forEach(b => {
    b.onclick = () => { draft[b.dataset.clear] = null; save(); };
  });
  form.querySelectorAll('[data-pick]').forEach(b => {
    b.onclick = async () => {
      let chosen;
      try { chosen = await window.pywebview.api.pick_folder(); } catch (err) { errorToast(err); return; }
      if (!chosen?.[0]) return;
      const input = $(`#${b.dataset.pick}`, form);
      input.value = chosen[0];
      input.dispatchEvent(new Event('input'));            // comme une saisie : à enregistrer
    };
  });
  bindUpdates(form);
  bindGateway(form);
  bindEngines(form);
  renderApproval($('#approval-box', root));
  $('#settings-save', root).onclick = save;
  $('#settings-cancel', root).onclick = render;           // le formulaire revient aux réglages
}

async function save() {
  if (!Object.keys(draft).length) { toast('Aucune modification.'); return; }
  try {
    state.settings = await put('/settings', draft);
    emit('settings:loaded');
    emit('models:reload');
    toast('Réglages enregistrés.', 'ok');
    render();
  } catch (e) { errorToast(e); }
}
