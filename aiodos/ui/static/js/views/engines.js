// Moteurs des applications (Réglages) : le ComfyUI de ce PC, trouvé par AIODOS, et accordé ou
// non aux travaux des applications (docs/conception/partage-puissance.md § 3.2). AIODOS ne
// livre ni n'installe ComfyUI : il envoie les travaux des applis que tu autorises (PANDORA…)
// au ComfyUI ouvert de ce PC, où ils s'ajoutent à sa file sous le nom « aiodos ».
import { get, post } from '../api.js';
import { esc } from '../dom.js';
import { icon } from '../icons.js';
import { state, emit } from '../state.js';
import { errorToast } from '../ui.js';

export async function loadEngines() {
  try { state.engines = await get('/engines'); } catch { return; }
  emit('engines:loaded');
}

function line(text, kind = '') {
  return `<div><small${kind ? ` class="${kind}"` : ''}>${text}</small></div>`;
}

function installLines(c) {
  if (!c.installs.length) {
    return line('Aucune installation de ComfyUI trouvée sur ce PC (Comfy Desktop, version portable). '
      + 'Indique son dossier ci-dessous s\'il est ailleurs.');
  }
  return c.installs.map(i => line(`${esc(i.kind === 'desktop' ? 'Comfy Desktop' : i.kind === 'portable'
    ? 'Version portable' : 'Dossier réglé')} : ComfyUI ${esc(i.version || '?')} — `
    + `<code class="mono">${esc(i.path)}</code>`)).join('');
}

function instanceLine(c) {
  const i = c.instance;
  if (!i && c.launched) return line('Lancé par AIODOS, sans fenêtre, pour les travaux en cours.', 'ok');
  if (!i && c.launch && c.enabled) {
    return line('Fermé : AIODOS le lancera sans fenêtre quand un travail attendra'
      + (c.learned ? '.' : ' (il le lance une fois pour lire ce qu\'il sait faire).'));
  }
  if (!i) {
    return line('ComfyUI n\'est pas ouvert : ouvre-le pour que les travaux des applications partent. '
      + 'D\'ici là, ils attendent dans la file.', 'warn');
  }
  const vram = i.vram_mib ? ` · ${Math.round(i.vram_mib / 1024)} Go de mémoire vidéo` : '';
  return line(`Ouvert sur ${esc(i.url)} : ComfyUI ${esc(i.version || '?')}${vram} · `
    + `${esc(String(i.models))} modèles.`, 'ok');
}

export function enginesCard() {
  const e = state.engines;
  if (!e) return '<div class="help">Recherche de ComfyUI…</div>';
  const c = e.comfyui;
  const jobs = c.jobs.active ? line(`${esc(String(c.jobs.active))} travail(aux) d'applications en cours.`) : '';
  return `<div class="setting-row"><div class="txt"><b>${icon('image')}ComfyUI sur ce PC</b>
      ${installLines(c)}${instanceLine(c)}${jobs}</div>
    <button class="btn sm" type="button" id="engines-refresh">${icon('retry')}Chercher de nouveau</button></div>`;
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
