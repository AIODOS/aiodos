// Réseau : réseau local, Internet et invitations, machines appairées et détectées, appairage par code.
import { get, post, del } from '../api.js';
import { $, h, esc, ago, bytes, duration, powerText, finite, count } from '../dom.js';
import { t, tt, locale } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { modal, menu, toast, errorToast, confirmDanger } from '../ui.js';
import { renderConnected } from './connected.js';

// Ce que disent les autres machines (profil d'une machine appairée, nœud du réseau public)
// vient d'ailleurs : typé avant d'entrer dans le HTML. Un objet à la place d'un nombre
// (« length » piégé) ou du HTML à la place d'un niveau de batterie ne passe pas. Les gardes
// des nombres et des listes sont celles de dom.js (finite, count), partagées avec les autres
// vues : une seule copie, qui ne peut pas diverger d'une vue à l'autre. Les textes (t, tt)
// sont du texte brut : échappés (esc) en entier, paramètres compris, avant d'entrer dans le HTML.
const flag = v => (typeof v === 'boolean' ? v : null);

/** Le pare-feu de Windows demande son accord au premier réseau : pas sur un téléphone. */
const onWindows = () => (state.node?.os ? state.node.os.startsWith('Windows')
  : navigator.userAgent.includes('Windows'));

let root;
let lastJson = '';

export function mountNetwork(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head"><div><h1>${esc(t('network.title'))}</h1>
        <div class="sub">${esc(t('network.intro'))}</div></div></div>
      <div class="card" id="net-live" hidden></div>
      <div class="card" id="net-state" style="margin-top:16px"></div>
      <div class="card" id="net-internet" style="margin-top:16px"></div>
      <div class="card" id="net-public" style="margin-top:16px"></div>
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
  renderPublic(net);
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
  const until = new Date(inv.expires * 1000).toLocaleString(locale(), { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  const name = inv.name || '?';
  const text = inv.role === 'received'
    ? t('network.invitation_received', { name, ago: ago(inv.created) })
    : inv.answered
      ? t('network.invitation_answered', { name })
      : t('network.invitation_sent', { ago: ago(inv.created), until });
  return `<div class="peer"><div class="peer-head">${icon('link')}<span style="flex:1">${esc(text)}</span>
    ${inv.role === 'sent' && !inv.answered ? `<button class="btn sm" data-reply="${inv.id}">${esc(t('network.paste_reply'))}</button>` : ''}
    <button class="icon-btn danger" data-cancel="${inv.id}" title="${esc(t('network.cancel_invitation'))}">${icon('x')}</button></div></div>`;
}

/** L'état de la connexion par Internet, en texte brut : « Prête · relais … · clé … ». */
function internetStatus(inet) {
  if (!inet.enabled) return t('network.internet_off');
  if (!inet.running) return inet.error || t('network.internet_starting');
  const parts = [t('network.internet_ready')];
  if (inet.relays?.length) {
    parts.push(t('network.internet_relays', { n: inet.relays.length, relays: inet.relays.map(host).join(', ') }));
  }
  // « (essai : relais seulement) » suit ce qui précède, sans séparateur
  if (inet.relay_only) parts.push(`${parts.pop()} ${t('network.internet_relay_only')}`);
  if (inet.id) parts.push(t('network.internet_key', { key: inet.id.slice(0, 10) }));
  for (const r of inet.denied_by || []) parts.push(t('network.internet_denied', { relay: host(r) }));
  return parts.join(' · ');
}

function renderInternet(net) {
  const box = $('#net-internet', root);
  const inet = net.internet || {};
  box.innerHTML = `
    <div class="setting-row" style="border:0;padding-top:0">
      <div class="txt"><b>${esc(t('network.internet_title'))}</b><small>${esc(internetStatus(inet))}</small></div>
      <label class="switch"><input type="checkbox" id="inet-toggle" ${inet.enabled ? 'checked' : ''}><span></span></label>
    </div>
    <h2 style="margin-top:4px">${icon('link')}${esc(t('network.invite_title'))}</h2>
    <p class="hint">${esc(t('network.invite_hint'))}</p>
    <div class="toolbar" style="margin:0 0 10px">
      <button class="btn sm" id="inv-create" ${inet.running ? '' : 'disabled'}>${icon('plus')}${esc(t('network.create_invitation'))}</button>
      <button class="btn sm" id="inv-accept" ${inet.running ? '' : 'disabled'}>${icon('link')}${esc(t('network.accept_invitation'))}</button>
      ${inet.running && inet.id ? `<button class="btn sm ghost" id="inet-key" title="${esc(t('network.copy_key_title'))}">${icon('copy')}${esc(t('network.copy_key'))}</button>` : ''}
    </div>
    <div class="peer-list">${(net.invitations || []).map(invitationRow).join('')}</div>`;
  $('#inet-toggle', box).onchange = async e => {
    try { await post('/network/internet', { enabled: e.target.checked }); lastJson = ''; loadNetwork(); }
    catch (err) { errorToast(err); }
  };
  $('#inv-create', box).onclick = createInvitation;
  $('#inet-key', box)?.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(inet.id); toast(t('network.key_copied'), 'ok'); }
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
  const r = await modal({ title, iconName: 'link', text, textarea: code,
    confirm: t('network.copy'), cancel: t('network.close') });
  if (!r.ok) return;
  try { await navigator.clipboard.writeText(code); toast(done, 'ok', 8000); }
  catch { toast(t('network.copy_failed'), 'err', 6000); }
}

async function createInvitation() {
  let created;
  try { created = await post('/network/invitations'); } catch (e) { errorToast(e); return; }
  lastJson = '';
  loadNetwork();
  await copyCode(t('network.invitation_title'), t('network.invitation_text'),
    created.code, t('network.invitation_copied'));
}

async function acceptInvitation() {
  const r = await modal({ title: t('network.accept_invitation'), iconName: 'link', textarea: '',
    placeholder: 'AIODOS1-…', text: t('network.accept_text'), confirm: t('network.continue') });
  if (!r.ok || !r.value.trim()) return;
  let answer;
  try { answer = await post('/network/invitations/accept', { code: r.value }); } catch (e) { errorToast(e); return; }
  lastJson = '';
  loadNetwork();
  await copyCode(t('network.reply_title', { name: answer.name }), t('network.reply_text', { name: answer.name }),
    answer.reply, t('network.reply_copied'));
}

async function pasteReply(invitationId) {
  const r = await modal({ title: t('network.paste_title'), iconName: 'link', textarea: '', placeholder: 'AIODOS1-…',
    text: t('network.paste_text'), confirm: t('network.pair') });
  if (!r.ok || !r.value.trim()) return;
  let pending;
  try { pending = await post(`/network/invitations/${invitationId}/reply`, { code: r.value }); }
  catch (e) { errorToast(e); return; }
  await confirmPairing(pending, t('network.paste_check'));
}

// ------------------------------------------------ réseau public (phase 3.0)
const shortKey = key => (key ? `${key.slice(0, 6)}…${key.slice(-4)}` : '');

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

function renderPublic(net) {
  const box = $('#net-public', root);
  const pub = net.public;
  box.hidden = !pub || !net.enabled;
  if (box.hidden) return;
  const nodes = Array.isArray(pub.nodes) ? pub.nodes : [];
  const detail = !pub.wanted ? ''
    : !pub.internet ? `<div class="hint" style="margin:8px 0 0">${esc(t('network.public_needs_internet'))}</div>`
      : pub.error ? `<div class="banner warn" style="margin:8px 0 0">${icon('warn')}<div>${esc(pub.error)}</div></div>`
        : !pub.joined ? `<div class="faint" style="margin-top:8px">${esc(t('network.public_connecting'))}</div>`
          : `<div class="peer-facts" style="margin:8px 0">${esc(t('network.public_key'))}
              <code title="${esc(pub.key)}">${esc(shortKey(String(pub.key || '')))}</code> · ${esc(publicCounts(pub))}</div>
            <div class="peer-list">${nodes.length ? nodes.map(publicNode).join('')
              : `<div class="empty" style="padding:12px">${esc(t('network.public_none'))}</div>`}</div>`;
  box.innerHTML = `
    <div class="setting-row" style="border:0;padding-top:0">
      <div class="txt"><b>${esc(t('network.public_title'))} <span class="pill">${esc(t('network.public_trial'))}</span></b>
        <small>${esc(t('network.public_help'))}</small></div>
      <label class="switch"><input type="checkbox" id="public-toggle" ${pub.wanted ? 'checked' : ''}
        ${pub.internet || pub.wanted ? '' : 'disabled'}><span></span></label>
    </div>
    ${detail}`;
  $('#public-toggle', box).onchange = async e => {
    try {
      state.network = await post('/network/public', { enabled: e.target.checked });
      lastJson = '';
      loadNetwork();
    } catch (err) {
      e.target.checked = !e.target.checked;
      errorToast(err);
    }
  };
}

// --------------------------------------------------------------------- état
function renderState(net) {
  const box = $('#net-state', root);
  // Réseau local coupé, mes machines restent jointes par Internet (net.enabled : le réseau tourne)
  const local = net.local ?? net.enabled;
  const detail = !local
    ? t(net.internet?.enabled ? 'network.local_off_internet' : 'network.local_off_nothing')
    : net.running
      ? t(net.discovery ? 'network.local_visible_discovery' : 'network.local_visible',
        { name: net.name, port: net.port })
      : t('network.starting');
  box.innerHTML = `
    <div class="setting-row" style="border:0;padding-top:0">
      <div class="txt"><b>${esc(t(local ? 'network.local_enabled' : 'network.local_disabled'))}</b><small>${esc(detail)}</small></div>
      <label class="switch"><input type="checkbox" id="net-toggle" ${local ? 'checked' : ''}><span></span></label>
    </div>
    ${net.error ? `<div class="banner warn" style="margin:8px 0 0">${icon('warn')}<div>${esc(net.error)}</div></div>` : ''}
    ${local || !onWindows() ? '' : `<div class="phase-note" style="margin-top:10px">${icon('shield')}<div>${t('network.firewall_html')}</div></div>`}`;
  $('#net-toggle', box).onchange = async e => {
    try {
      state.network = await post('/network/enable', { enabled: e.target.checked });
      lastJson = '';
      loadNetwork();
    } catch (err) { errorToast(err); }
  };
}

// ---------------------------------------------------------- machines appairées
/** powerText (dom.js) ne reçoit que des booléens et des nombres : rien d'autre du profil
 *  n'entre dans le HTML. */
function safePower(p) {
  const power = p.power && typeof p.power === 'object' ? p.power : {};
  return { on_ac_power: flag(p.on_ac_power),
    power: { mobile: power.mobile === true, plugged: flag(power.plugged),
      level: finite(power.level), temperature: finite(power.temperature) } };
}

// Profil envoyé par l'autre machine : chaque champ est typé (finite, count, flag) ou échappé
export function peerFacts(peer) {
  const p = peer.profile;
  if (!p || typeof p !== 'object') return `<span class="faint">${esc(t('network.profile_pending'))}</span>`;
  if (!p.memory) {                    // profil allégé : cette machine nous voit comme un ami
    const facts = [esc(t('network.friend_profile'))];
    if (p.lends_models) facts.push(esc(t('network.friend_lends_models', { n: count(p.ollama?.models) })));
    if (p.version) facts.push(`AIODOS ${esc(p.version)}`);
    return facts.join(' · ');
  }
  const gpu = Array.isArray(p.gpus) && p.gpus[0] && typeof p.gpus[0] === 'object' ? p.gpus[0] : null;
  const facts = [
    esc(gpu ? t('network.gpu', { name: gpu.name, vram: bytes(finite(gpu.vram_total) || 0) }) : t('network.no_gpu')),
    esc(t('network.ram', { size: bytes(finite(p.memory?.total) || 0) })),
    esc(t('network.models', { n: count(p.ollama?.models) })),
  ];
  if (p.lends_models) facts.push(esc(t('network.lends_models')));
  if (p.power?.mobile === true) {              // téléphone : charge, niveau, température
    const text = powerText(safePower(p));
    facts.push(esc(text.charAt(0).toLowerCase() + text.slice(1)));
  } else if (p.on_ac_power === false) facts.push(esc(t('network.on_battery')));
  const idle = finite(p.idle_seconds);
  if (idle != null) facts.push(esc(idle < 60 ? t('network.in_use') : t('network.idle_for', { time: duration(idle) })));
  if (p.version) facts.push(`AIODOS ${esc(p.version)}`);
  return facts.join(' · ');
}

/** « présentée par … » : la machine qui en a présenté une autre (appairage par groupe), par son nom. */
function presentedBy(peer, net) {
  if (!peer.introduced_by) return '';
  const by = net.peers.find(p => p.node_id === peer.introduced_by);
  return by ? t('network.introduced_by', { name: by.name }) : t('network.introduced_by_removed');
}

// Ce qui a été échangé avec une machine appairée (texte brut, une ligne chacun)
function reliability(peer) {
  return t('network.reliability', { facts: [
    t('network.reliability_checked', { n: peer.checked_units || 0 }),
    t('network.reliability_refused', { n: peer.refused_units || 0 }),
    t('network.reliability_failed', { n: peer.failed_units || 0 }),
  ].join(' · ') });
}

function sharedText(shared) {
  return t(shared.bytes ? 'network.shared_size' : 'network.shared',
    { n: shared.count, size: bytes(shared.bytes), ago: ago(shared.last) });
}

function consultedText(consulted) {
  const facts = [t('network.consulted_searches', { n: consulted.count }),
    t('network.consulted_passages', { n: consulted.passages })];
  if (consulted.refused) facts.push(t('network.consulted_refusals', { n: consulted.refused }));
  return t('network.consulted', { facts: facts.join(', '), ago: ago(consulted.last) });
}

function renderPeers(net) {
  const box = $('#net-peers', root);
  const intros = net.introductions || [];
  const offload = t(net.offload ? 'network.offload_on' : 'network.offload_off');
  box.innerHTML = `
    <h2>${icon('network')}${esc(t('network.peers_title'))}</h2>
    <p class="hint">${esc(t('network.peers_hint', { offload }))}</p>
    ${intros.length ? '<div class="peer-list intro-list"></div>' : ''}
    <div class="peer-list"></div>`;
  for (const o of intros) {                  // présentations qui attendent ma réponse
    const row = h(`
      <div class="peer intro"><div class="peer-head">${icon('network')}<b>${esc(o.machine)}</b>
        <span class="faint">${esc(t('network.intro_waiting', { presenter: o.presenter, ago: ago(o.created) }))}</span>
        <span style="flex:1"></span><button class="btn sm primary">${esc(t('network.answer'))}</button></div></div>`);
    row.querySelector('button').onclick = () => answerIntroduction(o);
    box.querySelector('.intro-list').append(row);
  }
  const list = box.querySelector('.peer-list:not(.intro-list)');
  if (!net.peers.length) {
    list.append(h(`<div class="empty" style="padding:18px">${esc(t('network.no_peers'))}</div>`));
    return;
  }
  for (const peer of net.peers) {
    const friend = peer.trust === 'friend';
    const shared = peer.shared;
    const consulted = peer.consulted;          // ses recherches dans mes bibliothèques ouvertes
    const presented = presentedBy(peer, net);
    // Raison gardée en base : un texte, ou [clé, paramètres] à traduire ici (tt)
    const reason = tt(peer.set_aside_reason) || t('network.check_failed');
    const row = h(`
      <div class="peer">
        <div class="peer-head">
          <span class="dot ${peer.online ? 'on' : ''}" title="${esc(t(peer.online ? 'network.online_title' : 'network.offline_title'))}"></span>
          <b>${esc(peer.name)}</b>
          ${friend ? `<span class="pill" title="${esc(t('network.friend_title'))}">${esc(t('network.friend'))}</span>` : ''}
          ${peer.set_aside_at ? `<span class="pill err">${esc(t('network.set_aside'))}</span>` : ''}
          ${presented ? `<span class="faint" title="${esc(t('network.introduced_title'))}">${esc(presented)}</span>` : ''}
          <span class="faint">${peer.address ? `${esc(peer.address)}:${esc(peer.port || '?')}`
            : esc(t(peer.net_id ? 'network.via_internet' : 'network.unknown_address'))}</span>
          <span class="faint">${esc(peer.online ? t('network.online')
            : peer.last_seen ? t('network.seen', { ago: ago(peer.last_seen) }) : t('network.never_seen'))}</span>
          <span style="flex:1"></span>
          <button class="icon-btn" data-act="refresh" title="${esc(t('network.refresh'))}">${icon('retry')}</button>
          <button class="icon-btn" data-act="more" title="${esc(t('network.options'))}">${icon('more')}</button>
          <button class="icon-btn danger" data-act="unpair" title="${esc(t('network.unpair'))}">${icon('x')}</button>
        </div>
        <div class="peer-facts">${peerFacts(peer)}${netFact(peer)}</div>
        ${offlineHint(peer, net)}
        ${peer.set_aside_at ? `<div class="peer-aside">${icon('warn')}<div>${esc(t('network.set_aside_text',
          { ago: ago(peer.set_aside_at), reason }))}</div>
          <button class="btn sm" data-act="restore">${esc(t('network.restore'))}</button></div>` : ''}
        ${peer.working_for_me ? `<div class="peer-work">${icon('bolt')}${esc(t('network.working_for_me',
          { n: peer.working_for_me }))}</div>` : ''}
        <div class="peer-ledger faint">${esc(t('network.ledger_received', { time: duration(peer.received_s), n: peer.received_units }))}
          · ${esc(t('network.ledger_given', { time: duration(peer.given_s), n: peer.given_units }))}</div>
        ${peer.received_chats || peer.given_chats ? `<div class="peer-ledger faint">${esc(t('network.ledger_chats',
          { received: peer.received_chats || 0, given: peer.given_chats || 0 }))}</div>` : ''}
        ${peer.checked_units || peer.refused_units || peer.failed_units
          ? `<div class="peer-ledger faint">${esc(reliability(peer))}</div>` : ''}
        ${shared ? `<div class="peer-ledger faint">${esc(sharedText(shared))}</div>` : ''}
        ${consulted ? `<div class="peer-ledger faint">${esc(consultedText(consulted))}</div>` : ''}
      </div>`);
    row.querySelector('[data-act="more"]').onclick = e => {
      e.stopPropagation();
      menu(e.currentTarget, [
        friend
          ? { label: t('network.set_mine'), icon: 'network', action: () => setTrust(peer, 'mine') }
          : { label: t('network.set_friend'), icon: 'shield', action: () => setTrust(peer, 'friend') },
        ...(friend && !peer.set_aside_at ? [{ label: t('network.present'), icon: 'network',
          action: () => presentMachines(peer) }] : []),
        ...(shared ? [{ label: t('network.show_shared'), icon: 'file', action: () => showShared(peer) }] : []),
        ...(consulted ? [{ label: t('network.show_consulted'), icon: 'search',
          action: () => showConsulted(peer) }] : []),
      ]);
    };
    row.querySelector('[data-act="restore"]')?.addEventListener('click', async () => {
      try { state.network = await post(`/network/peers/${encodeURIComponent(peer.node_id)}/restore`); lastJson = ''; loadNetwork(); }
      catch (e) { errorToast(e); }
    });
    row.querySelector('[data-act="refresh"]').onclick = async () => {
      try { state.network = await post(`/network/peers/${encodeURIComponent(peer.node_id)}/refresh`); lastJson = ''; loadNetwork(); }
      catch (e) { errorToast(e); }
    };
    row.querySelector('[data-act="unpair"]').onclick = async () => {
      // La machine d'un ami est retirée de toutes mes machines, sauf « seulement de celle-ci » ;
      // une de mes machines (perdue, volée) peut emporter les machines d'amis qu'elle a présentées
      const introduced = net.peers.filter(p => p.introduced_by === peer.node_id && p.trust === 'friend');
      const r = friend
        ? await modal({ title: t('network.unpair'), iconName: 'warn', danger: true, confirm: t('network.unpair_confirm'),
          text: t('network.unpair_friend_text', { name: peer.name }),
          check: t('network.unpair_only_here') })
        : introduced.length
          ? await modal({ title: t('network.unpair'), iconName: 'warn', danger: true, confirm: t('network.unpair_confirm'),
            text: t('network.unpair_introduced_text', { name: peer.name, n: introduced.length,
              names: names(introduced.map(p => p.name)) }),
            check: t('network.unpair_with_introduced') })
          : { ok: await confirmDanger(t('network.unpair'),
            t('network.unpair_text', { name: peer.name }), t('network.unpair_confirm')), checked: false };
      if (!r.ok) return;
      const query = friend ? `?only_here=${r.checked}` : r.checked ? '?with_introduced=true' : '';
      try {
        const done = await del(`/network/peers/${encodeURIComponent(peer.node_id)}${query}`);
        if (done?.missed?.length) {
          toast(t('network.unpair_missed', { n: done.missed.length, names: names(done.missed) }), 'info', 10000);
        }
        lastJson = '';
        loadNetwork();
      } catch (e) { errorToast(e); }
    };
    list.append(row);
  }
}

/** Une machine appairée hors ligne : ce qu'il faut vérifier pour la retrouver. */
function offlineHint(peer, net) {
  if (peer.online || peer.set_aside_at) return '';
  const hint = net.internet?.enabled
    ? (peer.net_id ? 'network.offline_hint' : 'network.offline_hint_peer_internet')
    : 'network.offline_hint_internet';
  return `<div class="peer-offline">${icon('info')}<div>${esc(t(hint))}</div></div>`;
}

function netFact(peer) {
  const inet = peer.internet;
  if (!inet?.connected) return '';
  const rtt = inet.rtt_ms ? ` (${Math.round(inet.rtt_ms)} ms)` : '';
  return ` · ${esc(t(inet.path === 'direct' ? 'network.internet_direct' : 'network.internet_relay'))}${rtt}`;
}

const names = list => list.map(name => t('network.quoted', { name })).join(', ');

/** « Présenter mes autres machines » : celles qui ne connaissent pas encore cette machine d'ami. */
async function presentMachines(peer) {
  toast(t('network.presenting', { name: peer.name }), 'info', 4000);
  let r;
  try { r = await post(`/network/peers/${encodeURIComponent(peer.node_id)}/present`); } catch (e) { errorToast(e); return; }
  const parts = [];
  if (r.introduced.length) parts.push(t('network.present_introduced', { names: names(r.introduced) }));
  if (r.asked.length) parts.push(t('network.present_asked', { names: names(r.asked) }));
  if (r.known.length) parts.push(t('network.present_known', { names: names(r.known) }));
  if (r.refused.length) parts.push(t('network.present_refused', { names: names(r.refused) }));
  if (r.unreachable.length) {
    parts.push(t('network.present_unreachable', { n: r.unreachable.length, names: names(r.unreachable) }));
  }
  toast(parts.length
    ? t('network.present_result', { name: peer.name, parts: parts.join(t('network.clause_separator')) })
    : t('network.present_none'), r.unreachable.length || r.refused.length ? 'info' : 'ok', 10000);
  lastJson = '';
  loadNetwork();
}

async function setTrust(peer, trust) {
  try {
    state.network = await post(`/network/peers/${encodeURIComponent(peer.node_id)}/trust`, { trust });
    toast(t(trust === 'friend' ? 'network.trust_friend_done' : 'network.trust_mine_done', { name: peer.name }),
      'ok', 7000);
    lastJson = '';
    loadNetwork();
  } catch (e) { errorToast(e); }
}

async function showShared(peer) {
  let entries;
  try { ({ entries } = await get(`/network/peers/${encodeURIComponent(peer.node_id)}/shared`)); } catch (e) { errorToast(e); return; }
  const lines = entries.map(e => `${new Date(e.at * 1000).toLocaleString(locale())} · ${e.what}`
    + `${e.sent_as && e.sent_as !== e.what ? ` → ${e.sent_as}` : ''}${e.size ? ` (${bytes(e.size)})` : ''}`);
  await modal({
    title: t('network.shared_title', { name: peer.name }), iconName: 'file', cancel: null, confirm: t('network.close'),
    text: t('network.shared_text'),
    quote: lines.join('\n') || t('network.nothing_yet'),
  });
}

async function showConsulted(peer) {
  let entries;
  try { ({ entries } = await get(`/network/peers/${encodeURIComponent(peer.node_id)}/consulted`)); } catch (e) { errorToast(e); return; }
  const lines = entries.map(e => {
    const when = new Date(e.at * 1000).toLocaleString(locale());
    // Raison gardée en base : un texte, ou [clé, paramètres] à traduire ici (tt)
    if (e.refused) return t('network.consulted_refused_line', { when, reason: tt(e.refused) });
    const docs = e.documents.map(d => `${d.document} (${d.library})`).join(', ');
    return t(docs ? 'network.consulted_line_docs' : 'network.consulted_line', { when, n: e.passages, docs });
  });
  await modal({
    title: t('network.consulted_title', { name: peer.name }), iconName: 'search', cancel: null,
    confirm: t('network.close'), text: t('network.consulted_text'),
    quote: lines.join('\n') || t('network.nothing_yet'),
  });
}

// ----------------------------------------------------------- machines détectées
function renderVisible(net) {
  const box = $('#net-visible', root);
  if (!net.running || !(net.local ?? true)) {
    box.innerHTML = `<h2>${icon('search')}${esc(t('network.visible_title'))}</h2>
      <p class="hint" style="margin:0">${esc(t('network.visible_off'))}</p>`;
    return;
  }
  box.innerHTML = `
    <h2>${icon('search')}${esc(t('network.visible_title'))}</h2>
    <p class="hint">${esc(t('network.visible_hint'))}</p>
    <div class="peer-list" id="visible-list"></div>
    <div class="toolbar" style="margin:14px 0 0">
      <input class="input" id="net-address" placeholder="${esc(t('network.address_placeholder'))}" style="max-width:360px">
      <button class="btn" id="net-add">${icon('plus')}${esc(t('network.add'))}</button>
    </div>`;
  const list = $('#visible-list', box);
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
  const pendingIds = new Set(net.pairing.map(p => p.node_id));
  for (const v of net.visible.filter(v => !pendingIds.has(v.node_id))) {
    const row = h(`
      <div class="peer"><div class="peer-head">
        <span class="dot on"></span><b>${esc(v.name)}</b>
        <span class="faint">${esc(v.address)}:${esc(v.port)}</span><span style="flex:1"></span>
        <button class="btn sm primary">${icon('shield')}${esc(t('network.pair'))}</button>
      </div></div>`);
    row.querySelector('button').onclick = () => startPairing(v);
    list.append(row);
  }
  if (!list.children.length) {
    list.append(h(`<div class="empty" style="padding:14px">${esc(t('network.no_visible'))}</div>`));
  }
  const add = async () => {
    const address = $('#net-address', box).value.trim();
    if (!address) return;
    try {
      const info = await post('/network/add', { address });
      toast(t('network.found', { name: info.name }), 'ok');
      lastJson = '';
      loadNetwork();
    } catch (e) { errorToast(e); }
  };
  $('#net-add', box).onclick = add;
  $('#net-address', box).onkeydown = e => { if (e.key === 'Enter') add(); };
}

// ------------------------------------------------------------------ appairage
// Ce qu'est l'autre machine pour celle-ci (network/sharing.py) : choisi sur chacune. Des
// clés de textes, traduites à l'ouverture de la fenêtre (trustChoices)
const TRUST_CHOICES = [
  { value: 'mine', label: 'network.trust_mine', help: 'network.trust_mine_help' },
  { value: 'friend', label: 'network.trust_friend', help: 'network.trust_friend_help' },
];
const trustChoices = () => TRUST_CHOICES.map(c => ({ value: c.value, label: t(c.label), help: t(c.help) }));

async function startPairing(v) {
  let pending;
  try { pending = await post('/network/pair', { node_id: v.node_id }); } catch (e) { errorToast(e); return; }
  await confirmPairing(pending, t('network.pair_check'));
}

let pairOpen = 0;                     // fenêtres d'appairage ouvertes en ce moment

/** Ce qu'une fenêtre d'appairage dit en plus de l'autre machine : par où elle est jointe,
 *  si elle est déjà appairée (accepter remplace sa clé), et si une autre demande est ouverte
 *  en même temps (quelqu'un d'autre tente peut-être de s'appairer). Texte brut : modal l'échappe. */
function pairDetails(p) {
  const parts = [];
  if (p.internet) parts.push(t('network.details_internet'));
  else if (p.address) parts.push(t('network.details_address', { address: p.address }));
  if (p.known) parts.push(t('network.details_known', { name: p.known }));
  const others = pairOpen + (state.network?.pairing || [])
    .filter(o => o.node_id !== p.node_id && !o.local_ok).length;
  if (others) parts.push(t('network.details_crowd'));
  return parts.map(part => ` ${part}`).join('');
}

/** La réponse vise la demande montrée (ref) ; replace : remplacer la clé d'une machine déjà appairée. */
const pairAnswer = (p, r) => post('/network/pair/answer', { node_id: p.node_id, accept: r.ok,
  trust: r.choice || 'mine', ref: p.ref, replace: !!p.known && r.ok });

/** Appairage commencé ici (réseau local ou invitation) : code vérifié, confiance choisie. */
async function confirmPairing(pending, text) {
  const details = pairDetails(pending);
  pairOpen += 1;
  let r;
  try {
    r = await modal({
      title: t('network.pair_with', { name: pending.name }), iconName: 'shield', code: pending.code,
      text: text + details, choices: trustChoices(), danger: !!pending.known,
      confirm: t(pending.known ? 'network.codes_match_replace' : 'network.codes_match'),
      cancel: t('ui.cancel'),
    });
  } finally { pairOpen -= 1; }
  try {
    const snap = await pairAnswer(pending, r);
    const done = snap.peers.some(p => p.node_id === pending.node_id);
    if (r.ok && !done) toast(t('network.confirmed_here'), 'info', 8000);
  } catch (e) { errorToast(e); }
  lastJson = '';
  loadNetwork();
}

/** Présentation reçue (appairage par groupe) : une autre machine de la même personne, présentée
 *  par une machine déjà appairée. Fermée sans répondre, elle attend dans « Machines appairées »
 *  (3 minutes) ; une seule question à la fois, les autres attendent là aussi. */
let introOpen = false;

export async function answerIntroduction(ev) {
  if (introOpen) return;
  introOpen = true;
  let r;
  try {
    r = await modal({
      title: t('network.intro_title'), iconName: 'network',
      text: t('network.intro_text', { presenter: ev.presenter, machine: ev.machine }),
      check: t('network.intro_always'),
      confirm: t('network.accept'), cancel: t('network.refuse'), cautious: true,
    });
  } finally { introOpen = false; }
  if (r.how === 'dismiss') return;
  try {
    state.network = await post('/network/introductions/answer',
      { intro: ev.intro, accept: r.ok, always: r.ok && r.checked });
  } catch (e) { errorToast(e); }
  lastJson = '';
  await loadNetwork();
  const next = (state.network?.introductions || [])[0];    // la suivante, s'il en reste une
  if (next) answerIntroduction(next);
}

/** Demande reçue d'une autre machine (affichée quelle que soit la vue ouverte). */
export async function answerPairRequest(ev) {
  const details = pairDetails(ev);
  pairOpen += 1;
  let r;
  try {
    r = await modal({
      title: t('network.request_title'), iconName: 'shield', code: ev.code,
      text: t('network.request_text', { name: ev.name }) + details,
      choices: trustChoices(), danger: !!ev.known, cautious: true,
      confirm: t(ev.known ? 'network.accept_replace' : 'network.accept'), cancel: t('network.refuse'),
    });
  } finally { pairOpen -= 1; }
  try {
    await pairAnswer(ev, r);
  } catch (e) { errorToast(e); }
  lastJson = '';
  loadNetwork();
}
