// Réglages : formulaire construit à partir d'une description des champs.
import { put } from '../api.js';
import { $, h, esc } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { toast, errorToast } from '../ui.js';
import { bindUpdates, loadUpdates, updatesStatus } from './updates.js';
import { renderApproval } from './approval.js';

const SECTIONS = [
  { title: 'Conversation', icon: 'chat', fields: [
    { key: 'default_model', label: 'Modèle par défaut', type: 'model' },
    { key: 'think', label: 'Laisser les modèles réfléchir avant de répondre', type: 'switch',
      help: 'Qwen3 et compagnie : de meilleures réponses, mais plus lentes à démarrer.' },
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
      help: 'Une validation est toujours demandée avant d\'envoyer une requête.' },
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
    { key: 'llamacpp_dir', label: 'Dossier de llama.cpp', type: 'text',
      help: 'Le dossier qui contient llama-server et ggml-rpc-server, de la même version sur toutes '
        + 'tes machines. Vide : celui livré avec les mises à jour. Le réglage « Prêter ma carte graphique » est '
        + 'dans la vue « Cette machine ».' },
    { key: 'lend_gpu_device', label: 'Carte prêtée', type: 'text',
      help: 'Vide : la carte qui a le plus de mémoire libre. Sinon son nom (« Vulkan1 », « CUDA0 »), '
        + 'ou « CPU » pour prêter la mémoire vive (bien plus lent).' },
  ] },
  { title: 'Mises à jour', icon: 'retry', fields: [
    { key: 'auto_update', label: 'Installer automatiquement les nouvelles versions', type: 'switch',
      help: 'Seules les versions signées par l\'auteur d\'AIODOS sont acceptées. Elles arrivent par tes '
        + 'machines appairées, s\'installent à côté de la version actuelle, et AIODOS redémarre dessus quand '
        + 'la machine est libre. Si la nouvelle version ne démarre pas, la précédente reprend la main.' },
    { type: 'updates' },
    { type: 'approval' },
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
  on('view:shown', view => { if (view === 'settings') { render(); loadUpdates(); } });
  on('updates:loaded', () => {
    const box = root && $('#updates-status', root);
    if (box) { box.innerHTML = updatesStatus(); bindUpdates(box); }
  });
}

function field(f) {
  const s = state.settings;
  if (f.type === 'updates') return `<div id="updates-status">${updatesStatus()}</div>`;
  if (f.type === 'approval') return '<div id="approval-box"></div>';
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
    const options = f.type === 'model'
      ? state.models.filter(m => m.group === 'local').map(m => [m.id, m.label])
      : f.options;
    control = `<select class="select" id="${id}" data-key="${f.key}">${options.map(([v, l]) =>
      `<option value="${esc(v)}" ${v === s[f.key] ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
  } else if (f.type === 'secret') {
    const placeholder = s[`${f.key}_set`] ? `Clé enregistrée (${s[`${f.key}_hint`] || '••••'}) — laisser vide pour la garder` : 'Aucune clé';
    control = `<div style="display:flex;gap:8px"><input class="input" type="password" id="${id}" data-key="${f.key}"
      autocomplete="off" placeholder="${esc(placeholder)}">${s[`${f.key}_set`]
        ? `<button class="btn" data-clear="${f.key}" type="button">Effacer</button>` : ''}</div>`;
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

function render() {
  if (!root || !state.settings) return;
  draft = {};
  const form = $('#settings-form', root);
  form.innerHTML = SECTIONS.map(sec => `
    <div class="card"><h2>${icon(sec.icon)}${esc(sec.title)}</h2>${sec.fields.map(field).join('')}</div>`).join('') +
    `<div class="save-bar"><button class="btn primary" id="settings-save">${icon('check')}Enregistrer</button></div>`;
  form.querySelectorAll('[data-key]').forEach(input => {
    const key = input.dataset.key;
    const read = () => {
      if (input.type === 'checkbox') return input.checked;
      if (input.type === 'number') return Number(input.value);
      if (input.dataset.list) return input.value.split(/[\s,]+/).filter(Boolean);
      return input.value;
    };
    input.addEventListener(input.type === 'checkbox' || input.tagName === 'SELECT' ? 'change' : 'input',
      () => { draft[key] = read(); });
  });
  form.querySelectorAll('[data-clear]').forEach(b => {
    b.onclick = () => { draft[b.dataset.clear] = null; save(); };
  });
  bindUpdates(form);
  renderApproval($('#approval-box', root));
  $('#settings-save', root).onclick = save;
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
