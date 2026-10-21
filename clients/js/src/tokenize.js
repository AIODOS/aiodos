// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — comfy.tokenize : un fichier ne se donne à un travail ComfyUI que par
// un jeton « @in:<id> » (docs/api-applications.md § 5.7). tokenizeGraph met les jetons dans
// l'entrée de fichier des nœuds de chargement donnés, sur une copie du graphe (LoadImage et
// LoadImageMask : « image » ; LoadVideo : « file » ; LoadAudio : « audio »), et rend les
// entrées à donner telles quelles à submit, qui les hache et envoie celles qui manquent.
// Fonctions pures : rien n'est lu ni envoyé ici.

import { makeError } from './errors.js';
import { inputSource } from './inputs.js';
import { isPlainObject } from './util.js';

/** L'entrée de fichier des nœuds de chargement de ComfyUI. */
export const FILE_LOADERS = Object.freeze({ LoadImage: 'image', LoadImageMask: 'image', LoadVideo: 'file', LoadAudio: 'audio' });

/** Le format de l'éditeur de ComfyUI (« nodes » et « links ») au lieu du format API. */
export function isEditorFormat(graph) {
  return isPlainObject(graph) && Array.isArray(graph.nodes) && 'links' in graph;
}

/** Un identifiant d'entrée d'après celui du nœud ([A-Za-z0-9_.-], 64 caractères au plus). */
function idFor(nodeId, used) {
  const base = String(nodeId).replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 56) || 'in';
  let id = base;
  for (let n = 2; used.has(id); n += 1) id = `${base}-${n}`;
  used.add(id);
  return id;
}

/**
 * tokenizeGraph(graph, { '10': octets }) → { graph, inputs: { '10': octets } } : une copie du
 * graphe (format API) avec « @in:10 » dans l'entrée de fichier du nœud 10. Une valeur
 * { data | path | sha256, type, input } donne aussi le genre, ou l'entrée de fichier d'un nœud
 * de chargement que le client ne connaît pas (« input »).
 */
export function tokenizeGraph(graph, files, lang = 'fr') {
  const en = lang === 'en';
  if (isEditorFormat(graph)) {
    throw makeError(
      'graph_not_api',
      en ? 'Editor-format graph: export it in API format (“Export (API)” in ComfyUI).' : "Graphe au format de l'éditeur : exporte-le au format API (« Exporter (API) » dans ComfyUI).",
      { param: 'graph' },
    );
  }
  if (!isPlainObject(graph)) {
    throw makeError('invalid_request', en ? 'graph: a ComfyUI graph in API format (an object of nodes).' : 'graph : un graphe ComfyUI au format API (un objet de nœuds).', { param: 'graph' });
  }
  if (!isPlainObject(files)) {
    throw makeError('invalid_request', en ? 'Files: { node id: input }.' : 'Fichiers : { nœud: entrée }.', { param: 'inputs' });
  }
  const out = structuredClone(graph);
  const inputs = {};
  const used = new Set();
  for (const [nodeId, value] of Object.entries(files)) {
    const node = out[nodeId];
    if (!isPlainObject(node) || typeof node.class_type !== 'string') {
      throw makeError('invalid_request', en ? `Node “${nodeId}” is not in the graph.` : `Nœud « ${nodeId} » absent du graphe.`, { param: 'inputs', detail: { node: nodeId } });
    }
    const spec = isPlainObject(value) ? value : null;
    const name = spec && typeof spec.input === 'string' && spec.input ? spec.input : FILE_LOADERS[node.class_type];
    if (!name) {
      throw makeError(
        'invalid_request',
        en
          ? `Node “${nodeId}” (${node.class_type}) is not a known file loader: give its file input (“input”).`
          : `Le nœud « ${nodeId} » (${node.class_type}) n'est pas un chargeur de fichier connu : donne son entrée de fichier (« input »).`,
        { param: 'inputs', detail: { node: nodeId, class: node.class_type } },
      );
    }
    let given = value;
    if (spec) {
      const { input: _input, ...rest } = spec;
      given = rest;
    }
    inputSource(given, lang); // une entrée illisible est refusée ici, pas au dépôt
    const id = idFor(nodeId, used);
    node.inputs = { ...(isPlainObject(node.inputs) ? node.inputs : {}), [name]: `@in:${id}` };
    inputs[id] = given;
  }
  return { graph: out, inputs };
}
