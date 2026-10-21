// Réglages → Modèles : ceux des discussions et ceux des images et des vidéos, du plus puissant
// au moins puissant, ceux à télécharger en bas. Le téléchargement (Hugging Face, ou copie depuis
// mes machines, fichiers déjà sur ce PC repris d'abord) se fait ici ; l'onglet Images (images.js)
// n'en montre qu'un résumé, avec un lien vers cette section.
import { get, post, put, del } from '../api.js';
import { bytes, esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { errorToast, modal } from '../ui.js';
import { byPower, catalog } from './models.js';
import { chooseModel, progressBar, cancelInstall, isPhone } from './model-install.js';

let info = null;                 // modèles d'images et de vidéos (GET /images/models)
let installing = null;           // installation d'un modèle de discussion en cours

export function mountModels() {
  on('model-install', ev => {    // avancement d'une installation (le mot de la fin : chat.js)
    installing = ev?.running ? ev : null;
    emit('image-models:loaded');
  });
  setInterval(() => {            // avancement d'un téléchargement
    if (state.view === 'settings' && info?.download?.running) loadImageModels();
  }, 1500);
}

export async function loadImageModels() {
  try { info = await get('/images/models'); } catch (err) { errorToast(err); return; }
  emit('image-models:loaded');
}

export function modelsCard() {
  return `<h3 class="settings-sub">${icon('chat')}${esc(t('nav.chat'))}</h3>${chatModels()}
    <h3 class="settings-sub">${icon('image')}${esc(t('images.title'))}</h3>${imageModels()}`;
}

export function bindModels(box) {
  if (!box) return;
  box.querySelectorAll('[data-install]').forEach(b => { b.onclick = chooseModel; });
  box.querySelectorAll('[data-install-cancel]').forEach(b => { b.onclick = cancelInstall; });
  box.querySelectorAll('[data-download]').forEach(b => {
    b.onclick = () => download(b.dataset.download, b.dataset.source || null);
  });
  box.querySelectorAll('[data-cancel]').forEach(b => { b.onclick = cancelDownload; });
  box.querySelectorAll('[data-friends-ok]').forEach(b => {
    b.onclick = async () => {
      try { state.settings = await put('/settings', { images_from_friends: true }); }
      catch (err) { errorToast(err); return; }
      loadImageModels();
    };
  });
}

// ------------------------------------------------------------- discussions
function chatModels() {
  const rows = catalog().map(e => `<div class="model-row"><b>${esc(e.name)}</b>
    <span class="faint">${bytes(e.size)} · ${esc(e.machines.map(m => m.label).join(', '))}</span></div>`).join('');
  const cluster = state.models.filter(m => m.group === 'cluster').sort(byPower).map(m => `
    <div class="model-row"><b>${esc(m.label)}</b>
    <span class="faint">${esc(t('settings_models.split', { size: bytes(m.size), machines: m.machines.join(', ') }))}</span></div>`).join('');
  // « ce téléphone » ou « ce PC » : une phrase entière pour chacun
  const none = rows || cluster ? '' : `<div class="faint">${esc(t(isPhone()
    ? 'settings_models.none_phone' : 'settings_models.none_pc'))}</div>`;
  return `${progressBar(installing)}${rows}${cluster}${none}
    <div class="model-actions"><button class="btn sm" type="button" data-install>${icon('plus')}${esc(t('chat.install'))}</button></div>`;
}

// --------------------------------------------------------- images et vidéos
const downloading = m => info?.download?.running && info.download.model === m.id;

function imageModels() {
  if (!info) return `<div class="faint">${esc(t('settings_models.reading'))}</div>`;
  const engine = info.engine.found ? '' : `
    <div class="banner warn">${icon('warn')}<div>${esc(t('settings_models.engine_missing'))}
      ${esc(t(info.models.some(m => m.helpers.length)
        ? 'settings_models.engine_helpers' : 'settings_models.engine_no_helpers'))}</div></div>`;
  const ready = info.models.filter(m => m.state === 'ready' && !downloading(m)).sort(byPower);
  const rest = info.models.filter(m => !ready.includes(m)).sort(byPower);
  return engine + ready.map(readyRow).join('')
    + (rest.length ? `<div class="settings-sub2">${esc(t('settings_models.to_download'))}</div>${rest.map(toDownload).join('')}` : '');
}

function license(m) {
  return `<span class="pill${m.notice ? ' warn' : ''}"${m.notice ? ` title="${esc(m.notice)}"` : ''}>${esc(m.license)}</span>`;
}

function helpersLine(m) {
  return m.helpers.length
    ? `<div class="faint">${esc(t('settings_models.helpers', { names: m.helpers.join(', ') }))}</div>` : '';
}

function readyRow(m) {
  const where = info.engine.device
    ? ` · ${esc(t('settings_models.computing_on', { device: info.engine.device }))}` : '';
  return `<div class="img-model ok">${icon('check')}<div><b>${esc(m.label)}</b>
    <span class="pill ok">${esc(t('settings_models.ready'))}</span> ${license(m)} <span class="faint">${bytes(m.size)}${where}</span>
    ${helpersLine(m)}</div></div>`;
}

function toDownload(m) {
  const dl = info.download?.model === m.id ? info.download : null;
  // Un ami propose sa carte graphique : avec ton accord, une fois pour toutes
  const friends = !info.from_friends && (m.friends || []).length ? `
    <div class="banner info img-friends">${icon('network')}<div>${esc(t('settings_models.friends', {
      names: m.friends.join(', '), n: m.friends.length }))}</div>
      <button class="btn sm" type="button" data-friends-ok>${esc(t('settings_models.friends_allow'))}</button></div>` : '';
  let action;
  if (dl?.running) {
    const pct = dl.total ? Math.floor(100 * dl.done / dl.total) : 0;
    action = `<div class="img-dl"><div class="bar"><i class="fill" style="width:${pct}%"></i></div>
      <span class="faint">${esc(t('settings_models.download_progress', { done: bytes(dl.done),
        total: bytes(dl.total), rate: bytes(dl.rate), source: dl.source || 'Hugging Face' }))}</span>
      <button class="btn sm ghost" type="button" data-cancel>${icon('x')}${esc(t('settings_models.stop'))}</button></div>`;
  } else if (m.state === 'checking') {
    action = `<span class="faint">${esc(t('settings_models.checking'))}</span>`;
  } else {
    // Fichiers identiques déjà sur ce PC (ComfyUI, PANDORA…) : repris d'abord, sans téléchargement
    const local = m.local || [];
    const localBytes = local.reduce((n, f) => n + f.size, 0);
    const rest = Math.max(0, m.missing_bytes - localBytes);
    const size = bytes(rest);
    const onDisk = local.length ? `<div class="faint">${esc(t('settings_models.local', { n: local.length,
      size: bytes(localBytes), sources: [...new Set(local.map(f => f.from))].join(', ') }))}</div>` : '';
    const copies = rest ? [...m.sources].sort((a, b) => b.near - a.near).map(s => `
      <button class="btn sm" type="button" data-download="${esc(m.id)}" data-source="${esc(s.id)}">${icon('network')}${esc(t(
        s.near ? 'settings_models.copy_lan' : 'settings_models.copy_internet', { name: s.name, size }))}</button>`).join('') : '';
    const label = !rest ? t('settings_models.take_local', { size: bytes(localBytes) })
      : m.state === 'bad' ? t('settings_models.download_again') : t('settings_models.download_hf', { size });
    action = `${onDisk}<div class="img-dl">${copies}
      <button class="btn sm${copies ? ' ghost' : ''}" type="button" data-download="${esc(m.id)}">${icon(rest ? 'download' : 'check')}${esc(label)}</button>
      ${dl?.error ? `<span class="err-text">${esc(dl.error)}</span>` : ''}</div>`;
  }
  return `<div class="model-dl">
    <div class="model-dl-head">${icon(m.kind === 'video' ? 'video' : 'image')}<b>${esc(m.label)}</b>${license(m)}</div>
    <p class="hint">${esc(m.about)} ${esc(t('settings_models.about_size', { size: bytes(m.size) }))}</p>
    ${helpersLine(m)}${friends}${action}</div>`;
}

async function download(id, source) {
  const model = info?.models.find(m => m.id === id);
  if (model?.notice) {          // licence restrictive : lue et acceptée avant de télécharger
    const { ok } = await modal({ title: t('settings_models.license_title', { name: model.label }),
      text: model.notice, iconName: 'warn', confirm: t('settings_models.license_confirm'),
      cancel: t('ui.cancel') });
    if (!ok) return;
  }
  try { info.download = await post(`/images/models/${encodeURIComponent(id)}/download`, { source }); }
  catch (err) { errorToast(err); return; }
  emit('image-models:loaded');
}

async function cancelDownload() {
  try { await del('/images/models/download'); } catch (err) { errorToast(err); }
  loadImageModels();
}
