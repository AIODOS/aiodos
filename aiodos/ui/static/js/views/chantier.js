// Tâches : le chantier en direct d'un travail ouvert (docs/conception/essaim.md § 13, étape 7,
// accepté par l'auteur le 27/10/2026), dans une section repliable du détail du travail :
// chaque fragment (une unité) avec son état, la machine qui le calcule (cette machine, une de
// mes machines ou un ami par son nom, le réseau public sans nom), depuis quand, son étape et
// son avancement estimé quand on les connaît, ses doublons de fin de partie (« doublée par »,
// puis « faite par » et « battue ») ; au-dessus, les machines du chantier et leurs places (les
// points de la ligne des ressources de jobs.js, placeDots, repris par jobs-workers.js).
//
// Lu à la demande (GET /api/jobs/{id}/chantier, jobs/chantier.py), section ouverte seulement :
// quand les compteurs du travail changent (ses événements « job », déjà reçus par jobs.js), et
// toutes les REFRESH_MS pendant le calcul (l'étape d'une unité n'a pas d'événement) ; jamais
// plus d'une lecture par MIN_GAP_MS. Entre deux lectures, les durées avancent ici, à l'heure
// du nœud (skew). Tout ce qui vient du nœud, noms de machines compris, est échappé (esc).
import { get } from '../api.js';
import { esc, ago, duration } from '../dom.js';
import { t, has } from '../i18n.js';
import { icon } from '../icons.js';
import { state, prefs } from '../state.js';

const REFRESH_MS = 5000;           // relecture pendant le calcul (étapes, places)
const MIN_GAP_MS = 1000;           // une lecture par seconde au plus, même si tout bouge
const RETRY_MS = 8000;             // après une lecture ratée
const MAX_PLACES = 64;             // comme network/places_seen.py
const ACTIVE = ['queued', 'running', 'paused'];
const RESOURCES = ['gpu', 'embed', 'cpu', 'net'];
const PILLS = { running: 'run', queued: 'idle', done: 'ok', skipped: 'idle', failed: 'err', cancelled: 'idle' };
const SUBS = ['running', 'queued', 'done', 'skipped', 'failed', 'cancelled'];   // chantier.sub_…
// La section ouverte ou fermée, pour tous les travaux (ouverte d'abord), comme fold.js
const OPEN_PREF = 'fold.chantier';

const sites = new Map();           // travail -> { data, sig, at, loading, failed, skew, html }

const resourceLabel = name => (RESOURCES.includes(name) ? t(`jobs.resource_${name}`) : String(name));
const whole = (v, max = Infinity) => (Number.isInteger(v) ? Math.min(max, Math.max(0, v)) : 0);
const time = v => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** Les places d'une machine en petits points (comme la ligne des ressources de la vue Tâches) :
 *  un point par place, plein s'il est pris ; names : les ressources à montrer (toutes sinon). */
export function placeDots(places, busy, names = null) {
  if (!places || typeof places !== 'object') return '';
  return (names || RESOURCES).filter(name => Number.isInteger(places[name])).map(name => {
    const n = whole(places[name], MAX_PLACES);
    const taken = Math.min(n, whole(busy?.[name]));
    const dots = Array.from({ length: n }, (_, i) => `<i class="${i < taken ? 'busy' : ''}"></i>`).join('');
    return `<span class="slot">${dots} ${esc(resourceLabel(name))} ${taken}/${n}</span>`;
  }).join('');
}

/** Qui calcule, dans une phrase : « cette machine », « « PC fixe » », « une machine du réseau
 *  public » (jamais nommée), « une autre machine ». Texte brut. */
export function who(m) {
  if (m?.kind === 'here') return t('chantier.who_here');
  if (m?.kind === 'public') return t('chantier.who_public');
  if ((m?.kind === 'mine' || m?.kind === 'friend') && m.name) return t('chantier.who_named', { name: String(m.name) });
  return t('chantier.who_other');
}

// Une machine en tête de la liste des machines du chantier
function chipName(m) {
  if (m?.kind === 'here') return t('chantier.here');
  if (m?.kind === 'public') return t('chantier.public');
  return m?.name ? String(m.name) : t('jobs.another_machine');
}

// Le résumé, sur la ligne de la section : d'après le travail lui-même (sans lecture)
function sub(j) {
  const running = whole(j.local) + Object.values(j.remote || {}).reduce((n, k) => n + whole(k), 0);
  const counts = { running, done: whole(j.done), skipped: whole(j.skipped), failed: whole(j.failed) };
  if (ACTIVE.includes(j.status)) {
    counts.queued = Math.max(0, whole(j.total) - counts.done - counts.skipped - counts.failed - running);
  }
  const parts = SUBS.filter(s => counts[s]).map(s => t(`chantier.sub_${s}`, { n: counts[s] }));
  if (j.copies) parts.push(t('chantier.sub_copies', { n: whole(j.copies) }));
  return parts.join(' · ');
}

// Ce qui, dans le travail, dit qu'il faut relire le chantier
const signature = j => [j.status, j.done, j.skipped, j.failed, j.local, JSON.stringify(j.remote || {}),
  j.copies || 0, JSON.stringify(j.copies_remote || {})].join('|');

// L'avancement estimé : le temps écoulé comparé à la durée habituelle (jobs/chantier.py)
function about(elapsed, typical) {
  const usual = time(typical);
  if (!usual || usual <= 0) return '';
  const n = Math.floor(100 * Math.min(0.95, Math.max(0, elapsed) / usual));
  return `<span title="${esc(t('chantier.about_title', { time: duration(usual) }))}">${esc(t('chantier.about', { n }))}</span>`;
}

function stage(name) {
  const key = `chantier.stage_${name}`;
  return typeof name === 'string' && has(key) ? esc(t(key)) : '';
}

const line = (parts, cls = '', title = '') =>
  `<div class="cu-line${cls ? ` ${cls}` : ''}"${title ? ` title="${esc(title)}"` : ''}>${parts.filter(Boolean).join(' · ')}</div>`;

// Ce que dit un fragment selon son état (texte échappé, morceaux déjà en HTML)
function details(u, now) {
  const lines = [];
  if (u.status === 'running') {
    const started = time(u.started);
    lines.push(line([esc(t('chantier.on', { who: who(u.machine) })),
      started !== null ? esc(t('chantier.since', { time: duration(now - started) })) : '',
      started !== null ? about(now - started, u.typical) : '', stage(u.stage)]));
    for (const c of Array.isArray(u.copies) ? u.copies : []) {
      const since = time(c.started);
      lines.push(line([`${icon('copy')}${esc(t('chantier.doubled_by', { who: who(c.machine) }))}`,
        since !== null ? esc(t('chantier.since', { time: duration(now - since) })) : '',
        since !== null ? about(now - since, c.typical) : ''], 'cu-copy', t('chantier.doubled_title')));
    }
  } else if (u.status === 'queued') {
    const since = time(u.since);
    lines.push(line([since !== null ? esc(t('chantier.queued_since', { time: duration(now - since) })) : '',
      u.retry ? esc(t('chantier.retry')) : '']));
  } else if (u.status === 'done') {
    const took = time(u.compute_s);
    const beaten = (Array.isArray(u.beaten) ? u.beaten : []).map(m =>
      `<span class="cu-beaten" title="${esc(t('chantier.beaten_title'))}">${esc(t('chantier.beaten', { who: who(m) }))}</span>`);
    lines.push(line([esc(t('chantier.done_by', { who: who(u.machine) })),
      took !== null && took > 0 ? esc(t('chantier.done_in', { time: duration(took) })) : '', ...beaten]));
  } else if (u.status === 'skipped') {
    lines.push(line([esc(t('chantier.skipped_by', { who: who(u.machine) }))]));
  } else if (u.status === 'failed') {
    lines.push(line([esc(t('chantier.failed_at', { who: who(u.machine) }))]));
    if (u.error) lines.push(line([esc(u.error)], 'cu-error', String(u.error)));
  }
  return lines.join('');
}

function unitRow(u, now) {
  const status = PILLS[u.status] ? u.status : 'cancelled';
  const doubled = status === 'running' && Array.isArray(u.copies) && u.copies.length > 0;
  const [cls, text] = doubled ? ['run copy', t('chantier.status_doubled')] : [PILLS[status], t(`chantier.status_${status}`)];
  const n = whole(u.n);
  const label = u.label ? String(u.label) : t('chantier.fragment', { n });
  return `<li class="cu cu-${status}">
    <span class="cu-n">${n ? `#${n}` : ''}</span>
    <span class="cu-label" title="${esc(label)}">${esc(label)}</span>
    <span class="pill ${cls}">${esc(text)}</span>
    ${details(u, now)}
  </li>`;
}

// Les machines du chantier, chacune avec ses fragments, ses doublons et ses places ; celles de
// cette machine en direct (state.executor), celles d'une autre telles qu'elle les a dites
function machinesHtml(data) {
  const names = Array.isArray(data.resources) && data.resources.length ? data.resources : null;
  const chips = (Array.isArray(data.machines) ? data.machines : []).map(entry => {
    const m = entry.machine || {};
    let seen = entry.places;
    let title = '';
    if (m.kind === 'here') {
      const ex = state.executor;
      if (ex?.slots) seen = { places: ex.slots, busy: ex.busy };
      title = t('chantier.places_here_title');
    } else if (time(seen?.at) !== null) {
      title = t('chantier.places_title', { ago: ago(seen.at) });
    }
    const counts = [entry.units ? t('chantier.units', { n: whole(entry.units) }) : '',
      entry.copies ? t('chantier.copies', { n: whole(entry.copies) }) : ''].filter(Boolean).join(' · ');
    const dots = seen ? placeDots(seen.places, seen.busy, names) : '';
    const places = dots || (m.kind === 'public' ? '' : `<span class="faint">${esc(t('chantier.places_unknown'))}</span>`);
    return `<span class="chantier-machine"${title ? ` title="${esc(title)}"` : ''}><span class="dot on"></span><b>${
      esc(chipName(m))}</b>${counts ? `<span class="faint">${esc(counts)}</span>` : ''}${places}</span>`;
  });
  return chips.length ? `<div class="chantier-machines"><span class="chantier-label">${esc(t('chantier.machines'))}</span>${chips.join('')}</div>` : '';
}

/** Le corps du chantier (HTML) d'après sa dernière lecture ; now : l'heure du nœud (s). */
export function chantierHtml(data, now) {
  const units = Array.isArray(data?.units) ? data.units : [];
  const hidden = Object.entries(data?.hidden || {}).filter(([s, n]) => SUBS.includes(s) && whole(n))
    .map(([s, n]) => t(`chantier.sub_${s}`, { n: whole(n) }));
  return `${machinesHtml(data || {})}
    ${units.length ? `<ol class="chantier-units">${units.map(u => unitRow(u, now)).join('')}</ol>`
      : `<p class="chantier-note">${esc(t('chantier.empty'))}</p>`}
    ${hidden.length ? `<p class="chantier-note">${esc(t('chantier.more', { list: hidden.join(' · ') }))}</p>` : ''}`;
}

function siteOf(id) {
  if (!sites.has(id)) sites.set(id, { data: null, sig: '', at: 0, loading: false, failed: false, skew: 0, html: '' });
  return sites.get(id);
}

async function load(j, site, box) {
  site.loading = true;
  site.sig = signature(j);
  site.at = Date.now();
  try {
    const data = await get(`/jobs/${j.id}/chantier`);
    site.data = data;
    site.failed = false;
    site.skew = (time(data?.now) ?? Date.now() / 1000) - Date.now() / 1000;
  } catch {
    site.failed = true;
  } finally {
    site.loading = false;
    site.at = Date.now();
  }
  if (box.isConnected) paint(box, site);
}

function paint(box, site) {
  const body = box.querySelector('.chantier-body');
  if (!body) return;
  const html = site.data ? chantierHtml(site.data, Date.now() / 1000 + site.skew)
    : `<p class="chantier-note">${esc(t(site.failed ? 'chantier.unavailable' : 'chantier.loading'))}</p>`;
  if (html === site.html && body.childElementCount) return;    // rien de changé : la sélection reste
  site.html = html;
  body.innerHTML = html;
}

/** Le chantier d'un travail dans sa ligne ouverte (jobs.js, à chaque rendu) : la section, son
 *  résumé, et sa lecture quand il le faut. box : le conteneur .job-chantier de la ligne. */
export function showChantier(box, j) {
  if (!box || !j) return;
  const site = siteOf(j.id);
  if (!box.firstElementChild) {
    box.innerHTML = `<details class="chantier"${prefs.get(OPEN_PREF, true) ? ' open' : ''}>
      <summary title="${esc(t('chantier.hint'))}">${icon('layers')}<span class="chantier-title">${esc(t('chantier.title'))}</span>
        <span class="chantier-sub"></span><span class="fold-chev">${icon('chev-right')}</span></summary>
      <div class="chantier-body"></div></details>`;
    site.html = '';
    const fold = box.firstElementChild;
    fold.addEventListener('toggle', () => {
      prefs.set(OPEN_PREF, fold.open);
      if (fold.open) showChantier(box, state.jobs.get(j.id) || j);
    });
  }
  const fold = box.firstElementChild;
  const text = sub(j);
  const subEl = fold.querySelector('.chantier-sub');
  if (subEl.textContent !== text) subEl.textContent = text;
  if (!fold.open) return;
  const now = Date.now();
  const due = (signature(j) !== site.sig && now - site.at >= MIN_GAP_MS)
    || (ACTIVE.includes(j.status) && now - site.at >= REFRESH_MS)
    || (site.failed && now - site.at >= RETRY_MS);
  if (due && !site.loading && !document.hidden) load(j, site, box);
  paint(box, site);
}

/** Le travail a quitté la liste : son chantier est oublié. */
export function forgetChantier(id) {
  sites.delete(id);
}
