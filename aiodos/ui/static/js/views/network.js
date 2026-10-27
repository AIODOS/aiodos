// Réseau : en tête, toujours ouvertes, les machines connectées (connected.js) ; dessous, des
// sections repliables d'une ligne (fold.js), chacune avec le résumé de son état : machines
// appairées, réseau local, machines détectées, Internet et invitations, réseau public. Fermées
// d'office, sauf quand quelque chose y demande l'attention (une erreur, un appairage en attente,
// une demande à confirmer). Les textes (t) sont du texte brut : échappés (esc) avant le HTML.
import { get, post } from '../api.js';
import { $, h, esc, ago, finite } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { toast, errorToast } from '../ui.js';
import { fold, keepFolds, openFold } from '../fold.js';
import { renderConnected } from './connected.js';
import { peersSummary, peersAttention, renderPeers } from './network-peers.js';
import { internetSummary, internetAttention, renderInternet, toggleInternet } from './network-internet.js';
import { startPairing, answerPairRequest } from './network-pairing.js';

export { peerFacts } from './network-peers.js';
export { answerPairRequest, answerIntroduction } from './network-pairing.js';

/** Le pare-feu de Windows demande son accord au premier réseau : pas sur un téléphone. */
const onWindows = () => (state.node?.os ? state.node.os.startsWith('Windows')
  : navigator.userAgent.includes('Windows'));

// Les sections, dans l'ordre : les machines, puis par où elles se joignent. toggle : le texte
// (pour les lecteurs d'écran) de l'interrupteur posé sur la ligne de titre
const SECTIONS = [
  { id: 'net-peers', title: 'network.peers_title', iconName: 'network' },
  { id: 'net-local', title: 'network.local_title', iconName: 'plug', toggle: 'network.local_title' },
  { id: 'net-visible', title: 'network.visible_title', iconName: 'search' },
  { id: 'net-internet', title: 'network.internet_fold', iconName: 'link', toggle: 'network.internet_title' },
  { id: 'net-public', title: 'network.public_fold', iconName: 'globe', toggle: 'network.public_title' },
];

let root;
let lastJson = '';
const noticed = new Map();      // section -> ce qui l'a ouverte d'office la dernière fois

export function mountNetwork(container) {
  // Ajouter une machine par son adresse : hors de ce qui est redessiné, la saisie reste
  const add = `
    <div class="net-add">
      <p class="hint">${esc(t('network.add_hint'))}</p>
      <div class="toolbar net-tools">
        <input class="input" id="net-address" placeholder="${esc(t('network.address_placeholder'))}">
        <button class="btn" id="net-add">${icon('plus')}${esc(t('network.add'))}</button>
      </div>
    </div>`;
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head"><div><h1>${esc(t('network.title'))}</h1>
        <div class="sub">${esc(t('network.intro'))}</div></div></div>
      <div class="card" id="net-live" hidden></div>
      <div class="net-folds">${SECTIONS.map(s => fold({ id: s.id, title: t(s.title), iconName: s.iconName,
        body: `<div class="card"><div class="net-part"></div>${s.id === 'net-local' ? add : ''}</div>` }))
        .join('')}</div>
    </div></div>`);
  for (const s of SECTIONS) {
    const d = section(s.id);
    // Ouverte d'office (attention) : pas retenue pour la suite, seuls les gestes le sont
    d.addEventListener('toggle', e => {
      if (d.dataset.auto) { delete d.dataset.auto; e.stopImmediatePropagation(); }
    });
    if (s.toggle) {
      const sw = h(`<label class="switch net-switch"><input type="checkbox" aria-label="${esc(t(s.toggle))}"><span></span></label>`);
      $('summary', d).insertBefore(sw, $('.fold-chev', d));
    }
  }
  keepFolds(root);
  $('.net-switch input', section('net-local')).onchange = toggleLocal;
  $('.net-switch input', section('net-internet')).onchange = toggleInternet;
  $('.net-switch input', section('net-public')).onchange = togglePublic;
  $('#net-add', root).onclick = addByAddress;
  $('#net-address', root).onkeydown = e => { if (e.key === 'Enter') addByAddress(); };
  container.append(root);
  on('network:changed', loadNetwork);
  on('network:reload', reloadNetwork);          // après une action : redessiner, même sans changement
  on('view:shown', view => { if (view === 'network') loadNetwork(); });
  setInterval(() => { if (state.view === 'network' && !document.hidden) loadNetwork(); }, 5000);
}

export async function loadNetwork() {
  try {
    const net = await get('/network');
    state.network = net;
    emit('network:loaded');
    const json = JSON.stringify(net);
    if (json !== lastJson) { lastJson = json; render(); }   // pas de reconstruction inutile
  } catch { /* nœud indisponible : on réessaiera */ }
}

function reloadNetwork() {
  lastJson = '';
  return loadNetwork();
}

const section = id => $(`details.fold[data-fold="${id}"]`, root);

function render() {
  const net = state.network;
  if (!root || !net) return;
  renderConnected($('#net-live', root), net);
  const local = net.local ?? net.enabled;      // réseau local coupé, Internet peut tourner
  update('net-peers', { sub: peersSummary(net), attention: peersAttention(net),
    draw: box => renderPeers(box, net) });
  update('net-local', { sub: localSummary(net, local), attention: net.error || '', checked: local,
    draw: box => renderLocal(box, net, local) });
  $('.net-add', root).hidden = !(net.running && local);
  update('net-visible', { sub: visibleSummary(net, local),
    attention: (net.pairing || []).map(p => p.ref || p.node_id).join('|'),
    draw: box => renderVisible(box, net, local) });
  update('net-internet', { sub: internetSummary(net), attention: internetAttention(net),
    checked: net.internet?.enabled, draw: box => renderInternet(box, net) });
  const pub = net.public;
  section('net-public').hidden = !pub || !net.enabled;
  if (pub && net.enabled) {
    update('net-public', { sub: publicSummary(pub), attention: pub.wanted && pub.error ? pub.error : '',
      checked: pub.wanted, disabled: !(pub.internet || pub.wanted), draw: box => renderPublic(box, pub) });
  }
}

/** Une section : son résumé, son interrupteur, son contenu ; ouverte d'office quand `attention`
 *  change (une erreur, un appairage en attente…), une fois : refermée, elle le reste. */
function update(id, { sub, attention = '', checked, disabled = false, draw }) {
  const d = section(id);
  $('.fold-sub', d).innerHTML = sub;
  const sw = $('.net-switch input', d);
  if (sw) { sw.checked = !!checked; sw.disabled = disabled; }
  draw($('.net-part', d));
  if (attention && attention !== noticed.get(id) && !d.open) { d.dataset.auto = '1'; d.open = true; }
  noticed.set(id, attention);
}

const warn = text => `<span class="warn">${esc(text)}</span>`;

// --------------------------------------------------------------- réseau local
function localSummary(net, local) {
  if (!local) return esc(t('network.sub_off'));
  if (net.error) return warn(net.error);
  return esc(net.running ? t('network.local_sub_on', { port: net.port }) : t('network.sub_starting'));
}

function renderLocal(box, net, local) {
  const detail = !local
    ? t(net.internet?.enabled ? 'network.local_off_internet' : 'network.local_off_nothing')
    : net.running
      ? t(net.discovery ? 'network.local_visible_discovery' : 'network.local_visible',
        { name: net.name, port: net.port })
      : t('network.starting');
  box.innerHTML = `
    <p class="hint">${esc(detail)}</p>
    ${net.error ? `<div class="banner warn net-banner">${icon('warn')}<div>${esc(net.error)}</div></div>` : ''}
    ${local || !onWindows() ? '' : `<div class="phase-note net-note">${icon('shield')}<div>${t('network.firewall_html')}</div></div>`}`;
}

async function toggleLocal(e) {
  try { state.network = await post('/network/enable', { enabled: e.target.checked }); }
  catch (err) { errorToast(err); }
  reloadNetwork();                // l'interrupteur reprend l'état réel, réussi ou non
}

async function addByAddress() {
  const input = $('#net-address', root);
  const address = input.value.trim();
  if (!address) return;
  try {
    const info = await post('/network/add', { address });
    toast(t('network.found', { name: info.name }), 'ok');
    input.value = '';
    openFold(root, 'net-visible');        // elle y apparaît, prête à appairer
    reloadNetwork();
  } catch (e) { errorToast(e); }
}

// ----------------------------------------------------------- machines détectées
function visibleSummary(net, local) {
  const pairing = net.pairing || [];
  const here = pairing.filter(p => !p.local_ok).length;     // à confirmer sur cette machine
  const pending = new Set(pairing.map(p => p.node_id));
  const visible = net.visible.filter(v => !pending.has(v.node_id)).length;
  const parts = [esc(!net.running || !local ? t('network.visible_sub_off')
    : visible ? t('network.visible_sub', { n: visible }) : t('network.visible_sub_none'))];
  if (pairing.length) {
    parts.push(`<span class="pill ${here ? 'warn' : 'run'}">${esc(here
      ? t('network.pairing_to_confirm', { n: here }) : t('network.pairing_pending', { n: pairing.length }))}</span>`);
  }
  return parts.join(' · ');
}

// Les appairages en cours (réseau local ou invitation), puis les machines qui s'annoncent
function renderVisible(box, net, local) {
  const on = net.running && local;
  box.innerHTML = `<p class="hint">${esc(t(on ? 'network.visible_hint' : 'network.visible_off'))}</p>
    <div class="peer-list"></div>`;
  const list = $('.peer-list', box);
  for (const p of net.pairing) {
    const facts = [t('network.pairing_code', { code: p.code })];
    if (p.internet) facts.push(t('network.via_internet'));
    else if (p.address) facts.push(p.address);
    facts.push(t(p.local_ok ? 'network.pairing_waiting' : 'network.pairing_confirm_here'));
    const row = h(`<div class="peer"><div class="peer-head">${icon('shield')}<b>${esc(p.name)}</b>
      <span class="faint">${esc(facts.join(' · '))}</span></div></div>`);
    if (!p.local_ok) {
      const btn = h(`<button class="btn sm">${esc(t('network.check_code'))}</button>`);
      btn.onclick = () => answerPairRequest(p);       // avec sa « ref » : la réponse vise cette demande
      row.querySelector('.peer-head').append(btn);
    }
    list.append(row);
  }
  if (!on) return;
  const pendingIds = new Set(net.pairing.map(p => p.node_id));
  for (const v of net.visible.filter(v => !pendingIds.has(v.node_id))) {
    const row = h(`
      <div class="peer"><div class="peer-head">
        <span class="dot on"></span><b>${esc(v.name)}</b>
        <span class="faint">${esc(v.address)}:${esc(v.port)}</span><span class="net-grow"></span>
        <button class="btn sm primary">${icon('shield')}${esc(t('network.pair'))}</button>
      </div></div>`);
    row.querySelector('button').onclick = () => startPairing(v);
    list.append(row);
  }
  if (!list.children.length) {
    list.append(h(`<div class="empty net-empty">${esc(t('network.no_visible'))}</div>`));
  }
}

// --------------------------------------------------------- réseau public (essai)
const shortKey = key => (key ? `${key.slice(0, 6)}…${key.slice(-4)}` : '');

function publicSummary(pub) {
  const status = !pub.wanted ? esc(t('network.sub_off'))
    : !pub.internet ? warn(t('network.public_sub_needs_internet'))
      : pub.error ? warn(pub.error)
        : !pub.joined ? esc(t('network.sub_connecting'))
          : esc(t('network.public_count', { n: finite(pub.count) ?? 0 }));
  return `<span class="pill">${esc(t('network.public_trial'))}</span> ${status}`;
}

// Ce que disent les nœuds du réseau public vient d'inconnus : typé avant d'entrer dans le HTML
function publicNode(n) {
  const models = Array.isArray(n.models) ? n.models.map(String) : [];
  const key = typeof n.key === 'string' ? n.key : '';
  const seen = ago(Date.now() / 1000 - (finite(n.seen_s) ?? 0));
  return `<div class="peer"><div class="peer-head"><span class="dot on"></span>
      <code title="${esc(key)}">${esc(shortKey(key))}</code>
      <span class="faint">AIODOS ${esc(n.version || '?')} · ${esc(t('network.public_seen', { ago: seen }))}</span></div>
    <div class="peer-facts">${esc(models.length ? models.join(', ') : t('network.public_no_models'))}</div></div>`;
}

/** « 3 autres nœuds publics en ligne · 2 voisins directs », en texte brut. */
function publicCounts(pub) {
  const facts = [t('network.public_count', { n: finite(pub.count) ?? 0 }),
    t('network.public_neighbors', { n: finite(pub.neighbors) ?? 0 })];
  if (pub.rendezvous?.error) facts.push(t('network.public_directory', { error: pub.rendezvous.error }));
  return facts.join(' · ');
}

function renderPublic(box, pub) {
  const nodes = Array.isArray(pub.nodes) ? pub.nodes : [];
  const detail = !pub.wanted ? ''
    : !pub.internet ? `<div class="hint">${esc(t('network.public_needs_internet'))}</div>`
      : pub.error ? `<div class="banner warn net-banner">${icon('warn')}<div>${esc(pub.error)}</div></div>`
        : !pub.joined ? `<div class="faint">${esc(t('network.public_connecting'))}</div>`
          : `<div class="peer-facts net-key">${esc(t('network.public_key'))}
              <code title="${esc(pub.key)}">${esc(shortKey(String(pub.key || '')))}</code> · ${esc(publicCounts(pub))}</div>
            <div class="peer-list">${nodes.length ? nodes.map(publicNode).join('')
              : `<div class="empty net-empty">${esc(t('network.public_none'))}</div>`}</div>`;
  box.innerHTML = `<p class="hint">${esc(t('network.public_help'))}</p>${detail}`;
}

async function togglePublic(e) {
  try { state.network = await post('/network/public', { enabled: e.target.checked }); }
  catch (err) { errorToast(err); }
  reloadNetwork();
}
