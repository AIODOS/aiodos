// Mes bibliothèques ouvertes à la consultation (knowledge/visibility.py) : qui peut y
// chercher (personne, mes machines, mes amis), et les dossiers ou documents fermés.
import { patch, post } from '../api.js';
import { esc } from '../dom.js';
import { icon } from '../icons.js';
import { modal, toast, errorToast } from '../ui.js';

const WHAT_IS_SEEN = 'Elles verront le texte des passages trouvés, quelques-uns par recherche, le nom du '
  + 'document (sans ses dossiers) et celui de la bibliothèque ; pas tes fichiers eux-mêmes, ni leur emplacement '
  + 'sur ce PC. Mais de recherche en recherche, une bibliothèque ouverte peut se lire passage par passage. '
  + 'Chaque recherche est notée dans la vue Réseau.\n\n'
  + 'Pour cacher un dossier ou un document, ferme-le d\'abord (bouton œil barré, au survol dans le contenu).';

// Du plus fermé au plus ouvert : seul un élargissement demande confirmation
const LEVELS = [
  { value: 'none', label: 'Fermer à la consultation', icon: 'eye-off', now: 'fermée',
    done: name => `Plus aucune autre machine ne peut chercher dans « ${name} ».` },
  { value: 'mine', label: 'Consultable par mes machines', icon: 'eye', now: 'mes machines',
    who: 'Tes machines',
    done: name => `Seules tes machines peuvent chercher dans « ${name} ».` },
  { value: 'friends', label: 'Consultable par mes machines et mes amis', icon: 'eye',
    now: 'mes machines et mes amis', who: 'Tes machines et celles de tes amis',
    done: name => `Tes machines et tes amis peuvent chercher dans « ${name} ».` },
];
const rank = value => LEVELS.findIndex(l => l.value === (value || 'none'));

/** Icône d'une bibliothèque ouverte, dans la liste des bibliothèques. */
export function openBadge(c) {
  if (c.local_only || !c.open_to || c.open_to === 'none') return '';
  const who = c.open_to === 'friends' ? 'tes machines et tes amis : ils' : 'tes machines : elles';
  return `<span class="open-to" title="Consultable par ${who} peuvent y chercher des passages, `
    + `sans copier tes fichiers">${icon('eye')}</span>`;
}

/** Entrées du menu ⋯ d'une bibliothèque. */
export function openingMenu(c) {
  if (c.local_only) return [{ title: 'Gardée sur ce PC : jamais consultable' }];
  const current = LEVELS[rank(c.open_to)];
  return [{ title: `Consultation : ${current.now}` },
    ...LEVELS.filter(l => l !== current).map(l => ({
      label: l.label, icon: l.icon, action: () => setOpenTo(c, l.value) }))];
}

async function setOpenTo(c, value) {
  const level = LEVELS.find(l => l.value === value);
  if (rank(value) > rank(c.open_to)) {             // plus ouverte qu'avant : on explique d'abord
    const r = await modal({
      title: `Ouvrir « ${c.name} » à la consultation ?`, iconName: 'eye', confirm: 'Ouvrir',
      text: `${level.who} pourront chercher dans cette bibliothèque depuis leurs conversations, `
        + `sans copier tes fichiers. ${WHAT_IS_SEEN}`,
    });
    if (!r.ok) return;
  }
  try {
    await patch(`/collections/${c.id}`, { open_to: value });
    toast(level.done(c.name), 'ok', 7000);
  } catch (e) { errorToast(e); }
}

/** Ce chemin est-il fermé ? { self } s'il l'est lui-même, { by: dossier } s'il l'est avec
 *  un dossier, sinon null (même règle que visibility._CLOSED). */
export function closedState(c, path, folder) {
  const parts = c?.closed || [];
  if (parts.some(p => p.path === path && p.folder === folder)) return { self: true };
  const by = parts.find(p => p.folder && (path.startsWith(`${p.path}/`) || (!folder && p.path === path)));
  return by ? { by: by.path } : null;
}

/** Marque et bouton d'une ligne du contenu (dossier ou document). */
export function closedControls(c, path, folder) {
  const state = closedState(c, path, folder);
  const mark = text => `<span class="closed-mark" title="${esc(text)}">${icon('eye-off')}</span>`;
  if (state?.by) return mark(`Fermé à la consultation avec le dossier « ${state.by} »`);
  if (state?.self) {
    return mark('Fermé à la consultation : jamais montré aux recherches des autres machines')
      + `<button class="icon-btn" data-part="open" title="Rouvrir à la consultation">${icon('eye')}</button>`;
  }
  return `<button class="icon-btn" data-part="close" title="Fermer à la consultation : jamais montré aux `
    + `recherches des autres machines">${icon('eye-off')}</button>`;
}

export async function setClosed(c, path, folder, closed) {
  try {
    await post(`/collections/${c.id}/closed`, { path, folder, closed });
    toast(closed
      ? `« ${path} » est fermé à la consultation : aucune recherche venue d'une autre machine ne le verra.`
      : `« ${path} » suit de nouveau le réglage de la bibliothèque.`, 'ok', 6000);
  } catch (e) { errorToast(e); }
}
