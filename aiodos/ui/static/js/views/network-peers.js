// Réseau → Machines appairées : les présentations qui attendent une réponse, puis une ligne par
// machine (en ligne ou non, ses actions) qui s'ouvre sur le détail (son profil, ce qui a été
// échangé). Une section repliable de la vue Réseau (network.js) : son résumé, son contenu.
import { get, post, del } from '../api.js';
import { h, esc, ago, bytes, duration, powerText, finite, count } from '../dom.js';
import { t, tt, locale } from '../i18n.js';
import { icon } from '../icons.js';
import { state, emit } from '../state.js';
import { modal, menu, toast, errorToast, confirmDanger } from '../ui.js';
import { answerIntroduction } from './network-pairing.js';

const reload = () => emit('network:reload');

// Ce que disent les autres machines (profil d'une machine appairée) vient d'ailleurs : typé
// avant d'entrer dans le HTML. Un objet à la place d'un nombre (« length » piégé) ou du HTML à
// la place d'un niveau de batterie ne passe pas. Les gardes des nombres et des listes sont
// celles de dom.js (finite, count), partagées avec les autres vues : une seule copie, qui ne
// peut pas diverger d'une vue à l'autre. Les textes (t, tt) sont du texte brut : échappés (esc)
// en entier, paramètres compris, avant d'entrer dans le HTML.
const flag = v => (typeof v === 'boolean' ? v : null);

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

/** Le résumé de la ligne de titre (HTML) : « 3 machines, 2 en ligne », et ce qui attend. */
export function peersSummary(net) {
  const peers = net.peers || [];
  const intros = (net.introductions || []).length;
  const aside = peers.filter(p => p.set_aside_at).length;
  const parts = [esc(peers.length
    ? t('network.peers_sub', { n: peers.length, online: peers.filter(p => p.online).length })
    : t('network.peers_sub_none'))];
  if (intros) parts.push(`<span class="pill warn">${esc(t('network.intros_pending', { n: intros }))}</span>`);
  if (aside) parts.push(`<span class="pill err">${esc(t('network.set_aside_count', { n: aside }))}</span>`);
  return parts.join(' · ');
}

/** Ce qui ouvre la section d'office : une présentation à accepter, une machine mise de côté. */
export function peersAttention(net) {
  return [...(net.introductions || []).map(o => o.intro),
    ...(net.peers || []).filter(p => p.set_aside_at).map(p => p.node_id)].join('|');
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

// Les machines dont le détail est ouvert : la liste est redessinée à chaque nouvelle
const opened = new Set();

/** Le contenu de la section : les présentations en attente, puis les machines appairées. */
export function renderPeers(box, net) {
  const intros = net.introductions || [];
  const offload = t(net.offload ? 'network.offload_on' : 'network.offload_off');
  box.innerHTML = `
    <p class="hint">${esc(t('network.peers_hint', { offload }))}</p>
    ${intros.length ? '<div class="peer-list intro-list"></div>' : ''}
    <div class="peer-list"></div>`;
  for (const o of intros) {                  // présentations qui attendent ma réponse
    const row = h(`
      <div class="peer intro"><div class="peer-head">${icon('network')}<b>${esc(o.machine)}</b>
        <span class="faint">${esc(t('network.intro_waiting', { presenter: o.presenter, ago: ago(o.created) }))}</span>
        <span class="net-grow"></span><button class="btn sm primary">${esc(t('network.answer'))}</button></div></div>`);
    row.querySelector('button').onclick = () => answerIntroduction(o);
    box.querySelector('.intro-list').append(row);
  }
  const list = box.querySelector('.peer-list:not(.intro-list)');
  if (!net.peers.length) {
    list.append(h(`<div class="empty net-empty">${esc(t('network.no_peers'))}</div>`));
    return;
  }
  for (const peer of net.peers) list.append(peerRow(peer, net));
}

function peerRow(peer, net) {
  const friend = peer.trust === 'friend';
  const shared = peer.shared;
  const consulted = peer.consulted;          // ses recherches dans mes bibliothèques ouvertes
  const presented = presentedBy(peer, net);
  // Raison gardée en base : un texte, ou [clé, paramètres] à traduire ici (tt)
  const reason = tt(peer.set_aside_reason) || t('network.check_failed');
  // Une ligne (en ligne ou non, ses actions), le détail à l'ouverture ; mise de côté, elle
  // le dit sans qu'il faille l'ouvrir
  const row = h(`
    <div class="peer">
      <details class="peer-more"${opened.has(peer.node_id) ? ' open' : ''}>
        <summary class="peer-head">
          <span class="dot ${peer.online ? 'on' : ''}" title="${esc(t(peer.online ? 'network.online_title' : 'network.offline_title'))}"></span>
          <b>${esc(peer.name)}</b>
          ${friend ? `<span class="pill" title="${esc(t('network.friend_title'))}">${esc(t('network.friend'))}</span>` : ''}
          ${peer.set_aside_at ? `<span class="pill err">${esc(t('network.set_aside'))}</span>` : ''}
          <span class="faint">${peer.address ? `${esc(peer.address)}:${esc(peer.port || '?')}`
            : esc(t(peer.net_id ? 'network.via_internet' : 'network.unknown_address'))}</span>
          <span class="faint">${esc(peer.online ? t('network.online')
            : peer.last_seen ? t('network.seen', { ago: ago(peer.last_seen) }) : t('network.never_seen'))}</span>
          <span class="peer-acts">
            <button class="icon-btn" data-act="refresh" title="${esc(t('network.refresh'))}">${icon('retry')}</button>
            <button class="icon-btn" data-act="more" title="${esc(t('network.options'))}">${icon('more')}</button>
            <button class="icon-btn danger" data-act="unpair" title="${esc(t('network.unpair'))}">${icon('x')}</button>
            <span class="peer-chev">${icon('chev-right')}</span>
          </span>
        </summary>
        <div class="peer-facts">${peerFacts(peer)}${netFact(peer)}</div>
        ${presented ? `<div class="peer-ledger faint" title="${esc(t('network.introduced_title'))}">${esc(presented)}</div>` : ''}
        ${offlineHint(peer, net)}
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
      </details>
      ${peer.set_aside_at ? `<div class="peer-aside">${icon('warn')}<div>${esc(t('network.set_aside_text',
        { ago: ago(peer.set_aside_at), reason }))}</div>
        <button class="btn sm" data-act="restore">${esc(t('network.restore'))}</button></div>` : ''}
    </div>`);
  const details = row.querySelector('details');
  details.addEventListener('toggle', () => {
    if (details.open) opened.add(peer.node_id); else opened.delete(peer.node_id);
  });
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
    try { state.network = await post(`/network/peers/${encodeURIComponent(peer.node_id)}/restore`); reload(); }
    catch (e) { errorToast(e); }
  });
  row.querySelector('[data-act="refresh"]').onclick = async () => {
    try { state.network = await post(`/network/peers/${encodeURIComponent(peer.node_id)}/refresh`); reload(); }
    catch (e) { errorToast(e); }
  };
  row.querySelector('[data-act="unpair"]').onclick = () => unpair(peer, net);
  return row;
}

async function unpair(peer, net) {
  const friend = peer.trust === 'friend';
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
    reload();
  } catch (e) { errorToast(e); }
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
  reload();
}

async function setTrust(peer, trust) {
  try {
    state.network = await post(`/network/peers/${encodeURIComponent(peer.node_id)}/trust`, { trust });
    toast(t(trust === 'friend' ? 'network.trust_friend_done' : 'network.trust_mine_done', { name: peer.name }),
      'ok', 7000);
    reload();
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
