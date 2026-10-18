// Images et vidéos : les modèles qui les calculent (téléchargement, copie depuis mes machines),
// les créations en cours et la galerie. Elles se demandent dans une discussion, comme dans les
// autres assistants (chat-create.js) ; chacune est une unité de la file, calculée ici ou par
// mes machines qui ont le modèle (docs/generation-repartie.md).
import { get, post, put, del } from '../api.js';
import { $, h, esc, bytes, debounce } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { errorToast, modal } from '../ui.js';
import { openMedia, fileOf, thumbOf, isVideo, length } from './media-viewer.js';

const ACTIVE = ['queued', 'running', 'paused'];
const PAGE = 60;

let root;
let info = null;                 // état des modèles et du moteur (GET /images/models)
let images = [];                 // galerie, les plus récentes d'abord
let complete = false;            // toute la galerie est chargée
let shownDone = new Map();       // travail → images déjà comptées (recharger à chaque nouvelle)

export function mountImages(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head">
        <div><h1>Images et vidéos</h1>
          <div class="sub">Tes créations, et les modèles qui les calculent ici ou chez tes machines qui
          prêtent leur puissance.</div></div>
      </div>
      <div class="card img-ask">
        <div class="img-ask-text">${icon('chat')}<div>Pour créer une image ou une vidéo, demande-la dans une
          discussion : « Crée une image d'un phare sous l'orage », ou touche ${icon('image')} à côté du
          message pour choisir le modèle, le format et le nombre.</div></div>
        <div class="img-ask-buttons">
          <button class="btn primary" data-create="image">${icon('image')}Créer une image</button>
          <button class="btn" data-create="video">${icon('video')}Créer une vidéo</button>
        </div>
      </div>
      <div id="img-models"></div>
      <div id="img-pending"></div>
      <div class="img-grid" id="img-grid"></div>
      <div class="img-more"><button class="btn ghost" id="img-more" hidden>Voir les plus anciennes</button></div>
    </div></div>`);
  container.append(root);
  root.querySelectorAll('[data-create]').forEach(b => {
    b.onclick = () => emit('chat:create', { kind: b.dataset.create });
  });
  $('#img-more', root).onclick = () => loadImages(true);
  on('view:shown', view => { if (view === 'images') refresh(); });
  on('images:changed', debounce(() => { if (state.view === 'images') refresh(); }, 300));
  on('jobs:changed', () => { if (state.view === 'images') onJobs(); });
  setInterval(() => {            // avancement d'un téléchargement
    if (state.view === 'images' && info?.download?.running) loadModels();
  }, 1500);
}

async function refresh() {
  await Promise.all([loadModels(), loadImages(false)]);
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
const modelOf = id => info?.models.find(m => m.id === id);

function renderModels() {
  const box = $('#img-models', root);
  const engine = info.engine.found ? '' : `
    <div class="banner warn">${icon('warn')}<div>stable-diffusion.cpp n'est pas installé sur cette
      machine (Réglages → Images). ${anyHelper() ? 'Tes machines qui ont le modèle calculeront à sa place.'
      : 'Sans lui, seules tes machines qui ont le modèle peuvent calculer.'}</div></div>`;
  box.innerHTML = engine + info.models.map(modelCard).join('');
  box.querySelectorAll('[data-download]').forEach(b => {
    b.onclick = () => download(b.dataset.download, b.dataset.source || null);
  });
  box.querySelectorAll('[data-cancel]').forEach(b => { b.onclick = cancelDownload; });
  box.querySelectorAll('[data-friends-ok]').forEach(b => {
    b.onclick = async () => {
      try { await put('/settings', { images_from_friends: true }); } catch (err) { errorToast(err); return; }
      loadModels();
    };
  });
}

function anyHelper() {
  return info.models.some(m => m.helpers.length);
}

function modelCard(m) {
  const dl = info.download?.model === m.id ? info.download : null;
  const helpers = m.helpers.length
    ? `<div class="faint">Machines qui l'ont et peuvent le calculer pour toi : ${m.helpers.map(esc).join(', ')}.</div>` : '';
  // Un ami propose sa carte graphique : avec ton accord, une fois pour toutes (Réglages)
  const friends = !info.from_friends && m.state !== 'ready' && (m.friends || []).length ? `
    <div class="banner info img-friends">${icon('network')}<div>${esc(m.friends.join(', '))} ${m.friends.length > 1
      ? 'peuvent' : 'peut'} calculer ce modèle pour toi : ta description et le résultat passeront chez
      ${m.friends.length > 1 ? 'eux' : 'lui'} le temps du calcul, sans y être gardés.</div>
      <button class="btn sm" data-friends-ok>Autoriser mes amis</button></div>` : '';
  const glyph = icon(m.kind === 'video' ? 'video' : 'image');
  const notice = m.notice ? `<div class="banner warn img-notice">${icon('warn')}<div>${esc(m.notice)}</div></div>` : '';
  if (m.state === 'ready' && !dl?.running) {
    const where = info.engine.device ? ` · calcul sur ${esc(info.engine.device)}` : '';
    return `<div class="img-model ok">${icon('check')}<div><b>${esc(m.label)}</b>
      <span class="pill ok">Prêt</span> <span class="faint">${esc(m.license)}${where}</span>${helpers}
      ${m.notice ? `<div class="faint">⚠ ${esc(m.notice)}</div>` : ''}</div></div>`;
  }
  let action;
  if (dl?.running) {
    const pct = dl.total ? Math.floor(100 * dl.done / dl.total) : 0;
    action = `<div class="img-dl"><div class="bar"><i class="fill" style="width:${pct}%"></i></div>
      <span class="faint">${bytes(dl.done)} sur ${bytes(dl.total)} · ${bytes(dl.rate)}/s
        · depuis ${esc(dl.source || 'Hugging Face')}</span>
      <button class="btn sm ghost" data-cancel>${icon('x')}Arrêter</button></div>`;
  } else if (m.state === 'checking') {
    action = '<span class="faint">Vérification des fichiers déjà présents…</span>';
  } else {
    // Fichiers identiques déjà sur ce PC (ComfyUI, PANDORA…) : repris d'abord, sans téléchargement
    const local = m.local || [];
    const localBytes = local.reduce((n, f) => n + f.size, 0);
    const rest = Math.max(0, m.missing_bytes - localBytes);
    const size = bytes(rest);
    const here = local.length ? `<div class="faint">${bytes(localBytes)} déjà sur ce PC
      (${esc([...new Set(local.map(f => f.from))].join(', '))}) : repris sans
      ${local.length > 1 ? 'les' : 'le'} retélécharger, après vérification.</div>` : '';
    const copies = rest ? [...m.sources].sort((a, b) => b.near - a.near).map(s => `
      <button class="btn sm" data-download="${esc(m.id)}" data-source="${esc(s.id)}">${icon('network')}Copier
        depuis ${esc(s.name)} (${s.near ? 'réseau local' : 'Internet'}, ${size})</button>`).join('') : '';
    const label = !rest ? `Reprendre depuis ce PC (${bytes(localBytes)})`
      : m.state === 'bad' ? 'Retélécharger' : `Télécharger depuis Hugging Face (${size})`;
    action = `${here}<div class="img-dl">${copies}
      <button class="btn sm${copies ? ' ghost' : ''}" data-download="${esc(m.id)}">${icon(rest ? 'cloud' : 'check')}${label}</button>
      ${dl?.error ? `<span class="err-text">${esc(dl.error)}</span>` : ''}</div>`;
  }
  return `<div class="card img-model-card">
    <h2>${glyph}${esc(m.label)} <span class="pill${m.notice ? ' warn' : ''}">${esc(m.license)}</span></h2>
    <p class="hint">${esc(m.about)} ${bytes(m.size)} depuis Hugging Face, chaque fichier vérifié par son empreinte.</p>
    ${notice}${helpers}${friends}${action}</div>`;
}

async function download(id, source) {
  const model = modelOf(id);
  if (model?.notice) {          // licence restrictive : lue et acceptée avant de télécharger
    const { ok } = await modal({ title: `Licence de ${model.label}`, text: model.notice,
      iconName: 'warn', confirm: 'J\'ai lu, télécharger', cancel: 'Annuler' });
    if (!ok) return;
  }
  try { info.download = await post(`/images/models/${encodeURIComponent(id)}/download`, { source }); }
  catch (err) { errorToast(err); return; }
  renderModels();
}

async function cancelDownload() {
  try { await del('/images/models/download'); } catch (err) { errorToast(err); }
  loadModels();
}

// --------------------------------------------------------- créations en cours
function onJobs() {
  const jobs = [...state.jobs.values()].filter(j => j.kind === 'generate_images');
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
    const p = j.params || {};
    const away = Object.entries(j.remote || {}).map(([id, n]) => `${esc(names.get(id) || 'une autre machine')} (${n})`);
    const where = away.length ? ` · en calcul chez ${away.join(', ')}` : '';
    const pct = j.total ? Math.floor(100 * (j.done + j.failed) / j.total) : 0;
    const open = p.conversation_id ? `<button class="icon-btn" data-conv="${p.conversation_id}"
      title="Ouvrir sa conversation">${icon('chat')}</button>` : '';
    return `<div class="card img-job">
      <div class="img-job-head"><span class="pill run">${j.done}/${j.total}</span>
        <span class="img-job-text">« ${esc(p.prompt || '')} »</span>${open}
        <button class="icon-btn danger" data-stop="${j.id}" title="Annuler">${icon('x')}</button></div>
      <div class="bar"><i class="fill" style="width:${pct}%"></i></div>
      <div class="faint">${waiting(j, p, away)}${where}</div></div>`;
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
  if (job.status !== 'queued' || away.length) return 'En cours';
  const model = info?.models.find(m => m.id === params.model);
  const here = info?.engine.found && model?.state === 'ready';
  return here ? 'En attente d\'une carte graphique libre'
    : 'En attente d\'une de tes machines qui a le modèle et prête sa puissance';
}

// ----------------------------------------------------------------- galerie
function renderGrid() {
  const grid = $('#img-grid', root);
  $('#img-more', root).hidden = complete;
  if (!images.length) {
    grid.innerHTML = `<div class="card empty img-empty">${icon('image')}<div>Aucune image ni vidéo pour l'instant.</div></div>`;
    return;
  }
  grid.innerHTML = images.map(img => `
    <button class="img-tile" data-id="${img.id}" title="${esc(img.prompt)}">
      ${isVideo(img)
        ? `<video muted loop playsinline preload="metadata" src="${fileOf(img)}"
             style="aspect-ratio:${img.width}/${img.height}"></video>`
        : `<img loading="lazy" src="${thumbOf(img)}" alt="${esc(img.prompt)}"
             style="aspect-ratio:${img.width}/${img.height}">`}
      <span class="img-tag">${isVideo(img) ? `vidéo ${length(img)} · ` : ''}${esc(img.machine || 'cette machine')}${img.seconds ? ` · ${Math.round(img.seconds)} s` : ''}</span>
    </button>`).join('');
  grid.querySelectorAll('.img-tile').forEach(t => {
    t.onclick = () => openMedia(images.find(i => i.id === Number(t.dataset.id)), {
      // « Reprendre » : la description revient dans une discussion, en mode « Créer »
      onReuse: img => emit('chat:create', { kind: isVideo(img) ? 'video' : 'image', model: img.model,
        prompt: img.prompt, width: img.width, height: img.height, frames: img.frames, seed: img.seed }),
      onDeleted: img => { images = images.filter(i => i.id !== img.id); renderGrid(); },
      onConversation: img => { emit('view:show', 'chat'); emit('chat:open', img.conversation_id); },
    });
    const video = t.querySelector('video');
    if (video) {                  // la vidéo s'anime au survol
      t.onmouseenter = () => video.play().catch(() => {});
      t.onmouseleave = () => video.pause();
    }
  });
}
