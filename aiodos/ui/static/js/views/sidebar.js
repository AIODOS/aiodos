// Barre latérale des conversations : dossiers, recherche, glisser-déposer, menus.
import { get, post, patch, del } from '../api.js';
import { $, h, esc } from '../dom.js';
import { icon } from '../icons.js';
import { state, emit, on, prefs } from '../state.js';
import { ask, confirmDanger, menu, errorToast } from '../ui.js';

let treeEl;
let filter = '';
const openFolders = new Set(prefs.get('openFolders', []));

export function mountSidebar(container) {
  const root = h(`
    <aside class="sidebar">
      <div class="sidebar-top">
        <div class="row">
          <button class="btn primary" id="new-chat">${icon('plus')}Nouvelle conversation</button>
          <button class="btn" id="new-folder" title="Nouveau dossier">${icon('folder-plus')}</button>
        </div>
        <div class="sidebar-head">
          <div class="search">${icon('search')}<input class="input" id="conv-search" placeholder="Rechercher une conversation"></div>
          <button class="icon-btn sidebar-fold" id="sidebar-fold" title="Replier la liste des conversations">${icon('chev-right')}</button>
        </div>
      </div>
      <div class="tree" id="conv-tree"></div>
    </aside>`);
  container.append(root);
  treeEl = $('#conv-tree', root);
  $('#new-chat', root).onclick = () => emit('chat:new');
  $('#new-folder', root).onclick = () => newFolder();
  $('#sidebar-fold', root).onclick = () => emit('chat:fold');
  $('#conv-search', root).oninput = e => { filter = e.target.value.trim().toLowerCase(); render(); };
  dropTarget(treeEl, null);                      // déposer dans le vide = hors dossier
  on('conversations:changed', refresh);
  on('chat:current', render);
  refresh();
}

export async function refresh() {
  try {
    state.tree = await get('/conversations');
    render();
  } catch (e) { /* le nœud redémarre peut-être : on réessaiera au prochain événement */ }
}

function render() {
  const { folders, conversations } = state.tree;
  const match = c => !filter || c.title.toLowerCase().includes(filter);
  treeEl.innerHTML = '';
  for (const f of folders) {
    const items = conversations.filter(c => c.folder_id === f.id && match(c));
    if (filter && !items.length) continue;
    const open = !!filter || openFolders.has(f.id);
    const row = h(`
      <div class="t-row folder ${open ? 'open' : ''}">
        ${icon('chev-right', 'chev')}${icon('folder')}
        <span class="label">${esc(f.name)}</span><span class="count">${items.length || ''}</span>
        <button class="icon-btn" title="Options du dossier">${icon('more')}</button>
      </div>`);
    row.onclick = e => { if (!e.target.closest('.icon-btn')) toggleFolder(f.id); };
    row.querySelector('.icon-btn').onclick = e => { e.stopPropagation(); folderMenu(row, f); };
    dropTarget(row, f.id);
    treeEl.append(row);
    if (open) {
      const kids = h('<div class="t-children"></div>');
      items.forEach(c => kids.append(convRow(c)));
      if (!items.length) kids.append(h('<div class="t-row faint" style="cursor:default">Vide : glisse une conversation ici</div>'));
      treeEl.append(kids);
    }
  }
  const loose = conversations.filter(c => !c.folder_id && match(c));
  if (folders.length && loose.length) treeEl.append(h('<div class="tree-section">Conversations</div>'));
  loose.forEach(c => treeEl.append(convRow(c)));
  if (!conversations.length) {
    treeEl.append(h(`<div class="empty">${icon('chat')}<div>Aucune conversation pour l'instant.</div></div>`));
  } else if (filter && !treeEl.children.length) {
    treeEl.append(h('<div class="empty">Aucun résultat.</div>'));
  }
}

function convRow(c) {
  const active = state.currentConversation?.id === c.id;
  const row = h(`
    <div class="t-row ${active ? 'active' : ''}" draggable="true" title="${esc(c.title)}">
      <span class="label">${esc(c.title)}</span>
      <button class="icon-btn" title="Options">${icon('more')}</button>
    </div>`);
  row.onclick = e => { if (!e.target.closest('.icon-btn')) emit('chat:open', c.id); };
  row.ondblclick = () => rename(c);
  row.querySelector('.icon-btn').onclick = e => { e.stopPropagation(); convMenu(row, c); };
  row.ondragstart = e => {
    e.dataTransfer.setData('text/aiodos-conv', String(c.id));
    e.dataTransfer.effectAllowed = 'move';
  };
  return row;
}

function dropTarget(el, folderId) {
  el.addEventListener('dragover', e => {
    if (!e.dataTransfer.types.includes('text/aiodos-conv')) return;
    e.preventDefault();
    e.stopPropagation();
    el.classList.add('drop-target');
  });
  el.addEventListener('dragleave', () => el.classList.remove('drop-target'));
  el.addEventListener('drop', e => {
    const id = Number(e.dataTransfer.getData('text/aiodos-conv'));
    el.classList.remove('drop-target');
    if (!id) return;
    e.preventDefault();
    e.stopPropagation();
    move(id, folderId);
  });
}

function toggleFolder(id) {
  openFolders.has(id) ? openFolders.delete(id) : openFolders.add(id);
  prefs.set('openFolders', [...openFolders]);
  render();
}

function convMenu(anchor, c) {
  const targets = state.tree.folders.filter(f => f.id !== c.folder_id);
  menu(anchor.querySelector('.icon-btn'), [
    { label: 'Renommer', icon: 'edit', action: () => rename(c) },
    { title: 'Déplacer vers' },
    ...(c.folder_id ? [{ label: 'Hors dossier', icon: 'chat', action: () => move(c.id, null) }] : []),
    ...targets.map(f => ({ label: f.name, icon: 'folder', action: () => move(c.id, f.id) })),
    { label: 'Nouveau dossier…', icon: 'folder-plus',
      action: async () => { const id = await newFolder(); if (id) move(c.id, id); } },
    'sep',
    { label: 'Supprimer', icon: 'trash', danger: true, action: () => remove(c) },
  ]);
}

function folderMenu(anchor, f) {
  menu(anchor.querySelector('.icon-btn'), [
    { label: 'Renommer le dossier', icon: 'edit', action: () => renameFolder(f) },
    { label: 'Supprimer le dossier', icon: 'trash', danger: true, action: () => deleteFolder(f) },
  ]);
}

async function rename(c) {
  const title = await ask('Renommer la conversation', 'Nouveau titre :', c.title);
  if (!title || title === c.title) return;
  try {
    await patch(`/conversations/${c.id}`, { title });
    if (state.currentConversation?.id === c.id) {
      state.currentConversation.title = title;
      emit('chat:renamed', title);
    }
  } catch (e) { errorToast(e); }
}

async function move(id, folderId) {
  try {
    await patch(`/conversations/${id}`, { folder_id: folderId });
    if (folderId) { openFolders.add(folderId); prefs.set('openFolders', [...openFolders]); }
  } catch (e) { errorToast(e); }
}

async function remove(c) {
  const ok = await confirmDanger('Supprimer la conversation',
    `« ${c.title} » et sa trace dans la mémoire seront supprimées définitivement.`);
  if (!ok) return;
  try {
    await del(`/conversations/${c.id}`);
    if (state.currentConversation?.id === c.id) emit('chat:new');
  } catch (e) { errorToast(e); }
}

async function newFolder() {
  const name = await ask('Nouveau dossier', 'Nom du dossier :', '', 'ex. Projet court-métrage');
  if (!name) return null;
  try {
    const { id } = await post('/folders', { name });
    openFolders.add(id);
    prefs.set('openFolders', [...openFolders]);
    return id;
  } catch (e) { errorToast(e); return null; }
}

async function renameFolder(f) {
  const name = await ask('Renommer le dossier', 'Nouveau nom :', f.name);
  if (!name || name === f.name) return;
  try { await patch(`/folders/${f.id}`, { name }); } catch (e) { errorToast(e); }
}

async function deleteFolder(f) {
  const ok = await confirmDanger('Supprimer le dossier',
    `Le dossier « ${f.name} » sera supprimé. Ses conversations sont conservées et reviennent hors dossier.`);
  if (!ok) return;
  try { await del(`/folders/${f.id}`); } catch (e) { errorToast(e); }
}
