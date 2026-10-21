// Pièces jointes d'un message : une image de référence, une vidéo, un son, un document ou un
// texte (chat/attachments.py). Le trombone, un fichier glissé sur la conversation ou une
// image collée les ajoutent ; chacune part aussitôt au nœud (POST /api/attachments) et
// s'affiche au-dessus de la zone de saisie, avec une croix pour la retirer. Le message les
// emporte ensuite (pendingIds) ; une création d'image part de l'image jointe.
import { del, fileUrl, rawUpload } from '../api.js';
import { bytes, esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { errorToast, toast } from '../ui.js';

const MAX = 10;                    // par message (comme le nœud)
const KIND_ICON = { image: 'image', video: 'video', audio: 'audio', document: 'file', text: 'file', other: 'file' };

let tray = null;
let items = [];                    // { key, file, name, size, state: 'sending'|'ready'|'error', id, kind, preview, error }
let counter = 0;

/** Le trombone, le glisser-déposer sur la conversation et le collage d'images. */
export function mountAttach(chat, input) {
  const box = chat.querySelector('.composer-box');
  tray = document.createElement('div');
  tray.className = 'attach-tray';
  tray.hidden = true;
  box.before(tray);
  const picker = document.createElement('input');
  picker.type = 'file';
  picker.multiple = true;
  picker.hidden = true;
  const button = document.createElement('button');
  button.className = 'attach';
  button.type = 'button';
  button.title = t('chat_attach.button');
  button.innerHTML = icon('clip');
  box.prepend(button, picker);
  button.onclick = () => picker.click();
  picker.onchange = () => { add([...picker.files]); picker.value = ''; };
  input.addEventListener('paste', e => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); add(files); }
  });
  chat.addEventListener('dragover', e => {
    if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
    e.preventDefault();
    chat.classList.add('dropping');
  });
  chat.addEventListener('dragleave', e => {
    if (!chat.contains(e.relatedTarget)) chat.classList.remove('dropping');
  });
  chat.addEventListener('drop', e => {
    chat.classList.remove('dropping');
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    e.preventDefault();
    add(files);
  });
}

function add(files) {
  for (const file of files) {
    // Le même texte que le refus du nœud (api/routes/chat.py)
    if (items.length >= MAX) { toast(t('chat.attachments_max', { n: MAX }), 'err'); break; }
    const item = { key: ++counter, file, name: file.name || 'image-collee.png', size: file.size,
                   state: 'sending', id: null, kind: null,
                   preview: file.type.startsWith('image/') ? URL.createObjectURL(file) : null };
    items.push(item);
    send(item);
  }
  render();
}

async function send(item) {
  try {
    const made = await rawUpload('/attachments', item.file, item.name);
    if (!items.includes(item)) {          // retirée pendant l'envoi
      del(`/attachments/${made.id}`).catch(() => {});
      return;
    }
    Object.assign(item, { state: 'ready', id: made.id, kind: made.kind });
  } catch (err) {
    Object.assign(item, { state: 'error', error: err.message });
    errorToast(err);
  }
  render();
}

function remove(key) {
  const item = items.find(i => i.key === key);
  if (!item) return;
  items = items.filter(i => i !== item);
  if (item.preview) URL.revokeObjectURL(item.preview);
  if (item.id) del(`/attachments/${item.id}`).catch(() => {});
  render();
}

// En route, refusée, ou sa taille
const status = i => (i.state === 'sending' ? t('chat_attach.sending')
  : i.state === 'error' ? t('chat_attach.refused') : bytes(i.size));

function render() {
  if (!tray) return;
  tray.hidden = !items.length;
  tray.innerHTML = items.map(i => `
    <div class="attach-chip ${i.state}" title="${esc(i.error || i.name)}">
      ${i.preview ? `<img src="${i.preview}" alt="">` : icon(KIND_ICON[i.kind] || 'file')}
      <span class="attach-name">${esc(i.name)}</span>
      <span class="faint">${esc(status(i))}</span>
      <button class="icon-btn" type="button" data-remove="${i.key}" title="${esc(t('chat_attach.remove'))}">${icon('x')}</button>
    </div>`).join('');
  tray.querySelectorAll('[data-remove]').forEach(b => { b.onclick = () => remove(Number(b.dataset.remove)); });
}

/** Les pièces jointes prêtes à partir avec le message : { id, name, kind, size }. */
export function pendingItems() {
  return items.filter(i => i.state === 'ready').map(i => ({ id: i.id, name: i.name, kind: i.kind, size: i.size }));
}

/** Vrai tant qu'une pièce jointe est en route vers le nœud. */
export function attaching() {
  return items.some(i => i.state === 'sending');
}

/** Le message les a emportées : la zone se vide (sans les effacer du nœud). */
export function clearAttachments() {
  for (const i of items) if (i.preview) URL.revokeObjectURL(i.preview);
  items = [];
  render();
}

/** Les pièces jointes d'un message affiché : vignettes pour les images, sinon le nom. */
export function attachmentsHtml(list) {
  if (!list?.length) return '';
  return `<div class="msg-attachments">${list.map(a => {
    const url = fileUrl(`/attachments/${a.id}`);
    return a.kind === 'image'
      ? `<a class="attach-thumb" href="${url}" target="_blank" rel="noopener" title="${esc(a.name)}"><img src="${url}" alt="${esc(a.name)}" loading="lazy"></a>`
      : `<a class="attach-chip ready" href="${url}" target="_blank" rel="noopener" title="${esc(a.name)}">${icon(KIND_ICON[a.kind] || 'file')}<span class="attach-name">${esc(a.name)}</span><span class="faint">${bytes(a.size)}</span></a>`;
  }).join('')}</div>`;
}
