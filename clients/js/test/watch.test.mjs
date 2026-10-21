// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : suivre un travail (job.watch, job.wait) : reconnexion du flux
// avec l'état complet relu et sans fichier en double, flux muet rouvert, AIODOS arrêté puis
// relancé, arrêt par signal ou par break (la connexion vraiment fermée), délai total.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { FAKE_FEATURES, contractInteraction, createFakeClient } from '../src/fake.js';

const GRAPH = contractInteraction('jobs', 'travail déposé').request.json.graph;
const NO_INPUT = (() => {
  const g = structuredClone(GRAPH);
  g['10'] = { class_type: 'EmptyLatentImage', inputs: { width: 512, height: 512, batch_size: 1 } };
  return g;
})();
const TWO = [{ label: 'Un' }, { label: 'Deux' }];

/** Un client du faux, travaux pilotés à la main, et un travail de deux unités déposé. */
async function manualJob(fakeOptions = {}) {
  const { client, fake } = createFakeClient({ fake: { features: FAKE_FEATURES, scopes: ['chat', 'engines:comfyui'], jobMode: 'manual', ...fakeOptions } });
  const job = await client.comfy.submit({ graph: NO_INPUT, items: TWO });
  return { client, fake, job };
}

const label = (ev) => (ev.type === 'job' ? `job:${ev.job.status}` : ev.type === 'file' ? 'file' : ev.type === 'end' ? `end:${ev.status}` : ev.type);

test('flux coupé : reconnexion tout de suite, l’état complet relu, chaque fichier une fois', async () => {
  const { fake, job } = await manualJob();
  fake.nextEvents({ cutAfter: 1 }); // le premier flux se coupe après son premier événement
  const seen = [];
  const files = new Set();
  for await (const ev of job.watch()) {
    seen.push(label(ev));
    if (ev.type === 'file') {
      assert.ok(!files.has(ev.file.id), 'un fichier rendu deux fois');
      files.add(ev.file.id);
    }
    if (seen.length === 1) {
      fake.step(); // l'unité 1 démarre : le flux se coupe
      fake.step(); // l'unité 1 finit pendant la coupure : son fichier arrive par l'état relu
    } else if (ev.type === 'file' && files.size === 1) {
      fake.finish(job.id);
    }
  }
  assert.deepEqual(seen.slice(0, 3), ['job:queued', 'job:in_progress', 'file']);
  assert.deepEqual(seen.slice(-2), ['job:completed', 'end:completed']);
  assert.equal(files.size, 2);
  assert.ok(!seen.includes('unreachable'), 'une coupure isolée ne fait pas attendre');
  assert.equal(fake.received(`/aiodos/v1/jobs/${job.id}/events`).length, 2);
});

test('flux muet : rouvert après idleTimeout', async () => {
  const { fake, job } = await manualJob();
  fake.nextEvents({ stallAfter: 1 }); // un seul événement, puis plus rien, connexion ouverte
  const seen = [];
  for await (const ev of job.watch({ idleTimeout: 80 })) {
    seen.push(label(ev));
    if (seen.length === 2) fake.finish(job.id); // le deuxième flux : l'état complet, puis la suite
  }
  assert.deepEqual(seen.slice(0, 2), ['job:queued', 'job:queued']);
  assert.equal(seen.at(-1), 'end:completed');
  assert.equal(fake.received(`/aiodos/v1/jobs/${job.id}/events`).length, 2);
  assert.equal(fake.closed, 1); // le flux muet a été fermé par le client
});

test('AIODOS arrêté puis relancé : un événement unreachable, puis la suite', async () => {
  const { fake, job } = await manualJob();
  const seen = [];
  for await (const ev of job.watch()) {
    seen.push(label(ev));
    if (seen.length === 1) {
      fake.listening = false; // AIODOS s'arrête : le flux se coupe, la connexion est refusée
      fake.cutStreams();
    }
    if (ev.type === 'unreachable') {
      assert.equal(ev.error.code, 'unreachable');
      assert.match(ev.error.message, /Connexion refusée/);
      assert.equal(ev.attempt, 1);
      assert.equal(ev.retryIn, 500);
      fake.listening = true; // relancé
    }
    if (seen.length === 3) fake.finish(job.id);
  }
  assert.deepEqual(seen.slice(0, 3), ['job:queued', 'unreachable', 'job:queued']);
  assert.equal(seen.at(-1), 'end:completed');
  // la preuve d'identité a été refaite avant de renvoyer la clé
  assert.ok(fake.requests.filter((r) => r.path.startsWith('/aiodos/v1/info?key_id=')).length >= 2);
});

test('signal : « Arrêté », la connexion vraiment fermée ; le travail continue dans AIODOS', async () => {
  const { fake, job } = await manualJob();
  const ctrl = new AbortController();
  const seen = [];
  await assert.rejects(
    (async () => {
      for await (const ev of job.watch({ signal: ctrl.signal })) {
        seen.push(label(ev));
        ctrl.abort();
      }
    })(),
    (e) => e.name === 'Cancelled' && e.message === 'Arrêté',
  );
  assert.deepEqual(seen, ['job:queued']);
  assert.equal(fake.closed, 1);
  assert.equal(fake.jobObject(job.id).status, 'queued');
});

test('break : la connexion fermée aussi', async () => {
  const { fake, job } = await manualJob();
  for await (const ev of job.watch()) if (ev.type === 'job') break;
  assert.equal(fake.closed, 1);
});

test('timeout : Timeout quand le travail ne finit pas à temps', async () => {
  const { job } = await manualJob();
  await assert.rejects(job.wait({ timeout: 60 }), (e) => e.name === 'Timeout' && e.code === 'timeout' && e.detail.job === job.id);
});

test('travail introuvable : JobNotFound, sans nouvel essai', async () => {
  const { client, fake, job } = await manualJob();
  const other = await client.job(job.id);
  fake.jobs.get(job.id).app = 'zzzzzzzz'; // devenu celui d'une autre appli
  await assert.rejects(other.wait(), (e) => e.name === 'JobNotFound');
  assert.equal(fake.received(`/aiodos/v1/jobs/${job.id}/events`).length, 1);
});

test('wait : le travail rendu, quel que soit son état final ; les erreurs des unités', async () => {
  const done = await manualJob();
  done.fake.finish(done.job.id);
  assert.equal((await done.job.wait()).status, 'completed');

  const { client, fake } = createFakeClient({ fake: { features: FAKE_FEATURES, scopes: ['chat', 'engines:comfyui'] } });
  fake.nextJob({ failUnits: [0, 1] });
  const failed = await client.comfy.submit({ graph: NO_INPUT, items: TWO });
  await failed.wait();
  assert.equal(failed.status, 'failed');
  assert.equal(failed.progress.failed, 2);
  assert.equal(failed.error.code, 'all_failed');
  assert.equal(failed.units[0].error.code, 'engine_error');
  assert.match(failed.units[0].error.message, /nœud 3/);
});
