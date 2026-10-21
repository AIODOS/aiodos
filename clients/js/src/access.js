// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — l'accès : connect(), requestAccess(), la relève de la clé (vocabulaire
// de la RFC 8628) et disconnect() (docs/api-applications.md § 4.3, § 4.4, § 6).
//
// connect() : passerelle ouverte, POST /aiodos/v1/access/requests ; fermée, AIODOS arrêté ou
// ouvert à la seule clé générale, le programme d'aide « --app-request ». Puis la relève, toutes
// les `interval` secondes (slow_down : 5 s de plus ; connexion refusée tolérée tant que la
// passerelle n'écoute pas), et le rangement de la clé par key.set(). Le device_code n'est
// jamais exposé : qui l'a peut relever la clé.

import { runAppRequest } from './discovery.js';
import { clientError, errorFromResponse, makeError } from './errors.js';
import { isAppKey } from './proof.js';
import { checkWhere, compact, sleep, toList } from './util.js';

const GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:device_code';
const REQUEST_TIMEOUT = 15_000;
const POLL_TIMEOUT = 10_000;
const REVOKE_TIMEOUT = 10_000;
const RESERVED_NAMES = new Set(['aiodos', 'autres outils', 'windows', 'microsoft']);
// caractères de contrôle et de sens d'écriture, retirés du nom (comme le fait AIODOS)
const STRIP_RE = /[\u0000-\u001f\u007f-\u009f‎‏‪-‮⁦-⁩]/g;
const INSPECT = Symbol.for('nodejs.util.inspect.custom');

function invalid(lang, param, fr, en) {
  return makeError('invalid_request', lang === 'en' ? en : fr, { param });
}

/** Le corps de la demande (§ 4.3), vérifié avant tout envoi. */
export function accessBody(ctx, { scopes, where, purpose } = {}, lang = 'fr') {
  const name = String(ctx.app ?? '').normalize('NFC').replace(STRIP_RE, '').trim();
  if (!name || name.length > 40 || RESERVED_NAMES.has(name.toLowerCase())) {
    throw invalid(lang, 'app', "Nom d'appli invalide (1 à 40 caractères, ni « AIODOS », « Autres outils », « Windows », « Microsoft »).", 'Invalid app name (1 to 40 characters, not “AIODOS”, “Autres outils”, “Windows”, “Microsoft”).');
  }
  const id = String(ctx.appId ?? '');
  if (!/^[a-z0-9.-]{2,40}$/.test(id)) {
    throw invalid(lang, 'appId', "Identifiant d'appli invalide ([a-z0-9.-], 2 à 40 caractères).", 'Invalid app id ([a-z0-9.-], 2 to 40 characters).');
  }
  const version = ctx.version == null || ctx.version === '' ? undefined : String(ctx.version);
  if (version && version.length > 40) throw invalid(lang, 'version', 'Version trop longue (40 caractères au plus).', 'Version too long (40 characters at most).');
  const list = [...new Set(toList(scopes))];
  if (!list.length || list.some((s) => !/^[a-z][a-z0-9_.:-]{0,39}$/.test(s))) {
    throw invalid(lang, 'scopes', 'Droits invalides (par exemple ["chat", "embed"]).', 'Invalid scopes (for example ["chat", "embed"]).');
  }
  const circles = where == null ? undefined : checkWhere(where, lang);
  const why = purpose == null ? undefined : String(purpose);
  if (why && why.length > 200) throw invalid(lang, 'purpose', 'Raison trop longue (200 caractères au plus).', 'Purpose too long (200 characters at most).');
  return compact({ client_name: name, client_id: id, client_version: version, scope: list.join(' '), where: circles, purpose: why });
}

function intervalSeconds(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return 5; // valeur de la RFC 8628 quand il manque
  return Math.min(60, Math.max(0.1, n));
}

function grantOf(d, upgraded) {
  const a = d && d.aiodos && typeof d.aiodos === 'object' ? d.aiodos : {};
  return {
    upgraded,
    scopes: toList(d && d.scope),
    where: toList(a.where),
    appId: a.app_id ?? null,
    priority: a.priority ?? null,
    expires: a.expires ?? null,
  };
}

/** Relève la clé (POST /aiodos/v1/access/token) jusqu'à la réponse de l'utilisateur. */
async function poll(ctx, r, pending, { signal, timeout } = {}) {
  const lang = ctx.lang();
  const deadline = Math.min(r.expiresAt, timeout > 0 ? Date.now() + timeout : Infinity);
  let intervalMs = r.interval * 1000;
  try {
    for (;;) {
      if (Date.now() + intervalMs > deadline) throw clientError('expired_token', lang);
      await sleep(intervalMs, signal, lang);
      let res;
      try {
        res = await ctx.transport.send({
          origin: r.origin,
          method: 'POST',
          path: '/aiodos/v1/access/token',
          json: { grant_type: GRANT_TYPE, device_code: r.deviceCode },
          signal,
          timeout: POLL_TIMEOUT,
        });
      } catch (err) {
        // la passerelle n'écoute pas encore (« Autoriser » l'ouvrira) : on continue
        if (err.code === 'unreachable' || err.code === 'timeout') continue;
        throw err;
      }
      if (res.ok) {
        const d = res.json || {};
        if (typeof d.access_token === 'string') {
          if (!isAppKey(d.access_token)) throw clientError('protocol', lang);
          await ctx.keyStore.set(d.access_token);
          ctx.session.forget(); // nouvelle clé : nouvelle preuve
          ctx.access.denied = false;
          ctx.log('info', `accès accordé par AIODOS (droits : ${toList(d.scope).join(' ') || '?'})`);
          return grantOf(d, false);
        }
        if (d.aiodos && d.aiodos.upgraded === true) {
          ctx.log('info', `droits en plus accordés par AIODOS (${toList(d.scope).join(' ') || '?'})`);
          return grantOf(d, true);
        }
        throw clientError('protocol', lang);
      }
      const e = res.json && typeof res.json.error === 'string' ? res.json.error : null;
      if (e === 'authorization_pending') continue;
      if (e === 'slow_down') {
        intervalMs += 5000;
        continue;
      }
      const err = errorFromResponse({ status: res.status, json: res.json, headers: res.headers, lang });
      if (e === 'access_denied') {
        if (!r.upgrade) ctx.access.denied = true;
        throw err;
      }
      if ((res.status === 429 || res.status === 503) && err.retryable) {
        intervalMs = Math.max(intervalMs, (err.retryAfter ?? 0) * 1000);
        continue;
      }
      throw err;
    }
  } finally {
    if (ctx.access.pending === pending) ctx.access.pending = null;
  }
}

/** L'objet rendu par requestAccess() : le code à afficher, et wait(). */
function accessRequest(ctx, r) {
  const expiresIn = Number(r.expiresIn) > 0 ? Number(r.expiresIn) : 600;
  const full = {
    origin: r.origin,
    deviceCode: r.deviceCode,
    interval: intervalSeconds(r.interval),
    expiresAt: Date.now() + expiresIn * 1000,
    upgrade: Boolean(r.upgrade),
  };
  const pending = { userCode: r.userCode, requestId: r.requestId ?? null, expiresAt: full.expiresAt, origin: r.origin };
  ctx.access.pending = pending;
  ctx.access.denied = false;
  let waiting = null;
  const visible = {
    userCode: r.userCode,
    requestId: r.requestId ?? null,
    expiresIn,
    interval: full.interval,
    expiresAt: full.expiresAt,
    message: r.message ?? null,
    url: r.origin,
    upgrade: full.upgrade,
  };
  return Object.freeze({
    ...visible,
    /** Attend la réponse de l'utilisateur ; range la clé ; rend les droits accordés. */
    wait({ signal, timeout } = {}) {
      if (!waiting) waiting = poll(ctx, full, pending, { signal, timeout });
      return waiting;
    },
    toJSON() {
      return { ...visible };
    },
    toString() {
      return `[demande d'accès à AIODOS, code ${visible.userCode}]`;
    },
    [INSPECT]() {
      return `AccessRequest ${JSON.stringify(visible)}`;
    },
  });
}

/**
 * La demande d'accès par la passerelle ouverte (POST /aiodos/v1/access/requests). Avec une
 * clé valide (après la preuve), c'est une demande de droits en plus pour la même clé.
 */
export async function requestAccess(ctx, { scopes, where, purpose, signal, origin: given } = {}) {
  const lang = ctx.lang();
  const body = accessBody(ctx, { scopes, where, purpose }, lang);
  const origin = given ?? (await ctx.resolveOrigin()).origin;
  await ctx.session.requireFeature('access', { signal, origin });
  let key = null;
  try {
    key = await ctx.session.readKey();
  } catch {
    key = null;
  }
  const post = (withKey) =>
    ctx.transport.send({ origin, method: 'POST', path: '/aiodos/v1/access/requests', json: body, key: withKey, signal, timeout: REQUEST_TIMEOUT });
  let res = null;
  let upgrade = false;
  if (key && isAppKey(key)) {
    await ctx.session.ensureProof(origin, key, { signal }); // preuve fausse : rejet, clé non envoyée
    res = await post(key);
    upgrade = res.ok;
    if (res.status === 401) res = null; // clé retirée ou inconnue : une nouvelle demande, sans elle
  }
  if (!res) res = await post(undefined);
  const d = ctx.transport.expectOk(res);
  if (!d || typeof d.device_code !== 'string' || typeof d.user_code !== 'string') throw clientError('protocol', lang);
  return accessRequest(ctx, {
    origin,
    deviceCode: d.device_code,
    userCode: d.user_code,
    interval: d.interval,
    expiresIn: d.expires_in,
    requestId: d.request_id,
    message: d.message,
    upgrade,
  });
}

/**
 * Se connecter à AIODOS : la demande (passerelle ouverte) ou le programme d'aide (passerelle
 * fermée, AIODOS arrêté), le code donné à `onCode`, puis la relève et le rangement de la clé.
 * À n'appeler que sur un clic de l'utilisateur.
 */
export async function connect(ctx, { scopes, where, purpose, onCode, signal, timeout } = {}) {
  const lang = ctx.lang();
  const body = accessBody(ctx, { scopes, where, purpose }, lang); // vérifié avant tout
  const { origin } = await ctx.resolveOrigin();
  let req = null;
  let info = null;
  try {
    info = await ctx.session.getInfo(origin, { signal, fresh: true });
  } catch (err) {
    if (err.code !== 'unreachable') throw err; // too_old, not_aiodos, other_account, cancelled…
  }
  if (info && !(info.access && info.access.apps === false)) {
    try {
      req = await requestAccess(ctx, { scopes, where, purpose, signal, origin });
    } catch (err) {
      // fermée aux applis entre-temps, ou ouverte à la seule clé générale : le programme d'aide
      if (err.code !== 'apps_closed' && err.code !== 'unreachable') throw err;
    }
  }
  if (!req) {
    const out = await runAppRequest(body, { env: ctx.env, platform: ctx.platform, lang, signal, log: ctx.log });
    req = accessRequest(ctx, {
      origin: out.url ?? origin,
      deviceCode: out.deviceCode,
      userCode: out.userCode,
      interval: out.interval,
      expiresIn: out.expiresIn,
      requestId: out.requestId,
      message: null,
      upgrade: false,
    });
  }
  // la relève part tout de suite ; un onCode lent ne la retarde pas, un onCode qui échoue l'arrête
  const stop = new AbortController();
  const waiting = req.wait({ signal: signal ? AbortSignal.any([signal, stop.signal]) : stop.signal, timeout });
  if (typeof onCode === 'function') {
    try {
      await onCode(req.userCode, { requestId: req.requestId, expiresIn: req.expiresIn, message: req.message, url: req.url });
    } catch (err) {
      stop.abort();
      waiting.catch(() => {});
      throw err;
    }
  }
  return waiting;
}

/**
 * Se déconnecter : POST /aiodos/v1/access/revoke avec la clé (après la preuve), puis
 * key.set(null). La clé est effacée même si AIODOS ne répond pas ; `revoked` le dit.
 */
export async function disconnect(ctx, { signal } = {}) {
  let revoked = false;
  let key = null;
  try {
    key = await ctx.session.readKey();
  } catch {
    key = null;
  }
  if (key && isAppKey(key)) {
    try {
      const res = await ctx.session.keyed({ method: 'POST', path: '/aiodos/v1/access/revoke', json: {}, signal, timeout: REVOKE_TIMEOUT });
      revoked = Boolean(res.ok && res.json && res.json.revoked === true);
      if (!res.ok) ctx.log('warn', `retrait refusé par AIODOS (HTTP ${res.status})`);
    } catch (err) {
      if (err.code === 'cancelled') throw err;
      ctx.log('warn', `retrait impossible (${err.code}) : la clé est effacée quand même`);
    }
  }
  await ctx.keyStore.set(null);
  ctx.session.forget();
  ctx.access.denied = false;
  ctx.access.pending = null;
  return { revoked };
}
