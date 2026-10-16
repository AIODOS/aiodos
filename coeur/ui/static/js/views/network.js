// Réseau local : activation, machines appairées et détectées, appairage par code.
import { get, post, del } from '../api.js';
import { $, h, esc, ago, bytes, duration } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { modal, toast, errorToast, confirmDanger } from '../ui.js';

let root;
let lastJson = '';

export function mountNetwork(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head"><div><h1>Réseau local</h1>
        <div class="sub">Fais travailler tes machines ensemble : chacune peut prêter sa puissance aux autres,
        à la façon de SETI@home. Rien ne part sans appairage confirmé sur les deux machines.</div></div></div>
      <div class="card" id="net-state"></div>
      <div class="card" id="net-peers" style="margin-top:16px"></div>
      <div class="card" id="net-visible" style="margin-top:16px"></div>
    </div></div>`);
  container.append(root);
  on('network:changed', loadNetwork);
  on('view:shown', view => { if (view === 'network') loadNetwork(); });
  setInterval(() => { if (state.view === 'network') loadNetwork(); }, 5000);
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

function render() {
  const net = state.network;
  if (!root || !net) return;
  renderState(net);
  renderPeers(net);
  renderVisible(net);
}

// --------------------------------------------------------------------- état
function renderState(net) {
  const box = $('#net-state', root);
  const detail = !net.enabled
    ? "Rien ne sort de cette machine. Active le réseau pour l'appairer avec tes autres PC."
    : net.running
      ? `Cette machine (« ${esc(net.name)} ») est visible sur ton réseau, port ${net.port}` +
        (net.discovery ? ', découverte automatique active.' : '.')
      : 'Démarrage…';
  box.innerHTML = `
    <div class="setting-row" style="border:0;padding-top:0">
      <div class="txt"><b>${net.enabled ? 'Réseau local activé' : 'Réseau local désactivé'}</b><small>${detail}</small></div>
      <label class="switch"><input type="checkbox" id="net-toggle" ${net.enabled ? 'checked' : ''}><span></span></label>
    </div>
    ${net.error ? `<div class="banner warn" style="margin:8px 0 0">${icon('warn')}<div>${esc(net.error)}</div></div>` : ''}
    ${net.enabled ? '' : `<div class="phase-note" style="margin-top:10px">${icon('shield')}<div>À l'activation,
      Windows demandera sans doute l'autorisation du pare-feu pour Python : accepte pour les
      <b>réseaux privés</b> seulement.</div></div>`}`;
  $('#net-toggle', box).onchange = async e => {
    try {
      state.network = await post('/network/enable', { enabled: e.target.checked });
      lastJson = '';
      loadNetwork();
    } catch (err) { errorToast(err); }
  };
}

// ---------------------------------------------------------- machines appairées
function peerFacts(peer) {
  const p = peer.profile;
  if (!p) return '<span class="faint">Profil pas encore reçu.</span>';
  const gpu = p.gpus?.[0];
  const facts = [
    gpu ? `${esc(gpu.name)} · ${bytes(gpu.vram_total)} de VRAM` : 'Pas de GPU NVIDIA',
    `${bytes(p.memory?.total || 0)} de RAM`,
    `${(p.ollama?.models || []).length} modèle(s)`,
  ];
  if (p.lends_models) facts.push('prête ses modèles');
  if (p.on_ac_power === false) facts.push('sur batterie');
  if (p.idle_seconds != null) facts.push(p.idle_seconds < 60 ? 'utilisée en ce moment' : `inactive depuis ${duration(p.idle_seconds)}`);
  return facts.join(' · ');
}

function renderPeers(net) {
  const box = $('#net-peers', root);
  box.innerHTML = `
    <h2>${icon('network')}Mes machines</h2>
    <p class="hint">Appairées avec celle-ci. Les échanges sont chiffrés, et chaque machine applique ses
      propres règles de partage (réglables dans « Cette machine »).
      ${net.offload ? 'Tes tâches peuvent leur être confiées.' : 'Tu ne leur confies pas tes tâches.'}</p>
    <div class="peer-list"></div>`;
  const list = box.querySelector('.peer-list');
  if (!net.peers.length) {
    list.append(h('<div class="empty" style="padding:18px">Aucune machine appairée pour l\'instant.</div>'));
    return;
  }
  for (const peer of net.peers) {
    const row = h(`
      <div class="peer">
        <div class="peer-head">
          <span class="dot ${peer.online ? 'on' : ''}" title="${peer.online ? 'En ligne' : 'Hors ligne'}"></span>
          <b>${esc(peer.name)}</b>
          <span class="faint">${esc(peer.address || '?')}:${peer.port || '?'}</span>
          <span class="faint">${peer.online ? 'en ligne' : peer.last_seen ? `vue ${ago(peer.last_seen)}` : 'jamais vue'}</span>
          <span style="flex:1"></span>
          <button class="icon-btn" data-act="refresh" title="Actualiser">${icon('retry')}</button>
          <button class="icon-btn danger" data-act="unpair" title="Retirer l'appairage">${icon('x')}</button>
        </div>
        <div class="peer-facts">${peerFacts(peer)}</div>
        ${peer.working_for_me ? `<div class="peer-work">${icon('bolt')}Calcule pour toi en ce moment :
          ${peer.working_for_me} unité(s)</div>` : ''}
        <div class="peer-ledger faint">A calculé pour toi : ${duration(peer.received_s)} (${peer.received_units} unité(s))
          · Tu as calculé pour elle : ${duration(peer.given_s)} (${peer.given_units} unité(s))</div>
      </div>`);
    row.querySelector('[data-act="refresh"]').onclick = async () => {
      try { state.network = await post(`/network/peers/${peer.node_id}/refresh`); lastJson = ''; loadNetwork(); }
      catch (e) { errorToast(e); }
    };
    row.querySelector('[data-act="unpair"]').onclick = async () => {
      const ok = await confirmDanger('Retirer l\'appairage',
        `« ${peer.name} » ne pourra plus échanger avec cette machine. Il faudra refaire l'appairage pour la retrouver.`,
        'Retirer');
      if (!ok) return;
      try { await del(`/network/peers/${peer.node_id}`); lastJson = ''; loadNetwork(); } catch (e) { errorToast(e); }
    };
    list.append(row);
  }
}

// ----------------------------------------------------------- machines détectées
function renderVisible(net) {
  const box = $('#net-visible', root);
  if (!net.running) {
    box.innerHTML = `<h2>${icon('search')}Machines détectées</h2>
      <p class="hint" style="margin:0">Active le réseau pour voir les autres machines Cœur IA de ton réseau.</p>`;
    return;
  }
  box.innerHTML = `
    <h2>${icon('search')}Machines détectées</h2>
    <p class="hint">Les nœuds Cœur IA qui s'annoncent sur ton réseau. Si une machine n'apparaît pas,
      ajoute-la par son adresse IP.</p>
    <div class="peer-list" id="visible-list"></div>
    <div class="toolbar" style="margin:14px 0 0">
      <input class="input" id="net-address" placeholder="Adresse : 192.168.1.20 ou 192.168.1.20:47701" style="max-width:360px">
      <button class="btn" id="net-add">${icon('plus')}Ajouter</button>
    </div>`;
  const list = $('#visible-list', box);
  for (const p of net.pairing) {
    const waiting = p.local_ok ? "en attente de la confirmation sur l'autre machine" : 'à confirmer ici';
    const row = h(`<div class="peer"><div class="peer-head">${icon('shield')}<b>${esc(p.name)}</b>
      <span class="faint">appairage en cours · code ${esc(p.code)} · ${waiting}</span></div></div>`);
    if (!p.local_ok) {
      const btn = h(`<button class="btn sm">Vérifier le code</button>`);
      btn.onclick = () => answerPairRequest({ node_id: p.node_id, name: p.name, code: p.code });
      row.querySelector('.peer-head').append(btn);
    }
    list.append(row);
  }
  const pendingIds = new Set(net.pairing.map(p => p.node_id));
  for (const v of net.visible.filter(v => !pendingIds.has(v.node_id))) {
    const row = h(`
      <div class="peer"><div class="peer-head">
        <span class="dot on"></span><b>${esc(v.name)}</b>
        <span class="faint">${esc(v.address)}:${v.port}</span><span style="flex:1"></span>
        <button class="btn sm primary">${icon('shield')}Appairer</button>
      </div></div>`);
    row.querySelector('button').onclick = () => startPairing(v);
    list.append(row);
  }
  if (!list.children.length) {
    list.append(h('<div class="empty" style="padding:14px">Aucune autre machine détectée pour l\'instant.</div>'));
  }
  const add = async () => {
    const address = $('#net-address', box).value.trim();
    if (!address) return;
    try {
      const info = await post('/network/add', { address });
      toast(`« ${info.name} » trouvée.`, 'ok');
      lastJson = '';
      loadNetwork();
    } catch (e) { errorToast(e); }
  };
  $('#net-add', box).onclick = add;
  $('#net-address', box).onkeydown = e => { if (e.key === 'Enter') add(); };
}

// ------------------------------------------------------------------ appairage
async function startPairing(v) {
  let pending;
  try { pending = await post('/network/pair', { node_id: v.node_id }); } catch (e) { errorToast(e); return; }
  const r = await modal({
    title: `Appairer avec « ${pending.name} »`, iconName: 'shield', code: pending.code,
    text: 'Vérifie que ce même code s\'affiche sur l\'autre machine, puis confirme ici ET là-bas.',
    confirm: 'Les codes sont identiques', cancel: 'Annuler',
  });
  try {
    const snap = await post('/network/pair/answer', { node_id: pending.node_id, accept: r.ok });
    const done = snap.peers.some(p => p.node_id === pending.node_id);
    if (r.ok && !done) toast('Confirmé ici. Confirme maintenant sur l\'autre machine.', 'info', 8000);
  } catch (e) { errorToast(e); }
  lastJson = '';
  loadNetwork();
}

/** Demande reçue d'une autre machine (affichée quelle que soit la vue ouverte). */
export async function answerPairRequest(ev) {
  const r = await modal({
    title: 'Demande d\'appairage', iconName: 'shield', code: ev.code,
    text: `« ${ev.name} » veut s'appairer avec cette machine. Accepte seulement si ce même code `
      + 's\'affiche sur son écran.',
    confirm: 'Accepter', cancel: 'Refuser',
  });
  try { await post('/network/pair/answer', { node_id: ev.node_id, accept: r.ok }); } catch (e) { errorToast(e); }
  lastJson = '';
  loadNetwork();
}
