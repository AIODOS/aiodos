// « Machines connectées » (demandé par l'auteur le 29/10/2026) : toutes les machines que cette
// machine voit, réseau public, mes machines, amis, réseau local, chacune une seule fois
// (network/machines_view.py), dans un seul bloc dépliant en tête de la vue Réseau, à la place
// des listes d'avant (machines connectées, détectées, appairées, nœuds du réseau public). Son
// titre dit combien sont en ligne et, à côté, ce qui se passe (ce qui attend ta réponse, qui
// travaille pour toi, ce que tu fais avec le réseau public) ; dedans, des filtres (Toutes,
// Réseau public, Mes machines, Amis, Réseau local, avec leur nombre), une ligne courte par
// machine qui se déplie sur son détail et ses actions (machine-row.js), PAGE lignes puis
// « Voir les N autres ». Le filtre et l'état du bloc sont retenus (prefs : localStorage, sous
// try/catch) ; ce qui attend une réponse (un code à vérifier, une présentation, une machine
// mise de côté) est montré quel que soit le filtre, sa ligne dépliée, et ouvre le bloc une
// fois. Sous la liste, ce que cette machine fait avec le réseau public (le modèle et la durée,
// jamais le texte ni une clé). Dans la barre du bas, le nombre de machines en ligne. Les
// textes (t) sont du texte brut : échappés (esc) en entier, paramètres compris.
import { $, esc, duration, finite } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit, prefs } from '../state.js';
import { fold, keepFolds } from '../fold.js';
import { helpsMe, pauseDetail } from './machine-live.js';
import { machineHtml, machineAction, machineName } from './machine-row.js';

export { link } from './machine-live.js';      // les machines au travail (jobs-workers.js)

export const FILTERS = ['all', 'public', 'mine', 'friend', 'local'];
export const PAGE = 10;                          // lignes montrées avant « Voir les N autres »
const FOLD = 'net-machines';
const FILTER_KEYS = { all: 'connected.filter_all', public: 'connected.filter_public',
  mine: 'connected.filter_mine', friend: 'connected.filter_friend', local: 'connected.filter_local' };
const RAIL_NAMES = 12;                           // noms dans l'info-bulle de la barre du bas

let filter = FILTERS.includes(prefs.get('machines.filter', 'all')) ? prefs.get('machines.filter', 'all') : 'all';
let showAll = false;                             // « Voir les N autres » : jusqu'au prochain filtre
let current = [];                                // les lignes montrées, pour leurs actions
let noticed = '';                                // ce qui a ouvert le bloc d'office la dernière fois
const opened = new Set();                        // lignes dépliées
const closed = new Set();                        // lignes qui attendaient une réponse, repliées

// ------------------------------------------------------------- liste (sans DOM)
/** Les machines de l'état du réseau (des objets seulement). */
export const machinesOf = net => (Array.isArray(net?.machines)
  ? net.machines.filter(m => m && typeof m === 'object' && typeof m.id === 'string') : []);

/** Le nombre de machines de chaque filtre. */
export function filterCounts(machines) {
  const counts = Object.fromEntries(FILTERS.map(f => [f, 0]));
  counts.all = machines.length;
  for (const m of machines) if (m.kind !== 'all' && Object.hasOwn(counts, m.kind)) counts[m.kind] += 1;
  return counts;
}

/** Les lignes montrées : ce qui attend une réponse quel que soit le filtre, puis les machines
 *  du filtre, dans l'ordre donné par le nœud ; PAGE au plus, sauf « Voir les N autres ». */
export function pick(machines, chosen, all = false) {
  const picked = machines.filter(m => m.attention || chosen === 'all' || m.kind === chosen);
  return { rows: all ? picked : picked.slice(0, PAGE), hidden: all ? 0 : Math.max(0, picked.length - PAGE),
    total: picked.length };
}

const isOpen = m => opened.has(m.id) || (!!m.attention && !closed.has(m.id));

function filtersHtml(counts) {
  return FILTERS.map(f => `<button type="button" class="mx-filter${f === filter ? ' on' : ''}${counts[f] ? '' : ' none'}"
    data-filter="${f}" aria-pressed="${f === filter}">${esc(t(FILTER_KEYS[f]))}<span class="mx-n">${counts[f]}</span></button>`)
    .join('');
}

/** L'explication du filtre choisi (texte brut), ou rien. */
function filterHint(net) {
  if (filter === 'public') return t('connected.hint_public');
  if (filter === 'mine' || filter === 'friend') {
    return t('network.peers_hint', { offload: t(net.offload ? 'network.offload_on' : 'network.offload_off') });
  }
  if (filter === 'local') return t(net.running && net.local ? 'network.visible_hint' : 'network.visible_off');
  return '';
}

/** Rien à montrer dans ce filtre (texte brut). */
function emptyText(net) {
  if (filter === 'public') return t(net.public?.wanted ? 'network.public_none' : 'connected.public_off');
  if (filter === 'mine' || filter === 'friend') return t('network.no_peers');
  if (filter === 'local') return t(net.running && net.local ? 'network.no_visible' : 'network.visible_off');
  return t('connected.empty');
}

/** Le contenu de la liste (HTML) : les lignes, puis « Voir les N autres » ou « Voir moins ». */
export function listHtml(net, chosen = filter, all = showAll) {
  const { rows, hidden, total } = pick(machinesOf(net), chosen, all);
  current = rows;
  if (!rows.length) return `<div class="empty net-empty">${esc(emptyText(net))}</div>`;
  const more = hidden ? `<button type="button" class="btn sm ghost mx-more" data-more="1">${
    esc(t('connected.show_more', { n: hidden }))}</button>`
    : all && total > PAGE ? `<button type="button" class="btn sm ghost mx-more" data-more="0">${
      esc(t('connected.show_less'))}</button>` : '';
  return `${rows.map(m => machineHtml(m, net, isOpen(m))).join('')}${more}`;
}

// --------------------------------------------------- ce qui se passe, réseau public
const publicServing = net => (Array.isArray(net.public_live?.serving) ? net.public_live.serving : []);
const publicAsking = net => (Array.isArray(net.public_live?.asking) ? net.public_live.asking : []);

// Ce que cette machine fait avec le réseau public en ce moment (activity.public_live) : les
// conversations qu'elle sert à des inconnus, celles qu'elle leur fait calculer
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

/** À côté du titre (HTML) : ce qui attend ta réponse, qui travaille pour toi, pour qui tu
 *  travailles, le réseau public, et si cette machine prête la sienne. */
function summary(net, machines) {
  const chips = [];
  const waiting = machines.filter(m => m.attention).length;
  if (waiting) chips.push(`<span class="pill warn">${esc(t('connected.waiting_you', { n: waiting }))}</span>`);
  const helping = machines.filter(helpsMe).length;
  if (helping) chips.push(`<span class="pill run">${esc(t('connected.helping', { n: helping }))}</span>`);
  const helped = machines.filter(m => (Array.isArray(m.doing) ? m.doing : [])
    .some(d => d?.what === 'for_it' || d?.what === 'chat_for_it')).length;
  if (helped) chips.push(`<span class="pill run">${esc(t('connected.helped', { n: helped }))}</span>`);
  if (publicServing(net).length) chips.push(`<span class="pill run">${esc(t('connected.public_serving_chip'))}</span>`);
  if (publicAsking(net).length) chips.push(`<span class="pill run">${esc(t('connected.public_asking_chip'))}</span>`);
  const me = net.sharing;
  if (me) {
    chips.push(me.active ? `<span class="pill">${esc(t('connected.me_lending'))}</span>`
      : `<span class="pill idle" title="${esc(pauseDetail(me))}">${esc(t('connected.me_not_lending'))}</span>`);
  }
  return chips.join('');
}

// Réciprocité souple : une machine qui reçoit sans rien prêter en est avertie (sans blocage)
function nudge(net) {
  const peers = Array.isArray(net.peers) ? net.peers : [];
  const total = keys => peers.reduce((n, p) => n + keys.reduce((m, k) => m + (finite(p[k]) || 0), 0), 0);
  const received = total(['received_units', 'received_chats']);
  const given = total(['given_units', 'given_chats']);
  const s = state.settings || {};
  const hasModels = (state.models || []).some(m => m.group === 'local');
  if (received < 3 || given > 0 || s.share_enabled || (s.lend_models && hasModels)) return '';
  return `<div class="banner info live-nudge">${icon('bolt')}<div>${esc(t(hasModels ? 'connected.nudge'
    : 'connected.nudge_no_models', { n: received }))}</div>
    <button class="btn sm" data-go-node>${esc(t('nav.node'))}</button></div>`;
}

// ------------------------------------------------------------------------- bloc
const block = box => box.querySelector(`details.fold[data-fold="${FOLD}"]`);

/** Le bloc, une fois : ses parties, et les gestes (filtres, lignes, actions). */
function mount(box) {
  box.innerHTML = fold({ id: FOLD, title: t('connected.title'), iconName: 'bolt', open: true, cls: 'machines',
    body: `<div class="mx-nudge"></div>
      <div class="mx-filters" role="group" aria-label="${esc(t('connected.filters'))}"></div>
      <p class="hint mx-filter-hint" hidden></p>
      <div class="mx-list"></div>
      <div class="mx-public"></div>` });
  const d = block(box);
  $('summary', d).title = t('connected.hint');
  // Ouvert d'office (une réponse attendue) : pas retenu pour la suite, seuls les gestes le sont
  d.addEventListener('toggle', e => {
    if (d.dataset.auto) { delete d.dataset.auto; e.stopImmediatePropagation(); }
  });
  keepFolds(box);
  $('.mx-filters', d).addEventListener('click', e => {
    const chosen = e.target.closest('[data-filter]')?.dataset.filter;
    if (!chosen || !FILTERS.includes(chosen)) return;
    filter = chosen;
    showAll = false;
    prefs.set('machines.filter', chosen);
    draw(box, state.network);
  });
  const list = $('.mx-list', d);
  list.addEventListener('click', e => {
    const more = e.target.closest('[data-more]');
    if (more) { showAll = more.dataset.more === '1'; draw(box, state.network); return; }
    const button = e.target.closest('[data-act]');
    const id = button?.closest('[data-row]')?.dataset.row;
    const m = id && current.find(item => item.id === id);
    if (m) machineAction(button.dataset.act, m, state.network, button);
  });
  // Le dépli de chaque ligne est retenu d'une nouvelle à l'autre (la liste est redessinée)
  list.addEventListener('toggle', e => {
    const row = e.target;
    if (!row.matches?.('details.mx')) return;
    const id = row.dataset.row;
    if (row.open) { opened.add(id); closed.delete(id); } else {
      opened.delete(id);
      if (row.dataset.attn) closed.add(id);
    }
  }, true);
  $('.mx-nudge', d).addEventListener('click', e => {
    if (e.target.closest('[data-go-node]')) emit('view:show', 'node');
  });
}

function draw(box, net) {
  const d = block(box);
  if (!d || !net) return;
  const machines = net.enabled ? machinesOf(net) : [];
  const online = machines.filter(m => m.online).length;
  $('.fold-title', d).textContent = t('connected.title_count', { n: online });
  $('.fold-sub', d).innerHTML = net.enabled ? summary(net, machines) : '';
  const filters = $('.mx-filters', d);
  const hint = $('.mx-filter-hint', d);
  filters.hidden = !net.enabled;
  if (!net.enabled) {
    hint.hidden = true;
    $('.mx-nudge', d).innerHTML = $('.mx-public', d).innerHTML = '';
    $('.mx-list', d).innerHTML = `<p class="hint live-off">${esc(t('connected.network_off'))}</p>`;
    current = [];
    return;
  }
  filters.innerHTML = filtersHtml(filterCounts(machines));
  const help = filterHint(net);
  hint.hidden = !help;
  hint.textContent = help;
  $('.mx-nudge', d).innerHTML = nudge(net);
  $('.mx-list', d).innerHTML = listHtml(net);
  $('.mx-public', d).innerHTML = publicLive(net);
  // Une nouvelle réponse attendue ouvre le bloc, une fois : refermé, il le reste
  const attention = machines.filter(m => m.attention).map(m => m.id).join('|');
  if (attention && attention !== noticed && !d.open) { d.dataset.auto = '1'; d.open = true; }
  noticed = attention;
}

/** Le bloc « Machines connectées », en tête de la vue Réseau, toujours là (même réseau
 *  coupé) : créé la première fois, puis mis à jour à chaque nouvelle de l'état du réseau. */
export function renderConnected(box, net) {
  box.hidden = false;
  if (!block(box)) mount(box);
  draw(box, net);
}

/** Montre le bloc (ouvert), sur un filtre si on le dit (« local » : une machine ajoutée par
 *  son adresse y apparaît, prête à appairer). */
function show(kind) {
  const box = $('#net-live');
  const d = box && block(box);
  if (!d) return;
  if (FILTERS.includes(kind) && kind !== filter) {
    filter = kind;
    showAll = false;
    prefs.set('machines.filter', kind);
    draw(box, state.network);
  }
  d.open = true;
  d.scrollIntoView({ block: 'start' });
}

/** Dans la barre du bas : machines en ligne (et qui travaille pour toi), un clic ouvre le bloc. */
export function mountRailStatus() {
  const status = $('#rail-status');
  const dot = $('#net-dot');
  on('machines:show', show);
  status.onclick = () => {
    emit('view:show', 'network');
    show();
  };
  on('network:loaded', () => {
    const net = state.network;
    const machines = net?.enabled ? machinesOf(net) : null;
    status.hidden = !machines;
    const online = (machines || []).filter(m => m.online);
    dot.hidden = !online.length;
    if (!machines) return;
    const helping = online.filter(helpsMe).length;
    status.innerHTML = !online.length ? esc(t('connected.rail_none_online'))
      : `<span class="dot on"></span>${esc(t('connected.rail_online', { n: online.length }))}`
        + (helping ? ` <span class="work">${icon('bolt')}${esc(t('connected.helping', { n: helping }))}</span>` : '');
    const names = online.slice(0, RAIL_NAMES).map(machineName);
    if (online.length > RAIL_NAMES) names.push('…');
    status.title = names.join(', ') || t('connected.rail_none_online');
  });
}
