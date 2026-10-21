// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — les entrées des travaux (livraison L2 d'AIODOS) : HEAD puis PUT
// /aiodos/v1/inputs/{sha256}. Rejette NotSupported tant que `features` n'annonce pas « inputs ».

import { notYet } from './util.js';

export function createInputs(ctx) {
  return Object.freeze({
    /** Envoie une entrée (octets) ; rendra son empreinte sha256. */
    put: async (data, o = {}) => notYet(ctx, 'inputs', o.signal),
  });
}
