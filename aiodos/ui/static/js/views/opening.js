// Mes bibliothèques ouvertes à la consultation (knowledge/visibility.py) : qui peut y
// chercher (personne, mes machines, mes amis), et les dossiers ou documents fermés.
import { patch, post } from '../api.js';
import { esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { modal, toast, errorToast } from '../ui.js';

// Du plus fermé au plus ouvert : seul un élargissement demande confirmation. Libellés et
// messages : clés de opening.json, traduites à l'affichage (ask : qui pourra chercher)
const LEVELS = [
  { value: 'none', label: 'opening.level_none', icon: 'eye-off', now: 'opening.now_none',
    done: 'opening.done_none' },
  { value: 'mine', label: 'opening.level_mine', icon: 'eye', now: 'opening.now_mine',
    ask: 'opening.ask_mine', done: 'opening.done_mine' },
  { value: 'friends', label: 'opening.level_friends', icon: 'eye', now: 'opening.now_friends',
    ask: 'opening.ask_friends', done: 'opening.done_friends' },
];
const rank = value => LEVELS.findIndex(l => l.value === (value || 'none'));

/** Icône d'une bibliothèque ouverte, dans la liste des bibliothèques. */
export function openBadge(c) {
  if (c.local_only || !c.open_to || c.open_to === 'none') return '';
  const title = t(c.open_to === 'friends' ? 'opening.badge_friends' : 'opening.badge_mine');
  return `<span class="open-to" title="${esc(title)}">${icon('eye')}</span>`;
}

/** Entrées du menu ⋯ d'une bibliothèque. */
export function openingMenu(c) {
  if (c.local_only) return [{ title: t('opening.local_only_menu') }];
  const current = LEVELS[rank(c.open_to)];
  return [{ title: t(current.now) },
    ...LEVELS.filter(l => l !== current).map(l => ({
      label: t(l.label), icon: l.icon, action: () => setOpenTo(c, l.value) }))];
}

async function setOpenTo(c, value) {
  const level = LEVELS.find(l => l.value === value);
  if (rank(value) > rank(c.open_to)) {             // plus ouverte qu'avant : on explique d'abord
    const r = await modal({
      title: t('opening.open_title', { name: c.name }), iconName: 'eye', confirm: t('opening.open'),
      text: `${t(level.ask)} ${t('opening.what_is_seen')}`,
    });
    if (!r.ok) return;
  }
  try {
    await patch(`/collections/${c.id}`, { open_to: value });
    toast(t(level.done, { name: c.name }), 'ok', 7000);
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
  if (state?.by) return mark(t('opening.closed_by', { folder: state.by }));
  if (state?.self) {
    return mark(t('opening.closed_self'))
      + `<button class="icon-btn" data-part="open" title="${esc(t('opening.reopen'))}">${icon('eye')}</button>`;
  }
  return `<button class="icon-btn" data-part="close" title="${esc(t('opening.close_hint'))}">${icon('eye-off')}</button>`;
}

export async function setClosed(c, path, folder, closed) {
  try {
    await post(`/collections/${c.id}/closed`, { path, folder, closed });
    toast(t(closed ? 'opening.closed_done' : 'opening.reopened_done', { path }), 'ok', 6000);
  } catch (e) { errorToast(e); }
}
