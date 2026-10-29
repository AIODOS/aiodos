// Les dossiers suivis d'une bibliothèque (knowledge/folders.py) : chacun avec son chemin, ses
// fichiers indexés, en attente, en échec ou non lus, sa dernière lecture ; « Relire » (les
// fichiers nouveaux ou modifiés, et ceux en échec) et « Retirer » (le dossier et ses passages,
// après confirmation). Le bouton « Dossier » de la bibliothèque reste celui de l'ajout ; cette
// liste se voit à côté, au-dessus du contenu.
import { get, post, del } from '../api.js';
import { ago, esc } from '../dom.js';
import { t, tt } from '../i18n.js';
import { icon } from '../icons.js';
import { confirmDanger, toast, errorToast } from '../ui.js';
import { openEmbedSetting } from './embed-model.js';

const TYPES_SHOWN = 4;               // extensions non prises en charge citées, au plus
let folders = [];
const opened = new Set();            // dossiers dont la liste des fichiers non lus est ouverte

export const watchedFolders = () => folders;

/** Les dossiers suivis de cette bibliothèque, relus au nœud. */
export async function loadFolders(collectionId) {
  if (collectionId == null) { folders = []; return; }
  try {
    folders = (await get(`/library_folders?collection_id=${encodeURIComponent(collectionId)}`)).folders;
  } catch (e) { errorToast(e); }
}

const name = f => f.path.split(/[\\/]/).filter(Boolean).at(-1) || f.path;

function typesText(types) {
  const all = Object.entries(types || {});
  const shown = all.slice(0, TYPES_SHOWN).map(([ext, n]) => `${ext} ×${n}`).join(', ');
  return all.length > TYPES_SHOWN ? `${shown}…` : shown;
}

function countsText(f) {
  const c = f.counts;
  const parts = [t('library_folders.indexed', { n: c.indexed })];
  if (c.pending) parts.push(t('library_folders.pending', { n: c.pending }));
  if (c.failed) parts.push(t('library_folders.failed', { n: c.failed }));
  if (c.skipped) parts.push(t('library_folders.unread', { n: c.skipped }));
  if (c.cancelled) parts.push(t('library_folders.cancelled', { n: c.cancelled }));
  parts.push(f.scanned ? t('library_folders.scanned', { when: ago(f.scanned) })
    : t('library_folders.never_scanned'));
  return parts.join(' · ');
}

// Ce qui demande l'attention : hors ligne, rien de lisible, modèle d'embeddings absent
function notes(f) {
  const out = [];
  if (!f.online) out.push(['warn', t('library_folders.offline')]);
  if (f.online && f.scanned && !f.files) out.push(['warn', t('library_folders.no_readable')]);
  if (f.ignored) {
    out.push(['faint', t('library_folders.ignored', { n: f.ignored, types: typesText(f.ignored_types) })]);
  }
  if (f.blocked) out.push(['warn', tt(f.blocked.message), true]);
  return out;
}

/** La liste dans `box` (vide sans dossier suivi). */
export function renderFolders(box) {
  if (!box) return;
  box.innerHTML = '';
  if (!folders.length) { box.hidden = true; return; }
  box.hidden = false;
  box.insertAdjacentHTML('beforeend', `<div class="lib-folders-head"><h3>${icon('folder')}${esc(t('library_folders.title'))}</h3>
    <span class="faint">${esc(t('library_folders.hint'))}</span></div>`);
  for (const f of folders) {
    const bad = f.counts.failed + f.counts.skipped;
    const row = document.createElement('div');
    row.className = `lib-folder${f.online ? '' : ' offline'}`;
    row.innerHTML = `
      <div class="lib-folder-main">
        <b title="${esc(f.path)}">${esc(f.path)}</b>
        <small>${esc(countsText(f))}</small>
        ${notes(f).map(([cls, text, fix]) => `<small class="${cls}">${esc(text)}${fix
          ? ` <button class="btn sm ghost" data-fix>${esc(t('embed_model.fix'))}</button>` : ''}</small>`).join('')}
        ${bad ? `<button class="link-btn" data-problems>${icon('chev-right', opened.has(f.id) ? 'chev open' : 'chev')}${esc(t('library_folders.problems'))}</button>
          <div class="lib-folder-problems" ${opened.has(f.id) ? '' : 'hidden'}></div>` : ''}
      </div>
      <div class="lib-folder-actions">
        <button class="btn sm" data-rescan title="${esc(t('library_folders.rescan_hint'))}">${icon('retry')}${esc(t('library_folders.rescan'))}</button>
        <button class="btn sm danger" data-remove title="${esc(t('library_folders.remove_hint'))}">${icon('trash')}${esc(t('library_folders.remove'))}</button>
      </div>`;
    row.querySelector('[data-rescan]').onclick = () => rescan(f);
    row.querySelector('[data-remove]').onclick = () => removeFolder(f);
    row.querySelector('[data-fix]')?.addEventListener('click', openEmbedSetting);
    const toggle = row.querySelector('[data-problems]');
    if (toggle) {
      const list = row.querySelector('.lib-folder-problems');
      toggle.onclick = () => {
        const open = list.hidden;
        list.hidden = !open;
        toggle.querySelector('.chev')?.classList.toggle('open', open);
        if (open) { opened.add(f.id); showProblems(f, list); } else opened.delete(f.id);
      };
      if (opened.has(f.id)) showProblems(f, list);
    }
    box.append(row);
  }
}

async function showProblems(f, list) {
  let files;
  try { files = (await get(`/library_folders/${f.id}/problems`)).files; } catch (e) { errorToast(e); return; }
  const total = f.counts.failed + f.counts.skipped;
  list.innerHTML = files.map(p => `<div class="lib-problem ${p.state}"><span>${esc(p.file)}</span>
    <small>${esc(p.reason)}</small></div>`).join('')
    + (total > files.length ? `<div class="faint">${esc(t('library_folders.problems_more'))}</div>` : '');
}

/** Ce que dit l'ajout d'un dossier (réponse de POST /index) : fichiers à indexer, déjà suivi,
 *  ou aucun fichier lisible (avec les extensions non prises en charge). */
export function announceAdded(found) {
  for (const f of found || []) {
    const n = f.planned || 0;
    if (f.busy) {
      toast(t('library_folders.rescan_busy', { name: name(f) }));
    } else if (!f.files && !n) {
      const why = f.ignored ? ` ${t('library_folders.ignored', { n: f.ignored, types: typesText(f.ignored_types) })}` : '';
      toast(t('library_folders.added_empty', { name: name(f) }) + why, 'err', 9000);
    } else if (n) {
      toast(t('library_folders.added', { name: name(f), n }), 'ok', 6000);
    } else {
      toast(t('library_folders.added_again', { name: name(f) }), 'ok');
    }
  }
}

async function rescan(f) {
  let r;
  try { r = await post(`/library_folders/${f.id}/rescan`); } catch (e) { errorToast(e); return; }
  if (r.busy) toast(t('library_folders.rescan_busy', { name: name(f) }));
  else if (r.offline) toast(t('library_folders.rescan_offline', { name: name(f) }), 'err', 7000);
  else if (r.planned) toast(t('library_folders.rescan_started', { name: name(f), n: r.planned }), 'ok');
  else toast(t('library_folders.rescan_nothing', { name: name(f) }), 'ok');
}

export async function removeFolder(f) {
  const ok = await confirmDanger(t('library_folders.remove_title'),
    t('library_folders.remove_text', { path: f.path }), t('library_folders.remove'));
  if (!ok) return;
  try {
    await del(`/library_folders/${f.id}`);
    toast(t('library_folders.removed'), 'ok');
  } catch (e) { errorToast(e); }
}
