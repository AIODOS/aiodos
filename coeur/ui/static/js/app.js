// Démarrage de l'interface : vues, navigation, événements du nœud.
import { get, listen } from './api.js';
import { $, $$, esc } from './dom.js';
import { hydrateIcons } from './icons.js';
import { state, emit, on, prefs } from './state.js';
import { toast } from './ui.js';
import { mountChat } from './views/chat.js';
import { mountLibrary } from './views/library.js';
import { mountJobs, loadJobs } from './views/jobs.js';
import { mountNode, loadNode } from './views/node.js';
import { mountSettings } from './views/settings.js';

const VIEWS = ['chat', 'library', 'jobs', 'node', 'settings'];
const ACTIVE = ['queued', 'running', 'paused'];

function show(view) {
  if (!VIEWS.includes(view)) view = 'chat';
  $$('.view').forEach(v => { v.hidden = v.id !== `view-${view}`; });
  $$('.rail-btn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  state.view = view;
  prefs.set('view', view);
  emit('view:shown', view);
}

async function loadSettings() {
  state.settings = await get('/settings');
  emit('settings:loaded');
}

async function loadModels() {
  try { state.models = await get('/models'); } catch { state.models = []; }
  emit('models:loaded');
}

function updateBadge() {
  const n = [...state.jobs.values()].filter(j => ACTIVE.includes(j.status)).length;
  const badge = $('#jobs-badge');
  badge.hidden = !n;
  badge.textContent = n;
}

function onJob(job) {
  const before = state.jobs.get(job.id)?.status;
  state.jobs.set(job.id, job);
  emit('jobs:changed');
  updateBadge();
  if (job.kind.startsWith('index_') && job.kind !== 'index_conversation') emit('knowledge:changed');
  const finished = before && ACTIVE.includes(before) && !ACTIVE.includes(job.status);
  if (finished && job.kind !== 'index_conversation') {
    const kind = job.status === 'done' && !job.failed ? 'ok' : job.status === 'cancelled' ? 'info' : 'err';
    const detail = job.result?.indexed !== undefined ? ` : ${job.result.indexed} élément(s) indexé(s)` : '';
    toast(`${job.title}${detail}`, kind, 6000);
  }
}

function onEvent(ev) {
  switch (ev.type) {
    case 'job': onJob(ev.job); break;
    case 'executor': state.executor = ev; emit('executor:changed'); break;
    case 'jobs_reset': loadJobs().then(updateBadge); break;
    case 'conversations': emit('conversations:changed'); break;
    case 'knowledge': emit('knowledge:changed'); break;
    case 'settings': loadSettings(); break;
    default: break;
  }
}

async function boot() {
  hydrateIcons();
  mountChat($('#view-chat'));
  mountLibrary($('#view-library'));
  mountJobs($('#view-jobs'));
  mountNode($('#view-node'));
  mountSettings($('#view-settings'));
  $$('.rail-btn').forEach(b => { b.onclick = () => show(b.dataset.view); });
  on('view:show', show);
  on('models:reload', loadModels);

  let wasConnected = true;
  listen(onEvent, connected => {
    if (connected && !wasConnected) { loadJobs().then(updateBadge); emit('conversations:changed'); }
    wasConnected = connected;
  });

  await Promise.all([loadSettings(), loadNode(), loadJobs().then(updateBadge)]);
  await loadModels();                       // après le profil : libellés VRAM
  show(prefs.get('view', 'chat'));
  const last = prefs.get('lastConversation', null);
  emit(last ? 'chat:open' : 'chat:new', last);
}

boot().catch(err => {
  document.body.innerHTML = `<div class="empty" style="margin-top:20vh">Impossible de joindre le nœud Cœur IA.<br>
    <span class="faint">${esc(err.message || err)}</span></div>`;
});
