// Cette machine : profil du nœud, modèles, préférences de partage de puissance.
import { get, post, put } from '../api.js';
import { $, h, esc, bytes, duration, powerText } from '../dom.js';
import { fold } from '../fold.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { ask, toast, errorToast } from '../ui.js';
import { loadNetwork } from './network.js';
import { chooseModel, here, integrated } from './model-install.js';

// Capacités affichées (libellés : clés traduites à l'affichage)
const CAPS = { vision: 'node.cap_vision', thinking: 'node.cap_thinking', tools: 'node.cap_tools',
  embedding: 'node.cap_embedding' };
const RESOURCE_ICONS = { gpu: 'image', embed: 'layers', cpu: 'cpu', net: 'globe' };
// Le profil reste visible ; le reste en sections repliables (leur titre remplace celui de la carte)
const PARTS = [['node-share', 'node.share_title', 'bolt'], ['node-background', 'node.background_title', 'cpu'],
  ['node-models', 'node.models_title', 'layers'], ['node-network', 'node.network_title', 'network']];
let root;
let shareMobile = null;            // préférences dessinées pour un téléphone ?
let trayRow = null;                // réglage de la zone de notification dessiné ?
let autostart = null;              // démarrage avec l'appareil (node/autostart.py)

/** Cette machine, dans sa section des Réglages (settings.js) : le profil, le partage, le travail
 *  de fond, les modèles, le réseau. Rafraîchie tant que sa section est ouverte. */
export function mountNode(container) {
  root = h(`
    <div class="node-panel">
      <div class="sub node-intro">${esc(t('node.intro'))}</div>
      <div class="card" id="node-machine"></div>
      ${PARTS.map(([id, title, name]) => fold({ id, title: t(title), iconName: name,
        body: `<div id="${id}"></div>` })).join('')}
    </div>`);
  container.append(root);
  on('view:shown', view => { if (view === 'settings' && shown()) refreshNode(); });
  on('settings:loaded', () => { renderShare(); renderBackground(); });
  on('network:loaded', () => { renderNetworkSummary(); renderShareStatus(); });
  on('executor:changed', () => { renderShareStatus(); renderBackgroundStatus(); });
  window.addEventListener('pywebviewready', renderBackground);
  // Fenêtre cachée ou réduite : rien à rafraîchir (le profil lance nvidia-smi)
  setInterval(() => { if (shown() && !document.hidden) { loadNode(); loadNetwork(); } }, 5000);
}

/** Visible : les Réglages ouverts, et la section Cette machine dépliée. */
const shown = () => state.view === 'settings' && !!root?.closest('details')?.open;

/** Tout relire : à l'ouverture de sa section, ou des Réglages. */
export function refreshNode() {
  loadNode();
  loadExecutor();
  loadAutostart();
}

export async function loadNode() {
  try {
    state.node = await get('/node');
    emit('node:loaded');
    if (root) render();
  } catch (e) { /* nœud indisponible : on réessaiera */ }
}

// Ce qu'AIODOS n'a pas pu fermer aux autres comptes de la machine (paths.left_open) :
// l'avertissement du journal ne se voit ni depuis la fenêtre ni depuis l'appli
function leftOpen(list) {
  const items = Array.isArray(list) ? list.filter(x => typeof x === 'string') : [];
  if (!items.length) return '';
  const shown = items.slice(0, 5).map(x => `<span class="mono">${esc(x)}</span>`).join(', ');
  return `<div class="banner warn" style="margin-top:10px">${icon('warn')}
    ${esc(t('node.left_open', { n: items.length }))} ${shown}${
    items.length > 5 ? ' …' : ''}. ${esc(t('node.left_open_fix'))}</div>`;
}

function gauge(used, total, label) {
  const pct = total ? Math.round(100 * used / total) : 0;
  return `<div class="gauge">${label}<div class="bar"><i class="fill" style="width:${pct}%"></i></div>
    <small>${bytes(used)} / ${bytes(total)} · ${esc(t('node.percent', { n: pct }))}</small></div>`;
}

function render() {
  const n = state.node;
  if (!n) return;
  const mem = n.memory;
  const gpus = n.gpus.length
    ? n.gpus.map(g => gauge(g.vram_used, g.vram_total,
        `${esc(g.name)} <span class="faint">· ${esc(t('node.gpu_use', { n: g.utilization }))}</span>`)).join('')
    : `<span class="muted">${esc(t('node.no_gpu'))}</span>`;
  const idle = n.idle_seconds == null ? (n.power?.mobile ? t('node.idle_phone') : '—')
    : n.idle_seconds < 60 ? t('node.in_use') : t('node.idle_for', { time: duration(n.idle_seconds) });
  const power = powerText(n);
  $('#node-machine', root).innerHTML = `
    <h2>${icon('node')}${esc(n.name)}
      <button class="icon-btn" id="rename-node" title="${esc(t('node.rename_title'))}">${icon('edit')}</button></h2>
    <p class="hint">${esc(t('node.node'))} <span class="mono">${esc(n.node_id.slice(0, 12))}</span> · AIODOS ${esc(n.version)}</p>
    <dl class="facts">
      <dt>${esc(t('node.system'))}</dt><dd>${esc(n.os)}</dd>
      <dt>${esc(t('node.processor'))}</dt><dd>${esc(n.cpu.name)} <span class="faint">· ${n.cpu.threads} threads</span></dd>
      <dt>${esc(t('node.memory'))}</dt><dd>${gauge(mem.total - mem.available, mem.total, '')}</dd>
      <dt>GPU</dt><dd>${gpus}</dd>
      <dt>${esc(t('node.power'))}</dt><dd>${power}</dd>
      <dt>${esc(t('node.activity'))}</dt><dd>${esc(idle)}</dd>
      <dt>${integrated() ? esc(t('node.engine')) : 'Ollama'}</dt><dd>${n.ollama.version
        ? (integrated() ? esc(t('node.engine_version', { version: n.ollama.version }))
          : `version ${esc(n.ollama.version)}`)
        : `<span style="color:var(--err)">${esc(t('node.not_responding'))}</span>`}</dd>
    </dl>${leftOpen(n.left_open)}`;
  $('#rename-node', root).onclick = renameNode;

  const vram = Math.max(0, ...n.gpus.map(g => g.vram_total));
  const rows = n.ollama.models.map(m => `
    <tr>
      <td><b>${esc(m.name)}</b>${m.loaded ? ` <span class="pill run">${esc(t('node.loaded'))}</span>` : ''}</td>
      <td class="muted">${esc(m.parameters || '')}</td>
      <td>${bytes(m.size)}</td>
      <td><div class="caps">${(m.caps || []).filter(c => CAPS[c]).map(c => `<span class="pill">${esc(t(CAPS[c]))}</span>`).join('')}</div></td>
      <td>${!vram ? '<span class="faint">—</span>' : m.fits_vram
        ? `<span class="pill ok">${esc(t('node.fits_vram'))}</span>`
        : `<span class="pill warn">${esc(t('node.too_big'))}</span>`}</td>
    </tr>`).join('');
  $('#node-models', root).innerHTML = `
    <h2>${icon('layers')}${esc(t('node.models_title'))}</h2>
    <p class="hint">${esc(t('node.models_hint'))}</p>
    ${rows ? `<table class="models"><thead><tr><th>${esc(t('node.col_model'))}</th><th>${esc(t('node.col_parameters'))}</th><th>${esc(t('node.col_disk'))}</th><th>${esc(t('node.col_capabilities'))}</th><th>VRAM</th></tr></thead>
      <tbody>${rows}</tbody></table>` : `<div class="empty">${esc(t(integrated() ? 'node.no_models' : 'node.no_models_ollama', { here: here() }))}</div>`}
    <button class="btn sm" data-install>${icon('plus')}${esc(t('model_install.title'))}</button>`;
  $('#node-models', root).querySelector('[data-install]').onclick = chooseModel;

  renderNetworkSummary();
  // Les préférences ne sont reconstruites qu'à leur changement (settings:loaded) :
  // les redessiner toutes les 5 s fermerait une liste ouverte et perdrait un clic
  if (!$('#share-status', root) || shareMobile !== !!n.power?.mobile) renderShare();
  if (!$('#background-status', root) || trayRow !== windowsWindow()) renderBackground();
}

// État de la file (dont la cadence du travail de fond), s'il n'est pas encore arrivé
// par le flux d'événements
async function loadExecutor() {
  if (state.executor) return;
  try {
    state.executor = (await get('/jobs')).executor;
    emit('executor:changed');
  } catch (e) { /* nœud indisponible : on réessaiera */ }
}

// Travail de fond : priorité basse toujours, et sur batterie, selon le réglage
function renderBackground() {
  const box = root && $('#node-background', root);
  if (!box || !state.settings) return;
  const mode = state.settings.battery_mode || 'eco';
  const options = [['eco', t('node.battery_eco')], ['pause', t('node.battery_pause')],
    ['normal', t('node.full_speed')]];
  trayRow = windowsWindow();
  box.innerHTML = `
    <h2>${icon('cpu')}${esc(t('node.background_title'))}</h2>
    <div id="background-status"></div>
    <p class="hint">${esc(t('node.background_hint'))}</p>
    <div class="setting-row"><div class="txt"><b>${esc(t('node.battery_title'))}</b><small>${esc(t('node.battery_help'))}</small></div>
      <select class="select wide" id="battery-mode">${options.map(([v, l]) =>
        `<option value="${v}" ${v === mode ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></div>
    ${windowsWindow() ? shareRow('close_to_tray', t('node.close_to_tray'), t('node.close_to_tray_help')) : ''}
    <div id="autostart-row"></div>`;
  $('#battery-mode', box).onchange = e => save({ battery_mode: e.target.value });
  box.querySelectorAll('input[data-key]').forEach(input => {
    input.onchange = () => save({ [input.dataset.key]: input.checked });
  });
  renderBackgroundStatus();
  renderAutostart();
}

async function loadAutostart() {
  try { autostart = await get('/node/autostart'); } catch { autostart = null; }
  renderAutostart();
}

/** Démarrer avec l'appareil, en arrière-plan : Windows (raccourci du dossier Démarrage) ou
 *  téléphone (l'appli AIODOS démarre son nœud, node/android_app.py). */
function renderAutostart() {
  const box = root && $('#autostart-row', root);
  if (!box) return;
  if (!autostart?.supported) { box.innerHTML = ''; return; }
  const phone = autostart.device === 'android';
  box.innerHTML = `<div class="setting-row"><div class="txt">
      <b>${esc(t(phone ? 'node.autostart_phone' : 'node.autostart_windows'))}</b>
      <small>${esc(t(phone ? 'node.autostart_phone_help' : 'node.autostart_windows_help'))}</small></div>
    <label class="switch"><input type="checkbox" id="autostart-toggle" ${autostart.enabled ? 'checked' : ''}>
      <span></span></label></div>`;
  $('#autostart-toggle', box).onchange = async e => {
    try {
      autostart = await post('/node/autostart', { enabled: e.target.checked });
      toast(t(autostart.enabled ? 'node.autostart_on' : 'node.autostart_off'), 'ok', 5000);
    } catch (err) {
      e.target.checked = !e.target.checked;
      errorToast(err);
    }
  };
}

// La fenêtre de Windows (pywebview) : la seule qui a une icône dans la zone de notification
function windowsWindow() {
  return !!window.pywebview && /^windows/i.test(state.node?.os || '');
}

function renderBackgroundStatus() {
  const box = root && $('#background-status', root);
  if (!box) return;
  const p = state.executor?.pacing;
  if (!p) { box.innerHTML = ''; return; }
  const [cls, label, detail] = p.chatting
    ? ['warn', t('node.status_chatting'), t('node.status_chatting_detail')]
    : p.mode === 'pause' ? ['warn', t('node.paused'), p.detail]
      : p.mode === 'eco' ? ['ok', t('node.status_eco'), p.detail] : ['ok', t('node.full_speed'), p.detail];
  box.innerHTML = `<div class="share-status"><span class="pill ${cls}">${esc(label)}</span>
    <span class="muted">${esc(detail)}</span></div>`;
}

function renderNetworkSummary() {
  const box = root && $('#node-network', root);
  if (!box) return;
  const net = state.network;
  const peers = net?.peers || [];
  const online = peers.filter(p => p.online).length;
  const summary = !net?.enabled ? t('node.off')
    : peers.length ? t('node.peers_summary', { n: peers.length, online })
      : t('node.no_peers_yet');
  box.innerHTML = `
    <h2>${icon('network')}${esc(t('node.network_title'))}</h2>
    <div class="setting-row" style="border:0;padding:0">
      <div class="txt"><b>${esc(summary)}</b>
        <small>${esc(t('node.network_help'))}</small></div>
      <button class="btn sm" id="go-network">${icon('network')}${esc(t('node.open'))}</button>
    </div>`;
  $('#go-network', box).onclick = () => emit('view:show', 'network');
}

// title et text : du texte brut (t()), échappé ici
function shareRow(key, title, text) {
  const on = !!state.settings?.[key];
  return `<div class="setting-row"><div class="txt"><b>${esc(title)}</b><small>${esc(text)}</small></div>
    <label class="switch"><input type="checkbox" data-key="${key}" ${on ? 'checked' : ''}><span></span></label></div>`;
}

// État du partage, en direct : prêt, en pause (pourquoi), unités calculées ici pour d'autres
function renderShareStatus() {
  const box = root && $('#share-status', root);
  if (!box) return;
  const net = state.network;
  if (!net?.enabled) {
    box.innerHTML = `<div class="phase-note">${icon('info')}<div>${esc(t('node.network_off'))}
      <a href="#" id="share-go-network">${esc(t('node.network_enable'))}</a></div></div>`;
    $('#share-go-network', box).onclick = e => { e.preventDefault(); emit('view:show', 'network'); };
    return;
  }
  const sharing = net.sharing;
  if (!sharing) { box.innerHTML = ''; return; }
  const working = state.executor?.remote || sharing.working || [];
  const [cls, label] = working.length ? ['run', t('node.share_working')]
    : sharing.active ? ['ok', t('node.share_ready')] : sharing.state === 'off' ? ['idle', t('node.off')]
      : ['warn', t('node.paused')];
  box.innerHTML = `
    <div class="share-status"><span class="pill ${cls}">${esc(label)}</span><span class="muted">${esc(sharing.detail)}</span></div>
    ${working.map(w => `<div class="remote-item">${icon(RESOURCE_ICONS[w.resource] || 'cpu')}
      <span>${esc(t('node.working_for', { name: w.peer_name }))} <span class="mono">${esc(w.label)}</span></span></div>`).join('')}`;
}

function renderShare() {
  const box = root && $('#node-share', root);
  if (!box || !state.settings) return;
  const s = state.settings;
  // Téléphone : pas d'inactivité mesurable ; en charge, assez chargé, pas trop chaud
  const mobile = shareMobile = !!state.node?.power?.mobile;
  const device = t(mobile ? 'node.this_phone' : 'node.this_pc');
  const idleRows = `
    ${shareRow('share_when_idle', t('node.when_idle'), t('node.when_idle_help'))}
    <div class="setting-row"><div class="txt"><b>${esc(t('node.idle_delay'))}</b><small>${esc(t('node.idle_delay_help'))}</small></div>
      <select class="select short" data-num="idle_minutes">${[2, 5, 10, 15, 30, 60].map(m =>
        `<option value="${m}" ${m === s.idle_minutes ? 'selected' : ''}>${m} min</option>`).join('')}</select></div>`;
  const phoneRows = `
    <div class="setting-row"><div class="txt"><b>${esc(t('node.min_battery'))}</b><small>${esc(t('node.min_battery_help'))}</small></div>
      <select class="select short" data-num="share_min_battery">${[50, 60, 70, 80, 90].map(p =>
        `<option value="${p}" ${p === s.share_min_battery ? 'selected' : ''}>${esc(t('node.percent', { n: p }))}</option>`).join('')}</select></div>
    <div class="setting-row"><div class="txt"><b>${esc(t('node.max_temp'))}</b><small>${esc(t('node.max_temp_help'))}</small></div>
      <select class="select short" data-num="share_max_temp">${[35, 38, 40, 42].map(deg =>
        `<option value="${deg}" ${deg === s.share_max_temp ? 'selected' : ''}>${esc(t('format.celsius', { n: deg }))}</option>`).join('')}</select></div>`;
  box.innerHTML = `
    <h2>${icon('bolt')}${esc(t('node.share_title'))}</h2>
    <div id="share-status"></div>
    ${shareRow('offload_enabled', t('node.offload'), t('node.offload_help', { device }))}
    ${shareRow('share_enabled', t('node.share'), t(mobile ? 'node.share_help_phone' : 'node.share_help_pc'))}
    ${shareRow('lend_models', t('node.lend_models'), t('node.lend_models_help', { device }))}
    ${mobile ? '' : shareRow('lend_gpu', t('node.lend_gpu'), t('node.lend_gpu_help'))}
    ${mobile ? '' : shareRow('lend_images_friends', t('node.lend_images'), t('node.lend_images_help'))}
    ${mobile ? '' : idleRows}
    ${shareRow('share_on_ac_only', t(mobile ? 'node.ac_only_phone' : 'node.ac_only_pc'),
      t(mobile ? 'node.ac_only_phone_help' : 'node.ac_only_pc_help'))}
    ${mobile ? phoneRows : ''}
    <div class="setting-row"><div class="txt"><b>${esc(t(mobile ? 'node.max_share_phone' : 'node.max_share_pc'))}</b><small>${esc(t(mobile
      ? 'node.max_share_phone_help' : 'node.max_share_pc_help'))}</small></div>
      <select class="select short" data-num="share_max_gpu">${[25, 50, 75, 100].map(p =>
        `<option value="${p}" ${p === s.share_max_gpu ? 'selected' : ''}>${esc(t('node.percent', { n: p }))}</option>`).join('')}</select></div>`;
  box.querySelectorAll('input[data-key]').forEach(input => {
    input.onchange = () => save({ [input.dataset.key]: input.checked });
  });
  box.querySelectorAll('select[data-num]').forEach(sel => {
    sel.onchange = () => save({ [sel.dataset.num]: Number(sel.value) });
  });
  renderShareStatus();
}

async function save(values) {
  try {
    state.settings = await put('/settings', values);
    emit('settings:loaded');
    loadNetwork();                 // l'état du partage en dépend
  } catch (e) { errorToast(e); }
}

async function renameNode() {
  const name = await ask(t('node.rename_ask_title'), t('node.rename_ask_text'), state.node.name);
  if (!name) return;
  await save({ node_name: name });
  toast(t('node.renamed'), 'ok');
  loadNode();
}
