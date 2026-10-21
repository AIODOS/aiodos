// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — choisir un modèle dans une liste rendue par models() (ou au format
// d'ARKLIGHT : { id, size, caps }).

import { toList } from './util.js';

const capsOf = (m) => (Array.isArray(m.caps) ? m.caps : []);

/**
 * Le meilleur modèle pour un usage, ou null.
 * - `needs` : capacités exigées (« tools », « vision », « thinking »…) ;
 * - `vision` : préférer un modèle qui voit les images (sans l'exiger : mettre « vision »
 *   dans `needs` pour l'exiger) ;
 * - `preferLocal` (vrai par défaut) : préférer les modèles de ce PC (`here`) ;
 * - `minContext` : écarter les modèles dont le contexte connu est plus petit.
 * Ensuite : celui qu'AIODOS prend lui-même (`default`), un modèle déjà chargé, puis le plus
 * léger.
 */
export function pickModel(list, { needs = [], vision = false, preferLocal = true, minContext = 0 } = {}) {
  const need = toList(needs);
  const wantsEmbedding = need.includes('embedding');
  const items = (Array.isArray(list) ? list : []).filter((m) => {
    if (!m || typeof m.id !== 'string' || m.installed === false) return false;
    const caps = capsOf(m);
    // un modèle d'embedding n'est pas un modèle de conversation
    if (!wantsEmbedding && caps.includes('embedding') && !caps.includes('completion')) return false;
    if (!need.every((c) => caps.includes(c))) return false;
    if (minContext > 0 && typeof m.context === 'number' && m.context < minContext) return false;
    return true;
  });
  const score = (m) => [
    vision && capsOf(m).includes('vision') ? 0 : 1,
    preferLocal && m.here !== false ? 0 : 1,
    m.default ? 0 : 1,
    m.loaded ? 0 : 1,
    m.size > 0 ? m.size : Number.MAX_SAFE_INTEGER,
  ];
  items.sort((a, b) => {
    const sa = score(a);
    const sb = score(b);
    for (let i = 0; i < sa.length; i++) if (sa[i] !== sb[i]) return sa[i] - sb[i];
    return a.id.localeCompare(b.id);
  });
  return items[0] ?? null;
}
