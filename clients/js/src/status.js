// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — l'état vu par l'appli : status(), qui ne rejette jamais, et
// waitReady() (docs/api-applications.md § 6).
//
// Ordre : demande en cours dans ce client ; fichier de découverte ; GET /aiodos/v1/info
// sans clé ; puis, avec une clé, la preuve d'identité et l'info avec la clé.

import { findInstall, pidAlive, readDiscovery } from './discovery.js';
import { clientError } from './errors.js';
import { isAppKey } from './proof.js';
import { stateText } from './texts.js';
import { sleep } from './util.js';

const WAITING_STATES = new Set(['not_running', 'not_installed']);

function list(v) {
  return Array.isArray(v) ? v.filter((s) => typeof s === 'string') : [];
}

function gatewayOf(disc) {
  const g = disc && disc.gateway;
  if (!g || typeof g !== 'object') return null;
  return {
    state: typeof g.state === 'string' ? g.state : null,
    url: typeof g.url === 'string' ? g.url : null,
    apps: g.apps ?? null,
    requests: g.requests ?? null,
    approvalUi: g.approval_ui ?? null,
    error: typeof g.error === 'string' ? g.error : null,
  };
}

function accessOf(info) {
  const a = info && info.access;
  if (!a || typeof a !== 'object') return null;
  return { apps: a.apps ?? null, requests: a.requests ?? null, approvalUi: a.approval_ui ?? null };
}

/**
 * L'état d'AIODOS pour cette appli. Ne rejette jamais.
 * @returns {Promise<object>} { state, message, code, url, installed, running, version, api,
 *   instance, features, access, gateway, app, limits, scopes, where, userCode, requestId, expiresAt }
 */
export async function status(ctx, { signal } = {}) {
  let lang = 'fr';
  try {
    lang = ctx.lang();
  } catch {
    /* langue par défaut */
  }
  const out = {
    state: null,
    message: '',
    code: null,
    url: null,
    installed: null,
    running: null,
    version: null,
    api: null,
    instance: null,
    features: [],
    access: null,
    gateway: null,
    app: null,
    limits: null,
    scopes: [],
    where: [],
    userCode: null,
    requestId: null,
    expiresAt: null,
  };
  const finish = (state, extra = {}) => {
    const r = { ...out, ...extra, state };
    r.message = extra.message || stateText(state, lang, { code: r.userCode ?? '' });
    return r;
  };
  try {
    return await compute(ctx, { lang, out, finish, signal });
  } catch (err) {
    return finish('not_aiodos', { code: (err && err.code) || 'protocol' });
  }
}

async function compute(ctx, { out, finish, signal }) {
  // 1. une demande d'accès de ce client attend l'utilisateur
  const pending = ctx.access.pending;
  if (pending && pending.expiresAt > Date.now()) {
    return finish('pending', {
      userCode: pending.userCode,
      requestId: pending.requestId,
      expiresAt: pending.expiresAt,
      url: pending.origin,
    });
  }

  // 2. le fichier de découverte, puis l'adresse
  const disc = await readDiscovery({ env: ctx.env, platform: ctx.platform });
  out.gateway = gatewayOf(disc);
  let origin;
  try {
    origin = (await ctx.resolveOrigin()).origin;
  } catch (err) {
    return finish('not_aiodos', { code: err.code, message: err.message });
  }
  out.url = origin;
  const discRunning = Boolean(disc) && disc.running !== false && pidAlive(disc.pid) !== false;

  // ce que dit une erreur de connexion ou de reconnaissance
  const fromError = async (err) => {
    const code = (err && err.code) || 'protocol';
    if (code === 'unreachable' || code === 'cancelled') {
      if (discRunning && out.gateway && out.gateway.state === 'error') {
        return finish('gateway_error', { code, installed: true, running: true, message: out.gateway.error || undefined });
      }
      if (discRunning) return finish('gateway_closed', { code, installed: true, running: true });
      const installed = Boolean(disc) || Boolean(await findInstall({ env: ctx.env, platform: ctx.platform }));
      return finish(installed ? 'not_running' : 'not_installed', { code, installed, running: false });
    }
    if (code === 'too_old') return finish('too_old', { code, installed: true, running: true });
    if (code === 'other_account') return finish('other_account', { code, installed: true, running: true });
    if (code === 'untrusted') return finish('untrusted', { code, installed: true, running: true });
    if (code === 'non_local_url') return finish('not_aiodos', { code, message: err.message });
    return finish('not_aiodos', { code });
  };

  // 3. l'info sans clé
  let info;
  try {
    info = await ctx.session.getInfo(origin, { signal, fresh: true });
  } catch (err) {
    return fromError(err);
  }
  out.installed = true;
  out.running = true;
  out.version = typeof info.version === 'string' ? info.version : null;
  out.api = typeof info.api === 'number' ? info.api : null;
  out.instance = typeof info.instance === 'string' ? info.instance : null;
  out.features = list(info.features);
  out.access = accessOf(info);
  if (out.access && out.access.apps === false) return finish('apps_closed');

  // 4. sans clé (un coffre de l'appli qui échoue vaut « pas de clé »)
  let key = null;
  try {
    key = await ctx.session.readKey();
  } catch {
    return finish('needs_access', { code: 'key_store' });
  }
  if (!key) {
    if (ctx.access.denied) return finish('denied', { code: 'access_denied' });
    if (out.access && out.access.requests === false) return finish('requests_closed');
    if (out.access && out.access.approvalUi === false) return finish('headless');
    return finish('needs_access');
  }
  if (!isAppKey(key)) return finish('needs_access', { code: 'bad_key' });

  // 5. la preuve, puis l'info avec la clé
  let keyed;
  try {
    await ctx.session.ensureProof(origin, key, { signal });
    keyed = await ctx.session.fetchInfo(origin, { key, signal });
  } catch (err) {
    return fromError(err);
  }
  if (keyed.auth === 'valid') {
    const a = keyed.app && typeof keyed.app === 'object' ? keyed.app : {};
    const app = {
      id: a.id ?? null,
      name: a.name ?? null,
      kind: a.kind ?? null,
      scopes: list(a.scopes),
      where: list(a.where),
      priority: a.priority ?? null,
      expires: a.expires ?? null,
    };
    // limites en camelCase (requestsPerMinute, streams, maxBodyBytes) ; une valeur inconnue reste null
    const limits =
      keyed.limits && typeof keyed.limits === 'object'
        ? Object.fromEntries(Object.entries(keyed.limits).map(([k, v]) => [k.replace(/_([a-z])/g, (_, c) => c.toUpperCase()), v]))
        : null;
    return finish('ready', {
      features: list(keyed.features).length ? list(keyed.features) : out.features,
      app,
      limits,
      scopes: app.scopes,
      where: app.where,
    });
  }
  if (keyed.auth === 'revoked' || keyed.auth === 'expired') {
    return finish('revoked', { code: keyed.auth === 'revoked' ? 'key_revoked' : 'key_expired' });
  }
  return finish('needs_access', { code: 'invalid_api_key' });
}

/**
 * Attend qu'AIODOS réponde (until: 'running', par défaut : tout état autre que
 * not_running et not_installed), ou qu'il soit prêt pour l'appli (until: 'ready').
 * Rend l'état ; rejette Timeout au-delà de `timeout`, « Arrêté » si `signal` est levé.
 * Ne lance rien : c'est launch() qui lance AIODOS.
 */
export async function waitReady(ctx, { timeout = 60_000, interval = 1000, until = 'running', signal } = {}) {
  const lang = ctx.lang();
  const deadline = Date.now() + timeout;
  for (;;) {
    if (signal && signal.aborted) throw clientError('cancelled', lang);
    const st = await status(ctx, { signal });
    if (until === 'ready' ? st.state === 'ready' : !WAITING_STATES.has(st.state)) return st;
    if (st.state === 'not_installed') return st; // rien à attendre
    if (Date.now() + interval > deadline) throw clientError('timeout', lang, { detail: { state: st.state } });
    await sleep(interval, signal, lang);
  }
}
