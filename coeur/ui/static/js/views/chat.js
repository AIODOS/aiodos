// Conversation : choix du modèle, options, messages en streaming, validations.
import { get, post, put, patch, streamChat, openExternal } from '../api.js';
import { $, h, esc, bytes, duration } from '../dom.js';
import { icon } from '../icons.js';
import { state, emit, on, prefs, session } from '../state.js';
import { modal, ask, toast, errorToast } from '../ui.js';
import { mountSidebar, refresh as refreshTree } from './sidebar.js';

const FLAGS = ['knowledge_enabled', 'conversations_enabled', 'web_search_enabled', 'think'];
let el = {};
let busy = false;
let controller = null;

export function mountChat(container) {
  mountSidebar(container);
  const chat = h(`
    <div class="chat">
      <header class="chat-head">
        <div class="chat-title" id="chat-title" title="Cliquer pour renommer">Nouvelle conversation</div>
        <select class="select" id="model-select" title="Modèle utilisé pour répondre"></select>
        <div class="chips">
          <button class="chip" data-flag="knowledge_enabled" title="Utiliser tes bibliothèques de connaissance">${icon('book')}Bibliothèques</button>
          <button class="chip" data-flag="conversations_enabled" title="Se souvenir de tes autres conversations">${icon('memory')}Mémoire</button>
          <button class="chip" data-flag="web_search_enabled" title="Chercher sur le web (validation avant chaque envoi)">${icon('globe')}Web</button>
          <button class="chip" data-flag="think" title="Laisser le modèle réfléchir avant de répondre (plus lent, souvent meilleur)">${icon('think')}Réflexion</button>
        </div>
      </header>
      <div id="chat-banner"></div>
      <div class="messages" id="messages"><div class="messages-inner" id="messages-inner"></div></div>
      <footer class="composer">
        <div class="composer-box">
          <textarea id="composer" rows="1" placeholder="Écris ton message…"></textarea>
          <button class="send" id="send" title="Envoyer (Entrée)">${icon('send')}</button>
        </div>
        <div class="hint">Entrée pour envoyer · Maj+Entrée pour aller à la ligne · Échap pour arrêter la réponse</div>
      </footer>
    </div>`);
  container.append(chat);
  el = {
    title: $('#chat-title'), model: $('#model-select'), banner: $('#chat-banner'),
    scroll: $('#messages'), list: $('#messages-inner'), input: $('#composer'), send: $('#send'),
  };

  chat.querySelectorAll('.chip').forEach(chip => { chip.onclick = () => toggleFlag(chip.dataset.flag); });
  el.model.onchange = onModelChange;
  el.input.addEventListener('input', autoresize);
  el.input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && busy) stop(); });
  el.send.onclick = () => (busy ? stop() : send());
  el.title.onclick = renameCurrent;
  el.list.addEventListener('click', onMessageClick);

  on('chat:new', newConversation);
  on('chat:open', openConversation);
  on('chat:renamed', title => setTitle(title));
  on('settings:loaded', syncChips);
  on('models:loaded', fillModels);
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

/** Libellé d'un modèle : « qwen3.6 · PC fixe » s'il est servi par une machine appairée. */
export function modelLabel(id) {
  const m = state.models.find(x => x.id === id);
  if (m) return m.peer ? `${m.label} · ${m.peer}` : m.label;
  if (id?.startsWith('peer:')) return `${id.split(':').slice(2).join(':')} · autre machine`;
  return id || '';
}

function fillModels() {
  const wanted = el.model.value || state.currentConversation?.model || state.settings?.default_model;
  const local = state.models.filter(m => m.group === 'local');
  const remote = state.models.filter(m => m.group === 'remote');
  const cloud = state.models.filter(m => m.group === 'cloud');
  const vram = vramTotal();
  el.model.innerHTML = '';
  const group = (label, models, text) => {
    if (!models.length) return;
    const g = document.createElement('optgroup');
    g.label = label;
    models.forEach(m => g.append(new Option(text(m), m.id)));
    el.model.append(g);
  };
  group('Sur cette machine', local, m =>
    `${m.label} · ${bytes(m.size)}${vram && m.size > vram * 0.92 ? ' · lent (dépasse la VRAM)' : ''}`);
  group('Sur mes machines · réponse calculée là-bas', remote, m =>
    `${m.label} · ${m.peer}${m.fits_vram === false ? ' · lent (dépasse sa VRAM)' : ''}`);
  group('Cloud · validation avant envoi', cloud, m => `${m.label}${m.has_key ? '' : ' · clé manquante'}`);
  const ids = state.models.map(m => m.id);
  el.model.value = ids.includes(wanted) ? wanted : (local[0]?.id || remote[0]?.id || cloud[0]?.id || '');
  el.banner.innerHTML = local.length ? '' : remote.length ? `
    <div class="banner info">${icon('network')}<div style="flex:1">Aucun modèle sur ce PC : tes réponses seront
    calculées par tes machines appairées qui prêtent les leurs.</div></div>` : `
    <div class="banner warn">${icon('warn')}<div style="flex:1">Aucun modèle local : Ollama ne répond pas ou
    aucun modèle n'est installé. Cœur IA réessaie tout seul toutes les 8 secondes.</div>
    <button class="btn sm" id="models-retry">${icon('retry')}Réessayer</button></div>`;
  el.banner.querySelector('#models-retry')?.addEventListener('click', () => emit('models:reload'));
}

async function onModelChange() {
  const id = el.model.value;
  if (id && !id.startsWith('cloud:')) {
    try { state.settings = await put('/settings', { default_model: id }); } catch { /* sans gravité */ }
  }
}

function setTitle(title) {
  el.title.textContent = title || 'Nouvelle conversation';
  document.title = title ? `${title} — Cœur IA` : 'Cœur IA';
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
  state.currentConversation = null;
  prefs.set('lastConversation', null);
  setTitle(null);
  showWelcome();
  emit('chat:current');
  el.input.focus();
}

async function openConversation(id) {
  if (busy) {
    toast('Une réponse est en cours : arrête-la (Échap) avant de changer de conversation.');
    return;
  }
  let conv;
  try { conv = await get(`/conversations/${id}`); } catch (e) {
    if (e.status === 404) { newConversation(); return; }
    errorToast(e); return;
  }
  state.currentConversation = { id: conv.id, title: conv.title, model: conv.model };
  prefs.set('lastConversation', conv.id);
  setTitle(conv.title);
  el.list.innerHTML = '';
  for (const m of conv.messages) {
    if (m.role === 'user') { appendUser(m.content); continue; }
    const view = appendAssistant(m.model);
    if (m.thinking) view.restoreThinking(m.thinking);
    view.sources(m.sources);
    view.done(m.html);
  }
  if (conv.model && [...el.model.options].some(o => o.value === conv.model)) el.model.value = conv.model;
  emit('chat:current');
  scrollBottom(true);
  el.input.focus();
}

function showWelcome() {
  el.list.innerHTML = '';
  const welcome = h(`
    <div class="welcome">
      <img src="/logo.png" alt="">
      <h1>Que fait-on aujourd'hui ?</h1>
      <p>Tout tourne sur ta machine. Le web et le cloud ne servent qu'avec ton accord.</p>
      <div class="suggestions">
        <div class="suggestion" data-text="Écris une fonction Python qui ">${icon('code')}<b>Écrire du code</b><span>Une fonction, un script, une correction…</span></div>
        <div class="suggestion" data-text="Qu'y a-t-il dans mes bibliothèques ? Résume leur contenu.">${icon('book')}<b>Interroger mes documents</b><span>Ce que contiennent tes bibliothèques</span></div>
        <div class="suggestion" data-text="Cherche sur le web ">${icon('globe')}<b>Chercher sur le web</b><span>Avec validation avant l'envoi</span></div>
      </div>
    </div>`);
  welcome.querySelectorAll('.suggestion').forEach(s => {
    s.onclick = () => { el.input.value = s.dataset.text; autoresize(); el.input.focus(); };
  });
  el.list.append(welcome);
}

// ------------------------------------------------------------------- messages
function appendUser(text) {
  el.list.querySelector('.welcome')?.remove();
  el.list.append(h(`<div class="msg user"><div class="bubble">${esc(text)}</div></div>`));
}

function appendAssistant(model) {
  el.list.querySelector('.welcome')?.remove();
  const label = modelLabel(model);
  const node = h(`
    <div class="msg assistant">
      <img class="avatar" src="/logo.png" alt="">
      <div class="body">
        <details class="thinking" hidden>
          <summary>${icon('think')}<span class="label">Réflexion</span><span class="faint time"></span></summary>
          <div class="thinking-body"></div>
        </details>
        <div class="md content"><div class="typing"><i></i><i></i><i></i></div></div>
        <div class="meta">
          <span class="model-tag">${esc(label)}</span>
          <span class="sources"></span>
          <span class="actions"><button class="icon-btn" data-act="copy" title="Copier la réponse">${icon('copy')}</button></span>
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

  // Premier message : charger le modèle en VRAM peut prendre 20-30 s. On le dit.
  const waitStart = Date.now();
  const waitTimer = setInterval(() => {
    const seconds = (Date.now() - waitStart) / 1000;
    const dots = content.querySelector('.typing');
    if (seconds < 3 || !dots) return;
    let label = content.querySelector('.wait-label');
    if (!label) { label = h('<span class="wait-label faint"></span>'); content.append(label); }
    label.textContent = `Le modèle se prépare… ${duration(seconds)}`;
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
        if (s.type === 'web') {
          box.append(h(`<a class="source" href="${esc(s.url)}" title="${esc(s.title || s.url)}">${icon('globe')}${esc(s.label)}</a>`));
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
  const link = e.target.closest('a[href]');
  if (link && /^https?:/i.test(link.getAttribute('href'))) {
    e.preventDefault();
    openExternal(link.href).catch(errorToast);
  }
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
  el.send.classList.toggle('stop', on);
  el.send.innerHTML = icon(on ? 'stop' : 'send');
  el.send.title = on ? 'Arrêter (Échap)' : 'Envoyer (Entrée)';
}

function stop() {
  controller?.abort();
}

// ------------------------------------------------------- envoi et validations
async function confirmOutgoing(text, model, pre) {
  let web = !!state.settings?.web_search_enabled;
  if (!web && pre.web_intent) {
    const r = await modal({
      title: 'Lancer une recherche web ?', iconName: 'globe',
      text: "Ta question semble demander une recherche sur le web, qui est désactivée. " +
            "La lancer pour ce message ? Seule la requête ci-dessous partira vers DuckDuckGo.",
      quote: text.slice(0, 300), confirm: 'Chercher sur le web', cancel: 'Répondre sans',
    });
    web = r.ok;
  } else if (web && !session.webConsent) {
    const r = await modal({
      title: 'Envoyer cette recherche ?', iconName: 'globe',
      text: 'La requête ci-dessous va être envoyée à DuckDuckGo. Aucune autre donnée ne quitte ta machine.',
      quote: text.slice(0, 300), confirm: 'Envoyer', cancel: 'Répondre sans',
      check: 'Ne plus demander pendant cette session',
    });
    web = r.ok;
    if (r.ok && r.checked) session.webConsent = true;
  }

  let cloudConfirmed = false;
  if (pre.cloud) {
    const label = state.models.find(m => m.id === model)?.label || pre.cloud;
    if (!pre.cloud_has_key) {
      const r = await modal({
        title: 'Clé API manquante', iconName: 'cloud',
        text: `Il faut une clé API pour utiliser ${label}. Tu peux la saisir dans les Réglages.`,
        confirm: 'Ouvrir les réglages',
      });
      if (r.ok) emit('view:show', 'settings');
      return null;
    }
    if (pre.cloud_confirm && !session.cloudConsent.has(pre.cloud)) {
      const leaving = ['ton message', "l'historique de cette conversation"];
      if (state.settings?.knowledge_enabled || state.settings?.conversations_enabled) {
        leaving.push('les extraits de tes connaissances utilisés pour répondre');
      }
      if (web) leaving.push('les résultats web');
      const r = await modal({
        title: 'Envoyer vers le cloud ?', iconName: 'cloud',
        text: `Cette réponse sera calculée par ${label}, sur les serveurs de ${pre.cloud_host}.\n` +
              `Quitteront ta machine : ${leaving.join(', ')}.`,
        quote: text.slice(0, 400), confirm: 'Envoyer', cancel: 'Annuler',
        check: 'Ne plus demander pour ce fournisseur pendant cette session',
      });
      if (!r.ok) return null;
      if (r.checked) session.cloudConsent.add(pre.cloud);
    }
    cloudConfirmed = true;
  }
  return { web, cloudConfirmed };
}

async function send() {
  if (busy) return;
  const text = el.input.value.trim();
  if (!text) return;
  const model = el.model.value;
  if (!model) {
    if (state.modelsLoaded) toast('Aucun modèle disponible : vérifie qu\'Ollama est lancé.', 'err');
    else toast('Les modèles se chargent, un instant…');
    return;
  }

  let pre;
  try { pre = await post('/chat/preflight', { text, model }); } catch (e) { errorToast(e); return; }
  const go = await confirmOutgoing(text, model, pre);
  if (!go) return;

  el.input.value = '';
  autoresize();
  appendUser(text);
  const view = appendAssistant(model);
  scrollBottom(true);
  setBusy(true);
  controller = new AbortController();
  try {
    await streamChat({
      text, model, web: go.web, cloud_confirmed: go.cloudConfirmed,
      conversation_id: state.currentConversation?.id ?? null,
    }, ev => {
      if (ev.type === 'start') {
        if (state.currentConversation?.id !== ev.conversation_id) {
          state.currentConversation = { id: ev.conversation_id, title: ev.title || 'Nouvelle conversation', model };
          prefs.set('lastConversation', ev.conversation_id);
        }
        if (ev.title) { state.currentConversation.title = ev.title; setTitle(ev.title); }
        emit('chat:current');
      } else if (ev.type === 'sources') view.sources(ev.sources);
      else if (ev.type === 'thinking') view.think(ev.text);
      else if (ev.type === 'render') view.render(ev.html);
      else if (ev.type === 'done') view.done(ev.html, ev.error);
    }, controller.signal);
  } catch (e) {
    if (e.name === 'AbortError') view.stopped();
    else view.error(e.message);
  } finally {
    setBusy(false);
    controller = null;
    refreshTree();
    el.input.focus();
  }
}
