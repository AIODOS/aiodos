// Une machine de la liste « Machines connectées » (connected.js) : une ligne courte (pastille
// d'état, nom ou « Inconnu a1b2c3d4… » pour un nœud du réseau public, sa sorte, puis ce qui
// compte : son état, ce qu'elle fait avec cette machine, ses modèles, la charge d'un
// téléphone), qui se déplie sur son détail et ses actions : appairer une machine détectée,
// vérifier le code d'un appairage, répondre à une présentation ; d'une machine appairée,
// actualiser, rétablir, ses options (confiance, présenter, ce qui lui a été confié, ses
// recherches) et retirer l'appairage (network-peers.js). Les entrées viennent de GET
// /api/network (« machines », network/machines_view.py) ; une machine appairée prend son
// détail dans « peers ». Ce qui vient d'une autre machine (nom, modèles, version) est du
// texte, échappé (esc) avant d'entrer dans le HTML ; les nombres passent par finite. D'un
// nœud public, jamais plus de SHORT caractères de sa clé.
import { esc, ago, finite, powerText } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { doing, link, lends, now } from './machine-live.js';
import { peerDetails, peerButtons, peerAction } from './network-peers.js';
import { startPairing, answerPairRequest, answerIntroduction } from './network-pairing.js';

const SHORT = 8;              // caractères d'une clé publique montrés, au plus (machines_view.SHORT)
const LINE_MODELS = 2;        // modèles nommés sur la ligne, les autres comptés (« +3 »)

const KINDS = { public: 'connected.kind_public', mine: 'connected.kind_mine',
  friend: 'connected.kind_friend', local: 'connected.kind_local' };
// L'état sur la ligne : son texte, et la couleur de la pastille et du texte
const STATES = {
  free: ['connected.state_free', 'on', 'ok'],
  busy: ['connected.state_busy', 'busy', 'run'],
  paused: ['connected.state_paused', 'warn', 'warn'],
  online: ['connected.state_online', 'on', ''],
  detected: ['connected.state_detected', 'on', ''],
  pairing: ['connected.state_pairing', 'on', 'run'],
  confirm: ['connected.state_confirm', 'warn', 'warn'],
};

const text = (value, limit = 200) => (typeof value === 'string' ? value.slice(0, limit) : '');
const list = value => (Array.isArray(value) ? value : []);

/** Les premiers caractères de la clé d'un nœud public : SHORT au plus, quoi qu'on reçoive. */
export const shortKey = m => text(m?.short, SHORT);

/** Le nom montré (texte brut) : le sien, ou « Inconnu a1b2c3d4… » pour un nœud public. */
export function machineName(m) {
  return m?.kind === 'public' ? t('connected.stranger', { key: shortKey(m) }) : (text(m?.name, 60) || '?');
}

const names = value => list(value).filter(n => typeof n === 'string' && n).map(n => n.slice(0, 64));

/** « qwen3.5:9b, gemma3:4b +3 » (texte brut). */
function modelsLine(m) {
  const found = names(m.models);
  if (!found.length) return '';
  const total = Math.max(finite(m.models_n) ?? 0, found.length);
  const rest = total - Math.min(LINE_MODELS, found.length);
  return `${found.slice(0, LINE_MODELS).join(', ')}${rest > 0 ? ` +${rest}` : ''}`;
}

/** Les modèles d'images qu'un nœud public prête aux inconnus (« images : z-image-turbo »). */
function imagesLine(m) {
  const found = names(m.public?.images);
  if (!found.length) return '';
  return t('connected.images_line', { list: `${found[0]}${found.length > 1 ? ` +${found.length - 1}` : ''}` });
}

/** La charge d'un téléphone (« en charge · batterie 84 % »), un PC sur batterie. */
function powerLine(m) {
  const p = m.power && typeof m.power === 'object' ? m.power : null;
  if (!p) return '';
  if (p.mobile !== true) return p.plugged === false ? t('network.on_battery') : '';
  const line = powerText({ power: { mobile: true, plugged: typeof p.plugged === 'boolean' ? p.plugged : null,
    level: finite(p.level) } });
  return line.charAt(0).toLowerCase() + line.slice(1);
}

function seenAgo(m) {
  if (m.kind === 'public') return ago(Date.now() / 1000 - (finite(m.seen_s) ?? 0));
  const seen = finite(m.seen);
  return seen ? ago(seen) : '';
}

/** L'état de la ligne (HTML) et la couleur de sa pastille. */
function stateOf(m) {
  if (m.state === 'set_aside') {
    return [`<span class="pill err">${esc(t('network.set_aside'))}</span>`, 'warn'];
  }
  if (m.state === 'offline') {
    const seen = seenAgo(m);
    return [esc(seen ? t('connected.offline_seen', { ago: seen }) : t('connected.never_seen')), ''];
  }
  if (m.state === 'introduced') {
    return [`<span class="warn">${esc(t('network.intro_waiting', { presenter: text(m.intro?.presenter, 60),
      ago: seenAgo(m) }))}</span>`, 'warn'];
  }
  const [key, dot, cls] = STATES[m.state] || STATES.online;
  return [cls ? `<span class="${cls}">${esc(t(key))}</span>` : esc(t(key)), dot];
}

/** La ligne repliée (HTML) : pastille, nom, sorte, puis l'état et ce qui compte. */
function lineHtml(m) {
  const [stateHtml, dot] = stateOf(m);
  const facts = [stateHtml,
    ...doing(m).map(d => `<span class="live-${d.dir}">${esc(d.text)}</span>`),
    ...[modelsLine(m), imagesLine(m), powerLine(m)].filter(Boolean).map(esc)];
  const kind = KINDS[m.kind] ? `<span class="mx-kind"${m.kind === 'friend' ? ` title="${esc(t('network.friend_title'))}"`
    : ''}>${esc(t(KINDS[m.kind]))}</span>` : '';
  return `<summary>
      <span class="dot ${dot}"></span><b class="mx-name">${esc(machineName(m))}</b>${kind}
      <span class="mx-facts">${facts.join('<span class="mx-sep"> · </span>')}</span>
      <span class="mx-chev">${icon('chev-right')}</span>
    </summary>`;
}

// ------------------------------------------------------------------------- détail
const row = (label, html) => `<div class="mx-row"><span class="mx-label">${esc(label)}</span><span>${html}</span></div>`;

/** Un appairage en cours sur cette machine : son code, par où, qui doit confirmer. */
function pairingHtml(m) {
  const p = m.pairing;
  if (!p || typeof p !== 'object') return '';
  const facts = [t('network.pairing_code', { code: text(p.code, 12) })];
  if (p.internet) facts.push(t('network.via_internet'));
  else if (p.address) facts.push(text(p.address, 64));
  facts.push(t(p.local_ok ? 'network.pairing_waiting' : 'network.pairing_confirm_here'));
  return `<div class="peer-facts">${icon('shield')} ${esc(facts.join(' · '))}</div>`;
}

const button = (act, label, cls = '') => `<button type="button" class="btn sm ${cls}" data-act="${act}">${esc(label)}</button>`;

function publicDetails(m) {
  const facts = m.public && typeof m.public === 'object' ? m.public : {};
  const free = finite(facts.free);
  const tps = finite(facts.tps);
  const lending = facts.lends === true
    ? t(free ? 'connected.public_lends_free' : free === 0 ? 'connected.public_lends_busy' : 'connected.public_lends')
    : t('connected.public_lends_no');
  const models = names(m.models);
  const images = names(facts.images);
  const live = doing(m);
  const head = [t('connected.public_key_short', { key: shortKey(m) })];
  if (m.version) head.push(`AIODOS ${text(m.version, 40)}`);
  head.push(t('network.public_seen', { ago: seenAgo(m) }));
  return `<p class="hint mx-hint">${esc(t('connected.public_stranger'))}</p>
    <div class="peer-facts">${esc(head.join(' · '))}</div>
    <div class="peer-facts">${esc(tps ? `${lending} · ${t('connected.public_speed', { n: Math.round(tps) })}` : lending)}</div>
    <div class="peer-facts">${esc(models.length || images.length
      ? [models.length ? t('connected.models_list', { list: models.join(', ') }) : '',
        images.length ? t('connected.images_list', { list: images.join(', ') }) : ''].filter(Boolean).join(' · ')
      : t('network.public_no_models'))}</div>
    ${live.length ? `<div class="peer-facts">${live.map(d => `<span class="live-${d.dir}">${esc(d.text)}</span>`)
      .join(' · ')}</div>` : ''}`;
}

function pairedDetails(m, net) {
  const peer = list(net?.peers).find(p => p?.node_id === m.node_id);
  if (!peer) return '';
  const paths = list(m.paths);
  const where = [link(peer)];
  const address = m.address ? `${m.address}${m.port ? `:${m.port}` : ''}` : '';
  if (address) where.push(esc(t('connected.address', { address })));
  else where.push(esc(t(peer.net_id ? 'network.via_internet' : 'network.unknown_address')));
  if (paths.includes('public') && m.public) {
    where.push(esc(t('connected.also_public', { key: text(m.public.short, SHORT) })));
  }
  // now(peer) dit ce qu'elle fait comme machine appairée ; s'y ajoute ce qu'elle calcule pour
  // moi comme nœud du réseau public (une de mes machines vue aussi dans l'annuaire)
  const publicDoing = doing(m).filter(d => d.what === 'public_for_me');
  const acts = [m.pairing && !m.pairing.local_ok ? button('check_code', t('network.check_code'), 'primary') : '',
    peerButtons(peer)];
  return `<div class="mx-rows">
      ${row(t('connected.col_link'), where.join(' · '))}
      ${row(t('connected.col_lends'), `<span class="caps">${lends(peer)}</span>`)}
      ${row(t('connected.col_now'), `<span class="live-now">${now(peer, publicDoing.map(d =>
        `<span class="live-in">${icon('chat')}${esc(d.text)}</span>`))}</span>`)}
    </div>
    ${peerDetails(peer, net)}
    ${pairingHtml(m)}
    <div class="mx-acts">${acts.join('')}</div>`;
}

function localDetails(m) {
  const facts = [];
  const address = m.address ? `${m.address}${m.port ? `:${m.port}` : ''}` : '';
  if (address) facts.push(t('connected.address', { address }));
  const seen = seenAgo(m);
  if (seen) facts.push(t('network.seen', { ago: seen }));
  const acts = list(m.actions).includes('pair') ? button('pair', t('network.pair'), 'primary')
    : m.pairing && !m.pairing.local_ok ? button('check_code', t('network.check_code'), 'primary') : '';
  return `${m.pairing ? '' : `<p class="hint mx-hint">${esc(t('connected.local_hint'))}</p>`}
    ${facts.length ? `<div class="peer-facts">${esc(facts.join(' · '))}</div>` : ''}
    ${pairingHtml(m)}
    ${acts ? `<div class="mx-acts">${acts}</div>` : ''}`;
}

function introDetails(m) {
  const o = m.intro && typeof m.intro === 'object' ? m.intro : {};
  return `<p class="hint mx-hint">${esc(t('connected.intro_hint', { presenter: text(o.presenter, 60) }))}</p>
    <div class="mx-acts">${button('answer_intro', t('network.answer'), 'primary')}</div>`;
}

function detailsHtml(m, net) {
  if (m.kind === 'public') return publicDetails(m);
  if (m.intro) return introDetails(m);
  if (m.id?.startsWith('peer:')) return pairedDetails(m, net);
  return localDetails(m);
}

/** Une ligne de la liste (HTML) : repliée, ou dépliée sur son détail (open). */
export function machineHtml(m, net, open = false) {
  const cls = ['mx', m.state === 'offline' ? 'off' : '', m.attention ? 'attn' : ''].filter(Boolean).join(' ');
  return `<details class="${cls}" data-row="${esc(text(m.id, 200))}"${m.attention ? ' data-attn="1"' : ''}${open ? ' open' : ''}>
    ${lineHtml(m)}
    <div class="mx-body">${detailsHtml(m, net)}</div></details>`;
}

/** Une action d'une ligne (data-act) ; anchor : le bouton (le menu des options s'y ouvre). */
export function machineAction(act, m, net, anchor) {
  if (act === 'pair' && m.node_id) return startPairing({ node_id: m.node_id });
  if (act === 'check_code' && m.pairing) return answerPairRequest(m.pairing);
  if (act === 'answer_intro' && m.intro) return answerIntroduction(m.intro);
  const peer = list(net?.peers).find(p => p?.node_id === m.node_id);
  return peer ? peerAction(act, peer, net, anchor) : undefined;
}
