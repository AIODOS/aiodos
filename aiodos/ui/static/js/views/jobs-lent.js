// Tâches, « Pour d'autres » : ce que cette machine calcule pour d'autres (les inconnus du réseau
// public, mes amis, mes autres machines). En tête, le travail donné (aujourd'hui et les 7
// derniers jours, par cercle) ; puis ce qui se calcule ici pour d'autres en ce moment ; puis
// l'historique, groupé par jour, les plus récents d'abord, par pages (GET /api/lent,
// network/lent_history.py). Jamais de contenu : le genre, le modèle, la taille, la durée, pour
// qui, l'issue et sa raison courte. Suit l'événement « lent » du nœud (un calcul pour d'autres
// commence ou finit, app.js), et dit à jobs.js combien sont en cours (« lent:live »).
import { get } from '../api.js';
import { esc, duration, finite, debounce } from '../dom.js';
import { t, has, locale } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { errorToast } from '../ui.js';

const PAGE = 50;                     // lignes d'une page (lent_history.PAGE_SIZE)…
const MAX_PAGE = 200;                // … et au plus, en relisant celles déjà montrées
const KIND_ICONS = { chat: 'chat', reading: 'eye', image: 'image', video: 'video', index: 'file',
  page: 'globe', engine: 'layers', unit: 'jobs' };
const OUTCOMES = { done: 'ok', failed: 'err', stopped: 'warn', refused: 'idle' };   // → pastille
const CIRCLES = ['public', 'friend', 'mine'];

let box = null;
let data = { items: [], more: false, totals: null, live: [] };
let loaded = false;
let failed = false;
let loading = null;                  // la lecture en cours (une à la fois)
let again = false;                   // un événement pendant la lecture : on relit ensuite
const shown = {};                    // partie -> dernier contenu posé : rien n'est refait pour rien

/** La partie « Pour d'autres » dans `container` (caché tant que l'onglet n'est pas ouvert). */
export function mountLent(container) {
  box = container;
  box.innerHTML = `
    <section class="card lent-totals">
      <h2>${icon('network')}${esc(t('jobs_lent.totals_title'))}</h2>
      <p class="hint">${esc(t('jobs_lent.totals_hint'))}</p>
      <div data-part="totals"></div>
    </section>
    <section class="lent-block">
      <h3>${icon('bolt')}${esc(t('jobs_lent.live_title'))}</h3>
      <div data-part="live"></div>
    </section>
    <section class="lent-block">
      <h3>${icon('jobs')}${esc(t('jobs_lent.history_title'))}</h3>
      <div data-part="history"></div>
      <button type="button" class="btn sm lent-more" hidden>${esc(t('jobs_lent.more'))}</button>
    </section>`;
  box.querySelector('.lent-more').onclick = e => more(e.currentTarget);
  on('lent:changed', debounce(() => { if (state.view === 'jobs') load(); }, 400));
  setInterval(() => { if (visible() && data.live.length) renderLive(); }, 2000);   // durées vivantes
}

const visible = () => state.view === 'jobs' && box && !box.hidden;

/** Combien de calculs pour d'autres en ce moment (l'onglet le dit à côté de son nom). */
export const liveCount = () => data.live.length;

/** L'onglet ouvert, ou la vue Tâches montrée : relue. */
export function showLent() {
  load();
}

async function load() {
  if (loading) {
    again = true;
    return;
  }
  // Les pages déjà montrées restent montrées (« Voir plus »), jusqu'à MAX_PAGE lignes
  const limit = Math.min(MAX_PAGE, Math.max(PAGE, data.items.length));
  loading = get(`/lent?limit=${limit}`);
  try {
    const found = await loading;
    data = { items: Array.isArray(found.items) ? found.items : [], more: !!found.more,
      totals: found.totals || null, live: Array.isArray(found.live) ? found.live : [] };
    loaded = true;
    failed = false;
  } catch {
    failed = true;
  } finally {
    loading = null;
  }
  render();
  emit('lent:live');
  if (again) {
    again = false;
    load();
  }
}

async function more(button) {
  const last = data.items[data.items.length - 1];
  if (!last) return;
  button.disabled = true;
  try {
    const found = await get(`/lent?before=${encodeURIComponent(last.id)}&limit=${PAGE}`);
    const known = new Set(data.items.map(r => r.id));
    const items = Array.isArray(found.items) ? found.items.filter(r => !known.has(r.id)) : [];
    data.items.push(...items);
    data.more = !!found.more && items.length > 0;
  } catch (err) {
    errorToast(err);
  } finally {
    button.disabled = false;
  }
  renderHistory();
}

function render() {
  if (!box) return;
  renderTotals();
  renderLive();
  renderHistory();
}

function put(part, html) {
  if (shown[part] === html) return;
  shown[part] = html;
  box.querySelector(`[data-part="${part}"]`).innerHTML = html;
}

// ------------------------------------------------------------ le travail donné
function cell(found) {
  const n = finite(found?.n) ?? 0;
  const seconds = finite(found?.seconds) ?? 0;
  if (!n && !seconds) return '<td class="faint">—</td>';
  return `<td><div>${esc(t('jobs_lent.count', { n }))}</div><div class="faint">${esc(duration(seconds))}</div></td>`;
}

function renderTotals() {
  const totals = data.totals;
  if (!totals) {
    put('totals', failed ? `<p class="lent-none">${esc(t('jobs_lent.history_failed'))}</p>` : '');
    return;
  }
  const rows = [...CIRCLES, 'all'].map(c => `<tr${c === 'all' ? ' class="lent-sum"' : ''}>
    <th scope="row">${esc(t(`jobs_lent.row_${c}`))}</th>${cell(totals.today?.[c])}${cell(totals.week?.[c])}</tr>`);
  put('totals', `<table class="lent-table"><thead><tr><th></th>
    <th scope="col">${esc(t('jobs_lent.col_today'))}</th><th scope="col">${esc(t('jobs_lent.col_week'))}</th></tr></thead>
    <tbody>${rows.join('')}</tbody></table>`);
}

// ------------------------------------------------------------ une ligne
const kindOf = kind => (typeof kind === 'string' && has(`jobs_lent.kind_${kind}`) ? kind : 'unit');

// Pour qui : jamais personne pour le réseau public ; le nom d'un ami ou d'une de mes machines
function who(r) {
  const name = typeof r.peer === 'string' ? r.peer : '';
  if (r.circle === 'public') return t('jobs_lent.for_public');
  if (r.circle === 'mine') return name ? t('jobs_lent.for_mine', { name }) : t('jobs_lent.for_mine_unknown');
  return name ? t('jobs_lent.for_friend', { name }) : t('jobs_lent.for_friend_unknown');
}

// Sa taille : largeur × hauteur, images d'une vidéo, jetons, passages
function sizes(r) {
  const out = [];
  const width = finite(r.width);
  const height = finite(r.height);
  if (width && height) out.push(t('jobs_lent.side', { width, height }));
  const frames = finite(r.frames);
  if (frames) out.push(t('jobs_lent.frames', { n: frames }));
  const tokens = finite(r.tokens);
  if (tokens) out.push(t(r.kind === 'reading' ? 'jobs_lent.tokens_read' : 'jobs_lent.tokens', { n: tokens }));
  const passages = finite(r.passages);
  if (passages) out.push(t('jobs_lent.passages', { n: passages }));
  return out;
}

// « Image · z-image-turbo · 1024 × 1024 · 26 s · un inconnu du réseau public » (la durée :
// pour une ligne de l'historique, sauf un refus ; plusieurs fois, la durée en tout)
function describe(r, withTime) {
  const parts = [t(`jobs_lent.kind_${kindOf(r.kind)}`)];
  if (typeof r.model === 'string' && r.model) parts.push(r.model);
  parts.push(...sizes(r));
  const seconds = finite(r.seconds);
  if (withTime && seconds !== null && r.outcome !== 'refused') {
    parts.push((finite(r.times) || 1) > 1 ? t('jobs_lent.total_time', { time: duration(seconds) }) : duration(seconds));
  }
  parts.push(who(r));
  return parts.map(esc).join(' · ');
}

function row(r) {
  const outcome = OUTCOMES[r.outcome] ? r.outcome : 'failed';
  const reason = typeof r.reason === 'string' && has(`jobs_lent.reason_${r.reason}`)
    ? t(`jobs_lent.reason_${r.reason}`) : '';
  const times = finite(r.times) || 1;
  const at = new Date((finite(r.at) ?? 0) * 1000);
  const clock = at.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });
  return `<div class="lent-row">${icon(KIND_ICONS[kindOf(r.kind)])}
    <div class="lent-text"><div>${describe(r, true)}</div>${reason ? `<div class="lent-reason">${esc(reason)}</div>` : ''}</div>
    <div class="lent-end"><span class="pill ${OUTCOMES[outcome]}">${esc(t(`jobs_lent.outcome_${outcome}`))}${
      times > 1 ? ` ${esc(t('jobs_lent.times', { n: times }))}` : ''}</span><time datetime="${esc(at.toISOString())}">${esc(clock)}</time></div>
  </div>`;
}

// ------------------------------------------------------------ en ce moment
function renderLive() {
  if (!loaded) {
    put('live', '');
    return;
  }
  const now = Date.now() / 1000;
  put('live', data.live.length ? data.live.map(l => `<div class="lent-row">${icon(KIND_ICONS[kindOf(l.kind)])}
      <div class="lent-text"><div>${describe(l, false)}</div></div>
      <div class="lent-end"><span class="pill run">${esc(t('jobs_lent.live_since', {
        time: duration(now - (finite(l.since) ?? now)) }))}</span></div>
    </div>`).join('') : `<p class="lent-none">${esc(t('jobs_lent.live_none'))}</p>`);
}

// ------------------------------------------------------------ l'historique, par jour
const dayKey = date => date.toDateString();

function dayTitle(date) {
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (dayKey(date) === dayKey(today)) return t('jobs_lent.today');
  if (dayKey(date) === dayKey(yesterday)) return t('jobs_lent.yesterday');
  const options = { weekday: 'long', day: 'numeric', month: 'long' };
  if (date.getFullYear() !== today.getFullYear()) options.year = 'numeric';
  return date.toLocaleDateString(locale(), options);
}

function renderHistory() {
  const button = box.querySelector('.lent-more');
  button.hidden = !loaded || !data.more;
  if (!loaded) {
    put('history', failed ? `<p class="lent-none">${esc(t('jobs_lent.history_failed'))}</p>` : '');
    return;
  }
  if (!data.items.length) {
    put('history', `<div class="lent-empty">${esc(t('jobs_lent.history_empty'))}</div>`);
    return;
  }
  const days = [];
  for (const r of data.items) {
    const date = new Date((finite(r.at) ?? 0) * 1000);
    const last = days[days.length - 1];
    if (last && last.key === dayKey(date)) last.rows.push(r);
    else days.push({ key: dayKey(date), date, rows: [r] });
  }
  put('history', days.map(d => `<div class="lent-day"><h4>${esc(dayTitle(d.date))}</h4>${
    d.rows.map(row).join('')}</div>`).join(''));
}
