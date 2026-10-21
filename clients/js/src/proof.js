// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — la preuve d'identité d'AIODOS (docs/api-applications.md § 5.1).
//
// Avant d'envoyer sa clé, le client tire un défi ; AIODOS répond
//   proof = base64url_sans_égal(HMAC-SHA256(clé = SHA-256(clé d'accès en UTF-8), 32 octets bruts ;
//                                           message = "aiodos-proof/1\n" + défi, en ASCII))
// Seul le vrai AIODOS, qui garde sha256(clé), sait la calculer. Vecteur d'essai :
// clients/contract/proof.json.

import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

/**
 * Forme d'une clé d'appli : aiodos_app_<id>_<secret>, l'identifiant en base32 ([a-z2-7], 8
 * caractères, public), le secret en base64url (43 caractères, 256 bits).
 */
export const KEY_PATTERN = /^aiodos_app_[a-z2-7]{8}_[A-Za-z0-9_-]{43}$/;
const B64URL_43 = /^[A-Za-z0-9_-]{43}$/;
const LABEL = 'aiodos-proof/1\n';

/** Vrai pour une clé d'appli bien formée. */
export function isAppKey(key) {
  return typeof key === 'string' && KEY_PATTERN.test(key);
}

/** L'identifiant public d'une clé d'appli (8 caractères), ou null. */
export function keyIdOf(key) {
  return isAppKey(key) ? key.slice(11, 19) : null;
}

/** Un défi neuf : 32 octets aléatoires en base64url sans « = » (43 caractères). */
export function newChallenge() {
  return randomBytes(32).toString('base64url');
}

/** La preuve attendue pour cette clé et ce défi. */
export function computeProof(key, challenge) {
  if (typeof key !== 'string' || typeof challenge !== 'string' || !B64URL_43.test(challenge)) {
    throw new TypeError('computeProof : clé ou défi invalide');
  }
  const secret = createHash('sha256').update(key, 'utf8').digest();
  return createHmac('sha256', secret).update(Buffer.from(LABEL + challenge, 'ascii')).digest('base64url');
}

/** Compare la preuve reçue à celle attendue, en temps constant. */
export function proofMatches(key, challenge, proof) {
  if (typeof proof !== 'string' || !B64URL_43.test(proof)) return false;
  const expected = Buffer.from(computeProof(key, challenge), 'base64url');
  const given = Buffer.from(proof, 'base64url');
  if (given.length !== expected.length) return false;
  return timingSafeEqual(given, expected);
}

/** Empreinte d'une clé (pour savoir si elle a changé sans la garder). */
export function keyFingerprint(key) {
  return createHash('sha256').update(`aiodos-client/fp\n${key}`, 'utf8').digest('base64url');
}
