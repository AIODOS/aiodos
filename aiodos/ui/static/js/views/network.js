// Réseau : réseau local, Internet et invitations, machines appairées et détectées, appairage par code.
import { get, post, del } from '../api.js';
import { $, h, esc, ago, bytes, duration, powerText } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { modal, menu, toast, errorToast, confirmDanger } from '../ui.js';
import { renderConnected } from './connected.js';

let root;
let lastJson = '';

export function mountNetwork(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head"><div><h1>Réseau</h1>
        <div class="sub">Fais travailler tes machines ensemble, et celles de tes amis : chacune peut prêter sa
        puissance de calcul aux autres. Rien ne part sans appairage confirmé sur les deux machines.</div></div></div>
      <div class="card" id="net-live" hidden></div>
      <div class="card" id="net-state" style="margin-top:16px"></div>
      <div class="card" id="net-internet" style="margin-top:16px"></div>
      <div class="card" id="net-peers" style="margin-top:16px"></div>
      <div class="card" id="net-visible" style="margin-top:16px"></div>
    </div></div>`);
  container.append(root);
  on('network:changed', loadNetwork);
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

function render() {
  const net = state.network;
  if (!root || !net) return;
  renderConnected($('#net-live', root), net);
  $('#net-state', root).style.marginTop = net.enabled ? '16px' : '0';
  renderState(net);
  renderInternet(net);
  renderPeers(net);
  renderVisible(net);
}

// ----------------------------------------------- Internet et invitations (2.4)
// Relais de n0 : leur région suffit (« euc1-1 ») ; un relais à soi garde son nom entier.
function host(url) {
  try {
    const u = new URL(url);
    return /\.iroh\.link\.?$/.test(u.hostname) ? u.hostname.split('.')[0] : u.host;
  } catch { return url; }
}

function invitationRow(inv) {
  const until = new Date(inv.expires * 1000).toLocaleString('fr-FR', { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  const text = inv.role === 'received'
    ? `Invitation de « ${esc(inv.name || '?')} » acceptée ${ago(inv.created)} : l'appairage commencera quand ton ami aura collé ta réponse.`
    : inv.answered
      ? `Réponse de « ${esc(inv.name || '?')} » reçue : appairage en cours.`
      : `Invitation créée ${ago(inv.created)}, en attente de la réponse de ton ami (valable jusqu'à ${esc(until)}).`;
  return `<div class="peer"><div class="peer-head">${icon('link')}<span style="flex:1">${text}</span>
    ${inv.role === 'sent' && !inv.answered ? `<button class="btn sm" data-reply="${inv.id}">Coller la réponse</button>` : ''}
    <button class="icon-btn danger" data-cancel="${inv.id}" title="Annuler l'invitation">${icon('x')}</button></div></div>`;
}

function renderInternet(net) {
  const box = $('#net-internet', root);
  const inet = net.internet || {};
  const status = !inet.enabled
    ? 'Désactivée : tes machines ne se joignent que sur le réseau local.'
    : inet.running
      ? `Prête${inet.relays?.length ? ` · relais ${inet.relays.map(r => esc(host(r))).join(', ')}` : ''}`
        + (inet.relay_only ? ' (essai : relais seulement)' : '')
        + (inet.id ? ` · clé ${esc(inet.id.slice(0, 10))}…` : '')
        + (inet.denied_by || []).map(r => ` · le relais ${esc(host(r))} refuse cette machine : `
          + 'sa clé doit être dans la liste blanche du relais').join('')
      : esc(inet.error || 'Démarrage du compagnon aiodos-net…');
  box.innerHTML = `
    <div class="setting-row" style="border:0;padding-top:0">
      <div class="txt"><b>Relier mes machines par Internet</b><small>${status}</small></div>
      <label class="switch"><input type="checkbox" id="inet-toggle" ${inet.enabled ? 'checked' : ''}><span></span></label>
    </div>
    <h2 style="margin-top:4px">${icon('link')}Inviter un ami</h2>
    <p class="hint">Pour appairer la machine d'un ami qui n'est pas sur ton réseau. Crée une invitation et envoie-lui le code
      (message, courriel) ; il le colle dans son AIODOS avec « Répondre à une invitation », puis te renvoie sa réponse, que
      tu colles ici. Vous confirmez alors chacun le même code à 6 chiffres. Une invitation sert une seule fois, pendant 24 heures,
      et il faut la connexion par Internet des deux côtés.</p>
    <div class="toolbar" style="margin:0 0 10px">
      <button class="btn sm" id="inv-create" ${inet.running ? '' : 'disabled'}>${icon('plus')}Créer une invitation</button>
      <button class="btn sm" id="inv-accept" ${inet.running ? '' : 'disabled'}>${icon('link')}Répondre à une invitation</button>
      ${inet.running && inet.id ? `<button class="btn sm ghost" id="inet-key" title="Pour la liste blanche d'un relais auto-hébergé (docs/relais.md)">${icon('copy')}Copier la clé de cette machine</button>` : ''}
    </div>
    <div class="peer-list">${(net.invitations || []).map(invitationRow).join('')}</div>`;
  $('#inet-toggle', box).onchange = async e => {
    try { await post('/network/internet', { enabled: e.target.checked }); lastJson = ''; loadNetwork(); }
    catch (err) { errorToast(err); }
  };
  $('#inv-create', box).onclick = createInvitation;
  $('#inet-key', box)?.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(inet.id); toast('Clé copiée.', 'ok'); }
    catch { toast(inet.id, 'info', 15000); }
  });
  $('#inv-accept', box).onclick = acceptInvitation;
  box.querySelectorAll('[data-reply]').forEach(b => { b.onclick = () => pasteReply(Number(b.dataset.reply)); });
  box.querySelectorAll('[data-cancel]').forEach(b => {
    b.onclick = async () => {
      try { state.network = await del(`/network/invitations/${b.dataset.cancel}`); lastJson = ''; loadNetwork(); }
      catch (e) { errorToast(e); }
    };
  });
}

async function copyCode(title, text, code, done) {
  const r = await modal({ title, iconName: 'link', text, textarea: code, confirm: 'Copier', cancel: 'Fermer' });
  if (!r.ok) return;
  try { await navigator.clipboard.writeText(code); toast(done, 'ok', 8000); }
  catch { toast('Copie impossible : sélectionne le code et copie-le à la main.', 'err', 6000); }
}

async function createInvitation() {
  let created;
  try { created = await post('/network/invitations'); } catch (e) { errorToast(e); return; }
  lastJson = '';
  loadNetwork();
  await copyCode('Invitation à envoyer à ton ami',
    'Envoie ce code à ton ami (message, courriel). Il le colle dans son AIODOS, puis te renvoie sa réponse : '
      + 'colle-la ici, avec « Coller la réponse ».',
    created.code, 'Invitation copiée : envoie-la à ton ami.');
}

async function acceptInvitation() {
  const r = await modal({ title: 'Répondre à une invitation', iconName: 'link', textarea: '',
    placeholder: 'AIODOS1-…', text: 'Colle ici le code d\'invitation reçu de ton ami.', confirm: 'Continuer' });
  if (!r.ok || !r.value.trim()) return;
  let answer;
  try { answer = await post('/network/invitations/accept', { code: r.value }); } catch (e) { errorToast(e); return; }
  lastJson = '';
  loadNetwork();
  await copyCode(`Réponse à renvoyer à « ${answer.name} »`,
    `Renvoie cette réponse à « ${answer.name} ». Quand il l'aura collée, une demande d'appairage s'affichera ici : `
      + 'vérifiez ensemble que vous voyez le même code à 6 chiffres.',
    answer.reply, 'Réponse copiée : renvoie-la à ton ami.');
}

async function pasteReply(invitationId) {
  const r = await modal({ title: 'Réponse de ton ami', iconName: 'link', textarea: '', placeholder: 'AIODOS1-…',
    text: 'Colle ici la réponse que ton ami t\'a renvoyée.', confirm: 'Appairer' });
  if (!r.ok || !r.value.trim()) return;
  let pending;
  try { pending = await post(`/network/invitations/${invitationId}/reply`, { code: r.value }); }
  catch (e) { errorToast(e); return; }
  await confirmPairing(pending, 'Vérifie avec ton ami (au téléphone, par message) qu\'il voit ce même code, '
    + 'dis ce qu\'est sa machine pour toi, puis confirmez chacun de votre côté.');
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
  if (!p.memory) {                    // profil allégé : cette machine nous voit comme un ami
    const facts = ['profil réduit (cette machine te compte parmi ses amis)'];
    if (p.lends_models) facts.push(`prête ${(p.ollama?.models || []).length} modèle(s)`);
    if (p.version) facts.push(`AIODOS ${esc(p.version)}`);
    return facts.join(' · ');
  }
  const gpu = p.gpus?.[0];
  const facts = [
    gpu ? `${esc(gpu.name)} · ${bytes(gpu.vram_total)} de VRAM` : 'Pas de GPU NVIDIA',
    `${bytes(p.memory?.total || 0)} de RAM`,
    `${(p.ollama?.models || []).length} modèle(s)`,
  ];
  if (p.lends_models) facts.push('prête ses modèles');
  if (p.power?.mobile) {                       // téléphone : charge, niveau, température
    const text = powerText(p);
    facts.push(text.charAt(0).toLowerCase() + text.slice(1));
  } else if (p.on_ac_power === false) facts.push('sur batterie');
  if (p.idle_seconds != null) facts.push(p.idle_seconds < 60 ? 'utilisée en ce moment' : `inactive depuis ${duration(p.idle_seconds)}`);
  if (p.version) facts.push(`AIODOS ${esc(p.version)}`);
  return facts.join(' · ');
}

function renderPeers(net) {
  const box = $('#net-peers', root);
  box.innerHTML = `
    <h2>${icon('network')}Machines appairées</h2>
    <p class="hint">Tes machines et celles de tes amis. Les échanges sont chiffrés, et chaque machine applique ses
      propres règles de partage (réglables dans « Cette machine »). Un ami ne reçoit que les bibliothèques
      partagées avec tes amis. ${net.offload ? 'Tes tâches peuvent leur être confiées.' : 'Tu ne leur confies pas tes tâches.'}</p>
    <div class="peer-list"></div>`;
  const list = box.querySelector('.peer-list');
  if (!net.peers.length) {
    list.append(h('<div class="empty" style="padding:18px">Aucune machine appairée pour l\'instant.</div>'));
    return;
  }
  for (const peer of net.peers) {
    const friend = peer.trust === 'friend';
    const shared = peer.shared;
    const consulted = peer.consulted;          // ses recherches dans mes bibliothèques ouvertes
    const row = h(`
      <div class="peer">
        <div class="peer-head">
          <span class="dot ${peer.online ? 'on' : ''}" title="${peer.online ? 'En ligne' : 'Hors ligne'}"></span>
          <b>${esc(peer.name)}</b>
          ${friend ? '<span class="pill" title="Machine d\'un ami : droits plus étroits">ami</span>' : ''}
          ${peer.set_aside_at ? '<span class="pill err">mise de côté</span>' : ''}
          <span class="faint">${peer.address ? `${esc(peer.address)}:${peer.port || '?'}`
            : peer.net_id ? 'par Internet' : 'adresse inconnue'}</span>
          <span class="faint">${peer.online ? 'en ligne' : peer.last_seen ? `vue ${ago(peer.last_seen)}` : 'jamais vue'}</span>
          <span style="flex:1"></span>
          <button class="icon-btn" data-act="refresh" title="Actualiser">${icon('retry')}</button>
          <button class="icon-btn" data-act="more" title="Options">${icon('more')}</button>
          <button class="icon-btn danger" data-act="unpair" title="Retirer l'appairage">${icon('x')}</button>
        </div>
        <div class="peer-facts">${peerFacts(peer)}${netFact(peer)}</div>
        ${peer.set_aside_at ? `<div class="peer-aside">${icon('warn')}<div>Mise de côté ${ago(peer.set_aside_at)} :
          ${esc(peer.set_aside_reason || 'contrôle échoué')}. Plus rien ne lui est confié.</div>
          <button class="btn sm" data-act="restore">Rétablir</button></div>` : ''}
        ${peer.working_for_me ? `<div class="peer-work">${icon('bolt')}Calcule pour toi en ce moment :
          ${peer.working_for_me} unité(s)</div>` : ''}
        <div class="peer-ledger faint">A calculé pour toi : ${duration(peer.received_s)} (${peer.received_units} unité(s))
          · Tu as calculé pour elle : ${duration(peer.given_s)} (${peer.given_units} unité(s))</div>
        ${peer.received_chats || peer.given_chats ? `<div class="peer-ledger faint">Conversations servies par
          un modèle prêté : ${peer.received_chats || 0} pour toi · ${peer.given_chats || 0} pour elle</div>` : ''}
        ${shared ? `<div class="peer-ledger faint">Confié à cette machine : ${shared.count} fichier(s) ou page(s)
          ${shared.bytes ? `(${bytes(shared.bytes)})` : ''}, le dernier ${ago(shared.last)}</div>` : ''}
        ${consulted ? `<div class="peer-ledger faint">A cherché dans tes bibliothèques : ${consulted.count} recherche(s),
          ${consulted.passages} passage(s) rendu(s)${consulted.refused ? `, ${consulted.refused} refus` : ''},
          la dernière ${ago(consulted.last)}</div>` : ''}
      </div>`);
    row.querySelector('[data-act="more"]').onclick = e => {
      e.stopPropagation();
      menu(e.currentTarget, [
        friend
          ? { label: 'C\'est une de mes machines', icon: 'network', action: () => setTrust(peer, 'mine') }
          : { label: 'C\'est la machine d\'un ami', icon: 'shield', action: () => setTrust(peer, 'friend') },
        ...(shared ? [{ label: 'Voir ce qui lui a été confié', icon: 'file', action: () => showShared(peer) }] : []),
        ...(consulted ? [{ label: 'Voir ses recherches dans tes bibliothèques', icon: 'search',
          action: () => showConsulted(peer) }] : []),
      ]);
    };
    row.querySelector('[data-act="restore"]')?.addEventListener('click', async () => {
      try { state.network = await post(`/network/peers/${peer.node_id}/restore`); lastJson = ''; loadNetwork(); }
      catch (e) { errorToast(e); }
    });
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

function netFact(peer) {
  const inet = peer.internet;
  if (!inet?.connected) return '';
  const rtt = inet.rtt_ms ? ` (${Math.round(inet.rtt_ms)} ms)` : '';
  return ` · Internet : ${inet.path === 'direct' ? 'en direct' : 'par un relais'}${rtt}`;
}

async function setTrust(peer, trust) {
  try {
    state.network = await post(`/network/peers/${peer.node_id}/trust`, { trust });
    toast(trust === 'friend'
      ? `« ${peer.name} » est désormais la machine d'un ami : seules les bibliothèques partagées avec tes amis lui seront confiées.`
      : `« ${peer.name} » est désormais une de tes machines : confiance totale.`, 'ok', 7000);
    lastJson = '';
    loadNetwork();
  } catch (e) { errorToast(e); }
}

async function showShared(peer) {
  let entries;
  try { ({ entries } = await get(`/network/peers/${peer.node_id}/shared`)); } catch (e) { errorToast(e); return; }
  const lines = entries.map(e => `${new Date(e.at * 1000).toLocaleString('fr-FR')} · ${e.what}`
    + `${e.sent_as && e.sent_as !== e.what ? ` → ${e.sent_as}` : ''}${e.size ? ` (${bytes(e.size)})` : ''}`);
  await modal({
    title: `Confié à « ${peer.name} »`, iconName: 'file', cancel: null, confirm: 'Fermer',
    text: 'Les fichiers et pages dont le calcul lui a été confié, du plus récent au plus ancien '
      + '(à droite de la flèche, le nom sous lequel il les a reçus).',
    quote: lines.join('\n') || 'Rien pour l\'instant.',
  });
}

async function showConsulted(peer) {
  let entries;
  try { ({ entries } = await get(`/network/peers/${peer.node_id}/consulted`)); } catch (e) { errorToast(e); return; }
  const lines = entries.map(e => {
    const when = new Date(e.at * 1000).toLocaleString('fr-FR');
    if (e.refused) return `${when} · refusée : ${e.refused}`;
    const docs = e.documents.map(d => `${d.document} (${d.library})`).join(', ');
    return `${when} · ${e.passages} passage(s)${docs ? ` : ${docs}` : ''}`;
  });
  await modal({
    title: `Recherches de « ${peer.name} »`, iconName: 'search', cancel: null, confirm: 'Fermer',
    text: 'Ses recherches dans les bibliothèques que tu lui ouvres, de la plus récente à la plus ancienne, '
      + 'avec les documents d\'où viennent les passages qu\'elle a reçus. Ses questions ne sont pas gardées.',
    quote: lines.join('\n') || 'Rien pour l\'instant.',
  });
}

// ----------------------------------------------------------- machines détectées
function renderVisible(net) {
  const box = $('#net-visible', root);
  if (!net.running) {
    box.innerHTML = `<h2>${icon('search')}Machines détectées</h2>
      <p class="hint" style="margin:0">Active le réseau pour voir les autres machines AIODOS de ton réseau.</p>`;
    return;
  }
  box.innerHTML = `
    <h2>${icon('search')}Machines détectées</h2>
    <p class="hint">Les nœuds AIODOS qui s'annoncent sur ton réseau. Si une machine n'apparaît pas,
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
// Ce qu'est l'autre machine pour celle-ci (network/sharing.py) : choisi sur chacune
const TRUST_CHOICES = [
  { value: 'mine', label: 'Une de mes machines',
    help: 'Confiance totale : toutes tes bibliothèques peuvent y être calculées, sauf celles gardées sur ce PC.' },
  { value: 'friend', label: 'La machine d\'un ami',
    help: 'Seulement les bibliothèques partagées avec tes amis, sous des noms de fichiers neutres ; '
      + 'ses calculs sont vérifiés par échantillon.' },
];

async function startPairing(v) {
  let pending;
  try { pending = await post('/network/pair', { node_id: v.node_id }); } catch (e) { errorToast(e); return; }
  await confirmPairing(pending, 'Vérifie que ce même code s\'affiche sur l\'autre machine, dis ce qu\'elle est '
    + 'pour toi, puis confirme ici ET là-bas.');
}

/** Appairage commencé ici (réseau local ou invitation) : code vérifié, confiance choisie. */
async function confirmPairing(pending, text) {
  const r = await modal({
    title: `Appairer avec « ${pending.name} »`, iconName: 'shield', code: pending.code, text,
    choices: TRUST_CHOICES, confirm: 'Les codes sont identiques', cancel: 'Annuler',
  });
  try {
    const snap = await post('/network/pair/answer',
      { node_id: pending.node_id, accept: r.ok, trust: r.choice || 'mine' });
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
      + 's\'affiche sur son écran, et dis ce qu\'elle est pour toi.',
    choices: TRUST_CHOICES, confirm: 'Accepter', cancel: 'Refuser',
  });
  try {
    await post('/network/pair/answer', { node_id: ev.node_id, accept: r.ok, trust: r.choice || 'mine' });
  } catch (e) { errorToast(e); }
  lastJson = '';
  loadNetwork();
}
