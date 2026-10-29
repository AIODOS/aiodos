// Ce qu'une machine fait avec celle-ci, en direct, pour la liste « Machines connectées »
// (connected.js, machine-row.js) et les machines au travail (jobs-workers.js) : par quel
// chemin elle est jointe, ce qu'elle prête, ce que chacune fait pour l'autre en ce moment
// (doublons de fin de partie compris, network/activity.py), et les courtes mentions de la
// ligne d'une machine (« doing », network/machines_view.py). Les textes (t, tt) sont du
// texte brut : échappés (esc) en entier, paramètres compris, avant d'entrer dans le HTML.
import { esc, ago, finite } from '../dom.js';
import { t, tt } from '../i18n.js';
import { icon } from '../icons.js';

const ROUTES = { lan: 'connected.route_lan', direct: 'connected.route_direct',
  relay: 'connected.route_relay', internet: 'connected.route_internet' };

const sum = items => (Array.isArray(items) ? items : []).reduce((n, item) => n + (finite(item?.units) || 0), 0);
// Prête sa puissance en ce moment ; d'un ami, on sait seulement si c'est activé
export const lendsNow = sharing => !!sharing && ('active' in sharing ? sharing.active : sharing.enabled);
// Pourquoi une machine ne prête pas en ce moment (« En pause : ce PC est sur batterie. »),
// écrit chez elle : dans la langue de l'interface s'il est venu traduisible (detail_t), sinon
// tel qu'elle l'a écrit (une version précédente, ou un texte inconnu ici)
export const pauseDetail = sharing => tt(sharing?.detail_t, sharing?.detail || '');

/** Par où une machine appairée est jointe, et son temps de réponse (HTML). */
export function link(peer) {
  if (!peer.online) {
    return esc(peer.last_seen ? t('connected.offline_seen', { ago: ago(peer.last_seen) }) : t('connected.never_seen'));
  }
  const live = peer.live || {};
  const rtt = finite(live.rtt_ms ?? live.internet_rtt_ms);
  return `${esc(t(ROUTES[live.route] || 'connected.online'))}${rtt != null ? ` · ${Math.round(rtt)} ms` : ''}`;
}

/** Ce qu'elle prête (HTML) : sa puissance (l'état du moment pour mes machines), ses modèles,
 *  sa carte graphique. */
export function lends(peer) {
  const p = peer.profile || {};
  const sharing = peer.live?.sharing;
  const pills = [];
  if (sharing ? sharing.enabled : p.images?.shares) {
    if (sharing && 'active' in sharing) {
      pills.push(sharing.active
        ? `<span class="pill ok">${esc(t('connected.lends_power'))}</span>`
        : `<span class="pill warn" title="${esc(pauseDetail(sharing))}">${esc(t('connected.power_paused'))}</span>`);
    } else {
      pills.push(`<span class="pill">${esc(t('connected.lends_power'))}</span>`);
    }
  }
  if (p.lends_models) pills.push(`<span class="pill">${esc(t('connected.lends_models'))}</span>`);
  if (p.cluster?.lends) pills.push(`<span class="pill">${esc(t('connected.lends_gpu'))}</span>`);
  return pills.length ? pills.join('') : `<span class="faint">${esc(t('connected.lends_nothing'))}</span>`;
}

/** En ce moment, dans les deux sens (HTML), avec en info-bulle les travaux concernés ; extra :
 *  d'autres mentions (HTML), mises à la suite. */
export function now(peer, extra = []) {
  const live = peer.live || {};
  const items = [];
  const forMe = sum(live.for_me);
  if (forMe) {
    // Le titre d'un de mes travaux : traduit s'il est venu traduisible (title_t, cf. jobs.js)
    const what = live.for_me.map(j => t('connected.job_units', { title: tt(j.title_t, j.title), n: j.units }))
      .join(', ');
    items.push(`<span class="live-in" title="${esc(what)}">${icon('bolt')}${esc(t('connected.computing_for_you',
      { n: forMe }))}</span>`);
  }
  const doubling = sum(live.copies_for_me);
  if (doubling) {
    const what = live.copies_for_me.map(j => t('connected.job_units', { title: tt(j.title_t, j.title), n: j.units }))
      .join(', ');
    items.push(`<span class="live-in" title="${esc(`${t('connected.doubling_title')} ${what}`)}">${icon('copy')}${
      esc(t('connected.doubling_for_you', { n: doubling }))}</span>`);
  }
  if (live.chats_for_me) {
    items.push(`<span class="live-in">${icon('chat')}${esc(t('connected.answering_your_chat'))}</span>`);
  }
  const forIt = Array.isArray(live.for_it) ? live.for_it : [];
  if (forIt.length) {
    items.push(`<span class="live-out" title="${esc(forIt.join(', '))}">${icon('cpu')}${esc(t('connected.you_compute',
      { n: forIt.length }))}</span>`);
  }
  if (live.chats_for_it) {
    items.push(`<span class="live-out">${icon('chat')}${esc(t('connected.you_answer_its_chat'))}</span>`);
  }
  items.push(...extra);
  return items.length ? items.join('')
    : `<span class="faint">${peer.online ? esc(t('connected.nothing_now')) : '—'}</span>`;
}

// Les mentions de la ligne d'une machine (« doing » de machines_view.py), en texte brut ;
// « in » : elle travaille pour moi, « out » : je travaille pour elle
const DOING = {
  for_me: ['in', d => t('connected.computing_for_you', { n: d.n })],
  copies_for_me: ['in', d => t('connected.doubling_for_you', { n: d.n })],
  chat_for_me: ['in', () => t('connected.answering_your_chat')],
  public_for_me: ['in', d => (d.model ? t('connected.public_answering', { model: d.model })
    : t('connected.answering_your_chat'))],
  for_it: ['out', d => t('connected.you_compute', { n: d.n })],
  chat_for_it: ['out', () => t('connected.you_answer_its_chat')],
};

/** Ce qu'une machine de la liste fait avec celle-ci : [{ what, dir, text }] (texte brut). Ce
 *  qui n'a pas la forme attendue est laissé de côté. */
export function doing(machine) {
  return (Array.isArray(machine?.doing) ? machine.doing : []).flatMap(d => {
    const known = d && typeof d === 'object' && Object.hasOwn(DOING, d.what) ? DOING[d.what] : null;
    if (!known) return [];
    const item = { n: finite(d.n) ?? 1, model: typeof d.model === 'string' ? d.model.slice(0, 64) : '' };
    return [{ what: d.what, dir: known[0], text: known[1](item) }];
  });
}

/** Elle travaille pour moi en ce moment (unités, doublons, conversations). */
export const helpsMe = machine => doing(machine).some(d => d.dir === 'in');
