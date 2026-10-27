// Bibliothèques : deux onglets, les documents (library.js) et les images et vidéos créées
// (images.js). « Images » a quitté la barre du bas le 27/10/2026 : c'est la vue « images »,
// montrée dans cette section, et la barre allume alors « Bibliothèques ».
import { esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { on, emit, prefs } from '../state.js';

const TABS = [['library', 'library', 'nav.library_docs'], ['images', 'image', 'nav.images_title']];

/** Les onglets dans `section` → { library, images } : où monter chaque vue. */
export function mountLibraryTabs(section) {
  section.classList.add('tabbed');
  section.innerHTML = `<div class="view-tabs" role="tablist">${TABS.map(([view, name, label]) => `
      <button class="view-tab" role="tab" data-tab="${view}">${icon(name)}${esc(t(label))}</button>`).join('')}</div>
    ${TABS.map(([view]) => `<div class="view-pane" data-pane="${view}" hidden></div>`).join('')}`;
  section.querySelectorAll('[data-tab]').forEach(b => { b.onclick = () => emit('view:show', b.dataset.tab); });
  on('view:shown', view => {
    if (!TABS.some(([v]) => v === view)) return;
    prefs.set('libraryTab', view);
    section.querySelectorAll('[data-tab]').forEach(b => {
      b.classList.toggle('active', b.dataset.tab === view);
      b.setAttribute('aria-selected', b.dataset.tab === view);
    });
    section.querySelectorAll('[data-pane]').forEach(p => { p.hidden = p.dataset.pane !== view; });
  });
  return Object.fromEntries(TABS.map(([view]) => [view, section.querySelector(`[data-pane="${view}"]`)]));
}

/** L'onglet ouvert la dernière fois (« Bibliothèques » touché dans la barre du bas). */
export const lastTab = () => prefs.get('libraryTab', 'library');
