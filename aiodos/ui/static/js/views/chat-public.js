// Le réseau public dans la conversation (chat/public_consent.py, network/public_chat.py) :
// « Sur mes machines seulement », par conversation (dans le panneau des autorisations,
// chat-permissions.js), les cartes d'accord (un modèle plus gros, les passages de mes
// documents) et la note de la première fois, montrées au-dessus de la réponse qu'elles
// concernent. Rien n'y bloque l'envoi : la réponse en cours part sur ce qui est permis sans
// accord, et le choix vaut pour la suite (Réglages, Cette machine, Partage de puissance).
import { patch, put } from '../api.js';
import { h, esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { errorToast } from '../ui.js';

const PUBLIC = 'public:';
// Carte d'accord : le réglage qu'elle remplit, sa question et ce qu'elle dit de la réponse en cours
const CARDS = {
  public_bigger: { key: 'public_bigger_model', text: 'chat.public_bigger_text', note: 'chat.public_bigger_note' },
  public_documents: { key: 'public_documents', text: 'chat.public_documents_text', note: 'chat.public_documents_note' },
};
let next = false;          // conversation pas encore créée : le choix fait avant son premier message
let row = null;

/** Un identifiant de modèle servi par une machine du réseau public (« public:<clé>:<modèle> »). */
export const isPublic = id => typeof id === 'string' && id.startsWith(PUBLIC);

/** « Sur mes machines seulement » pour la conversation affichée (ou la prochaine). */
export function mineOnly() {
  const conv = state.currentConversation;
  return conv?.id ? !!conv.mine_only : next;
}

/** L'interrupteur de la conversation, pour le panneau des autorisations. */
export function mineOnlyRow() {
  return `<div class="perm-group" data-mine-only>
      <div class="perm-head">${esc(t('chat.this_conversation'))}</div>
      <label class="perm-row">${icon('lock')}
        <span class="txt"><b>${esc(t('chat.mine_only'))}</b><small>${esc(t('chat.mine_only_help'))}</small></span>
        <span class="switch"><input type="checkbox"><span></span></span></label>
    </div>`;
}

/** Relie l'interrupteur (dans `root`) ; changed() redessine ce qui en dépend (le bouton). */
export function bindMineOnly(root, changed) {
  row = root.querySelector('[data-mine-only]');
  const input = row.querySelector('input');
  input.onchange = async () => { await setMineOnly(input.checked); sync(); changed(); };
  on('chat:current', () => {
    const conv = state.currentConversation;
    if (!conv) next = false;                    // nouvelle conversation : désactivé
    else if (conv.id && conv.mine_only === undefined) {
      // Créée par une image (chat-create.js) : le choix fait avant son premier message
      conv.mine_only = next;
      if (next) patch(`/conversations/${conv.id}`, { mine_only: true }).catch(errorToast);
      next = false;
    }
    sync();
    changed();
  });
  on('settings:loaded', () => { sync(); changed(); });
  sync();
}

async function setMineOnly(value) {
  const conv = state.currentConversation;
  if (!conv?.id) { next = value; return; }
  try {
    await patch(`/conversations/${conv.id}`, { mine_only: value });
    conv.mine_only = value;
  } catch (e) { errorToast(e); }
}

/** L'interrupteur d'après la conversation ; caché si mes conversations ne partent jamais
 *  chez un inconnu (réglage use_public). */
function sync() {
  if (!row) return;
  const kept = mineOnly();
  row.hidden = !state.settings?.use_public;
  row.querySelector('input').checked = kept;
  row.querySelector('.perm-row').classList.toggle('on', kept);
}

/** Une carte d'accord ou la note de la première fois (événement « notice »), au-dessus de
 *  la réponse `answer`. */
export function showNotice(ev, answer) {
  const node = ev.kind === 'public_first' ? firstNote() : consentCard(ev);
  if (node && answer?.isConnected) answer.before(node);
}

function card(kind, body) {
  return h(`<div class="msg assistant notice side-note ${kind}">
      <img class="avatar" src="/logo.png?v=serpents" alt="">
      <div class="body">${body}</div>
    </div>`);
}

function consentCard(ev) {
  const spec = CARDS[ev.kind];
  // Déjà répondu (dans une autre fenêtre, ou à une carte précédente) : rien à demander
  if (!spec || (state.settings && state.settings[spec.key] != null)) return null;
  // Une seule carte de ce genre à la fois : la plus récente
  document.querySelectorAll(`.side-note.public-card[data-kind="${ev.kind}"]:not(.answered)`)
    .forEach(n => n.remove());
  const node = card('public-card', `<div class="banner info wrap">${icon('globe')}
      <div class="banner-text"><b>${esc(t(spec.text, { model: ev.model || '' }))}</b>
        <small>${esc(t(spec.note))}</small></div>
      <div class="banner-actions">
        <button class="btn sm primary" type="button" data-answer="yes">${esc(t('chat.public_yes'))}</button>
        <button class="btn sm" type="button" data-answer="no">${esc(t('chat.public_no'))}</button></div>
    </div>`);
  node.dataset.kind = ev.kind;
  node.querySelectorAll('[data-answer]').forEach(button => {
    button.onclick = () => answer(node, spec.key, button.dataset.answer === 'yes');
  });
  return node;
}

/** Oui ou Non : gardé dans les Réglages ; la carte dit où le changer. */
async function answer(node, key, value) {
  const buttons = node.querySelectorAll('button');
  buttons.forEach(b => { b.disabled = true; });
  try {
    state.settings = await put('/settings', { [key]: value });
  } catch (e) {
    errorToast(e);
    buttons.forEach(b => { b.disabled = false; });
    return;
  }
  node.classList.add('answered');
  node.querySelector('.banner').innerHTML = `${icon('check')}<div class="banner-text">${
    esc(t(value ? 'chat.public_saved_yes' : 'chat.public_saved_no'))}</div>`;
  emit('settings:loaded');
}

function firstNote() {
  if (state.settings) state.settings.public_warned = true;
  const node = card('public-note', `<div class="banner warn wrap">${icon('globe')}
      <div class="banner-text"><b>${esc(t('chat.public_first_title'))}</b>
        <small>${esc(t('chat.public_first_text'))}</small></div>
      <div class="banner-actions">
        <button class="btn sm" type="button" data-ok>${esc(t('chat.public_first_ok'))}</button></div>
    </div>`);
  node.querySelector('[data-ok]').onclick = () => node.remove();
  return node;
}
