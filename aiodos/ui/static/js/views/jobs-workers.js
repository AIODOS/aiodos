// Tâches : les machines au travail en ce moment, dans un panneau repliable en bas de la vue, à
// la façon de la liste des pairs d'un client BitTorrent. Cette machine (les unités de mes
// travaux calculées ici, celles qu'elle calcule pour d'autres), puis mes machines appairées qui
// calculent pour moi : ce qu'elle calcule, son état, le chemin qui y mène, son temps moyen par
// unité. Tout vient de ce que l'interface a déjà : state.jobs (local, remote, et les doublons de
// fin de partie : copies, copies_remote), state.executor (remote : les unités d'autres machines
// calculées ici) et state.network (cf. connected.js ; live.places : les places qu'une machine
// annonce en demandant du travail, en petits points sous son nom, comme chantier.js).
import { esc, duration, ago } from '../dom.js';
import { fold, keepFolds } from '../fold.js';
import { t, tt, locale } from '../i18n.js';
import { state } from '../state.js';
import { placeDots } from './chantier.js';
import { link } from './connected.js';

let box = null;
let shown = '';                          // dernier contenu posé : rien n'est refait pour rien

// Le titre d'un travail, comme jobTitle (jobs.js), sans import croisé
const titleOf = j => tt(j?.title_t, j?.title || '');
const findPeer = id => state.network?.peers?.find(p => p.node_id === id);

/** Le nom d'une machine appairée, d'après la liste du réseau. */
export function peerName(id) {
  return findPeer(id)?.name || t('jobs.another_machine');
}

/** Le panneau, replié d'abord, puis comme on l'a laissé (fold.js) : son titre résume déjà. */
export function mountWorkers(container) {
  box = container;
  box.innerHTML = fold({ id: 'jobs-workers', title: t('jobs.workers_title'), iconName: 'network' });
  keepFolds(box);
  shown = '';
}

// Cette machine d'abord, puis celles qui calculent pour moi, les plus chargées en tête
function machines() {
  const self = { self: true, jobs: [], copies: [], lent: new Map(), units: 0 };
  const others = new Map();                          // machine -> { id, peer, jobs, copies, units }
  const other = id => {
    if (!others.has(id)) others.set(id, { id, peer: findPeer(id), jobs: [], copies: [], units: 0 });
    return others.get(id);
  };
  for (const j of state.jobs?.values() || []) {
    if (j.local) {
      self.jobs.push([j, j.local]);
      self.units += j.local;
    }
    for (const [id, n] of Object.entries(j.remote || {})) {
      const m = other(id);
      m.jobs.push([j, n]);
      m.units += n;
    }
    // Doublons de fin de partie : ailleurs (copies_remote), et le reste ici
    let away = 0;
    for (const [id, n] of Object.entries(j.copies_remote || {})) {
      const m = other(id);
      m.copies.push([j, n]);
      m.units += n;
      away += n;
    }
    if ((j.copies || 0) > away) {
      self.copies.push([j, j.copies - away]);
      self.units += j.copies - away;
    }
  }
  for (const w of state.executor?.remote || []) {    // unités d'autres machines calculées ici
    const lent = self.lent.get(w.peer) || { name: w.peer_name, labels: [] };
    lent.labels.push(w.label);
    self.lent.set(w.peer, lent);
    self.units += 1;
  }
  const name = m => m.peer?.name || '';
  return [self, ...[...others.values()].sort((a, b) => (b.units - a.units)
    || name(a).localeCompare(name(b), locale()))];
}

const pill = (cls, key, title = '') =>
  `<span class="pill ${cls}"${title ? ` title="${esc(title)}"` : ''}>${esc(t(key))}</span>`;

// Ce qu'elle calcule : unités de mes travaux, et pour cette machine celles prêtées à d'autres
function what(m) {
  const lines = m.jobs.map(([j, n]) => esc(t('connected.job_units', { title: titleOf(j), n })));
  for (const [j, n] of m.copies || []) {
    lines.push(`<span class="workers-copy" title="${esc(t('chantier.doubled_title'))}">${
      esc(t('chantier.workers_copies', { title: titleOf(j), n }))}</span>`);
  }
  for (const lent of m.lent?.values() || []) {
    lines.push(`<span title="${esc(lent.labels.join(', '))}">${esc(t('jobs.computing_for',
      { name: lent.name, n: lent.labels.length }))}</span>`);
  }
  return lines.length ? lines.map(line => `<div>${line}</div>`).join('')
    : `<span class="faint">${esc(t('connected.nothing_now'))}</span>`;
}

// Son état : une machine qui ne répond plus garde ses unités jusqu'à l'échéance de leur bail ;
// cette machine peut attendre (file en pause, batterie) ou se reposer
function stateOf(m) {
  if (!m.self) {
    return m.peer && !m.peer.online
      ? pill('warn', 'jobs.workers_unreachable', t('jobs.workers_unreachable_hint'))
      : pill('run', 'jobs.workers_computing');
  }
  const ex = state.executor || {};
  const busy = Object.values(ex.busy || {}).reduce((n, k) => n + k, 0);
  if (m.units || busy) return pill('run', 'jobs.workers_computing');
  if (state.queuePause) return pill('warn', 'jobs.workers_paused');
  if (ex.pacing?.on_battery && ex.pacing.mode === 'pause') {
    return pill('warn', 'jobs.battery_waiting', ex.pacing.detail || '');
  }
  return pill('idle', 'jobs.workers_idle');
}

// Son temps moyen par unité calculée pour moi, depuis l'appairage (peers : received_s, _units)
function speed(m) {
  const p = m.peer;
  if (!p?.received_units || !p.received_s) return '';
  return `<span title="${esc(t('jobs.workers_per_unit_title', { n: p.received_units }))}">${
    esc(t('jobs.workers_per_unit', { time: duration(p.received_s / p.received_units) }))}</span>`;
}

// Une cellule sans rien à dire : un tiret (caché sur téléphone)
const cell = html => (html ? `<td class="faint">${html}</td>` : '<td class="faint workers-none">—</td>');

// Ses places, et combien sont prises, telles qu'elle les a dites en demandant du travail
// (network/places_seen.py) ; celles de cette machine sont sur la ligne des ressources, en haut
function places(m) {
  const seen = m.self ? null : m.peer?.live?.places;
  const dots = seen ? placeDots(seen.places, seen.busy) : '';
  if (!dots) return '';
  const at = typeof seen.at === 'number' && Number.isFinite(seen.at) ? seen.at : null;
  return `<div class="workers-places"${at !== null ? ` title="${esc(t('chantier.places_title', { ago: ago(at) }))}"` : ''}>${dots}</div>`;
}

function row(m) {
  const me = state.network?.name;
  const name = m.self ? t('jobs.workers_here') : m.peer?.name || t('jobs.another_machine');
  const online = m.self || !!m.peer?.online;
  return `<tr>
    <td class="workers-name"><span class="dot ${online ? 'on' : ''}"></span><b${m.self && me
      ? ` title="${esc(me)}"` : ''}>${esc(name)}</b>${places(m)}</td>
    <td class="workers-what">${what(m)}</td>
    <td>${stateOf(m)}</td>
    ${cell(!m.self && m.peer ? link(m.peer) : '')}
    ${cell(speed(m))}
  </tr>`;
}

// Sous la liste : le réseau éteint, ou les machines en ligne qui n'ont rien à faire
function foot(list) {
  const net = state.network;
  if (!net) return '';
  if (!net.enabled) return `<p class="workers-foot">${esc(t('jobs.workers_network_off'))}</p>`;
  const working = new Set(list.map(m => m.id));
  const idle = (net.peers || []).filter(p => p.online && !working.has(p.node_id)).length;
  return idle ? `<p class="workers-foot">${esc(t('jobs.workers_idle_peers', { n: idle }))}</p>` : '';
}

/** Met le panneau à jour (appelé à chaque rendu de la vue Tâches). */
export function renderWorkers() {
  const details = box?.querySelector('details.fold');
  if (!details) return;
  const list = machines();
  const units = list.reduce((n, m) => n + m.units, 0);
  const sub = units ? `${t('jobs.workers_machines', { n: list.filter(m => m.units).length })} · ${
    t('jobs.workers_units', { n: units })}` : t('jobs.workers_none');
  const columns = ['jobs.workers_col_machine', 'jobs.workers_col_computing', 'jobs.workers_col_state',
    'jobs.workers_col_link', 'jobs.workers_col_speed'].map(key => `<th>${esc(t(key))}</th>`).join('');
  const body = `<table class="workers"><thead><tr>${columns}</tr></thead>
    <tbody>${list.map(row).join('')}</tbody></table>${foot(list)}`;
  if (sub + body === shown) return;
  shown = sub + body;
  details.querySelector('.fold-sub').textContent = sub;
  details.querySelector('.fold-body').innerHTML = body;
}
