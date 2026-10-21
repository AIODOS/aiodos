// Conversation : choix du modèle, options, messages en streaming, validations.
import { get, post, put, patch, streamChat, openExternal, inNativeWindow } from '../api.js';
import { $, h, esc, bytes, duration, linkAction, linkKind } from '../dom.js';
import { icon } from '../icons.js';
import { state, emit, on, prefs, session } from '../state.js';
import { modal, ask, toast, errorToast } from '../ui.js';
import { AUTO, asDefault, byPower, catalog, defaultChoice, modelLabel, modelName } from './models.js';
import { mountSidebar, refresh as refreshTree } from './sidebar.js';
import { chooseModel, progressBar, cancelInstall, finished, here, integrated } from './model-install.js';
import { mountAttach, pendingItems, attaching, clearAttachments, attachmentsHtml } from './chat-attach.js';
import { mountCreate, creationRequest, autoRequest, appendMedia, appendNotice, appendWaiting,
  resetMedia } from './chat-create.js';

const FLAGS = ['knowledge_enabled', 'conversations_enabled', 'web_search_enabled', 'think',
  'peer_libraries_enabled'];
let el = {};
let busy = false;
let creating = false;      // une création d'image en route vers le nœud
let sending = false;       // un envoi en préparation (preflight, validations) : pas un second
let displayed = 0;         // change à chaque conversation affichée (créations en route)
let controller = null;
let entries = [];          // modèles proposés, une fois chacun (cf. models.js)
let editing = null;        // { node } : le dernier message de l'utilisateur, en cours de modification
let opening = 0;           // ouvertures de conversation : seule la dernière s'affiche
let installing = null;     // installation d'un modèle en cours (avancement)

export function mountChat(container) {
  mountSidebar(container);
  const chat = h(`
    <div class="chat">
      <header class="chat-head">
        <button class="icon-btn chat-list-toggle" id="chat-list-toggle" title="Afficher ou masquer les conversations">${icon('menu')}</button>
        <button class="icon-btn chat-new-btn" id="chat-new-btn" title="Nouvelle conversation">${icon('plus')}</button>
        <div class="chat-title" id="chat-title" title="Cliquer pour renommer">Nouvelle conversation</div>
        <select class="select" id="model-select" title="Modèle utilisé pour répondre"></select>
        <select class="select" id="machine-select" hidden
          title="Machine qui répond. Automatique : cette machine si elle est libre, sinon la moins occupée de tes machines qui prêtent ce modèle."></select>
        <div class="chips">
          <button class="chip" data-flag="knowledge_enabled" title="Utiliser tes bibliothèques de connaissance">${icon('book')}Bibliothèques</button>
          <button class="chip" data-flag="peer_libraries_enabled" title="Chercher aussi dans les bibliothèques de mes machines et de mes amis (ta question part chez eux)">${icon('network')}Chez les autres</button>
          <button class="chip" data-flag="conversations_enabled" title="Se souvenir de tes autres conversations">${icon('memory')}Mémoire</button>
          <button class="chip" data-flag="web_search_enabled" title="Chercher sur le web quand ta question le demande (validation avant l'envoi, ou pour la session)">${icon('globe')}Web</button>
          <button class="chip" data-flag="think" title="Laisser le modèle réfléchir avant de répondre (plus lent, souvent meilleur)">${icon('think')}Réflexion</button>
        </div>
      </header>
      <div id="chat-banner"></div>
      <div id="cluster-prep" hidden></div>
      <div class="messages" id="messages"><div class="messages-inner" id="messages-inner"></div></div>
      <footer class="composer">
        <div class="edit-bar" id="edit-bar" hidden>${icon('edit')}<span>Modification de ton dernier
          message : l'envoyer remplace la suite de la conversation.</span>
          <button class="link-btn" id="edit-cancel" type="button">Annuler</button></div>
        <div class="composer-box">
          <textarea id="composer" rows="1" placeholder="Écris ton message…"></textarea>
          <button class="send" id="send" title="Envoyer (Entrée)">${icon('send')}</button>
        </div>
        <div class="hint">Entrée pour envoyer · Maj+Entrée pour aller à la ligne · Échap pour arrêter la réponse</div>
      </footer>
    </div>`);
  container.append(chat);
  el = {
    chat, title: $('#chat-title'), model: $('#model-select'), machine: $('#machine-select'),
    banner: $('#chat-banner'), editBar: $('#edit-bar'),
    scroll: $('#messages'), list: $('#messages-inner'), input: $('#composer'), send: $('#send'),
  };
  $('#edit-cancel').onclick = () => stopEditing();

  chat.querySelectorAll('.chip').forEach(chip => { chip.onclick = () => toggleFlag(chip.dataset.flag); });
  // La liste des conversations, avec ☰ ou « Discussions » touché à nouveau dans la barre du
  // bas : sur un écran étroit (téléphone), elle s'ouvre par-dessus ; sur un ordinateur, elle se
  // replie pour laisser toute la place à la conversation (« + » en démarre alors une autre)
  const phone = matchMedia('(max-width: 760px)');
  const showList = shown => {
    if (container.classList.contains('show-list') === shown) return;
    container.classList.toggle('show-list', shown);
    emit('chat:list-shown', shown);
  };
  const collapse = folded => {
    container.classList.toggle('list-collapsed', folded);
    prefs.set('chatListCollapsed', folded);
  };
  collapse(!!prefs.get('chatListCollapsed', false));
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
  el.model.onchange = onModelChange;
  on('cluster:changed', showPreparation);
  el.machine.onchange = rememberModel;
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

  on('chat:new', newConversation);
  on('chat:open', openConversation);
  on('chat:as-text', answerAsText);
  on('chat:renamed', title => setTitle(title));
  on('settings:loaded', syncChips);
  on('models:loaded', fillModels);
  on('model-install', ev => {            // avancement d'une installation (model-install.js)
    installing = ev;
    if (finished(ev, () => emit('models:reload'))) installing = null;
    if (state.modelsLoaded) fillModels();
  });
  on('node:loaded', () => { if (state.modelsLoaded) fillModels(); });   // libellés VRAM
  el.model.innerHTML = '<option value="">Chargement des modèles…</option>';
  showWelcome();
}

// ------------------------------------------------------------------ en-tête
function syncChips() {
  document.querySelectorAll('.chat-head .chip').forEach(chip => {
    chip.classList.toggle('on', !!state.settings?.[chip.dataset.flag]);
  });
}

async function toggleFlag(flag) {
  if (!state.settings || !FLAGS.includes(flag)) return;
  try {
    state.settings = await put('/settings', { [flag]: !state.settings[flag] });
    syncChips();
  } catch (e) { errorToast(e); }
}

function vramTotal() {
  return Math.max(0, ...(state.node?.gpus || []).map(g => g.vram_total));
}

/** Modèle choisi : « auto:… » (la machine se choisit seule), un modèle d'une machine
 *  précise, ou un gros modèle réparti. */
function selectedModel() {
  const value = el.model.value;
  if (/^reparti:/.test(value)) return value;
  const entry = entries.find(e => e.name === value);
  return entry ? (el.machine.value || defaultChoice(entry)) : '';
}

function fillModels() {
  const wanted = selectedModel() || state.currentConversation?.model
    || asDefault(state.settings?.default_model);
  entries = catalog();
  const local = state.models.filter(m => m.group === 'local');
  const remote = state.models.filter(m => m.group === 'remote');
  const cluster = state.models.filter(m => m.group === 'cluster').sort(byPower);
  el.model.innerHTML = '';
  const group = (label, items) => {
    if (!items.length) return;
    const g = document.createElement('optgroup');
    g.label = label;
    items.forEach(([value, text]) => g.append(new Option(text, value)));
    el.model.append(g);
  };
  // Chaque modèle une fois, même s'il est sur plusieurs machines : la machine se choisit à côté
  // Sans mode automatique (prêté seulement par un ami…), l'option dit qui le prête
  group(remote.length ? 'Modèles · la machine qui répond se choisit seule' : 'Sur cette machine',
    entries.map(e => [e.name, e.auto ? `${e.name} · ${bytes(e.size)}`
      : `${e.name} · ${e.machines.map(m => m.label).join(', ')} · ${bytes(e.size)}`]));
  group('Réparti entre mes machines · cartes graphiques réunies', cluster.map(m => [m.id,
    `${m.label} · ${bytes(m.size)} · avec ${m.machines.join(', ')}${m.complete ? '' : ' · en partie en mémoire vive'}`]));
  selectModel(wanted);
  // Sans modèle ici : le dire (« ce téléphone » ou « ce PC »), et proposer d'en installer un
  el.banner.innerHTML = progressBar(installing) + (local.length ? '' : remote.length ? `
    <div class="banner info">${icon('network')}<div style="flex:1">Aucun modèle sur ${here()} : tes réponses
    seront calculées par tes machines appairées qui prêtent les leurs.</div>
    <button class="btn sm" data-install>${icon('plus')}Installer un modèle ici</button></div>` : `
    <div class="banner warn wrap">${icon('warn')}<div class="banner-text">Aucun modèle sur ${here()} pour
    répondre${integrated() ? '' : ' (ou Ollama ne répond pas : AIODOS réessaie tout seul toutes les 8 secondes)'}.</div>
    <div class="banner-actions"><button class="btn sm primary" data-install>${icon('plus')}Installer un modèle</button>
    <button class="btn sm" id="models-retry">${icon('retry')}Réessayer</button></div></div>`);
  el.banner.querySelector('#models-retry')?.addEventListener('click', () => emit('models:reload'));
  el.banner.querySelector('[data-install]')?.addEventListener('click', chooseModel);
  el.banner.querySelector('[data-install-cancel]')?.addEventListener('click', cancelInstall);
}

/** Sélectionne un modèle d'après son identifiant (« auto:x », « peer:…:x », « x »,
 *  « reparti:x »). */
function selectModel(id) {
  const value = /^reparti:/.test(id || '') ? id : modelName(id);
  const values = [...el.model.options].map(o => o.value);
  // Sans choix connu : un modèle de cette machine d'abord, puis un de mes machines
  const here = entries.find(e => e.machines.some(m => m.local))?.name;
  const usable = values.filter(Boolean);
  if (!values.includes(value) && !here && !usable.length) {
    if (!values.includes('')) el.model.prepend(new Option('Aucun modèle prêt pour l’instant', ''));
    el.model.value = '';
  } else {
    el.model.value = values.includes(value) ? value : (here || usable[0] || values[0] || '');
  }
  fillMachines(id);
}

/** Machines du modèle choisi : « Automatique », puis chacune pour la désigner (cachées
 *  quand une seule machine a ce modèle). */
function fillMachines(wanted) {
  const entry = entries.find(e => e.name === el.model.value);
  el.machine.innerHTML = '';
  if (!entry) { el.machine.hidden = true; return; }
  const vram = vramTotal();
  if (entry.auto) el.machine.append(new Option('Automatique', entry.auto));
  for (const m of entry.machines) {
    const slow = m.local ? vram && m.size > vram * 0.92 : m.slow;
    const hint = slow ? (m.local ? ' · lent (dépasse la VRAM)' : ' · lent (dépasse sa VRAM)') : '';
    el.machine.append(new Option(`${m.label}${hint}`, m.id));
  }
  el.machine.hidden = entry.machines.length < 2;
  const values = [...el.machine.options].map(o => o.value);
  el.machine.value = values.includes(wanted) ? wanted : defaultChoice(entry);
}

function onModelChange() {
  fillMachines(null);                     // mode automatique par défaut
  rememberModel();
  prepareCluster();
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
  box.className = `banner ${ev.state === 'error' ? 'warn' : 'info'}`;
  box.innerHTML = ev.state === 'ready'
    ? `${icon('check')}<div>Modèle réparti prêt sur tes machines.</div>`
    : ev.state === 'error' ? `${icon('warn')}<div>Préparation impossible : ${esc(ev.error || '')}</div>`
    : `${icon('network')}<div>Préparation du modèle réparti pendant que tu écris : ${esc(ev.status || 'cartes demandées…')}</div>`;
  if (ev.state === 'ready') setTimeout(() => { if (box.innerHTML.includes('prêt')) box.hidden = true; }, 6000);
}

async function rememberModel() {
  const id = selectedModel();
  if (id) {
    try { state.settings = await put('/settings', { default_model: id }); } catch { /* sans gravité */ }
  }
}

function setTitle(title) {
  el.title.textContent = title || 'Nouvelle conversation';
  document.title = title ? `${title} — AIODOS` : 'AIODOS';
}

async function renameCurrent() {
  const conv = state.currentConversation;
  if (!conv) return;
  const title = await ask('Renommer la conversation', 'Nouveau titre :', conv.title);
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
    toast('Une réponse est en cours : arrête-la (Échap) avant de changer de conversation.');
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
  state.currentConversation = { id: conv.id, title: conv.title, model: conv.model };
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
      <h1>Que fait-on aujourd'hui&nbsp;?</h1>
      <p>Tout tourne sur tes machines. Le web ne sert qu'avec ton accord.</p>
    </div>`);
  el.list.append(welcome);
}

// ------------------------------------------------------------------- messages
function appendUser(text, id = null, attached = []) {
  el.list.querySelector('.welcome')?.remove();
  // Les pièces jointes à côté de la bulle, jamais dedans : son texte est celui du message
  const bubble = `<div class="bubble">${esc(text)}</div>`;
  const node = h(`<div class="msg user">
      <div class="turn-actions turn-only"><button class="icon-btn" data-act="edit" title="Modifier ce message">${icon('edit')}</button></div>
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
  const after = msgs.slice(msgs.indexOf(asked) + 1);
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
          <summary>${icon('think')}<span class="label">Réflexion</span><span class="faint time"></span></summary>
          <div class="thinking-body"></div>
        </details>
        <div class="md content"><div class="typing"><i></i><i></i><i></i></div></div>
        <div class="meta">
          <span class="model-tag">${esc(label)}</span>
          <span class="sources"></span>
          <span class="actions"><button class="icon-btn" data-act="copy" title="Copier la réponse">${icon('copy')}</button><button class="icon-btn turn-only" data-act="regenerate" title="Régénérer la réponse (avec le modèle choisi)">${icon('retry')}</button></span>
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
    label.textContent = statusText || `Le modèle se prépare… ${duration(seconds)}`;
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
  return {
    node,
    route(ev) {                       // mode automatique : la machine choisie, ou qui prend le relais
      if (!model?.startsWith(AUTO)) return;
      node.querySelector('.model-tag').textContent =
        `${modelName(ev.model)} · ${ev.machine || 'cette machine'}`;
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
        if (['summary', 'inherited', 'error'].includes(s.type)) continue;   // marques internes
        if (s.type === 'web' && linkKind(s.url) === 'web') {
          // Un nouvel onglet hors de la fenêtre native : l'interface reste où elle est
          box.append(h(`<a class="source" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer"
            title="${esc(s.title || s.url)}">${icon('globe')}${esc(s.label)}</a>`));
        } else if (s.type === 'web') {
          // Ni http ni https (javascript:, search-ms:…) : du texte, jamais un lien, comme
          // render.py le fait dans les réponses ; un changement futur de linkAction ne
          // rouvrirait pas la brèche
          box.append(h(`<span class="source link-off" title="Lien désactivé : ${esc(s.url)}">`
            + `${icon('globe')}${esc(s.label || s.url)}</span>`));
        } else if (s.type === 'remote') {         // trouvé dans la bibliothèque d'une autre machine
          box.append(h(`<span class="source remote" title="Bibliothèque « ${esc(s.collection)} », chez « ${esc(s.peer)} »">`
            + `${icon('network')}${esc(s.label)} · chez ${esc(s.peer)}</span>`));
        } else {
          const ico = s.type === 'conversation' ? 'memory' : 'file';
          box.append(h(`<span class="source" title="${esc(s.collection || '')}">${icon(ico)}${esc(s.label)}</span>`));
        }
      }
    },
    done(html, error) {
      stopThinking();
      content.innerHTML = html || (error ? '' : '<p class="faint">(réponse vide)</p>');
      if (error) content.append(h(`<p style="color:var(--err)">⚠ ${esc(error)}</p>`));
      scrollBottom();
    },
    stopped() {
      stopThinking();
      content.querySelector('.typing')?.remove();
      content.append(h('<p class="faint">(réponse interrompue)</p>'));
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
      copyCode.textContent = 'Copié ✓';
      setTimeout(() => { copyCode.textContent = 'Copier'; }, 1400);
    });
    return;
  }
  const copyAll = e.target.closest('[data-act="copy"]');
  if (copyAll) {
    const text = copyAll.closest('.body').querySelector('.content').innerText;
    navigator.clipboard.writeText(text).then(() => toast('Réponse copiée.', 'ok', 1800));
    return;
  }
  const again = e.target.closest('[data-act="regenerate"]');
  if (again) { regenerate(again.closest('.msg')); return; }
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
  el.send.title = on ? 'Arrêter (Échap)' : 'Envoyer (Entrée)';
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
      title: 'Lancer une recherche web ?', iconName: 'globe',
      text: "Ta question semble demander une recherche sur le web, qui est désactivée. " +
            "La lancer pour ce message ? Seule la requête ci-dessous partira vers DuckDuckGo.",
      quote: text.slice(0, 300), confirm: 'Chercher sur le web', cancel: 'Répondre sans',
    });
    web = r.ok;
  } else if (allowed && pre.web_intent && session.webConsent) {
    web = true;
  } else if (allowed && pre.web_intent) {
    const r = await modal({
      title: 'Envoyer cette recherche ?', iconName: 'globe',
      text: 'La requête ci-dessous va être envoyée à DuckDuckGo. Aucune autre donnée ne quitte ta machine.',
      quote: text.slice(0, 300), confirm: 'Envoyer', cancel: 'Répondre sans',
      check: 'Ne plus demander pendant cette session',
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
    toast('Les passages trouvés chez tes machines et tes amis ne vont pas à un modèle prêté par '
      + 'un ami : cette réponse s\'en passera.', 'info', 7000);
    return [];
  }
  const ids = pre.consult.map(m => m.node_id);
  const unasked = pre.consult.filter(m => m.friend && !session.peerConsent.has(m.node_id));
  if (!unasked.length) return ids;
  const r = await modal({
    title: 'Chercher aussi chez tes amis ?', iconName: 'network',
    text: `Ta question va partir chez ${pre.consult.map(m => `« ${m.name} »`).join(', ')} : chaque machine `
      + 'cherche dans les bibliothèques qu\'elle t\'ouvre, et seuls les passages trouvés reviennent. AIODOS n\'y garde '
      + 'pas ta question, mais chaque machine note dans son journal que tu as cherché, et combien de passages elle '
      + 't\'a rendus.',
    quote: text.slice(0, 300), confirm: 'Chercher chez eux', cancel: 'Répondre sans',
    check: 'Ne plus redemander pour ces amis pendant cette session',
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
    toast('Une pièce jointe est encore en route : un instant…');
    return false;
  }
  const typed = asText ?? el.input.value.trim();
  const joined = asText === null ? pendingItems() : [];
  const attached = joined.map(a => a.id);
  const text = typed || (attached.length ? 'Que peux-tu me dire de la pièce jointe ?' : '');
  if (!text) return false;
  if (editing && !editing.node.isConnected) stopEditing(false);   // retiré entre-temps
  if (editing && asText === null) replace = replacing(editing.node, editing.text);
  const swap = replace ? { replace_from: replace.id, replace_text: replace.text } : {};
  const explicit = asText === null && creationRequest(text);     // mode « Créer » ouvert
  if (explicit && !typed) {
    toast('Décris l\'image à créer : l\'image jointe en sera le point de départ.');
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
  if (!model) {
    if (!state.modelsLoaded) toast('Les modèles se chargent, un instant…');
    else if (integrated()) toast('Aucun modèle disponible : installe-en un (« Installer un modèle »).', 'err');
    else toast('Aucun modèle disponible : vérifie qu\'Ollama est lancé.', 'err');
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
    await streamChat({
      text, model, web: go.web, peer_libraries: go.peers, attachments: attached,
      conversation_id: state.currentConversation?.id ?? null, ...swap,
    }, ev => {
      if (ev.type === 'start') {
        started = true;
        if (attached.length) clearAttachments();   // le message les a emportées
        dropReplaced(replace, asked);              // acceptée : l'ancien échange s'en va
        asked.dataset.id = ev.user_message_id;
        if (state.currentConversation?.id !== ev.conversation_id) {
          state.currentConversation = { id: ev.conversation_id, title: ev.title || 'Nouvelle conversation', model };
          prefs.set('lastConversation', ev.conversation_id);
        }
        if (ev.title) { state.currentConversation.title = ev.title; setTitle(ev.title); }
        emit('chat:current');
      } else if (ev.type === 'route') view.route(ev);
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
  const done = appendWaiting(el.list, request.followup === 'edit' ? 'Je retouche la description…'
    : !request.followup && state.settings?.images_enrich ? 'J\'écris une description détaillée…' : '');
  scrollBottom(true);
  let made;
  try {
    made = await post('/chat/create', {
      ...request, text, conversation_id: state.currentConversation?.id ?? null,
      chat_model: selectedModel() || null,
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
    // 409 : aucun modèle ne peut la calculer ; des amis le pourraient peut-être. Reconnue
    // au vol, la demande n'en était peut-être pas une : « Répondre en texte » (si le
    // message ne part pas, il revient dans la zone de saisie)
    const notice = appendNotice(el.list, e.message, {
      images: e.status === 409, friends: e.data?.friends || [],
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
    toast(`Création lancée dans « ${made.title || 'sa conversation'} ».`, 'info', 5000);
    return;
  }
  if (state.currentConversation?.id !== made.conversation_id) {
    state.currentConversation = { id: made.conversation_id, title: made.title || 'Nouvelle conversation',
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
