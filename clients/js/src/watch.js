// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — suivre un travail : GET /aiodos/v1/jobs/{id}/events (SSE), en
// itérateur asynchrone (docs/api-applications.md § 5.7).
//
// Les événements rendus :
//   { type: 'job', job }                 l'état complet : d'abord, puis à chaque changement ;
//   { type: 'file', file, job }          un fichier pas encore récupéré, une fois chacun ;
//   { type: 'end', status, job }         la fin (completed, failed, cancelled ; deleted) ;
//   { type: 'unreachable', error, attempt, retryIn, job }   AIODOS ne répond plus : nouvel
//                                        essai dans retryIn ms.
//
// - reconnexion automatique : AIODOS ne rejoue rien, mais le premier événement de chaque
//   connexion est l'état complet, relu ; les fichiers pas encore récupérés qu'il cite et qui
//   n'ont pas encore été rendus le sont alors ;
// - un flux muet plus de `idleTimeout` (45 s ; AIODOS envoie « : ping » toutes les 15 s) est
//   rouvert ; une connexion refusée est réessayée de plus en plus lentement (10 s au plus) ;
// - `signal` arrête vraiment la connexion (« Arrêté ») ; `timeout` borne toute l'attente
//   (Timeout) ; sortir de la boucle (break) ferme aussi la connexion.
// Le travail, lui, continue dans AIODOS quoi qu'il arrive au suivi.

import { AiodosError, clientError } from './errors.js';
import { readSse } from './sse.js';
import { camelize, sleep } from './util.js';

/** Silence après lequel le flux est rouvert (AIODOS envoie « : ping » toutes les 15 s). */
export const WATCH_IDLE = 45_000;
const FIRST_RETRY = 500;
const MAX_RETRY = 10_000;
const QUICK = 1000; // deux coupures à moins d'une seconde : on attend avant la suivante

function retryable(err) {
  return err instanceof AiodosError && (err.code === 'unreachable' || err.code === 'timeout' || err.retryable === true);
}

/**
 * Les événements d'un travail. `hands` : { path, apply(état), file(fiche) }, fournis par Job,
 * qui tient l'état (le même objet Job est rendu dans chaque événement, à jour).
 */
export async function* watchJob(ctx, job, hands, { signal, timeout, idleTimeout = WATCH_IDLE } = {}) {
  const lang = ctx.lang();
  await ctx.session.requireFeature('jobs', { signal });
  const given = new Set(); // les fichiers déjà rendus par ce suivi
  const deadline = new AbortController();
  let expired = false;
  const timer =
    Number.isFinite(timeout) && timeout > 0
      ? setTimeout(() => {
          expired = true;
          deadline.abort();
        }, Math.min(timeout, 2 ** 31 - 1))
      : null;
  const stop = signal ? AbortSignal.any([signal, deadline.signal]) : deadline.signal;
  const halt = () => (expired && !(signal && signal.aborted) ? clientError('timeout', lang, { detail: { job: job.id } }) : clientError('cancelled', lang));
  let failures = 0;
  let quickAt = 0;
  try {
    for (;;) {
      if (stop.aborted) throw halt();
      let lost = null;
      let h = null;
      try {
        h = await ctx.session.keyed({ path: hands.path, signal: stop, timeout: idleTimeout, stream: true, feature: 'jobs' });
        if (!h.stream) ctx.transport.expectOk(h); // une réponse d'erreur (JobNotFound…) : levée
      } catch (err) {
        if (stop.aborted) throw halt();
        if (!retryable(err)) throw err;
        lost = err;
        h = null;
      }
      if (h) {
        let first = true;
        try {
          for await (const ev of readSse(h.res.body)) {
            h.timer.reset(idleTimeout); // signe de vie, « : ping » compris
            if (ev.type !== 'event') continue;
            let data;
            try {
              data = JSON.parse(ev.data);
            } catch {
              throw clientError('protocol', lang);
            }
            if (ev.event === 'job') {
              hands.apply(data);
              failures = 0;
              yield { type: 'job', job };
              if (first) {
                first = false;
                // rien n'est rejoué : les fichiers pas encore récupérés que cite l'état complet
                for (const f of job.state.files) {
                  if (f.fetched || given.has(f.id)) continue;
                  given.add(f.id);
                  yield { type: 'file', file: f, job };
                }
              }
            } else if (ev.event === 'file') {
              if (!data || typeof data.id !== 'string') throw clientError('protocol', lang);
              const f = hands.file(data);
              if (!given.has(f.id)) {
                given.add(f.id);
                yield { type: 'file', file: f, job };
              }
            } else if (ev.event === 'end') {
              yield { type: 'end', status: data && typeof data.status === 'string' ? data.status : null, job };
              return;
            } else {
              // un événement d'une version suivante d'AIODOS (unit, machine…), tel quel
              yield { type: ev.event, data: camelize(data), job };
            }
          }
          // fermé sans « end » : AIODOS s'arrête, ou redémarre
          lost = clientError('unreachable', lang, { textKey: 'connection_lost', vars: { host: h.url.host } });
        } catch (err) {
          if (stop.aborted) throw halt();
          const mapped = err instanceof AiodosError ? err : h.fail(err);
          if (!retryable(mapped)) throw mapped;
          lost = mapped;
        } finally {
          h.abort();
          h.done();
        }
        if (!first && Date.now() - quickAt > QUICK) {
          quickAt = Date.now(); // le flux marchait : reconnexion tout de suite
          continue;
        }
      }
      failures += 1;
      const backoff = Math.min(MAX_RETRY, FIRST_RETRY * 2 ** (failures - 1));
      const retryIn = Math.min(60_000, Math.max(backoff, (lost.retryAfter ?? 0) * 1000));
      yield { type: 'unreachable', error: lost, attempt: failures, retryIn, job };
      try {
        await sleep(retryIn, stop, lang);
      } catch {
        throw halt();
      }
    }
  } finally {
    if (timer) clearTimeout(timer);
  }
}
