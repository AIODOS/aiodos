// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — petits outils communs : attente annulable, listes, cercles.

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
 * La surface de L2 (moteurs, travaux, entrées) : NotSupported tant que `features` n'annonce
 * pas la fonction, sans rien essayer d'autre que GET /aiodos/v1/info ; et, si un AIODOS plus
 * récent l'annonce déjà, NotSupported aussi (detail.announced vrai) : ce client 0.1.0 n'en
 * connaît pas encore les routes, la version suivante les servira.
 */
export async function notYet(ctx, feature, signal) {
  await ctx.session.requireFeature(feature, { signal });
  throw clientError('not_supported', ctx.lang(), {
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
