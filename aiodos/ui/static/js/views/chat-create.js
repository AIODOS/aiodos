// Créer une image ou une vidéo dans la conversation, comme dans les autres assistants
// (chat/creations.py). Le bouton à côté du message ouvre le mode « Créer » : modèle, format,
// durée, nombre ; sans lui, une demande écrite (« Crée une image d'un phare… ») est reconnue
// avant l'envoi. La réponse de l'assistant montre les images à mesure qu'elles arrivent,
// calculées ici ou par tes machines qui ont le modèle.
import { get, post, put } from '../api.js';
import { h, esc, duration } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit, prefs } from '../state.js';
import { menu, toast, errorToast } from '../ui.js';
import { openMedia, fileOf, isVideo, length } from './media-viewer.js';

const ACTIVE = ['queued', 'running', 'paused'];
const COUNTS = [1, 2, 4, 8];
const KINDS = { image: { noun: 'image', label: 'Image', icon: 'image' },
  video: { noun: 'vidéo', label: 'Vidéo', icon: 'video' } };
const PLACEHOLDER = { image: "Décris l'image à créer…", video: 'Décris la vidéo à créer…' };
const HINT = 'Entrée pour créer · Maj+Entrée pour aller à la ligne · Échap pour revenir à la conversation';

let info = null;              // modèles d'images et de vidéos (GET /images/models)
let mode = null;              // null, 'image' ou 'video' : le mode « Créer » du message
let preset = {};              // « Reprendre » : la graine, pour la prochaine création seulement
let ui = {};                  // éléments du compositeur
let onGrow = () => {};        // la conversation suit les images qui arrivent
const live = new Map();       // lot → réponse affichée (suivi du travail)

// ----------------------------------------------------------------- le mode « Créer »
/** Le bouton « Créer » dans la boîte du message, et sa barre de réglages au-dessus. */
export function mountCreate(footer, input, grow) {
  onGrow = grow || onGrow;
  const box = footer.querySelector('.composer-box');
  ui = {
    input, hint: footer.querySelector('.hint'),
    placeholder: input.placeholder, hintText: footer.querySelector('.hint')?.textContent || '',
    button: h(`<button class="icon-btn create-btn" title="Créer une image ou une vidéo">${icon('image')}</button>`),
    bar: h(`<div class="create-bar" hidden>
      <button class="create-kind" data-c="close" title="Revenir à la conversation"></button>
      <select class="select" data-c="model" title="Modèle"></select>
      <select class="select" data-c="format" title="Format"></select>
      <select class="select" data-c="frames" title="Durée" hidden></select>
      <select class="select" data-c="count" title="Combien"></select>
      <span class="chip on create-seed" hidden title="Même graine, même description : même résultat"></span>
      <span class="create-note"></span>
    </div>`),
  };
  box.prepend(ui.button);
  box.before(ui.bar);
  ui.button.onclick = () => menu(ui.button, Object.entries(KINDS).map(([kind, k]) => ({
    label: `Créer une ${k.noun}`, icon: k.icon, action: () => setMode(kind) })));
  // Les modèles se chargent dès que le pointeur approche du bouton : la barre est prête au clic
  ui.button.addEventListener('pointerenter', () => { if (!info) load(); }, { once: true });
  const field = name => ui.bar.querySelector(`[data-c="${name}"]`);
  field('close').onclick = () => setMode(null);
  field('model').onchange = () => { prefs.set(`create.model.${mode}`, field('model').value); fill(); };
  field('format').onchange = () => prefs.set(`images.format.${field('model').value}`, field('format').value);
  field('frames').onchange = () => prefs.set(`images.frames.${field('model').value}`, Number(field('frames').value));
  field('count').onchange = () => prefs.set(`create.count.${mode}`, Number(field('count').value));
  ui.bar.querySelector('.create-seed').onclick = () => { delete preset.seed; fill(); };
  input.addEventListener('keydown', e => {      // Échap : retour à la conversation
    if (e.key === 'Escape' && mode) { e.preventDefault(); setMode(null); }
  });
  on('images:changed', () => { if (mode) load(); });
  on('chat:create', p => { emit('view:show', 'chat'); setMode(p?.kind || 'image', p || {}); });
  on('jobs:changed', follow);
  setInterval(() => {                 // le temps qui passe, pour les créations en cours
    if (state.view !== 'chat') return;
    for (const view of live.values()) if (active(view)) paintState(view);
  }, 1000);
}

/** Ouvre le mode « Créer » (kind : 'image' ou 'video'), ou le ferme (null). p : réglages
 *  repris d'une création (modèle, format, durée, graine, description). */
export function setMode(kind, p = {}) {
  mode = kind;
  preset = kind && p.seed != null ? { seed: p.seed } : {};
  ui.bar.hidden = !kind;
  ui.button.classList.toggle('on', !!kind);
  ui.input.placeholder = kind ? PLACEHOLDER[kind] : ui.placeholder;
  if (ui.hint) ui.hint.textContent = kind ? HINT : ui.hintText;
  if (!kind) { ui.input.focus(); return; }
  if (p.prompt) {
    ui.input.value = p.prompt;
    ui.input.dispatchEvent(new Event('input'));
  }
  if (p.model) {                      // repris : ses réglages deviennent ceux de la barre
    prefs.set(`create.model.${kind}`, p.model);
    if (p.width && p.height) prefs.set(`images.format.${p.model}`, `${p.width}x${p.height}`);
    if (p.frames) prefs.set(`images.frames.${p.model}`, Number(p.frames));
  }
  fill();
  load();
  ui.input.focus();
}

async function load() {
  try { info = await get('/images/models'); } catch (err) { errorToast(err); return; }
  if (mode) fill();
}

const usable = m => (m.state === 'ready' && info?.engine.found) || m.helpers.length > 0;

/** Les listes de la barre, selon le genre et le modèle choisis (règles du catalogue). */
function fill() {
  const field = name => ui.bar.querySelector(`[data-c="${name}"]`);
  const k = KINDS[mode];
  ui.bar.querySelector('.create-kind').innerHTML = `${icon(k.icon)}${k.label}${icon('x')}`;
  const models = (info?.models || []).filter(m => (m.kind || 'image') === mode);
  const select = field('model');
  const wanted = prefs.get(`create.model.${mode}`, '') || select.value;
  select.innerHTML = models.map(m => {
    const where = !usable(m) ? ' · à installer' : m.state === 'ready' && info.engine.found ? ''
      : ` · chez ${m.helpers.join(', ')}`;
    return `<option value="${esc(m.id)}"${usable(m) ? '' : ' disabled'}>${esc(m.label)}${esc(where)}</option>`;
  }).join('');
  const ready = models.filter(usable);
  select.value = ready.some(m => m.id === wanted) ? wanted : ready[0]?.id || models[0]?.id || '';
  const model = models.find(m => m.id === select.value);
  const note = ui.bar.querySelector('.create-note');
  note.innerHTML = !info ? '<span class="faint">Chargement des modèles…</span>' : ready.length ? ''
    : `<span class="faint">Aucun modèle de ${k.noun}s prêt.</span>
       <button class="btn sm" data-go-images>${icon(k.icon)}Images et vidéos</button>`;
  note.querySelector('[data-go-images]')?.addEventListener('click', () => emit('view:show', 'images'));
  for (const name of ['format', 'frames', 'count']) field(name).hidden = !model || !ready.length;
  if (!model) return;
  // « Portrait » suffit ici ; les dimensions sont dans la galerie
  const sizes = model.sizes.map(([w, hgt, label]) => [`${w}x${hgt}`, label.split(' · ')[0]]);
  const format = field('format');
  const shape = prefs.get(`images.format.${model.id}`, '');
  format.innerHTML = sizes.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('');
  format.value = sizes.some(([v]) => v === shape) ? shape : sizes[0][0];
  const frames = field('frames');
  frames.hidden = mode !== 'video' || !ready.length;
  frames.innerHTML = (model.durations || []).map(([n, l]) => `<option value="${n}">${esc(l)}</option>`).join('');
  const frameCount = String(prefs.get(`images.frames.${model.id}`, model.default_frames || ''));
  frames.value = (model.durations || []).some(([n]) => String(n) === frameCount) ? frameCount
    : String(model.default_frames || model.durations?.[0]?.[0] || '');
  const count = field('count');
  const n = String(prefs.get(`create.count.${mode}`, 1));
  count.innerHTML = COUNTS.map(c => `<option value="${c}">${c} ${k.noun}${c > 1 ? 's' : ''}</option>`).join('');
  count.value = COUNTS.map(String).includes(n) ? n : '1';
  const seed = ui.bar.querySelector('.create-seed');
  seed.hidden = preset.seed == null;
  seed.innerHTML = preset.seed == null ? '' : `graine ${esc(preset.seed)} ${icon('x')}`;
}

/** La demande du mode « Créer », s'il est ouvert : la description est le message. */
export function creationRequest(text) {
  if (!mode) return null;
  const field = name => ui.bar.querySelector(`[data-c="${name}"]`);
  const [width, height] = (field('format').value || '').split('x').map(Number);
  const request = { kind: mode, model: field('model').value || null, prompt: text,
    width: width || null, height: height || null, count: Number(field('count').value) || 1,
    frames: mode === 'video' ? Number(field('frames').value) || null : null,
    seed: preset.seed ?? null };
  delete preset.seed;                   // la graine reprise ne sert qu'une fois
  fill();
  return request;
}

/** La demande reconnue dans un message (preflight) : le modèle préféré de ce genre, son
 *  format habituel si la phrase n'en dit rien. */
export function autoRequest(found) {
  if (found.followup) {           // retouche ou variante : les réglages de la dernière création
    return { kind: found.kind, followup: found.followup, count: found.count };
  }
  const model = prefs.get(`create.model.${found.kind}`, found.model) || found.model;
  const [width, height] = String(prefs.get(`images.format.${model}`, '')).split('x').map(Number);
  return { kind: found.kind, model, prompt: found.prompt, count: found.count, shape: found.shape,
    seconds: found.seconds, width: width || null, height: height || null,
    frames: found.kind === 'video' && !found.seconds ? prefs.get(`images.frames.${model}`, null) : null };
}

// ------------------------------------------------------------ la réponse de l'assistant
/** Oublie les créations suivies (autre conversation). */
export function resetMedia() {
  live.clear();
}

/** Une réponse qui ne vient pas d'un modèle (« aucun modèle d'images prêt »…), non gardée.
 *  friends : amis qui pourraient calculer, et retry() pour recommencer une fois autorisés. */
export function appendNotice(list, message, { images = false, friends = [], retry = null } = {}) {
  list.querySelector('.welcome')?.remove();
  const node = h(`
    <div class="msg assistant notice">
      <img class="avatar" src="/logo.png?v=serpents" alt="">
      <div class="body"><div class="banner warn">${icon('warn')}<div>${esc(message)}</div>
        ${friends.length && retry ? `<button class="btn sm" data-friends-ok>${icon('network')}Autoriser mes amis</button>` : ''}
        ${images ? `<button class="btn sm ghost" data-go-images>${icon('image')}Images et vidéos</button>` : ''}</div></div>
    </div>`);
  node.querySelector('[data-go-images]')?.addEventListener('click', () => emit('view:show', 'images'));
  node.querySelector('[data-friends-ok]')?.addEventListener('click', async () => {
    try { state.settings = await put('/settings', { images_from_friends: true }); }
    catch (err) { errorToast(err); return; }
    node.remove();
    retry();
  });
  list.append(node);
}

/** En attendant la réponse du nœud (une retouche réécrit d'abord la description). */
export function appendWaiting(list, label = '') {
  list.querySelector('.welcome')?.remove();
  const node = h(`
    <div class="msg assistant waiting">
      <img class="avatar" src="/logo.png?v=serpents" alt="">
      <div class="body"><div class="typing"><i></i><i></i><i></i></div>
        ${label ? `<span class="wait-label faint">${esc(label)}</span>` : ''}</div>
    </div>`);
  list.append(node);
  return () => node.remove();
}

/** La réponse qui montre une création (media : chat/creations.py, view). */
export function appendMedia(list, media) {
  list.querySelector('.welcome')?.remove();
  const orientation = media.width > media.height ? 'wide' : media.width < media.height ? 'tall' : 'square';
  const node = h(`
    <div class="msg assistant creation">
      <img class="avatar" src="/logo.png?v=serpents" alt="">
      <div class="body">
        <div class="creation-grid ${orientation}${media.count > 1 ? ' several' : ''}"></div>
        <div class="creation-state"></div>
        <div class="meta">
          <span class="model-tag"></span>
          <span class="actions">
            <button class="icon-btn" data-act="reuse" title="Reprendre cette description">${icon('retry')}</button>
            <button class="icon-btn" data-act="copy-prompt" title="Copier la description">${icon('copy')}</button>
          </span>
        </div>
      </div>
    </div>`);
  list.append(node);
  const view = { media, node, images: media.images || [], job: media.job || null };
  if (view.job) state.jobs.set(view.job.id, { ...view.job, ...state.jobs.get(view.job.id) });
  live.set(media.batch, view);
  node.querySelector('[data-act="reuse"]').onclick = () => setMode(media.kind, {
    model: media.model, prompt: media.prompt, width: media.width, height: media.height,
    frames: media.frames });
  node.querySelector('[data-act="copy-prompt"]').onclick = () => navigator.clipboard
    .writeText(media.prompt).then(() => toast('Description copiée.', 'ok', 1800));
  paintGrid(view);
  paintState(view);
  return view;
}

const active = view => !!view.job && ACTIVE.includes(view.job.status);

function paintGrid(view) {
  const { media, images } = view;
  const grid = view.node.querySelector('.creation-grid');
  const waiting = active(view) ? Math.max(0, media.count - images.length) : 0;
  const ratio = `aspect-ratio:${media.width}/${media.height}`;
  grid.innerHTML = images.map(img => `
      <button class="creation-tile" data-id="${img.id}" title="${esc(img.prompt)}" style="${ratio}">
        ${isVideo(img)
          ? `<video muted loop autoplay playsinline preload="metadata" src="${fileOf(img)}"></video>
             <span class="img-tag">vidéo ${length(img)}</span>`
          : `<img loading="lazy" src="${fileOf(img)}" alt="${esc(img.prompt)}">`}
      </button>`).join('')
    + `<div class="creation-slot" style="${ratio}">${icon(KINDS[media.kind]?.icon || 'image')}</div>`.repeat(waiting);
  grid.classList.toggle('n1', images.length + waiting === 1);
  grid.querySelectorAll('.creation-tile').forEach(tile => {
    tile.onclick = () => openMedia(images.find(i => i.id === Number(tile.dataset.id)), {
      onReuse: img => setMode(media.kind, { model: img.model, prompt: img.prompt, width: img.width,
        height: img.height, frames: img.frames, seed: img.seed }),
      onDeleted: img => { view.images = view.images.filter(i => i.id !== img.id); paintGrid(view); },
    });
  });
  const machines = [...new Set(images.map(i => i.machine || 'cette machine'))];
  const seconds = images.length ? Math.round(images.reduce((n, i) => n + (i.seconds || 0), 0) / images.length) : 0;
  view.node.querySelector('.model-tag').textContent = [media.label, machines.join(', '),
    seconds ? `${seconds} s${images.length > 1 ? ' par ' + KINDS[media.kind].noun : ''}` : '']
    .filter(Boolean).join(' · ');
  onGrow();
}

function paintState(view) {
  const { media, images } = view;
  const job = view.job ? state.jobs.get(view.job.id) || view.job : null;
  const box = view.node.querySelector('.creation-state');
  const noun = KINDS[media.kind]?.noun || 'image';
  let text = '';
  let kind = 'faint';
  if (job && ACTIVE.includes(job.status)) {
    const names = new Map((state.network?.peers || []).map(p => [p.node_id, p.name]));
    const away = Object.keys(job.remote || {}).map(id => names.get(id) || 'une autre machine');
    const doing = job.status === 'paused' ? 'En pause'
      : away.length ? `Calcul chez ${away.join(', ')}`
      : job.status === 'running' ? 'Création en cours' : 'En attente de son tour';
    const count = media.count > 1 ? ` · ${images.length} sur ${media.count}` : '';
    const since = job.created ? ` · ${duration(Math.max(0, Date.now() / 1000 - job.created))}` : '';
    // D'après les précédentes, de ce modèle à cette taille (la première est plus longue)
    const usual = media.expected_s && job.status !== 'paused'
      ? ` · d'habitude ${duration(media.expected_s)}${media.count > 1 ? ` par ${noun}` : ''}` : '';
    text = `${doing}${count}${since}${usual}`;
    box.innerHTML = `<span class="typing"><i></i><i></i><i></i></span><span class="faint">${esc(text)}</span>
      <button class="icon-btn" data-stop title="Arrêter">${icon('stop')}</button>`;
    box.querySelector('[data-stop]').onclick = async () => {
      try { await post(`/jobs/${job.id}/cancel`); } catch (err) { errorToast(err); }
    };
    return;
  }
  if (job?.status === 'cancelled') {
    text = images.length ? `Arrêtée : ${images.length} sur ${media.count}.` : 'Création arrêtée.';
  } else if (job && (job.status === 'failed' || job.failed)) {
    const failed = job.failed || media.count - images.length;
    text = `${failed} ${noun}${failed > 1 ? 's' : ''} sur ${media.count} n'${failed > 1 ? 'ont' : 'a'} pas pu être `
      + `créée${failed > 1 ? 's' : ''}${job.error ? ` : ${job.error}` : ' (le détail est dans Tâches)'}.`;
    kind = 'err-text';
  } else if (!job && images.length < media.count && !images.length) {
    text = 'Création interrompue, ou ses fichiers ont été supprimés.';
  }
  box.innerHTML = text ? `<span class="${kind}">${esc(text)}</span>` : '';
}

/** Les travaux avancent (événements du nœud) : état, et nouvelles images à montrer. */
function follow() {
  for (const view of live.values()) {
    if (!view.job) continue;
    const job = state.jobs.get(view.job.id);
    if (!job) continue;
    const before = view.job;
    view.job = job;
    if (job.done !== before.done || job.status !== before.status) reload(view);
    paintState(view);
  }
}

async function reload(view) {
  let rows;
  try { rows = await get(`/images?batch=${encodeURIComponent(view.media.batch)}`); } catch { return; }
  if (live.get(view.media.batch) !== view) return;          // autre conversation entre-temps
  const changed = rows.length !== view.images.length || rows.some((r, i) => r.id !== view.images[i]?.id);
  view.images = rows;
  if (changed || !active(view)) paintGrid(view);
  paintState(view);
}
