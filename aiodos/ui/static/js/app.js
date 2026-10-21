// Démarrage de l'interface : vues, navigation, événements du nœud.
import { get, listen } from './api.js';
import { $, $$, esc, debounce, count, acceptsText, dropRefused } from './dom.js';
import { hydrateIcons } from './icons.js';
import { state, emit, on, prefs } from './state.js';
import { toast } from './ui.js';
import { mountChat } from './views/chat.js';
import { mountLibrary } from './views/library.js';
import { mountJobs, loadJobs } from './views/jobs.js';
import { mountImages } from './views/images.js';
import { mountNode, loadNode } from './views/node.js';
import { mountNetwork, loadNetwork, answerPairRequest, answerIntroduction } from './views/network.js';
import { mountSettings } from './views/settings.js';
import { announceUpdate, loadUpdates } from './views/updates.js';
import { loadGateway } from './views/gateway.js';
import { mountRailStatus } from './views/connected.js';
import { mountMobile } from './mobile.js';
import { frenchSpacing } from './typo.js';

const VIEWS = ['chat', 'library', 'images', 'jobs', 'node', 'network', 'settings'];
const ACTIVE = ['queued', 'running', 'paused'];

function show(view) {
  if (!VIEWS.includes(view)) view = 'chat';
  $$('.view').forEach(v => { v.hidden = v.id !== `view-${view}`; });
  $$('.rail-btn[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  state.view = view;
  prefs.set('view', view);
  emit('view:shown', view);
}

async function loadSettings() {
  state.settings = await get('/settings');
  emit('settings:loaded');
}

let lastRemote = '';
function remoteSignature(net) {
  return (net?.peers || []).filter(p => p.online && p.profile?.lends_models)
    .map(p => `${p.node_id}:${count(p.profile.ollama?.models)}`).sort().join(',');
}

async function loadModels() {
  try { state.models = await get('/models'); } catch { state.models = []; }
  state.modelsLoaded = true;
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
  if ((job.kind.startsWith('index_') && job.kind !== 'index_conversation') || job.kind === 'reindex') {
    emit('knowledge:changed');
  }
  if (!before && job.kind === 'reindex' && ACTIVE.includes(job.status)) {
    toast('Nouveau modèle d\'embedding : réindexation des connaissances lancée (voir Tâches).', 'info', 6000);
  }
  const finished = before && ACTIVE.includes(before) && !ACTIVE.includes(job.status);
  if (finished && job.kind === 'generate_images') {
    const n = job.result?.images ?? job.done;
    const noun = job.params?.frames ? 'vidéo' : 'image';
    // Demandée dans une conversation : rien à dire si elle est sous les yeux
    const conv = job.params?.conversation_id;
    const seen = conv ? state.view === 'chat' && state.currentConversation?.id === conv
      : state.view === 'images';
    if (!seen && job.status !== 'cancelled') {
      toast(`${job.title} : ${n} ${noun}${n > 1 ? 's' : ''} prête${n > 1 ? 's' : ''} `
        + `(${conv ? 'dans sa conversation' : 'vue Images et vidéos'}).`,
        job.status === 'done' && !job.failed ? 'ok' : 'err', 6000);
    }
  } else if (finished && job.kind !== 'index_conversation') {
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
    case 'network': emit('network:changed'); break;
    case 'updates': loadUpdates(); break;
    case 'gateway': if (state.view === 'settings') loadGateway(); break;
    case 'images': emit('images:changed'); break;
    case 'model_install': emit('model-install', ev); break;
    case 'cluster': emit('cluster:changed', ev); break;
    case 'pair_request': answerPairRequest(ev); break;      // quelle que soit la vue ouverte
    case 'group_request': answerIntroduction(ev); break;   // présentation (appairage par groupe)
    case 'pair_result':
      toast(ev.outcome === 'paired'
        ? `Machine appairée : « ${ev.name} »${ev.via ? `, présentée par « ${ev.via} »` : ''}`
        : `Appairage avec « ${ev.name} » annulé.`, ev.outcome === 'paired' ? 'ok' : 'info', 6000);
      break;
    default: break;
  }
}

// Un fichier ou un lien lâché sur la fenêtre ne remplace pas l'interface : la fenêtre native
// donnerait à la page ainsi ouverte son pont vers Python (ui/window.py le refuse aussi). Le
// texte se glisse toujours dans les zones de saisie, les conversations dans leur liste
// (sidebar.js, qui garde ses propres glisser-déposer).
function keepDrops() {
  const refused = e => dropRefused(e.dataTransfer?.types, acceptsText(e.target));
  document.addEventListener('dragover', e => {
    if (e.defaultPrevented || !refused(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'none';
  });
  document.addEventListener('drop', e => { if (refused(e)) e.preventDefault(); });
}

async function boot() {
  frenchSpacing();                          // avant les vues : tout ce qui s'ajoute est suivi
  keepDrops();
  hydrateIcons();
  mountChat($('#view-chat'));
  mountLibrary($('#view-library'));
  mountImages($('#view-images'));
  mountJobs($('#view-jobs'));
  mountNode($('#view-node'));
  mountNetwork($('#view-network'));
  mountSettings($('#view-settings'));
  // Toucher à nouveau la vue ouverte : au téléphone, « Discussions » ouvre ou ferme la liste
  $$('.rail-btn[data-view]').forEach(b => {
    b.onclick = () => {
      const again = state.view === b.dataset.view;
      show(b.dataset.view);
      if (again) emit('rail:again', b.dataset.view);
    };
  });
  mountMobile();
  mountRailStatus();
  on('view:show', show);
  on('models:reload', loadModels);
  // Les modèles prêtés par mes machines apparaissent et disparaissent avec elles
  on('network:loaded', debounce(() => {
    const ids = remoteSignature(state.network);
    if (ids !== lastRemote) { lastRemote = ids; loadModels(); }
  }, 1000));

  show(prefs.get('view', 'chat'));          // affichage immédiat, les données suivent

  let wasConnected = true;
  listen(onEvent, connected => {
    if (connected && !wasConnected) { loadJobs().then(updateBadge); emit('conversations:changed'); }
    wasConnected = connected;
  });

  loadJobs().then(updateBadge);
  loadNode();                               // en parallèle : les libellés VRAM suivront
  loadNetwork();
  await loadSettings();
  announceUpdate();                         // « AIODOS a été mis à jour », une fois
  await loadModels();
  const last = prefs.get('lastConversation', null);
  emit(last ? 'chat:open' : 'chat:new', last);

  // Ollama arrêté (redémarrage, mise à jour…) : on le guette sans rien demander
  setInterval(() => {
    if (!state.models.some(m => m.group === 'local')) loadModels();
  }, 8000);
  setInterval(() => { if (!document.hidden) loadNetwork(); }, 30000);   // machines qui apparaissent ou partent
}

boot().catch(err => {
  document.body.innerHTML = `<div class="empty" style="margin-top:20vh">Impossible de joindre le nœud AIODOS.<br>
    <span class="faint">${esc(err.message || err)}</span></div>`;
});
