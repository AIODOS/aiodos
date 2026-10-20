// Mises à jour : version de cette machine, nouvelle version en cours de réception ou
// prête à démarrer, et message « AIODOS a été mis à jour » montré une fois.
import { get, post } from '../api.js';
import { esc, bytes } from '../dom.js';
import { icon } from '../icons.js';
import { state, emit } from '../state.js';
import { toast, errorToast } from '../ui.js';

export async function loadUpdates() {
  try { state.updates = await get('/updates'); } catch { return; }
  emit('updates:loaded');
}

// Au démarrage de l'interface : le message laissé par le lanceur, une seule fois
export async function announceUpdate() {
  await loadUpdates();
  const notice = state.updates?.notice;
  if (!notice) return;
  if (notice.kind === 'updated') {
    toast(`AIODOS a été mis à jour : version ${notice.version}.${notice.notes ? ` ${notice.notes}` : ''}`,
      'ok', 12000);
  } else {
    toast(`La version ${notice.version} n'a pas démarré sur cette machine : AIODOS continue avec la précédente.`,
      'err', 12000);
  }
  try { await post('/updates/notice/seen'); } catch { /* redit au prochain démarrage */ }
}

function stateLine(u) {
  if (u.android_app) {
    return 'Appli Android : une nouvelle version d\'AIODOS arrive avec une nouvelle appli.';
  }
  if (u.developer) {
    return 'Copie de développement (dépôt git) : son code n\'est jamais remplacé. '
      + 'Elle transmet les versions publiées à tes machines.';
  }
  if (u.ready) {
    return `Version ${esc(u.ready)} installée : elle démarrera ${u.restart_when === 'idle'
      ? 'dès que la machine sera libre' : 'au prochain démarrage d\'AIODOS'}.`;
  }
  if (u.installing) return `Installation de la version ${esc(u.installing)}…`;
  if (u.download) {
    const d = u.download;
    const progress = d.size ? ` : ${bytes(d.received)} / ${bytes(d.size)}` : '…';
    const what = d.what === 'compagnon'
      ? `du compagnon Internet de la version ${esc(d.version)}`
      : d.what ? `du ${esc(d.what)} de la version ${esc(d.version)}` : `de la version ${esc(d.version)}`;
    return `Réception ${what} depuis « ${esc(d.from)} »${progress}`;
  }
  if (!u.auto) return 'Mises à jour automatiques désactivées.';
  return 'À jour : aucune version plus récente chez tes machines appairées.';
}

export function updatesStatus() {
  const u = state.updates;
  if (!u) return '<div class="help">État des mises à jour indisponible.</div>';
  return `
    <div class="setting-row updates-status"><div class="txt"><b>AIODOS ${esc(u.version)}</b>
      <small>${stateLine(u)}</small>
      ${u.error ? `<small class="warn">${esc(u.error)}</small>` : ''}
      ${u.offer ? `<small>Transmet la version ${esc(u.offer)} à tes machines appairées.</small>` : ''}</div>
      ${u.ready ? `<button class="btn sm" id="updates-restart" type="button">${icon('retry')}Redémarrer maintenant</button>` : ''}
    </div>`;
}

export function bindUpdates(box) {
  const button = box.querySelector('#updates-restart');
  if (!button) return;
  button.onclick = async () => {
    button.disabled = true;
    try {
      await post('/updates/restart');
      toast('Redémarrage sur la nouvelle version…', 'info', 6000);
    } catch (e) { button.disabled = false; errorToast(e); }
  };
}
