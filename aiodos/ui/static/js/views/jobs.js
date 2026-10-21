// Tâches : la file d'attente (travaux découpés en unités), priorités, commandes.
import { get, post, patch } from '../api.js';
import { $, h, esc, duration, ago } from '../dom.js';
import { t, tt } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { toast, errorToast, confirmDanger } from '../ui.js';

const ACTIVE = ['queued', 'running', 'paused'];
const PRIORITIES = [1, 2, 3];                      // libellés : jobs.priority_1…3
const STATUS = {
  running: 'run', queued: 'idle', paused: 'warn', done: 'ok', failed: 'err', cancelled: 'idle',
};
const KIND_ICONS = { index_paths: 'folder', index_urls: 'globe', index_conversation: 'memory', reindex: 'retry' };

let root;
const cards = new Map();             // job id → { el, shape }
const expanded = new Set();
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
        <button class="btn" id="jobs-clear">${icon('trash')}${esc(t('jobs.clear_finished'))}</button>
      </div>
      <div class="stats" id="jobs-stats"></div>
      <div class="slots" id="jobs-slots"></div>
      <div class="jobs" id="jobs-list"></div>
    </div></div>`);
  container.append(root);
  $('#jobs-clear', root).onclick = clearFinished;
  on('jobs:changed', () => { if (state.view === 'jobs') render(); });
  on('executor:changed', () => { if (state.view === 'jobs') renderSlots(); });
  on('view:shown', view => { if (view === 'jobs') render(); });
  setInterval(() => { if (state.view === 'jobs') render(); }, 2000);   // durées vivantes
}

export async function loadJobs() {
  const data = await get('/jobs');
  state.jobs = new Map(data.jobs.map(j => [j.id, j]));
  state.executor = data.executor;
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
  const list = $('#jobs-list', root);
  const jobs = sorted();
  for (const [id, c] of cards) {
    if (!state.jobs.has(id)) { c.el.remove(); cards.delete(id); }
  }
  list.querySelector(':scope > .empty')?.remove();
  if (!jobs.length) {
    list.append(h(`<div class="card empty">${icon('jobs')}<div>${esc(t('jobs.empty'))}<br>
      ${esc(t('jobs.empty_hint'))}</div></div>`));
    return;
  }
  // Mise à jour en place : une carte n'est reconstruite que si sa structure
  // change (statut, priorité…). Sinon un clic tombant pendant une mise à jour
  // serait perdu, et les survols clignoteraient.
  jobs.forEach((j, index) => {
    let c = cards.get(j.id);
    const s = shape(j);
    if (!c || c.shape !== s) {
      const el = card(j);
      if (c) c.el.replaceWith(el);
      c = { el, shape: s };
      cards.set(j.id, c);
    } else {
      update(c.el, j);
    }
    if (list.children[index] !== c.el) list.insertBefore(c.el, list.children[index] || null);
  });
}

function hasDetails(j) {
  return Object.keys(j.result?.skipped_types || {}).length || j.failed || j.result?.message || j.error;
}

function shape(j) {
  return [j.status, j.priority, !!j.failed, !!hasDetails(j)].join('|');
}

function widths(j) {
  const pct = n => (j.total ? (100 * n / j.total).toFixed(2) : 0);
  return [pct(j.done), pct(j.skipped), pct(j.failed)];
}

function update(el, j) {
  el.querySelectorAll('.bar i').forEach((bar, i) => { bar.style.width = `${widths(j)[i]}%`; });
  el.querySelector('.foot').innerHTML = footText(j, j.done + j.skipped + j.failed);
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

function peerName(id) {
  return state.network?.peers?.find(p => p.node_id === id)?.name || t('jobs.another_machine');
}

// Unités de ce travail calculées en ce moment sur d'autres machines
function remoteText(j) {
  const where = Object.entries(j.remote || {});
  if (!where.length) return '';
  return `<span class="remote-where">${icon('network')}${where.map(([id, n]) =>
    esc(t('jobs.remote_on', { n, name: peerName(id) }))).join(', ')}</span>`;
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

function card(j) {
  const active = ACTIVE.includes(j.status);
  const processed = j.done + j.skipped + j.failed;
  const [done, skipped, failed] = widths(j);
  let cls = STATUS[j.status] || 'idle';
  let label = STATUS[j.status] ? t(`jobs.status_${j.status}`) : j.status;
  if (j.status === 'done' && j.failed) [cls, label] = ['warn', t('jobs.status_done_errors')];
  const title = jobTitle(j);
  const el = h(`
    <div class="job">
      <div class="top">
        ${icon(KIND_ICONS[j.kind] || 'layers')}
        <span class="title" title="${esc(title)}">${esc(title)}</span>
        <span class="pill ${cls}">${esc(label)}</span>
        ${active ? `<select class="select" title="${esc(t('jobs.priority'))}">${PRIORITIES.map(v =>
          `<option value="${v}" ${v === j.priority ? 'selected' : ''}>${esc(t(`jobs.priority_${v}`))}</option>`).join('')}</select>` : ''}
        <span class="actions"></span>
      </div>
      <div class="bar">
        <i class="fill" style="width:${done}%"></i><i class="skip" style="width:${skipped}%"></i><i class="fail" style="width:${failed}%"></i>
      </div>
      <div class="foot">${footText(j, processed)}</div>
    </div>`);
  const actions = el.querySelector('.actions');
  const button = (ico, key, fn, extra = '') => {
    const b = h(`<button class="icon-btn ${extra}" title="${esc(t(key))}">${icon(ico)}</button>`);
    b.onclick = fn;
    actions.append(b);
  };
  // Images ou vidéos demandées dans une conversation : y revenir
  if (j.params?.conversation_id) {
    button('chat', 'jobs.open_conversation', () => {
      emit('view:show', 'chat');
      emit('chat:open', j.params.conversation_id);
    });
  }
  if (j.status === 'paused') button('play', 'jobs.resume', () => act(j, 'resume'));
  else if (active) button('pause', 'jobs.pause', () => act(j, 'pause'));
  if (active) button('x', 'jobs.cancel', () => cancel(j), 'danger');
  if (!active && j.failed) button('retry', 'jobs.retry_failed', () => act(j, 'retry'));
  if (!active) button('trash', 'jobs.remove', () => act(j, 'delete'), 'danger');
  el.querySelector('select')?.addEventListener('change', async e => {
    try { await patch(`/jobs/${j.id}`, { priority: Number(e.target.value) }); } catch (err) { errorToast(err); }
  });
  const details = detailsBlock(j);
  if (details) el.append(details);
  return el;
}

function footText(j, processed) {
  const parts = [esc(t('jobs.units', { done: processed, n: j.total }))];
  if (j.skipped) parts.push(esc(t('jobs.skipped', { n: j.skipped })));
  if (j.failed) parts.push(`<span style="color:var(--err)">${esc(t('jobs.failed', { n: j.failed }))}</span>`);
  if (j.status === 'running' && j.started) {
    const elapsed = Date.now() / 1000 - j.started;
    parts.push(esc(t('jobs.since', { time: duration(elapsed) })));
    if (processed > 0 && processed < j.total) {
      parts.push(esc(t('jobs.remaining', { time: duration(elapsed / processed * (j.total - processed)) })));
    }
    const remote = remoteText(j);
    if (remote) parts.push(remote);
  } else if (j.finished && j.started) {
    parts.push(esc(t('jobs.took', { time: duration(j.finished - j.started) })));
  }
  if (j.result?.indexed !== undefined && !ACTIVE.includes(j.status)) {
    parts.push(esc(t('jobs.indexed', { n: j.result.indexed, chunks: j.result.chunks || 0 })));
  }
  if (!ACTIVE.includes(j.status)) parts.push(`<span class="faint">${esc(ago(j.finished || j.created))}</span>`);
  return parts.join(' · ');
}

function detailsBlock(job) {
  if (!hasDetails(job)) return null;
  const d = h(`<details ${expanded.has(job.id) ? 'open' : ''}><summary>${esc(t('jobs.details'))}</summary><div class="body"></div></details>`);
  const body = d.querySelector('.body');
  const fill = async () => {
    const j = state.jobs.get(job.id) || job;          // données à jour, pas celles de la création
    const skippedTypes = Object.entries(j.result?.skipped_types || {});
    let html = '';
    if (j.error) html += `<p>${esc(j.error)}</p>`;
    if (j.result?.message) html += `<p>${esc(tt(j.result.message))}</p>`;
    if (skippedTypes.length) {
      html += `<p>${esc(t('jobs.skipped_types', {
        types: skippedTypes.map(([ext, n]) => `${ext} ×${n}`).join(', ') }))}</p>`;
    }
    body.innerHTML = html;
    if (j.failed) {
      let cached = failuresCache.get(j.id);
      if (!cached || cached.count !== j.failed) {
        try {
          cached = { count: j.failed, list: await get(`/jobs/${j.id}/failures`) };
          failuresCache.set(j.id, cached);
        } catch { return; }
      }
      cached.list.forEach(f => body.append(h(`<div class="failure">${esc(f.source || '?')} — ${esc(f.error || '')}</div>`)));
    }
  };
  d.addEventListener('toggle', () => {
    if (d.open) { expanded.add(job.id); fill(); } else expanded.delete(job.id);
  });
  if (d.open) fill();
  return d;
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
