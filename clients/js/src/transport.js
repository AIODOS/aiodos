// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — le transport vers la passerelle.
//
// - `fetch` est relu à chaque appel (option `fetch` du client, sinon globalThis.fetch) :
//   les essais le remplacent ;
// - l'hôte est vérifié AVANT tout envoi : 127.0.0.1, localhost ou [::1] (code non_local_url) ;
// - aucune redirection suivie (redirect: 'error'), aucun proxy (le proxy d'environnement de
//   Node, NODE_USE_ENV_PROXY, ferait passer 127.0.0.1 par lui : la requête est refusée) ;
// - un délai d'inactivité, que le lecteur d'un flux remet à zéro à chaque morceau ;
// - l'annulation par AbortSignal, qui ferme vraiment la connexion.

import { AiodosError, clientError, errorFromResponse } from './errors.js';
import { CLIENT_HEADER } from './version.js';

/** Adresse par défaut : 127.0.0.1 et non localhost, la passerelle n'écoute pas en IPv6. */
export const DEFAULT_URL = 'http://127.0.0.1:11435';

const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);
// le fetch de Node tel qu'au chargement : lui seul passe par le répartiteur global d'undici
const NODE_FETCH = globalThis.fetch;
const DISPATCHER = Symbol.for('undici.globalDispatcher.1');

const REFUSED = new Set(['ECONNREFUSED', 'EADDRNOTAVAIL', 'EHOSTUNREACH', 'ENETUNREACH', 'ENOTFOUND', 'EAI_AGAIN']);
const LOST = new Set(['ECONNRESET', 'EPIPE', 'ECONNABORTED', 'ETIMEDOUT', 'UND_ERR_SOCKET', 'UND_ERR_CLOSED']);

/**
 * L'origine (« http://127.0.0.1:11435 ») d'une adresse locale.
 * Toute autre adresse lève une erreur non_local_url, sans rien envoyer.
 */
export function localOrigin(raw, lang = 'fr') {
  let u = null;
  try {
    u = raw instanceof URL ? raw : new URL(String(raw));
  } catch {
    u = null;
  }
  const ok =
    u &&
    (u.protocol === 'http:' || u.protocol === 'https:') &&
    LOCAL_HOSTS.has(u.hostname.toLowerCase()) &&
    !u.username &&
    !u.password;
  if (!ok) {
    // l'adresse montrée n'a ni identifiants ni chemin
    throw clientError('non_local_url', lang, { vars: { url: u ? `${u.protocol}//${u.host}` : '?' } });
  }
  return u.origin;
}

/** Vrai si l'adresse est locale (127.0.0.1, localhost ou [::1]). */
export function isLocalUrl(raw) {
  try {
    localOrigin(raw);
    return true;
  } catch {
    return false;
  }
}

// --- le proxy d'environnement de Node ---------------------------------------------------

function envFlag(v) {
  return typeof v === 'string' && /^(1|true|yes|on)$/i.test(v.trim());
}

function envProxyOn() {
  const env = process.env;
  if (envFlag(env.NODE_USE_ENV_PROXY)) return true;
  if ((process.execArgv || []).includes('--use-env-proxy')) return true;
  return /(^|\s)--use-env-proxy(\s|$)/.test(env.NODE_OPTIONS || '');
}

function proxyVarFor(u) {
  const env = process.env;
  return u.protocol === 'https:' ? env.https_proxy || env.HTTPS_PROXY : env.http_proxy || env.HTTP_PROXY;
}

// NO_PROXY tel qu'undici le lit : « * », l'hôte exact (avec ou sans port), ou un suffixe « .xyz »
function noProxyCovers(u) {
  const list = process.env.no_proxy || process.env.NO_PROXY || '';
  const host = u.hostname.toLowerCase();
  const port = Number(u.port || (u.protocol === 'https:' ? 443 : 80));
  for (const raw of list.split(/[\s,]+/)) {
    const entry = raw.trim().toLowerCase();
    if (!entry) continue;
    if (entry === '*') return true;
    const m = /^(.*?)(?::(\d+))?$/.exec(entry);
    let h = m[1];
    const p = m[2] ? Number(m[2]) : 0;
    if (p && p !== port) continue;
    if (h.startsWith('*')) h = h.slice(1);
    if (h.startsWith('.') ? host.endsWith(h) : host === h) return true;
  }
  return false;
}

/** Vrai si le fetch de Node ferait passer cette adresse par un proxy. */
export function proxyInTheWay(origin, fetchFn) {
  if (fetchFn !== NODE_FETCH) return false; // fetch fourni par l'appli ou par les essais
  const u = new URL(origin);
  if (noProxyCovers(u)) return false;
  const d = globalThis[DISPATCHER];
  const name = (d && d.constructor && d.constructor.name) || '';
  if (/proxy/i.test(name)) {
    // celui que Node crée lui-même d'après l'environnement : proxy seulement si la variable est là
    if (name === 'EnvHttpProxyAgent' && envProxyOn()) return Boolean(proxyVarFor(u));
    return true;
  }
  return envProxyOn() && Boolean(proxyVarFor(u));
}

// --- le délai d'inactivité ----------------------------------------------------------------

/** Un minuteur que l'on remet à zéro à chaque signe de vie. */
export class IdleTimer {
  constructor(ms, onFire) {
    this.ms = ms;
    this.onFire = onFire;
    this.handle = null;
    this.fired = false;
    this.reset(ms);
  }

  reset(ms = this.ms) {
    this.ms = ms;
    if (this.handle) clearTimeout(this.handle);
    this.handle = null;
    if (this.fired || !Number.isFinite(ms) || ms <= 0) return;
    this.handle = setTimeout(() => {
      this.handle = null;
      this.fired = true;
      this.onFire();
    }, Math.min(ms, 2 ** 31 - 1));
  }

  stop() {
    if (this.handle) clearTimeout(this.handle);
    this.handle = null;
  }
}

// --- la requête -----------------------------------------------------------------------------

const TIMEOUT_REASON = new Error('aiodos-client : délai dépassé');
const STOP_REASON = new Error('aiodos-client : flux fermé par le client');

function parseJson(text) {
  if (typeof text !== 'string' || !text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** Traduit l'échec de fetch (ou de la lecture du corps) en AiodosError. */
function mapFailure(err, { lang, userSignal, timedOut, host }) {
  if (err instanceof AiodosError) return err;
  if (userSignal && userSignal.aborted) return clientError('cancelled', lang);
  if (timedOut) return clientError('timeout', lang);
  const cause = err && err.cause;
  const code = (cause && cause.code) || (err && err.code);
  if (cause && /redirect/i.test(String(cause.message || ''))) {
    return clientError('protocol', lang, { textKey: 'redirect', vars: { host } });
  }
  if (err && err.name === 'AbortError') return clientError('cancelled', lang);
  if (REFUSED.has(code)) return clientError('unreachable', lang, { vars: { host }, cause: err });
  if (LOST.has(code) || (err instanceof TypeError && /terminated|closed|socket/i.test(String(err.message)))) {
    return clientError('unreachable', lang, { textKey: 'connection_lost', vars: { host }, cause: err });
  }
  if (err instanceof TypeError) return clientError('unreachable', lang, { vars: { host }, cause: err });
  return clientError('protocol', lang, { cause: err });
}

/**
 * Le transport d'un client.
 * @param {object} ctx
 * @param {() => Function} ctx.getFetch  le fetch à utiliser, relu à chaque appel
 * @param {() => string} ctx.lang
 * @param {(level: string, message: string, data?: object) => void} ctx.log
 * @param {() => void} [ctx.connectionLost]  prévenu de chaque erreur de connexion (la preuve sera refaite)
 */
export function createTransport(ctx) {
  const lost = () => {
    try {
      if (typeof ctx.connectionLost === 'function') ctx.connectionLost();
    } catch {
      /* rien */
    }
  };
  /**
   * Une requête. Rend la réponse entière ({status, ok, headers, json, text}), ou, pour un
   * flux qui a réussi, une poignée {stream: true, res, timer, fail, abort, done}.
   * N'échoue (rejette) que pour une erreur de transport : une réponse HTTP d'erreur est rendue.
   */
  async function send(o) {
    const lang = ctx.lang();
    const origin = localOrigin(o.origin, lang); // avant tout envoi
    const url = new URL(o.path, origin);
    if (url.origin !== origin) throw clientError('non_local_url', lang, { vars: { url: url.origin } });
    const fetchFn = ctx.getFetch();
    if (typeof fetchFn !== 'function') throw new TypeError('aiodos-client : aucun fetch disponible (Node 20.3 ou plus)');
    if (proxyInTheWay(origin, fetchFn)) throw clientError('non_local_url', lang, { textKey: 'proxy', detail: { proxy: true } });
    if (o.signal && o.signal.aborted) throw clientError('cancelled', lang);

    const method = o.method || 'GET';
    const headers = {
      Accept: o.stream ? 'text/event-stream' : 'application/json',
      'Accept-Language': lang,
      'X-Aiodos-Client': CLIENT_HEADER,
      ...(o.headers || {}),
    };
    let body;
    if (o.json !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(o.json);
    }
    if (o.key) headers.Authorization = `Bearer ${o.key}`;

    const ctrl = new AbortController();
    let timedOut = false;
    const timer = new IdleTimer(o.timeout, () => {
      timedOut = true;
      ctrl.abort(TIMEOUT_REASON);
    });
    const signal = o.signal ? AbortSignal.any([o.signal, ctrl.signal]) : ctrl.signal;
    const fail = (err) => {
      const mapped = mapFailure(err, { lang, userSignal: o.signal, timedOut, host: url.host });
      if (mapped.code === 'unreachable' || mapped.code === 'timeout') lost(); // même en plein flux
      return mapped;
    };
    const started = Date.now();
    let res;
    try {
      res = await fetchFn(url.href, { method, headers, body, signal, redirect: 'error' });
    } catch (err) {
      timer.stop();
      const mapped = fail(err);
      ctx.log('debug', `${method} ${url.pathname} : ${mapped.code}`);
      throw mapped;
    }
    ctx.log('debug', `${method} ${url.pathname} → ${res.status} (${Date.now() - started} ms)`);

    if ((res.status >= 300 && res.status < 400) || res.type === 'opaqueredirect') {
      // un fetch qui n'a pas levé d'erreur lui-même : la redirection n'est pas suivie non plus
      timer.stop();
      try {
        await res.body?.cancel();
      } catch {
        /* rien à libérer */
      }
      throw clientError('protocol', lang, { textKey: 'redirect', vars: { host: url.host }, status: res.status });
    }
    if (o.stream && res.ok) {
      return {
        stream: true,
        res,
        url,
        timer,
        fail,
        abort: (reason) => ctrl.abort(reason ?? STOP_REASON),
        done: () => timer.stop(),
      };
    }
    let text;
    try {
      text = await res.text();
    } catch (err) {
      throw fail(err);
    } finally {
      timer.stop();
    }
    return { stream: false, status: res.status, ok: res.ok, headers: res.headers, json: parseJson(text), text, url };
  }

  /** Lève l'erreur de la réponse si elle n'est pas un succès ; sinon rend son JSON. */
  function expectOk(r) {
    if (r.ok) return r.json;
    throw errorFromResponse({ status: r.status, json: r.json, headers: r.headers, lang: ctx.lang() });
  }

  return { send, expectOk };
}
