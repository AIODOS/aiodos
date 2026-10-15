// Bibliothèques : collections, arborescence du contenu, ajouts, mémoire.
import { get, post, patch, del, pickFiles, pickFolder } from '../api.js';
import { $, h, esc, debounce } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, prefs, emit } from '../state.js';
import { ask, confirmDanger, menu, modal, toast, errorToast } from '../ui.js';

const KIND_FILTERS = [['all', 'Tout'], ['file', 'Fichiers'], ['folder', 'Dossiers'], ['url', 'Web']];
const IMAGE = /\.(jpe?g|png|gif|bmp|tiff?|webp|heic)$/i;
const MEDIA = /\.(mp3|wav|flac|aac|ogg|m4a|opus|mp4|mov|avi|mkv|webm|wmv|flv)$/i;

let root;
let collections = [];
let memory = null;
let entries = [];
let selected = prefs.get('library.selected', null);
let kindFilter = 'all';
const openDirs = new Set();

export function mountLibrary(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head">
        <div><h1>Bibliothèques</h1>
          <div class="sub">Tes documents, dossiers et pages web, indexés pour que l'IA s'en serve. Tout reste sur ta machine.</div></div>
        <div class="grow"></div>
        <button class="btn" id="lib-new">${icon('plus')}Nouvelle bibliothèque</button>
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
  $('#lib-new', root).onclick = createCollection;
  const reload = debounce(load, 600);
  on('knowledge:changed', reload);
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
  renderContent();
  renderMemory();
}

// ----------------------------------------------------------------- collections
function renderCollections() {
  const list = $('#coll-list', root);
  list.innerHTML = '';
  for (const c of collections) {
    const item = h(`
      <div class="coll ${c.id === selected ? 'active' : ''} ${c.enabled ? '' : 'disabled'}">
        ${icon('library')}
        <div class="name"><b>${esc(c.name)}</b><small>${c.entries} élément(s) · ${c.chunks} passages</small></div>
        <label class="switch" title="${c.enabled ? 'Utilisée dans les réponses' : 'Ignorée dans les réponses'}">
          <input type="checkbox" ${c.enabled ? 'checked' : ''}><span></span></label>
        <button class="icon-btn" title="Options">${icon('more')}</button>
      </div>`);
    item.onclick = e => {
      if (e.target.closest('.switch, .icon-btn')) return;
      selected = c.id;
      prefs.set('library.selected', selected);
      renderCollections();
      renderContent();
    };
    item.querySelector('input').onchange = async e => {
      try { await patch(`/collections/${c.id}`, { enabled: e.target.checked }); } catch (err) { errorToast(err); }
    };
    item.querySelector('.icon-btn').onclick = e => {
      e.stopPropagation();
      menu(e.currentTarget, [
        { label: 'Renommer', icon: 'edit', action: () => renameCollection(c) },
        { label: 'Supprimer', icon: 'trash', danger: true, action: () => deleteCollection(c) },
      ]);
    };
    list.append(item);
  }
  if (!collections.length) list.append(h('<div class="empty">Aucune bibliothèque.</div>'));
}

async function createCollection() {
  const name = await ask('Nouvelle bibliothèque', 'Nom :', '', 'ex. 2024, Sécurité, Projet X');
  if (!name) return;
  try {
    const { id } = await post('/collections', { name });
    selected = id;
    prefs.set('library.selected', id);
    load();
  } catch (e) { errorToast(e); }
}

async function renameCollection(c) {
  const name = await ask('Renommer la bibliothèque', 'Nouveau nom :', c.name);
  if (!name || name === c.name) return;
  try { await patch(`/collections/${c.id}`, { name }); } catch (e) { errorToast(e); }
}

async function deleteCollection(c) {
  const ok = await confirmDanger('Supprimer la bibliothèque',
    `« ${c.name} » et tout son contenu indexé (${c.entries} élément(s)) seront supprimés. Tes fichiers d'origine ne sont pas touchés.`);
  if (!ok) return;
  try { await del(`/collections/${c.id}`); } catch (e) { errorToast(e); }
}

// ------------------------------------------------------------------ contenu
function renderContent() {
  const box = $('#lib-content', root);
  const c = collections.find(x => x.id === selected);
  if (!c) {
    box.innerHTML = `<div class="empty">${icon('library')}<div>Crée une bibliothèque pour commencer.</div></div>`;
    return;
  }
  box.innerHTML = `
    <div class="toolbar">
      <h2 style="margin:0;flex:1">${esc(c.name)}</h2>
      <button class="btn sm" data-add="files">${icon('file')}Fichiers</button>
      <button class="btn sm" data-add="folder">${icon('folder')}Dossier</button>
      <button class="btn sm" data-add="urls">${icon('globe')}Pages web</button>
    </div>
    <div class="job-strip" id="job-strip"></div>
    <div class="toolbar">
      <div class="segmented">${KIND_FILTERS.map(([k, label]) =>
        `<button data-kind="${k}" class="${k === kindFilter ? 'on' : ''}">${label}</button>`).join('')}</div>
      <div style="flex:1"></div>
      <button class="btn ghost sm" data-tree="open">Tout déplier</button>
      <button class="btn ghost sm" data-tree="close">Tout replier</button>
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
    box.innerHTML = `<div class="empty">${icon('folder-plus')}<div>Rien ici pour l'instant.<br>
      Ajoute des fichiers, un dossier entier ou des pages web : PDF, Word, Excel, code, images, audio…</div></div>`;
    return;
  }
  box.append(renderNode(top));
}

function renderNode(node) {
  const frag = document.createDocumentFragment();
  for (const [name, dir] of [...node.dirs.entries()].sort((a, b) => a[0].localeCompare(b[0], 'fr'))) {
    const open = openDirs.has(dir.path);
    const row = h(`
      <div class="k-row dir ${open ? 'open' : ''}">
        ${icon('chev-right', 'chev')}${icon('folder', 'kind-dir')}
        <span class="label">${esc(name)}</span><span class="faint">${countFiles(dir)}</span>
        <button class="icon-btn danger" title="Retirer ce dossier de la bibliothèque">${icon('trash')}</button>
      </div>`);
    row.onclick = e => {
      if (e.target.closest('.icon-btn')) return;
      open ? openDirs.delete(dir.path) : openDirs.add(dir.path);
      renderTree();
    };
    row.querySelector('.icon-btn').onclick = () => removeFolder(dir.path, countFiles(dir));
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
        <span class="label">${esc(name)}</span><span class="faint">${entry.chunks} passage(s)</span>
        <button class="icon-btn danger" title="Retirer de la bibliothèque">${icon('trash')}</button>
      </div>`);
    row.querySelector('.icon-btn').onclick = () => removeEntry(entry);
    frag.append(row);
  }
  return frag;
}

async function removeEntry(entry) {
  try { await del(`/entries/${entry.id}`); } catch (e) { errorToast(e); }
}

async function removeFolder(path, count) {
  const ok = await confirmDanger('Retirer le dossier',
    `Les ${count} élément(s) de « ${path} » seront retirés de la bibliothèque (tes fichiers ne sont pas touchés).`, 'Retirer');
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
  const r = await modal({ title: 'Chemins à indexer', iconName: 'folder',
    text: 'Colle un chemin complet par ligne (fichier ou dossier).',
    textarea: '', placeholder: 'C:\\Users\\moi\\Documents\\Projet', confirm: 'Indexer' });
  if (r.ok) launch(r.value.split('\n').map(s => s.trim()).filter(Boolean));
}

async function launch(paths) {
  try {
    await post('/index', { paths, collection_id: selected });
    toast('Indexation lancée : la progression s\'affiche ici et dans Tâches.', 'ok');
  } catch (e) { errorToast(e); }
}

async function addUrls() {
  const r = await modal({ title: 'Ajouter des pages web', iconName: 'globe',
    text: 'Une adresse par ligne. Les pages sont téléchargées puis analysées.',
    textarea: '', placeholder: 'https://…', confirm: 'Ajouter' });
  if (!r.ok) return;
  const urls = r.value.split('\n').map(s => s.trim()).filter(Boolean);
  if (!urls.length) return;
  try {
    await post('/index_urls', { urls, collection_id: selected });
    toast('Lecture des pages lancée.', 'ok');
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
        <div class="top"><span>${esc(j.title)}</span><span class="muted">${processed}/${j.total} · ${pct} %</span></div>
        <div class="bar"><i class="fill" style="width:${pct}%"></i></div>
      </div>`);
    mini.style.cursor = 'pointer';
    mini.title = 'Voir dans Tâches';
    mini.onclick = () => emit('view:show', 'jobs');
    strip.append(mini);
  }
}

// --------------------------------------------------------------------- mémoire
function renderMemory() {
  const card = $('#memory-card', root);
  if (!card) return;
  const enabled = !!state.settings?.conversations_enabled;
  card.innerHTML = `
    <h2>${icon('memory')}Mémoire des conversations</h2>
    <p class="hint">${enabled
      ? "Active : l'IA peut s'appuyer sur tes autres conversations."
      : "Désactivée : chaque conversation reste isolée."}</p>
    <div class="muted" style="font-size:13px;margin-bottom:12px">${memory ? `${memory.entries} conversation(s) mémorisée(s) · ${memory.chunks} passages` : ''}</div>
    <button class="btn sm danger" id="memory-clear" ${memory?.chunks ? '' : 'disabled'}>${icon('trash')}Vider la mémoire</button>`;
  card.querySelector('#memory-clear').onclick = async () => {
    const ok = await confirmDanger('Vider la mémoire',
      'La mémoire inter-conversations sera effacée. Les conversations elles-mêmes sont conservées.', 'Vider');
    if (!ok) return;
    try { await post('/memory/clear'); toast('Mémoire vidée.', 'ok'); } catch (e) { errorToast(e); }
  };
}
