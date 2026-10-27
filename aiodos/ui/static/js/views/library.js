// Bibliothèques : collections, arborescence du contenu, ajouts, mémoire, ouverture à la
// consultation (opening.js) et recherche chez les autres (consult.js).
import { get, post, patch, del, pickFiles, pickFolder } from '../api.js';
import { $, h, esc, debounce } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on, prefs, emit } from '../state.js';
import { ask, confirmDanger, menu, modal, toast, errorToast } from '../ui.js';
import { mountConsult, refreshConsult } from './consult.js';
import { jobTitle } from './jobs.js';
import { closedControls, openBadge, openingMenu, setClosed } from './opening.js';

const KIND_FILTERS = [['all', 'library.filter_all'], ['file', 'library.filter_file'],
  ['folder', 'library.filter_folder'], ['url', 'library.filter_url']];
const IMAGE = /\.(jpe?g|png|gif|bmp|tiff?|webp|heic)$/i;
const MEDIA = /\.(mp3|wav|flac|aac|ogg|m4a|opus|mp4|mov|avi|mkv|webm|wmv|flv)$/i;
// La bibliothèque affichée, retenue d'une ouverture à l'autre (une préférence, pas une clé
// de texte de library.json)
const SELECTED = 'library.selected';          // i18n: tel quel (nom d'une préférence)

let root;
let collections = [];
let memory = null;
let entries = [];
let selected = prefs.get(SELECTED, null);
let kindFilter = 'all';
let shownCollection = null;          // bibliothèque dont le contenu est affiché
const openDirs = new Set();

export function mountLibrary(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head">
        <div><h1>${esc(t('library.title'))}</h1>
          <div class="sub">${esc(t('library.intro'))}</div></div>
        <div class="grow"></div>
        <button class="btn" id="lib-new">${icon('plus')}${esc(t('library.new'))}</button>
      </div>
      <div class="library">
        <div>
          <div class="coll-list" id="coll-list"></div>
          <div class="card" id="memory-card" style="margin-top:16px"></div>
        </div>
        <div class="card" id="lib-content"></div>
      </div>
    </div></div>`);
  container.append(root);
  mountConsult($('.page-inner', root));
  $('#lib-new', root).onclick = createCollection;
  const reload = debounce(load, 600);
  on('knowledge:changed', reload);
  on('network:changed', () => { if (state.view === 'library') refreshConsult(); });
  on('jobs:changed', () => { if (state.view === 'library') renderJobStrip(); });
  on('settings:loaded', renderMemory);
  on('view:shown', view => { if (view === 'library') load(); });
}

async function load() {
  try {
    const [data, list] = await Promise.all([get('/collections'), get('/entries')]);
    collections = data.collections;
    memory = data.memory;
    entries = list;
  } catch (e) { errorToast(e); return; }
  if (!collections.some(c => c.id === selected)) selected = collections[0]?.id ?? null;
  renderCollections();
  // Même bibliothèque : on ne refait que l'arborescence (la barre d'outils reste,
  // un clic pendant une indexation n'est donc pas perdu)
  if (shownCollection === selected && $('#ktree', root)) {
    $('#lib-title', root).textContent = collections.find(c => c.id === selected)?.name || '';
    renderJobStrip();
    renderTree();
  } else {
    renderContent();
  }
  renderMemory();
  refreshConsult();
}

// ----------------------------------------------------------------- collections
function renderCollections() {
  const list = $('#coll-list', root);
  list.innerHTML = '';
  for (const c of collections) {
    const item = h(`
      <div class="coll ${c.id === selected ? 'active' : ''} ${c.enabled ? '' : 'disabled'}">
        ${icon('library')}
        <div class="name"><b>${esc(c.name)}</b><small>${esc(t('library.entries', { n: c.entries }))} · ${esc(t('library.chunks_total', { n: c.chunks }))}</small></div>
        ${c.local_only ? `<span class="local-only" title="${esc(t('library.local_only_hint'))}">${icon('lock')}</span>` : ''}
        ${c.share_friends && !c.local_only ? `<span class="local-only" title="${esc(t('library.shared_hint'))}">${icon('globe')}</span>` : ''}
        ${openBadge(c)}
        <label class="switch" title="${esc(t(c.enabled ? 'library.used' : 'library.ignored'))}">
          <input type="checkbox" ${c.enabled ? 'checked' : ''}><span></span></label>
        <button class="icon-btn" title="${esc(t('library.options'))}">${icon('more')}</button>
      </div>`);
    item.onclick = e => {
      if (e.target.closest('.switch, .icon-btn')) return;
      selected = c.id;
      prefs.set(SELECTED, selected);
      renderCollections();
      renderContent();
    };
    item.querySelector('input').onchange = async e => {
      try { await patch(`/collections/${c.id}`, { enabled: e.target.checked }); } catch (err) { errorToast(err); }
    };
    item.querySelector('.icon-btn').onclick = e => {
      e.stopPropagation();
      const scope = c.scope || (c.local_only ? 'local' : 'mine');
      menu(e.currentTarget, [
        { label: t('library.rename'), icon: 'edit', action: () => renameCollection(c) },
        'sep',
        { title: t('library.compute_title') },
        ...SCOPES.filter(s => s.value !== scope).map(s => ({
          label: t(s.from?.[scope] || s.label), icon: s.icon, action: () => setScope(c, s.value) })),
        'sep',
        ...openingMenu(c),
        'sep',
        { label: t('library.delete'), icon: 'trash', danger: true, action: () => deleteCollection(c) },
      ]);
    };
    list.append(item);
  }
  if (!collections.length) list.append(h(`<div class="empty">${esc(t('library.none'))}</div>`));
}

async function createCollection() {
  const name = await ask(t('library.new'), t('library.name_label'), '', t('library.name_placeholder'));
  if (!name) return;
  try {
    const { id } = await post('/collections', { name });
    selected = id;
    prefs.set(SELECTED, id);
    load();
  } catch (e) { errorToast(e); }
}

// Où le calcul d'une bibliothèque peut partir (network/sharing.py, knowledge/store.set_scope).
// Libellés et messages : clés de library.json, traduites à l'affichage
const SCOPES = [
  { value: 'local', label: 'library.scope_local', icon: 'lock', done: 'library.scope_local_done' },
  { value: 'mine', label: 'library.scope_mine', icon: 'network',
    from: { friends: 'library.scope_mine_from_friends' }, done: 'library.scope_mine_done' },
  { value: 'friends', label: 'library.scope_friends', icon: 'globe', done: 'library.scope_friends_done' },
];

async function setScope(c, scope) {
  try {
    await patch(`/collections/${c.id}`, { scope });
    toast(t(SCOPES.find(s => s.value === scope).done, { name: c.name }), 'ok', 7000);
  } catch (e) { errorToast(e); }
}

async function renameCollection(c) {
  const name = await ask(t('library.rename_title'), t('library.new_name_label'), c.name);
  if (!name || name === c.name) return;
  try { await patch(`/collections/${c.id}`, { name }); } catch (e) { errorToast(e); }
}

async function deleteCollection(c) {
  const ok = await confirmDanger(t('library.delete_title'),
    t('library.delete_text', { name: c.name, n: c.entries }));
  if (!ok) return;
  try { await del(`/collections/${c.id}`); } catch (e) { errorToast(e); }
}

// ------------------------------------------------------------------ contenu
function renderContent() {
  const box = $('#lib-content', root);
  const c = collections.find(x => x.id === selected);
  shownCollection = c ? c.id : null;
  if (!c) {
    box.innerHTML = `<div class="empty">${icon('library')}<div>${esc(t('library.create_first'))}</div></div>`;
    return;
  }
  box.innerHTML = `
    <div class="toolbar">
      <h2 class="toolbar-title" id="lib-title">${esc(c.name)}</h2>
      <button class="btn sm" data-add="files">${icon('file')}${esc(t('library.add_files'))}</button>
      <button class="btn sm" data-add="folder">${icon('folder')}${esc(t('library.add_folder'))}</button>
      <button class="btn sm" data-add="urls">${icon('globe')}${esc(t('library.add_urls'))}</button>
    </div>
    <div class="job-strip" id="job-strip"></div>
    <div class="toolbar">
      <div class="segmented">${KIND_FILTERS.map(([k, label]) =>
        `<button data-kind="${k}" class="${k === kindFilter ? 'on' : ''}">${esc(t(label))}</button>`).join('')}</div>
      <div class="grow"></div>
      <button class="btn ghost sm" data-tree="open">${esc(t('library.expand_all'))}</button>
      <button class="btn ghost sm" data-tree="close">${esc(t('library.collapse_all'))}</button>
    </div>
    <div class="ktree" id="ktree"></div>`;
  box.querySelectorAll('[data-add]').forEach(b => { b.onclick = () => add(b.dataset.add); });
  box.querySelectorAll('[data-kind]').forEach(b => {
    b.onclick = () => { kindFilter = b.dataset.kind; renderContent(); };
  });
  box.querySelector('[data-tree="open"]').onclick = () => { allDirs(tree()).forEach(p => openDirs.add(p)); renderTree(); };
  box.querySelector('[data-tree="close"]').onclick = () => { openDirs.clear(); renderTree(); };
  renderJobStrip();
  renderTree();
}

function visibleEntries() {
  return entries.filter(e => e.collection_id === selected && (kindFilter === 'all' || e.kind === kindFilter));
}

function tree() {
  const top = { dirs: new Map(), files: [], path: '' };
  for (const e of visibleEntries()) {
    if (e.kind === 'url') { top.files.push({ name: e.source, entry: e }); continue; }
    const parts = e.source.split('/');
    let node = top;
    for (const part of parts.slice(0, -1)) {
      if (!node.dirs.has(part)) {
        node.dirs.set(part, { dirs: new Map(), files: [], path: node.path ? `${node.path}/${part}` : part });
      }
      node = node.dirs.get(part);
    }
    node.files.push({ name: parts.at(-1), entry: e });
  }
  return top;
}

function allDirs(node, out = []) {
  for (const d of node.dirs.values()) { out.push(d.path); allDirs(d, out); }
  return out;
}

function countFiles(node) {
  let n = node.files.length;
  for (const d of node.dirs.values()) n += countFiles(d);
  return n;
}

function fileIcon(e) {
  if (e.kind === 'url') return icon('globe', 'kind-url');
  if (IMAGE.test(e.source)) return icon('image', 'kind-image');
  if (MEDIA.test(e.source)) return icon('audio', 'kind-media');
  return icon('file', 'kind-file');
}

function renderTree() {
  const box = $('#ktree', root);
  const top = tree();
  box.innerHTML = '';
  if (!countFiles(top)) {
    box.innerHTML = `<div class="empty">${icon('folder-plus')}<div>${esc(t('library.tree_empty'))}<br>
      ${esc(t('library.tree_empty_hint'))}</div></div>`;
    return;
  }
  box.append(renderNode(top));
}

// Fermer (ou rouvrir) à la consultation la ligne d'un dossier ou d'un document
function bindClosed(row, path, folder) {
  const c = collections.find(x => x.id === selected);
  row.querySelector('[data-part]')?.addEventListener('click', e =>
    setClosed(c, path, folder, e.currentTarget.dataset.part === 'close'));
}

function renderNode(node) {
  const frag = document.createDocumentFragment();
  const c = collections.find(x => x.id === selected);
  for (const [name, dir] of [...node.dirs.entries()].sort((a, b) => a[0].localeCompare(b[0], 'fr'))) {
    const open = openDirs.has(dir.path);
    const row = h(`
      <div class="k-row dir ${open ? 'open' : ''}">
        ${icon('chev-right', 'chev')}${icon('folder', 'kind-dir')}
        <span class="label">${esc(name)}</span><span class="faint">${countFiles(dir)}</span>
        ${closedControls(c, dir.path, true)}
        <button class="icon-btn danger" data-act="remove" title="${esc(t('library.remove_folder_hint'))}">${icon('trash')}</button>
      </div>`);
    row.onclick = e => {
      if (e.target.closest('.icon-btn')) return;
      open ? openDirs.delete(dir.path) : openDirs.add(dir.path);
      renderTree();
    };
    row.querySelector('[data-act="remove"]').onclick = () => removeFolder(dir.path, countFiles(dir));
    bindClosed(row, dir.path, true);
    frag.append(row);
    if (open) {
      const kids = h('<div class="k-children"></div>');
      kids.append(renderNode(dir));
      frag.append(kids);
    }
  }
  for (const { name, entry } of node.files.sort((a, b) => a.name.localeCompare(b.name, 'fr'))) {
    const row = h(`
      <div class="k-row" title="${esc(entry.origin || entry.source)}">
        <span style="width:14px"></span>${fileIcon(entry)}
        <span class="label">${esc(name)}</span><span class="faint">${esc(t('library.entry_passages', { n: entry.chunks }))}</span>
        ${closedControls(c, entry.source, false)}
        <button class="icon-btn danger" data-act="remove" title="${esc(t('library.remove_entry_hint'))}">${icon('trash')}</button>
      </div>`);
    row.querySelector('[data-act="remove"]').onclick = () => removeEntry(entry);
    bindClosed(row, entry.source, false);
    frag.append(row);
  }
  return frag;
}

async function removeEntry(entry) {
  try { await del(`/entries/${entry.id}`); } catch (e) { errorToast(e); }
}

async function removeFolder(path, count) {
  const ok = await confirmDanger(t('library.remove_folder_title'),
    t('library.remove_folder_text', { n: count, path }), t('library.remove'));
  if (!ok) return;
  try { await post('/entries/remove_prefix', { collection_id: selected, prefix: path }); } catch (e) { errorToast(e); }
}

// ------------------------------------------------------------------- ajouts
async function add(kind) {
  if (kind === 'urls') return addUrls();
  const paths = kind === 'files' ? await pickFiles() : await pickFolder();
  if (paths === null) return manualPaths();          // ouvert hors de la fenêtre native
  if (paths.length) launch(paths);
}

async function manualPaths() {
  const r = await modal({ title: t('library.paths_title'), iconName: 'folder',
    text: t('library.paths_text'),
    textarea: '', placeholder: t('library.paths_placeholder'), confirm: t('library.index') });
  if (r.ok) launch(r.value.split('\n').map(s => s.trim()).filter(Boolean));
}

async function launch(paths) {
  try {
    await post('/index', { paths, collection_id: selected });
    toast(t('library.index_started'), 'ok');
  } catch (e) { errorToast(e); }
}

async function addUrls() {
  const r = await modal({ title: t('library.urls_title'), iconName: 'globe',
    text: t('library.urls_text'),
    textarea: '', placeholder: 'https://…', confirm: t('library.add') });
  if (!r.ok) return;
  const urls = r.value.split('\n').map(s => s.trim()).filter(Boolean);
  if (!urls.length) return;
  try {
    await post('/index_urls', { urls, collection_id: selected });
    toast(t('library.urls_started'), 'ok');
  } catch (e) { errorToast(e); }
}

// ------------------------------------------------------- travaux en cours
function renderJobStrip() {
  const strip = $('#job-strip', root);
  if (!strip) return;
  const active = [...state.jobs.values()].filter(j =>
    ['queued', 'running', 'paused'].includes(j.status) && j.params?.collection_id === selected);
  strip.innerHTML = '';
  for (const j of active) {
    const processed = j.done + j.skipped + j.failed;
    const pct = j.total ? Math.round(100 * processed / j.total) : 0;
    const mini = h(`
      <div class="mini">
        <div class="top"><span>${esc(jobTitle(j))}</span><span class="muted">${esc(t('library.job_progress', { done: processed, total: j.total, pct }))}</span></div>
        <div class="bar"><i class="fill" style="width:${pct}%"></i></div>
      </div>`);
    mini.style.cursor = 'pointer';
    mini.title = t('library.see_in_tasks');
    mini.onclick = () => emit('view:show', 'jobs');
    strip.append(mini);
  }
}

// --------------------------------------------------------------------- mémoire
function renderMemory() {
  const card = $('#memory-card', root);
  if (!card) return;
  const enabled = !!state.settings?.conversations_enabled;
  const counts = memory
    ? `${t('library.memory_entries', { n: memory.entries })} · ${t('library.chunks_total', { n: memory.chunks })}` : '';
  card.innerHTML = `
    <h2>${icon('memory')}${esc(t('library.memory_title'))}</h2>
    <p class="hint">${esc(t(enabled ? 'library.memory_on' : 'library.memory_off'))}</p>
    <div class="muted" style="font-size:13px;margin-bottom:12px">${esc(counts)}</div>
    <button class="btn sm danger" id="memory-clear" ${memory?.chunks ? '' : 'disabled'}>${icon('trash')}${esc(t('library.memory_clear'))}</button>`;
  card.querySelector('#memory-clear').onclick = async () => {
    const ok = await confirmDanger(t('library.memory_clear'),
      t('library.memory_clear_text'), t('library.memory_clear_confirm'));
    if (!ok) return;
    try { await post('/memory/clear'); toast(t('library.memory_cleared'), 'ok'); } catch (e) { errorToast(e); }
  };
}
