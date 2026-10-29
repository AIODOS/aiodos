// Le choix du modèle (docs/conception/modeles-auto.md § 4) : un seul bouton, qui dit le genre en
// cours et son choix (« Texte · Automatique », « Image · Z-Image Turbo »), et son panneau à
// quatre onglets, Texte · Images · Vidéos · Audio. Chaque onglet commence par « Automatique » (le
// choix par défaut : le plus puissant disponible au moment d'envoyer, avec ce qu'il prendrait en
// ce moment), puis les modèles, du plus puissant au plus modeste, avec où ils peuvent tourner
// (ici, mes machines, mes amis, le réseau public) et ⚠ pour une licence restrictive ; ceux qui ne
// sont nulle part, « à télécharger », mènent à Réglages → Modèles (jamais un téléchargement
// direct). L'onglet Audio a trois parties, Voix, Musique et Effets sonores (audio/ranking.py,
// « genres ») : chacune son « Automatique » et ses modèles ; un choix y dit son genre.
// Les données : GET /models/menu (chat/model_menu.py), lues à l'ouverture et gardées pour tous
// les panneaux. Le même panneau sert sous la saisie (chat.js : le texte et le mode « Créer » de
// chat-create.js) et dans Réglages → Modèles pour les choix par défaut (settings-models.js).
// Au clavier : flèches entre les onglets et entre les modèles, Entrée pour choisir, Échap pour
// fermer ; sur un écran étroit (téléphone), il s'ouvre en bas de l'écran.
import { get, post } from '../api.js';
import { h, esc, bytes } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { on, emit } from '../state.js';
import { isPhone } from './model-install.js';
import { errorToast, modal } from '../ui.js';

export const KINDS = ['text', 'image', 'video', 'audio'];
const ICONS = { text: 'chat', image: 'image', video: 'video', audio: 'audio' };
const MIB = 1048576;          // « vram » arrive en Mio, « size » en octets

const cache = new Map();      // « Sur mes machines seulement » (vrai, faux) → le dernier menu lu
const reading = new Map();    // les lectures en cours, de même
const instances = new Set();  // les boutons montés : leur libellé suit chaque lecture
let active = null;            // le panneau ouvert (un seul à la fois)
let made = 0;

/** Le menu lu (null tant qu'il ne l'a pas été) ; mine : « Sur mes machines seulement ». */
export function menuData(mine = false) {
  return cache.get(!!mine) || null;
}

/** Relit le menu (une lecture à la fois) ; les boutons montés suivent. */
export function loadMenu(mine = false) {
  const key = !!mine;
  if (!reading.has(key)) {
    reading.set(key, get(`/models/menu?mine_only=${key}`)
      .then(data => {
        cache.set(key, data);
        for (const menu of [...instances]) {
          if (menu.root.isConnected) menu.refresh(); else instances.delete(menu);
        }
        return data;
      })
      .finally(() => reading.delete(key)));
  }
  return reading.get(key);
}

// Les modèles ont changé (installés, téléchargés, une machine partie, un réglage) : relus tout
// de suite si un panneau est ouvert, sinon à sa prochaine ouverture (chacune relit le menu)
const changed = () => active?.load();
on('models:loaded', changed);
on('images:changed', changed);
on('image-models:loaded', changed);
on('settings:loaded', changed);

// Échap ferme le panneau ouvert, avant tout le reste (une réponse en cours continue, une
// modification aussi) ; un clic ailleurs aussi
document.addEventListener('keydown', e => {
  if (!active || e.key !== 'Escape') return;
  e.preventDefault();
  e.stopPropagation();
  active.close(true);
}, true);
document.addEventListener('pointerdown', e => {
  if (active && !active.root.contains(e.target)) active.close(false);
});

/** Réglages → Modèles : un modèle à télécharger s'y télécharge (jamais d'ici). */
function showModels() {
  emit('view:show', 'settings');
  emit('settings:focus', 'modeles');
}

/** Où un modèle peut tourner : « ici, PC fixe, Léa (ami), réseau public ». */
function whereText(m) {
  const friend = t('models.peer_friend', { name: '' }).trim();
  const names = (m.where || []).map(w => (w.circle === 'here' ? t('model_menu.here')
    : w.circle === 'public' ? t('model_menu.public')
    : w.circle === 'friend' && !String(w.name).endsWith(friend) ? t('models.peer_friend', { name: w.name })
    : w.name));
  return [m.split ? t('model_menu.split') : '', [...new Set(names)].join(', ')].filter(Boolean).join(' · ');
}

/** « → qwen3.5:9b · PC fixe », ce que prendrait « Automatique ». */
function autoText(auto) {
  if (!auto?.model) return t('model_menu.auto_nothing');
  const machine = auto.where === 'here' ? t('model_menu.here')
    : auto.where === 'public' ? t('model_menu.public') : auto.machine || '';
  return t('model_menu.auto_takes', { model: auto.label || auto.model, machine });
}

function warn(m) {
  if (!m.restricted) return '';
  const text = m.notice || t('model_menu.restricted', { license: m.license || '' });
  return ` <span class="mm-warn" title="${esc(text)}">⚠</span>`;
}

function radio({ id = '', label, small = '', checked, extra = '', title = '', genre = '' }) {
  return `<button type="button" class="mm-item${id ? '' : ' mm-auto'}" role="radio" aria-checked="${checked}"
      data-id="${esc(id)}"${genre ? ` data-genre="${esc(genre)}"` : ''}${title ? ` title="${esc(title)}"` : ''}>
      <span class="mm-mark" aria-hidden="true"></span>
      <span class="mm-txt"><b>${label}</b>${small ? `<small>${esc(small)}</small>` : ''}</span>${extra}</button>`;
}

function downloadRow(m) {
  const d = m.download || {};
  const phone = isPhone();
  // La mémoire demandée, si elle n'est pas simplement la taille des fichiers (un modèle de texte)
  const vram = d.vram && Math.abs(d.vram * MIB - (d.size || 0)) > MIB ? d.vram * MIB : 0;
  const bits = [d.size ? t('model_menu.download_size', { size: bytes(d.size) }) : t('model_menu.to_download'),
    vram ? t('model_menu.needs_vram', { size: bytes(vram) }) : '',
    d.fits_here === true ? t(phone ? 'model_menu.fits_phone' : 'model_menu.fits_pc')
      : d.fits_here === false ? t(phone ? 'model_menu.too_big_phone' : 'model_menu.too_big_pc') : '']
    .filter(Boolean).join(' · ');
  return `<button type="button" class="mm-item mm-dl" data-download="${esc(m.id)}">${icon('download', 'mm-mark-ico')}
      <span class="mm-txt"><b>${esc(m.label)}${warn(m)}</b><small>${esc(bits)}</small></span>${icon('chev-right', 'mm-go')}</button>`;
}

function suggestRow(s) {
  const text = t(isPhone() ? 'model_menu.suggest_phone' : 'model_menu.suggest_pc',
    { model: s.label || s.model, size: bytes(s.vram ? s.vram * MIB : s.size || 0) });
  return `<button type="button" class="mm-item mm-suggest" data-download="${esc(s.model)}">${icon('download', 'mm-mark-ico')}
      <span class="mm-txt"><small>${esc(text)}</small></span>${icon('chev-right', 'mm-go')}</button>`;
}

/**
 * Un bouton « Modèle » et son panneau. options :
 * - kindNow() : le genre en cours ('text', 'image', 'video', 'audio') : le bouton le dit, le
 *   panneau s'ouvre sur son onglet ;
 * - chosen(kind, genre) : l'identifiant choisi pour ce genre, null pour « Automatique » (l'audio :
 *   genre, 'voice', 'music' ou 'sound') ;
 * - genre() : le genre d'audio en cours (le bouton le dit : « Voix · Automatique ») ;
 * - pick(kind, id, model) : un choix (id null : « Automatique » ; l'audio : model.genre dit le
 *   genre, { genre } pour son « Automatique ») ;
 * - mineOnly() : « Sur mes machines seulement » (rien du réseau public) ;
 * - name(kind, id) : le nom d'un identifiant que le menu ne connaît pas (encore) ;
 * - same(kind, a, b) : deux identifiants désignent-ils le même modèle ? (texte : la machine
 *   choisie à côté ne compte pas) ;
 * - prefix : le bouton dit le genre (« Texte · ») ; drop : 'up' (sous la saisie) ou 'down'.
 * → { root, refresh(), open(kind), close(focus), load() }
 */
export function modelMenu(options = {}) {
  const o = { kindNow: () => 'text', chosen: () => null, pick: () => {}, mineOnly: () => false,
    genre: () => 'voice',
    name: (kind, id) => id || '', same: (kind, a, b) => a === b, prefix: true, drop: 'up',
    kinds: KINDS, ...options };
  const uid = `mm${++made}`;
  const root = h(`
    <div class="mm${o.drop === 'down' ? ' down' : ''}">
      <button class="mm-btn" type="button" aria-haspopup="dialog" aria-expanded="false" aria-controls="${uid}"
        title="${esc(t('model_menu.button_title'))}"><span class="mm-ico"></span><span class="mm-label"></span>${icon('chev-right', 'mm-chev')}</button>
      <div class="mm-backdrop" hidden></div>
      <div class="mm-panel" id="${uid}" role="dialog" aria-label="${esc(t('model_menu.panel'))}" hidden>
        <div class="mm-head">
          <div class="mm-tabs" role="tablist">${o.kinds.map(k => `
            <button type="button" class="mm-tab" role="tab" id="${uid}-${k}" data-kind="${k}" aria-controls="${uid}-body"
              aria-selected="false" tabindex="-1">${icon(ICONS[k])}<span>${esc(t(`model_menu.tab_${k}`))}</span></button>`).join('')}</div>
          <button type="button" class="icon-btn mm-close" title="${esc(t('model_menu.close'))}"
            aria-label="${esc(t('model_menu.close'))}">${icon('x')}</button>
        </div>
        <div class="mm-body" id="${uid}-body" role="tabpanel"></div>
      </div>
    </div>`);
  const button = root.querySelector('.mm-btn');
  const panel = root.querySelector('.mm-panel');
  const backdrop = root.querySelector('.mm-backdrop');
  const body = root.querySelector('.mm-body');
  const tabs = [...root.querySelectorAll('.mm-tab')];
  let tab = null;
  let failure = null;

  const part = () => menuData(o.mineOnly())?.[tab] || null;

  /** L'onglet Audio : une partie par genre (voix, musique, effets), chacune avec son
   *  « Automatique » et ses modèles prêts ; ceux à télécharger, ensemble, en bas. */
  function audioHtml(found) {
    const later = (found.models || []).filter(m => !m.ready);
    const genres = found.genres || [{ genre: 'voice', auto: found.auto, suggest: found.suggest }];
    return genres.map(g => {
      const current = o.chosen('audio', g.genre);
      const ready = (found.models || []).filter(m => m.ready && (m.genre || 'voice') === g.genre);
      const auto = radio({ label: esc(t('model_menu.automatic')), small: autoText(g.auto), genre: g.genre,
        checked: !current, title: t('model_menu.auto_title_audio'),
        extra: `<span class="pill ok mm-pill">${esc(t('model_menu.recommended'))}</span>` });
      const models = ready.map(m => radio({ id: m.id, label: esc(m.label) + warn(m), small: whereText(m),
        checked: !!current && o.same('audio', m.id, current) }));
      const suggest = g.suggest && later.some(m => m.id === g.suggest.model) ? suggestRow(g.suggest) : '';
      // model_menu.genre_voice, model_menu.genre_music, model_menu.genre_sound
      const name = t(`model_menu.genre_${g.genre}`);
      return `<div class="mm-sub">${esc(name)}</div>
        <div class="mm-list" role="radiogroup" aria-label="${esc(t('model_menu.list_label', { kind: name }))}">
        ${auto}${models.join('')}</div>${suggest}`;
    }).join('') + (later.length
      ? `<div class="mm-sub">${esc(t('model_menu.to_download'))}</div>${later.map(downloadRow).join('')}` : '');
  }

  function listHtml(kind, found) {
    if (kind === 'audio' && !found.models?.length) {
      return `<div class="mm-empty">${icon('audio')}<span>${esc(t('model_menu.no_audio'))}</span></div>`;
    }
    if (kind === 'audio') return audioHtml(found);
    const current = o.chosen(kind);
    const ready = (found.models || []).filter(m => m.ready);
    const later = (found.models || []).filter(m => !m.ready);
    const auto = radio({ label: esc(t('model_menu.automatic')), small: autoText(found.auto),
      checked: !current, title: t('model_menu.auto_title'),
      extra: `<span class="pill ok mm-pill">${esc(t('model_menu.recommended'))}</span>` });
    const models = ready.map(m => radio({ id: m.id, label: esc(m.label) + warn(m), small: whereText(m),
      checked: !!current && o.same(kind, m.id, current) }));
    const suggest = found.suggest && later.some(m => m.id === found.suggest.model) ? suggestRow(found.suggest) : '';
    return `<div class="mm-list" role="radiogroup" aria-label="${esc(t('model_menu.list_label', { kind: t(`model_menu.tab_${kind}`) }))}">
        ${auto}${models.join('')}</div>${suggest}
      ${later.length ? `<div class="mm-sub">${esc(t('model_menu.to_download'))}</div>${later.map(downloadRow).join('')}` : ''}`;
  }

  function paint() {
    for (const b of tabs) {
      const on = b.dataset.kind === tab;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    body.setAttribute('aria-labelledby', `${uid}-${tab}`);
    const found = part();
    body.innerHTML = found ? listHtml(tab, found)
      : `<div class="mm-empty">${esc(failure ? t('model_menu.unreadable', { error: failure.message }) : t('model_menu.loading'))}</div>`;
  }

  /** Redessine sans perdre la place du clavier (le même modèle, ou l'onglet). */
  function repaint() {
    const focused = document.activeElement;
    const inBody = body.contains(focused);
    const id = inBody ? focused.closest('.mm-item')?.dataset : null;
    paint();
    if (!inBody) return;
    const again = id?.download !== undefined
      ? body.querySelector(`[data-download="${CSS.escape(id.download)}"]`)
      : body.querySelector(`[data-id="${CSS.escape(id?.id || '')}"]`);
    (again || checkedItem() || tabs.find(b => b.dataset.kind === tab))?.focus();
  }

  const checkedItem = () => body.querySelector('.mm-item[aria-checked="true"]') || body.querySelector('.mm-item');

  function load() {
    loadMenu(o.mineOnly()).then(() => { failure = null; if (active === api) repaint(); },
      err => { failure = err; if (active === api && !part()) paint(); });
  }

  function open(kind) {
    if (active && active !== api) active.close(false);
    active = api;
    tab = o.kinds.includes(kind) ? kind : o.kinds.includes(o.kindNow()) ? o.kindNow() : o.kinds[0];
    panel.hidden = false;
    backdrop.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    paint();
    ((part() && checkedItem()) || tabs.find(b => b.dataset.kind === tab))?.focus();
    load();
  }

  function close(focus = false) {
    if (active === api) active = null;
    panel.hidden = true;
    backdrop.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    if (focus) button.focus();
  }

  function select(kind) {
    tab = kind;
    paint();
  }

  /** Le libellé du bouton : le genre en cours et son choix. */
  function refresh() {
    const kind = o.kindNow();
    const genre = kind === 'audio' ? o.genre() : null;
    const id = o.chosen(kind, genre);
    const found = id ? menuData(o.mineOnly())?.[kind]?.models?.find(m => o.same(kind, m.id, id)) : null;
    const choice = !id ? t('model_menu.automatic')
      : found ? [found.label, found.split ? t('model_menu.split') : ''].filter(Boolean).join(' · ')
      : o.name(kind, id);
    root.querySelector('.mm-ico').innerHTML = icon(ICONS[kind] || 'chat');
    // L'audio dit son genre : « Voix · Automatique »
    const what = genre ? t(`model_menu.genre_${genre}`) : t(`model_menu.kind_${kind}`);
    root.querySelector('.mm-label').textContent = o.prefix ? `${what} · ${choice}` : choice;
    if (active === api) repaint();
  }

  button.onclick = () => (active === api ? close(true) : open());
  backdrop.onclick = () => close(true);
  root.querySelector('.mm-close').onclick = () => close(true);
  for (const b of tabs) b.onclick = () => select(b.dataset.kind);
  // Le clavier ou un clic hors du panneau le ferme (Tab au-delà du dernier modèle…)
  root.addEventListener('focusout', e => {
    if (active === api && e.relatedTarget && !root.contains(e.relatedTarget)) close(false);
  });
  root.addEventListener('keydown', e => {
    if (active !== api) return;
    const onTab = e.target.closest('.mm-tab');
    if (onTab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
      e.preventDefault();
      let i = tabs.indexOf(onTab);
      i = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1
        : (i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      select(tabs[i].dataset.kind);
      tabs[i].focus();
      return;
    }
    if (onTab && e.key === 'ArrowDown') {
      e.preventDefault();
      checkedItem()?.focus();
      return;
    }
    const item = e.target.closest('.mm-item');
    if (item && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
      e.preventDefault();
      const items = [...body.querySelectorAll('.mm-item')];
      let i = items.indexOf(item);
      if (e.key === 'ArrowUp' && i === 0) { tabs.find(b => b.dataset.kind === tab)?.focus(); return; }
      i = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1
        : Math.min(items.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1));
      items[i]?.focus();
    }
  });
  body.addEventListener('click', e => {
    const item = e.target.closest('.mm-item');
    if (!item) return;
    if (item.dataset.download !== undefined) {
      close(false);
      showModels();
      return;
    }
    const id = item.dataset.id || null;
    const model = id ? (part()?.models || []).find(m => m.id === id) || null
      : item.dataset.genre ? { genre: item.dataset.genre } : null;
    const kind = tab;
    close(true);
    accepted(model).then(ok => {
      if (!ok) return;
      o.pick(kind, id, model);
      refresh();
    });
  });

  /** Un modèle à licence restrictive pas encore acceptée ici (calculé par une autre machine) :
   *  sa licence lue et acceptée d'abord, puis notée (« Automatique » pourra le prendre). */
  async function accepted(model) {
    if (!model?.restricted || model.accepted !== false) return true;
    const { ok } = await modal({ title: t('model_menu.license_title', { name: model.label }),
      text: model.notice || t('model_menu.restricted', { license: model.license || '' }),
      iconName: 'warn', confirm: t('model_menu.license_confirm'), cancel: t('ui.cancel') });
    if (!ok) return false;
    const where = model.kind === 'audio' ? 'audio' : 'images';
    try { await post(`/${where}/models/${encodeURIComponent(model.id)}/license`, {}); }
    catch (err) { errorToast(err); return false; }
    model.accepted = true;
    return true;
  }

  const api = { root, refresh, open, close, load };
  instances.add(api);
  refresh();
  return api;
}
