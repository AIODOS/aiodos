// Tâches : la file d'attente (travaux découpés en unités), priorités, commandes. Une ligne
// repliable par travail (titre, état, progression, pause ou reprise, annulation), le détail à
// l'ouverture, avec le chantier en direct (chantier.js) ; en bas, les machines au travail
// (jobs-workers.js).
import { get, post, patch } from '../api.js';
import { $, h, esc, duration, ago } from '../dom.js';
import { t, tt } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { toast, errorToast, confirmDanger, menu } from '../ui.js';
import { showChantier, forgetChantier } from './chantier.js';
import { mountWorkers, renderWorkers, peerName } from './jobs-workers.js';

const ACTIVE = ['queued', 'running', 'paused'];
const PRIORITIES = [1, 2, 3];                      // libellés : jobs.priority_1…3
const STATUS = {
  running: 'run', queued: 'idle', paused: 'warn', done: 'ok', failed: 'err', cancelled: 'idle',
};
const KIND_ICONS = { index_paths: 'folder', index_urls: 'globe', index_conversation: 'memory', reindex: 'retry',
  engine: 'image' };

let root;
const rows = new Map();              // job id → { el, shape, facts, notes }
const expanded = new Set();          // lignes ouvertes (fermées par défaut)
const failuresCache = new Map();     // job id → { count, list }

/** Le titre d'un travail, dans la langue de l'interface s'il est venu traduisible
 *  (title_t, cf. jobs/queue.py), sinon tel qu'il a été écrit à sa création. */
export const jobTitle = j => tt(j?.title_t, j?.title || '');

export function mountJobs(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head">
        <div><h1>${esc(t('jobs.title'))}</h1>
          <div class="sub">${esc(t('jobs.intro'))}</div></div>
        <div class="grow"></div>
        <button class="btn" id="jobs-pause-all">${icon('pause')}${esc(t('jobs.pause_all'))}</button>
        <button class="btn" id="jobs-clear">${icon('trash')}${esc(t('jobs.clear_finished'))}</button>
      </div>
      <div class="stats" id="jobs-stats"></div>
      <div class="slots" id="jobs-slots"></div>
      <div class="jobs" id="jobs-list"></div>
      <div class="jobs-workers" id="jobs-workers"></div>
    </div></div>`);
  container.append(root);
  mountWorkers($('#jobs-workers', root));
  $('#jobs-clear', root).onclick = clearFinished;
  $('#jobs-pause-all', root).onclick = e => pauseAll(e.currentTarget);
  on('jobs:changed', () => { if (state.view === 'jobs') render(); });
  on('executor:changed', () => { if (state.view === 'jobs') { renderSlots(); renderWorkers(); } });
  on('network:loaded', () => { if (state.view === 'jobs') renderWorkers(); });
  on('view:shown', view => { if (view === 'jobs') render(); });
  setInterval(() => { if (state.view === 'jobs') render(); }, 2000);   // durées vivantes
}

export async function loadJobs() {
  const data = await get('/jobs');
  state.jobs = new Map(data.jobs.map(j => [j.id, j]));
  state.executor = data.executor;
  state.queuePause = data.queue_pause || '';
  emit('jobs:changed');
  emit('executor:changed');
}

function sorted() {
  return [...state.jobs.values()].sort((a, b) => {
    const ra = ACTIVE.includes(a.status) ? 0 : 1;
    const rb = ACTIVE.includes(b.status) ? 0 : 1;
    if (ra !== rb) return ra - rb;
    if (ra === 0) return (a.status === 'running' ? 0 : 1) - (b.status === 'running' ? 0 : 1)
      || a.priority - b.priority || a.created - b.created;
    return (b.finished || b.created) - (a.finished || a.created);
  });
}

function render() {
  // Ne pas toucher la liste pendant qu'une liste de priorité est ouverte
  const focused = document.activeElement;
  if (focused?.tagName === 'SELECT' && root.contains(focused)) return;
  renderStats();
  renderSlots();
  renderWorkers();
  const list = $('#jobs-list', root);
  const jobs = sorted();
  for (const [id, r] of rows) {
    if (!state.jobs.has(id)) {
      r.el.remove(); rows.delete(id); expanded.delete(id); failuresCache.delete(id); forgetChantier(id);
    }
  }
  list.querySelector(':scope > .empty')?.remove();
  if (!jobs.length) {
    list.append(h(`<div class="card empty">${icon('jobs')}<div>${esc(t('jobs.empty'))}<br>
      ${esc(t('jobs.empty_hint'))}</div></div>`));
    return;
  }
  // Mise à jour en place : une ligne n'est reconstruite que si sa structure
  // change (statut, priorité…). Sinon un clic tombant pendant une mise à jour
  // serait perdu, et les survols clignoteraient.
  jobs.forEach((j, index) => {
    let r = rows.get(j.id);
    const s = shape(j);
    if (!r || r.shape !== s) {
      const el = row(j);
      if (r) r.el.replaceWith(el);
      r = { el, shape: s };
      rows.set(j.id, r);
    }
    update(r, j);
    if (list.children[index] !== r.el) list.insertBefore(r.el, list.children[index] || null);
  });
}

function shape(j) {
  return [j.status, j.priority, !!j.failed, !!j.params?.conversation_id, !!j.blocked].join('|');
}

function processedOf(j) {
  return j.done + j.skipped + j.failed;
}

function widths(j) {
  const pct = n => (j.total ? (100 * n / j.total).toFixed(2) : 0);
  return [pct(j.done), pct(j.skipped), pct(j.failed)];
}

// Ce qui bouge sans changer la structure : la barre, le pourcentage, le détail
function update(r, j) {
  const el = r.el;
  el.querySelectorAll('.bar i').forEach((bar, i) => { bar.style.width = `${widths(j)[i]}%`; });
  el.querySelector('.job-pct').textContent = j.total
    ? t('jobs.percent', { n: Math.floor(100 * processedOf(j) / j.total) }) : '';
  // Chaque partie n'est réécrite que si elle a changé : un texte sélectionné reste sélectionné
  for (const [part, html] of [['facts', facts(j)], ['notes', notes(j)]]) {
    if (r[part] === html) continue;
    r[part] = html;
    el.querySelector(`.job-${part}`).innerHTML = html;
  }
  if (el.open) {
    showFailures(el, j);
    showChantier(el.querySelector('.job-chantier'), j);
  }
}

function renderStats() {
  const jobs = [...state.jobs.values()];
  const count = s => jobs.filter(j => j.status === s).length;
  const units = jobs.filter(j => ACTIVE.includes(j.status))
    .reduce((n, j) => n + j.total - j.done - j.skipped - j.failed, 0);
  $('#jobs-stats', root).innerHTML = [
    [count('running'), 'jobs.stat_running'], [count('queued'), 'jobs.stat_queued'],
    [count('paused'), 'jobs.stat_paused'], [units, 'jobs.stat_units_left'], [count('done'), 'jobs.stat_done'],
  ].map(([n, key]) => `<div class="stat"><b>${n}</b><span>${esc(t(key, { n }))}</span></div>`).join('');
}

// Unités d'autres machines calculées ici, regroupées par machine
function lentPills(ex) {
  const byPeer = new Map();
  for (const w of ex.remote || []) byPeer.set(w.peer_name, (byPeer.get(w.peer_name) || 0) + 1);
  return [...byPeer].map(([name, n]) =>
    `<span class="pill run">${icon('network')}${esc(t('jobs.computing_for', { name, n }))}</span>`).join('');
}

// Sur batterie : moins d'unités à la fois (les points ci-contre), ou aucune (jobs/pacing.py)
function batteryPill(p) {
  if (!p?.on_battery || p.mode === 'normal') return '';
  const text = t(p.mode === 'pause' ? 'jobs.battery_waiting' : 'jobs.battery_eco');
  return `<span class="pill warn" title="${esc(p.detail)}">${icon('bolt')}${esc(text)}</span>`;
}

function renderSlots() {
  const ex = state.executor;
  const box = $('#jobs-slots', root);
  if (!ex || !box) return;
  box.innerHTML = `<span>${esc(t('jobs.resources'))}</span>` + Object.entries(ex.slots).map(([name, n]) => {
    const busy = ex.busy[name] || 0;
    const dots = Array.from({ length: n }, (_, i) => `<i class="${i < busy ? 'busy' : ''}"></i>`).join('');
    const label = ['gpu', 'embed', 'cpu', 'net'].includes(name) ? t(`jobs.resource_${name}`) : name;
    return `<span class="slot">${dots} ${esc(label)} ${busy}/${n}</span>`;
  }).join('') + (ex.interactive
    ? `<span class="pill warn">${icon('chat')}${esc(t('jobs.chat_running'))}</span>` : '')
    + batteryPill(ex.pacing)
    + lentPills(ex)
    + (Object.keys(ex.waiting || {}).length
      ? `<span class="pill warn">${icon('warn')}${esc(t('jobs.engine_unreachable'))}</span>` : '');
}

// Une ligne par travail : titre, état, progression et commandes ; le reste à l'ouverture
function row(j) {
  const active = ACTIVE.includes(j.status);
  const [done, skipped, failed] = widths(j);
  let cls = STATUS[j.status] || 'idle';
  let label = STATUS[j.status] ? t(`jobs.status_${j.status}`) : j.status;
  if (j.status === 'done' && j.failed) [cls, label] = ['warn', t('jobs.status_done_errors')];
  // Le modèle d'embeddings manque ici (knowledge/embed_model.py) : dit sur la ligne même
  if (j.blocked && active) [cls, label] = ['warn', t('jobs.status_blocked')];
  const title = jobTitle(j);
  const el = h(`
    <details class="fold job"${expanded.has(j.id) ? ' open' : ''}>
      <summary>${icon(KIND_ICONS[j.kind] || 'layers')}
        <span class="fold-title" title="${esc(title)}">${j.app_name ? `<b>${esc(j.app_name)}</b> · ` : ''}${esc(title)}</span>
        <span class="job-meta">
          <span class="pill ${cls}">${esc(label)}</span>
          <span class="bar"><i class="fill" style="width:${done}%"></i><i class="skip" style="width:${skipped}%"></i><i class="fail" style="width:${failed}%"></i></span>
          <span class="job-pct"></span>
          <span class="actions"></span>
        </span>
        <span class="fold-chev">${icon('chev-right')}</span>
        ${j.blocked && active ? `<span class="job-blocked">${icon('warn')}<span>${esc(tt(j.blocked.message))}</span>
          <button class="btn sm ghost" data-fix>${esc(t('embed_model.fix'))}</button></span>` : ''}</summary>
      <div class="fold-body">
        <dl class="facts job-facts"></dl>
        <div class="job-notes"></div>
        <div class="job-chantier"></div>
        <div class="job-failures"></div>
      </div>
    </details>`);
  // Sur la ligne : les boutons ne l'ouvrent ni ne la ferment
  const actions = el.querySelector('.actions');
  const button = (ico, key, fn, extra = '') => {
    const b = h(`<button class="icon-btn ${extra}" title="${esc(t(key))}">${icon(ico)}</button>`);
    b.onclick = e => { e.preventDefault(); fn(e); };
    actions.append(b);
  };
  if (j.status === 'paused') button('play', 'jobs.resume', () => act(j, 'resume'));
  else if (active) button('pause', 'jobs.pause', e => pauseMenu(e.currentTarget, j));
  if (active) button('x', 'jobs.cancel', () => cancel(j), 'danger');
  if (!active && j.failed) button('retry', 'jobs.retry_failed', () => act(j, 'retry'));
  if (!active) button('trash', 'jobs.remove', () => act(j, 'delete'), 'danger');
  // Dans le détail : la priorité, et la conversation d'où viennent des images ou des vidéos
  const controls = [];
  if (active) {
    controls.push(`<label>${esc(t('jobs.priority'))} <select class="select">${PRIORITIES.map(v =>
      `<option value="${v}" ${v === j.priority ? 'selected' : ''}>${esc(t(`jobs.priority_${v}`))}</option>`).join('')}</select></label>`);
  }
  if (j.params?.conversation_id) {
    controls.push(`<button class="btn sm" data-open-chat>${icon('chat')}${esc(t('jobs.open_conversation'))}</button>`);
  }
  if (controls.length) el.querySelector('.fold-body').append(h(`<div class="job-controls">${controls.join('')}</div>`));
  el.querySelector('select')?.addEventListener('change', async e => {
    try { await patch(`/jobs/${j.id}`, { priority: Number(e.target.value) }); } catch (err) { errorToast(err); }
  });
  el.querySelector('[data-open-chat]')?.addEventListener('click', () => {
    emit('view:show', 'chat');
    emit('chat:open', j.params.conversation_id);
  });
  // « Régler » : la bibliothèque, où le bandeau propose les modèles présents et le
  // téléchargement (embed-model.js)
  el.querySelector('[data-fix]')?.addEventListener('click', e => {
    e.preventDefault();
    emit('view:show', 'library');
  });
  el.addEventListener('toggle', () => {
    if (!el.open) { expanded.delete(j.id); return; }
    expanded.add(j.id);
    const now = state.jobs.get(j.id) || j;             // données à jour, pas celles de la création
    showFailures(el, now);
    showChantier(el.querySelector('.job-chantier'), now);
  });
  return el;
}

// Le détail : unités, durées, machines qui calculent, résultat (recalculé à chaque passage)
function facts(j) {
  const processed = processedOf(j);
  const lines = [];
  const units = [esc(t('jobs.units', { done: processed, n: j.total }))];
  if (j.skipped) units.push(esc(t('jobs.skipped', { n: j.skipped })));
  if (j.failed) units.push(`<span class="job-fail">${esc(t('jobs.failed', { n: j.failed }))}</span>`);
  lines.push(['jobs.detail_units', units]);
  const time = [];
  if (j.status === 'running' && j.started) {
    const elapsed = Date.now() / 1000 - j.started;
    time.push(esc(t('jobs.since', { time: duration(elapsed) })));
    if (processed > 0 && processed < j.total) {
      time.push(esc(t('jobs.remaining', { time: duration(elapsed / processed * (j.total - processed)) })));
    }
  } else if (j.finished && j.started) {
    time.push(esc(t('jobs.took', { time: duration(j.finished - j.started) })));
  }
  if (!ACTIVE.includes(j.status)) time.push(`<span class="faint">${esc(ago(j.finished || j.created))}</span>`);
  if (time.length) lines.push(['jobs.detail_time', time]);
  // Unités en calcul ici et sur d'autres machines
  const where = Object.entries(j.remote || {}).map(([id, n]) => esc(t('jobs.remote_on', { n, name: peerName(id) })));
  if (j.local) where.unshift(esc(t('jobs.local_here', { n: j.local })));
  if (j.copies) where.push(esc(t('chantier.facts_copies', { n: j.copies })));   // fin de partie
  if (where.length) lines.push(['jobs.detail_machines', [where.join(', ')]]);
  if (j.result?.indexed !== undefined && !ACTIVE.includes(j.status)) {
    lines.push(['jobs.detail_result', [esc(t('jobs.indexed', { n: j.result.indexed, chunks: j.result.chunks || 0 }))]]);
  }
  return lines.map(([key, parts]) => `<dt>${esc(t(key))}</dt><dd>${parts.join(' · ')}</dd>`).join('');
}

// Ce que le travail a laissé : son erreur, son message, les types de fichiers ignorés
function notes(j) {
  const skippedTypes = Object.entries(j.result?.skipped_types || {});
  let html = '';
  if (j.error) html += `<p>${esc(j.error)}</p>`;
  if (j.result?.message) html += `<p>${esc(tt(j.result.message))}</p>`;
  if (skippedTypes.length) {
    html += `<p>${esc(t('jobs.skipped_types', {
      types: skippedTypes.map(([ext, n]) => `${ext} ×${n}`).join(', ') }))}</p>`;
  }
  return html;
}

// Les unités en échec et leur erreur, demandées au nœud à l'ouverture (et quand il y en a d'autres)
async function showFailures(el, j) {
  const box = el.querySelector('.job-failures');
  const count = String(j.failed || 0);
  if (box.dataset.count === count || box.dataset.loading === count) return;   // déjà là, ou demandées
  let cached = failuresCache.get(j.id);
  if (j.failed && cached?.count !== j.failed) {
    box.dataset.loading = count;
    try {
      cached = { count: j.failed, list: await get(`/jobs/${j.id}/failures`) };
      failuresCache.set(j.id, cached);
    } catch { return; } finally { delete box.dataset.loading; }
  }
  box.innerHTML = j.failed ? cached.list.map(f =>
    `<div class="failure">${esc(f.source || '?')} — ${esc(f.error || '')}</div>`).join('') : '';
  box.dataset.count = count;
}

async function act(j, action) {
  try { await post(`/jobs/${j.id}/${action}`); } catch (e) { errorToast(e); }
}

async function cancel(j) {
  const ok = await confirmDanger(t('jobs.cancel_title'),
    t('jobs.cancel_text', { title: jobTitle(j) }), t('jobs.cancel_title'));
  if (ok) act(j, 'cancel');
}

async function clearFinished() {
  try {
    const { removed } = await post('/jobs/clear_finished');
    toast(removed ? t('jobs.cleared', { n: removed }) : t('jobs.nothing_to_clear'), 'ok');
  } catch (e) { errorToast(e); }
}

// Pause « après l'unité en cours » (rien ne se perd) ou « tout de suite » (le calcul en cours
// s'arrête, ici et chez les autres machines ; l'unité est rendue sans compter d'essai).
// [mode, libellé pour un travail, libellé pour toute la file, icône]
const PAUSES = [
  ['after_current', 'jobs.pause_after', 'jobs.pause_all_after', 'pause'],
  ['now', 'jobs.pause_now', 'jobs.pause_all_now', 'stop'],
];

function pauseMenu(anchor, j) {
  menu(anchor, PAUSES.map(([when, label, , ico]) => ({
    label: t(label), icon: ico,
    action: async () => {
      try { await post(`/jobs/${j.id}/pause`, { when }); } catch (e) { errorToast(e); }
    },
  })));
}

async function pauseAll(anchor) {
  const paused = !!state.queuePause;
  if (paused) {
    try { await post('/jobs/resume_all'); toast(t('jobs.queue_resumed'), 'ok'); } catch (e) { errorToast(e); }
  } else {
    menu(anchor, PAUSES.map(([when, , label, ico]) => ({
      label: t(label), icon: ico,
      action: async () => {
        try { await post('/jobs/pause_all', { when }); toast(t('jobs.queue_paused'), 'ok'); } catch (e) { errorToast(e); }
      },
    })));
    return;
  }
  loadJobs();
}

on('jobs:changed', () => {
  const btn = root && $('#jobs-pause-all', root);
  if (btn) btn.innerHTML = `${icon(state.queuePause ? 'play' : 'pause')}${esc(t(state.queuePause ? 'jobs.resume_all' : 'jobs.pause_all'))}`;
});
