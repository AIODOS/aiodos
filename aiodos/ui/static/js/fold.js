// Sections repliables, communes à toutes les vues : un titre d'une ligne (et un résumé à côté),
// le contenu à l'ouverture. L'état ouvert ou fermé de chacune est retenu d'une fois sur l'autre.
import { esc } from './dom.js';
import { icon } from './icons.js';
import { prefs } from './state.js';

/** Le HTML d'une section : id (pour retenir son état), titre, icône, résumé, contenu. */
export function fold({ id, title, iconName = '', sub = '', body = '', open = false, cls = '' }) {
  const opened = prefs.get(`fold.${id}`, open);
  return `<details class="fold ${cls}" data-fold="${esc(id)}"${opened ? ' open' : ''}>
    <summary>${iconName ? icon(iconName) : ''}<span class="fold-title">${esc(title)}</span>
      <span class="fold-sub">${sub}</span><span class="fold-chev">${icon('chev-right')}</span></summary>
    <div class="fold-body">${body}</div></details>`;
}

/** Retient l'état des sections de `root` quand on les ouvre ou les ferme. */
export function keepFolds(root) {
  root.querySelectorAll('details.fold[data-fold]').forEach(d => {
    if (d.dataset.kept) return;
    d.dataset.kept = '1';
    d.addEventListener('toggle', () => prefs.set(`fold.${d.dataset.fold}`, d.open));
  });
}

/** Ouvre une section (et la montre), par exemple depuis un lien d'une autre vue. */
export function openFold(root, id) {
  const d = root?.querySelector(`details.fold[data-fold="${CSS.escape(id)}"]`);
  if (!d) return;
  d.open = true;
  d.scrollIntoView({ block: 'start' });
}
