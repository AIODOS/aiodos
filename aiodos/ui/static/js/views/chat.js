// Conversation : choix du modèle, messages en streaming, validations. Les autorisations
// (bibliothèques, web…) sont dans chat-permissions.js.
import { get, post, patch, streamChat, openExternal, inNativeWindow } from '../api.js';
import { $, h, esc, duration, linkAction, linkKind } from '../dom.js';
import { t, tt } from '../i18n.js';
import { icon } from '../icons.js';
import { state, emit, on, prefs, session } from '../state.js';
import { modal, ask, toast, errorToast } from '../ui.js';
import { AUTO, BEST_AUTO, PUBLIC_AUTO, asDefault, catalog, defaultChoice, modelLabel, modelName,
  publicChoice, unverifiedTag } from './models.js';
import { mountSidebar, refresh as refreshTree } from './sidebar.js';
import { chooseModel, progressBar, cancelInstall, finished, isPhone, integrated } from './model-install.js';
import { mountAttach, pendingItems, attaching, clearAttachments, attachmentsHtml } from './chat-attach.js';
import { mountCreate, creationRequest, autoRequest, appendMedia, appendNotice, appendWaiting,
  resetMedia, setMode, createKind, createChoice, createLabel, pickCreate } from './chat-create.js';
import { isAudioKind } from './chat-audio.js';
import { mountPermissions } from './chat-permissions.js';
import { isPublic, mineOnly, showNotice } from './chat-public.js';
import { menuData, modelMenu } from './model-menu.js';

let el = {};
let busy = false;
let creating = false;      // une création d'image en route vers le nœud
let sending = false;       // un envoi en préparation (preflight, validations) : pas un second
let displayed = 0;         // change à chaque conversation affichée (créations en route)
let controller = null;
let entries = [];          // modèles proposés, une fois chacun (cf. models.js)
// Le modèle de conversation choisi (« auto:best », « auto:<modèle> », « peer:… », « reparti:… ») ;
// la machine d'un modèle se choisit à côté (#machine-select)
let textChoice = '';
let menu = null;           // le bouton « Modèle » et son panneau (model-menu.js)
let editing = null;        // { node } : le dernier message de l'utilisateur, en cours de modification
let opening = 0;           // ouvertures de conversation : seule la dernière s'affiche
let installing = null;     // installation d'un modèle en cours (avancement)
let filledMine = null;     // « Sur mes machines seulement » quand le choix du modèle a été fait
let publicSettings = null; // réglages du réseau public au dernier chargement des réglages
let searchAgain = null;    // la première minute : les modèles relus tant qu'on cherche le réseau public
const SEARCH_AGAIN_MS = 5000;

export function mountChat(container) {
  mountSidebar(container);
  const chat = h(`
    <div class="chat">
      <header class="chat-head">
        <button class="icon-btn chat-list-toggle" id="chat-list-toggle" title="${esc(t('chat.list_toggle'))}">${icon('menu')}</button>
        <button class="icon-btn chat-new-btn" id="chat-new-btn" title="${esc(t('chat.new_conversation'))}">${icon('plus')}</button>
        <div class="chat-title" id="chat-title" title="${esc(t('chat.rename_hint'))}">${esc(t('chat.new_conversation'))}</div>
      </header>
      <div id="chat-banner"></div>
      <div id="cluster-prep" hidden></div>
      <div class="messages" id="messages"><div class="messages-inner" id="messages-inner"></div></div>
      <footer class="composer">
        <div class="edit-bar" id="edit-bar" hidden>${icon('edit')}<span>${esc(t('chat.edit_bar'))}</span>
          <button class="link-btn" id="edit-cancel" type="button">${esc(t('ui.cancel'))}</button></div>
        <div class="composer-box">
          <textarea id="composer" rows="1" placeholder="${esc(t('chat.placeholder'))}"></textarea>
          <button class="send" id="send" title="${esc(t('chat.send_title'))}">${icon('send')}</button>
        </div>
        <div class="composer-tools">
          <span class="grow"></span>
          <select class="select" id="machine-select" hidden
            title="${esc(t('chat.machine_select'))}"></select>
        </div>
        <div class="hint">${esc(t('chat.hint'))}</div>
      </footer>
    </div>`);
  container.append(chat);
  el = {
    chat, title: $('#chat-title'), machine: $('#machine-select'),
    banner: $('#chat-banner'), editBar: $('#edit-bar'),
    scroll: $('#messages'), list: $('#messages-inner'), input: $('#composer'), send: $('#send'),
  };
  $('#edit-cancel').onclick = () => stopEditing();

  // La liste des conversations, avec ☰ ou « Discussions » touché à nouveau dans la barre du
  // bas : sur un écran étroit (téléphone), elle s'ouvre par-dessus ; sur un ordinateur, elle se
  // replie pour laisser toute la place à la conversation (« + » en démarre alors une autre).
  // À l'ouverture d'AIODOS, elle est fermée, sur l'ordinateur comme sur le téléphone
  const phone = matchMedia('(max-width: 760px)');
  const showList = shown => {
    if (container.classList.contains('show-list') === shown) return;
    container.classList.toggle('show-list', shown);
    emit('chat:list-shown', shown);
  };
  const collapse = folded => container.classList.toggle('list-collapsed', folded);
  collapse(true);
  const toggleList = () => (phone.matches ? showList(!container.classList.contains('show-list'))
    : collapse(!container.classList.contains('list-collapsed')));
  $('#chat-list-toggle').onclick = toggleList;
  $('#chat-new-btn').onclick = () => emit('chat:new');
  on('chat:list', shown => showList(shown));
  on('chat:fold', () => (phone.matches ? showList(false) : collapse(true)));   // bouton de la liste
  on('rail:again', view => { if (view === 'chat') toggleList(); });
  on('chat:open', () => showList(false));
  on('chat:new', () => showList(false));
  el.scroll.addEventListener('click', () => showList(false));
  on('cluster:changed', showPreparation);
  el.input.addEventListener('input', autoresize);
  el.input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    // Flèche haut dans une zone vide : modifier son dernier message
    if (e.key === 'ArrowUp' && !el.input.value && !editing) {
      const last = el.list.querySelector('.msg.user.last-turn');
      if (last) { e.preventDefault(); startEditing(last); }
    }
  });
  document.addEventListener('keydown', e => {
    // Échap déjà pris par une fenêtre de dialogue (validation d'une recherche web…) : rien de plus
    if (e.key !== 'Escape' || e.defaultPrevented) return;
    if (busy) stop();
    else if (editing) stopEditing();
  });
  el.send.onclick = () => (busy ? stop() : send());
  el.title.onclick = renameCurrent;
  el.list.addEventListener('click', onMessageClick);
  el.list.addEventListener('auxclick', onReplyLink);   // clic du milieu : sans passer par click
  mountCreate(chat.querySelector('.composer'), el.input, () => scrollBottom());
  mountAttach(chat, el.input);              // trombone, fichier glissé, image collée
  mountPermissions(chat.querySelector('.composer-tools'));   // sous la saisie, avant le modèle
  // Le modèle : le texte de la conversation, ou celui du mode « Créer » ouvert (chat-create.js)
  // L'audio : un onglet, trois genres (voix, musique, effets sonores), chacun son choix
  const audioNow = () => (isAudioKind(createKind()) ? createKind() : 'voice');
  menu = modelMenu({
    kindNow: () => (isAudioKind(createKind()) ? 'audio' : createKind() || 'text'),
    chosen: (kind, genre) => (kind === 'text' ? (textChoice && textChoice !== BEST_AUTO ? textChoice : null)
      : kind === 'audio' ? createChoice(genre || audioNow()) : createChoice(kind)),
    genre: audioNow,
    pick: pickModel,
    mineOnly,
    name: (kind, id) => (kind === 'text' ? modelName(id) : createLabel(id)),
    // Texte : la machine choisie à côté ne change pas le modèle ; un gros modèle réparti est
    // un autre choix que le même modèle sur une seule machine
    same: (kind, a, b) => a === b || (kind === 'text' && !/^reparti:/.test(a) && !/^reparti:/.test(b)
      && modelName(a) === modelName(b)),
  });
  el.machine.before(menu.root);
  on('create:changed', () => menu.refresh());

  on('chat:new', newConversation);
  on('chat:open', openConversation);
  on('chat:as-text', answerAsText);
  on('chat:renamed', title => setTitle(title));
  on('models:loaded', fillModels);
  on('model-install', ev => {            // avancement d'une installation (model-install.js)
    installing = ev;
    if (finished(ev, () => emit('models:reload'))) installing = null;
    if (state.modelsLoaded) fillModels();
  });
  on('node:loaded', () => { if (state.modelsLoaded) fillModels(); });   // libellés VRAM
  // Sans modèle ici : le réseau public se propose selon la conversation (« Sur mes machines
  // seulement », chat-public.js) et les Réglages (use_public, public_network)
  const mineChanged = () => { if (state.modelsLoaded && mineOnly() !== filledMine) fillModels(); };
  on('chat:current', mineChanged);
  on('chat:mine-only', mineChanged);
  on('settings:loaded', () => {
    const now = `${!!state.settings?.use_public}:${!!state.settings?.public_network}`;
    if (publicSettings !== null && now !== publicSettings && state.modelsLoaded) emit('models:reload');
    publicSettings = now;
  });
  showWelcome();
}

// ------------------------------------------- modèle et machine (sous la saisie)
function vramTotal() {
  return Math.max(0, ...(state.node?.gpus || []).map(g => g.vram_total));
}

const isSplit = id => /^reparti:/.test(id || '');

/** Modèle choisi : « Automatique » (« auto:best » : le modèle et la machine se choisissent
 *  seuls), « auto:… » (la machine se choisit seule), un modèle d'une machine précise, un gros
 *  modèle réparti ; '' tant que les modèles ne sont pas lus. */
function selectedModel() {
  if (!textChoice || textChoice === BEST_AUTO || isSplit(textChoice)) return textChoice;
  const entry = entries.find(e => e.name === modelName(textChoice));
  // Un modèle du réseau public seulement (le menu le propose) : « auto:<modèle> » tel quel
  return entry ? (el.machine.value || defaultChoice(entry)) : textChoice;
}

/** Le modèle d'une nouvelle conversation : celui des Réglages (« Automatique » par défaut). */
const defaultText = () => asDefault(state.settings?.default_model) || BEST_AUTO;

function fillModels() {
  const wanted = selectedModel() || state.currentConversation?.model || defaultText();
  entries = catalog();
  const local = state.models.filter(m => m.group === 'local');
  const remote = state.models.filter(m => m.group === 'remote');
  const cluster = state.models.filter(m => m.group === 'cluster');
  // Rien ici ni sur mes machines : le réseau public, si cette conversation peut y partir
  filledMine = mineOnly();
  const open = entries.length || cluster.length ? null : publicChoice(filledMine);
  selectModel(wanted);
  // Sans modèle ici : le dire (« ce téléphone » ou « ce PC »), et proposer d'en installer un ;
  // le réseau public, s'il peut répondre, qu'on cherche encore ses machines (la première
  // minute : relu dans quelques secondes), ou qu'aucune ne le peut pour l'instant
  const searching = !!(open && !open.ready && open.searching);
  clearTimeout(searchAgain);
  if (searching) searchAgain = setTimeout(() => emit('models:reload'), SEARCH_AGAIN_MS);
  const phone = isPhone();
  const none = open ? (phone ? 'chat.no_public_phone' : 'chat.no_public_pc')
    : integrated() ? (phone ? 'chat.no_model_phone' : 'chat.no_model_pc')
    : (phone ? 'chat.no_model_phone_ollama' : 'chat.no_model_pc_ollama');
  el.banner.innerHTML = progressBar(installing) + (local.length ? '' : remote.length ? `
    <div class="banner info">${icon('network')}<div style="flex:1">${esc(t(phone
      ? 'chat.no_model_remote_phone' : 'chat.no_model_remote_pc'))}</div>
    <button class="btn sm" data-install>${icon('plus')}${esc(t('chat.install_here'))}</button></div>` : open?.ready ? `
    <div class="banner info wrap">${icon('globe')}<div class="banner-text">${esc(t(phone
      ? 'chat.no_model_public_phone' : 'chat.no_model_public_pc'))}</div>
    <div class="banner-actions"><button class="btn sm" data-install>${icon('plus')}${esc(t('chat.install'))}</button></div></div>` : searching ? `
    <div class="banner info wrap">${icon('globe')}<div class="banner-text">${esc(t(phone
      ? 'chat.no_model_searching_phone' : 'chat.no_model_searching_pc'))}</div>
    <div class="banner-actions"><button class="btn sm" data-install>${icon('plus')}${esc(t('chat.install'))}</button></div></div>` : `
    <div class="banner warn wrap">${icon('warn')}<div class="banner-text">${esc(t(none))}</div>
    <div class="banner-actions"><button class="btn sm primary" data-install>${icon('plus')}${esc(t('chat.install'))}</button>
    <button class="btn sm" id="models-retry">${icon('retry')}${esc(t('chat.retry'))}</button></div></div>`);
  el.banner.querySelector('#models-retry')?.addEventListener('click', () => emit('models:reload'));
  el.banner.querySelector('[data-install]')?.addEventListener('click', chooseModel);
  el.banner.querySelector('[data-install-cancel]')?.addEventListener('click', cancelInstall);
}

/** Un modèle que l'on peut encore choisir : « Automatique », un modèle d'ici ou de mes
 *  machines, un gros modèle réparti, ou un modèle que le menu propose (réseau public). */
function known(id) {
  if (!id || id === BEST_AUTO) return !!id;
  if (isSplit(id)) return state.models.some(m => m.id === id);
  if (entries.some(e => e.name === modelName(id))) return true;
  return !!menuData(mineOnly())?.text?.models?.some(m => m.ready && m.id === id);
}

/** Sélectionne un modèle d'après son identifiant (« auto:best », « auto:x », « peer:…:x »,
 *  « x », « reparti:x ») ; un modèle qui n'est plus là, ou « Automatique · réseau public »
 *  (le plus puissant du réseau public : ce que fait « Automatique » sans modèle à moi),
 *  laisse la place à « Automatique ». */
function selectModel(id) {
  // Avant la lecture des modèles (une conversation rouverte au démarrage) : gardé tel quel
  textChoice = known(id) || (id && !state.modelsLoaded) ? id : BEST_AUTO;
  fillMachines(id);
  menu?.refresh();
}

/** Machines du modèle choisi : « Automatique », puis chacune pour la désigner (cachées
 *  quand une seule machine a ce modèle, ou pour « Automatique » qui choisit aussi le modèle). */
function fillMachines(wanted) {
  const entry = textChoice === BEST_AUTO || isSplit(textChoice) ? null
    : entries.find(e => e.name === modelName(textChoice));
  el.machine.innerHTML = '';
  if (!entry) { el.machine.hidden = true; return; }
  const vram = vramTotal();
  if (entry.auto) el.machine.append(new Option(t('chat.automatic'), entry.auto));
  for (const m of entry.machines) {
    const slow = m.local ? vram && m.size > vram * 0.92 : m.slow;
    const text = slow ? t(m.local ? 'chat.slow_here' : 'chat.slow_there', { label: m.label }) : m.label;
    el.machine.append(new Option(text, m.id));
  }
  el.machine.hidden = entry.machines.length < 2;
  const values = [...el.machine.options].map(o => o.value);
  el.machine.value = values.includes(wanted) ? wanted : defaultChoice(entry);
}

/** Un choix du menu des modèles : un modèle de conversation (le mode « Créer » se ferme), ou
 *  un modèle d'images, de vidéos ou d'audio (le mode « Créer » de ce genre s'ouvre,
 *  chat-create.js ; l'audio : le genre de la partie de l'onglet, model.genre). Il vaut pour
 *  cette conversation ; le choix par défaut se règle dans Réglages → Modèles. */
function pickModel(kind, id, model) {
  if (kind === 'audio') { pickCreate(model?.genre || 'voice', id); return; }
  if (kind !== 'text') { pickCreate(kind, id); return; }
  if (createKind()) setMode(null);
  textChoice = id || BEST_AUTO;
  fillMachines(null);                             // la machine se choisit seule par défaut
  prepareCluster();
  el.input.focus();
}

/** Modèle réparti choisi : le groupe se forme et le modèle se charge pendant qu'on écrit
 *  (cluster/group.py, prepare). Une question envoyée avant la fin reprend où il en est. */
async function prepareCluster() {
  const model = selectedModel();
  if (!model.startsWith('reparti:')) { showPreparation(null); return; }
  try { showPreparation({ model, ...await post('/cluster/prepare', { model }) }); }
  catch (err) { showPreparation({ model, state: 'error', error: err.message }); }
}

function showPreparation(ev) {
  const box = $('#cluster-prep');
  if (!ev || ev.model !== selectedModel() || ev.state === 'none') { box.hidden = true; return; }
  box.hidden = false;
  box.dataset.state = ev.state;
  box.className = `banner ${ev.state === 'error' ? 'warn' : 'info'}`;
  box.innerHTML = ev.state === 'ready'
    ? `${icon('check')}<div>${esc(t('chat.cluster_ready'))}</div>`
    : ev.state === 'error' ? `${icon('warn')}<div>${esc(t('chat.cluster_failed', { error: tt(ev.error_t, ev.error || '') }))}</div>`
    : `${icon('network')}<div>${esc(t('chat.cluster_preparing', { status: tt(ev.status_t, ev.status || '') || t('chat.cluster_cards') }))}</div>`;
  // Toujours « prêt » six secondes plus tard (pas une autre préparation entre-temps) : rangé
  if (ev.state === 'ready') setTimeout(() => { if (box.dataset.state === 'ready') box.hidden = true; }, 6000);
}

function setTitle(title) {
  el.title.textContent = title || t('chat.new_conversation');
  document.title = title ? `${title} — AIODOS` : 'AIODOS';
}

async function renameCurrent() {
  const conv = state.currentConversation;
  if (!conv) return;
  const title = await ask(t('chat.rename_title'), t('chat.rename_label'), conv.title);
  if (!title || title === conv.title) return;
  try {
    await patch(`/conversations/${conv.id}`, { title });
    conv.title = title;
    setTitle(title);
  } catch (e) { errorToast(e); }
}

// --------------------------------------------------------- conversation courante
function newConversation() {
  if (busy) stop();
  stopEditing();
  displayed += 1;
  state.currentConversation = null;
  prefs.set('lastConversation', null);
  setTitle(null);
  if (state.modelsLoaded) selectModel(defaultText());    // le modèle des Réglages : « Automatique »
  resetMedia();
  showWelcome();
  emit('chat:current');
  el.input.focus();
}

/** settled : l'écran se resynchronise (remplacement interrompu ou refusé) ; le nœud
 *  répond une fois enregistrée la question en cours. Seule la dernière ouverture compte :
 *  une réponse en retard ne ramène pas une conversation quittée entre-temps. */
async function openConversation(id, { settled = false } = {}) {
  if (busy) {
    toast(t('chat.answer_running'));
    return;
  }
  const mine = ++opening;
  let conv;
  try { conv = await get(`/conversations/${id}${settled ? '?settled=1' : ''}`); } catch (e) {
    if (mine !== opening) return;
    if (e.status === 404) { newConversation(); return; }
    errorToast(e); return;
  }
  if (mine !== opening || busy) return;
  stopEditing();
  displayed += 1;
  state.currentConversation = { id: conv.id, title: conv.title, model: conv.model,
    mine_only: !!conv.mine_only };
  prefs.set('lastConversation', conv.id);
  setTitle(conv.title);
  el.list.innerHTML = '';
  resetMedia();
  const auto = !!conv.model?.startsWith(AUTO);   // les réponses disent quelle machine a répondu
  for (const m of conv.messages) {
    if (m.role === 'user') { appendUser(m.content, m.id, m.attachments); continue; }
    if (m.media) { appendMedia(el.list, m.media, m.id); continue; }    // image ou vidéo créée
    const view = appendAssistant(m.model, auto);
    view.node.dataset.id = m.id;
    view.unverified(!!m.unverified);   // « non vérifiable pour l'instant », gardé avec la réponse
    if (m.thinking) view.restoreThinking(m.thinking);
    view.sources(m.sources);
    view.done(m.html);
  }
  markTurn();
  if (conv.model) selectModel(conv.model);
  emit('chat:current');
  scrollBottom(true);
  el.input.focus();
}

function showWelcome() {
  el.list.innerHTML = '';
  const welcome = h(`
    <div class="welcome">
      <img src="/logo.png?v=serpents" alt="">
      <h1>${esc(t('chat.welcome_title'))}</h1>
      <p>${esc(t('chat.welcome_text'))}</p>
    </div>`);
  el.list.append(welcome);
}

// ------------------------------------------------------------------- messages
function appendUser(text, id = null, attached = []) {
  el.list.querySelector('.welcome')?.remove();
  // Les pièces jointes à côté de la bulle, jamais dedans : son texte est celui du message
  const bubble = `<div class="bubble">${esc(text)}</div>`;
  const node = h(`<div class="msg user">
      <div class="turn-actions turn-only"><button class="icon-btn" data-act="edit" title="${esc(t('chat.edit_message'))}">${icon('edit')}</button></div>
      ${attached?.length ? `<div class="user-col">${bubble}${attachmentsHtml(attached)}</div>` : bubble}</div>`);
  if (id) node.dataset.id = id;
  el.list.append(node);
  return node;
}

/** « Modifier » sur le dernier message de l'utilisateur, « Régénérer » sur la réponse qui le
 *  suit, si c'est une réponse écrite (une création a « Encore une » et ses retouches). */
function markTurn() {
  el.list.querySelectorAll('.last-turn').forEach(n => n.classList.remove('last-turn'));
  const msgs = [...el.list.children].filter(n => n.classList.contains('msg'));
  const asked = msgs.filter(n => n.classList.contains('user')).pop();
  if (!asked?.dataset.id) return;
  asked.classList.add('last-turn');
  // Une carte ou une note du réseau public (chat-public.js) n'est pas une réponse
  const after = msgs.slice(msgs.indexOf(asked) + 1).filter(n => !n.classList.contains('side-note'));
  // Une réponse écrite, même arrêtée (son numéro n'est pas connu ici : seul compte celui
  // de la question) ; une création a « Encore une » et ses retouches
  if (after.length === 1 && after[0].classList.contains('assistant')
      && !after[0].matches('.creation, .notice, .waiting')) after[0].classList.add('last-turn');
}

/** Ce qu'un envoi remplace (« Régénérer », « Modifier ») : la question `node` et la suite.
 *  Le nœud ne les retire qu'une fois la nouvelle demande acceptée (replace_from), et si
 *  c'est bien ce texte-là (replace_text). */
const replacing = (node, text) => ({ node, id: Number(node.dataset.id), text });

/** La demande acceptée : l'échange remplacé quitte l'écran (jusqu'à la nouvelle question). */
function dropReplaced(replace, newest) {
  if (!replace?.node?.isConnected) return;
  let node = replace.node;
  while (node && node !== newest) {
    const next = node.nextElementSibling;
    node.remove();
    node = next;
  }
}

function regenerate(answer) {
  if (busy || creating || sending) return;
  stopEditing();
  let asked = answer.previousElementSibling;
  while (asked && !asked.classList.contains('user')) asked = asked.previousElementSibling;
  if (!asked?.dataset.id) return;
  const text = asked.querySelector('.bubble').textContent;
  send(text, replacing(asked, text));       // tel quel, avec le modèle choisi maintenant
}

function startEditing(node) {
  if (busy || creating || sending || !node?.dataset.id) return;
  beginEditing(node);
}

function beginEditing(node) {
  stopEditing();
  const text = node.querySelector('.bubble').textContent;
  editing = { node, text };
  node.classList.add('editing');
  el.input.value = text;
  autoresize();
  el.input.focus();
  el.editBar.hidden = false;
}

/** Un envoi qui n'a pas été accepté (refus, Stop, réseau) ne perd pas le texte écrit : il
 *  revient dans la zone de saisie, et « Modifier » reprend s'il s'agissait d'une
 *  modification et que le message est toujours là. */
function keepEdit(text, replace, convId) {
  if (state.currentConversation?.id !== convId) return;     // une autre conversation entre-temps
  const last = el.list.querySelector('.msg.user.last-turn');
  if (last?.querySelector('.bubble').textContent === text) return;   // passé, finalement
  if (replace && last && Number(last.dataset.id) === replace.id
      && last.querySelector('.bubble').textContent === replace.text) beginEditing(last);
  if (!el.input.value.trim() || editing) {
    el.input.value = text;
    autoresize();
  }
}

function stopEditing(clear = true) {
  if (!editing) return;
  editing.node.classList.remove('editing');
  editing = null;
  el.editBar.hidden = true;
  if (clear) { el.input.value = ''; autoresize(); }
}

function appendAssistant(model, here = false) {
  el.list.querySelector('.welcome')?.remove();
  const label = modelLabel(model, here);
  const node = h(`
    <div class="msg assistant">
      <img class="avatar" src="/logo.png?v=serpents" alt="">
      <div class="body">
        <details class="thinking" hidden>
          <summary>${icon('think')}<span class="label">${esc(t('chat.thinking'))}</span><span class="faint time"></span></summary>
          <div class="thinking-body"></div>
        </details>
        <div class="md content"><div class="typing"><i></i><i></i><i></i></div></div>
        <div class="meta">
          <span class="model-tag${isPublic(model) ? ' public' : ''}">${esc(label)}</span>${unverifiedTag()}
          <span class="sources"></span>
          <span class="actions"><button class="icon-btn" data-act="copy" title="${esc(t('chat.copy_answer'))}">${icon('copy')}</button><button class="icon-btn turn-only" data-act="regenerate" title="${esc(t('chat.regenerate'))}">${icon('retry')}</button></span>
        </div>
      </div>
    </div>`);
  el.list.append(node);
  const thinking = node.querySelector('.thinking');
  const thinkingBody = node.querySelector('.thinking-body');
  const content = node.querySelector('.content');
  let thinkStart = 0;
  let thinkTimer = null;
  let answered = false;
  let statusText = '';                 // modèle réparti : où en est sa mise en place

  // Premier message : charger le modèle en VRAM peut prendre 20-30 s. On le dit.
  const waitStart = Date.now();
  const waitTimer = setInterval(() => {
    const seconds = (Date.now() - waitStart) / 1000;
    const dots = content.querySelector('.typing');
    if (seconds < 3 || !dots) return;
    let label = content.querySelector('.wait-label');
    if (!label) { label = h('<span class="wait-label faint"></span>'); content.append(label); }
    label.textContent = statusText || t('chat.model_waking', { time: duration(seconds) });
  }, 1000);
  const stopWaiting = () => {
    clearInterval(waitTimer);
    content.querySelector('.wait-label')?.remove();
  };
  const stopThinking = () => {
    stopWaiting();
    thinking.classList.remove('live');
    clearInterval(thinkTimer);
    thinkTimer = null;
  };
  // Une machine du réseau public passée devant par la règle des petits réseaux (placement.py)
  const unverified = on => { node.querySelector('.unverified-tag').hidden = !on; };
  return {
    node,
    unverified,
    route(ev) {                       // mode automatique : la machine choisie, ou qui prend le relais
      unverified(!!ev.unverified);    // celle qui prend le relais ne l'est peut-être pas
      if (!model?.startsWith(AUTO)) return;
      const tag = node.querySelector('.model-tag');
      // Une machine du réseau public : « Répondu par qwen3.5:9b, sur une machine du réseau public »
      tag.textContent = isPublic(ev.model) ? modelLabel(ev.model)
        : `${modelName(ev.model)} · ${ev.machine || t('chat.this_machine')}`;
      tag.classList.toggle('public', isPublic(ev.model));
    },
    status(text) {                    // mise en place d'un modèle réparti (cluster/group.py)
      statusText = text;
      if (!content.querySelector('.typing')) return;
      let label = content.querySelector('.wait-label');
      if (!label) { label = h('<span class="wait-label faint"></span>'); content.append(label); }
      label.textContent = text;
      scrollBottom();
    },
    think(text) {
      stopWaiting();
      if (!thinkStart) {
        thinkStart = Date.now();
        thinking.hidden = false;
        thinking.classList.add('live');
        thinkTimer = setInterval(() => {
          thinking.querySelector('.time').textContent = `· ${duration((Date.now() - thinkStart) / 1000)}`;
        }, 500);
      }
      thinkingBody.textContent += text;
      if (thinking.open) thinkingBody.scrollTop = thinkingBody.scrollHeight;
      scrollBottom();
    },
    restoreThinking(text) {
      thinking.hidden = false;
      thinkingBody.textContent = text;
    },
    render(html) {
      if (!answered) { answered = true; stopThinking(); }
      content.innerHTML = html;
      scrollBottom();
    },
    sources(list) {
      const box = node.querySelector('.sources');
      box.innerHTML = '';
      for (const s of list || []) {
        // Marques internes (« unverified » : montrée à côté du modèle, unverifiedTag)
        if (['summary', 'inherited', 'error', 'unverified'].includes(s.type)) continue;
        if (s.type === 'web' && linkKind(s.url) === 'web') {
          // Un nouvel onglet hors de la fenêtre native : l'interface reste où elle est
          box.append(h(`<a class="source" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer"
            title="${esc(s.title || s.url)}">${icon('globe')}${esc(s.label)}</a>`));
        } else if (s.type === 'web') {
          // Ni http ni https (javascript:, search-ms:…) : du texte, jamais un lien, comme
          // render.py le fait dans les réponses ; un changement futur de linkAction ne
          // rouvrirait pas la brèche
          box.append(h(`<span class="source link-off" title="${esc(t('render.link_off', { url: s.url }))}">`
            + `${icon('globe')}${esc(s.label || s.url)}</span>`));
        } else if (s.type === 'remote') {         // trouvé dans la bibliothèque d'une autre machine
          const where = { library: s.collection, peer: s.peer, label: s.label };
          box.append(h(`<span class="source remote" title="${esc(t('chat.remote_source_title', where))}">`
            + `${icon('network')}${esc(t('chat.remote_source', where))}</span>`));
        } else {
          const ico = s.type === 'conversation' ? 'memory' : 'file';
          box.append(h(`<span class="source" title="${esc(s.collection || '')}">${icon(ico)}${esc(s.label)}</span>`));
        }
      }
    },
    done(html, error) {
      stopThinking();
      content.innerHTML = html || (error ? '' : `<p class="faint">${esc(t('chat.empty_answer'))}</p>`);
      if (error) content.append(h(`<p style="color:var(--err)">⚠ ${esc(error)}</p>`));
      scrollBottom();
    },
    stopped() {
      stopThinking();
      content.querySelector('.typing')?.remove();
      content.append(h(`<p class="faint">${esc(t('chat.interrupted'))}</p>`));
    },
    error(message) {
      stopThinking();
      content.innerHTML = `<p style="color:var(--err)">⚠ ${esc(message)}</p>`;
    },
  };
}

function onMessageClick(e) {
  const copyCode = e.target.closest('.code-copy');
  if (copyCode) {
    const code = copyCode.closest('.code-block').querySelector('pre').innerText;
    navigator.clipboard.writeText(code).then(() => {
      copyCode.textContent = t('chat.code_copied');
      setTimeout(() => { copyCode.textContent = t('render.copy'); }, 1400);   // celui de render.py
    });
    return;
  }
  const copyAll = e.target.closest('[data-act="copy"]');
  if (copyAll) {
    const text = copyAll.closest('.body').querySelector('.content').innerText;
    navigator.clipboard.writeText(text).then(() => toast(t('chat.answer_copied'), 'ok', 1800));
    return;
  }
  const again = e.target.closest('[data-act="regenerate"]');
  if (again) { regenerate(again.closest('.msg')); return; }
  // « Non vérifiable pour l'instant » : pourquoi, aussi sur un écran tactile (sans infobulle)
  const mark = e.target.closest('.unverified-tag');
  if (mark) { toast(mark.title, 'info', 8000); return; }
  const change = e.target.closest('[data-act="edit"]');
  if (change) { startEditing(change.closest('.msg')); return; }
  onReplyLink(e);
}

// Un lien d'une réponse : une page web s'ouvre dans un navigateur, un courriel dans la
// messagerie ; tout autre lien (search-ms:, ms-word:…) ne s'ouvre pas (dom.js, linkAction)
function onReplyLink(e) {
  const link = e.target.closest('a[href]');
  if (!link) return;
  const action = linkAction(link.getAttribute('href'),
    { button: e.button, nativeWindow: inNativeWindow() });
  if (action === 'follow') return;
  e.preventDefault();
  if (action === 'external') openExternal(link.href).catch(errorToast);
}

function scrollBottom(force = false) {
  const s = el.scroll;
  if (force || s.scrollHeight - s.scrollTop - s.clientHeight < 140) s.scrollTop = s.scrollHeight;
}

function autoresize() {
  el.input.style.height = 'auto';
  el.input.style.height = `${el.input.scrollHeight}px`;
}

function setBusy(on) {
  busy = on;
  el.chat.classList.toggle('busy', on);
  el.send.classList.toggle('stop', on);
  el.send.innerHTML = icon(on ? 'stop' : 'send');
  el.send.title = t(on ? 'chat.stop_title' : 'chat.send_title');
}

function stop() {
  controller?.abort();
}

// ------------------------------------------------------- envoi et validations
// « Web » activé : la recherche part quand la question semble la demander (preflight), après
// accord (ou pour la session) ; désactivé, elle est proposée pour ce message seulement
async function confirmOutgoing(text, model, pre) {
  const allowed = !!state.settings?.web_search_enabled;
  let web = false;
  if (!allowed && pre.web_intent) {
    const r = await modal({
      title: t('chat.web_off_title'), iconName: 'globe',
      text: t('chat.web_off_text'),
      quote: text.slice(0, 300), confirm: t('chat.web_search'), cancel: t('chat.answer_without'),
    });
    web = r.ok;
  } else if (allowed && pre.web_intent && session.webConsent) {
    web = true;
  } else if (allowed && pre.web_intent) {
    const r = await modal({
      title: t('chat.web_send_title'), iconName: 'globe',
      text: t('chat.web_send_text'),
      quote: text.slice(0, 300), confirm: t('chat.send'), cancel: t('chat.answer_without'),
      check: t('chat.ask_no_more_session'),
    });
    web = r.ok;
    if (r.ok && r.checked) session.webConsent = true;
  }

  const peers = await confirmPeers(text, pre);

  return { web, peers };
}

// « Chez les autres » : la question part chez les machines qui m'ouvrent leurs bibliothèques
// (preflight.consult) ; chez un ami, seulement après accord (network/remote_search.py).
// → identifiants des machines acceptées : le serveur ne cherche que chez elles.
async function confirmPeers(text, pre) {
  if (!state.settings?.peer_libraries_enabled || !pre.consult?.length) return [];
  if (!pre.consult_allowed) {
    toast(t('chat.peers_refused'), 'info', 7000);
    return [];
  }
  const ids = pre.consult.map(m => m.node_id);
  const unasked = pre.consult.filter(m => m.friend && !session.peerConsent.has(m.node_id));
  if (!unasked.length) return ids;
  const names = pre.consult.map(m => t('chat.quoted', { name: m.name })).join(', ');
  const r = await modal({
    title: t('chat.peers_title'), iconName: 'network',
    text: t('chat.peers_text', { names }),
    quote: text.slice(0, 300), confirm: t('chat.peers_confirm'), cancel: t('chat.answer_without'),
    check: t('chat.ask_no_more_friends'),
  });
  if (r.ok && r.checked) pre.consult.forEach(m => session.peerConsent.add(m.node_id));
  return r.ok ? ids : [];
}

/** asText : un message envoyé au modèle de conversation tel quel, sans chercher d'image à
 *  créer (« Répondre en texte » sur une création qui n'en était pas une, « Régénérer »).
 *  replace : l'échange qu'il remplace (« Régénérer » ; « Modifier » passe par editing).
 *  → faux si rien n'est parti (validation refusée, aucun modèle…). */
async function send(asText = null, replace = null) {
  if (busy || creating || sending) return false;
  sending = true;              // un double clic, ou Entrée pendant les validations : un seul envoi
  try {
    return await sendNow(asText, replace);
  } finally {
    sending = false;
  }
}

async function sendNow(asText, replace) {
  if (asText === null && attaching()) {
    toast(t('chat.attachment_sending'));
    return false;
  }
  const typed = asText ?? el.input.value.trim();
  const joined = asText === null ? pendingItems() : [];
  const attached = joined.map(a => a.id);
  // Une pièce jointe seule : une question par défaut, dans la langue de l'interface
  const text = typed || (attached.length ? t('chat.attachment_question') : '');
  if (!text) return false;
  if (editing && !editing.node.isConnected) stopEditing(false);   // retiré entre-temps
  if (editing && asText === null) replace = replacing(editing.node, editing.text);
  const swap = replace ? { replace_from: replace.id, replace_text: replace.text } : {};
  const explicit = asText === null && creationRequest(text);     // mode « Créer » ouvert
  if (explicit && !typed) {
    toast(t('chat.attachment_describe'));
    return false;
  }
  if (explicit) {
    await create(text, { ...explicit, ...swap, attachments: attached }, false, replace, null, joined);
    return true;
  }
  const model = selectedModel();

  let pre;
  try {
    pre = await post('/chat/preflight', {
      text, model, conversation_id: state.currentConversation?.id ?? null,
      replace_from: replace?.id ?? null });
  } catch (e) { errorToast(e); return false; }
  if (pre.create && asText === null) {             // « Crée une image de… », reconnu au vol
    await create(text, { ...autoRequest(pre.create), auto: true, ...swap, attachments: attached },
      false, replace, null, joined);
    return true;
  }
  // « Automatique » sans aucun modèle ici ni sur mes machines : le réseau public seul, s'il
  // peut répondre (le nœud prend alors le meilleur de ses modèles, network/placement.py)
  const alone = model === BEST_AUTO && !entries.length && !state.models.some(m => m.group === 'cluster');
  if (!model || !state.modelsLoaded || (alone && !publicChoice(mineOnly())?.ready)) {
    if (!state.modelsLoaded) toast(t('chat.models_loading'));
    else if (publicChoice(mineOnly())?.searching) toast(t('chat.public_searching'));   // la première minute
    else if (publicChoice(mineOnly())) toast(t('placement.no_public'), 'err');   // aucune ne prête
    else if (integrated()) toast(t('chat.no_model_install'), 'err');
    else toast(t('chat.no_model_ollama'), 'err');
    return false;
  }
  // Une pièce jointe ne part jamais chez un inconnu, et rien ici ne la lirait : le message
  // reste dans la zone de saisie (le nœud le refuserait de même, chat/public_consent.py)
  if ((model === PUBLIC_AUTO || alone) && attached.length) {
    toast(t('public_chat.closed_attachments'), 'err', 8000);
    return false;
  }
  const go = await confirmOutgoing(text, model, pre);
  if (!go) return false;

  if (asText === null) {
    el.input.value = '';
    autoresize();
  }
  stopEditing(false);
  const asked = appendUser(text, null, joined);
  const view = appendAssistant(model);
  scrollBottom(true);
  setBusy(true);
  controller = new AbortController();
  let started = false;
  let failure = null;
  try {
    const mine = mineOnly();       // « Sur mes machines seulement » (chat-public.js)
    await streamChat({
      text, model, web: go.web, peer_libraries: go.peers, attachments: attached,
      conversation_id: state.currentConversation?.id ?? null, mine_only: mine, ...swap,
    }, ev => {
      if (ev.type === 'start') {
        started = true;
        if (attached.length) clearAttachments();   // le message les a emportées
        dropReplaced(replace, asked);              // acceptée : l'ancien échange s'en va
        asked.dataset.id = ev.user_message_id;
        if (state.currentConversation?.id !== ev.conversation_id) {
          state.currentConversation = { id: ev.conversation_id, title: ev.title || t('chat.new_conversation'),
            model, mine_only: mine };
          prefs.set('lastConversation', ev.conversation_id);
        }
        if (ev.title) { state.currentConversation.title = ev.title; setTitle(ev.title); }
        emit('chat:current');
      } else if (ev.type === 'route') view.route(ev);
      else if (ev.type === 'notice') showNotice(ev, view.node);   // réseau public : carte, note
      else if (ev.type === 'sources') view.sources(ev.sources);
      else if (ev.type === 'status') view.status(ev.text);
      else if (ev.type === 'thinking') view.think(ev.text);
      else if (ev.type === 'render') view.render(ev.html);
      else if (ev.type === 'done') {
        if (ev.message_id) view.node.dataset.id = ev.message_id;
        view.done(ev.html, ev.error);
      }
    }, controller.signal);
  } catch (e) {
    failure = e;
    if (e.name === 'AbortError') view.stopped();
    else view.error(e.message);
  } finally {
    setBusy(false);
    controller = null;
    markTurn();
    refreshTree();
    el.input.focus();
  }
  // Un remplacement sans « start » (Stop, refus, réseau coupé) : le nœud a peut-être retiré
  // l'ancien échange, ou pas ; la conversation est relue telle qu'il la garde
  if (replace && !started && state.currentConversation) {
    if (failure && failure.name !== 'AbortError') errorToast(failure);
    const id = state.currentConversation.id;
    await openConversation(id, { settled: true });
    if (asText === null) keepEdit(text, replace, id);
  }
  return true;
}

// Une image ou une vidéo à créer (chat-create.js) : le message et la réponse qui montrera les
// images s'ajoutent à la conversation, le calcul part dans la file. replace : l'échange
// remplacé (« Modifier »), retiré une fois la demande acceptée
async function create(text, request, again = false, replace = null, askedNode = null, joined = []) {
  if (creating) return false;        // une création déjà en route : pas une deuxième
  creating = true;
  const seen = displayed;            // la conversation affichée au départ
  let asked = askedNode;             // une nouvelle tentative garde la question d'origine
  if (!again) {                  // une nouvelle tentative (amis autorisés) ne répète pas le message
    el.input.value = '';
    autoresize();
    stopEditing(false);
    asked = appendUser(text, null, joined);
  }
  const done = appendWaiting(el.list, request.followup === 'edit' ? t('chat.retouching')
    : !request.followup && state.settings?.images_enrich ? t('chat.enriching') : '');
  scrollBottom(true);
  let made;
  try {
    made = await post('/chat/create', {
      ...request, text, conversation_id: state.currentConversation?.id ?? null,
      chat_model: selectedModel() || null,
      mine_only: mineOnly(),       // « Sur mes machines seulement » : le réseau public en dépend
    });
  } catch (e) {
    done();
    if (seen !== displayed) return;
    if (e.status === 412) {          // la conversation a changé (autre fenêtre) : relue
      errorToast(e);
      const id = state.currentConversation?.id;
      if (id) await openConversation(id, { settled: true });
      keepEdit(text, replace, id);
      return;
    }
    // 409 : aucun modèle ne peut la calculer ; des amis le pourraient peut-être, ou une
    // machine du réseau public, avec l'accord demandé une fois (public : « ask »). Reconnue
    // au vol, la demande n'en était peut-être pas une : « Répondre en texte » (si le
    // message ne part pas, il revient dans la zone de saisie)
    const notice = appendNotice(el.list, e.message, {
      images: e.status === 409, friends: e.data?.friends || [],
      publicAsk: e.status === 409 && e.data?.public === 'ask',
      retry: () => create(text, request, true, replace, asked),
      asText: request.auto ? async () => {
        asked?.remove();
        notice?.remove();
        if (!await send(text, replace) && !el.input.value.trim()) {
          el.input.value = text;
          autoresize();
        }
      } : null });
    markTurn();
    scrollBottom(true);
    return;
  } finally {
    creating = false;
  }
  done();
  if (request.attachments?.length) clearAttachments();   // la demande les a emportées
  if (seen !== displayed) {          // une autre conversation entre-temps : celle-ci reste à sa place
    refreshTree();
    toast(made.title ? t('chat.creation_started', { title: made.title })
      : t('chat.creation_started_untitled'), 'info', 5000);
    return;
  }
  if (state.currentConversation?.id !== made.conversation_id) {
    state.currentConversation = { id: made.conversation_id, title: made.title || t('chat.new_conversation'),
      model: selectedModel() };
    prefs.set('lastConversation', made.conversation_id);
  }
  if (made.title) { state.currentConversation.title = made.title; setTitle(made.title); }
  emit('chat:current');
  const userNode = asked || [...el.list.querySelectorAll('.msg.user')].pop();
  dropReplaced(replace, userNode);                // acceptée : l'ancien échange s'en va
  if (userNode && made.user_message_id) userNode.dataset.id = made.user_message_id;
  appendMedia(el.list, made.media, made.message_id);
  markTurn();
  scrollBottom(true);
  refreshTree();
  el.input.focus();
}

// « Répondre en texte » : la création reconnue au vol n'en était pas une. Elle quitte la
// conversation (avec le message, que le modèle de conversation reçoit à la place)
async function answerAsText({ messageId, node }) {
  if (busy || creating || sending) return;
  stopEditing();
  let undone;
  sending = true;                    // cliqué deux fois : une seule fois
  try { undone = await post(`/chat/create/${messageId}/undo`); } catch (e) { errorToast(e); return; }
  finally { sending = false; }
  const asked = node.previousElementSibling;
  if (asked?.classList.contains('user')) asked.remove();
  node.remove();
  markTurn();
  refreshTree();
  // Pas parti (validation refusée…) : le message revient dans la zone de saisie
  if (!await send(undone.text) && !el.input.value.trim()) {
    el.input.value = undone.text;
    autoresize();
  }
}
