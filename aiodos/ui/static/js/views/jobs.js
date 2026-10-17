// Tâches : la file d'attente (travaux découpés en unités), priorités, commandes.
import { get, post, patch } from '../api.js';
import { $, h, esc, duration, ago } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { toast, errorToast, confirmDanger } from '../ui.js';

const ACTIVE = ['queued', 'running', 'paused'];
const PRIORITIES = [[1, 'Urgent'], [2, 'Normal'], [3, 'De fond']];
const RESOURCES = { gpu: 'GPU', embed: 'Embeddings', cpu: 'Processeur', net: 'Réseau' };
const STATUS = {
  running: ['run', 'En cours'], queued: ['idle', 'En attente'], paused: ['warn', 'En pause'],
  done: ['ok', 'Terminé'], failed: ['err', 'Échec'], cancelled: ['idle', 'Annulé'],
};
const KIND_ICONS = { index_paths: 'folder', index_urls: 'globe', index_conversation: 'memory', reindex: 'retry' };

let root;
const cards = new Map();             // job id → { el, shape }
const expanded = new Set();
const failuresCache = new Map();     // job id → { count, list }

export function mountJobs(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head">
        <div><h1>Tâches</h1>
          <div class="sub">Les gros travaux sont découpés en petites unités indépendantes, traitées en
          arrière-plan : ici, ou sur tes machines appairées qui prêtent leur puissance.</div></div>
        <div class="grow"></div>
        <button class="btn" id="jobs-clear">${icon('trash')}Nettoyer les terminées</button>
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
    list.append(h(`<div class="card empty">${icon('jobs')}<div>Aucune tâche.<br>
      Ajoute un dossier dans une bibliothèque pour voir la file au travail.</div></div>`));
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
    [count('running'), 'en cours'], [count('queued'), 'en attente'], [count('paused'), 'en pause'],
    [units, 'unités restantes'], [count('done'), 'terminées'],
  ].map(([n, label]) => `<div class="stat"><b>${n}</b><span>${label}</span></div>`).join('');
}

function peerName(id) {
  return state.network?.peers?.find(p => p.node_id === id)?.name || 'une autre machine';
}

// Unités de ce travail calculées en ce moment sur d'autres machines
function remoteText(j) {
  const where = Object.entries(j.remote || {});
  if (!where.length) return '';
  return `<span class="remote-where">${icon('network')}${where.map(([id, n]) =>
    `${n} sur « ${esc(peerName(id))} »`).join(', ')}</span>`;
}

// Unités d'autres machines calculées ici, regroupées par machine
function lentPills(ex) {
  const byPeer = new Map();
  for (const w of ex.remote || []) byPeer.set(w.peer_name, (byPeer.get(w.peer_name) || 0) + 1);
  return [...byPeer].map(([name, n]) =>
    `<span class="pill run">${icon('network')}Calcule pour « ${esc(name)} » : ${n} unité(s)</span>`).join('');
}

// Sur batterie : moins d'unités à la fois (les points ci-contre), ou aucune (jobs/pacing.py)
function batteryPill(p) {
  if (!p?.on_battery || p.mode === 'normal') return '';
  const text = p.mode === 'pause' ? 'Sur batterie : en attente du secteur'
    : 'Sur batterie : travail de fond ménagé';
  return `<span class="pill warn" title="${esc(p.detail)}">${icon('bolt')}${text}</span>`;
}

function renderSlots() {
  const ex = state.executor;
  const box = $('#jobs-slots', root);
  if (!ex || !box) return;
  box.innerHTML = '<span>Ressources de cette machine :</span>' + Object.entries(ex.slots).map(([name, n]) => {
    const busy = ex.busy[name] || 0;
    const dots = Array.from({ length: n }, (_, i) => `<i class="${i < busy ? 'busy' : ''}"></i>`).join('');
    return `<span class="slot">${dots} ${RESOURCES[name] || name} ${busy}/${n}</span>`;
  }).join('') + (ex.interactive
    ? `<span class="pill warn">${icon('chat')}Conversation en cours : le GPU lui est réservé</span>` : '')
    + batteryPill(ex.pacing)
    + lentPills(ex)
    + (Object.keys(ex.waiting || {}).length
      ? `<span class="pill warn">${icon('warn')}Ollama injoignable : nouvel essai automatique</span>` : '');
}

function card(j) {
  const active = ACTIVE.includes(j.status);
  const processed = j.done + j.skipped + j.failed;
  const [done, skipped, failed] = widths(j);
  let [cls, label] = STATUS[j.status] || ['idle', j.status];
  if (j.status === 'done' && j.failed) [cls, label] = ['warn', 'Terminé avec erreurs'];
  const el = h(`
    <div class="job">
      <div class="top">
        ${icon(KIND_ICONS[j.kind] || 'layers')}
        <span class="title" title="${esc(j.title)}">${esc(j.title)}</span>
        <span class="pill ${cls}">${label}</span>
        ${active ? `<select class="select" title="Priorité">${PRIORITIES.map(([v, l]) =>
          `<option value="${v}" ${v === j.priority ? 'selected' : ''}>${l}</option>`).join('')}</select>` : ''}
        <span class="actions"></span>
      </div>
      <div class="bar">
        <i class="fill" style="width:${done}%"></i><i class="skip" style="width:${skipped}%"></i><i class="fail" style="width:${failed}%"></i>
      </div>
      <div class="foot">${footText(j, processed)}</div>
    </div>`);
  const actions = el.querySelector('.actions');
  const button = (ico, title, fn, extra = '') => {
    const b = h(`<button class="icon-btn ${extra}" title="${title}">${icon(ico)}</button>`);
    b.onclick = fn;
    actions.append(b);
  };
  if (j.status === 'paused') button('play', 'Reprendre', () => act(j, 'resume'));
  else if (active) button('pause', 'Mettre en pause', () => act(j, 'pause'));
  if (active) button('x', 'Annuler', () => cancel(j), 'danger');
  if (!active && j.failed) button('retry', 'Relancer les unités en échec', () => act(j, 'retry'));
  if (!active) button('trash', 'Retirer de la liste', () => act(j, 'delete'), 'danger');
  el.querySelector('select')?.addEventListener('change', async e => {
    try { await patch(`/jobs/${j.id}`, { priority: Number(e.target.value) }); } catch (err) { errorToast(err); }
  });
  const details = detailsBlock(j);
  if (details) el.append(details);
  return el;
}

function footText(j, processed) {
  const parts = [`${processed}/${j.total} unité(s)`];
  if (j.skipped) parts.push(`${j.skipped} ignorée(s)`);
  if (j.failed) parts.push(`<span style="color:var(--err)">${j.failed} en échec</span>`);
  if (j.status === 'running' && j.started) {
    const elapsed = Date.now() / 1000 - j.started;
    parts.push(`depuis ${duration(elapsed)}`);
    if (processed > 0 && processed < j.total) {
      parts.push(`reste ~${duration(elapsed / processed * (j.total - processed))}`);
    }
    const remote = remoteText(j);
    if (remote) parts.push(remote);
  } else if (j.finished && j.started) {
    parts.push(`en ${duration(j.finished - j.started)}`);
  }
  if (j.result?.indexed !== undefined && !ACTIVE.includes(j.status)) {
    parts.push(`${j.result.indexed} indexé(s) · ${j.result.chunks || 0} passages`);
  }
  if (!ACTIVE.includes(j.status)) parts.push(`<span class="faint">${ago(j.finished || j.created)}</span>`);
  return parts.join(' · ');
}

function detailsBlock(job) {
  if (!hasDetails(job)) return null;
  const d = h(`<details ${expanded.has(job.id) ? 'open' : ''}><summary>Détails</summary><div class="body"></div></details>`);
  const body = d.querySelector('.body');
  const fill = async () => {
    const j = state.jobs.get(job.id) || job;          // données à jour, pas celles de la création
    const skippedTypes = Object.entries(j.result?.skipped_types || {});
    let html = '';
    if (j.error) html += `<p>${esc(j.error)}</p>`;
    if (j.result?.message) html += `<p>${esc(j.result.message)}</p>`;
    if (skippedTypes.length) {
      html += `<p>Ignorés (pas de texte exploitable, ou type désactivé dans les réglages) : ${
        skippedTypes.map(([ext, n]) => `${esc(ext)} ×${n}`).join(', ')}</p>`;
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
  const ok = await confirmDanger('Annuler la tâche',
    `« ${j.title} » : les unités pas encore traitées seront abandonnées. Ce qui est déjà fait est conservé.`, 'Annuler la tâche');
  if (ok) act(j, 'cancel');
}

async function clearFinished() {
  try {
    const { removed } = await post('/jobs/clear_finished');
    toast(removed ? `${removed} tâche(s) retirée(s).` : 'Rien à nettoyer.', 'ok');
  } catch (e) { errorToast(e); }
}
