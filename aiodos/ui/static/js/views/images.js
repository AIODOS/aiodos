// Images et vidéos : les créations en cours et la galerie. Elles se demandent dans une
// discussion, comme dans les autres assistants (chat-create.js) ; chacune est une unité de la
// file, calculée ici ou par mes machines qui ont le modèle (docs/generation-repartie.md). Les
// modèles se gèrent dans Réglages → Modèles (settings-models.js) : ici, un résumé en bas. Au-dessus
// de la galerie, ComfyUI : ses recettes, mes workflows, et « Créer » (images-comfy.js).
import { get, post } from '../api.js';
import { $, h, esc, debounce } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { errorToast } from '../ui.js';
import { openMedia, fileOf, thumbOf, isVideo, length } from './media-viewer.js';
import { byPower } from './models.js';
import { mountComfy, refreshComfy } from './images-comfy.js';

// Les créations : celles de sd.cpp, et celles de ComfyUI (images-comfy.js)
const KINDS = ['generate_images', 'comfy_create'];

const ACTIVE = ['queued', 'running', 'paused'];
const PAGE = 60;

let root;
let info = null;                 // état des modèles et du moteur (GET /images/models)
let images = [];                 // galerie, les plus récentes d'abord
let complete = false;            // toute la galerie est chargée
let shownDone = new Map();       // travail → images déjà comptées (recharger à chaque nouvelle)

export function mountImages(container) {
  // L'icône du bouton, au milieu de la phrase : posée après l'échappement du texte
  const ask = esc(t('images.ask')).replace('{icon}', () => icon('image'));
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head">
        <div class="grow"><h1>${esc(t('images.title'))}</h1>
          <div class="sub img-sub">${ask}</div></div>
        <div class="page-actions">
          <button class="btn primary" data-create="image">${icon('image')}${esc(t('images.create_image'))}</button>
          <button class="btn" data-create="video">${icon('video')}${esc(t('images.create_video'))}</button>
        </div>
      </div>
      <div id="img-banner"></div>
      <div id="img-pending"></div>
      <div id="img-comfy-box"></div>
      <div class="img-grid" id="img-grid"></div>
      <div class="img-more"><button class="btn ghost" id="img-more" hidden>${esc(t('images.older'))}</button></div>
      <div class="img-summary" id="img-summary"></div>
    </div></div>`);
  container.append(root);
  mountComfy($('#img-comfy-box', root));
  root.querySelectorAll('[data-create]').forEach(b => {
    b.onclick = () => emit('chat:create', { kind: b.dataset.create });
  });
  $('#img-more', root).onclick = () => loadImages(true);
  on('view:shown', view => { if (view === 'images') refresh(); });
  on('images:changed', debounce(() => { if (state.view === 'images') refresh(); }, 300));
  on('jobs:changed', () => { if (state.view === 'images') onJobs(); });
  setInterval(() => {            // avancement d'un téléchargement, dans le résumé
    if (state.view === 'images' && info?.download?.running) loadModels();
  }, 1500);
}

async function refresh() {
  await Promise.all([loadModels(), loadImages(false), refreshComfy()]);
  onJobs();
}

async function loadModels() {
  try { info = await get('/images/models'); } catch (err) { errorToast(err); return; }
  renderModels();
}

async function loadImages(more) {
  const before = more && images.length ? `&before=${images[images.length - 1].id}` : '';
  let page;
  try { page = await get(`/images?limit=${PAGE}${before}`); } catch (err) { errorToast(err); return; }
  images = more ? images.concat(page) : page;
  complete = page.length < PAGE;
  renderGrid();
}

// ------------------------------------------------------------------ modèles
/** Réglages → Modèles, la section affichée. */
function manageModels() {
  emit('view:show', 'settings');
  emit('settings:focus', 'modeles');
}

function renderModels() {
  // En haut, seulement quand rien ne peut calculer : ni ce PC, ni une de mes machines
  const usable = info.models.some(m => (m.state === 'ready' && info.engine.found) || m.helpers.length);
  $('#img-banner', root).innerHTML = usable ? '' : `
    <div class="banner info wrap">${icon('image')}<div class="banner-text">${esc(t('images.no_model'))}</div>
      <div class="banner-actions"><button class="btn sm primary" data-manage>${icon('download')}${esc(t('chat_create.choose_model'))}</button></div></div>`;
  // En bas, le résumé, du plus puissant au moins puissant ; la gestion est dans les Réglages
  const ready = info.models.filter(m => m.state === 'ready').sort(byPower);
  const elsewhere = info.models.filter(m => m.state !== 'ready' && m.helpers.length).sort(byPower);
  const missing = info.models.length - ready.length - elsewhere.length;
  const dl = info.download?.running ? info.download : null;
  const parts = [];                // des textes (échappés une fois assemblés)
  if (ready.length) {
    parts.push(t('images.summary_ready', { names: ready.map(m => m.label).join(', '), n: ready.length }));
  }
  for (const m of elsewhere) parts.push(t('images.summary_elsewhere', { name: m.label, machines: m.helpers.join(', ') }));
  if (dl) {
    const label = info.models.find(m => m.id === dl.model)?.label || dl.model;
    parts.push(t('images.summary_download', { name: label, pct: dl.total ? Math.floor(100 * dl.done / dl.total) : 0 }));
  } else if (missing) {
    parts.push(t('images.summary_missing', { n: missing }));
  }
  const summary = parts.length ? t('images.summary', { parts: parts.join(' · ') }) : t('images.summary_none');
  $('#img-summary', root).innerHTML = `${icon('layers')}<span class="img-summary-text">${esc(summary)}</span>
    <button class="btn sm ghost" data-manage>${esc(t('images.manage_models'))}${icon('chev-right')}</button>`;
  root.querySelectorAll('[data-manage]').forEach(b => { b.onclick = manageModels; });
}

// --------------------------------------------------------- créations en cours
function onJobs() {
  const jobs = [...state.jobs.values()].filter(j => KINDS.includes(j.kind));
  let fresh = false;
  for (const j of jobs) {
    const before = shownDone.get(j.id);
    if (before !== undefined && j.done > before) fresh = true;
    shownDone.set(j.id, j.done);
  }
  if (fresh) loadImages(false);
  const pending = jobs.filter(j => ACTIVE.includes(j.status)).sort((a, b) => a.created - b.created);
  const names = new Map((state.network?.peers || []).map(p => [p.node_id, p.name]));
  $('#img-pending', root).innerHTML = pending.map(j => {
    // Une création de ComfyUI garde sa description dans « creation » (jobs/kinds/comfy_create.py)
    const p = (j.kind === 'comfy_create' ? j.params?.creation : j.params) || {};
    const away = Object.entries(j.remote || {}).map(([id, n]) => `${names.get(id) || t('images.another_machine')} (${n})`);
    const where = away.length ? ` · ${esc(t('images.computing_at', { machines: away.join(', ') }))}` : '';
    const pct = j.total ? Math.floor(100 * (j.done + j.failed) / j.total) : 0;
    const open = p.conversation_id ? `<button class="icon-btn" data-conv="${p.conversation_id}"
      title="${esc(t('images.open_conversation'))}">${icon('chat')}</button>` : '';
    return `<div class="card img-job">
      <div class="img-job-head"><span class="pill run">${j.done}/${j.total}</span>
        <span class="img-job-text">${esc(t('images.prompt', { prompt: p.prompt || '' }))}</span>${open}
        <button class="icon-btn danger" data-stop="${j.id}" title="${esc(t('images.cancel'))}">${icon('x')}</button></div>
      <div class="bar"><i class="fill" style="width:${pct}%"></i></div>
      <div class="faint">${esc(waiting(j, p, away))}${where}</div></div>`;
  }).join('');
  $('#img-pending', root).querySelectorAll('[data-stop]').forEach(b => {
    b.onclick = async () => {
      try { await post(`/jobs/${b.dataset.stop}/cancel`); } catch (err) { errorToast(err); }
    };
  });
  $('#img-pending', root).querySelectorAll('[data-conv]').forEach(b => {
    b.onclick = () => { emit('view:show', 'chat'); emit('chat:open', Number(b.dataset.conv)); };
  });
}

function waiting(job, params, away) {
  if (job.status !== 'queued' || away.length) return t('images.running');
  if (job.kind === 'comfy_create') return t('images_comfy.waiting');
  const model = info?.models.find(m => m.id === params.model);
  const here = info?.engine.found && model?.state === 'ready';
  return t(here ? 'images.waiting_gpu' : 'images.waiting_machine');
}

// ----------------------------------------------------------------- galerie
function renderGrid() {
  const grid = $('#img-grid', root);
  $('#img-more', root).hidden = complete;
  if (!images.length) {
    grid.innerHTML = `<div class="card empty img-empty">${icon('image')}<div>${esc(t('images.empty'))}</div></div>`;
    return;
  }
  grid.innerHTML = images.map(img => `
    <button class="img-tile" data-id="${img.id}" title="${esc(img.prompt)}">
      ${isVideo(img)
        ? `<video muted loop playsinline preload="metadata" src="${fileOf(img)}"
             style="aspect-ratio:${img.width}/${img.height}"></video>`
        : `<img loading="lazy" src="${thumbOf(img)}" alt="${esc(img.prompt)}"
             style="aspect-ratio:${img.width}/${img.height}">`}
      <span class="img-tag">${isVideo(img) ? `${esc(t('images.video_length', { length: length(img) }))} · ` : ''}${esc(img.machine || t('images.this_machine'))}${img.seconds ? ` · ${Math.round(img.seconds)} s` : ''}</span>
    </button>`).join('');
  grid.querySelectorAll('.img-tile').forEach(tile => {
    const shown = images.find(i => i.id === Number(tile.dataset.id));
    tile.onclick = () => openMedia(shown, {
      // « Reprendre » : la description revient dans une discussion, en mode « Créer » (un modèle
      // de sd.cpp seulement : une création de ComfyUI se refait depuis sa recette, ci-dessus)
      onReuse: String(shown?.model || '').startsWith('comfyui/') ? undefined
        : img => emit('chat:create', { kind: isVideo(img) ? 'video' : 'image', model: img.model,
          prompt: img.prompt, width: img.width, height: img.height, frames: img.frames, seed: img.seed }),
      onDeleted: img => { images = images.filter(i => i.id !== img.id); renderGrid(); },
      onConversation: img => { emit('view:show', 'chat'); emit('chat:open', img.conversation_id); },
    });
    const video = tile.querySelector('video');
    if (video) {                  // la vidéo s'anime au survol
      tile.onmouseenter = () => video.play().catch(() => {});
      tile.onmouseleave = () => video.pause();
    }
  });
}
