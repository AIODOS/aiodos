// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : ComfyUI par AIODOS, de bout en bout contre le faux
// (tokenize → submit → watch → download, run), clientRef, le lieu (where, sensitive), les
// refus avant tout envoi, les erreurs du faux, l'isolation entre applis, et le même parcours
// par un vrai serveur HTTP et le vrai fetch.

import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { inspect } from 'node:util';

import { createClient, memoryKeyStore } from '../src/index.js';
import { FAKE_FEATURES, FAKE_KEY, FAKE_PNG, contractInteraction, createFakeClient, startFakeServer } from '../src/fake.js';

const L2 = { features: FAKE_FEATURES, scopes: ['chat', 'engines:comfyui'] };
const GRAPH = contractInteraction('jobs', 'travail déposé').request.json.graph;
const PNG_SHA = createHash('sha256').update(FAKE_PNG).digest('hex');
const ITEMS = [
  { label: 'Mood 1', seed: 1201, set: [['6', 'text', 'Salon baigné de lumière dorée']] },
  { label: 'Mood 2', seed: 1202, set: [['6', 'text', 'Cuisine de nuit, néons']] },
];
const NO_INSTALL = { AIODOS_DISCOVERY_FILE: '-', AIODOS_HOME: path.join(tmpdir(), 'aiodos-client-aucune-installation') };

/** Le graphe du contrat, son nœud LoadImage encore rempli à la façon de l'éditeur. */
function rawGraph() {
  const g = structuredClone(GRAPH);
  g['10'].inputs.image = 'reference.png';
  return g;
}

async function tempDir(t) {
  const dir = await mkdtemp(path.join(tmpdir(), 'aiodos-comfy-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  return dir;
}

test('de bout en bout : tokenize → submit → watch → download', async (t) => {
  const { client, fake } = createFakeClient({ fake: L2 });
  const dir = await tempDir(t);
  const original = rawGraph();
  const { graph, inputs } = await client.comfy.tokenize(original, { 10: FAKE_PNG });
  assert.equal(graph['10'].inputs.image, '@in:10');
  assert.equal(original['10'].inputs.image, 'reference.png'); // une copie : l'original ne change pas
  assert.ok(inputs['10'].equals(FAKE_PNG));

  const job = await client.comfy.submit({ graph, inputs, items: ITEMS, label: 'Moods · Séq. 3', clientRef: 'arklight:mood:1' });
  assert.equal(job.status, 'queued');
  // l'entrée d'abord (HEAD, puis PUT), puis le travail
  const order = fake.requests.filter((r) => r.path !== '/aiodos/v1/info' && !r.path.startsWith('/aiodos/v1/info?')).map((r) => `${r.method} ${r.path}`);
  assert.deepEqual(order, [`HEAD /aiodos/v1/inputs/${PNG_SHA}`, `PUT /aiodos/v1/inputs/${PNG_SHA}`, 'POST /aiodos/v1/jobs']);
  const body = fake.received('/aiodos/v1/jobs')[0].json;
  assert.deepEqual(body.inputs, { 10: { sha256: PNG_SHA } });
  assert.equal(body.engine, 'comfyui');
  assert.equal(body.client_ref, 'arklight:mood:1');
  for (const k of ['priority', 'models', 'strict', 'where']) assert.equal(body[k], undefined, k); // les défauts ne partent pas

  const seen = [];
  const files = [];
  for await (const ev of job.watch()) {
    seen.push(ev.type);
    if (ev.type === 'file') files.push(ev.file.id);
    if (ev.type === 'end') assert.equal(ev.status, 'completed');
  }
  assert.equal(seen[0], 'job');
  assert.equal(seen.at(-1), 'end');
  assert.equal(files.length, 2);
  assert.equal(new Set(files).size, 2);
  assert.equal(job.status, 'completed');
  assert.equal(job.progress.percent, 100);

  const got = [];
  const paths = await job.download(dir, { stem: 'mood', onFile: (p, f) => got.push([path.basename(p), f.id]) });
  assert.deepEqual(paths.map((p) => path.basename(p)), ['mood-1.png', 'mood-2.png']);
  assert.deepEqual(got.map(([, id]) => id), job.state.files.map((f) => f.id));
  for (const p of paths) assert.ok((await readFile(p)).equals(FAKE_PNG));
  assert.deepEqual((await readdir(dir)).sort(), ['mood-1.png', 'mood-2.png']); // aucun .part ne reste
  // récupérés : AIODOS les garde encore 7 jours ; plus rien à récupérer
  assert.ok([...fake.files.values()].every((f) => f.fetchedAt != null));
  assert.deepEqual(await job.files(), []);
  // relancé, download ne redemande rien
  const before = fake.requests.filter((r) => r.path.endsWith('/content')).length;
  assert.deepEqual(await job.download(dir, { stem: 'mood' }), paths);
  assert.equal(fake.requests.filter((r) => r.path.endsWith('/content')).length, before);
});

test('run : dépose, attend la fin, télécharge ; onEvent, onFile', async (t) => {
  const { client } = createFakeClient({ fake: L2 });
  const dir = await tempDir(t);
  const events = [];
  const saved = [];
  const paths = await client.comfy.run({
    graph: GRAPH,
    inputs: { ref0: FAKE_PNG },
    items: ITEMS,
    destDir: dir,
    stem: 'plan',
    onEvent: (ev) => events.push(ev.type),
    onFile: (p) => saved.push(path.basename(p)),
  });
  assert.deepEqual(paths.map((p) => path.basename(p)), ['plan-1.png', 'plan-2.png']);
  assert.deepEqual(saved, ['plan-1.png', 'plan-2.png']);
  assert.equal(events.at(-1), 'end');
  assert.ok(events.includes('file'));
});

test('le même clientRef rend le même travail ; l’entrée déjà envoyée ne repart pas', async () => {
  const { client, fake } = createFakeClient({ fake: L2 });
  const o = { graph: GRAPH, inputs: { ref0: FAKE_PNG }, items: ITEMS, clientRef: 'arklight:mood:2' };
  const a = await client.comfy.submit(o);
  const b = await client.comfy.submit(o);
  assert.equal(b.id, a.id);
  assert.equal(fake.jobs.size, 1);
  assert.equal(fake.requests.filter((r) => r.method === 'PUT').length, 1); // la deuxième fois : HEAD 200 seulement
  await a.wait();
  // retrouvé après un redémarrage de l'appli : par sa référence, même fini
  const [found] = await client.jobs({ clientRef: 'arklight:mood:2' });
  assert.equal(found.id, a.id);
  assert.equal(found.status, 'completed');
  // l'en-tête Idempotency-Key ne sert pas : le client donne client_ref dans le corps
  assert.equal(fake.received('/aiodos/v1/jobs')[1].json.client_ref, 'arklight:mood:2');
});

test('le lieu : where et sensitive passent par X-Aiodos-Where, dans l’accord', async () => {
  const { client, fake } = createFakeClient({ fake: { ...L2, jobMode: 'manual' } });
  const o = { graph: GRAPH, inputs: { ref0: FAKE_PNG } };
  const sent = () => fake.received('/aiodos/v1/jobs').at(-1);
  await client.comfy.submit({ ...o, where: 'here' });
  assert.equal(sent().headers['x-aiodos-where'], 'here');
  assert.equal(sent().json.where, undefined);
  // sensible : resserré à here et mine, d'après l'accord lu avec la clé
  await client.comfy.submit({ ...o, sensitive: true });
  assert.equal(sent().headers['x-aiodos-where'], 'here,mine');
  assert.ok(fake.requests.some((r) => r.path === '/aiodos/v1/info' && r.headers.authorization === `Bearer ${FAKE_KEY}`));
  await client.comfy.submit({ ...o, sensitive: true, where: ['here', 'friends'] });
  assert.equal(sent().headers['x-aiodos-where'], 'here');
  // un accord sans ce PC ni mes machines : refusé avant tout envoi
  const posts = fake.received('/aiodos/v1/jobs').length;
  fake.keys.get(FAKE_KEY).where = ['friends'];
  await assert.rejects(client.comfy.submit({ ...o, sensitive: true }), (e) => e.code === 'where_denied' && e.name === 'WhereDenied');
  assert.equal(fake.received('/aiodos/v1/jobs').length, posts);
  // plus large que l'accord : AIODOS refuse
  fake.keys.get(FAKE_KEY).where = ['here'];
  await assert.rejects(client.comfy.submit({ ...o, where: ['here', 'mine'] }), (e) => e.code === 'where_denied' && e.status === 403);
  await assert.rejects(client.comfy.submit({ ...o, where: 'partout' }), (e) => e.code === 'invalid_request' && e.param === 'where');
});

test('refusé avant tout envoi : graphe, priorité, entrées, recette', async () => {
  const { client, fake } = createFakeClient({ fake: L2 });
  const cases = [
    [{}, 'graph'],
    [{ graph: GRAPH, graphs: [GRAPH] }, 'graph'],
    [{ graph: [] }, 'graph'],
    [{ graphs: [] }, 'graphs'],
    [{ graph: GRAPH, items: [], repeat: { count: 2 } }, 'items'],
    [{ graph: GRAPH, priority: 'urgent' }, 'priority'],
    [{ graph: GRAPH, models: 'tous' }, 'models'],
    [{ graph: GRAPH, inputs: { 'ref 0': FAKE_PNG } }, 'inputs'],
    [{ graph: GRAPH, inputs: { ref0: 42 } }, 'inputs'],
    [{ graph: GRAPH, inputs: { ref0: { data: FAKE_PNG, type: 'png' } } }, 'inputs'],
    [{ graph: GRAPH, inputs: { ref0: Buffer.alloc(0) } }, 'inputs'],
    [{ graph: GRAPH, inputs: { ref0: path.join(tmpdir(), 'aiodos-absent', 'rien.png') } }, 'inputs'],
  ];
  for (const [o, param] of cases) {
    await assert.rejects(client.comfy.submit(o), (e) => e.code === 'invalid_request' && e.param === param, JSON.stringify(Object.keys(o)));
  }
  await assert.rejects(client.comfy.submit({ graph: GRAPH, recipe: 'comfy-templates/image_sdxl_t2i' }), (e) => e.name === 'NotSupported' && e.detail.feature === 'recipes');
  assert.ok(fake.requests.every((r) => r.method === 'GET' && r.path.startsWith('/aiodos/v1/info')), 'rien ne doit partir');
});

test('les refus d’AIODOS : modèle absent, entrée jamais envoyée, fichier sans jeton, sans sortie, ComfyUI fermé', async () => {
  const { client, fake } = createFakeClient({ fake: L2 });
  const absent = structuredClone(GRAPH);
  absent['4'].inputs.ckpt_name = 'absent.safetensors';
  await assert.rejects(client.comfy.submit({ graph: absent, inputs: { ref0: FAKE_PNG } }), (e) => e.name === 'ModelUnavailable' && e.models[0].name === 'absent.safetensors');
  const plan = await client.comfy.plan({ graph: absent, inputs: { ref0: FAKE_PNG } });
  assert.equal(plan.ok, false);
  assert.deepEqual(plan.missing, [{ folder: null, name: 'absent.safetensors', machine: 'PC-BUREAU' }]);
  await assert.rejects(client.comfy.submit({ graph: GRAPH, inputs: { ref0: { sha256: '0'.repeat(64) } } }), (e) => e.name === 'InputMissing' && e.input === 'ref0');
  await assert.rejects(client.comfy.submit({ graph: rawGraph() }), (e) => e.name === 'ForbiddenValue' && e.reason === 'file_input' && e.node === '10' && e.input === 'image');
  const noOutput = structuredClone(GRAPH);
  delete noOutput['9'];
  await assert.rejects(client.comfy.submit({ graph: noOutput, inputs: { ref0: FAKE_PNG } }), (e) => e.name === 'GraphInvalid' && e.code === 'graph_invalid');
  await assert.rejects(client.comfy.submit({ graph: GRAPH, inputs: { ref0: FAKE_PNG }, items: [{ set: [['3', 'model', 'x']] }] }), (e) => e.name === 'GraphInvalid' && e.code === 'patch_invalid');
  const cloud = { ...structuredClone(GRAPH), 20: { class_type: 'OpenAIDalle3', inputs: { prompt: 'x' } } };
  await assert.rejects(client.comfy.submit({ graph: cloud, inputs: { ref0: FAKE_PNG } }), (e) => e.name === 'ForbiddenNode' && e.nodeClass === 'OpenAIDalle3');
  fake.comfy.enabled = false;
  await assert.rejects(client.comfy.submit({ graph: GRAPH, inputs: { ref0: FAKE_PNG } }), (e) => e.name === 'EngineUnavailable' && e.status === 409);
  const [engine] = await client.engines();
  assert.deepEqual(engine.here, { enabled: false, version: null, vramMib: null });
  assert.equal(engine.machines.here, 0);
  fake.comfy.enabled = true;
  fake.maxActiveJobs = 0;
  await assert.rejects(client.comfy.submit({ graph: GRAPH, inputs: { ref0: FAKE_PNG } }), (e) => e.name === 'QuotaExceeded' && e.quota === 'active_jobs' && e.status === 429);
});

test('tokenize : les nœuds de chargement, une entrée nommée, les refus', async () => {
  const { client } = createFakeClient({ fake: L2 });
  const graph = {
    1: { class_type: 'LoadVideo', inputs: { file: 'clip.mp4' } },
    2: { class_type: 'LoadAudio', inputs: { audio: 'son.wav' } },
    '5:12': { class_type: 'LoadImageMask', inputs: { image: 'masque.png', channel: 'alpha' } },
    7: { class_type: 'VHS_LoadVideo', inputs: { video: 'autre.mp4' } },
    9: { class_type: 'SaveImage', inputs: { images: ['1', 0] } },
  };
  const { graph: g, inputs } = await client.comfy.tokenize(graph, {
    1: '/videos/clip.mp4',
    2: { path: '/sons/son.wav', type: 'audio/wav' },
    '5:12': FAKE_PNG,
    7: { data: FAKE_PNG, input: 'video' },
  });
  assert.equal(g['1'].inputs.file, '@in:1');
  assert.equal(g['2'].inputs.audio, '@in:2');
  assert.equal(g['5:12'].inputs.image, '@in:5_12');
  assert.equal(g['5:12'].inputs.channel, 'alpha');
  assert.equal(g['7'].inputs.video, '@in:7');
  assert.deepEqual(Object.keys(inputs).sort(), ['1', '2', '5_12', '7']);
  assert.deepEqual(inputs['2'], { path: '/sons/son.wav', type: 'audio/wav' });
  assert.deepEqual(inputs['7'], { data: FAKE_PNG }); // « input » ne part pas
  await assert.rejects(client.comfy.tokenize(graph, { 9: FAKE_PNG }), (e) => e.code === 'invalid_request' && e.detail.class === 'SaveImage');
  await assert.rejects(client.comfy.tokenize(graph, { 42: FAKE_PNG }), (e) => e.code === 'invalid_request' && e.detail.node === '42');
  await assert.rejects(client.comfy.tokenize(graph, { 1: 12 }), (e) => e.code === 'invalid_request' && e.param === 'inputs');
  await assert.rejects(client.comfy.tokenize({ nodes: [], links: [] }, {}), (e) => e.name === 'GraphInvalid' && e.code === 'graph_not_api');
  await assert.rejects(client.comfy.tokenize(null, {}), (e) => e.code === 'invalid_request' && e.param === 'graph');
});

test('isolation : une appli ne voit ni les travaux, ni les fichiers, ni les entrées d’une autre', async () => {
  const a = createFakeClient({ fake: L2 });
  const otherKey = `aiodos_app_bbbbbbbb_${'C'.repeat(43)}`;
  a.fake.addKey(otherKey, { scopes: ['engines:comfyui'] });
  const b = createClient({ app: 'Autre', appId: 'arkane.autre', key: memoryKeyStore(otherKey), url: a.fake.url, fetch: a.fetch, env: NO_INSTALL });
  const job = await a.client.comfy.submit({ graph: GRAPH, inputs: { ref0: FAKE_PNG } });
  await job.wait();
  const [file] = job.state.files;
  await assert.rejects(b.job(job.id), (e) => e.name === 'JobNotFound');
  assert.deepEqual(await b.jobs({ active: null }), []);
  await assert.rejects(b.file(file.id), (e) => e.name === 'FileNotFound');
  // la même entrée : AIODOS ne la connaît pas pour B, qui l'envoie à son tour
  await assert.rejects(b.comfy.submit({ graph: GRAPH, inputs: { ref0: { sha256: PNG_SHA } } }), (e) => e.name === 'InputMissing');
  assert.equal(await b.inputs.put(FAKE_PNG), PNG_SHA);
  assert.equal(a.fake.requests.filter((r) => r.method === 'PUT').length, 2);
});

test('la clé n’apparaît jamais : travaux, fichiers, événements, erreurs, journal', async (t) => {
  const logs = [];
  const { client, fake } = createFakeClient({ fake: L2, log: (level, message, data) => logs.push(`${level} ${message} ${JSON.stringify(data ?? null)}`) });
  const secret = FAKE_KEY.slice(20);
  const dir = await tempDir(t);
  const events = [];
  const paths = await client.comfy.run({ graph: GRAPH, inputs: { ref0: FAKE_PNG }, destDir: dir, onEvent: (ev) => events.push(ev) });
  assert.equal(paths.length, 1);
  const job = await client.job(events[0].job.id);
  fake.revokeKey();
  const failure = await job.refresh().catch((e) => e);
  const shown = [String(job), JSON.stringify(job), inspect(job, { depth: 6 }), JSON.stringify(events), inspect(events, { depth: 6 }), String(job.state.files[0]), inspect(job.state.files[0]), JSON.stringify(failure), String(failure)];
  for (const s of [...shown, ...logs]) assert.ok(!s.includes(secret), s.slice(0, 200));
  assert.ok(logs.length > 0);
});

test('par un vrai serveur et le vrai fetch : dépôt, événements en direct, téléchargement repris', async (t) => {
  const server = await startFakeServer({ ...L2, jobMode: 'manual' });
  t.after(() => server.close());
  const dir = await tempDir(t);
  const client = createClient({ app: 'ARKLIGHT PC', appId: 'arkane.arklight', key: memoryKeyStore(FAKE_KEY), url: server.url, env: NO_INSTALL });
  const job = await client.comfy.submit({ graph: GRAPH, inputs: { ref0: FAKE_PNG }, items: ITEMS });
  const seen = [];
  for await (const ev of job.watch()) {
    seen.push(ev.type === 'job' ? `job:${ev.job.status}` : ev.type);
    if (seen.length === 1) server.fake.finish(job.id); // le reste arrive en direct
  }
  assert.equal(seen[0], 'job:queued');
  assert.deepEqual(seen.slice(-2), ['job:completed', 'end']);
  assert.equal(seen.filter((s) => s === 'file').length, 2);
  // le contenu coupé en route (connexion perdue) : repris par Range au deuxième essai
  server.fake.nextContent({ cutAt: 30 });
  const [first] = job.state.files;
  const dest = await first.save(path.join(dir, 'coupe.png'));
  assert.ok((await readFile(dest)).equals(FAKE_PNG));
  const asked = server.fake.received(`/aiodos/v1/files/${first.id}/content`);
  assert.equal(asked.length, 2);
  assert.equal(asked[1].headers.range, 'bytes=30-');
  // HEAD par le vrai fetch : sans corps
  assert.equal(await client.inputs.put(FAKE_PNG), PNG_SHA);
});
