// Machines connectées, en direct, à la façon de la liste des pairs d'un client BitTorrent :
// qui est en ligne et par quel chemin, qui prête sa puissance, ce que chacune fait pour
// l'autre en ce moment (doublons de fin de partie compris), et ce qui a été échangé
// (network/activity.py) ; sous la liste, ce que cette machine fait avec le réseau public (les
// conversations qu'elle sert à des inconnus, celles qu'ils calculent pour elle : le modèle et
// la durée, jamais le texte ni une clé). En tête de la vue Réseau ; et dans la barre du bas,
// le nombre de machines en ligne. Les textes (t, tt) sont du texte brut : échappés (esc) en
// entier, paramètres compris, avant d'entrer dans le HTML.
import { $, esc, ago, duration, finite } from '../dom.js';
import { t, tt, locale } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';

const ROUTES = { lan: 'connected.route_lan', direct: 'connected.route_direct',
  relay: 'connected.route_relay', internet: 'connected.route_internet' };

const sum = items => (items || []).reduce((n, item) => n + (item.units || 0), 0);
// Prête sa puissance en ce moment ; d'un ami, on sait seulement si c'est activé
const lendsNow = sharing => !!sharing && ('active' in sharing ? sharing.active : sharing.enabled);
// Pourquoi une machine ne prête pas en ce moment (« En pause : ce PC est sur batterie. »),
// écrit chez elle : dans la langue de l'interface s'il est venu traduisible (detail_t), sinon
// tel qu'elle l'a écrit (une version précédente, ou un texte inconnu ici)
const pauseDetail = sharing => tt(sharing.detail_t, sharing.detail || '');

function busy(peer) {
  const live = peer.live || {};
  return sum(live.for_me) + sum(live.copies_for_me) + (live.for_it || []).length + (live.chats_for_me || 0)
    + (live.chats_for_it || 0);
}

// Elle travaille pour moi en ce moment : unités, doublons de fin de partie, conversations
const helpsMe = peer => !!(sum(peer.live?.for_me) || sum(peer.live?.copies_for_me) || peer.live?.chats_for_me);

export function link(peer) {           // aussi pour les machines au travail (jobs-workers.js)
  if (!peer.online) {
    return esc(peer.last_seen ? t('connected.offline_seen', { ago: ago(peer.last_seen) }) : t('connected.never_seen'));
  }
  const live = peer.live || {};
  const rtt = live.rtt_ms ?? live.internet_rtt_ms;
  return `${esc(t(ROUTES[live.route] || 'connected.online'))}${rtt != null ? ` · ${Math.round(rtt)} ms` : ''}`;
}

// Ce qu'elle prête : sa puissance (l'état du moment pour mes machines), ses modèles, sa carte
function lends(peer) {
  const p = peer.profile || {};
  const sharing = peer.live?.sharing;
  const pills = [];
  if (sharing ? sharing.enabled : p.images?.shares) {
    if (sharing && 'active' in sharing) {
      pills.push(sharing.active
        ? `<span class="pill ok">${esc(t('connected.lends_power'))}</span>`
        : `<span class="pill warn" title="${esc(pauseDetail(sharing))}">${esc(t('connected.power_paused'))}</span>`);
    } else {
      pills.push(`<span class="pill">${esc(t('connected.lends_power'))}</span>`);
    }
  }
  if (p.lends_models) pills.push(`<span class="pill">${esc(t('connected.lends_models'))}</span>`);
  if (p.cluster?.lends) pills.push(`<span class="pill">${esc(t('connected.lends_gpu'))}</span>`);
  return pills.length ? pills.join('') : `<span class="faint">${esc(t('connected.lends_nothing'))}</span>`;
}

// En ce moment, dans les deux sens
function now(peer) {
  const live = peer.live || {};
  const items = [];
  const forMe = sum(live.for_me);
  if (forMe) {
    // Le titre d'un de mes travaux : traduit s'il est venu traduisible (title_t, cf. jobs.js)
    const what = live.for_me.map(j => t('connected.job_units', { title: tt(j.title_t, j.title), n: j.units }))
      .join(', ');
    items.push(`<span class="live-in" title="${esc(what)}">${icon('bolt')}${esc(t('connected.computing_for_you',
      { n: forMe }))}</span>`);
  }
  const doubling = sum(live.copies_for_me);
  if (doubling) {
    const what = live.copies_for_me.map(j => t('connected.job_units', { title: tt(j.title_t, j.title), n: j.units }))
      .join(', ');
    items.push(`<span class="live-in" title="${esc(`${t('connected.doubling_title')} ${what}`)}">${icon('copy')}${
      esc(t('connected.doubling_for_you', { n: doubling }))}</span>`);
  }
  if (live.chats_for_me) {
    items.push(`<span class="live-in">${icon('chat')}${esc(t('connected.answering_your_chat'))}</span>`);
  }
  const forIt = live.for_it || [];
  if (forIt.length) {
    items.push(`<span class="live-out" title="${esc(forIt.join(', '))}">${icon('cpu')}${esc(t('connected.you_compute',
      { n: forIt.length }))}</span>`);
  }
  if (live.chats_for_it) {
    items.push(`<span class="live-out">${icon('chat')}${esc(t('connected.you_answer_its_chat'))}</span>`);
  }
  return items.length ? items.join('')
    : `<span class="faint">${peer.online ? esc(t('connected.nothing_now')) : '—'}</span>`;
}

// Échangé depuis l'appairage, dans chaque sens : le temps de calcul des tâches, puis les
// conversations auxquelles un modèle prêté a répondu
function exchanged(peer) {
  const compute = [];
  if (peer.received_s) {
    compute.push(`<span title="${esc(t('connected.received_units', { n: peer.received_units }))}">${
      esc(t('connected.received', { time: duration(peer.received_s) }))}</span>`);
  }
  if (peer.given_s) {
    compute.push(`<span title="${esc(t('connected.given_units', { n: peer.given_units }))}">${
      esc(t('connected.given', { time: duration(peer.given_s) }))}</span>`);
  }
  const chats = [];
  if (peer.received_chats) chats.push(t('connected.chats_for_you', { n: peer.received_chats }));
  if (peer.given_chats) chats.push(t('connected.chats_for_it', { n: peer.given_chats }));
  const lines = [];
  if (compute.length) lines.push(compute.join(' · '));
  // Fiabilité : ses résultats refusés (faux ou illisibles), les unités qu'elle n'a pas pu calculer
  const trouble = [];
  if (peer.refused_units) trouble.push(t('connected.refused_results', { n: peer.refused_units }));
  if (peer.failed_units) trouble.push(t('connected.failures', { n: peer.failed_units }));
  if (trouble.length) {
    lines.push(`<span class="live-trouble" title="${esc(t('connected.trouble_title'))}">${esc(trouble.join(' · '))}</span>`);
  }
  if (chats.length) {
    lines.push(`<span title="${esc(t('connected.chats_title'))}">${esc(t('connected.chats',
      { n: (peer.received_chats || 0) + (peer.given_chats || 0), list: chats.join(' · ') }))}</span>`);
  }
  return lines.length ? lines.join('<br>') : `<span class="faint">${esc(t('connected.nothing_yet'))}</span>`;
}

// Ce que cette machine fait avec le réseau public en ce moment (activity.public_live) : les
// conversations qu'elle sert à des inconnus, celles qu'elle leur fait calculer. Le modèle et la
// durée seulement : le nœud n'envoie ni le texte ni aucune clé
const publicServing = net => (Array.isArray(net.public_live?.serving) ? net.public_live.serving : []);
const publicAsking = net => (Array.isArray(net.public_live?.asking) ? net.public_live.asking : []);

function publicLive(net) {
  const serving = publicServing(net);
  const asking = publicAsking(net);
  if (!serving.length && !asking.length) return '';
  const now = Date.now() / 1000;
  const items = [
    ...serving.map(s => `<div class="live-out">${icon('chat')}<span>${esc(t('connected.public_serving', {
      model: String(s.model ?? ''), time: duration(now - (finite(s.since) ?? now)) }))}</span></div>`),
    ...asking.map(a => {
      const n = Number.isInteger(a.n) && a.n > 0 ? a.n : 1;
      return `<div class="live-in">${icon('chat')}<span>${esc(a.model
        ? t('connected.public_asking', { n, model: String(a.model) })
        : t('connected.public_asking_unknown', { n }))}</span></div>`;
    }),
  ];
  return `<div class="live-public"><h3 title="${esc(t('connected.public_hint'))}">${icon('globe')}${
    esc(t('connected.public_title'))}</h3>${items.join('')}</div>`;
}

function summary(net) {
  const peers = net.peers;
  const online = peers.filter(p => p.online);
  const helping = online.filter(helpsMe).length;
  const helped = online.filter(p => (p.live?.for_it || []).length || p.live?.chats_for_it).length;
  const lending = online.filter(p => lendsNow(p.live?.sharing)).length;
  const me = net.sharing;
  const chips = [`<span class="pill ${online.length ? 'ok' : 'idle'}">${esc(t('connected.online_of',
    { online: online.length, total: peers.length }))}</span>`];
  if (lending) chips.push(`<span class="pill ok">${esc(t('connected.lending', { n: lending }))}</span>`);
  if (helping) chips.push(`<span class="pill run">${esc(t('connected.helping', { n: helping }))}</span>`);
  if (helped) chips.push(`<span class="pill run">${esc(t('connected.helped', { n: helped }))}</span>`);
  if (publicServing(net).length) chips.push(`<span class="pill run">${esc(t('connected.public_serving_chip'))}</span>`);
  if (publicAsking(net).length) chips.push(`<span class="pill run">${esc(t('connected.public_asking_chip'))}</span>`);
  if (me) {
    chips.push(me.active ? `<span class="pill">${esc(t('connected.me_lending'))}</span>`
      : `<span class="pill idle" title="${esc(pauseDetail(me))}">${esc(t('connected.me_not_lending'))}</span>`);
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
  return `<div class="banner info live-nudge">${icon('bolt')}<div>${esc(t(hasModels ? 'connected.nudge'
    : 'connected.nudge_no_models', { n: received }))}</div>
    <button class="btn sm" data-go-node>${esc(t('nav.node'))}</button></div>`;
}

/** Info-bulle d'une machine appairée par une présentation (appairage par groupe). */
function introduced(peer, net) {
  if (!peer.introduced_by) return '';
  const by = net.peers.find(p => p.node_id === peer.introduced_by);
  return ` title="${esc(by ? t('connected.introduced_by', { name: by.name }) : t('connected.introduced_by_removed'))}"`;
}

/** La carte « Machines connectées », en tête de la vue Réseau, toujours ouverte (même réseau
 *  coupé) : le titre et, sur sa ligne, le résumé ; l'explication passe en info-bulle du titre. */
export function renderConnected(box, net) {
  box.hidden = false;
  const head = summaryHtml => `<div class="live-head"><h2 title="${esc(t('connected.hint'))}">${icon('bolt')}${
    esc(t('connected.title'))}</h2>${summaryHtml ? `<div class="live-summary">${summaryHtml}</div>` : ''}</div>`;
  if (!net.enabled) {
    box.innerHTML = `${head('')}<p class="hint live-off">${esc(t('connected.network_off'))}</p>`;
    return;
  }
  const order = [...net.peers].sort((a, b) => (b.online - a.online) || (busy(b) - busy(a))
    || a.name.localeCompare(b.name, locale()));
  const rows = order.map(peer => `
    <tr class="${peer.online ? '' : 'off'}">
      <td class="live-name"><span class="dot ${peer.online ? 'on' : ''}"></span><b${introduced(peer, net)}>${esc(peer.name)}</b>
        ${peer.trust === 'friend' ? `<span class="pill">${esc(t('connected.friend'))}</span>` : ''}</td>
      <td class="faint">${link(peer)}</td>
      <td><div class="caps">${lends(peer)}</div></td>
      <td class="live-now">${now(peer)}</td>
      <td class="faint">${exchanged(peer)}</td>
    </tr>`).join('');
  const columns = ['connected.col_machine', 'connected.col_link', 'connected.col_lends', 'connected.col_now',
    'connected.col_exchanged'].map(key => `<th>${esc(t(key))}</th>`).join('');
  box.innerHTML = `
    ${head(net.peers.length ? summary(net) : '')}
    ${net.peers.length ? `${nudge(net)}
      <table class="live"><thead><tr>${columns}</tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="hint live-off">${esc(t('connected.empty'))}</p>`}
    ${publicLive(net)}`;
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
    const helping = online.filter(helpsMe).length;
    status.innerHTML = !peers.length ? esc(t('connected.rail_no_peers'))
      : `<span class="dot ${online.length ? 'on' : ''}"></span>${esc(t('connected.rail_online', { n: online.length }))}`
        + (helping ? ` <span class="work">${icon('bolt')}${esc(t('connected.helping', { n: helping }))}</span>` : '');
    status.title = online.map(p => p.name).join(', ') || t('connected.rail_none_online');
  });
}
