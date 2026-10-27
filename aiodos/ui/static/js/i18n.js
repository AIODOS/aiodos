// Textes de l'interface, en français ou en anglais : aiodos/i18n/<langue>/<fonctionnalité>.json,
// reçus d'un bloc au démarrage (GET /api/i18n, app.js). La clé d'un texte commence par le nom
// de son fichier (« empty » dans chat.json : « chat.empty »). Paramètres : {nom} ; deux formes
// selon un nombre, { one, other }, choisie par le paramètre n.
//
// t() ne s'appelle jamais au chargement d'un module, les textes n'y sont pas encore : seulement
// dans les fonctions qui affichent. t() rend du texte brut, à échapper (esc) dans du HTML.
let texts = null;
export let lang = 'fr';
let machine = null;              // la langue de la machine (GET /api/i18n), si AIODOS la parle

// Le nœud injoignable : aucun texte n'a pu venir, ces deux phrases suffisent
const OFFLINE = {
  fr: 'Impossible de joindre le nœud AIODOS.',     // i18n: tel quel
  en: 'Cannot reach the AIODOS node.',
};

export function setTexts(language, all, system = null) {
  lang = language === 'fr' ? 'fr' : 'en';
  machine = system === 'fr' || system === 'en' ? system : null;
  texts = all || {};
  if (globalThis.document) document.documentElement.lang = lang;   // (essais sous Node.js : sans page)
}

/** La langue préférée du navigateur parmi le français et l'anglais (l'anglais sinon). */
export function browserLanguage() {
  const tags = navigator.languages?.length ? navigator.languages : [navigator.language || ''];
  for (const tag of tags) {
    const primary = String(tag).toLowerCase().split(/[-_]/)[0];
    if (primary === 'fr' || primary === 'en') return primary;
  }
  return 'en';
}

/** La langue que donne le réglage « Automatique » : celle de la machine, sinon celle du
 *  navigateur. */
export const autoLanguage = () => machine || browserLanguage();

export const offline = () => OFFLINE[texts ? lang : browserLanguage()];

export const has = key => !!texts && typeof key === 'string' && Object.hasOwn(texts, key);

function form(n) {
  const x = Math.abs(Number(n));
  if (!Number.isFinite(x)) return 'other';
  return lang === 'fr' ? (x < 2 ? 'one' : 'other') : (x === 1 ? 'one' : 'other');
}

export function t(key, params = {}) {
  if (!texts) {
    console.warn(`i18n : t('${key}') avant l'arrivée des textes`);   // i18n: tel quel (journal)
    return key;
  }
  let value = texts[key];
  if (value === undefined) {
    console.warn(`i18n : texte manquant, ${key}`);                  // i18n: tel quel (journal)
    return key;
  }
  if (value && typeof value === 'object') value = value[form(params.n)] ?? value.other ?? '';
  return String(value).replace(/\{\{|\}\}|\{(\w+)\}/g, (match, name) => {
    if (name === undefined) return match[0];
    return Object.hasOwn(params, name) ? String(params[name]) : match;
  });
}

/** Un texte venu du nœud ou d'une autre machine : une chaîne telle quelle, ou [clé, {paramètres}]
 *  (i18n.lazy côté Python), traduit ici. Une clé inconnue (autre version) : fallback. Seuls des
 *  textes et des nombres passent en paramètres. */
export function tt(value, fallback = '') {
  if (typeof value === 'string') return value;
  if (Array.isArray(value) && value.length === 2 && has(value[0])) {
    const params = {};
    if (value[1] && typeof value[1] === 'object') {
      for (const [k, v] of Object.entries(value[1])) {
        if (typeof v === 'string' || (typeof v === 'number' && Number.isFinite(v))) params[k] = v;
      }
    }
    return t(value[0], params);
  }
  return fallback;
}

/** Les textes posés dans le HTML : data-t (contenu), data-t-title (bulle), data-t-label
 *  (aria-label), data-t-placeholder. */
export function translatePage(root = document) {
  root.querySelectorAll('[data-t]').forEach(el => { el.textContent = t(el.dataset.t); });
  root.querySelectorAll('[data-t-title]').forEach(el => { el.title = t(el.dataset.tTitle); });
  root.querySelectorAll('[data-t-label]').forEach(el => {
    el.setAttribute('aria-label', t(el.dataset.tLabel));
  });
  root.querySelectorAll('[data-t-placeholder]').forEach(el => {
    el.placeholder = t(el.dataset.tPlaceholder);
  });
}

/** Les réglages régionaux pour les dates et les nombres : ceux du navigateur s'il parle la
 *  langue de l'interface (en-GB, fr-CA…), sinon fr-FR ou en-US. */
export function locale() {
  const own = navigator.language || '';
  return own.toLowerCase().startsWith(lang) ? own : (lang === 'fr' ? 'fr-FR' : 'en-US');
}

/** Un nombre écrit tel quel, avec la virgule décimale en français (31,2 °C ; 31.2 °C). */
export const decimal = x => (lang === 'fr' ? String(x).replace('.', ',') : String(x));

/** Un nombre à la façon de la langue : 0,45 en français, 0.45 en anglais. */
export function number(x, digits = 0) {
  const value = Number(x);
  if (!Number.isFinite(value)) return '';
  return value.toLocaleString(locale(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
