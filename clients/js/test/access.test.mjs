// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : connect() passerelle ouverte (accord, refus, expiration,
// slow_down) et passerelle fermée (faux programme d'aide), droits en plus, disconnect().

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { inspect } from 'node:util';

import { isAppKey } from '../src/proof.js';
import { FAKE_KEY, createFakeClient, createFakeHome } from '../src/fake.js';

const discovery = JSON.parse(readFileSync(new URL('../../contract/discovery.json', import.meta.url), 'utf8'));

test('passerelle ouverte : la demande, le code, l’accord, la clé rangée', async () => {
  const { client, fake, keyStore } = createFakeClient({ key: null, fake: { interval: 0.05 } });
  let code = null;
  const result = await client.connect({
    scopes: ['chat'],
    where: ['here'],
    purpose: 'Assistant lumière',
    onCode: (c, more) => {
      code = c;
      assert.ok(more.expiresIn > 0);
      // le client attend au moins 0,1 s entre deux relèves : plusieurs passent avant l'accord
      setTimeout(() => fake.approve(), 250);
    },
  });
  assert.match(code, /^\d{3} \d{3}$/);
  assert.deepEqual(result, { upgraded: false, scopes: ['chat'], where: ['here'], appId: keyStore.get().slice(11, 19), priority: 'interactive', expires: null });
  assert.ok(isAppKey(keyStore.get()));
  // ce que la passerelle a reçu : la forme du contrat (§ 4.3)
  const [req] = fake.received('/aiodos/v1/access/requests');
  assert.deepEqual(req.json, { client_name: 'ARKLIGHT PC', client_id: 'arkane.arklight', client_version: '0.0.0-essai', scope: 'chat', where: ['here'], purpose: 'Assistant lumière' });
  assert.equal(req.headers.authorization, undefined);
  // la relève : sans clé, toutes les `interval` secondes
  const polls = fake.received('/aiodos/v1/access/token');
  assert.ok(polls.length >= 2);
  for (let i = 1; i < polls.length; i++) assert.ok(polls[i].at - polls[i - 1].at >= 90, `relève trop rapprochée (${polls[i].at - polls[i - 1].at} ms)`);
  for (const p of polls) {
    assert.equal(p.headers.authorization, undefined);
    assert.equal(p.json.grant_type, 'urn:ietf:params:oauth:grant-type:device_code');
  }
  // la nouvelle clé marche, après une preuve
  assert.equal((await client.status()).state, 'ready');
  assert.equal((await client.models()).length, 2);
});

test('refus, expiration, demande inconnue : la bonne erreur, pas de clé', async () => {
  const cases = [
    ['deny', 'access_denied', 'AccessDenied'],
    ['expire', 'expired_token', 'AccessExpired'],
  ];
  for (const [action, code, name] of cases) {
    const { client, fake, keyStore } = createFakeClient({ key: null, fake: { interval: 0.05 } });
    await assert.rejects(
      client.connect({ scopes: ['chat'], onCode: () => setTimeout(() => fake[action](), 30) }),
      (e) => e.code === code && e.name === name && e.status === 400,
    );
    assert.equal(keyStore.get(), null);
  }
  // une clé relevée deux fois : invalid_grant
  const { client, fake } = createFakeClient({ key: null, fake: { interval: 0.05 } });
  const req = await client.requestAccess({ scopes: ['chat'] });
  fake.pending.deviceCode = 'autre'; // la demande n'est plus connue d'AIODOS
  await assert.rejects(req.wait(), (e) => e.code === 'invalid_grant' && e.name === 'AccessExpired');
});

test('slow_down : 5 s de plus à l’intervalle (RFC 8628)', async () => {
  const { client, fake } = createFakeClient({ key: null, fake: { interval: 0.1 } });
  fake.slowDown(1);
  const t0 = Date.now();
  const done = client.connect({ scopes: ['chat'] });
  // la première relève reçoit slow_down ; l'accord arrive pendant l'attente allongée
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(fake.received('/aiodos/v1/access/token').length, 1);
  fake.approve();
  const result = await done;
  assert.equal(result.upgraded, false);
  const polls = fake.received('/aiodos/v1/access/token');
  assert.equal(polls.length, 2);
  assert.ok(polls[1].at - polls[0].at >= 5000, `${polls[1].at - polls[0].at} ms`);
  assert.ok(Date.now() - t0 < 9000);
});

test('la demande expire côté client (expires_in) ; l’annulation dit « Arrêté »', async () => {
  const { client } = createFakeClient({ key: null, fake: { interval: 0.05, expiresIn: 0.3 } });
  await assert.rejects(client.connect({ scopes: ['chat'] }), (e) => e.code === 'expired_token');

  const other = createFakeClient({ key: null, fake: { interval: 0.05 } }).client;
  const ctrl = new AbortController();
  setTimeout(() => ctrl.abort(), 100);
  await assert.rejects(other.connect({ scopes: ['chat'], signal: ctrl.signal }), (e) => e.code === 'cancelled' && e.message === 'Arrêté');
  assert.equal((await other.status()).state, 'needs_access'); // plus « pending »
});

test('onCode lent : la relève n’attend pas ; onCode qui échoue : la relève s’arrête', async () => {
  const { client, fake } = createFakeClient({ key: null, fake: { interval: 0.05 } });
  let shownAt = 0;
  await client.connect({
    scopes: ['chat'],
    onCode: async () => {
      setTimeout(() => fake.approve(), 150);
      await new Promise((r) => setTimeout(r, 600)); // l'interface met du temps à montrer le code
      shownAt = Date.now();
    },
  });
  const polls = fake.received('/aiodos/v1/access/token');
  assert.ok(polls.length >= 1);
  assert.ok(polls[0].at < shownAt, 'la relève a commencé pendant onCode');

  const other = createFakeClient({ key: null, fake: { interval: 0.05 } });
  const boom = new Error('interface fermée');
  await assert.rejects(other.client.connect({ scopes: ['chat'], onCode: () => { throw boom; } }), (e) => e === boom);
  const n = other.fake.received('/aiodos/v1/access/token').length;
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(other.fake.received('/aiodos/v1/access/token').length, n, 'plus aucune relève');
  assert.equal((await other.client.status()).state, 'needs_access');
});

test('la demande ne montre jamais le device_code (JSON, inspection, clés)', async () => {
  const { client, fake } = createFakeClient({ key: null, fake: { interval: 0.05 } });
  const req = await client.requestAccess({ scopes: ['chat', 'embed'], where: 'here,mine' });
  const deviceCode = fake.pending.deviceCode;
  for (const s of [JSON.stringify(req), String(req), inspect(req, { depth: 5 }), Object.keys(req).join(' '), JSON.stringify(Object.values(req))]) {
    assert.ok(!s.includes(deviceCode), s);
  }
  assert.equal(req.userCode, fake.pending.userCode);
  assert.deepEqual(fake.received('/aiodos/v1/access/requests')[0].json.where, ['here', 'mine']);
  setTimeout(() => fake.approve(), 20);
  const r = await req.wait();
  assert.deepEqual(r.scopes, ['chat', 'embed']);
  assert.deepEqual(r.where, ['here', 'mine']);
  assert.equal(req.wait(), req.wait()); // une seule relève
});

test('les champs sont vérifiés avant tout envoi', async () => {
  const bad = [
    [{ app: 'AIODOS' }, 'app'],
    [{ app: '' }, 'app'],
    [{ app: 'x'.repeat(41) }, 'app'],
    [{ appId: 'Arkane Arklight' }, 'appId'],
    [{ version: 'v'.repeat(41) }, 'version'],
  ];
  for (const [opts, param] of bad) {
    const { client, fake } = createFakeClient({ key: null, ...opts });
    await assert.rejects(client.connect({ scopes: ['chat'] }), (e) => e.code === 'invalid_request' && e.param === param);
    assert.equal(fake.requests.length, 0);
  }
  const { client, fake } = createFakeClient({ key: null });
  await assert.rejects(client.connect({ scopes: [] }), (e) => e.param === 'scopes');
  await assert.rejects(client.connect({ scopes: ['chat'], where: ['partout'] }), (e) => e.param === 'where');
  await assert.rejects(client.connect({ scopes: ['chat'], purpose: 'p'.repeat(201) }), (e) => e.param === 'purpose');
  assert.equal(fake.requests.length, 0);
  // le nom est nettoyé (contrôle et sens d'écriture), comme le fait AIODOS
  const named = createFakeClient({ key: null, app: 'ARK‮LIGHT\u0007', fake: { interval: 0.05 } });
  const req = await named.client.requestAccess({ scopes: ['chat'] });
  assert.equal(named.fake.received('/aiodos/v1/access/requests')[0].json.client_name, 'ARKLIGHT');
  assert.ok(req.userCode);
});

test('passerelle fermée : le programme d’aide (--app-request), puis la relève', async () => {
  const home = await createFakeHome({ helper: 'ok' });
  try {
    const { client, fake, keyStore } = createFakeClient({ key: null, fake: { listening: false }, env: home.env });
    let code = null;
    const result = await client.connect({
      scopes: ['chat'],
      where: ['here'],
      purpose: 'Assistant lumière',
      onCode: (c) => {
        code = c;
        // AIODOS a reçu la demande par son canal privé ; « Autoriser » ouvre la passerelle
        fake.adoptRequest({ ...home.stdoutOk, scope: 'chat', where: ['here'] });
        setTimeout(() => fake.approve(), 300);
      },
    });
    assert.equal(code, discovery.app_request.stdout_ok.user_code);
    assert.equal(result.upgraded, false);
    assert.ok(isAppKey(keyStore.get()));
    // le programme d'aide : -m aiodos --app-request, depuis la racine, la demande en une ligne
    const calls = await home.calls();
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].args, ['-m', 'aiodos', '--app-request']);
    assert.equal(calls[0].cwd.toLowerCase(), home.dir.toLowerCase());
    assert.ok(calls[0].stdin.endsWith('\n'));
    assert.equal(calls[0].stdin.trim().split('\n').length, 1);
    assert.deepEqual(JSON.parse(calls[0].stdin), {
      client_name: 'ARKLIGHT PC',
      client_id: 'arkane.arklight',
      client_version: '0.0.0-essai',
      scope: 'chat',
      where: ['here'],
      purpose: 'Assistant lumière',
    });
    // aucune demande n'est passée par la passerelle
    assert.equal(fake.received('/aiodos/v1/access/requests').length, 0);
    assert.ok(fake.received('/aiodos/v1/access/token').length >= 1);
  } finally {
    await home.remove();
  }
});

test('passerelle ouverte à la seule clé générale (apps_closed) : le programme d’aide aussi', async () => {
  const home = await createFakeHome({ helper: 'ok' });
  try {
    const { client, fake } = createFakeClient({ key: null, fake: { access: { apps: false } }, env: home.env });
    const result = await client.connect({
      scopes: ['chat'],
      onCode: () => {
        fake.adoptRequest({ ...home.stdoutOk, scope: 'chat' });
        fake.approve();
      },
    });
    assert.equal(result.upgraded, false);
    assert.equal((await home.calls()).length, 1);
  } finally {
    await home.remove();
  }
});

test('le programme d’aide échoue : son erreur (approval_unavailable), code 1', async () => {
  const home = await createFakeHome({ helper: 'error' });
  try {
    const { client } = createFakeClient({ key: null, fake: { listening: false }, env: home.env });
    const want = discovery.app_request.stdout_error.error;
    await assert.rejects(client.connect({ scopes: ['chat'] }), (e) => e.code === want.code && e.name === 'ApprovalUnavailable' && e.message === want.message);
  } finally {
    await home.remove();
  }
  // pas d'installation : NotInstalled
  const none = createFakeClient({ key: null, fake: { listening: false } }).client;
  await assert.rejects(none.connect({ scopes: ['chat'] }), (e) => e.code === 'not_installed' && e.name === 'NotInstalled');
});

test('droits en plus : la même clé, après la preuve (upgraded)', async () => {
  const { client, fake, keyStore } = createFakeClient({ fake: { interval: 0.05 } });
  const result = await client.connect({ scopes: ['chat', 'embed', 'libraries'], onCode: () => setTimeout(() => fake.approve(), 20) });
  assert.equal(result.upgraded, true);
  assert.deepEqual(result.scopes, ['chat', 'embed', 'libraries']);
  assert.equal(keyStore.get(), FAKE_KEY); // la clé ne change pas
  const [req] = fake.received('/aiodos/v1/access/requests');
  assert.equal(req.headers.authorization, `Bearer ${FAKE_KEY}`);
  // la preuve a précédé l'envoi de la clé
  const firstKeyed = fake.requests.findIndex((r) => r.headers.authorization);
  const proof = fake.requests.findIndex((r) => r.path.includes('challenge='));
  assert.ok(proof >= 0 && proof < firstKeyed);
  assert.deepEqual(fake.keys.get(FAKE_KEY).scopes, ['chat', 'embed', 'libraries']);
});

test('clé retirée : la demande repart sans elle, et une nouvelle clé est rangée', async () => {
  const { client, fake, keyStore } = createFakeClient({ fake: { interval: 0.05 } });
  fake.revokeKey();
  const result = await client.connect({ scopes: ['chat'], onCode: () => setTimeout(() => fake.approve(), 20) });
  assert.equal(result.upgraded, false);
  assert.notEqual(keyStore.get(), FAKE_KEY);
  const reqs = fake.received('/aiodos/v1/access/requests');
  assert.equal(reqs.length, 2);
  assert.ok(reqs[0].headers.authorization);
  assert.equal(reqs[1].headers.authorization, undefined);
});

test('preuve fausse : la demande de droits en plus n’envoie pas la clé', async () => {
  const { client, fake } = createFakeClient({ fake: { wrongProof: true } });
  await assert.rejects(client.connect({ scopes: ['chat'] }), (e) => e.code === 'untrusted');
  assert.ok(fake.requests.every((r) => r.headers.authorization === undefined));
  assert.equal(fake.received('/aiodos/v1/access/requests').length, 0);
});

test('disconnect : retrait dans AIODOS, puis key.set(null)', async () => {
  const { client, fake, keyStore } = createFakeClient();
  assert.deepEqual(await client.disconnect(), { revoked: true });
  assert.equal(keyStore.get(), null);
  assert.equal(fake.keys.get(FAKE_KEY).state, 'revoked');
  const [rev] = fake.received('/aiodos/v1/access/revoke');
  assert.equal(rev.headers.authorization, `Bearer ${FAKE_KEY}`);
  // AIODOS injoignable : la clé est effacée quand même
  const off = createFakeClient({ fake: { listening: false } });
  assert.deepEqual(await off.client.disconnect(), { revoked: false });
  assert.equal(off.keyStore.get(), null);
  // sans clé : rien à retirer
  const none = createFakeClient({ key: null });
  assert.deepEqual(await none.client.disconnect(), { revoked: false });
  assert.equal(none.fake.requests.length, 0);
});
