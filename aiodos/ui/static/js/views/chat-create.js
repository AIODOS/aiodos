// Créer une image ou une vidéo dans la conversation, comme dans les autres assistants
// (chat/creations.py). Le bouton à côté du message ouvre le mode « Créer » : modèle, format,
// durée, nombre ; sans lui, une demande écrite (« Crée une image d'un phare… ») est reconnue
// avant l'envoi. La réponse de l'assistant montre les images à mesure qu'elles arrivent,
// calculées ici ou par tes machines qui ont le modèle ; sinon, une image ou une vidéo peut être
// créée par une machine du réseau public, avec l'accord demandé une fois, le même pour les deux
// (chat/public_creations.py). Les recettes de ComfyUI et mes workflows prêts ici ou chez mes
// machines s'ajoutent aux modèles (GET /chat/create/comfyui, chat/comfy_creations.py).
import { get, post, put } from '../api.js';
import { h, esc, duration } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { byPower } from './models.js';
import { state, on, emit, prefs } from '../state.js';
import { menu, toast, errorToast } from '../ui.js';
import { openMedia, fileOf, thumbOf, isVideo, length } from './media-viewer.js';
import { mineOnly } from './chat-public.js';

const ACTIVE = ['queued', 'running', 'paused'];
const COUNTS = [1, 2, 4, 8];
// Les textes de chaque genre : chat_create.kind_image, create_image, placeholder_image,
// no_model_image, count_image… (et _video)
const KINDS = { image: { icon: 'image' }, video: { icon: 'video' } };
const kindOf = media => (KINDS[media.kind] ? media.kind : 'image');

let info = null;              // modèles d'images et de vidéos (GET /images/models)
let comfy = [];               // recettes de ComfyUI et mes workflows prêts (GET /chat/create/comfyui)
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
    button: h(`<button class="icon-btn create-btn" title="${esc(t('chat_create.button'))}">${icon('image')}</button>`),
    bar: h(`<div class="create-bar" hidden>
      <button class="create-kind" data-c="close" title="${esc(t('chat_create.close'))}"></button>
      <select class="select" data-c="model" title="${esc(t('chat_create.model'))}"></select>
      <select class="select" data-c="format" title="${esc(t('chat_create.format'))}"></select>
      <select class="select" data-c="frames" title="${esc(t('chat_create.duration'))}" hidden></select>
      <select class="select" data-c="count" title="${esc(t('chat_create.count'))}"></select>
      <span class="chip on create-seed" hidden title="${esc(t('chat_create.seed_title'))}"></span>
      <span class="create-note"></span>
    </div>`),
  };
  box.prepend(ui.button);
  box.before(ui.bar);
  ui.button.onclick = () => menu(ui.button, Object.entries(KINDS).map(([kind, k]) => ({
    label: t(`chat_create.create_${kind}`), icon: k.icon, action: () => setMode(kind) })));
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
  on('chat:mine-only', () => { if (mode) fill(); });   // le réseau public, ou non (viaPublic)
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
  ui.input.placeholder = kind ? t(`chat_create.placeholder_${kind}`) : ui.placeholder;
  if (ui.hint) ui.hint.textContent = kind ? t('chat_create.hint') : ui.hintText;
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
  // ComfyUI fermé ou absent : seulement les modèles d'AIODOS
  const recipes = get('/chat/create/comfyui').then(r => r.models || [], () => []);
  try { info = await get('/images/models'); } catch (err) { errorToast(err); return; }
  comfy = await recipes;
  if (mode) fill();
}

// Une recette de ComfyUI : prête ici (« here ») ou chez mes machines (helpers)
const here = m => (m.engine === 'comfyui' ? m.here : m.state === 'ready' && info?.engine.found)
  || m.helpers.length > 0;
// Ni ici ni chez mes machines : une machine du réseau public le prête, et la conversation peut
// partir chez un inconnu (le nœud redit tout à l'envoi, avec la carte d'accord s'il le faut)
const viaPublic = m => !here(m) && !!m.public && !mineOnly();
const usable = m => here(m) || viaPublic(m);

/** Réglages → Modèles : les modèles d'images et de vidéos s'y téléchargent. */
function showModels() {
  emit('view:show', 'settings');
  emit('settings:focus', 'modeles');
}

/** Les listes de la barre, selon le genre et le modèle choisis (règles du catalogue). */
function fill() {
  const field = name => ui.bar.querySelector(`[data-c="${name}"]`);
  const k = KINDS[mode];
  ui.bar.querySelector('.create-kind').innerHTML = `${icon(k.icon)}${esc(t(`chat_create.kind_${mode}`))}${icon('x')}`;
  const models = [...(info?.models || []).filter(m => (m.kind || 'image') === mode).sort(byPower),
    ...comfy.filter(m => m.kind === mode)];
  const select = field('model');
  const wanted = prefs.get(`create.model.${mode}`, '') || select.value;
  select.innerHTML = models.map(m => {
    const text = !usable(m) ? t('chat_create.model_to_install', { label: m.label })
      : viaPublic(m) ? t('public_images.model_public', { label: m.label })
      : m.engine === 'comfyui' ? (m.here ? m.label : t('chat_create.model_on', { label: m.label, names: m.helpers.join(', ') }))
      : m.state === 'ready' && info.engine.found ? m.label
      : t('chat_create.model_on', { label: m.label, names: m.helpers.join(', ') });
    return `<option value="${esc(m.id)}"${usable(m) ? '' : ' disabled'}>${esc(text)}</option>`;
  }).join('');
  const ready = models.filter(usable);
  select.value = ready.some(m => m.id === wanted) ? wanted : ready[0]?.id || models[0]?.id || '';
  const model = models.find(m => m.id === select.value);
  const note = ui.bar.querySelector('.create-note');
  note.innerHTML = !info ? `<span class="faint">${esc(t('chat.loading_models'))}</span>` : ready.length ? ''
    : `<span class="faint">${esc(t(`chat_create.no_model_${mode}`))}</span>
       <button class="btn sm" data-go-models>${icon('download')}${esc(t('chat_create.choose_model'))}</button>`;
  note.querySelector('[data-go-models]')?.addEventListener('click', showModels);
  for (const name of ['format', 'frames', 'count']) field(name).hidden = !model || !ready.length;
  if (!model) return;
  field('format').hidden ||= !model.sizes.length;     // une recette sans largeur ni hauteur
  // « Portrait » suffit ici ; les dimensions sont dans la galerie
  const sizes = model.sizes.map(([w, hgt, label]) => [`${w}x${hgt}`, label.split(' · ')[0]]);
  const format = field('format');
  const shape = prefs.get(`images.format.${model.id}`, '');
  format.innerHTML = sizes.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('');
  format.value = sizes.some(([v]) => v === shape) ? shape : sizes[0]?.[0] || '';
  const frames = field('frames');
  frames.hidden = mode !== 'video' || !ready.length || !(model.durations || []).length;
  frames.innerHTML = (model.durations || []).map(([n, l]) => `<option value="${n}">${esc(l)}</option>`).join('');
  const frameCount = String(prefs.get(`images.frames.${model.id}`, model.default_frames || ''));
  frames.value = (model.durations || []).some(([n]) => String(n) === frameCount) ? frameCount
    : String(model.default_frames || model.durations?.[0]?.[0] || '');
  const count = field('count');
  const n = String(prefs.get(`create.count.${mode}`, 1));
  count.innerHTML = COUNTS.map(c =>
    `<option value="${c}">${esc(t(`chat_create.count_${mode}`, { n: c }))}</option>`).join('');
  count.value = COUNTS.map(String).includes(n) ? n : '1';
  const seed = ui.bar.querySelector('.create-seed');
  seed.hidden = preset.seed == null;
  seed.innerHTML = preset.seed == null ? '' : `${esc(t('chat_create.seed', { seed: preset.seed }))} ${icon('x')}`;
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
 *  friends : amis qui pourraient calculer, et retry() pour recommencer une fois autorisés ;
 *  publicAsk : une machine du réseau public le pourrait, l'accord pas encore demandé (la carte :
 *  Oui, gardé, et retry() ; Non, gardé). */
export function appendNotice(list, message, { images = false, friends = [], retry = null,
  asText = null, publicAsk = false } = {}) {
  list.querySelector('.welcome')?.remove();
  if (publicAsk && retry) return appendPublicAsk(list, message, retry, asText);
  const node = h(`
    <div class="msg assistant notice">
      <img class="avatar" src="/logo.png?v=serpents" alt="">
      <div class="body"><div class="banner warn">${icon('warn')}<div>${esc(message)}</div>
        ${friends.length && retry ? `<button class="btn sm" data-friends-ok>${icon('network')}${esc(t('chat_create.allow_friends'))}</button>` : ''}
        ${images ? `<button class="btn sm ghost" data-go-models>${icon('download')}${esc(t('chat_create.choose_model'))}</button>` : ''}
        ${asText ? `<button class="btn sm ghost" data-as-text>${icon('chat')}${esc(t('chat_create.as_text'))}</button>` : ''}</div></div>
    </div>`);
  node.querySelector('[data-go-models]')?.addEventListener('click', showModels);
  node.querySelector('[data-as-text]')?.addEventListener('click', () => asText());
  const friendsOk = node.querySelector('[data-friends-ok]');
  friendsOk?.addEventListener('click', async () => {
    friendsOk.disabled = true;                     // cliqué deux fois : une seule fois
    try { state.settings = await put('/settings', { images_from_friends: true }); }
    catch (err) { errorToast(err); friendsOk.disabled = false; return; }
    node.hidden = true;
    if (await retry() === false) {                // une autre création en route : plus tard
      node.hidden = false;
      friendsOk.disabled = false;
      toast(t('chat_create.busy'));
      return;
    }
    node.remove();
  });
  list.append(node);
  return node;
}

/** La carte d'accord des images par le réseau public, dans la conversation : Oui (gardé dans
 *  les Réglages, puis la création recommence : retry) ou Non (gardé ; la carte dit où le
 *  changer). Reconnue au vol, la demande peut aussi partir en texte (asText). */
function appendPublicAsk(list, message, retry, asText) {
  const node = h(`
    <div class="msg assistant notice side-note public-card">
      <img class="avatar" src="/logo.png?v=serpents" alt="">
      <div class="body"><div class="banner info wrap">${icon('globe')}
        <div class="banner-text"><b>${esc(message)}</b><small>${esc(t('public_images.ask_note'))}</small></div>
        <div class="banner-actions">
          <button class="btn sm primary" type="button" data-answer="yes">${esc(t('chat.public_yes'))}</button>
          <button class="btn sm" type="button" data-answer="no">${esc(t('chat.public_no'))}</button>
          ${asText ? `<button class="btn sm ghost" type="button" data-as-text>${icon('chat')}${esc(t('chat_create.as_text'))}</button>` : ''}</div>
      </div></div>
    </div>`);
  node.querySelector('[data-as-text]')?.addEventListener('click', () => asText());
  node.querySelectorAll('[data-answer]').forEach(button => {
    button.onclick = async () => {
      const yes = button.dataset.answer === 'yes';
      const buttons = node.querySelectorAll('button');
      buttons.forEach(b => { b.disabled = true; });          // cliqué deux fois : une seule fois
      try { state.settings = await put('/settings', { public_images: yes }); }
      catch (err) { errorToast(err); buttons.forEach(b => { b.disabled = false; }); return; }
      emit('settings:loaded');
      if (!yes) {
        node.classList.add('answered');
        node.querySelector('.banner').innerHTML = `${icon('check')}<div class="banner-text">${esc(t('public_images.saved_no'))}</div>`;
        return;
      }
      node.hidden = true;
      if (await retry() === false) {                       // une autre création en route : plus tard
        node.hidden = false;
        buttons.forEach(b => { b.disabled = false; });
        toast(t('chat_create.busy'));
        return;
      }
      node.remove();
      toast(t('public_images.saved_yes'), 'ok', 5000);
    };
  });
  list.append(node);
  return node;
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

/** La réponse qui montre une création (media : chat/creations.py, view ; messageId : son
 *  message, pour « Répondre en texte » quand elle a été reconnue au vol). */
export function appendMedia(list, media, messageId = null) {
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
          ${media.auto && messageId ? `<button class="link-btn as-text" data-act="as-text"
            title="${esc(t('chat_create.as_text_title'))}">${esc(t('chat_create.as_text'))}</button>` : ''}
          <span class="actions">
            <button class="icon-btn" data-act="reuse" title="${esc(t('chat_create.reuse'))}">${icon('retry')}</button>
            <button class="icon-btn" data-act="copy-prompt" title="${esc(t('chat_create.copy_prompt'))}">${icon('copy')}</button>
          </span>
        </div>
      </div>
    </div>`);
  list.append(node);
  const view = { media, node, images: media.images || [], job: sameJob(media.job, media) ? media.job : null };
  if (view.job) {                 // l'état le plus récent l'emporte, s'il est connu
    const known = state.jobs.get(view.job.id);
    state.jobs.set(view.job.id, sameJob(known, media) ? { ...view.job, ...known } : view.job);
  }
  live.set(media.batch, view);
  node.querySelector('[data-act="reuse"]').onclick = () => setMode(media.kind, {
    model: media.model, prompt: media.prompt, width: media.width, height: media.height,
    frames: media.frames });
  node.querySelector('[data-act="copy-prompt"]').onclick = () => navigator.clipboard
    .writeText(media.prompt).then(() => toast(t('chat_create.prompt_copied'), 'ok', 1800));
  node.querySelector('[data-act="as-text"]')?.addEventListener('click', () => {
    live.delete(media.batch);
    emit('chat:as-text', { messageId, node });
  });
  paintGrid(view);
  paintState(view);
  return view;
}

/** Ce travail est-il celui de cette création ? Les numéros des travaux retirés de la liste
 *  sont repris par les suivants (le serveur fait la même vérification). */
const sameJob = (job, media) => !!job && (job.kind === 'comfy_create'
  ? job.params?.creation?.batch === media.batch
  : job.kind === 'generate_images' && job.params?.batch === media.batch);

const active = view => !!view.job && ACTIVE.includes(view.job.status);

// Les vidéos de la conversation ne tournent que visibles : au téléphone, plusieurs vidéos
// ne se téléchargent ni ne se décodent toutes à la fois
const onScreen = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
  for (const { target, isIntersecting } of entries) {
    if (isIntersecting) target.play().catch(() => {});
    else target.pause();
  }
}, { threshold: 0.4 }) : null;

function watchVideos(grid, on) {
  grid.querySelectorAll('video').forEach(video => {
    if (!onScreen) { if (on) video.play().catch(() => {}); return; }
    if (on) onScreen.observe(video); else onScreen.unobserve(video);
  });
}

function paintGrid(view) {
  const { media, images } = view;
  const grid = view.node.querySelector('.creation-grid');
  const waiting = active(view) ? Math.max(0, media.count - images.length) : 0;
  const ratio = `aspect-ratio:${media.width}/${media.height}`;
  watchVideos(grid, false);
  grid.innerHTML = images.map(img => `
      <button class="creation-tile" data-id="${img.id}" title="${esc(img.prompt)}" style="${ratio}">
        ${isVideo(img)
          ? `<video muted loop playsinline preload="metadata" src="${fileOf(img)}"></video>
             <span class="img-tag">${esc(t('chat_create.video_tag', { length: length(img) }))}</span>`
          : `<img loading="lazy" src="${thumbOf(img)}" alt="${esc(img.prompt)}">`}
      </button>`).join('')
    + `<div class="creation-slot" style="${ratio}">${icon(KINDS[media.kind]?.icon || 'image')}</div>`.repeat(waiting);
  grid.classList.toggle('n1', images.length + waiting === 1);
  watchVideos(grid, true);
  grid.querySelectorAll('.creation-tile').forEach(tile => {
    tile.onclick = () => openMedia(images.find(i => i.id === Number(tile.dataset.id)), {
      onReuse: img => setMode(media.kind, { model: img.model, prompt: img.prompt, width: img.width,
        height: img.height, frames: img.frames, seed: img.seed }),
      onDeleted: img => { view.images = view.images.filter(i => i.id !== img.id); paintGrid(view); },
    });
  });
  const machines = [...new Set(images.map(i => i.machine || t('chat.this_machine')))];
  const seconds = images.length ? Math.round(images.reduce((n, i) => n + (i.seconds || 0), 0) / images.length) : 0;
  view.node.querySelector('.model-tag').textContent = [media.label,
    media.retouch ? t('chat_create.retouch') : '', machines.join(', '),
    !seconds ? '' : images.length > 1 ? t(`chat_create.seconds_each_${kindOf(media)}`, { seconds })
      : t('format.seconds', { n: seconds })]
    .filter(Boolean).join(' · ');
  onGrow();
}

function paintState(view) {
  const { media, images } = view;
  const job = view.job ? state.jobs.get(view.job.id) || view.job : null;
  const box = view.node.querySelector('.creation-state');
  const what = kindOf(media);
  let text = '';
  let kind = 'faint';
  if (job && sameJob(job, media) && ACTIVE.includes(job.status)) {
    const names = new Map((state.network?.peers || []).map(p => [p.node_id, p.name]));
    const away = Object.keys(job.remote || {}).map(id => names.get(id) || t('jobs.another_machine'));
    const doing = job.status === 'paused' ? t('chat_create.paused')
      : media.public && job.status === 'running' ? t('public_images.computing')
      : away.length ? t('chat_create.computing_on', { names: away.join(', ') })
      : t(job.status === 'running' ? 'chat_create.running' : 'chat_create.queued');
    const count = media.count > 1 ? t('chat_create.progress', { done: images.length, n: media.count }) : '';
    const since = job.created ? duration(Math.max(0, Date.now() / 1000 - job.created)) : '';
    // D'après les précédentes, de ce modèle à cette taille (la première est plus longue)
    const usual = !media.expected_s || job.status === 'paused' ? ''
      : t(media.count > 1 ? `chat_create.usual_each_${what}` : 'chat_create.usual',
        { time: duration(media.expected_s) });
    // Construit une fois, puis seul le texte change : un clic sur « Arrêter » n'est pas
    // perdu quand l'état se redessine (chaque seconde)
    if (!box.querySelector('[data-stop]')) {
      box.innerHTML = `<span class="typing"><i></i><i></i><i></i></span><span class="faint state-text"></span>
        <button class="icon-btn" data-stop title="${esc(t('chat_create.stop'))}">${icon('stop')}</button>`;
      box.querySelector('[data-stop]').onclick = async () => {
        try { await post(`/jobs/${job.id}/cancel`); } catch (err) { errorToast(err); }
      };
    }
    box.querySelector('.state-text').textContent = [doing, count, since, usual].filter(Boolean).join(' · ');
    return;
  }
  if (job?.status === 'cancelled') {
    text = images.length ? t('chat_create.stopped_partial', { done: images.length, n: media.count })
      : t('chat_create.stopped');
  } else if (job && (job.status === 'failed' || job.failed)) {
    const failed = job.failed || media.count - images.length;
    text = job.error
      ? t(`chat_create.failed_${what}`, { n: failed, total: media.count, error: job.error })
      : t(`chat_create.failed_${what}_tasks`, { n: failed, total: media.count });
    kind = 'err-text';
  } else if (!job && images.length < media.count && !images.length) {
    text = t('chat_create.interrupted');
  }
  box.innerHTML = text ? `<span class="${kind}">${esc(text)}</span>` : '';
}

/** Les travaux avancent (événements du nœud) : état, et nouvelles images à montrer. */
function follow() {
  for (const view of live.values()) {
    if (!view.job) continue;
    const job = state.jobs.get(view.job.id);
    if (!sameJob(job, view.media)) continue;          // numéro repris par un autre travail
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
