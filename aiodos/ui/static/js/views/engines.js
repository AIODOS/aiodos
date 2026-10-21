// Moteurs des applications (Réglages) : le ComfyUI de ce PC, trouvé par AIODOS, et accordé ou
// non aux travaux des applications (docs/conception/partage-puissance.md § 3.2). AIODOS ne
// livre ni n'installe ComfyUI : il envoie les travaux des applis que tu autorises (PANDORA…)
// au ComfyUI ouvert de ce PC, où ils s'ajoutent à sa file sous le nom « aiodos ».
import { get, post } from '../api.js';
import { esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, emit } from '../state.js';
import { errorToast } from '../ui.js';

export async function loadEngines() {
  try { state.engines = await get('/engines'); } catch { return; }
  emit('engines:loaded');
}

// text : du HTML (un texte traduit passe par esc)
function line(text, kind = '') {
  return `<div><small${kind ? ` class="${kind}"` : ''}>${text}</small></div>`;
}

// Le genre d'une installation : Comfy Desktop (son nom, dans toutes les langues), la version
// portable, ou le dossier réglé ci-dessous
function kindLabel(kind) {
  if (kind === 'desktop') return 'Comfy Desktop';
  return t(kind === 'portable' ? 'app_engines.kind_portable' : 'app_engines.kind_folder');
}

function installLines(c) {
  if (!c.installs.length) return line(esc(t('app_engines.no_install')));
  return c.installs.map(i => line(t('app_engines.install_html', { kind: esc(kindLabel(i.kind)),
    version: esc(i.version || '?'), path: `<code class="mono">${esc(i.path)}</code>` }))).join('');
}

function instanceLine(c) {
  const i = c.instance;
  if (!i && c.launched) return line(esc(t('app_engines.launched')), 'ok');
  if (!i && c.launch && c.enabled) {
    return line(esc(t(c.learned ? 'app_engines.closed' : 'app_engines.closed_first')));
  }
  if (!i) return line(esc(t('app_engines.not_open')), 'warn');
  const facts = { url: i.url, version: i.version || '?', n: i.models,
    vram: Math.round(i.vram_mib / 1024) };
  return line(esc(t(i.vram_mib ? 'app_engines.open_vram' : 'app_engines.open', facts)), 'ok');
}

export function enginesCard() {
  const e = state.engines;
  if (!e) return `<div class="help">${esc(t('app_engines.searching'))}</div>`;
  const c = e.comfyui;
  const jobs = c.jobs.active ? line(esc(t('app_engines.jobs_active', { n: c.jobs.active }))) : '';
  return `<div class="setting-row"><div class="txt"><b>${icon('image')}${esc(t('app_engines.title'))}</b>
      ${installLines(c)}${instanceLine(c)}${jobs}</div>
    <button class="btn sm" type="button" id="engines-refresh">${icon('retry')}${esc(t('app_engines.refresh'))}</button></div>`;
}

export function bindEngines(box) {
  const button = box.querySelector('#engines-refresh');
  if (!button) return;
  button.onclick = async () => {
    button.disabled = true;
    try { state.engines = await post('/engines/comfyui/refresh', {}); emit('engines:loaded'); }
    catch (err) { errorToast(err); }
    finally { button.disabled = false; }
  };
}
