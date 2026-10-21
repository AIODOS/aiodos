// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : la preuve d'identité (clients/contract/proof.json).

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { KEY_PATTERN, computeProof, isAppKey, keyIdOf, newChallenge, proofMatches } from '../src/proof.js';

const contract = JSON.parse(readFileSync(new URL('../../contract/proof.json', import.meta.url), 'utf8'));

test('le vecteur du contrat donne la preuve attendue', () => {
  assert.ok(contract.vectors.length > 0);
  for (const v of contract.vectors) {
    assert.equal(computeProof(v.key, v.challenge), v.proof);
    assert.equal(keyIdOf(v.key), v.key_id);
    assert.equal(proofMatches(v.key, v.challenge, v.proof), true);
  }
});

test('la forme de clé est celle du contrat', () => {
  const f = contract.key_format;
  assert.equal(KEY_PATTERN.source, f.pattern);
  const v = contract.vectors[0];
  assert.equal(v.key.slice(f.id_offset, f.id_offset + f.id_length), v.key_id);
  assert.ok(isAppKey(v.key));
});

test('un défi : 43 caractères base64url sans « = », jamais deux fois le même', () => {
  const seen = new Set();
  for (let i = 0; i < 50; i++) {
    const c = newChallenge();
    assert.match(c, /^[A-Za-z0-9_-]{43}$/);
    assert.ok(!seen.has(c));
    seen.add(c);
  }
});

test('une preuve fausse, mal formée ou absente est refusée', () => {
  const v = contract.vectors[0];
  const flipped = (v.proof[0] === 'A' ? 'B' : 'A') + v.proof.slice(1);
  assert.equal(proofMatches(v.key, v.challenge, flipped), false);
  assert.equal(proofMatches(v.key, v.challenge, v.proof.slice(1)), false);
  assert.equal(proofMatches(v.key, v.challenge, `${v.proof}=`), false);
  assert.equal(proofMatches(v.key, v.challenge, null), false);
  assert.equal(proofMatches(v.key, v.challenge, 42), false);
  // une autre clé ne donne pas la même preuve
  const other = `aiodos_app_${v.key_id}_${'B'.repeat(43)}`;
  assert.equal(proofMatches(other, v.challenge, v.proof), false);
  // un autre défi non plus
  assert.equal(proofMatches(v.key, newChallenge(), v.proof), false);
});

test('les clés mal formées ne sont pas des clés d’appli', () => {
  for (const k of [
    null,
    '',
    'aiodos-abc',
    `aiodos_app_k3j9m2qa_${'A'.repeat(43)}`, // « 9 » hors de base32
    `aiodos_app_K3J7M2QA_${'A'.repeat(43)}`,
    `aiodos_app_k3j7m2qa_${'A'.repeat(42)}`,
    `aiodos_app_k3j7m2qa_${'A'.repeat(43)} `,
  ]) {
    assert.equal(isAppKey(k), false, String(k));
    assert.equal(keyIdOf(k), null);
  }
  assert.throws(() => computeProof(contract.vectors[0].key, 'court'), TypeError);
});
