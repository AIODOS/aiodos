// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : les entrées des travaux (HEAD, puis PUT s'il manque), leur
// genre, la borne des 100 Mo vérifiée avant l'envoi, et les refus d'un HEAD sans corps
// expliqués (droit manquant, clé retirée, AIODOS fermé aux applis).

import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { pathToFileURL } from 'node:url';

import { InputTooLarge } from '../src/index.js';
import { FAKE_FEATURES, FAKE_KEY, FAKE_PNG, createFakeClient } from '../src/fake.js';
import { MAX_INPUT_BYTES } from '../src/inputs.js';
import { sniffType } from '../src/media.js';

const L2 = { features: FAKE_FEATURES, scopes: ['chat', 'engines:comfyui'] };
const PNG_SHA = createHash('sha256').update(FAKE_PNG).digest('hex');
const notInfo = (r) => !r.path.startsWith('/aiodos/v1/info');

test('put : des octets → leur sha256 ; HEAD, puis PUT avec le genre reconnu ; la deuxième fois, HEAD seul', async () => {
  const { client, fake } = createFakeClient({ fake: L2 });
  assert.equal(await client.inputs.put(FAKE_PNG), PNG_SHA);
  const sent = fake.requests.filter(notInfo);
  assert.deepEqual(sent.map((r) => r.method), ['HEAD', 'PUT']);
  assert.equal(sent[1].headers['content-type'], 'image/png');
  assert.equal(sent[1].headers.authorization, `Bearer ${FAKE_KEY}`);
  assert.equal(sent[1].size, FAKE_PNG.length);
  assert.equal(fake.inputs.get('k3j7m2qa').get(PNG_SHA).type, 'image/png');
  // Uint8Array et ArrayBuffer aussi
  const bytes = new Uint8Array(FAKE_PNG);
  assert.equal(await client.inputs.put(bytes), PNG_SHA);
  assert.equal(await client.inputs.put(bytes.buffer), PNG_SHA);
  assert.deepEqual(fake.requests.filter(notInfo).map((r) => r.method), ['HEAD', 'PUT', 'HEAD', 'HEAD']);
});

test('put : un chemin ou une URL file:, le genre donné', async (t) => {
  const dir = await mkdtemp(path.join(tmpdir(), 'aiodos-entree-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'reference.png');
  await writeFile(file, FAKE_PNG);
  const { client, fake } = createFakeClient({ fake: L2 });
  assert.equal(await client.inputs.put(file, { type: 'image/png' }), PNG_SHA);
  assert.equal(await client.inputs.put(pathToFileURL(file)), PNG_SHA);
  await assert.rejects(client.inputs.put(path.join(dir, 'absente.png')), (e) => e.code === 'invalid_request' && e.param === 'inputs');
  await assert.rejects(client.inputs.put(FAKE_PNG, { type: 'png' }), (e) => e.code === 'invalid_request');
  await assert.rejects(client.inputs.put({ sha256: PNG_SHA }), (e) => e.code === 'invalid_request');
  assert.equal(fake.requests.filter((r) => r.method === 'PUT').length, 1);
});

test('plus de 100 Mo : InputTooLarge, rien n’est envoyé', async () => {
  const { client, fake } = createFakeClient({ fake: L2 });
  const big = Buffer.alloc(MAX_INPUT_BYTES + 1);
  await assert.rejects(client.inputs.put(big), (e) => {
    assert.ok(e instanceof InputTooLarge);
    assert.equal(e.code, 'input_too_large');
    assert.equal(e.status, null);
    assert.match(e.message, /^Entrée trop lourde \(101 Mo\) : 100 Mo au plus\. Rien n'a été envoyé\.$/);
    assert.deepEqual(e.detail, { bytes: MAX_INPUT_BYTES + 1, max_bytes: MAX_INPUT_BYTES });
    assert.ok(e.hint);
    return true;
  });
  // au dépôt : toutes les entrées vérifiées avant la première envoyée
  await assert.rejects(client.comfy.submit({ graph: { 9: { class_type: 'SaveImage', inputs: {} } }, inputs: { a: FAKE_PNG, b: big } }), (e) => e.name === 'InputTooLarge');
  assert.deepEqual(fake.requests.filter(notInfo), []);
  // en anglais
  const en = createFakeClient({ fake: L2, lang: 'en' });
  await assert.rejects(en.client.inputs.put(big), (e) => e.message === 'Input too large (101 MB): 100 MB at most. Nothing was sent.');
});

test('un genre que la passerelle ne connaît pas : 415 UnsupportedMediaType', async () => {
  const { client } = createFakeClient({ fake: L2 });
  await assert.rejects(client.inputs.put(Buffer.from('juste du texte')), (e) => e.name === 'UnsupportedMediaType' && e.status === 415);
});

test('un HEAD refusé (sans corps) est expliqué : droit manquant, clé retirée, AIODOS fermé aux applis', async () => {
  const noScope = createFakeClient({ fake: { features: FAKE_FEATURES } }); // chat et embed seulement
  await assert.rejects(noScope.client.inputs.put(FAKE_PNG), (e) => {
    assert.equal(e.name, 'ScopeDenied');
    assert.equal(e.scope, 'engines:comfyui');
    assert.equal(e.status, 403);
    return true;
  });
  assert.equal(noScope.fake.requests.filter((r) => r.method === 'PUT').length, 0);

  const revoked = createFakeClient({ fake: L2 });
  await revoked.client.inputs.put(FAKE_PNG); // la preuve est faite
  revoked.fake.revokeKey();
  await assert.rejects(revoked.client.inputs.put(Buffer.concat([FAKE_PNG, Buffer.from([0])])), (e) => e.name === 'Revoked' && e.code === 'key_revoked');

  const closed = createFakeClient({ fake: L2 });
  closed.fake.access.apps = false;
  await assert.rejects(closed.client.inputs.put(FAKE_PNG), (e) => e.name === 'AppsClosed' && e.status === 403);
});

test('« inputs » pas annoncé : NotSupported ; une entrée déjà envoyée ({ sha256 }) n’en a pas besoin', async () => {
  const features = FAKE_FEATURES.filter((f) => f !== 'inputs');
  const { client, fake } = createFakeClient({ fake: { ...L2, features } });
  await assert.rejects(client.inputs.put(FAKE_PNG), (e) => e.name === 'NotSupported' && e.detail.feature === 'inputs');
  const graph = { 10: { class_type: 'LoadImage', inputs: { image: '@in:ref0' } }, 9: { class_type: 'SaveImage', inputs: { images: ['10', 0] } } };
  await assert.rejects(client.comfy.submit({ graph, inputs: { ref0: FAKE_PNG } }), (e) => e.name === 'NotSupported' && e.detail.feature === 'inputs');
  assert.deepEqual(fake.requests.filter(notInfo), []);
  // déjà envoyée (par une version qui servait « inputs ») : le dépôt part, AIODOS dit si elle manque
  await assert.rejects(client.comfy.submit({ graph, inputs: { ref0: { sha256: PNG_SHA } } }), (e) => e.name === 'InputMissing');
});

test('le genre d’après les premiers octets', () => {
  const head = (s) => Buffer.from(s, 'latin1');
  assert.equal(sniffType(FAKE_PNG), 'image/png');
  assert.equal(sniffType(Buffer.from([0xff, 0xd8, 0xff, 0xe0])), 'image/jpeg');
  assert.equal(sniffType(head('GIF89a......')), 'image/gif');
  assert.equal(sniffType(head('RIFF\0\0\0\0WEBPVP8 ')), 'image/webp');
  assert.equal(sniffType(head('RIFF\0\0\0\0WAVEfmt ')), 'audio/wav');
  assert.equal(sniffType(Buffer.from([0x1a, 0x45, 0xdf, 0xa3, 0x01])), 'video/webm');
  assert.equal(sniffType(head('\0\0\0\x18ftypmp42')), 'video/mp4');
  assert.equal(sniffType(head('fLaC\0\0\0')), 'audio/flac');
  assert.equal(sniffType(head('OggS\0\x02')), 'audio/ogg');
  assert.equal(sniffType(head('ID3\x04\0')), 'audio/mpeg');
  assert.equal(sniffType(Buffer.from([0xff, 0xfb, 0x90])), 'audio/mpeg');
  assert.equal(sniffType(head('bonjour')), null);
  assert.equal(sniffType(Buffer.alloc(0)), null);
});
