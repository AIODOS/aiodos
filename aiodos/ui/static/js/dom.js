// Petits utilitaires DOM et de mise en forme.
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function h(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ESCAPES[c]);

export function bytes(n) {
  if (!n) return '0 o';
  const units = ['o', 'Ko', 'Mo', 'Go', 'To'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  const v = n / 1024 ** i;
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`.replace('.', ',');
}

export function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "à l'instant";
  if (s < 3600) return `il y a ${Math.floor(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.floor(s / 3600)} h`;
  if (s < 86400 * 7) return `il y a ${Math.floor(s / 86400)} j`;
  return new Date(ts * 1000).toLocaleDateString('fr-FR');
}

export function duration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, '0')}`;
  return `${Math.floor(s / 3600)} h ${String(Math.floor(s % 3600 / 60)).padStart(2, '0')}`;
}

// Un nombre venu d'une autre machine (son profil) : un nombre fini, sinon null. Une
// chaîne « <img onerror=…> » à la place d'un niveau de batterie ne doit jamais entrer
// dans une page.
export const finite = v => (typeof v === 'number' && Number.isFinite(v) ? v : null);

// Le nombre d'éléments d'une liste reçue (ollama.models…) : 0 si ce n'est pas une liste
// (un objet { length: "<img…>" } ne donne pas son « length »)
export const count = list => (Array.isArray(list) ? list.length : 0);

// « Sur secteur · batterie 100 % », « En charge · batterie 84 % · 31,2 °C »… Le profil
// peut venir d'une autre machine : seuls des nombres sont affichés.
export function powerText(profile) {
  const p = (profile?.power && typeof profile.power === 'object') ? profile.power : {};
  const plugged = p.plugged ?? profile?.on_ac_power;
  const parts = [plugged == null ? (p.mobile ? 'Charge inconnue' : '—')
    : plugged ? (p.mobile ? 'En charge' : 'Sur secteur') : 'Sur batterie'];
  const level = finite(p.level);
  const temperature = finite(p.temperature);
  if (level != null) parts.push(`batterie ${Math.round(level)} %`);
  if (temperature != null) parts.push(`${esc(String(temperature).replace('.', ','))} °C`);
  return parts.join(' · ');
}

// Où mène un lien d'une réponse : « web » (http, https : le navigateur du système),
// « mail » (un courriel), ou null : search-ms:, ms-word:, intent:… ne s'ouvrent pas
// (ils lanceraient une autre application sans confirmation, cf. render.py)
export function linkKind(href) {
  const address = String(href ?? '').trim();
  if (/^https?:\/\//i.test(address)) return 'web';
  if (/^mailto:/i.test(address)) return 'mail';
  return null;
}

// Que faire d'un clic sur un lien d'une réponse (chat.js) :
//  - « block » : ni le web ni un courriel, rien ne s'ouvre ;
//  - « external » : clic simple sur une page web dans la fenêtre native, qui la fait ouvrir
//    par le nœud dans le navigateur du système (/api/open_url) ;
//  - « follow » : laisser le lien suivre son cours. Un courriel (sa messagerie) ; une page web
//    ailleurs : un onglet du navigateur où l'interface est ouverte, ou, dans l'appli Android,
//    le navigateur du téléphone (MainActivity). /api/open_url l'ouvrirait sur la machine du
//    nœud, pas devant l'utilisateur, et n'ouvre rien sous Android. Clic du milieu dans la
//    fenêtre native : elle le passe au navigateur du système (ui/window.py).
export function linkAction(href, { button = 0, nativeWindow = false } = {}) {
  const kind = linkKind(href);
  if (!kind) return 'block';
  return kind === 'web' && nativeWindow && button === 0 ? 'external' : 'follow';
}

// Une vraie zone de saisie, où du texte lâché s'insère : un champ texte modifiable, une zone de
// texte, un contenu éditable. Lâché ailleurs (case à cocher, curseur, bouton, champ en lecture
// seule…), un lien serait suivi et la page remplacée (app.js, keepDrops).
const TEXT_INPUTS = new Set(['text', 'search', 'url', 'email', 'tel', 'number', 'password']);
export function acceptsText(target) {
  if (!target) return false;
  if (target.isContentEditable === true) return true;
  const field = target.closest?.('textarea, input');
  if (!field || field.readOnly || field.disabled) return false;
  if (String(field.tagName).toUpperCase() === 'TEXTAREA') return true;
  return TEXT_INPUTS.has(String(field.type || 'text').toLowerCase());
}

// Un glisser-déposer sur la fenêtre, refusé s'il apporte des fichiers ou s'il ne tombe pas
// dans une zone de saisie (app.js, keepDrops) : types, ceux de dataTransfer
export function dropRefused(types, intoTyping) {
  return Array.from(types || []).includes('Files') || !intoTyping;
}

export function debounce(fn, ms = 200) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
