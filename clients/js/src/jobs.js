// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — retrouver ses travaux (docs/api-applications.md § 5.7), après un
// redémarrage de l'appli comme d'AIODOS : GET /aiodos/v1/jobs?active=&client_ref=&limit= et
// GET /aiodos/v1/jobs/{id}. Une appli ne voit que les siens : celui d'une autre est
// introuvable (JobNotFound). Rejettent NotSupported tant que `features` n'annonce pas « jobs ».

import { clientError, makeError } from './errors.js';
import { Job, jobPath } from './job.js';
import { toList } from './util.js';

const LIST_TIMEOUT = 30_000;

/**
 * Ses travaux, les plus récents d'abord. Sans filtre, ceux en cours (active vrai) ; avec
 * clientRef ou status, tous, finis compris (active: null pour tous, false pour les finis).
 */
export async function listJobs(ctx, { active, clientRef, status, limit, signal } = {}) {
  await ctx.session.requireFeature('jobs', { signal });
  const lang = ctx.lang();
  const wanted = toList(status);
  const act = active !== undefined ? active : clientRef == null && !wanted.length ? true : null;
  const q = new URLSearchParams();
  if (act === true) q.set('active', '1');
  else if (act === false) q.set('active', '0');
  if (clientRef != null) q.set('client_ref', String(clientRef));
  if (wanted.length) q.set('status', wanted.join(','));
  if (limit != null) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 200) {
      throw makeError('invalid_request', lang === 'en' ? '“limit”: from 1 to 200.' : '« limit » : de 1 à 200.', { param: 'limit' });
    }
    q.set('limit', String(limit));
  }
  const qs = q.toString();
  const res = await ctx.session.keyed({ path: `/aiodos/v1/jobs${qs ? `?${qs}` : ''}`, signal, timeout: LIST_TIMEOUT, feature: 'jobs' });
  const d = ctx.transport.expectOk(res);
  if (!d || !Array.isArray(d.jobs)) throw clientError('protocol', lang);
  const jobs = d.jobs.map((j) => new Job(ctx, j));
  // l'état filtré ici aussi : cette version d'AIODOS ne lit pas « status »
  return wanted.length ? jobs.filter((j) => wanted.includes(j.status)) : jobs;
}

/** Un de ses travaux, par son identifiant. */
export async function getJob(ctx, id, { signal } = {}) {
  await ctx.session.requireFeature('jobs', { signal });
  const res = await ctx.session.keyed({ path: jobPath(id, ctx.lang()), signal, timeout: LIST_TIMEOUT, feature: 'jobs' });
  return new Job(ctx, ctx.transport.expectOk(res));
}

export function createJobs(ctx) {
  return Object.freeze({
    /** Ses travaux (après un redémarrage) : { active, clientRef, status, limit }. */
    jobs: (o) => listJobs(ctx, o),
    /** Un de ses travaux, par son identifiant. */
    job: (id, o) => getJob(ctx, id, o),
  });
}
