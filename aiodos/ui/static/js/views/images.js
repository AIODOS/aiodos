// Images et vidéos : une description, et chaque image ou vidéo demandée devient une unité
// de la file, calculée ici ou par mes machines qui ont le modèle (docs/generation-repartie.md).
import { get, post, del, fileUrl } from '../api.js';
import { $, h, esc, bytes, ago, debounce } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, prefs } from '../state.js';
import { toast, errorToast, confirmDanger, modal } from '../ui.js';

const ACTIVE = ['queued', 'running', 'paused'];
const COUNTS = [1, 2, 4, 8, 16];
const PAGE = 60;
const PLACEHOLDER = {
  image: 'Un phare sous l\'orage, vagues immenses, lumière dorée, photo argentique…',
  video: 'Un renard roux qui trotte dans la neige fraîche, lumière du matin, caméra qui le suit…',
};

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
          <div class="sub">Décris une image ou une vidéo : chacune est une unité de la file, calculée
          ici ou par tes machines qui ont le même modèle et prêtent leur puissance.</div></div>
      </div>
      <div id="img-models"></div>
      <div class="card img-form">
        <textarea class="textarea" id="img-prompt" rows="3" maxlength="2000"
          placeholder="${esc(PLACEHOLDER.image)}"></textarea>
        <div class="img-controls">
          <select class="select" id="img-model" title="Modèle"></select>
          <select class="select" id="img-format" title="Format"></select>
          <select class="select" id="img-frames" title="Durée" hidden></select>
          <select class="select" id="img-count" title="Combien"></select>
          <input class="input" id="img-seed" inputmode="numeric" placeholder="Graine : au hasard"
            title="Même graine, même description : même résultat">
          <div class="grow"></div>
          <button class="btn primary" id="img-go">${icon('image')}Créer</button>
        </div>
      </div>
      <div id="img-pending"></div>
      <div class="img-grid" id="img-grid"></div>
      <div class="img-more"><button class="btn ghost" id="img-more" hidden>Voir les plus anciennes</button></div>
    </div></div>`);
  container.append(root);
  $('#img-model', root).onchange = () => { fillControls(); prefs.set('images.model', $('#img-model', root).value); };
  $('#img-go', root).onclick = create;
  $('#img-prompt', root).onkeydown = e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); create(); }
  };
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
const kindOf = id => modelOf(id)?.kind || 'image';

function renderModels() {
  const box = $('#img-models', root);
  const select = $('#img-model', root);
  const chosen = select.value || prefs.get('images.model', '');
  const group = (kind, label) => {
    const items = info.models.filter(m => (m.kind || 'image') === kind);
    return items.length ? `<optgroup label="${label}">${items.map(m =>
      `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('')}</optgroup>` : '';
  };
  select.innerHTML = group('image', 'Images') + group('video', 'Vidéos');
  select.value = info.models.some(m => m.id === chosen) ? chosen : info.models[0]?.id || '';
  fillControls();
  const engine = info.engine.found ? '' : `
    <div class="banner warn">${icon('warn')}<div>stable-diffusion.cpp n'est pas installé sur cette
      machine (Réglages → Images). ${anyHelper() ? 'Tes machines qui ont le modèle calculeront à sa place.'
      : 'Sans lui, seules tes machines qui ont le modèle peuvent calculer.'}</div></div>`;
  box.innerHTML = engine + info.models.map(modelCard).join('');
  box.querySelectorAll('[data-download]').forEach(b => {
    b.onclick = () => download(b.dataset.download, b.dataset.source || null);
  });
  box.querySelectorAll('[data-cancel]').forEach(b => { b.onclick = cancelDownload; });
}

/** Formats, durée et nombre selon le modèle choisi (règles du catalogue). */
function fillControls() {
  const id = $('#img-model', root).value;
  const model = modelOf(id);
  if (!model) return;
  const kind = model.kind || 'image';
  const sizes = model.sizes.map(([w, hgt, label]) => [`${w}x${hgt}`, label]);
  const format = $('#img-format', root);
  const previous = prefs.get(`images.format.${id}`, sizes[0][0]);
  format.innerHTML = sizes.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('');
  format.value = sizes.some(([v]) => v === previous) ? previous : sizes[0][0];
  const frames = $('#img-frames', root);
  frames.hidden = kind !== 'video';
  frames.innerHTML = (model.durations || []).map(([n, l]) => `<option value="${n}">${esc(l)}</option>`).join('');
  const wanted = String(prefs.get(`images.frames.${id}`, model.default_frames || ''));
  frames.value = (model.durations || []).some(([n]) => String(n) === wanted) ? wanted
    : String(model.default_frames || model.durations?.[0]?.[0] || '');
  const count = $('#img-count', root);
  const n = count.value || String(prefs.get('images.count', 1));
  const noun = kind === 'video' ? 'vidéo' : 'image';
  count.innerHTML = COUNTS.map(c => `<option value="${c}">${c} ${noun}${c > 1 ? 's' : ''}</option>`).join('');
  count.value = COUNTS.map(String).includes(n) ? n : '1';
  $('#img-prompt', root).placeholder = PLACEHOLDER[kind];
}

function anyHelper() {
  return info.models.some(m => m.helpers.length);
}

function modelCard(m) {
  const dl = info.download?.model === m.id ? info.download : null;
  const helpers = m.helpers.length
    ? `<div class="faint">Mes machines qui l'ont et prêtent leur puissance : ${m.helpers.map(esc).join(', ')}.</div>` : '';
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
    const size = bytes(m.missing_bytes);
    const copies = [...m.sources].sort((a, b) => b.near - a.near).map(s => `
      <button class="btn sm" data-download="${esc(m.id)}" data-source="${esc(s.id)}">${icon('network')}Copier
        depuis ${esc(s.name)} (${s.near ? 'réseau local' : 'Internet'}, ${size})</button>`).join('');
    const label = m.state === 'bad' ? 'Retélécharger' : `Télécharger depuis Hugging Face (${size})`;
    action = `<div class="img-dl">${copies}
      <button class="btn sm${copies ? ' ghost' : ''}" data-download="${esc(m.id)}">${icon('cloud')}${label}</button>
      ${dl?.error ? `<span class="err-text">${esc(dl.error)}</span>` : ''}</div>`;
  }
  return `<div class="card img-model-card">
    <h2>${glyph}${esc(m.label)} <span class="pill${m.notice ? ' warn' : ''}">${esc(m.license)}</span></h2>
    <p class="hint">${esc(m.about)} ${bytes(m.size)} depuis Hugging Face, chaque fichier vérifié par son empreinte.</p>
    ${notice}${helpers}${action}</div>`;
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

// --------------------------------------------------------------- demandes
async function create() {
  const prompt = $('#img-prompt', root).value.trim();
  if (!prompt) { $('#img-prompt', root).focus(); return; }
  const model = $('#img-model', root).value;
  const kind = kindOf(model);
  const [width, height] = $('#img-format', root).value.split('x').map(Number);
  const count = Number($('#img-count', root).value);
  const frames = kind === 'video' ? Number($('#img-frames', root).value) : null;
  const seedText = $('#img-seed', root).value.trim();
  prefs.set(`images.format.${model}`, $('#img-format', root).value);
  prefs.set('images.count', count);
  prefs.set('images.model', model);
  if (frames) prefs.set(`images.frames.${model}`, frames);
  const go = $('#img-go', root);
  go.disabled = true;
  try {
    const job = await post('/images/generate', {
      model, prompt, width, height, count, frames, seed: seedText === '' ? null : Number(seedText),
    });
    if (!job.here) toast('Elles seront calculées par tes machines qui ont le modèle.', 'info', 5000);
  } catch (err) {
    errorToast(err);
  } finally {
    go.disabled = false;
  }
}

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
    return `<div class="card img-job">
      <div class="img-job-head"><span class="pill run">${j.done}/${j.total}</span>
        <span class="img-job-text">« ${esc(p.prompt || '')} »</span>
        <button class="icon-btn danger" data-stop="${j.id}" title="Annuler">${icon('x')}</button></div>
      <div class="bar"><i class="fill" style="width:${pct}%"></i></div>
      <div class="faint">${waiting(j, p, away)}${where}</div></div>`;
  }).join('');
  $('#img-pending', root).querySelectorAll('[data-stop]').forEach(b => {
    b.onclick = async () => {
      try { await post(`/jobs/${b.dataset.stop}/cancel`); } catch (err) { errorToast(err); }
    };
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
// L'adresse porte le nom du fichier : un numéro réutilisé après une suppression ne ressort
// pas l'ancienne image du cache du navigateur
const fileOf = img => `${fileUrl(`/images/${img.id}/file`)}&v=${encodeURIComponent(img.file)}`;
const isVideo = img => img.media === 'video';
const length = img => (img.frames && img.fps ? `${(img.frames / img.fps).toFixed(1).replace('.', ',')} s` : '');

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
        : `<img loading="lazy" src="${fileOf(img)}" alt="${esc(img.prompt)}"
             style="aspect-ratio:${img.width}/${img.height}">`}
      <span class="img-tag">${isVideo(img) ? `vidéo ${length(img)} · ` : ''}${esc(img.machine || 'cette machine')}${img.seconds ? ` · ${Math.round(img.seconds)} s` : ''}</span>
    </button>`).join('');
  grid.querySelectorAll('.img-tile').forEach(t => {
    t.onclick = () => open(images.find(i => i.id === Number(t.dataset.id)));
    const video = t.querySelector('video');
    if (video) {                  // la vidéo s'anime au survol
      t.onmouseenter = () => video.play().catch(() => {});
      t.onmouseleave = () => video.pause();
    }
  });
}

function open(img) {
  if (!img) return;
  const ext = isVideo(img) ? 'webm' : 'png';
  const media = isVideo(img)
    ? `<video src="${fileOf(img)}" controls autoplay loop playsinline></video>`
    : `<img src="${fileOf(img)}" alt="${esc(img.prompt)}">`;
  const frames = isVideo(img) && img.frames ? ` · ${img.frames} images à ${img.fps} i/s (${length(img)})` : '';
  const back = h(`
    <div class="modal-back img-view" role="dialog" aria-modal="true">
      <div class="img-view-box">
        ${media}
        <div class="img-view-side">
          <div class="img-view-prompt">${esc(img.prompt)}</div>
          <div class="faint">${img.width} × ${img.height}${frames} · graine ${img.seed} · ${img.steps} pas<br>
            ${esc(img.machine || 'cette machine')}${img.seconds ? ` · ${Math.round(img.seconds)} s` : ''} · ${ago(img.created)}</div>
          <div class="img-view-actions">
            <a class="btn sm" href="${fileOf(img)}" download="aiodos-${img.id}.${ext}">${icon('file')}Enregistrer</a>
            <button class="btn sm" data-act="reuse">${icon('retry')}Reprendre</button>
            <button class="btn sm danger" data-act="delete">${icon('trash')}Supprimer</button>
            <button class="btn sm ghost" data-act="close">${icon('x')}Fermer</button>
          </div>
        </div>
      </div>
    </div>`);
  const close = () => { back.remove(); document.removeEventListener('keydown', onKey, true); };
  const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  document.addEventListener('keydown', onKey, true);
  back.onclick = e => { if (e.target === back) close(); };
  back.querySelector('[data-act="close"]').onclick = close;
  back.querySelector('[data-act="reuse"]').onclick = () => {
    const select = $('#img-model', root);
    if ([...select.options].some(o => o.value === img.model)) { select.value = img.model; fillControls(); }
    $('#img-prompt', root).value = img.prompt;
    $('#img-seed', root).value = img.seed;
    const format = `${img.width}x${img.height}`;
    if ([...$('#img-format', root).options].some(o => o.value === format)) $('#img-format', root).value = format;
    if (img.frames && [...$('#img-frames', root).options].some(o => o.value === String(img.frames))) {
      $('#img-frames', root).value = String(img.frames);
    }
    close();
    $('#img-prompt', root).focus();
  };
  back.querySelector('[data-act="delete"]').onclick = async () => {
    const what = isVideo(img) ? 'cette vidéo' : 'cette image';
    if (!await confirmDanger(`Supprimer ${what} ?`, 'Le fichier est effacé de cette machine.')) return;
    try { await del(`/images/${img.id}`); } catch (err) { errorToast(err); return; }
    images = images.filter(i => i.id !== img.id);
    renderGrid();
    close();
  };
  document.body.append(back);
}
