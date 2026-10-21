// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — essais : le contrat partagé (la copie embarquée, le faux serveur qui
// le sert avec la même règle que l'essai de la passerelle) et les règles du paquet.

import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { VERSION, API_VERSION } from '../src/index.js';
import { FAKE_KEY, createFakeFetch } from '../src/fake.js';
import { newChallenge } from '../src/proof.js';

const ROOT = new URL('../', import.meta.url);
const SHARED = new URL('../../contract/', import.meta.url);
const COPY = new URL('../contract/', import.meta.url);
const json = (url) => JSON.parse(readFileSync(url, 'utf8'));

test('la copie embarquée du contrat est identique à clients/contract/', () => {
  const shared = readdirSync(SHARED).filter((f) => f.endsWith('.json')).sort();
  const copy = readdirSync(COPY).filter((f) => f.endsWith('.json')).sort();
  assert.deepEqual(copy, shared, 'mêmes fichiers (copier clients/contract/*.json dans clients/js/contract/)');
  for (const f of shared) {
    assert.ok(readFileSync(new URL(f, COPY)).equals(readFileSync(new URL(f, SHARED))), `${f} diffère : recopier clients/contract/${f}`);
  }
});

// --- la règle du README du contrat : chaque clé de l'exemple existe avec le même type JSON,
// récursivement (une liste : son premier élément quand les deux en ont) ; `exact` égal ;
// les en-têtes de l'exemple présents avec la même valeur.
const typeOf = (v) => (v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v);
function shapeErrors(example, actual, at = '') {
  if (typeOf(example) !== typeOf(actual)) return [`${at || '.'} : ${typeOf(example)} attendu, ${typeOf(actual)} reçu`];
  if (typeOf(example) === 'array') return example.length && actual.length ? shapeErrors(example[0], actual[0], `${at}.0`) : [];
  if (typeOf(example) !== 'object') return [];
  return Object.keys(example).flatMap((k) => (k in actual ? shapeErrors(example[k], actual[k], at ? `${at}.${k}` : k) : [`${at ? `${at}.` : ''}${k} : absent`]));
}
const pick = (obj, dotted) => dotted.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);

/** Prépare le faux pour une interaction, et rend la requête à envoyer. */
function prepare(fake, file, it) {
  const req = structuredClone(it.request);
  let deviceCode = null;
  const name = it.name;
  if (file === 'access' && name.startsWith('relève')) {
    fake.adoptRequest({ device_code: 'D'.repeat(43), user_code: '482 197', scope: 'chat embed', where: ['here', 'mine'] });
    deviceCode = 'D'.repeat(43);
    if (name === 'relève : accordé') fake.approve();
    if (name === 'relève : refusé') fake.deny();
  }
  if (name === "demande refusée : AIODOS n'accepte pas de nouvelle application") fake.access.requests = false;
  if (name === 'info avec une clé retirée' || name === 'clé retirée') fake.revokeKey();
  if (name === 'embeddings sans le droit') fake.keys.get(FAKE_KEY).scopes = ['chat'];
  const fill = (s) => s.replaceAll('{key}', FAKE_KEY).replaceAll('{key_id}', FAKE_KEY.slice(11, 19)).replaceAll('{challenge}', newChallenge()).replaceAll('{device_code}', deviceCode ?? '');
  req.path = fill(req.path);
  req.headers = Object.fromEntries(Object.entries(req.headers || {}).map(([k, v]) => [k, fill(v)]));
  if (req.json && typeof req.json.device_code === 'string') req.json.device_code = fill(req.json.device_code);
  return req;
}

test('le faux AIODOS sert chaque exemple du contrat côté serveur (même règle que la passerelle)', async () => {
  let checked = 0;
  for (const file of ['info', 'access', 'models', 'embed', 'errors', 'chat']) {
    for (const it of json(new URL(`${file}.json`, SHARED)).interactions) {
      if ((it.server ?? 'shape') !== 'shape') continue;
      const fetch = createFakeFetch();
      const req = prepare(fetch.fake, file, it);
      const headers = { ...req.headers };
      if (req.json !== null && req.json !== undefined) headers['Content-Type'] = 'application/json';
      const res = await fetch(`${fetch.fake.url}${req.path}`, { method: req.method, headers, body: req.json == null ? undefined : JSON.stringify(req.json), redirect: 'error' });
      assert.equal(res.status, it.response.status, `${file} / ${it.name} : statut`);
      for (const [h, v] of Object.entries(it.response.headers || {})) assert.equal(res.headers.get(h), v, `${file} / ${it.name} : en-tête ${h}`);
      const body = await res.text();
      const got = body ? JSON.parse(body) : null;
      assert.deepEqual(shapeErrors(it.response.json, got), [], `${file} / ${it.name}`);
      for (const p of it.exact || []) assert.deepEqual(pick(got, p), pick(it.response.json, p), `${file} / ${it.name} : ${p}`);
      checked += 1;
    }
  }
  assert.ok(checked >= 18, `${checked} exemples vérifiés`);
});

// --- les règles du paquet ---------------------------------------------------------------------

const pkg = json(new URL('package.json', ROOT));
const srcDir = new URL('src/', ROOT);
const sources = readdirSync(srcDir).filter((f) => f.endsWith('.js'));

test('package.json : nom, version, module ES, exports, Node 20.3, Apache-2.0, aucune dépendance', () => {
  assert.equal(pkg.name, 'aiodos-client');
  assert.equal(pkg.version, VERSION);
  assert.equal(VERSION, '0.1.0');
  assert.equal(API_VERSION, 1);
  assert.equal(pkg.type, 'module');
  assert.equal(pkg.exports['.'].default, './src/index.js');
  assert.equal(pkg.exports['.'].types, './index.d.ts');
  assert.equal(pkg.types, './index.d.ts');
  assert.equal(pkg.engines.node, '>=20.3');
  assert.equal(pkg.license, 'Apache-2.0');
  for (const k of ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies', 'bundleDependencies']) assert.equal(pkg[k], undefined, k);
  assert.ok(pkg.files.includes('contract/'));
});

test('LICENSE : la copie exacte de celle du dépôt ; NOTICE', () => {
  assert.ok(readFileSync(new URL('LICENSE', ROOT)).equals(readFileSync(new URL('../../LICENSE', ROOT))));
  assert.match(readFileSync(new URL('NOTICE', ROOT), 'utf8'), /^AIODOS client — Copyright 2026 22eme Arkane/);
});

test('chaque fichier porte la licence et la version ; aucune fonction interdite', () => {
  const files = [...sources.map((f) => new URL(f, srcDir)), ...readdirSync(new URL('test/', ROOT)).map((f) => new URL(`test/${f}`, ROOT)), new URL('index.d.ts', ROOT), new URL('fake.d.ts', ROOT)];
  for (const url of files) {
    const text = readFileSync(url, 'utf8');
    const [first, second] = text.split('\n');
    const name = fileURLToPath(url);
    assert.equal(first, '// SPDX-License-Identifier: Apache-2.0', name);
    assert.match(second, new RegExp(`aiodos-client ${VERSION.replaceAll('.', '\\.')}`), name);
  }
  for (const f of sources) {
    const text = readFileSync(new URL(f, srcDir), 'utf8');
    for (const banned of ['Promise.withResolvers', 'Array.fromAsync', 'console.', 'shell: true', "from 'undici'", 'eval(', 'new Function(']) {
      assert.ok(!text.includes(banned), `${f} contient ${banned}`);
    }
    // seulement la bibliothèque standard de Node, et nos propres fichiers
    for (const m of text.matchAll(/from '([^']+)'/g)) assert.ok(m[1].startsWith('node:') || m[1].startsWith('./'), `${f} importe ${m[1]}`);
  }
});
