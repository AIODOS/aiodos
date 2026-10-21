// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — les moteurs et ComfyUI (livraison L2 d'AIODOS).
//
// La surface existe dès 0.1.0 pour que le code des applis n'ait pas à changer ensuite ;
// chaque fonction rejette NotSupported tant que `features` n'annonce pas « engines » (pour
// engines()) ou « engines.comfyui » (pour comfy.*).

import { notYet } from './util.js';

/** GET /aiodos/v1/engines (L2). */
export function engines(ctx, { signal } = {}) {
  return notYet(ctx, 'engines', signal);
}

/** comfy.plan, objectInfo, template, recipes, recipeFor, tokenize, submit, run (L2 à L4). */
export function createComfy(ctx) {
  const need = (o) => notYet(ctx, 'engines.comfyui', o && o.signal);
  return Object.freeze({
    /** Pré-vol d'un travail ComfyUI, sans rien mettre en file. */
    plan: async (o = {}) => need(o),
    /** L'object_info relayé (le ComfyUI de ce PC, ou de ma machine de référence). */
    objectInfo: async (cls = null, o = {}) => need(o),
    /** Un gabarit relayé. */
    template: async (name, o = {}) => need(o),
    /** Les recettes servies aux inconnus. */
    recipes: async (o = {}) => need(o),
    /** La recette qui correspond à un gabarit. */
    recipeFor: async (template, o = {}) => need(o),
    /** Met les jetons d'entrée dans les nœuds LoadImage et LoadVideo donnés. */
    tokenize: async (graph, inputs, o = {}) => need(o),
    /** Envoie les entrées absentes, puis le travail ; rend un Job. */
    submit: async (o = {}) => need(o),
    /** submit, puis wait, puis download. */
    run: async (o = {}) => need(o),
  });
}
