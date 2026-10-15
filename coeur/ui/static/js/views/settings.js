// Réglages : formulaire construit à partir d'une description des champs.
import { put } from '../api.js';
import { $, h, esc } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { toast, errorToast } from '../ui.js';

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
    { key: 'embed_model', label: 'Modèle d\'embedding', type: 'text',
      help: 'Changer de modèle impose de réindexer les bibliothèques.' },
    { key: 'rag_min_score', label: 'Seuil de pertinence des extraits (0 à 1)', type: 'number',
      min: 0, max: 1, step: 0.01,
      help: 'Plus haut : moins d\'extraits mais mieux ciblés. 0,70 convient à nomic-embed-text.' },
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
  on('view:shown', view => { if (view === 'settings') render(); });
}

function field(f) {
  const s = state.settings;
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
      return input.value;
    };
    input.addEventListener(input.type === 'checkbox' || input.tagName === 'SELECT' ? 'change' : 'input',
      () => { draft[key] = read(); });
  });
  form.querySelectorAll('[data-clear]').forEach(b => {
    b.onclick = () => { draft[b.dataset.clear] = null; save(); };
  });
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
