// Réglages : la langue en tête, puis des sections repliables (js/fold.js), dans l'ordre voulu
// par l'auteur (27/10/2026) : Cette machine (views/node.js), Conversation, Modèles,
// Connaissances, puis le reste. Le formulaire est construit à partir d'une description des
// champs ; une section repliée ne montre que son titre et un mot de son état.
import { put } from '../api.js';
import { $, h, esc } from '../dom.js';
import { fold, keepFolds, openFold } from '../fold.js';
import { autoLanguage, lang, t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { toast, errorToast } from '../ui.js';
import { bindGateway, gatewayCard, loadGateway } from './gateway.js';
import { appsCard, bindApps } from './gateway-apps.js';
import { loadEmbed } from './embed-model.js';
import { bindEngines, enginesCard, loadEngines } from './engines.js';
import { asDefault, catalog, defaultChoice, modelLabel } from './models.js';
import { bindUpdates, loadUpdates, updatesStatus } from './updates.js';
import { renderApproval } from './approval.js';
import { modelsCard, bindModels, loadImageModels, mountModels } from './settings-models.js';
import { mountNode, refreshNode } from './node.js';
import { mountAbout } from './legal.js';

// Titres, libellés, aides et choix sont des clés de textes (i18n/*/settings.json), traduites
// à l'affichage. Le choix d'une liste : [valeur, clé de son libellé].
const SECTIONS = [
  { id: 'conversation', title: 'settings.section_conversation', icon: 'chat',
    sub: () => modelLabel(asDefault(state.settings.default_model)), fields: [
    { key: 'default_model', label: 'settings.default_model', type: 'model' },
    { key: 'auto_route_friends', label: 'settings.auto_route_friends', type: 'switch',
      help: 'settings.auto_route_friends_help' },
    { key: 'think', label: 'settings.think', type: 'switch', help: 'settings.think_help' },
    { key: 'chat_context', label: 'settings.chat_context', type: 'select', options: [
      ['0', 'settings.chat_context_auto'], ['8192', 'settings.chat_context_8k'],
      ['16384', 'settings.chat_context_16k'], ['32768', 'settings.chat_context_32k']],
      help: 'settings.chat_context_help' },
    { key: 'system_prompt', label: 'settings.system_prompt', type: 'textarea' },
    { type: 'subhead', label: 'settings.section_web' },
    { key: 'web_search_enabled', label: 'settings.web_search_enabled', type: 'switch',
      help: 'settings.web_search_enabled_help' },
    { key: 'web_results', label: 'settings.web_results', type: 'number', min: 1, max: 8 },
  ] },
  // Les modèles : ceux des discussions, puis ceux des images et des vidéos (settings-models.js),
  // du plus puissant au moins puissant ; l'onglet Images y renvoie (« settings:focus »)
  { id: 'modeles', title: 'settings.section_models', icon: 'layers', fields: [
    { type: 'models' },
    { type: 'subhead', label: 'settings.models_images' },
    { key: 'images_enrich', label: 'settings.images_enrich', type: 'switch', help: 'settings.images_enrich_help' },
    { key: 'images_from_friends', label: 'settings.images_from_friends', type: 'switch',
      help: 'settings.images_from_friends_help' },
    { key: 'images_device', label: 'settings.images_device', type: 'text', help: 'settings.images_device_help' },
    { key: 'images_models_dir', label: 'settings.images_models_dir', type: 'folder',
      help: 'settings.images_models_dir_help' },
    { key: 'images_other_dirs', label: 'settings.images_other_dirs', type: 'textarea',
      help: 'settings.images_other_dirs_help' },
    { key: 'images_out_dir', label: 'settings.images_out_dir', type: 'folder',
      help: 'settings.images_out_dir_help' },
    { key: 'sdcpp_dir', label: 'settings.sdcpp_dir', type: 'folder', help: 'settings.sdcpp_dir_help' },
    { type: 'subhead', label: 'settings.section_cluster' },
    { key: 'llamacpp_dir', label: 'settings.llamacpp_dir', type: 'folder', help: 'settings.llamacpp_dir_help' },
    { key: 'lend_gpu_device', label: 'settings.lend_gpu_device', type: 'text',
      help: 'settings.lend_gpu_device_help' },
  ] },
  { id: 'knowledge', title: 'settings.section_knowledge', icon: 'book',
    sub: () => state.settings.embed_model || '', fields: [
    { key: 'knowledge_enabled', label: 'settings.knowledge_enabled', type: 'switch' },
    { key: 'conversations_enabled', label: 'settings.conversations_enabled', type: 'switch',
      help: 'settings.conversations_enabled_help' },
    { key: 'peer_libraries_enabled', label: 'settings.peer_libraries_enabled', type: 'switch',
      help: 'settings.peer_libraries_enabled_help' },
    { key: 'embed_model', label: 'settings.embed_model', type: 'embed', help: 'settings.embed_model_help' },
    { key: 'rag_min_score', label: 'settings.rag_min_score', type: 'number',
      min: 0, max: 1, step: 0.01, help: 'settings.rag_min_score_help' },
    { type: 'subhead', label: 'settings.section_media' },
    { key: 'media_vision', label: 'settings.media_vision', type: 'switch' },
    { key: 'media_vision_mode', label: 'settings.media_vision_mode', type: 'select', options: [
      ['both', 'settings.vision_mode_both'], ['ocr', 'settings.vision_mode_ocr'],
      ['describe', 'settings.vision_mode_describe']] },
    { key: 'vision_model', label: 'settings.vision_model', type: 'text' },
    { key: 'media_transcribe', label: 'settings.media_transcribe', type: 'switch' },
    { key: 'whisper_model', label: 'settings.whisper_model', type: 'select', options: [
      ['tiny', 'settings.whisper_tiny'], ['base', 'settings.whisper_base'],
      ['small', 'settings.whisper_small'], ['medium', 'settings.whisper_medium'],
      ['large-v3', 'settings.whisper_large_v3']] },
  ] },
  { id: 'internet', title: 'settings.section_internet', icon: 'globe', fields: [
    { key: 'internet_relays', label: 'settings.internet_relays', type: 'list',
      help: 'settings.internet_relays_help' },
    { key: 'internet_directory', label: 'settings.internet_directory', type: 'list',
      help: 'settings.internet_directory_help' },
    { key: 'internet_portmap', label: 'settings.internet_portmap', type: 'switch',
      help: 'settings.internet_portmap_help' },
    { key: 'p2p_route', label: 'settings.p2p_route', type: 'select', options: [
      ['auto', 'settings.route_auto'], ['lan', 'settings.route_lan'], ['internet', 'settings.route_internet']] },
    { key: 'internet_max_blob_mb', label: 'settings.internet_max_blob_mb', type: 'number',
      min: 1, max: 512, help: 'settings.internet_max_blob_mb_help' },
  ] },
  { id: 'apps', title: 'settings.section_other_tools', icon: 'plug', fields: [
    { type: 'gateway' },
    { type: 'subhead', label: 'settings.section_engines' },
    { type: 'engines' },
    { key: 'engines_comfyui_enabled', label: 'settings.engines_comfyui_enabled', type: 'switch',
      help: 'settings.engines_comfyui_enabled_help' },
    { key: 'engines_comfyui_path', label: 'settings.engines_comfyui_path', type: 'folder',
      help: 'settings.engines_comfyui_path_help' },
    { key: 'engines_comfyui_launch', label: 'settings.engines_comfyui_launch', type: 'switch',
      help: 'settings.engines_comfyui_launch_help' },
    { key: 'keep_awake_jobs', label: 'settings.keep_awake_jobs', type: 'switch',
      help: 'settings.keep_awake_jobs_help' },
    { key: 'engines_comfyui_url', label: 'settings.engines_comfyui_url', type: 'text',
      help: 'settings.engines_comfyui_url_help' },
  ] },
  { id: 'updates', title: 'settings.section_updates', icon: 'retry', fields: [
    { key: 'auto_update', label: 'settings.auto_update', type: 'switch', help: 'settings.auto_update_help' },
    { type: 'updates' },
    { type: 'approval' },
  ] },
  { id: 'advanced', title: 'settings.section_advanced', icon: 'settings', fields: [
    { key: 'ollama_url', label: 'settings.ollama_url', type: 'text' },
  ] },
];

/** Les choix de la langue, déjà traduits. « Automatique » dit entre parenthèses celle qu'il
 *  donne ici : celle de la machine, comme l'interface sans réglage (GET /api/i18n). */
function languageOptions() {
  const auto = t(`settings.language_${autoLanguage()}`);
  return [['auto', `${t('settings.language_auto')} (${auto})`],
    ['fr', t('settings.language_fr')], ['en', t('settings.language_en')]];
}

/** La langue que l'interface prendrait avec ce réglage. */
const shownLanguage = setting => (setting === 'fr' || setting === 'en' ? setting : autoLanguage());

let root;
let draft = {};

export function mountSettings(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head"><div><h1>${esc(t('settings.title'))}</h1>
        <div class="sub">${esc(t('settings.intro'))}</div></div><div class="grow"></div>
        <label class="lang-pick" title="${esc(t('settings.language_help'))}">${icon('globe')}
          <select class="select" id="set-language" aria-label="${esc(t('settings.language'))}"></select></label></div>
      <div class="settings" id="settings-machine"></div>
      <div class="settings" id="settings-form"></div>
      <div class="settings" id="settings-about"></div>
    </div></div>`);
  container.append(root);
  mountLanguage($('#set-language', root));
  // Cette machine : montée une fois (elle se rafraîchit seule), pas avec le formulaire
  const machine = h(fold({ id: 'settings-machine', title: t('node.title'), iconName: 'node' }));
  $('#settings-machine', root).append(machine);
  mountNode($('.fold-body', machine));
  // À propos, en dernier : la version, la licence, la charte et les conditions (legal.js)
  const about = h(fold({ id: 'settings-about', title: t('legal.about'), iconName: 'info' }));
  $('#settings-about', root).append(about);
  mountAbout($('.fold-body', about));
  keepFolds(root);
  machine.addEventListener('toggle', () => { if (machine.open) refreshNode(); });
  on('node:loaded', () => {
    const n = state.node;
    $('.fold-sub', machine).textContent = n ? [n.name, n.gpus?.[0]?.name].filter(Boolean).join(' · ') : '';
  });
  // On ne réaffiche pas le formulaire par-dessus des modifications non enregistrées
  const refresh = () => { if (state.view !== 'settings' || !Object.keys(draft).length) render(); };
  on('settings:loaded', refresh);
  on('models:loaded', refresh);
  on('embed:loaded', refresh);              // les modèles d'embeddings de cette machine
  window.addEventListener('pywebviewready', refresh);     // les boutons « Choisir… » des dossiers
  on('view:shown', view => {
    if (view === 'settings') {
      render(); loadUpdates(); loadGateway(); loadEngines(); loadImageModels(); loadEmbed();
    }
  });
  mountModels();
  const refreshModels = () => {
    const box = root && $('#models-box', root);
    if (box) { box.innerHTML = modelsCard(); bindModels(box); }
  };
  on('image-models:loaded', refreshModels);
  on('models:loaded', refreshModels);
  // Depuis l'onglet Images (« Gérer les modèles ») ou un lien vers Cette machine : la section,
  // ouverte et tout de suite visible
  on('settings:focus', id => openFold(root, `settings-${id}`));
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

/** La langue, en tête de page : enregistrée aussitôt ; une autre langue recharge l'interface. */
function mountLanguage(select) {
  const fill = () => {
    if (!state.settings) return;
    select.innerHTML = languageOptions().map(([v, l]) =>
      `<option value="${esc(v)}" ${v === state.settings.language ? 'selected' : ''}>${esc(l)}</option>`).join('');
  };
  on('settings:loaded', fill);
  fill();
  select.onchange = async () => {
    try {
      state.settings = await put('/settings', { language: select.value });
    } catch (e) { errorToast(e); fill(); return; }
    if (shownLanguage(select.value) !== lang) location.reload();
    else toast(t('settings.saved'), 'ok');
  };
}

function field(f, section) {
  const s = state.settings;
  if (f.type === 'updates') return `<div id="updates-status">${updatesStatus()}</div>`;
  if (f.type === 'gateway') return `<div id="gateway-box">${gatewayCard()}</div>`;
  if (f.type === 'approval') return '<div id="approval-box"></div>';
  if (f.type === 'engines') return `<div id="engines-box">${enginesCard()}</div>`;
  if (f.type === 'models') return `<div id="models-box">${modelsCard()}</div>`;
  if (f.type === 'subhead') return `<h3 class="settings-sub">${esc(t(f.label))}</h3>`;
  const id = `set-${f.key}`;
  const help = f.help ? `<div class="help">${esc(t(f.help))}</div>` : '';
  if (f.type === 'switch') {
    return `<div class="setting-row"><div class="txt"><b>${esc(t(f.label))}</b>${f.help ? `<small>${esc(t(f.help))}</small>` : ''}</div>
      <label class="switch"><input type="checkbox" id="${id}" data-key="${f.key}" ${s[f.key] ? 'checked' : ''}><span></span></label></div>`;
  }
  let control;
  if (f.type === 'textarea') {
    control = `<textarea class="textarea" id="${id}" data-key="${f.key}" rows="4">${esc(s[f.key])}</textarea>`;
  } else if (f.type === 'select' || f.type === 'model') {
    const current = f.type === 'model' ? asDefault(s[f.key]) : s[f.key];
    const options = f.type === 'model' ? modelOptions(current) : choices(f);
    // Seul champ de sa section, sans libellé (la langue) : le titre de la section le nomme
    const named = f.label ? '' : ` aria-label="${esc(t(section.title))}"`;
    control = `<select class="select" id="${id}" data-key="${f.key}"${named}>${options.map(([v, l]) =>
      `<option value="${esc(v)}" ${v === current ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
  } else if (f.type === 'folder') {       // un dossier : tapé, ou choisi dans la fenêtre d'AIODOS
    control = `<div class="folder-field"><input class="input" type="text" id="${id}" data-key="${f.key}"
      value="${esc(s[f.key])}">${window.pywebview?.api?.pick_folder
        ? `<button class="btn" type="button" data-pick="${id}">${icon('folder')}${esc(t('settings.choose'))}</button>` : ''}</div>`;
  } else if (f.type === 'embed') {        // le modèle d'embeddings : ceux de cette machine proposés
    const names = [...new Set([s[f.key], ...(state.embed?.candidates || [])].filter(Boolean))];
    control = `<input class="input" type="text" id="${id}" data-key="${f.key}" value="${esc(s[f.key])}"
      list="${id}-choices"><datalist id="${id}-choices">${names.map(n =>
        `<option value="${esc(n)}">`).join('')}</datalist>`;
  } else if (f.type === 'list') {         // une liste : les valeurs séparées par des virgules
    control = `<input class="input" type="text" id="${id}" data-key="${f.key}" data-list="1"
      value="${esc((s[f.key] || []).join(', '))}">`;
  } else {
    const type = f.type === 'number' ? 'number' : 'text';
    const limits = f.type === 'number' ? `min="${f.min}" max="${f.max}" step="${f.step || 1}"` : '';
    control = `<input class="input" type="${type}" ${limits} id="${id}" data-key="${f.key}" value="${esc(s[f.key])}">`;
  }
  const label = f.label ? `<label for="${id}">${esc(t(f.label))}</label>` : '';
  return `<div class="field">${label}${control}${help}</div>`;
}

/** Les choix d'une liste, traduits : ceux de la description, ou ceux que rend sa fonction
 *  (la langue). */
function choices(f) {
  if (typeof f.options === 'function') return f.options();
  return f.options.map(([value, key]) => [value, t(key)]);
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
  form.innerHTML = SECTIONS.map(sec => fold({ id: `settings-${sec.id}`, title: t(sec.title),
    iconName: sec.icon, sub: esc(sec.sub ? sec.sub() : ''),
    body: sec.fields.map(f => field(f, sec)).join('') })).join('') +
    // N'apparaît qu'avec des modifications à enregistrer : toujours affichée, elle cachait
    // le réglage du dessous en faisant défiler la page
    `<div class="save-bar" hidden><div class="save-box"><span>${esc(t('settings.unsaved'))}</span>
      <button class="btn ghost sm" id="settings-cancel">${esc(t('ui.cancel'))}</button>
      <button class="btn primary sm" id="settings-save">${icon('check')}${esc(t('settings.save'))}</button></div></div>`;
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
  keepFolds(form);
  bindUpdates(form);
  bindGateway(form);
  bindEngines(form);
  bindModels($('#models-box', root));
  renderApproval($('#approval-box', root));
  $('#settings-save', root).onclick = save;
  $('#settings-cancel', root).onclick = render;           // le formulaire revient aux réglages
}

async function save() {
  if (!Object.keys(draft).length) { toast(t('settings.no_changes')); return; }
  // Une autre langue : tous les textes de l'interface changent, elle se recharge
  const reload = 'language' in draft && shownLanguage(draft.language) !== lang;
  try {
    state.settings = await put('/settings', draft);
    if (reload) { location.reload(); return; }
    emit('settings:loaded');
    emit('models:reload');
    toast(t('settings.saved'), 'ok');
    render();
  } catch (e) { errorToast(e); }
}
