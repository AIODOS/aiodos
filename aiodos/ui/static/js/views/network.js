// Réseau : en tête, le bloc « Machines connectées » (connected.js), toutes les machines en une
// liste (réseau public, mes machines, amis, réseau local), chacune une fois ; dessous, les
// réglages, en sections repliables d'une ligne (fold.js), chacune avec le résumé de son état :
// réseau public, Internet et invitations, réseau local (et l'ajout d'une machine par son
// adresse). Leurs machines sont toutes dans le bloc, plus dans les sections (demandé par
// l'auteur le 29/10/2026). Une section est fermée d'office, sauf quand quelque chose y demande
// l'attention (une erreur, une invitation en cours). Les textes (t) sont du texte brut :
// échappés (esc) avant le HTML.
import { get, post } from '../api.js';
import { $, h, esc, finite } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { toast, errorToast } from '../ui.js';
import { fold, keepFolds } from '../fold.js';
import { renderConnected } from './connected.js';
import { internetSummary, internetAttention, renderInternet, toggleInternet } from './network-internet.js';

export { peerFacts } from './network-peers.js';
export { answerPairRequest, answerIntroduction } from './network-pairing.js';

/** Le pare-feu de Windows demande son accord au premier réseau : pas sur un téléphone. */
const onWindows = () => (state.node?.os ? state.node.os.startsWith('Windows')
  : navigator.userAgent.includes('Windows'));

// Les sections de réglage, dans l'ordre : le réseau public et Internet d'abord (AIODOS relie
// par Internet les machines d'inconnus, demandé par l'auteur le 27/10/2026), le réseau local en
// dernier. toggle : le texte (pour les lecteurs d'écran) de l'interrupteur posé sur la ligne de
// titre ; open : dépliée tant qu'on ne l'a pas repliée
const SECTIONS = [
  { id: 'net-public', title: 'network.public_fold', iconName: 'globe', toggle: 'network.public_title',
    open: true },
  { id: 'net-internet', title: 'network.internet_fold', iconName: 'link', toggle: 'network.internet_title' },
  { id: 'net-local', title: 'network.local_title', iconName: 'plug', toggle: 'network.local_title' },
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
      <div class="net-machines" id="net-live" hidden></div>
      <div class="net-folds">${SECTIONS.map(s => fold({ id: s.id, title: t(s.title), iconName: s.iconName,
        open: s.open, body: `<div class="card"><div class="net-part"></div>${s.id === 'net-local' ? add : ''}</div>` }))
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
  update('net-local', { sub: localSummary(net, local), attention: net.error || '', checked: local,
    draw: box => renderLocal(box, net, local) });
  $('.net-add', root).hidden = !(net.running && local);
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
 *  change (une erreur, une invitation en cours…), une fois : refermée, elle le reste. */
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
    emit('machines:show', 'local');       // dans « Machines connectées », prête à appairer
    reloadNetwork();
  } catch (e) { errorToast(e); }
}

// --------------------------------------------------------- réseau public (essai)
const shortKey = key => (key ? `${key.slice(0, 6)}…${key.slice(-4)}` : '');

// La première fois, le compagnon cherche la clé du nœud public (une preuve de travail : une
// dizaine de secondes sur un téléphone) : « preparing », dit au lieu de « connexion… »
const publicJoining = pub => t(pub.preparing ? 'network.public_preparing' : 'network.sub_connecting');

function publicSummary(pub) {
  const status = !pub.wanted ? esc(t('network.sub_off'))
    : !pub.internet ? warn(t('network.public_sub_needs_internet'))
      : pub.error ? warn(pub.error)
        : !pub.joined ? esc(publicJoining(pub))
          : esc(t('network.public_count', { n: finite(pub.count) ?? 0 }));
  return `<span class="pill">${esc(t('network.public_trial'))}</span> ${status}`;
}

/** « 3 autres nœuds publics en ligne · 2 voisins directs », en texte brut. */
function publicCounts(pub) {
  const facts = [t('network.public_count', { n: finite(pub.count) ?? 0 }),
    t('network.public_neighbors', { n: finite(pub.neighbors) ?? 0 })];
  if (pub.rendezvous?.error) facts.push(t('network.public_directory', { error: pub.rendezvous.error }));
  return facts.join(' · ');
}

// Les nœuds vus sont dans « Machines connectées » (filtre Réseau public), où le bouton les montre
function renderPublic(box, pub) {
  const detail = !pub.wanted ? ''
    : !pub.internet ? `<div class="hint">${esc(t('network.public_needs_internet'))}</div>`
      : pub.error ? `<div class="banner warn net-banner">${icon('warn')}<div>${esc(pub.error)}</div></div>`
        : !pub.joined ? `<div class="faint">${esc(t(pub.preparing ? 'network.public_preparing'
          : 'network.public_connecting'))}</div>`
          : `<div class="peer-facts net-key">${esc(t('network.public_key'))}
              <code title="${esc(pub.key)}">${esc(shortKey(String(pub.key || '')))}</code> · ${esc(publicCounts(pub))}</div>
            ${finite(pub.count) ? `<button type="button" class="btn sm" data-show-public>${icon('globe')}${
              esc(t('network.public_show'))}</button>` : ''}`;
  box.innerHTML = `<p class="hint">${esc(t('network.public_help'))}</p>${detail}`;
  box.querySelector('[data-show-public]')?.addEventListener('click', () => emit('machines:show', 'public'));
}

async function togglePublic(e) {
  try { state.network = await post('/network/public', { enabled: e.target.checked }); }
  catch (err) { errorToast(err); }
  reloadNetwork();
}
