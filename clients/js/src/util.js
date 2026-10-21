// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — petits outils communs : attente annulable, listes, cercles, noms des
// champs (snake_case d'AIODOS, camelCase du client), fonctions pas encore servies.

import { clientError, makeError } from './errors.js';
import { VERSION } from './version.js';

/** Les cercles de calcul connus (docs/api-applications.md § 4.6). */
export const CIRCLES = Object.freeze(['here', 'mine', 'friends', 'public']);

/** Attend `ms` millisecondes, ou rejette « Arrêté » dès que `signal` est levé. */
export function sleep(ms, signal, lang = 'fr') {
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) {
      reject(clientError('cancelled', lang));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(clientError('cancelled', lang));
    };
    const timer = setTimeout(() => {
      if (signal) signal.removeEventListener('abort', onAbort);
      resolve();
    }, Math.max(0, ms));
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
  });
}

/** Une liste de chaînes, depuis une liste ou un texte séparé par des espaces ou des virgules. */
export function toList(value) {
  if (value == null) return [];
  const items = Array.isArray(value) ? value : String(value).split(/[\s,]+/);
  return items.map((s) => String(s).trim()).filter(Boolean);
}

/** Les cercles demandés, vérifiés (une erreur invalid_request avant tout envoi). */
export function checkWhere(where, lang = 'fr') {
  const list = toList(where);
  const bad = list.filter((c) => !CIRCLES.includes(c));
  if (bad.length || (where != null && !list.length)) {
    const msg =
      lang === 'en'
        ? `Unknown place of computation: ${bad.join(', ') || '(empty)'} (here, mine, friends, public).`
        : `Lieu de calcul inconnu : ${bad.join(', ') || '(vide)'} (here, mine, friends, public).`;
    throw makeError('invalid_request', msg, { param: 'where' });
  }
  return [...new Set(list)];
}

/**
 * Une fonction qu'AIODOS ne sert pas encore (object_info relayé et gabarits en L2c, recettes
 * en L4, liens signés) : NotSupported. D'abord `needs`, la fonction qui la porte (absente :
 * NotSupported, detail.announced faux, sans rien essayer d'autre que GET /aiodos/v1/info) ;
 * puis `feature` elle-même : pas annoncée, NotSupported (« mets AIODOS à jour ») ; déjà
 * annoncée par un AIODOS plus récent, NotSupported aussi (detail.announced vrai) : ce client
 * n'en connaît pas encore les routes, une version suivante les servira.
 */
export async function notYet(ctx, { needs, feature, signal }) {
  const info = await ctx.session.requireFeature(needs ?? feature, { signal });
  const lang = ctx.lang();
  if (!(Array.isArray(info.features) && info.features.includes(feature))) {
    throw clientError('not_supported', lang, { vars: { feature }, detail: { feature, announced: false } });
  }
  throw clientError('not_supported', lang, {
    textKey: 'client_too_old',
    vars: { feature, version: VERSION },
    detail: { feature, announced: true },
  });
}

/** Une copie sans les champs de valeur undefined. */
export function compact(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) if (v !== undefined) out[k] = v;
  return out;
}

/**
 * Une réponse d'AIODOS (snake_case) en camelCase, récursivement : client_ref → clientRef,
 * elapsed_s → elapsedS, vram_mib → vramMib. Les valeurs ne changent pas.
 */
export function camelize(value, depth = 0) {
  if (Array.isArray(value)) return value.map((v) => camelize(v, depth + 1));
  if (!value || typeof value !== 'object' || depth > 12) return value;
  const out = {};
  for (const [k, v] of Object.entries(value)) out[k.replace(/_([a-z0-9])/g, (_, c) => c.toUpperCase())] = camelize(v, depth + 1);
  return out;
}

/** Les clés d'un objet (un niveau) de camelCase en snake_case : unitSeconds → unit_seconds. */
export function snakeKeys(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) out[k.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`)] = v;
  return out;
}

/** Vrai pour un objet simple ({…}, ni liste, ni octets). */
export function isPlainObject(v) {
  return Boolean(v) && typeof v === 'object' && !Array.isArray(v) && !ArrayBuffer.isView(v) && !(v instanceof ArrayBuffer);
}
