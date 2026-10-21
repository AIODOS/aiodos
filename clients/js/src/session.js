// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — la session d'un client : GET /aiodos/v1/info, la preuve d'identité,
// et l'envoi de la clé (docs/api-applications.md § 5.1).
//
// La clé n'est envoyée qu'après une preuve juste. La preuve est refaite quand `instance`
// change (fichier de découverte, ou info), après une erreur de connexion, et si elle a plus
// de 60 s. La clé n'est jamais gardée : seule son empreinte l'est, pour voir qu'elle change.

import { readDiscovery, discoveryUrl } from './discovery.js';
import { clientError } from './errors.js';
import { isAppKey, keyFingerprint, keyIdOf, newChallenge, proofMatches } from './proof.js';
import { API_VERSION } from './version.js';

const PROOF_TTL = 60_000;
export const INFO_TIMEOUT = 10_000;

function query(params) {
  if (!params) return '';
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v != null) q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : '';
}

/**
 * @param {object} ctx  le contexte du client (transport, lang, log, keyStore, env, platform)
 */
export function createSession(ctx) {
  let proof = null; // { origin, fp, instance, at }
  let info = null; // { origin, data, at } : la dernière info sans clé

  function forget() {
    proof = null;
    info = null;
  }

  /**
   * GET /aiodos/v1/info, avec ou sans clé, avec ou sans défi. Reconnaît AIODOS : un ancien
   * AIODOS répond 401 sans X-Aiodos-Api (too_old), un autre programme 404 ou autre chose
   * (not_aiodos).
   */
  async function fetchInfo(origin, { params, key, signal, timeout = INFO_TIMEOUT } = {}) {
    let r;
    try {
      r = await ctx.transport.send({ origin, path: `/aiodos/v1/info${query(params)}`, key, signal, timeout });
    } catch (err) {
      if (err.code !== 'cancelled') forget(); // après une erreur de connexion, la preuve sera refaite
      throw err;
    }
    const lang = ctx.lang();
    const host = new URL(origin).host;
    const fromAiodos = r.headers.get('x-aiodos-api') != null;
    if (r.ok) {
      const d = r.json;
      if (!d || typeof d !== 'object' || d.product !== 'aiodos') throw clientError('not_aiodos', lang, { vars: { host }, status: r.status });
      if (typeof d.api !== 'number' || d.api < API_VERSION) throw clientError('too_old', lang, { status: r.status });
      return d;
    }
    if (!fromAiodos) {
      if (r.status === 401 && r.json && typeof r.json.error === 'string') throw clientError('too_old', lang, { status: 401 });
      throw clientError('not_aiodos', lang, { vars: { host }, status: r.status });
    }
    return ctx.transport.expectOk(r); // lève l'erreur d'AIODOS (other_account…)
  }

  /** L'info sans clé, gardée 60 s au plus. */
  async function getInfo(origin, { signal, fresh = false } = {}) {
    if (!fresh && info && info.origin === origin && Date.now() - info.at < PROOF_TTL) return info.data;
    const data = await fetchInfo(origin, { signal });
    info = { origin, data, at: Date.now() };
    if (proof && proof.origin === origin && proof.instance !== (data.instance ?? null)) proof = null;
    return data;
  }

  /** L'instance du fichier de découverte a-t-elle changé depuis la preuve ? */
  async function instanceMoved(origin, instance) {
    const disc = await readDiscovery({ env: ctx.env, platform: ctx.platform });
    if (!disc || disc.running === false || typeof disc.instance !== 'string') return false;
    if (discoveryUrl(disc) !== origin) return false;
    return disc.instance !== instance;
  }

  /** Fait (ou reprend) la preuve d'identité pour cette clé ; lève untrusted si elle est fausse. */
  async function ensureProof(origin, key, { signal } = {}) {
    const fp = keyFingerprint(key);
    if (
      proof &&
      proof.origin === origin &&
      proof.fp === fp &&
      Date.now() - proof.at < PROOF_TTL &&
      !(await instanceMoved(origin, proof.instance))
    ) {
      return proof;
    }
    proof = null;
    const challenge = newChallenge();
    const data = await fetchInfo(origin, { params: { key_id: keyIdOf(key), challenge }, signal });
    if (!proofMatches(key, challenge, data.proof)) {
      const host = new URL(origin).host;
      ctx.log('warn', `preuve d'identité fausse sur ${host} : la clé n'est pas envoyée`);
      throw clientError('untrusted', ctx.lang(), { vars: { host } });
    }
    const { proof: _ignored, ...rest } = data;
    info = { origin, data: rest, at: Date.now() };
    proof = { origin, fp, instance: data.instance ?? null, at: Date.now() };
    return proof;
  }

  /** La clé rangée par l'appli (relue à chaque appel), ou null. */
  async function readKey() {
    const key = await ctx.keyStore.get();
    return typeof key === 'string' && key.trim() ? key.trim() : null;
  }

  /** La clé, ou une erreur needs_access sans rien envoyer. */
  async function requireKey() {
    const key = await readKey();
    const lang = ctx.lang();
    if (!key) throw clientError('needs_access', lang);
    if (!isAppKey(key)) throw clientError('needs_access', lang, { textKey: 'bad_key' });
    return key;
  }

  /**
   * Une requête avec la clé, après une preuve juste. Rend la réponse brute du transport.
   * `feature` : la fonction (ou la liste de fonctions) que l'info doit annoncer (sinon
   * NotSupported, sans rien envoyer).
   */
  async function keyed(o) {
    const key = await requireKey();
    const origin = o.origin ?? (await ctx.resolveOrigin()).origin;
    await ensureProof(origin, key, { signal: o.signal });
    if (o.feature) requireFeatureIn(await getInfo(origin, { signal: o.signal }), o.feature);
    try {
      return await ctx.transport.send({ ...o, origin, key });
    } catch (err) {
      if (err.code !== 'cancelled') forget();
      throw err;
    }
  }

  /** La première fonction demandée que l'info n'annonce pas lève NotSupported. */
  function requireFeatureIn(data, feature) {
    const list = data && Array.isArray(data.features) ? data.features : [];
    for (const f of Array.isArray(feature) ? feature : [feature]) {
      if (!list.includes(f)) {
        throw clientError('not_supported', ctx.lang(), { vars: { feature: f }, detail: { feature: f, announced: false } });
      }
    }
  }

  /** Lève NotSupported si l'AIODOS joint n'annonce pas cette fonction (ou l'une de ces fonctions). */
  async function requireFeature(feature, { signal, origin } = {}) {
    const o = origin ?? (await ctx.resolveOrigin()).origin;
    const data = await getInfo(o, { signal });
    requireFeatureIn(data, feature);
    return data;
  }

  /**
   * L'info avec la clé, après la preuve : l'état de la clé (auth), les droits et les cercles
   * accordés à l'appli (app.scopes, app.where), l'ouverture aux applis (access).
   */
  async function appInfo({ signal } = {}) {
    const key = await requireKey();
    const origin = (await ctx.resolveOrigin()).origin;
    await ensureProof(origin, key, { signal });
    return fetchInfo(origin, { key, signal });
  }

  return { fetchInfo, getInfo, ensureProof, readKey, requireKey, keyed, requireFeature, appInfo, forget };
}
