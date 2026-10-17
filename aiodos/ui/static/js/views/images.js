// Images : une description, et chaque image demandée devient une unité de la file,
// calculée ici ou par mes machines qui ont le modèle (docs/generation-repartie.md).
import { get, post, del, fileUrl } from '../api.js';
import { $, h, esc, bytes, ago, debounce } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, prefs } from '../state.js';
import { toast, errorToast, confirmDanger } from '../ui.js';

const ACTIVE = ['queued', 'running', 'paused'];
const FORMATS = [
  ['1024x1024', 'Carré · 1024 × 1024'], ['768x1344', 'Portrait · 768 × 1344'],
  ['1344x768', 'Paysage · 1344 × 768'], ['512x512', 'Rapide · 512 × 512'],
];
const COUNTS = [1, 2, 4, 8, 16];
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
        <div><h1>Images</h1>
          <div class="sub">Décris une image : chacune est une unité de la file, calculée ici ou par
          tes machines qui ont le même modèle et prêtent leur puissance.</div></div>
      </div>
      <div id="img-models"></div>
      <div class="card img-form">
        <textarea class="textarea" id="img-prompt" rows="3" maxlength="2000"
          placeholder="Un phare sous l'orage, vagues immenses, lumière dorée, photo argentique…"></textarea>
        <div class="img-controls">
          <select class="select" id="img-model" title="Modèle"></select>
          <select class="select" id="img-format" title="Format">
            ${FORMATS.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select>
          <select class="select" id="img-count" title="Nombre d'images">
            ${COUNTS.map(n => `<option value="${n}">${n} image${n > 1 ? 's' : ''}</option>`).join('')}</select>
          <input class="input" id="img-seed" inputmode="numeric" placeholder="Graine : au hasard"
            title="Même graine, même description : même image">
          <div class="grow"></div>
          <button class="btn primary" id="img-go">${icon('image')}Créer</button>
        </div>
      </div>
      <div id="img-pending"></div>
      <div class="img-grid" id="img-grid"></div>
      <div class="img-more"><button class="btn ghost" id="img-more" hidden>Voir les plus anciennes</button></div>
    </div></div>`);
  container.append(root);
  $('#img-format', root).value = prefs.get('images.format', '1024x1024');
  $('#img-count', root).value = String(prefs.get('images.count', 1));
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
function renderModels() {
  const box = $('#img-models', root);
  const select = $('#img-model', root);
  const chosen = select.value || prefs.get('images.model', '');
  select.innerHTML = info.models.map(m => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
  select.value = info.models.some(m => m.id === chosen) ? chosen : info.models[0]?.id || '';
  select.hidden = info.models.length < 2;
  const engine = info.engine.found ? '' : `
    <div class="banner warn">${icon('warn')}<div>stable-diffusion.cpp n'est pas installé sur cette
      machine (Réglages → Images). ${anyHelper() ? 'Tes machines qui ont le modèle calculeront à sa place.'
      : 'Sans lui, seules tes machines qui ont le modèle peuvent calculer les images.'}</div></div>`;
  box.innerHTML = engine + info.models.map(modelCard).join('');
  box.querySelectorAll('[data-download]').forEach(b => {
    b.onclick = () => download(b.dataset.download, b.dataset.source || null);
  });
  box.querySelectorAll('[data-cancel]').forEach(b => { b.onclick = cancelDownload; });
}

function anyHelper() {
  return info.models.some(m => m.helpers.length);
}

function modelCard(m) {
  const dl = info.download?.model === m.id ? info.download : null;
  const helpers = m.helpers.length
    ? `<div class="faint">Mes machines qui l'ont et prêtent leur puissance : ${m.helpers.map(esc).join(', ')}.</div>` : '';
  const ready = m.state === 'ready';
  if (ready && !dl?.running) {
    const where = info.engine.device ? ` · calcul sur ${esc(info.engine.device)}` : '';
    return `<div class="img-model ok">${icon('check')}<div><b>${esc(m.label)}</b>
      <span class="pill ok">Prêt</span> <span class="faint">${esc(m.license)}${where}</span>${helpers}</div></div>`;
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
    <h2>${icon('image')}${esc(m.label)} <span class="pill">${esc(m.license)}</span></h2>
    <p class="hint">${esc(m.about)} ${bytes(m.size)} depuis Hugging Face, chaque fichier vérifié par son empreinte.</p>
    ${helpers}${action}</div>`;
}

async function download(id, source) {
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
  const [width, height] = $('#img-format', root).value.split('x').map(Number);
  const count = Number($('#img-count', root).value);
  const seedText = $('#img-seed', root).value.trim();
  const model = $('#img-model', root).value;
  prefs.set('images.format', $('#img-format', root).value);
  prefs.set('images.count', count);
  prefs.set('images.model', model);
  const go = $('#img-go', root);
  go.disabled = true;
  try {
    const job = await post('/images/generate', {
      model, prompt, width, height, count, seed: seedText === '' ? null : Number(seedText),
    });
    if (!job.here) toast('Les images seront calculées par tes machines qui ont le modèle.', 'info', 5000);
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
      <div class="faint">${j.status === 'queued' && !away.length ? 'En attente d\'une carte graphique libre'
        : 'En cours'}${where}</div></div>`;
  }).join('');
  $('#img-pending', root).querySelectorAll('[data-stop]').forEach(b => {
    b.onclick = async () => {
      try { await post(`/jobs/${b.dataset.stop}/cancel`); } catch (err) { errorToast(err); }
    };
  });
}

// ----------------------------------------------------------------- galerie
function renderGrid() {
  const grid = $('#img-grid', root);
  $('#img-more', root).hidden = complete;
  if (!images.length) {
    grid.innerHTML = `<div class="card empty img-empty">${icon('image')}<div>Aucune image pour l'instant.</div></div>`;
    return;
  }
  grid.innerHTML = images.map(img => `
    <button class="img-tile" data-id="${img.id}" title="${esc(img.prompt)}">
      <img loading="lazy" src="${fileUrl(`/images/${img.id}/file`)}" alt="${esc(img.prompt)}"
        style="aspect-ratio:${img.width}/${img.height}">
      <span class="img-tag">${esc(img.machine || 'cette machine')}${img.seconds ? ` · ${Math.round(img.seconds)} s` : ''}</span>
    </button>`).join('');
  grid.querySelectorAll('.img-tile').forEach(t => {
    t.onclick = () => open(images.find(i => i.id === Number(t.dataset.id)));
  });
}

function open(img) {
  if (!img) return;
  const back = h(`
    <div class="modal-back img-view" role="dialog" aria-modal="true">
      <div class="img-view-box">
        <img src="${fileUrl(`/images/${img.id}/file`)}" alt="${esc(img.prompt)}">
        <div class="img-view-side">
          <div class="img-view-prompt">${esc(img.prompt)}</div>
          <div class="faint">${img.width} × ${img.height} · graine ${img.seed} · ${img.steps} pas<br>
            ${esc(img.machine || 'cette machine')}${img.seconds ? ` · ${Math.round(img.seconds)} s` : ''} · ${ago(img.created)}</div>
          <div class="img-view-actions">
            <a class="btn sm" href="${fileUrl(`/images/${img.id}/file`)}" download="aiodos-${img.id}.png">${icon('file')}Enregistrer</a>
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
    $('#img-prompt', root).value = img.prompt;
    $('#img-seed', root).value = img.seed;
    const format = `${img.width}x${img.height}`;
    if (FORMATS.some(([v]) => v === format)) $('#img-format', root).value = format;
    close();
    $('#img-prompt', root).focus();
  };
  back.querySelector('[data-act="delete"]').onclick = async () => {
    if (!await confirmDanger('Supprimer cette image ?', 'Le fichier est effacé de cette machine.')) return;
    try { await del(`/images/${img.id}`); } catch (err) { errorToast(err); return; }
    images = images.filter(i => i.id !== img.id);
    renderGrid();
    close();
  };
  document.body.append(back);
}
