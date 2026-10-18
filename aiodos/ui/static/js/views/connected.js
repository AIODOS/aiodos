// Machines connectées, en direct, à la façon de la liste des pairs d'un client BitTorrent :
// qui est en ligne et par quel chemin, qui prête sa puissance, ce que chacune fait pour
// l'autre en ce moment, et ce qui a été échangé (network/activity.py). En tête de la vue
// Réseau ; et dans la barre du bas, le nombre de machines en ligne.
import { $, esc, ago, duration } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';

const ROUTES = { lan: 'réseau local', direct: 'Internet, en direct', relay: 'Internet, par un relais',
  internet: 'Internet' };

const plural = (n, word) => `${n} ${word}${n > 1 ? 's' : ''}`;
const sum = items => (items || []).reduce((n, item) => n + (item.units || 0), 0);
// Prête sa puissance en ce moment ; d'un ami, on sait seulement si c'est activé
const lendsNow = sharing => !!sharing && ('active' in sharing ? sharing.active : sharing.enabled);

function busy(peer) {
  const live = peer.live || {};
  return sum(live.for_me) + (live.for_it || []).length + (live.chats_for_me || 0) + (live.chats_for_it || 0);
}

function link(peer) {
  if (!peer.online) return peer.last_seen ? `hors ligne · vue ${ago(peer.last_seen)}` : 'jamais vue';
  const live = peer.live || {};
  const rtt = live.rtt_ms ?? live.internet_rtt_ms;
  return `${ROUTES[live.route] || 'en ligne'}${rtt != null ? ` · ${Math.round(rtt)} ms` : ''}`;
}

// Ce qu'elle prête : sa puissance (l'état du moment pour mes machines), ses modèles, sa carte
function lends(peer) {
  const p = peer.profile || {};
  const sharing = peer.live?.sharing;
  const pills = [];
  if (sharing ? sharing.enabled : p.images?.shares) {
    if (sharing && 'active' in sharing) {
      pills.push(sharing.active
        ? '<span class="pill ok">sa puissance</span>'
        : `<span class="pill warn" title="${esc(sharing.detail)}">puissance en pause</span>`);
    } else {
      pills.push('<span class="pill">sa puissance</span>');
    }
  }
  if (p.lends_models) pills.push('<span class="pill">ses modèles</span>');
  if (p.cluster?.lends) pills.push('<span class="pill">sa carte graphique</span>');
  return pills.length ? pills.join('') : '<span class="faint">rien</span>';
}

// En ce moment, dans les deux sens
function now(peer) {
  const live = peer.live || {};
  const items = [];
  const forMe = sum(live.for_me);
  if (forMe) {
    const what = live.for_me.map(j => `${j.title} (${plural(j.units, 'unité')})`).join(', ');
    items.push(`<span class="live-in" title="${esc(what)}">${icon('bolt')}calcule ${plural(forMe, 'unité')} pour toi</span>`);
  }
  if (live.chats_for_me) items.push(`<span class="live-in">${icon('chat')}répond à ta conversation</span>`);
  const forIt = live.for_it || [];
  if (forIt.length) {
    items.push(`<span class="live-out" title="${esc(forIt.join(', '))}">${icon('cpu')}tu calcules
      ${plural(forIt.length, 'unité')} pour elle</span>`);
  }
  if (live.chats_for_it) items.push(`<span class="live-out">${icon('chat')}tu réponds à sa conversation</span>`);
  return items.length ? items.join('') : `<span class="faint">${peer.online ? 'rien en ce moment' : '—'}</span>`;
}

// Échangé depuis l'appairage, dans chaque sens : le temps de calcul des tâches, puis les
// conversations auxquelles un modèle prêté a répondu
function exchanged(peer) {
  const compute = [];
  if (peer.received_s) {
    compute.push(`<span title="${plural(peer.received_units, 'unité')} calculée(s) pour toi">reçu ${duration(peer.received_s)}</span>`);
  }
  if (peer.given_s) {
    compute.push(`<span title="${plural(peer.given_units, 'unité')} calculée(s) pour elle">donné ${duration(peer.given_s)}</span>`);
  }
  const chats = [];
  if (peer.received_chats) chats.push(`${peer.received_chats} pour toi`);
  if (peer.given_chats) chats.push(`${peer.given_chats} pour elle`);
  const lines = [];
  if (compute.length) lines.push(compute.join(' · '));
  // Fiabilité : ses résultats refusés (faux ou illisibles), les unités qu'elle n'a pas pu calculer
  const trouble = [];
  if (peer.refused_units) trouble.push(`${plural(peer.refused_units, 'résultat')} refusé${peer.refused_units > 1 ? 's' : ''}`);
  if (peer.failed_units) trouble.push(`${plural(peer.failed_units, 'échec')}`);
  if (trouble.length) {
    lines.push(`<span class="live-trouble" title="Résultats qu'elle t'a rendus et qui ont été refusés (faux ou illisibles), et unités qu'elle n'a pas pu calculer pour toi">${trouble.join(' · ')}</span>`);
  }
  if (chats.length) {
    lines.push(`<span title="Réponses de ses modèles à tes conversations, et des tiens aux siennes">${
      peer.received_chats + peer.given_chats > 1 ? 'conversations' : 'conversation'} : ${chats.join(' · ')}</span>`);
  }
  return lines.length ? lines.join('<br>') : '<span class="faint">rien encore</span>';
}

function summary(net) {
  const peers = net.peers;
  const online = peers.filter(p => p.online);
  const helping = online.filter(p => sum(p.live?.for_me) || p.live?.chats_for_me).length;
  const helped = online.filter(p => (p.live?.for_it || []).length || p.live?.chats_for_it).length;
  const lending = online.filter(p => lendsNow(p.live?.sharing)).length;
  const me = net.sharing;
  const chips = [`<span class="pill ${online.length ? 'ok' : 'idle'}">${online.length} en ligne sur ${peers.length}</span>`];
  if (lending) {
    chips.push(`<span class="pill ok">${lending} ${lending > 1 ? 'prêtent leur' : 'prête sa'} puissance</span>`);
  }
  if (helping) chips.push(`<span class="pill run">${helping} travaille${helping > 1 ? 'nt' : ''} pour toi</span>`);
  if (helped) chips.push(`<span class="pill run">tu travailles pour ${helped}</span>`);
  if (me) {
    chips.push(me.active ? '<span class="pill">cette machine prête la sienne</span>'
      : `<span class="pill idle" title="${esc(me.detail || '')}">cette machine ne prête pas en ce moment</span>`);
  }
  return chips.join('');
}

// Réciprocité souple : une machine qui reçoit sans rien prêter en est avertie (sans blocage)
function nudge(net) {
  const total = (keys) => net.peers.reduce((n, p) => n + keys.reduce((m, k) => m + (p[k] || 0), 0), 0);
  const received = total(['received_units', 'received_chats']);
  const given = total(['given_units', 'given_chats']);
  const s = state.settings || {};
  const hasModels = (state.models || []).some(m => m.group === 'local');
  if (received < 3 || given > 0 || s.share_enabled || (s.lend_models && hasModels)) return '';
  return `<div class="banner info live-nudge">${icon('bolt')}<div>Tes machines et tes amis t'ont déjà rendu
    ${received} services (conversations, calculs), et cette machine ne prête encore rien en retour. Pour
    participer : ${hasModels ? '' : 'installe un modèle d’Ollama, puis '}active « Prêter mes modèles » ou
    « Prêter ma puissance ». Ceux qui participent passent devant quand une machine est occupée.</div>
    <button class="btn sm" data-go-node>Cette machine</button></div>`;
}

/** La carte « Machines connectées », en tête de la vue Réseau. */
export function renderConnected(box, net) {
  box.hidden = !net.enabled;
  if (!net.enabled) return;
  const order = [...net.peers].sort((a, b) => (b.online - a.online) || (busy(b) - busy(a))
    || a.name.localeCompare(b.name, 'fr'));
  const rows = order.map(peer => `
    <tr class="${peer.online ? '' : 'off'}">
      <td class="live-name"><span class="dot ${peer.online ? 'on' : ''}"></span><b>${esc(peer.name)}</b>
        ${peer.trust === 'friend' ? '<span class="pill">ami</span>' : ''}</td>
      <td class="faint">${link(peer)}</td>
      <td><div class="caps">${lends(peer)}</div></td>
      <td class="live-now">${now(peer)}</td>
      <td class="faint">${exchanged(peer)}</td>
    </tr>`).join('');
  box.innerHTML = `
    <h2>${icon('bolt')}Machines connectées</h2>
    <p class="hint">En direct : qui est en ligne, qui prête sa puissance, et ce que chaque machine fait pour
      l'autre. Nouvelles toutes les 15 secondes environ.</p>
    ${net.peers.length ? `${nudge(net)}<div class="live-summary">${summary(net)}</div>
      <table class="live"><thead><tr><th>Machine</th><th>Liaison</th><th>Prête</th><th>En ce moment</th>
        <th>Échangé</th></tr></thead><tbody>${rows}</tbody></table>`
      : '<div class="empty" style="padding:14px">Aucune machine appairée : appaire tes machines plus bas.</div>'}`;
  box.querySelector('[data-go-node]')?.addEventListener('click', () => emit('view:show', 'node'));
}

/** Dans la barre du bas : machines en ligne (et qui travaille pour toi), un clic ouvre Réseau. */
export function mountRailStatus() {
  const status = $('#rail-status');
  const dot = $('#net-dot');
  status.onclick = () => {
    emit('view:show', 'network');
    $('#net-live')?.scrollIntoView({ block: 'start' });
  };
  on('network:loaded', () => {
    const net = state.network;
    const peers = net?.enabled ? net.peers : null;
    status.hidden = !peers;
    const online = (peers || []).filter(p => p.online);
    dot.hidden = !online.length;
    if (!peers) return;
    const helping = online.filter(p => sum(p.live?.for_me) || p.live?.chats_for_me).length;
    status.innerHTML = !peers.length ? 'Aucune machine appairée'
      : `<span class="dot ${online.length ? 'on' : ''}"></span>${plural(online.length, 'machine')} en ligne`
        + (helping ? ` <span class="work">${icon('bolt')}${helping} travaille${helping > 1 ? 'nt' : ''} pour toi</span>` : '');
    status.title = online.map(p => p.name).join(', ') || 'Aucune machine en ligne';
  });
}
