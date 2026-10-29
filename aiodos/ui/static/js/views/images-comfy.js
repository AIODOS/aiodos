// ComfyUI dans « Images et vidéos » (docs/conception/comfyui-createur.md § 3.2) : son état sur ce
// PC (ouvert, lancé par AIODOS, trouvé, absent : « Installer ComfyUI » ouvre la page officielle,
// AIODOS ne télécharge rien lui-même dans cette version), les recettes et leurs modèles
// (présents, ou à télécharger sur le clic, licence affichée), mes workflows importés, et
// « Créer » pour essayer une recette ou un workflow d'ici. Une création est un travail de la file
// (jobs/kinds/comfy_create.py) : ici ou chez mes machines, puis dans la galerie.
import { get, post, put, del, patch, rawUpload, openExternal, inNativeWindow } from '../api.js';
import { $, h, esc, bytes } from '../dom.js';
import { has, t } from '../i18n.js';
import { icon } from '../icons.js';
import { prefs } from '../state.js';
import { toast, errorToast, modal, ask, confirmDanger } from '../ui.js';

const MAX_BYTES = 2 * 1024 * 1024;           // comme le nœud (engines/comfy_workflows.py)
const MAX_COUNT = { image: 16, video: 4 };    // jobs/kinds/comfy_create.py
const PILLS = { open: 'ok', launched: 'ok', found: 'idle', absent: 'warn' };

let box;                 // la section (un <details>)
let info = null;         // GET /engines/comfyui/recipes
let form = null;         // { type: 'recipe' | 'workflow', id } : le formulaire « Créer » ouvert
let poll = null;

export function mountComfy(container) {
  box = h(`<details class="card comfy" id="img-comfy"></details>`);
  box.open = prefs.get('comfyOpen', true);
  box.addEventListener('toggle', () => prefs.set('comfyOpen', box.open));
  container.append(box);
}

export async function refreshComfy() {
  try { info = await get('/engines/comfyui/recipes'); } catch (err) { errorToast(err); return; }
  render();
  clearInterval(poll);
  poll = info.download?.running ? setInterval(refreshComfy, 1500) : null;
}

// ------------------------------------------------------------------ l'état
function stateLine(c) {
  const facts = { version: c.version || '?', url: c.url || '' };
  const lines = [];
  if (c.state === 'absent') {
    lines.push(`<div>${esc(t('images_comfy.state_absent'))}</div>`);
    if (c.installable) {
      lines.push(`<div class="comfy-actions"><button class="btn sm primary" data-act="install">${icon('download')}${esc(t('images_comfy.install'))}</button></div>
        <div class="faint">${esc(t('images_comfy.install_help'))}</div>`);
    } else {
      lines.push(`<div class="faint">${esc(t('images_comfy.absent_phone'))}</div>`);
    }
    return lines.join('');
  }
  const key = { open: 'state_open', launched: 'state_launched' }[c.state]
    || (c.granted && c.launch ? 'state_found_launch' : 'state_found');
  lines.push(`<div>${esc(t(`images_comfy.${key}`, facts))}</div>`);
  if (!c.granted) {
    lines.push(`<div class="comfy-actions"><span class="faint">${esc(t('images_comfy.not_granted'))}</span>
      <button class="btn sm primary" data-act="grant">${icon('check')}${esc(t('images_comfy.grant'))}</button></div>`);
  } else if (c.state === 'found' && !c.launch) {
    lines.push(`<div class="comfy-actions"><span class="faint">${esc(t('images_comfy.launch_help'))}</span>
      <button class="btn sm" data-act="launch">${icon('play')}${esc(t('images_comfy.launch'))}</button></div>`);
  }
  return lines.join('');
}

// ---------------------------------------------------------------- les recettes
function paramLabel(name) {
  return has(`images_comfy.param_${name}`) ? t(`images_comfy.param_${name}`) : name;
}

function modelLine(recipe, m) {
  const dl = info.download?.running && info.download.recipe === recipe.id && info.download.model === m.key
    ? info.download : null;
  let status;
  if (m.state === 'present') status = `<span class="pill ok">${esc(t('images_comfy.model_present'))}</span>`;
  else if (dl) {
    const pct = dl.total ? Math.floor(100 * dl.done / dl.total) : 0;
    status = `<span class="pill run">${esc(t('images_comfy.model_downloading', { pct }))}</span>
      <button class="btn sm ghost" data-act="stop-download">${icon('x')}${esc(t('images_comfy.download_stop'))}</button>`;
  } else if (m.state === 'missing') {
    status = `<span class="pill warn">${esc(t('images_comfy.model_missing'))}</span>
      <button class="btn sm" data-act="download" data-recipe="${esc(recipe.id)}" data-key="${esc(m.key)}">${icon('download')}${esc(t('images_comfy.download'))}</button>`;
  } else {
    const which = m.state === 'no_comfyui' ? 'model_no_comfyui' : 'model_unpinned';
    status = `<span class="pill idle" title="${esc(t(`images_comfy.${which}_help`))}">${esc(t(`images_comfy.${which}`))}</span>`;
  }
  const size = m.size ? ` · ${esc(bytes(m.size))}` : '';
  const license = m.license_url
    ? `<a href="${esc(m.license_url)}" target="_blank" rel="noopener" data-external>${esc(m.license)}</a>`
    : esc(m.license);
  return `<div class="comfy-model">
      <div class="comfy-model-text"><code class="mono">${esc(m.name)}</code>
        <span class="faint">${esc(m.folder)}${size} · ${esc(t('images_comfy.license_label'))} ${license}</span></div>
      <div class="comfy-model-state">${status}</div></div>`;
}

function recipeBlock(r) {
  const usable = r.ready_here || r.machines.length;
  const typical = Object.entries(r.typical_seconds || {})[0];
  const facts = [t(`images_comfy.kind_${r.kind}`)];
  if (typical) facts.push(t('images_comfy.typical', { seconds: Math.round(typical[1]), gb: typical[0] }));
  return `<div class="comfy-item" data-recipe="${esc(r.id)}">
      <div class="comfy-item-head"><b>${esc(r.title)}</b>
        <span class="faint">${esc(facts.join(' · '))}</span>
        ${r.signed ? '' : `<span class="pill idle" title="${esc(t('images_comfy.unsigned_help'))}">${esc(t('images_comfy.unsigned'))}</span>`}
        <span class="grow"></span>
        <button class="btn sm primary" data-act="create" data-type="recipe" data-id="${esc(r.id)}" ${usable ? '' : 'disabled'}
          title="${esc(usable ? '' : t('images_comfy.not_ready'))}">${icon(r.kind === 'video' ? 'video' : 'image')}${esc(t('images_comfy.create'))}</button></div>
      <div class="faint comfy-about">${esc(r.about)}</div>
      ${r.models.map(m => modelLine(r, m)).join('')}
      ${r.machines.length ? `<div class="faint">${esc(t('images_comfy.elsewhere', { machines: r.machines.join(', ') }))}</div>` : ''}
      ${form?.type === 'recipe' && form.id === r.id ? createForm(r.params, r.kind) : ''}
    </div>`;
}

// --------------------------------------------------------------- mes workflows
function workflowBlock(w) {
  return `<div class="comfy-item" data-workflow="${esc(w.id)}">
      <div class="comfy-item-head"><b>${esc(w.name)}</b>
        <span class="faint">${esc(t(`images_comfy.kind_${w.kind}`))} · ${esc(t('images_comfy.params_count', { n: w.params.length }))}</span>
        <span class="grow"></span>
        <button class="btn sm primary" data-act="create" data-type="workflow" data-id="${esc(w.id)}">${icon(w.kind === 'video' ? 'video' : 'image')}${esc(t('images_comfy.create'))}</button>
        <button class="icon-btn" data-act="rename" data-id="${esc(w.id)}" title="${esc(t('images_comfy.rename'))}">${icon('edit')}</button>
        <button class="icon-btn danger" data-act="delete" data-id="${esc(w.id)}" title="${esc(t('ui.delete'))}">${icon('trash')}</button></div>
      ${form?.type === 'workflow' && form.id === w.id ? createForm(w.params, w.kind) : ''}
    </div>`;
}

// ------------------------------------------------------------------ « Créer »
function field(p) {
  const label = `<label for="comfy-p-${esc(p.name)}">${esc(paramLabel(p.name))}</label>`;
  const id = `comfy-p-${esc(p.name)}`;
  if (p.type === 'text') {
    return `<div class="field comfy-wide">${label}<textarea class="textarea" id="${id}" data-param="${esc(p.name)}"
      maxlength="${p.max_length || 2000}" rows="${p.name === 'prompt' ? 3 : 2}">${esc(p.default || '')}</textarea></div>`;
  }
  if (p.type === 'seed') {
    return `<div class="field">${label}<input class="input" type="number" id="${id}" data-param="seed" min="0" step="1"
      placeholder="${esc(t('images_comfy.seed_help'))}"></div>`;
  }
  const step = p.step || (p.type === 'int' ? 1 : 'any');
  return `<div class="field">${label}<input class="input" type="number" id="${id}" data-param="${esc(p.name)}"
    data-kind="${p.type}" min="${p.min}" max="${p.max}" step="${step}" value="${esc(p.default)}"></div>`;
}

function createForm(params, kind) {
  return `<form class="comfy-form">
      <div class="comfy-fields">${params.map(field).join('')}
        <div class="field"><label for="comfy-count">${esc(t('images_comfy.count'))}</label>
          <input class="input" type="number" id="comfy-count" min="1" max="${MAX_COUNT[kind] || 1}" step="1" value="1"></div></div>
      <div class="comfy-actions">
        <button class="btn sm primary" type="submit">${icon('play')}${esc(t('images_comfy.create'))}</button>
        <button class="btn sm ghost" type="button" data-act="close-form">${esc(t('ui.cancel'))}</button></div>
    </form>`;
}

function values(formEl) {
  const out = {};
  formEl.querySelectorAll('[data-param]').forEach(el => {
    const name = el.dataset.param;
    if (el.tagName === 'TEXTAREA') { out[name] = el.value; return; }
    if (el.value === '') return;                       // vide : défaut (graine au hasard)
    out[name] = el.dataset.kind === 'float' ? Number(el.value) : Math.round(Number(el.value));
  });
  return out;
}

async function submit(formEl) {
  const body = { [form.type]: form.id, params: values(formEl),
                 count: Math.round(Number($('#comfy-count', formEl).value) || 1) };
  const button = formEl.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const made = await post('/engines/comfyui/create', body);
    toast(t('images_comfy.create_started', { n: made.count }), 'ok');
    form = null;
    render();
  } catch (err) {
    errorToast(err);
    button.disabled = false;
  }
}

// ------------------------------------------------------------------- l'affichage
function render() {
  if (!box || !info) return;
  const c = info.comfyui;
  const open = box.open;
  box.innerHTML = `
    <summary class="comfy-summary">${icon('layers')}<b>ComfyUI</b>
      <span class="pill ${PILLS[c.state] || 'idle'}">${esc(t(`images_comfy.pill_${c.state}`))}</span>
      <span class="faint comfy-summary-text">${esc(t('images_comfy.summary'))}</span></summary>
    <div class="comfy-body">
      <div class="comfy-state">${stateLine(c)}</div>
      <h3 class="comfy-title">${esc(t('images_comfy.recipes'))}</h3>
      <div class="faint">${esc(t('images_comfy.recipes_help'))}</div>
      ${info.recipes.map(recipeBlock).join('') || `<div class="faint">${esc(t('images_comfy.no_recipe'))}</div>`}
      <h3 class="comfy-title">${esc(t('images_comfy.workflows'))}</h3>
      <div class="faint">${esc(t('images_comfy.workflows_help'))}</div>
      ${info.workflows.map(workflowBlock).join('') || `<div class="faint comfy-item">${esc(t('images_comfy.no_workflow'))}</div>`}
      <div class="comfy-actions">
        <button class="btn sm" data-act="import">${icon('file')}${esc(t('images_comfy.import'))}</button>
        <input type="file" accept=".json,application/json" hidden id="comfy-file"></div>
      ${info.download?.error && !info.download.running
        ? `<div class="err-text">${esc(t('images_comfy.download_failed', { error: info.download.error }))}</div>` : ''}
    </div>`;
  box.open = open;
  bind();
}

function bind() {
  box.querySelectorAll('[data-act]').forEach(b => {
    const act = b.dataset.act;
    b.addEventListener('click', e => { e.preventDefault(); actions[act]?.(b); });
  });
  box.querySelectorAll('a[data-external]').forEach(a => {
    a.addEventListener('click', e => {             // la fenêtre native : le navigateur du système
      if (inNativeWindow()) { e.preventDefault(); openExternal(a.href).catch(errorToast); }
    });
  });
  const formEl = box.querySelector('.comfy-form');
  if (formEl) formEl.addEventListener('submit', e => { e.preventDefault(); submit(formEl); });
  const file = $('#comfy-file', box);
  file.addEventListener('change', () => importFile(file.files[0]));
}

const actions = {
  install: () => {
    const url = info.comfyui.install_url;
    if (inNativeWindow()) openExternal(url).catch(errorToast);
    else window.open(url, '_blank', 'noopener');
  },
  grant: async () => {
    const r = await modal({ title: t('images_comfy.grant_title'), text: t('images_comfy.grant_text'),
                            confirm: t('images_comfy.grant'), iconName: 'plug' });
    if (!r.ok) return;
    try { await put('/settings', { engines_comfyui_enabled: true }); } catch (err) { errorToast(err); }
    refreshComfy();
  },
  launch: async () => {
    try { await put('/settings', { engines_comfyui_launch: true }); } catch (err) { errorToast(err); }
    refreshComfy();
  },
  download: async b => {
    const recipe = info.recipes.find(r => r.id === b.dataset.recipe);
    const m = recipe?.models.find(x => x.key === b.dataset.key);
    if (!m) return;
    const r = await modal({ title: t('images_comfy.download_title', { name: m.name }),
      text: t('images_comfy.download_text', { size: bytes(m.size), license: m.license,
                                              url: m.license_url || '' }),
      confirm: t('images_comfy.download'), iconName: 'download' });
    if (!r.ok) return;
    try {
      await post('/engines/comfyui/models/download', { recipe: recipe.id, key: m.key });
      toast(t('images_comfy.download_started', { name: m.name }), 'info');
    } catch (err) { errorToast(err); }
    refreshComfy();
  },
  'stop-download': async () => {
    try { await del('/engines/comfyui/models/download'); } catch (err) { errorToast(err); }
    refreshComfy();
  },
  create: b => {
    form = form?.id === b.dataset.id ? null : { type: b.dataset.type, id: b.dataset.id };
    render();
    box.querySelector('.comfy-form textarea, .comfy-form input')?.focus();
  },
  'close-form': () => { form = null; render(); },
  import: () => $('#comfy-file', box).click(),
  rename: async b => {
    const w = info.workflows.find(x => x.id === b.dataset.id);
    const name = await ask(t('images_comfy.rename_title'), t('images_comfy.rename_label'), w?.name || '');
    if (!name) return;
    try { await patch(`/engines/comfyui/workflows/${w.id}`, { name }); } catch (err) { errorToast(err); }
    refreshComfy();
  },
  delete: async b => {
    if (!await confirmDanger(t('images_comfy.delete_title'), t('images_comfy.delete_text'))) return;
    try { await del(`/engines/comfyui/workflows/${b.dataset.id}`); } catch (err) { errorToast(err); }
    if (form?.id === b.dataset.id) form = null;
    refreshComfy();
  },
};

async function importFile(file) {
  if (!file) return;
  if (file.size > MAX_BYTES) { toast(t('images_comfy.too_big'), 'err', 6000); return; }
  try {
    const w = await rawUpload('/engines/comfyui/workflows', file, file.name);
    toast(t('images_comfy.imported', { n: w.params.length }), 'ok');
  } catch (err) { errorToast(err); }
  refreshComfy();
}
