// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — les travaux (livraison L2 d'AIODOS) : retrouver ses travaux, en
// suivre un. Rejettent NotSupported tant que `features` n'annonce pas « jobs ».

import { notYet } from './util.js';

export function createJobs(ctx) {
  return Object.freeze({
    /** GET /aiodos/v1/jobs?active=&status=&client_ref= : ses travaux, après un redémarrage. */
    jobs: async (o = {}) => notYet(ctx, 'jobs', o.signal),
    /** GET /aiodos/v1/jobs/{id} : un travail (watch, wait, pause, resume, cancel, files, download…). */
    job: async (id, o = {}) => notYet(ctx, 'jobs', o.signal),
  });
}
