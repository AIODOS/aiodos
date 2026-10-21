// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : le contrat des travaux ComfyUI (clients/contract/jobs.json).
// Chacune des réponses est comprise par le client ; et le faux AIODOS sert chaque exemple
// côté serveur, dans l'ordre, avec la même règle que l'essai de la passerelle
// (tests/test_gateway_engines.py).

import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { GraphInvalid, InputHashMismatch, JobNotCancellable, JobNotFound, ModelUnavailable, ScopeDenied } from '../src/index.js';
import { FAKE_FEATURES, FAKE_KEY, FAKE_PNG, createFakeClient, createFakeFetch } from '../src/fake.js';

const contract = JSON.parse(readFileSync(new URL('../../contract/jobs.json', import.meta.url), 'utf8'));
const byName = (name) => {
  const it = contract.interactions.find((i) => i.name === name);
  assert.ok(it, `« ${name} » absent de jobs.json`);
  return structuredClone(it);
};
const sha = (b) => createHash('sha256').update(b).digest('hex');
const PNG_SHA = sha(FAKE_PNG);
const L2 = { features: FAKE_FEATURES, scopes: ['chat', 'engines:comfyui'] };
const JOB_ID = 'job_EUi5rlF-uTASX7Pm';
const FILE_ID = 'file_juJXvOesHQXzSYVLCmYBXw';

/** Le corps d'une requête du contrat, {sha256} remplacé. */
function filled(json) {
  return JSON.parse(JSON.stringify(json).replaceAll('{sha256}', PNG_SHA));
}

/** Les options de submit d'après le corps d'une requête du contrat. */
function submitOptions(json) {
  return { graph: json.graph, label: json.label, clientRef: json.client_ref ?? undefined, items: json.items, inputs: { ref0: FAKE_PNG } };
}

function l2Client() {
  return createFakeClient({ fake: L2 });
}

// --- chaque réponse est comprise ------------------------------------------------------------

test('moteurs : la liste, en camelCase', async () => {
  const { client, fake } = l2Client();
  fake.respondNext(byName('moteurs').response, { path: '/aiodos/v1/engines' });
  const [e] = await client.engines();
  assert.equal(e.id, 'comfyui');
  assert.deepEqual(e.here, { enabled: true, version: '0.35.1', vramMib: 8187 });
  assert.deepEqual(e.machines, { here: 1, mine: null, friends: null, public: null });
  assert.equal(e.models[0].name, 'sd_xl_base_1.0.safetensors');
  assert.deepEqual(e.models[0].where, { here: true, mine: [], friends: null, public: null });
  assert.deepEqual(e.objectInfo, { available: true, machine: 'PC-BUREAU', digest: 'c0a1f3e29b7d4e51' });
  assert.equal(e.recipes, 0);
});

test('entrée envoyée, entrée déjà là, entrée inconnue : HEAD puis PUT seulement s’il manque', async () => {
  const { client, fake } = l2Client();
  const isPut = (r) => r.method === 'PUT';
  fake.respondNext(byName('entrée envoyée').response, { path: `/aiodos/v1/inputs/${PNG_SHA}`, when: isPut });
  assert.equal(await client.inputs.put(FAKE_PNG), PNG_SHA);
  const [head, put] = fake.received(`/aiodos/v1/inputs/${PNG_SHA}`);
  assert.equal(head.method, 'HEAD'); // « entrée inconnue » : 404, donc l'envoi
  assert.equal(put.method, 'PUT');
  assert.equal(put.headers['content-type'], 'image/png');
  assert.equal(put.size, FAKE_PNG.length);
  // « entrée déjà là » : HEAD 200, rien n'est renvoyé
  fake.respondNext(byName('entrée déjà là').response, { path: `/aiodos/v1/inputs/${PNG_SHA}` });
  assert.equal(await client.inputs.put(FAKE_PNG), PNG_SHA);
  assert.equal(fake.received(`/aiodos/v1/inputs/${PNG_SHA}`).length, 3);
});

test('entrée à la mauvaise empreinte : InputHashMismatch', async () => {
  const { client, fake } = l2Client();
  const it = byName('entrée à la mauvaise empreinte');
  fake.respondNext(it.response, { path: `/aiodos/v1/inputs/${PNG_SHA}`, when: (r) => r.method === 'PUT' });
  await assert.rejects(client.inputs.put(FAKE_PNG), (e) => {
    assert.ok(e instanceof InputHashMismatch);
    assert.equal(e.code, 'input_hash_mismatch');
    assert.equal(e.status, 400);
    assert.equal(e.hint, it.response.json.error.aiodos.hint);
    assert.deepEqual(e.detail, it.response.json.error.aiodos.detail);
    return true;
  });
});

test('pré-vol : le corps du contrat part, la réponse est rendue en camelCase', async () => {
  const { client, fake } = l2Client();
  const it = byName('pré-vol');
  fake.respondNext(it.response, { path: '/aiodos/v1/engines/comfyui/plan' });
  const plan = await client.comfy.plan(submitOptions(it.request.json));
  assert.deepEqual(plan, { ok: true, whereEffective: ['here'], whereReason: null, machines: { here: true, mine: null, friends: null, public: null }, missing: [], refused: {}, licenses: [], units: 2, estimate: null });
  assert.deepEqual(fake.received('/aiodos/v1/engines/comfyui/plan')[0].json, filled(it.request.json));
});

test('travail déposé, puis le même client_ref : le même travail', async () => {
  const { client, fake } = l2Client();
  const it = byName('travail déposé');
  fake.respondNext(it.response, { path: '/aiodos/v1/jobs' });
  const job = await client.comfy.submit(submitOptions(it.request.json));
  assert.deepEqual(fake.received('/aiodos/v1/jobs')[0].json, filled(it.request.json)); // exactement le corps du contrat
  assert.equal(job.id, JOB_ID);
  assert.equal(job.status, 'queued');
  assert.equal(job.label, 'Moods · Séq. 3');
  assert.equal(job.clientRef, 'pandora:mood:5f2c9a');
  assert.equal(job.engine, 'comfyui');
  assert.equal(job.priority, 'normal');
  assert.deepEqual(job.whereEffective, ['here']);
  assert.equal(job.progress.total, 2);
  assert.equal(job.progress.elapsedS, 11);
  assert.equal(job.progress.message, '0 sur 2');
  assert.equal(job.units[0].stage, 'waiting_machine');
  assert.equal(job.state.pause, null);
  assert.deepEqual(job.state.files, []);
  assert.equal(job.done, false);
  assert.equal(job.startedAt, null);
  assert.equal(job.createdAt, 1760950000);
  const again = byName('même client_ref : le même travail');
  fake.respondNext(again.response, { path: '/aiodos/v1/jobs' });
  const same = await client.comfy.submit(submitOptions(again.request.json));
  assert.equal(same.id, job.id);
});

test('travail en cours, travail fini : les champs, les unités, les fichiers', async () => {
  const { client, fake } = l2Client();
  fake.respondNext(byName('travail en cours').response, { path: `/aiodos/v1/jobs/${JOB_ID}` });
  const job = await client.job(JOB_ID);
  assert.equal(job.status, 'in_progress');
  assert.equal(job.progress.percent, 50);
  assert.equal(job.progress.stage, 'running');
  assert.deepEqual(job.progress.machines, [{ name: 'PC-BUREAU', place: 'here', running: 1, done: 1 }]);
  assert.equal(job.progress.message, '1 sur 2 · 1 en cours sur 1 machine');
  assert.deepEqual(job.units[0].files, [FILE_ID]);
  assert.equal(job.units[0].seconds, 10.6);
  assert.equal(job.units[1].stage, 'running');
  const [f] = job.state.files;
  assert.equal(f.id, FILE_ID);
  assert.equal(f.unit, 0);
  assert.equal(f.node, '9');
  assert.equal(f.type, 'image/png');
  assert.equal(f.bytes, 1014522);
  assert.equal(f.width, 768);
  assert.equal(f.machine, 'PC-BUREAU');
  assert.equal(f.place, 'here');
  assert.equal(f.engine, 'comfyui 0.35.1');
  assert.equal(f.verified, 'none');
  assert.equal(f.fetched, false);
  assert.equal(f.expiresAt, null);
  assert.equal(f.createdAt, 1760950013);
  fake.respondNext(byName('travail fini').response, { path: `/aiodos/v1/jobs/${JOB_ID}` });
  assert.equal(await job.refresh(), job);
  assert.equal(job.status, 'completed');
  assert.equal(job.done, true);
  assert.equal(job.completedAt, 1760950017);
  assert.equal(job.progress.done, 2);
  assert.equal(job.state.files.length, 2);
  assert.equal(job.state.files[0], f); // le même fichier reste le même objet
});

test('ses travaux : par client_ref, finis compris', async () => {
  const { client, fake } = l2Client();
  fake.respondNext(byName('ses travaux').response, { path: '/aiodos/v1/jobs' });
  const jobs = await client.jobs({ clientRef: 'pandora:mood:5f2c9a' });
  assert.deepEqual(jobs.map((j) => [j.id, j.status]), [[JOB_ID, 'completed']]);
  const [req] = fake.received('/aiodos/v1/jobs');
  const q = new URL(req.path, 'http://x').searchParams;
  assert.equal(q.get('client_ref'), 'pandora:mood:5f2c9a');
  assert.equal(q.get('active'), null); // avec client_ref : tous, finis compris
});

test('événements jusqu’à la fin : job, file, end ; « : ping » ignoré ; chaque fichier une fois', async () => {
  const { client, fake } = l2Client();
  fake.respondNext(byName('travail en cours').response, { path: `/aiodos/v1/jobs/${JOB_ID}` });
  const job = await client.job(JOB_ID);
  const it = byName('événements jusqu\'à la fin');
  fake.respondNext({ ...it.response }, { path: `/aiodos/v1/jobs/${JOB_ID}/events` });
  const seen = [];
  for await (const ev of job.watch()) {
    seen.push(ev.type === 'job' ? `job:${ev.job.status}` : ev.type === 'file' ? `file:${ev.file.id}` : ev.type === 'end' ? `end:${ev.status}` : ev.type);
    assert.equal(ev.job, job);
  }
  assert.deepEqual(seen, [
    'job:queued',
    'job:in_progress',
    `file:${FILE_ID}`,
    'file:file_bvuRVoags4c-28D6pPV-yw',
    'job:completed',
    'end:completed',
  ]);
  assert.equal(job.status, 'completed');
  const [req] = fake.received(`/aiodos/v1/jobs/${JOB_ID}/events`);
  assert.equal(req.headers.accept, 'text/event-stream');
});

test('fichiers pas encore récupérés, un fichier', async () => {
  const { client, fake } = l2Client();
  fake.respondNext(byName('travail fini').response, { path: `/aiodos/v1/jobs/${JOB_ID}` });
  const job = await client.job(JOB_ID);
  fake.respondNext(byName('fichiers pas encore récupérés').response, { path: `/aiodos/v1/jobs/${JOB_ID}/files` });
  const files = await job.files();
  assert.deepEqual(files.map((f) => f.id), [FILE_ID, 'file_bvuRVoags4c-28D6pPV-yw']);
  assert.equal(files[0], job.state.files[0]);
  assert.match(fake.received(`/aiodos/v1/jobs/${JOB_ID}/files`)[0].path, /\?unfetched=1$/);
  fake.respondNext(byName('un fichier').response, { path: `/aiodos/v1/files/${FILE_ID}` });
  const card = await client.file(FILE_ID);
  assert.equal(card.type, 'image/png');
  assert.equal(card.verified, 'none');
  assert.equal(card.sha256, '9b71d224bd62f3785d96d46ad3ea3d73319bfbc2890caadae2dff72519673ca7');
});

test('le contenu d’un fichier, par plage : un fichier partiel repris (Range, 206)', async () => {
  const { client, fake } = l2Client();
  const first = FAKE_PNG.subarray(0, 10);
  const whole = Buffer.concat([first, first]); // ses 10 derniers octets sont ceux de la plage du contrat
  const card = { ...byName('un fichier').response.json, bytes: whole.length, sha256: sha(whole) };
  fake.respondNext({ status: 200, headers: { 'X-Aiodos-Api': '1' }, json: card }, { path: `/aiodos/v1/files/${FILE_ID}` });
  const f = await client.file(FILE_ID);
  const it = byName('le contenu d\'un fichier, par plage');
  fake.respondNext({ status: it.response.status, headers: it.response.headers, bytes: first }, { path: `/aiodos/v1/files/${FILE_ID}/content` });
  const dir = await mkdtemp(path.join(tmpdir(), 'aiodos-plage-'));
  try {
    const dest = path.join(dir, 'mood.png');
    await writeFile(`${dest}.part`, first); // déjà reçus : les 10 premiers octets
    assert.equal(await f.save(dest), dest);
    assert.ok((await readFile(dest)).equals(whole));
    const [req] = fake.received(`/aiodos/v1/files/${FILE_ID}/content`);
    assert.equal(req.headers.range, 'bytes=10-');
    assert.equal(f.fetched, true);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('pause après l’unité en cours, annulation : l’état rendu', async () => {
  const { client, fake } = l2Client();
  fake.respondNext(byName('travail en cours').response, { path: `/aiodos/v1/jobs/${JOB_ID}` });
  const job = await client.job(JOB_ID);
  fake.respondNext(byName('pause après l\'unité en cours').response, { path: `/aiodos/v1/jobs/${JOB_ID}/pause` });
  assert.equal(await job.pause(), job);
  assert.equal(job.status, 'pausing');
  assert.deepEqual(job.state.pause, { when: 'after_current', pending: 1 });
  assert.deepEqual(fake.received(`/aiodos/v1/jobs/${JOB_ID}/pause`)[0].json, { when: 'after_current' });
  fake.respondNext(byName('annulation').response, { path: `/aiodos/v1/jobs/${JOB_ID}/cancel` });
  await job.cancel();
  assert.equal(job.status, 'cancelling');
  assert.equal(job.cancelledBy, 'app');
  assert.equal(job.units[1].status, 'cancelled');
  assert.equal(fake.received(`/aiodos/v1/jobs/${JOB_ID}/cancel`)[0].json, null); // sans corps, comme le contrat
});

test('annulation d’un travail fini, travail d’une autre appli : JobNotCancellable, JobNotFound', async () => {
  const { client, fake } = l2Client();
  fake.respondNext(byName('travail fini').response, { path: `/aiodos/v1/jobs/${JOB_ID}` });
  const job = await client.job(JOB_ID);
  fake.respondNext(byName("annulation d'un travail fini").response, { path: `/aiodos/v1/jobs/${JOB_ID}/cancel` });
  await assert.rejects(job.cancel(), (e) => e instanceof JobNotCancellable && e.code === 'job_not_cancellable' && e.status === 409 && e.message === 'Ce travail est déjà fini.');
  fake.respondNext(byName("travail d'une autre appli").response, { path: '/aiodos/v1/jobs/job_autre' });
  await assert.rejects(client.job('job_autre'), (e) => e instanceof JobNotFound && e.status === 404 && e.message === 'Travail introuvable.');
});

test('graphe au format de l’éditeur, modèle absent, sans le droit : GraphInvalid, ModelUnavailable, ScopeDenied', async () => {
  const { client, fake } = l2Client();
  const editor = byName("graphe au format de l'éditeur");
  fake.respondNext(editor.response, { path: '/aiodos/v1/jobs' });
  await assert.rejects(client.comfy.submit({ graph: editor.request.json.graph }), (e) => {
    assert.ok(e instanceof GraphInvalid);
    assert.equal(e.code, 'graph_not_api');
    assert.equal(e.param, 'graphs');
    assert.equal(e.graph, 0);
    assert.equal(e.hint, 'Exporte le graphe au format API dans ComfyUI.');
    return true;
  });
  const absent = byName('modèle absent');
  fake.respondNext(absent.response, { path: '/aiodos/v1/jobs' });
  await assert.rejects(client.comfy.submit(submitOptions(absent.request.json)), (e) => {
    assert.ok(e instanceof ModelUnavailable);
    assert.deepEqual(e.models, [{ folder: null, name: 'absent.safetensors' }]);
    assert.equal(e.retryable, true);
    assert.equal(e.status, 409);
    return true;
  });
  const denied = byName('sans le droit engines:comfyui');
  fake.respondNext(denied.response, { path: '/aiodos/v1/jobs' });
  await assert.rejects(client.comfy.submit(submitOptions(denied.request.json)), (e) => e instanceof ScopeDenied && e.scope === 'engines:comfyui' && e.status === 403);
});

// --- le faux sert le contrat comme la passerelle ------------------------------------------------

const typeOf = (v) => (v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v);
function shapeErrors(example, actual, at = '') {
  if (typeOf(example) !== typeOf(actual)) return [`${at || '.'} : ${typeOf(example)} attendu, ${typeOf(actual)} reçu`];
  if (typeOf(example) === 'array') return example.length && actual.length ? shapeErrors(example[0], actual[0], `${at}.0`) : [];
  if (typeOf(example) !== 'object') return [];
  return Object.keys(example).flatMap((k) => (k in actual ? shapeErrors(example[k], actual[k], at ? `${at}.${k}` : k) : [`${at ? `${at}.` : ''}${k} : absent`]));
}
const dig = (obj, dotted) => dotted.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);

test('le faux AIODOS sert jobs.json dans l’ordre, comme la passerelle (même règle que son essai)', async () => {
  const fetch = createFakeFetch({ ...L2, jobMode: 'manual' });
  const fake = fetch.fake;
  const chatOnly = fake.addKey(`aiodos_app_texte234_${'B'.repeat(43)}`, { scopes: ['chat'] });
  const found = { sha256: PNG_SHA };
  const fill = (s) => Object.entries(found).reduce((v, [k, x]) => v.replaceAll(`{${k}}`, x), s);
  let checked = 0;
  for (const it of contract.interactions) {
    if (it.server === 'client') continue;
    if (it.name === 'travail fini') fake.finish(found.job_id);
    if (it.name === 'un fichier') {
      const listed = await fetch(`${fake.url}/aiodos/v1/jobs/${found.job_id}/files`, { headers: { Authorization: `Bearer ${FAKE_KEY}` } });
      found.file_id = (await listed.json()).files[0].id;
    }
    const key = it.name === 'sans le droit engines:comfyui' ? chatOnly : FAKE_KEY;
    const headers = Object.fromEntries(Object.entries(it.request.headers || {}).map(([k, v]) => [k, v.replace('{key}', key)]));
    let body;
    if (it.request.body === '{png}') body = FAKE_PNG;
    else if (it.request.json != null) {
      body = fill(JSON.stringify(it.request.json));
      headers['Content-Type'] = 'application/json';
    }
    const res = await fetch(`${fake.url}${fill(it.request.path)}`, { method: it.request.method, headers, body, redirect: 'error' });
    assert.equal(res.status, it.response.status, `${it.name} : statut`);
    for (const [h, v] of Object.entries(it.response.headers || {})) assert.equal(res.headers.get(h), v, `${it.name} : en-tête ${h}`);
    if (it.response.body === '{png:0-9}') {
      assert.ok(Buffer.from(await res.arrayBuffer()).equals(FAKE_PNG.subarray(0, 10)), it.name);
    } else {
      const text = await res.text();
      const got = text ? JSON.parse(text) : null;
      if (it.response.json != null) {
        assert.deepEqual(shapeErrors(it.response.json, got), [], it.name);
        for (const p of it.exact || []) assert.deepEqual(dig(got, p), dig(it.response.json, p), `${it.name} : ${p}`);
      }
      if (it.name === 'travail déposé') found.job_id = got.id;
    }
    checked += 1;
  }
  assert.equal(checked, contract.interactions.filter((i) => i.server !== 'client').length);
  assert.ok(checked >= 17, `${checked} exemples vérifiés`);
});

test('le contrat compte ses 22 interactions, toutes rejouées ici ou côté client', () => {
  assert.equal(contract.interactions.length, 22);
});
