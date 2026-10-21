// Mises à jour : version de cette machine, nouvelle version en cours de réception ou
// prête à démarrer, et message « AIODOS a été mis à jour » montré une fois. Sur un nœud
// dont la version d'origine ignore les amis (updates/launcher.friends_refused), pourquoi
// il n'en accueille pas et quoi faire (le réinstaller) : rien d'autre ne le dit à qui
// n'essaie pas d'appairer un ami.
import { get, post } from '../api.js';
import { esc, bytes } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, emit } from '../state.js';
import { toast, errorToast } from '../ui.js';

// Ce que la machine reçoit en plus d'une version (« what », updates/service.py) : son
// compagnon Internet, ou un moteur (« moteur llama.cpp »)
const COMPANION = 'compagnon';      // i18n: tel quel (valeur envoyée par le nœud)
const ENGINE = 'moteur ';           // i18n: tel quel (valeur envoyée par le nœud)

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
    toast(t(notice.notes ? 'updates.updated_notes' : 'updates.updated',
      { version: notice.version, notes: notice.notes }), 'ok', 12000);
  } else {
    toast(t('updates.rolled_back', { version: notice.version }), 'err', 12000);
  }
  try { await post('/updates/notice/seen'); } catch { /* redit au prochain démarrage */ }
}

// Réception en cours : la version, son compagnon ou un moteur, avec sa progression si sa
// taille est connue
function receiving(d) {
  const params = { version: d.version, from: d.from, received: bytes(d.received), size: bytes(d.size) };
  if (d.what === COMPANION) {
    return t(d.size ? 'updates.receiving_companion_progress' : 'updates.receiving_companion', params);
  }
  if (d.what?.startsWith(ENGINE)) {
    return t(d.size ? 'updates.receiving_engine_progress' : 'updates.receiving_engine',
      { ...params, engine: d.what.slice(ENGINE.length) });
  }
  return t(d.size ? 'updates.receiving_progress' : 'updates.receiving', params);
}

function stateLine(u) {
  if (u.android_app) return t('updates.android_app');
  if (u.developer) return t('updates.developer');
  if (u.ready) {
    return t(u.restart_when === 'idle' ? 'updates.ready_idle' : 'updates.ready_next_start',
      { version: u.ready });
  }
  if (u.installing) return t('updates.installing', { version: u.installing });
  if (u.download) return receiving(u.download);
  if (!u.auto) return t('updates.auto_off');
  return t('updates.up_to_date');
}

export function updatesStatus() {
  const u = state.updates;
  if (!u) return `<div class="help">${esc(t('updates.unavailable'))}</div>`;
  return `
    <div class="setting-row updates-status"><div class="txt"><b>AIODOS ${esc(u.version)}</b>
      <small>${esc(stateLine(u))}</small>
      ${u.error ? `<small class="warn">${esc(u.error)}</small>` : ''}
      ${u.friends_refused ? `<small class="warn">${esc(u.friends_refused)}</small>` : ''}
      ${u.offer ? `<small>${esc(t('updates.offer', { version: u.offer }))}</small>` : ''}</div>
      ${u.ready ? `<button class="btn sm" id="updates-restart" type="button">${icon('retry')}${esc(t('updates.restart_now'))}</button>` : ''}
    </div>`;
}

export function bindUpdates(box) {
  const button = box.querySelector('#updates-restart');
  if (!button) return;
  button.onclick = async () => {
    button.disabled = true;
    try {
      await post('/updates/restart');
      toast(t('updates.restarting'), 'info', 6000);
    } catch (e) { button.disabled = false; errorToast(e); }
  };
}
