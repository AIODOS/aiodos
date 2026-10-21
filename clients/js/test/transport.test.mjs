// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — essais : le transport (hôte local vérifié avant tout envoi, aucune
// redirection suivie, aucun proxy, fetch relu à chaque appel, annulation, délais).

import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { createClient, memoryKeyStore } from '../src/index.js';
import { FAKE_KEY, createFakeClient, createFakeFetch, startFakeServer, writeFakeDiscovery } from '../src/fake.js';
import { isLocalUrl, localOrigin } from '../src/transport.js';

const NO_INSTALL = { AIODOS_DISCOVERY_FILE: '-', AIODOS_HOME: path.join(tmpdir(), 'aiodos-client-aucune-installation') };

function spyFetch() {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url: String(url), init });
    throw new TypeError('fetch failed', { cause: Object.assign(new Error('refusé'), { code: 'ECONNREFUSED' }) });
  };
  fn.calls = calls;
  return fn;
}

test('les adresses locales, et elles seules', () => {
  for (const ok of ['http://127.0.0.1:11435', 'http://localhost:11435/', 'http://LOCALHOST:8080', 'http://[::1]:11435', 'http://127.0.0.1:11435/v1', 'https://127.0.0.1:1']) {
    assert.equal(isLocalUrl(ok), true, ok);
  }
  assert.equal(localOrigin('http://127.0.0.1:11435/v1/chat'), 'http://127.0.0.1:11435');
  for (const bad of [
    'http://exemple.fr:11435',
    'http://localhost.exemple.fr',
    'http://127.0.0.1.nip.io',
    'http://[::ffff:127.0.0.1]:11435',
    'http://192.168.1.10:11435',
    'http://0.0.0.0:11435',
    'http://moi:secret@127.0.0.1:11435',
    'ftp://127.0.0.1',
    'file:///C:/aiodos',
    '127.0.0.1:11435',
    '',
    null,
  ]) {
    assert.equal(isLocalUrl(bad), false, String(bad));
  }
  assert.throws(() => localOrigin('http://moi:secret@exemple.fr'), (e) => e.code === 'non_local_url' && !e.message.includes('secret'));
});

test('une adresse non locale est refusée avant tout envoi (option, AIODOS_URL)', async () => {
  for (const setup of [{ url: 'http://exemple.fr:11435' }, { env: { ...NO_INSTALL, AIODOS_URL: 'http://192.168.1.10:11435' } }]) {
    const fetch = spyFetch();
    const client = createClient({ app: 'Essai', appId: 'arkane.essai', key: memoryKeyStore(FAKE_KEY), fetch, env: NO_INSTALL, ...setup });
    await assert.rejects(client.models(), (e) => e.code === 'non_local_url' && e.name === 'NonLocalUrl');
    await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Bonjour' }] }), (e) => e.code === 'non_local_url');
    await assert.rejects(client.connect({ scopes: ['chat'] }), (e) => e.code === 'non_local_url');
    const st = await client.status();
    assert.equal(st.state, 'not_aiodos');
    assert.equal(st.code, 'non_local_url');
    assert.equal(fetch.calls.length, 0, 'rien ne doit partir');
  }
});

test("l'adresse du fichier de découverte n'est retenue que si elle est locale", async () => {
  const disc = await writeFakeDiscovery({ state: 'open', url: 'http://exemple.fr:11435' });
  try {
    // url: undefined : l'adresse vient de l'environnement, du fichier ou du défaut
    const { client, fake } = createFakeClient({ url: undefined, env: disc.env });
    const st = await client.status();
    assert.equal(st.url, 'http://127.0.0.1:11435'); // le défaut, pas exemple.fr
    assert.equal(st.state, 'ready');
    assert.deepEqual(fake.foreign, []);
  } finally {
    await disc.remove();
  }
  const local = await writeFakeDiscovery({ state: 'open', url: 'http://127.0.0.1:11999' });
  try {
    const fetch = createFakeFetch({ url: 'http://127.0.0.1:11999' });
    const client = createClient({ app: 'Essai', appId: 'arkane.essai', key: memoryKeyStore(FAKE_KEY), fetch, env: { ...NO_INSTALL, ...local.env } });
    assert.equal((await client.status()).url, 'http://127.0.0.1:11999');
  } finally {
    await local.remove();
  }
});

test('les en-têtes : X-Aiodos-Client, Accept-Language, jamais Origin', async () => {
  const { client, fake } = createFakeClient({ lang: 'en' });
  await client.models();
  for (const r of fake.requests) {
    assert.equal(r.headers['x-aiodos-client'], 'js/0.1.0');
    assert.equal(r.headers['accept-language'], 'en');
    assert.equal(r.headers.origin, undefined);
  }
  // la clé n'est jamais dans l'adresse
  assert.ok(fake.requests.every((r) => !r.path.includes('AAAAAAAA')));
});

test('fetch est relu à chaque appel (globalThis.fetch remplacé par les essais)', async () => {
  const saved = globalThis.fetch;
  try {
    const a = createFakeFetch();
    const b = createFakeFetch();
    const client = createClient({ app: 'Essai', appId: 'arkane.essai', key: memoryKeyStore(FAKE_KEY), env: NO_INSTALL });
    globalThis.fetch = a;
    await client.info();
    globalThis.fetch = b;
    await client.info();
    assert.equal(a.fake.requests.length, 1);
    assert.equal(b.fake.requests.length, 1);
  } finally {
    globalThis.fetch = saved;
  }
});

test('une redirection est refusée, et sa cible jamais jointe (vrai serveur, vrai fetch)', async () => {
  let reached = 0;
  const target = createServer((req, res) => {
    reached += 1;
    res.end('{}');
  });
  await new Promise((r) => target.listen(0, '127.0.0.1', r));
  const srv = await startFakeServer();
  try {
    srv.fake.redirectNext(`http://127.0.0.1:${target.address().port}/aiodos/v1/models`, { path: '/aiodos/v1/models' });
    const client = createClient({ app: 'Essai', appId: 'arkane.essai', key: memoryKeyStore(FAKE_KEY), url: srv.url, env: NO_INSTALL });
    await assert.rejects(client.models(), (e) => e.code === 'protocol' && /redirige/.test(e.message));
    // sur info, la redirection dit « pas AIODOS »
    srv.fake.redirectNext(`http://127.0.0.1:${target.address().port}/`, { path: '/aiodos/v1/info' });
    const st = await client.status();
    assert.equal(st.state, 'not_aiodos');
    assert.equal(reached, 0);
  } finally {
    await srv.close();
    await new Promise((r) => target.close(r));
  }
});

test('une redirection est refusée aussi par un fetch qui la rendrait sans lever', async () => {
  const fake = createFakeFetch();
  const loose = (url, init) => fake(url, { ...init, redirect: 'manual' });
  fake.fake.redirectNext('http://127.0.0.1:9/', { path: '/aiodos/v1/models' });
  const client = createClient({ app: 'Essai', appId: 'arkane.essai', key: memoryKeyStore(FAKE_KEY), url: fake.fake.url, fetch: loose, env: NO_INSTALL });
  await assert.rejects(client.models(), (e) => e.code === 'protocol' && e.status === 302);
});

test('connexion refusée : unreachable, le message contient « Connexion refusée »', async () => {
  const { client } = createFakeClient({ fake: { listening: false } });
  await assert.rejects(client.models(), (e) => e.code === 'unreachable' && e.name === 'Unreachable' && e.message.includes('Connexion refusée') && e.retryable === true);
  const en = createFakeClient({ fake: { listening: false }, lang: 'en' }).client;
  await assert.rejects(en.models(), (e) => e.code === 'unreachable' && e.message.startsWith('Connection refused'));
});

test('annulé avant de partir : « Arrêté », rien n’est envoyé', async () => {
  const { client, fake } = createFakeClient();
  const ctrl = new AbortController();
  ctrl.abort(new Error('Arrêté par l’appli'));
  await assert.rejects(client.models({ signal: ctrl.signal }), (e) => e.code === 'cancelled' && e.message === 'Arrêté');
  assert.equal(fake.requests.length, 0);
});

test('délai dépassé : timeout (le corps qui ne vient pas)', async () => {
  const { client, fake } = createFakeClient();
  fake.respondNext({ hang: true }, { path: '/aiodos/v1/models' });
  const t0 = Date.now();
  await assert.rejects(client.models({ timeout: 150 }), (e) => e.code === 'timeout' && e.name === 'Timeout');
  assert.ok(Date.now() - t0 < 2000);
});

// --- le proxy d'environnement de Node -------------------------------------------------------

async function runChild(env, serverUrl) {
  const dir = await mkdtemp(path.join(tmpdir(), 'aiodos-proxy-'));
  const script = path.join(dir, 'enfant.mjs');
  const index = new URL('../src/index.js', import.meta.url).href;
  await writeFile(
    script,
    `import { createClient, memoryKeyStore } from ${JSON.stringify(index)};
const client = createClient({ app: 'Essai', appId: 'arkane.essai', url: ${JSON.stringify(serverUrl)}, key: memoryKeyStore(${JSON.stringify(FAKE_KEY)}), env: { AIODOS_DISCOVERY_FILE: '-' } });
const out = {};
try { const m = await client.models(); out.models = m.length; } catch (e) { out.code = e.code; out.proxy = Boolean(e.detail && e.detail.proxy); out.message = e.message; }
const st = await client.status();
out.state = st.state; out.stateCode = st.code;
process.stdout.write(JSON.stringify(out));
`,
  );
  try {
    return await new Promise((resolve, reject) => {
      execFile(process.execPath, [script], { env: { ...process.env, ...env }, windowsHide: true, timeout: 30_000 }, (err, stdout, stderr) =>
        err ? reject(new Error(`${err.message}\n${stderr}`)) : resolve(JSON.parse(stdout)),
      );
    });
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

test('NODE_USE_ENV_PROXY avec HTTP_PROXY : refusé avant tout envoi, le proxy ne voit rien', async () => {
  let proxied = 0;
  const proxy = createServer((req, res) => {
    proxied += 1;
    res.end('proxy');
  });
  proxy.on('connect', (req, sock) => {
    proxied += 1;
    sock.destroy();
  });
  await new Promise((r) => proxy.listen(0, '127.0.0.1', r));
  const srv = await startFakeServer();
  const proxyEnv = { NODE_USE_ENV_PROXY: '1', HTTP_PROXY: `http://127.0.0.1:${proxy.address().port}`, NO_PROXY: '', no_proxy: '', http_proxy: '' };
  try {
    const out = await runChild(proxyEnv, srv.url);
    assert.equal(out.code, 'non_local_url');
    assert.equal(out.proxy, true);
    assert.match(out.message, /NO_PROXY/);
    assert.equal(out.state, 'not_aiodos');
    assert.equal(out.stateCode, 'non_local_url');
    assert.equal(proxied, 0);
    assert.equal(srv.fake.requests.length, 0);
    // avec NO_PROXY=127.0.0.1, la requête part en direct
    const direct = await runChild({ ...proxyEnv, NO_PROXY: '127.0.0.1' }, srv.url);
    assert.equal(direct.models, 2);
    assert.equal(direct.state, 'ready');
    assert.equal(proxied, 0);
  } finally {
    await srv.close();
    await new Promise((r) => proxy.close(r));
  }
});
