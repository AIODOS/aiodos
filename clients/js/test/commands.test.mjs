// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : les commandes d'un travail (pause après l'unité en cours ou
// tout de suite, reprise, annulation, relance des unités en échec, oubli), retrouver ses
// travaux (jobs, job), et run() quand le travail finit sans résultat ou que l'appli l'arrête.

import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { JobFailed } from '../src/index.js';
import { FAKE_FEATURES, contractInteraction, createFakeClient } from '../src/fake.js';

const GRAPH = (() => {
  const g = structuredClone(contractInteraction('jobs', 'travail déposé').request.json.graph);
  g['10'] = { class_type: 'EmptyLatentImage', inputs: { width: 512, height: 512, batch_size: 1 } };
  return g;
})();
const TWO = [{ label: 'Un' }, { label: 'Deux' }];
const L2 = { features: FAKE_FEATURES, scopes: ['chat', 'engines:comfyui'] };

function manual(extra = {}) {
  return createFakeClient({ fake: { ...L2, jobMode: 'manual', ...extra } });
}

test('pause après l’unité en cours : rien ne se perd ; reprise', async () => {
  const { client, fake } = manual();
  const job = await client.comfy.submit({ graph: GRAPH, items: TWO });
  fake.step(); // l'unité 1 calcule
  await job.pause();
  assert.equal(job.status, 'pausing');
  assert.deepEqual(job.state.pause, { when: 'after_current', pending: 1 });
  assert.deepEqual(fake.received(`/aiodos/v1/jobs/${job.id}/pause`)[0].json, { when: 'after_current' });
  fake.step(); // elle finit, et rien d'autre ne part
  assert.equal(fake.step(), false);
  await job.refresh();
  assert.equal(job.status, 'paused');
  assert.equal(job.progress.done, 1);
  assert.equal(job.state.files.length, 1);
  await job.resume();
  assert.equal(job.status, 'in_progress');
  assert.equal(job.state.pause, null);
  fake.finish(job.id);
  assert.equal((await job.refresh()).status, 'completed');
});

test('pause tout de suite : l’unité en cours est rendue ; « when » vérifié avant l’envoi', async () => {
  const { client, fake } = manual();
  const job = await client.comfy.submit({ graph: GRAPH, items: TWO });
  fake.step();
  await job.pause({ when: 'now' });
  assert.equal(job.status, 'paused');
  assert.equal(job.units[0].status, 'queued');
  assert.equal(job.progress.running, 0);
  await assert.rejects(job.pause({ when: 'bientôt' }), (e) => e.code === 'invalid_request' && e.param === 'when');
  assert.equal(fake.received(`/aiodos/v1/jobs/${job.id}/pause`).length, 1);
});

test('annuler : le calcul en cours s’arrête, les fichiers déjà rendus restent ; un travail fini ne s’annule plus', async () => {
  const { client, fake } = manual();
  const job = await client.comfy.submit({ graph: GRAPH, items: TWO });
  fake.step(); // unité 1 en cours
  fake.step(); // unité 1 finie : un fichier
  fake.step(); // unité 2 en cours
  await job.cancel();
  assert.equal(job.status, 'cancelling');
  assert.equal(job.cancelledBy, 'app');
  assert.equal(fake.received(`/aiodos/v1/jobs/${job.id}/cancel`)[0].json, null);
  fake.step(); // le calcul s'arrête dans ComfyUI
  await job.wait();
  assert.equal(job.status, 'cancelled');
  assert.equal(job.state.files.length, 1);
  assert.deepEqual(job.units.map((u) => u.status), ['completed', 'cancelled']);
  await assert.rejects(job.cancel(), (e) => e.name === 'JobNotCancellable' && e.status === 409);
  await assert.rejects(job.pause(), (e) => e.name === 'JobNotPausable' && e.status === 409);
});

test('retryFailed : les unités en échec repartent', async () => {
  const { client, fake } = createFakeClient({ fake: L2 });
  fake.nextJob({ failUnits: [1] });
  const job = await client.comfy.submit({ graph: GRAPH, items: TWO });
  await job.wait();
  assert.equal(job.status, 'completed'); // une unité faite : fini, avec un échec
  assert.equal(job.progress.failed, 1);
  assert.equal(job.units[1].error.code, 'engine_error');
  await job.retryFailed();
  assert.notEqual(job.status, 'completed');
  await job.wait();
  assert.equal(job.status, 'completed');
  assert.equal(job.progress.failed, 0);
  assert.equal(job.state.files.length, 2);
});

test('delete : un travail en cours s’annule d’abord ; ses fichiers gardés, ou effacés', async () => {
  const { client, fake } = manual();
  const kept = await client.comfy.submit({ graph: GRAPH, items: TWO });
  await assert.rejects(kept.delete(), (e) => e.name === 'JobNotCancellable');
  fake.finish(kept.id);
  await kept.refresh();
  const fileId = kept.state.files[0].id;
  assert.deepEqual(await kept.delete(), { id: kept.id, deleted: true });
  assert.equal(kept.deleted, true);
  assert.match(fake.requests.filter((r) => r.method === 'DELETE').at(-1).path, /\?files=keep$/);
  await assert.rejects(client.job(kept.id), (e) => e.name === 'JobNotFound');
  assert.equal((await client.file(fileId)).id, fileId); // le fichier suit sa garde

  const gone = await client.comfy.submit({ graph: GRAPH, items: TWO });
  fake.finish(gone.id);
  await gone.refresh();
  const goneFile = gone.state.files[0].id;
  await gone.delete({ files: true });
  assert.match(fake.requests.filter((r) => r.method === 'DELETE').at(-1).path, /\?files=delete$/);
  await assert.rejects(client.file(goneFile), (e) => e.name === 'FileNotFound');
});

test('jobs : en cours par défaut ; tous avec clientRef ou status ; limit', async () => {
  const { client, fake } = manual();
  const finished = await client.comfy.submit({ graph: GRAPH, clientRef: 'arklight:a' });
  fake.finish(finished.id);
  const running = await client.comfy.submit({ graph: GRAPH, clientRef: 'arklight:b' });
  const ids = (list) => list.map((j) => j.id);
  assert.deepEqual(ids(await client.jobs()), [running.id]);
  assert.deepEqual(ids(await client.jobs({ active: null })), [running.id, finished.id]);
  assert.deepEqual(ids(await client.jobs({ active: false })), [finished.id]);
  assert.deepEqual(ids(await client.jobs({ clientRef: 'arklight:a' })), [finished.id]);
  assert.deepEqual(ids(await client.jobs({ status: 'completed' })), [finished.id]);
  assert.deepEqual(ids(await client.jobs({ status: ['queued', 'in_progress'] })), [running.id]);
  assert.deepEqual(ids(await client.jobs({ active: null, limit: 1 })), [running.id]);
  const asked = fake.received('/aiodos/v1/jobs').filter((r) => r.method === 'GET').map((r) => new URL(r.path, 'http://x').search);
  assert.deepEqual(asked, ['?active=1', '', '?active=0', '?client_ref=arklight%3Aa', '?status=completed', '?status=queued%2Cin_progress', '?limit=1']);
  await assert.rejects(client.jobs({ limit: 0 }), (e) => e.code === 'invalid_request' && e.param === 'limit');
  await assert.rejects(client.job(''), (e) => e.code === 'invalid_request' && e.param === 'id');
  const same = await client.job(running.id);
  assert.equal(same.id, running.id);
  assert.equal(String(same), `[travail ${running.id} · queued 0 %]`);
  assert.equal(JSON.parse(JSON.stringify(same)).id, running.id);
});

test('« jobs.pause » pas annoncé : pause et resume NotSupported, cancel marche', async () => {
  const { client, fake } = manual({ features: FAKE_FEATURES.filter((f) => f !== 'jobs.pause') });
  const job = await client.comfy.submit({ graph: GRAPH });
  await assert.rejects(job.pause(), (e) => e.name === 'NotSupported' && e.detail.feature === 'jobs.pause');
  await assert.rejects(job.resume(), (e) => e.name === 'NotSupported' && e.detail.feature === 'jobs.pause');
  assert.equal(fake.received(`/aiodos/v1/jobs/${job.id}/pause`).length, 0);
  await job.cancel();
  assert.equal(job.status, 'cancelled');
});

test('run : JobFailed pour un travail en échec, ou annulé ailleurs ; arrêté par l’appli, il est annulé dans AIODOS', async (t) => {
  const dir = await mkdtemp(path.join(tmpdir(), 'aiodos-run-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const { client, fake } = createFakeClient({ fake: L2 });
  fake.nextJob({ failUnits: [0] });
  await assert.rejects(client.comfy.run({ graph: GRAPH, destDir: dir }), (e) => {
    assert.ok(e instanceof JobFailed);
    assert.equal(e.code, 'job_failed');
    assert.equal(e.detail.status, 'failed');
    assert.equal(e.detail.units[0].error.code, 'engine_error');
    assert.match(e.message, /^Le travail a échoué : /);
    assert.equal(e.job, e.detail.job);
    return true;
  });

  const m = manual();
  let id = null;
  await assert.rejects(
    m.client.comfy.run({
      graph: GRAPH,
      destDir: dir,
      onEvent: async (ev) => {
        if (id) return;
        id = ev.job.id;
        await m.client.job(id).then((j) => j.cancel()); // annulé ailleurs (l'appli, ou l'utilisateur dans AIODOS)
      },
    }),
    (e) => e.name === 'JobFailed' && e.detail.status === 'cancelled' && e.message === 'Le travail a été annulé dans AIODOS.',
  );

  const stopped = manual();
  const ctrl = new AbortController();
  await assert.rejects(
    stopped.client.comfy.run({ graph: GRAPH, destDir: dir, signal: ctrl.signal, onEvent: () => ctrl.abort() }),
    (e) => e.name === 'Cancelled' && e.message === 'Arrêté',
  );
  const [job] = stopped.fake.jobs.values();
  assert.equal(job.status, 'cancelled'); // le calcul s'arrête aussi dans AIODOS
  assert.equal(job.cancelledBy, 'app');

  await assert.rejects(client.comfy.run({ graph: GRAPH }), (e) => e.code === 'invalid_request' && e.param === 'destDir');
});
