// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : les erreurs du contrat (code, sous-classe, hint, retryable,
// detail, retryAfter), et la clé absente des messages, des JSON et du journal.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { inspect } from 'node:util';

import { AiodosError, ContextTooLong, NeedsAccess, ScopeDenied, errorClassFor } from '../src/index.js';
import { FAKE_KEY, createFakeClient } from '../src/fake.js';
import { errorFromResponse, parseRetryAfter, redact } from '../src/errors.js';

const SECRET = FAKE_KEY.slice(20);
const read = (name) => JSON.parse(readFileSync(new URL(`../../contract/${name}.json`, import.meta.url), 'utf8'));

function expected(response) {
  const j = response.json;
  if (j.error && typeof j.error === 'object') {
    return { code: j.error.code, message: j.error.message, hint: j.error.aiodos.hint, retryable: j.error.aiodos.retryable, detail: j.error.aiodos.detail, param: j.error.param, type: j.error.type };
  }
  return { code: j.code, message: j.error, hint: j.hint ?? null };
}

test('chaque erreur de errors.json : la bonne sous-classe, et tous ses champs', async () => {
  const { interactions } = read('errors');
  assert.ok(interactions.length >= 8);
  for (const it of interactions) {
    const { client, fake } = createFakeClient();
    fake.respondNext(it.response, { path: '/aiodos/v1/models' });
    const want = expected(it.response);
    await assert.rejects(
      client.models(),
      (e) => {
        assert.ok(e instanceof AiodosError, it.name);
        assert.ok(e instanceof errorClassFor(want.code), `${it.name} : ${e.name}`);
        assert.equal(e.code, want.code, it.name);
        assert.equal(e.status, it.response.status, it.name);
        assert.equal(e.message, want.message, it.name);
        assert.equal(e.hint, want.hint, it.name);
        if (want.retryable !== undefined) assert.equal(e.retryable, want.retryable, it.name);
        if (want.detail !== undefined) assert.deepEqual(e.detail, want.detail, it.name);
        if (want.param !== undefined) assert.equal(e.param, want.param, it.name);
        if (want.type !== undefined) assert.equal(e.type, want.type, it.name);
        const ra = it.response.headers['Retry-After'];
        assert.equal(e.retryAfter, ra === undefined ? null : Number(ra), it.name);
        return true;
      },
      it.name,
    );
  }
});

test('les sous-classes portent leurs champs : ContextTooLong, ScopeDenied, NeedsAccess', async () => {
  const { interactions } = read('errors');
  const ctxErr = interactions.find((i) => i.name === 'texte trop long pour le contexte').response;
  const e1 = errorFromResponse({ status: ctxErr.status, json: ctxErr.json });
  assert.ok(e1 instanceof ContextTooLong);
  assert.equal(e1.needed, 20512);
  assert.equal(e1.limit, 16384);

  const { client, fake } = createFakeClient();
  fake.keys.get(FAKE_KEY).scopes = ['chat'];
  await assert.rejects(client.embed({ input: 'un chat noir', model: 'embeddinggemma' }), (e) => {
    assert.ok(e instanceof ScopeDenied);
    assert.equal(e.scope, 'embed');
    assert.deepEqual(e.detail, { scope: 'embed' });
    assert.equal(e.hint, 'Demande ce droit en plus (connect avec ce droit).');
    return true;
  });

  const absent = interactions.find((i) => i.name === 'clé absente').response;
  assert.ok(errorFromResponse({ status: 401, json: absent.json }) instanceof NeedsAccess);
});

test('les erreurs de la demande d’accès (requests_closed, origin_refused)', async () => {
  const { interactions } = read('access');
  for (const name of ["demande refusée : AIODOS n'accepte pas de nouvelle application", "demande refusée : en-têtes d'une page web"]) {
    const it = interactions.find((i) => i.name === name);
    const { client, fake } = createFakeClient({ key: null });
    fake.respondNext(it.response, { path: '/aiodos/v1/access/requests' });
    await assert.rejects(client.requestAccess({ scopes: ['chat'] }), (e) => {
      assert.equal(e.code, it.response.json.error.code);
      assert.equal(e.status, 403);
      assert.equal(e.hint, it.response.json.error.aiodos.hint);
      return true;
    });
  }
});

test('la forme d’Ollama et celle d’OAuth sont lues aussi', () => {
  const ollama = errorFromResponse({ status: 403, json: { error: 'Fermé.', code: 'apps_closed', hint: 'Réglages.' } });
  assert.equal(ollama.code, 'apps_closed');
  assert.equal(ollama.message, 'Fermé.');
  assert.equal(ollama.hint, 'Réglages.');
  const oauth = errorFromResponse({ status: 400, json: { error: 'access_denied', error_description: 'Refusé.' } });
  assert.equal(oauth.code, 'access_denied');
  assert.equal(oauth.name, 'AccessDenied');
  assert.equal(oauth.message, 'Refusé.');
  // sans code : d'après le statut
  assert.equal(errorFromResponse({ status: 503, json: null }).code, 'engine_unavailable');
  assert.equal(errorFromResponse({ status: 418, json: null }).code, 'protocol');
  assert.equal(errorFromResponse({ status: 418, json: null, lang: 'en' }).message, 'Unexpected response from AIODOS (HTTP 418).');
});

test('Retry-After : secondes ou date HTTP', () => {
  assert.equal(parseRetryAfter('2'), 2);
  assert.equal(parseRetryAfter(null), null);
  assert.equal(parseRetryAfter('demain'), null);
  const inTen = new Date(Date.now() + 10_000).toUTCString();
  const s = parseRetryAfter(inTen);
  assert.ok(s >= 8 && s <= 11, String(s));
});

test('la clé n’apparaît jamais : messages, toString, JSON, inspection, journal', async () => {
  const logs = [];
  const { client, fake } = createFakeClient({ log: (level, message, data) => logs.push(`${level} ${message} ${JSON.stringify(data ?? null)}`) });
  // un serveur qui renverrait la clé dans son message
  fake.failNext('invalid_request', { status: 400, message: `Clé refusée : ${FAKE_KEY} (Bearer ${FAKE_KEY})`, hint: `Recopie ${FAKE_KEY}`, detail: { key: FAKE_KEY }, path: '/aiodos/v1/models' });
  let caught;
  try {
    await client.models();
  } catch (e) {
    caught = e;
  }
  assert.ok(caught instanceof AiodosError);
  const shown = [caught.message, String(caught), JSON.stringify(caught), inspect(caught, { depth: 5 }), caught.hint, JSON.stringify(caught.detail), caught.stack ?? ''];
  for (const s of shown) assert.ok(!s.includes(SECRET), s);
  assert.match(caught.message, /aiodos_app_k3j7m2qa_…/); // l'identifiant public reste lisible

  // tout un parcours : état, catalogue, conversation, erreurs, retrait
  await client.status();
  await client.models();
  await client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Bonjour' }] });
  fake.failNext('busy', { path: '/v1/chat/completions' });
  await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Bonjour' }] }));
  fake.wrongProof = true;
  await new Promise((r) => setTimeout(r, 5));
  await client.status();
  fake.wrongProof = false;
  await client.disconnect();
  assert.ok(logs.length > 0);
  for (const line of logs) assert.ok(!line.includes(SECRET), line);
  for (const s of [String(client), JSON.stringify(client), inspect(client, { depth: 5 })]) assert.ok(!s.includes(SECRET), s);
});

test('les codes des travaux ComfyUI : une classe chacun, des messages de secours en français et en anglais', () => {
  const classes = {
    graph_not_api: 'GraphInvalid',
    graph_invalid: 'GraphInvalid',
    patch_invalid: 'GraphInvalid',
    graph_too_large: 'GraphTooLarge',
    forbidden_value: 'ForbiddenValue',
    forbidden_node: 'ForbiddenNode',
    input_missing: 'InputMissing',
    input_hash_mismatch: 'InputHashMismatch',
    input_too_large: 'InputTooLarge',
    engine_not_found: 'EngineNotFound',
    input_not_found: 'InputNotFound',
    job_not_found: 'JobNotFound',
    file_not_found: 'FileNotFound',
    engine_unavailable: 'EngineUnavailable',
    model_unavailable: 'ModelUnavailable',
    model_unresolved: 'ModelUnresolved',
    model_ambiguous: 'ModelAmbiguous',
    job_not_cancellable: 'JobNotCancellable',
    job_not_pausable: 'JobNotPausable',
    quota_exceeded: 'QuotaExceeded',
    job_failed: 'JobFailed',
  };
  for (const [code, name] of Object.entries(classes)) {
    assert.equal(errorClassFor(code).name, name, code);
    // une réponse sans message : celui du client, dans la langue demandée
    if (code !== 'job_failed') {
      const fr = errorFromResponse({ status: 409, json: { error: { code } } });
      const en = errorFromResponse({ status: 409, json: { error: { code } }, lang: 'en' });
      assert.equal(fr.name, name, code);
      assert.ok(fr.message && fr.message !== code && !/HTTP/.test(fr.message), `${code} : ${fr.message}`);
      assert.ok(en.message && en.message !== fr.message, `${code} : ${en.message}`);
    }
  }
  // les champs portés par les sous-classes
  const detail = (code, d) => errorFromResponse({ status: 400, json: { error: { code, message: 'x', aiodos: { detail: d } } } });
  assert.deepEqual(detail('model_unavailable', { models: [{ folder: null, name: 'a.safetensors' }] }).models, [{ folder: null, name: 'a.safetensors' }]);
  assert.deepEqual(detail('model_unavailable', {}).models, []);
  assert.deepEqual(detail('model_ambiguous', { candidates: ['checkpoints', 'loras'] }).candidates, ['checkpoints', 'loras']);
  assert.equal(detail('model_unresolved', { model: 'ckpt' }).model, 'ckpt');
  const fv = detail('forbidden_value', { node: '10', input: 'image', reason: 'file_input' });
  assert.deepEqual([fv.node, fv.input, fv.reason], ['10', 'image', 'file_input']);
  assert.equal(detail('forbidden_node', { class: 'OpenAIDalle3' }).nodeClass, 'OpenAIDalle3');
  assert.equal(detail('input_missing', { input: 'ref0' }).input, 'ref0');
  assert.deepEqual([detail('graph_invalid', { node: '3', graph: 1 }).node, detail('graph_invalid', { node: '3', graph: 1 }).graph], ['3', 1]);
  assert.equal(detail('quota_exceeded', { quota: 'active_jobs' }).quota, 'active_jobs');
  // réessayer a un sens pour ces deux-là (comme la passerelle le dit)
  assert.equal(errorFromResponse({ status: 409, json: { error: { code: 'model_unavailable' } } }).retryable, true);
  assert.equal(errorFromResponse({ status: 429, json: { error: { code: 'quota_exceeded' } } }).retryable, true);
});

test('redact masque les clés d’appli, la clé générale et les en-têtes Bearer', () => {
  const general = `aiodos-${'x'.repeat(43)}`;
  const s = redact(`a ${FAKE_KEY} b ${general} c Authorization: Bearer abcdefghijklmnop`);
  assert.ok(!s.includes(SECRET));
  assert.ok(!s.includes('x'.repeat(43)));
  assert.ok(!s.includes('abcdefghijklmnop'));
  assert.ok(s.includes('aiodos_app_k3j7m2qa_…'));
});
