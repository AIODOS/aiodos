// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — le corps d'un travail ComfyUI (POST /aiodos/v1/jobs, et le pré-vol
// POST /aiodos/v1/engines/comfyui/plan), vérifié avant tout envoi et traduit en snake_case
// (docs/api-applications.md § 5.7), et le lieu de calcul.
//
// - les valeurs par défaut d'AIODOS ne partent pas (priority « normal », models « auto »,
//   strict faux) : le corps reste celui du contrat ;
// - le lieu passe par l'en-tête X-Aiodos-Where, comme pour la conversation : il resserre
//   l'accord de l'appli, le pré-vol le lit aussi, et un cercle accordé que cette version
//   d'AIODOS ne sert pas encore (mine) n'y est pas une erreur ;
// - sensitive : le lieu resserré à ce PC et à mes machines (here, mine), dans l'accord de
//   l'appli (fiches de casting, visages de référence : jamais chez un ami ni un inconnu).

import { makeError } from './errors.js';
import { checkWhere, isPlainObject, snakeKeys, toList } from './util.js';

const PRIORITIES = new Set(['high', 'normal', 'low']);
const SENSITIVE = ['here', 'mine'];

/** Le corps d'un travail (sans ses entrées, envoyées d'abord). Lève invalid_request. */
export function jobBody(o, lang = 'fr') {
  const en = lang === 'en';
  const bad = (param, fr, english) => makeError('invalid_request', en ? english : fr, { param });
  const { graph, graphs, items, repeat, models, priority, label, clientRef, bounds, strict } = o;
  if ((graph == null) === (graphs == null)) {
    throw bad('graph', 'Un graphe au format API : « graph », ou « graphs » (une liste).', 'A graph in API format: “graph”, or “graphs” (a list).');
  }
  if (graph != null && !isPlainObject(graph)) throw bad('graph', '« graph » : un graphe au format API (un objet de nœuds).', '“graph”: a graph in API format (an object of nodes).');
  if (graphs != null && (!Array.isArray(graphs) || !graphs.length || !graphs.every(isPlainObject))) {
    throw bad('graphs', '« graphs » : une liste de graphes au format API.', '“graphs”: a list of graphs in API format.');
  }
  if (items != null && repeat != null) throw bad('items', '« items » ou « repeat », pas les deux.', '“items” or “repeat”, not both.');
  if (items != null && !Array.isArray(items)) throw bad('items', "« items » : une liste d'éléments { graph, label, seed, set }.", '“items”: a list of { graph, label, seed, set }.');
  if (repeat != null && !isPlainObject(repeat)) throw bad('repeat', '« repeat » : { count, vary }.', '“repeat”: { count, vary }.');
  if (models != null && models !== 'auto' && !isPlainObject(models)) throw bad('models', '« models » : "auto", ou { clé: { folder, name } }.', '“models”: "auto", or { key: { folder, name } }.');
  if (priority != null && !PRIORITIES.has(priority)) throw bad('priority', '« priority » : high, normal ou low.', '“priority”: high, normal or low.');
  if (bounds != null && !isPlainObject(bounds)) throw bad('bounds', '« bounds » : { unitSeconds, outputMb, waitS }.', '“bounds”: { unitSeconds, outputMb, waitS }.');
  const body = { engine: 'comfyui' };
  if (graph != null) body.graph = graph;
  else body.graphs = graphs;
  if (label != null) body.label = String(label);
  if (clientRef != null) body.client_ref = String(clientRef);
  if (items != null) body.items = items;
  if (repeat != null) body.repeat = repeat;
  if (models != null && models !== 'auto') body.models = models;
  if (priority != null && priority !== 'normal') body.priority = priority;
  if (bounds != null) body.bounds = snakeKeys(bounds);
  if (strict) body.strict = true;
  return body;
}

/**
 * L'en-tête du lieu : `where` tel que demandé (vérifié), resserré à here et mine pour un
 * travail sensible (dans l'accord de l'appli, lu dans l'info avec la clé quand `where` manque).
 */
export async function placeHeaders(ctx, { where, sensitive, signal }, lang = 'fr') {
  let circles = where == null ? null : checkWhere(where, lang);
  if (sensitive) {
    if (circles == null) {
      const info = await ctx.session.appInfo({ signal });
      const granted = info && info.auth === 'valid' && info.app ? toList(info.app.where) : [];
      circles = granted.length ? granted : [...SENSITIVE]; // clé refusée : le dépôt dira pourquoi
    }
    circles = circles.filter((c) => SENSITIVE.includes(c));
    if (!circles.length) {
      throw makeError(
        'where_denied',
        lang === 'en' ? 'Sensitive job: neither this PC nor my machines are allowed here.' : 'Travail sensible : ni ce PC ni mes machines ne sont permis ici.',
        { param: 'where', detail: { sensitive: true, allowed: SENSITIVE } },
      );
    }
  }
  return circles ? { 'X-Aiodos-Where': circles.join(',') } : {};
}
