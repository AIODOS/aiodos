// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : les fichiers d'un travail (File.save, content, delete, link ;
// job.download) : reprise d'un téléchargement coupé par Range, le « .part » gardé pour
// l'appel suivant, l'empreinte toujours vérifiée, rien de redemandé pour un fichier déjà là,
// des noms stables.

import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { FAKE_FEATURES, FAKE_PNG, contractInteraction, createFakeClient } from '../src/fake.js';
import { fileNames, safeStem } from '../src/download.js';

const GRAPH = (() => {
  const g = structuredClone(contractInteraction('jobs', 'travail déposé').request.json.graph);
  g['10'] = { class_type: 'EmptyLatentImage', inputs: { width: 512, height: 512, batch_size: 1 } };
  return g;
})();
const L2 = { features: FAKE_FEATURES, scopes: ['chat', 'engines:comfyui'] };

async function finishedJob(t, script) {
  const dir = await mkdtemp(path.join(tmpdir(), 'aiodos-fichiers-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const { client, fake } = createFakeClient({ fake: { ...L2, jobMode: 'manual' } });
  if (script) fake.nextJob(script);
  const job = await client.comfy.submit({ graph: GRAPH, items: script && script.items });
  fake.finish(job.id);
  await job.refresh();
  return { client, fake, job, dir, file: job.state.files[0] };
}

const contentRequests = (fake, file) => fake.received(`/aiodos/v1/files/${file.id}/content`);

test('coupé en route : repris par Range au deuxième essai, sans .part qui reste', async (t) => {
  const { fake, dir, file } = await finishedJob(t);
  fake.nextContent({ cutAt: 30 });
  const dest = await file.save(path.join(dir, 'a.png'));
  assert.ok((await readFile(dest)).equals(FAKE_PNG));
  const [first, second] = contentRequests(fake, file);
  assert.equal(first.headers.range, undefined);
  assert.equal(second.headers.range, 'bytes=30-');
  assert.deepEqual(await readdir(dir), ['a.png']);
  assert.equal(file.fetched, true);
  assert.ok(fake.files.get(file.id).fetchedAt != null); // servi jusqu'au dernier octet : récupéré
});

test('coupé, puis AIODOS arrêté : le .part reste, repris à l’appel suivant', async (t) => {
  const { fake, dir, file } = await finishedJob(t);
  fake.nextContent({ cutAt: 30 });
  const off = fake.on((kind, rec) => {
    if (kind === 'request' && rec.path.endsWith('/content')) fake.listening = false; // après cette requête
  });
  const dest = path.join(dir, 'b.png');
  await assert.rejects(file.save(dest), (e) => e.code === 'unreachable');
  off();
  assert.equal((await stat(`${dest}.part`)).size, 30);
  await assert.rejects(stat(dest));
  fake.listening = true;
  await file.save(dest);
  assert.ok((await readFile(dest)).equals(FAKE_PNG));
  assert.equal(contentRequests(fake, file).at(-1).headers.range, 'bytes=30-');
  await assert.rejects(stat(`${dest}.part`));
});

test('un .part faux (même entier) : jeté, le fichier redemandé depuis le début', async (t) => {
  const { fake, dir, file } = await finishedJob(t);
  const dest = path.join(dir, 'c.png');
  await writeFile(`${dest}.part`, Buffer.alloc(FAKE_PNG.length, 7));
  await file.save(dest);
  assert.ok((await readFile(dest)).equals(FAKE_PNG));
  const asked = contentRequests(fake, file);
  assert.equal(asked.length, 1);
  assert.equal(asked[0].headers.range, undefined);
});

test('un contenu faux : refusé, rien n’est gardé ; trop d’octets aussi', async (t) => {
  const { fake, dir, file } = await finishedJob(t);
  const wrong = Buffer.from(FAKE_PNG);
  wrong[40] ^= 0xff;
  const headers = { 'X-Aiodos-Api': '1', 'Content-Type': 'image/png' };
  fake.respondNext({ status: 200, headers, bytes: wrong }, { path: `/aiodos/v1/files/${file.id}/content` });
  const dest = path.join(dir, 'd.png');
  await assert.rejects(file.save(dest), (e) => e.name === 'ProtocolError' && e.code === 'protocol' && e.detail.file === file.id && /empreinte/.test(e.message));
  fake.respondNext({ status: 200, headers, bytes: Buffer.concat([FAKE_PNG, Buffer.from('en trop')]) }, { path: `/aiodos/v1/files/${file.id}/content` });
  await assert.rejects(file.content(), (e) => e.code === 'protocol' && /plus d'octets/.test(e.message));
  assert.deepEqual(await readdir(dir), []);
});

test('déjà là, entier et juste : rien n’est redemandé ; content() rend les octets vérifiés', async (t) => {
  const { fake, dir, file } = await finishedJob(t);
  const dest = path.join(dir, 'e.png');
  await file.save(dest);
  await file.save(dest);
  assert.equal(contentRequests(fake, file).length, 1);
  const bytes = await file.content();
  assert.ok(bytes.equals(FAKE_PNG));
  await file.refresh();
  assert.equal(file.fetched, true);
  assert.equal(typeof file.expiresAt, 'number');
  assert.match(String(file), /^\[fichier file_.+ · image\/png · 74 o\]$/);
  await assert.rejects(file.save(''), (e) => e.code === 'invalid_request' && e.param === 'path');
});

test('download : noms stables (plusieurs fichiers par unité), onFile, rien de redemandé la deuxième fois', async (t) => {
  const items = Array.from({ length: 10 }, (_, i) => ({ label: `Plan ${i + 1}` }));
  const { fake, job, dir } = await finishedJob(t, { items, filesPerUnit: 2 });
  const seen = [];
  const paths = await job.download(dir, { stem: 'séq/3', onFile: (p, f) => seen.push([path.basename(p), f.unit]) });
  assert.equal(paths.length, 20);
  assert.deepEqual(seen.slice(0, 3), [['séq_3-01.png', 0], ['séq_3-01-2.png', 0], ['séq_3-02.png', 1]]);
  assert.equal(seen.at(-1)[0], 'séq_3-10-2.png');
  const before = fake.requests.filter((r) => r.path.endsWith('/content')).length;
  assert.deepEqual(await job.download(dir, { stem: 'séq/3' }), paths);
  assert.equal(fake.requests.filter((r) => r.path.endsWith('/content')).length, before);
  // sans stem : l'identifiant du travail
  const other = await job.download(path.join(dir, 'b'));
  assert.equal(path.basename(other[0]), `${job.id}-01.png`);
  await assert.rejects(job.download(''), (e) => e.code === 'invalid_request' && e.param === 'destDir');
});

test('les noms : sûrs pour Windows, stables', () => {
  assert.equal(safeStem('../plans:3?'), '.._plans_3_');
  assert.equal(safeStem('CON'), '_CON');
  assert.equal(safeStem('fin. '), 'fin');
  assert.equal(safeStem(''), 'aiodos');
  const job = { progress: { total: 3 } };
  const names = fileNames(job, [{ id: 'a', unit: 2, type: 'video/mp4' }, { id: 'b', unit: 0, type: 'image/png' }, { id: 'c', unit: 2, type: 'x/y' }, { id: 'd', unit: null, type: 'audio/mpeg' }], 'm');
  assert.deepEqual([...names.values()], ['m-3.mp4', 'm-1.png', 'm-3-2.bin', 'm-x.mp3']);
});

test('File.delete, puis introuvable ; link() : NotSupported (pas encore servi)', async (t) => {
  const { client, file } = await finishedJob(t);
  await assert.rejects(file.link(), (e) => e.name === 'NotSupported' && e.detail.feature === 'files.link' && e.detail.announced === false);
  assert.deepEqual(await file.delete(), { id: file.id, deleted: true });
  await assert.rejects(file.refresh(), (e) => e.name === 'FileNotFound');
  await assert.rejects(client.file(file.id), (e) => e.name === 'FileNotFound');
  await assert.rejects(file.content(), (e) => e.name === 'FileNotFound');
});
