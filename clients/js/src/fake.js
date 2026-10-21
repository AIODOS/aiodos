// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — un faux AIODOS, pour les essais du client et ceux des applis.
//
// - createFakeFetch() : une fonction fetch, sans réseau ;
// - startFakeServer() : un vrai serveur node:http sur 127.0.0.1:0 ;
// Les deux servent le contrat partagé (contract/, copie de clients/contract/ vérifiée par
// les essais) : info avec une preuve juste, demandes d'accès pilotables (approve, deny,
// expire, slowDown), relève, revoke, models, chat en flux (rejoue chat.json, y compris
// « : aiodos loading »), embeddings, erreurs à la demande (failNext).
// - writeFakeDiscovery() : un faux gateway.json dans un dossier temporaire ;
// - createFakeHome() : un faux AIODOS_HOME dont l'interpréteur est un script Node (faux
//   programme d'aide « --app-request », faux lancement « --background ») ;
// - createFakeClient() : un client branché sur un faux fetch, sans toucher à rien.
// Rien ici ne touche au vrai fichier de découverte, ni au réseau, ni à la vraie installation.

import { randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { createClient, memoryKeyStore } from './client.js';
import { computeProof } from './proof.js';

const CONTRACT_DIR = new URL('../contract/', import.meta.url);
const contractCache = new Map();
const GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:device_code';
const CIRCLES = ['here', 'mine', 'friends', 'public'];
const KNOWN_SCOPES = new Set(['chat', 'embed', 'libraries']);
const FAKE_MARK = Symbol('aiodos-fake');

/** Un fichier du contrat (copie neuve à chaque appel). */
export function loadContract(name) {
  if (!contractCache.has(name)) {
    contractCache.set(name, JSON.parse(readFileSync(new URL(`${name}.json`, CONTRACT_DIR), 'utf8')));
  }
  return structuredClone(contractCache.get(name));
}

/** Une interaction du contrat, par son nom. */
export function contractInteraction(file, name) {
  const it = loadContract(file).interactions.find((i) => i.name === name);
  if (!it) throw new Error(`contrat : « ${name} » absent de ${file}.json`);
  return it;
}

/** La clé de l'appli d'essai du contrat (proof.json). */
export const FAKE_KEY = 'aiodos_app_k3j7m2qa_' + 'A'.repeat(43);

const STATUS_OF = {
  host_refused: 400,
  invalid_request: 400,
  context_length_exceeded: 400,
  tools_unavailable: 400,
  vision_unsupported: 400,
  local_only: 400,
  unsupported_parameter: 400,
  invalid_api_key: 401,
  key_revoked: 401,
  key_expired: 401,
  scope_denied: 403,
  where_denied: 403,
  apps_closed: 403,
  requests_closed: 403,
  blocked_program: 403,
  other_account: 403,
  program_mismatch: 403,
  origin_refused: 403,
  model_not_found: 404,
  not_supported: 404,
  no_machine: 409,
  file_too_large: 413,
  unsupported_media_type: 415,
  rate_limited: 429,
  busy: 429,
  peer_failed: 502,
  engine_failed: 502,
  engine_unavailable: 503,
  approval_unavailable: 503,
};
const RETRYABLE = new Set(['rate_limited', 'busy', 'peer_failed', 'engine_failed', 'engine_unavailable', 'no_machine']);

const B32 = 'abcdefghijklmnopqrstuvwxyz234567';
function base32(n) {
  const bytes = randomBytes(n);
  let s = '';
  for (let i = 0; i < n; i++) s += B32[bytes[i] % 32];
  return s;
}
function newKey() {
  return `aiodos_app_${base32(8)}_${randomBytes(32).toString('base64url')}`;
}
function userCode() {
  const n = randomBytes(4).readUInt32BE(0) % 1_000_000;
  const s = String(n).padStart(6, '0');
  return `${s.slice(0, 3)} ${s.slice(3)}`;
}
function list(v) {
  if (v == null) return [];
  return (Array.isArray(v) ? v : String(v).split(/[\s,]+/)).map((s) => String(s).trim()).filter(Boolean);
}

/** Toutes les réponses d'erreur du contrat, par code (la première trouvée). */
function contractErrors() {
  const out = new Map();
  for (const file of ['errors', 'access', 'chat', 'embed']) {
    for (const it of loadContract(file).interactions) {
      const j = it.response && it.response.json;
      const code = j && (j.error && typeof j.error === 'object' ? j.error.code : j.code);
      if (typeof code === 'string' && it.response.status >= 400 && !out.has(code)) out.set(code, it.response);
    }
  }
  return out;
}

/**
 * Le faux AIODOS : son état, ses commandes, et handle(requête) → réponse.
 * @param {object} [options]
 */
export function createFake(options = {}) {
  const info0 = contractInteraction('info', 'info sans clé').response.json;
  const infoValid = contractInteraction('info', 'info avec une clé valide').response.json;
  const access0 = contractInteraction('access', "demande d'accès").response.json;
  const errorsByCode = contractErrors();
  const chats = loadContract('chat').interactions;
  const modelsContract = loadContract('models').interactions;
  const embedContract = contractInteraction('embed', 'embeddings').response.json;

  const fake = {
    [FAKE_MARK]: true,
    url: options.url ?? 'http://127.0.0.1:11435',
    version: options.version ?? info0.version,
    api: options.api ?? info0.api,
    instance: options.instance ?? info0.instance,
    features: options.features ? [...options.features] : [...info0.features],
    access: { apps: true, requests: true, approval_ui: true, ...(options.access || {}) },
    /** « aiodos », « old » (ancien AIODOS : 401 sans X-Aiodos-Api) ou « ollama » (404). */
    mode: options.mode ?? 'aiodos',
    /** faux : le fetch répond « connexion refusée » (passerelle fermée, AIODOS arrêté). */
    listening: options.listening ?? true,
    /** vrai : la preuve est fausse (un imposteur sur le port). */
    wrongProof: Boolean(options.wrongProof),
    /** l'intervalle de relève (secondes) et la durée d'une demande. */
    interval: options.interval ?? access0.interval,
    expiresIn: options.expiresIn ?? access0.expires_in,
    /** délai entre deux événements d'un flux (ms). */
    chatDelayMs: options.chatDelayMs ?? 0,
    models: {
      chat: structuredClone(modelsContract[0].response.json.models),
      embed: structuredClone(modelsContract[1].response.json.models),
    },
    /** les clés connues : clé → { id, name, scopes, where, priority, state } */
    keys: new Map(),
    /** chaque requête reçue : { method, path, headers, json, at } */
    requests: [],
    /** les adresses étrangères qu'un fetch a tenté de joindre (jamais servies) */
    foreign: [],
    /** les flux que le client a fermés avant la fin */
    closed: 0,
    /** la demande d'accès en cours */
    pending: null,
    queue: [],
    chatQueue: [],
    slowDownLeft: 0,
    listeners: new Set(),
  };

  /** Une clé connue d'AIODOS. */
  fake.addKey = (key, { scopes = ['chat', 'embed'], where = ['here', 'mine'], state = 'valid', name = 'PANDORA', priority = 'interactive' } = {}) => {
    fake.keys.set(key, { id: key.slice(11, 19), name, scopes: [...scopes], where: [...where], priority, state });
    return key;
  };
  /** Retire (« revoked ») ou fait expirer (« expired ») une clé. */
  fake.setKeyState = (key, state) => {
    const e = fake.keys.get(key);
    if (e) e.state = state;
  };
  fake.revokeKey = (key = FAKE_KEY) => fake.setKeyState(key, 'revoked');

  /** La réponse de l'utilisateur à la demande en cours. */
  fake.approve = ({ scopes, where } = {}) => {
    if (!fake.pending) throw new Error('faux AIODOS : aucune demande en cours');
    fake.pending.state = 'approved';
    fake.pending.grantedScopes = scopes ? [...scopes] : [...fake.pending.scopes];
    fake.pending.grantedWhere = where ? [...where] : fake.pending.where.filter((c) => c !== 'friends' && c !== 'public');
    if (!fake.pending.grantedWhere.length) fake.pending.grantedWhere = ['here'];
    // « Autoriser » ouvre la passerelle aux applications
    fake.access.apps = true;
    fake.listening = true;
  };
  fake.deny = () => {
    if (!fake.pending) throw new Error('faux AIODOS : aucune demande en cours');
    fake.pending.state = 'denied';
  };
  fake.expire = () => {
    if (!fake.pending) throw new Error('faux AIODOS : aucune demande en cours');
    fake.pending.state = 'expired';
  };
  /** Les `n` relèves suivantes répondent slow_down. */
  fake.slowDown = (n = 1) => {
    fake.slowDownLeft += n;
  };
  /** Une demande reçue par le canal privé (programme d'aide) plutôt que par la passerelle. */
  fake.adoptRequest = ({ device_code, user_code, request_id, scope = 'chat', where = CIRCLES } = {}) => {
    fake.pending = {
      deviceCode: device_code,
      userCode: user_code,
      requestId: request_id ?? `req_${base32(8)}`,
      state: 'pending',
      scopes: list(scope),
      where: list(where),
      upgradeOf: null,
      polls: [],
      collected: false,
    };
    return fake.pending;
  };

  /** La prochaine requête (hors /aiodos/v1/info, sauf `path`) reçoit cette erreur. */
  fake.failNext = (code, o = {}) => {
    let response = !o.status && !o.message && !o.detail ? errorsByCode.get(code) : null;
    if (response) response = structuredClone(response);
    else {
      const status = o.status ?? STATUS_OF[code] ?? 400;
      response = {
        status,
        headers: { 'X-Aiodos-Api': '1' },
        json: {
          error: {
            message: o.message ?? `Erreur d'essai : ${code}.`,
            type: status >= 500 ? 'api_error' : 'invalid_request_error',
            param: o.param ?? null,
            code,
            aiodos: { hint: o.hint ?? null, retryable: o.retryable ?? RETRYABLE.has(code), detail: o.detail ?? {} },
          },
        },
      };
    }
    if (o.retryAfter != null) response.headers = { ...response.headers, 'Retry-After': String(o.retryAfter) };
    else if ((response.status === 429 || response.status === 503) && !response.headers['Retry-After']) {
      response.headers = { ...response.headers, 'Retry-After': '1' };
    }
    fake.queue.push({ path: o.path, response });
  };
  /**
   * La prochaine requête (hors /aiodos/v1/info, sauf `path`) reçoit cette réponse brute.
   * `when(requête)` resserre encore (par exemple : seulement la requête qui porte la clé).
   */
  fake.respondNext = (response, o = {}) => {
    fake.queue.push({ path: o.path, when: o.when, response: structuredClone(response) });
  };
  /** La prochaine requête reçoit une redirection (que le client ne doit pas suivre). */
  fake.redirectNext = (location = 'http://127.0.0.1:9/ailleurs', o = {}) => {
    fake.queue.push({ path: o.path, response: { status: 302, headers: { Location: location }, text: '' } });
  };
  /**
   * La prochaine conversation : le nom d'une interaction de chat.json, ou
   * { sse: [...lignes], headers, delayMs, firstDelayMs, stallAfter, cutAfter } — stallAfter :
   * se taire après n événements sans fermer ; cutAfter : couper la connexion après n événements.
   */
  fake.nextChat = (spec) => {
    fake.chatQueue.push(spec);
  };
  /** Les requêtes reçues sur un chemin. */
  fake.received = (p) => fake.requests.filter((r) => r.path.split('?')[0] === p);
  /** Prévenu de chaque événement : (« request », rec) ou (« closed »). */
  fake.on = (fn) => {
    fake.listeners.add(fn);
    return () => fake.listeners.delete(fn);
  };
  fake.emit = (kind, data) => {
    for (const fn of fake.listeners) {
      try {
        fn(kind, data);
      } catch {
        /* un écouteur d'essai */
      }
    }
  };

  if (options.key !== null) fake.addKey(options.key ?? FAKE_KEY, { scopes: options.scopes, where: options.where });

  // --- les réponses -------------------------------------------------------------------------

  const api = (headers = {}) => (fake.mode === 'aiodos' ? { 'X-Aiodos-Api': '1', ...headers } : headers);
  const ok = (json, status = 200, headers = {}) => ({ status, headers: api(headers), json });
  const fail = (code, o = {}) => {
    const known = errorsByCode.get(code);
    if (known && !o.message && !o.detail) return { ...structuredClone(known), headers: api(known.headers) };
    const status = o.status ?? STATUS_OF[code] ?? 400;
    return {
      status,
      headers: api(o.headers),
      json: {
        error: {
          message: o.message ?? code,
          type: status >= 500 ? 'api_error' : 'invalid_request_error',
          param: o.param ?? null,
          code,
          aiodos: { hint: o.hint ?? null, retryable: RETRYABLE.has(code), detail: o.detail ?? {} },
        },
      },
    };
  };
  const oauth = (error) => {
    const known = loadContract('access').interactions.find((i) => i.response.json && i.response.json.error === error);
    const description = known ? known.response.json.error_description : error;
    return { status: 400, headers: api(), json: { error, error_description: description } };
  };

  function authOf(req) {
    const h = req.headers.authorization;
    const key = h && /^Bearer\s+/i.test(h) ? h.replace(/^Bearer\s+/i, '').trim() : req.headers['x-api-key'] || null;
    if (!key) return { state: 'none' };
    const entry = fake.keys.get(key);
    if (!entry) return { state: 'invalid', key };
    return { state: entry.state, key, entry };
  }
  function keyError(a) {
    if (a.state === 'revoked') return fail('key_revoked');
    if (a.state === 'expired') return fail('key_expired', { message: "L'accès de cette application a expiré dans AIODOS." });
    return fail('invalid_api_key');
  }

  function info(req) {
    const body = {
      object: 'aiodos.info',
      product: 'aiodos',
      version: fake.version,
      api: fake.api,
      instance: fake.instance,
      features: [...fake.features],
      access: { ...fake.access },
      auth: 'none',
    };
    const keyId = req.url.searchParams.get('key_id');
    const challenge = req.url.searchParams.get('challenge');
    if (keyId != null && challenge != null) {
      const found = [...fake.keys.entries()].find(([, e]) => e.id === keyId);
      let proof = randomBytes(32).toString('base64url'); // identifiant inconnu : preuve aléatoire
      if (found && !fake.wrongProof) {
        try {
          proof = computeProof(found[0], challenge);
        } catch {
          /* défi mal formé : preuve aléatoire */
        }
      }
      body.proof = proof;
    }
    const a = authOf(req);
    if (a.state !== 'none') {
      body.auth = a.state;
      if (a.state === 'valid') {
        body.app = { ...infoValid.app, id: a.entry.id, name: a.entry.name, scopes: [...a.entry.scopes], where: [...a.entry.where], priority: a.entry.priority };
        body.limits = { ...infoValid.limits };
      }
    }
    return ok(body);
  }

  function accessRequests(req) {
    if (!fake.access.apps) return fail('apps_closed', { message: "AIODOS n'est ouvert qu'aux autres outils : ouvre-le aussi aux applications.", hint: 'Réglages → Applications et autres outils.' });
    if (!fake.access.requests) return fail('requests_closed');
    if (!fake.access.approval_ui) return fail('approval_unavailable', { message: "AIODOS tourne sans fenêtre : ouvre-le pour autoriser l'application." });
    const b = req.json;
    const scopes = list(b && b.scope);
    const where = b && b.where != null ? list(b.where) : [...CIRCLES];
    const bad =
      !b ||
      typeof b.client_name !== 'string' ||
      !b.client_name.trim() ||
      b.client_name.length > 40 ||
      typeof b.client_id !== 'string' ||
      !/^[a-z0-9.-]{2,40}$/.test(b.client_id) ||
      !scopes.length ||
      scopes.some((s) => !KNOWN_SCOPES.has(s)) ||
      where.some((c) => !CIRCLES.includes(c));
    if (bad) return fail('invalid_request', { message: 'Demande invalide.', param: 'scope' });
    const a = authOf(req);
    if (a.state !== 'none' && a.state !== 'valid') return keyError(a);
    const deviceCode = randomBytes(32).toString('base64url');
    fake.pending = {
      deviceCode,
      userCode: userCode(),
      requestId: `req_${base32(8)}`,
      state: 'pending',
      scopes,
      where,
      upgradeOf: a.state === 'valid' ? a.key : null,
      name: b.client_name,
      polls: [],
      collected: false,
    };
    return ok({
      ...access0,
      device_code: deviceCode,
      user_code: fake.pending.userCode,
      interval: fake.interval,
      expires_in: fake.expiresIn,
      request_id: fake.pending.requestId,
      message: `Une demande d'accès t'attend dans AIODOS. Vérifie que le code est ${fake.pending.userCode}.`,
    });
  }

  function accessToken(req) {
    const b = req.json || {};
    if (b.grant_type !== GRANT_TYPE) return { status: 400, headers: api(), json: { error: 'unsupported_grant_type', error_description: 'grant_type inconnu.' } };
    const p = fake.pending && fake.pending.deviceCode === b.device_code ? fake.pending : null;
    if (!p) return oauth('invalid_grant');
    p.polls.push(Date.now());
    if (fake.slowDownLeft > 0) {
      fake.slowDownLeft -= 1;
      return oauth('slow_down');
    }
    if (p.state === 'pending') return oauth('authorization_pending');
    if (p.state === 'denied') return oauth('access_denied');
    if (p.state === 'expired') return oauth('expired_token');
    if (p.collected) return oauth('invalid_grant');
    p.collected = true;
    const scope = p.grantedScopes.join(' ');
    if (p.upgradeOf) {
      const e = fake.keys.get(p.upgradeOf);
      if (e) e.scopes = [...new Set([...e.scopes, ...p.grantedScopes])];
      return ok({ token_type: 'Bearer', scope, aiodos: { app_id: e ? e.id : null, where: e ? [...e.where] : p.grantedWhere, priority: 'interactive', expires: null, upgraded: true } });
    }
    const key = newKey();
    fake.addKey(key, { scopes: p.grantedScopes, where: p.grantedWhere, name: p.name || 'appli' });
    return ok({ access_token: key, token_type: 'Bearer', scope, aiodos: { app_id: key.slice(11, 19), where: [...p.grantedWhere], priority: 'interactive', expires: null } });
  }

  function requireValid(req) {
    const a = authOf(req);
    if (a.state !== 'valid') return { error: keyError(a) };
    return { entry: a.entry, key: a.key };
  }

  function knownChatModel(id) {
    return fake.models.chat.some((m) => m.id === id || (Array.isArray(m.where) && m.where.some((w) => w.id === id)));
  }

  function chatResponse(req) {
    const v = requireValid(req);
    if (v.error) return v.error;
    if (!v.entry.scopes.includes('chat')) {
      return fail('scope_denied', { message: "Cette application n'a pas le droit « chat » : demande-le dans AIODOS.", detail: { scope: 'chat' } });
    }
    const asked = list(req.headers['x-aiodos-where']);
    if (asked.some((c) => !v.entry.where.includes(c))) {
      const r = structuredClone(errorsByCode.get('where_denied'));
      r.json.error.aiodos.detail = { allowed: [...v.entry.where] };
      r.json.error.message = `Lieu de calcul refusé : cette application n'a pas accès à « ${asked.find((c) => !v.entry.where.includes(c))} ».`;
      return r;
    }
    const b = req.json || {};
    const tools = Array.isArray(b.tools) && b.tools.length > 0;
    if (b.model === 'grand:7b' && tools && req.headers['x-aiodos-tools'] !== 'optional') {
      return structuredClone(chats.find((i) => i.name === 'outils demandés, modèle seulement sur une autre machine').response);
    }
    if (!knownChatModel(b.model)) return fail('model_not_found');
    if (b.stream === false) return structuredClone(chats.find((i) => i.name === 'conversation sans flux').response);
    let spec = fake.chatQueue.shift();
    if (spec == null) spec = tools ? "conversation en flux : appel d'outil" : 'conversation en flux : chargement, réflexion, texte';
    if (typeof spec === 'string') {
      const it = chats.find((i) => i.name === spec);
      if (!it) throw new Error(`faux AIODOS : conversation « ${spec} » absente de chat.json`);
      return { ...structuredClone(it.response), delayMs: fake.chatDelayMs };
    }
    return {
      status: spec.status ?? 200,
      headers: api({ 'Content-Type': 'text/event-stream', ...(spec.headers || {}) }),
      sse: [...spec.sse],
      delayMs: spec.delayMs ?? fake.chatDelayMs,
      firstDelayMs: spec.firstDelayMs,
      stallAfter: spec.stallAfter,
      cutAfter: spec.cutAfter,
    };
  }

  function embeddings(req) {
    const v = requireValid(req);
    if (v.error) return v.error;
    if (!v.entry.scopes.includes('embed')) return structuredClone(contractInteraction('embed', 'embeddings sans le droit').response);
    const b = req.json || {};
    const inputs = typeof b.input === 'string' ? [b.input] : Array.isArray(b.input) ? b.input : null;
    if (!inputs || !inputs.length) return fail('invalid_request', { message: 'input manquant.', param: 'input' });
    const known = fake.models.embed.some((m) => m.id === b.model);
    if (!known) return fail('model_not_found');
    const base = embedContract.data;
    return ok({
      object: 'list',
      model: b.model,
      data: inputs.map((_, i) => ({ object: 'embedding', index: i, embedding: [...base[i % base.length].embedding] })),
      usage: { ...embedContract.usage },
    });
  }

  function route(req) {
    const p = req.url.pathname;
    const m = req.method;
    if (m === 'GET' && p === '/') return { status: 200, headers: api({ 'Content-Type': 'text/plain' }), text: 'Ollama is running' };
    if (m === 'GET' && p === '/aiodos/v1/info') return info(req);
    if (m === 'POST' && p === '/aiodos/v1/access/requests') return accessRequests(req);
    if (m === 'POST' && p === '/aiodos/v1/access/token') return accessToken(req);
    if (m === 'POST' && p === '/aiodos/v1/access/revoke') {
      const v = requireValid(req);
      if (v.error) return v.error;
      v.entry.state = 'revoked';
      return ok({ revoked: true });
    }
    if (m === 'GET' && p === '/aiodos/v1/models') {
      const v = requireValid(req);
      if (v.error) return v.error;
      const kind = req.url.searchParams.get('kind') || 'chat';
      if (kind !== 'chat' && kind !== 'embed') return fail('invalid_request', { message: 'kind inconnu.', param: 'kind' });
      return ok({ object: 'list', models: structuredClone(fake.models[kind]) });
    }
    if (m === 'POST' && p === '/aiodos/v1/open') {
      const v = requireValid(req);
      if (v.error) return v.error;
      return ok({ opened: true }, 202);
    }
    if (m === 'GET' && p === '/v1/models') {
      const v = requireValid(req);
      if (v.error) return v.error;
      return structuredClone(modelsContract[2].response);
    }
    if (m === 'POST' && p === '/v1/chat/completions') return chatResponse(req);
    if (m === 'POST' && p === '/v1/embeddings') return embeddings(req);
    return fail('not_supported', { message: `Route inconnue : ${m} ${p}` });
  }

  /** Une requête → la réponse { status, headers, json | text | sse, delayMs, stallAfter, hang }. */
  fake.handle = async (req) => {
    const rec = { method: req.method, path: req.url.pathname + req.url.search, headers: { ...req.headers }, json: null, at: Date.now() };
    try {
      rec.json = req.body ? JSON.parse(req.body) : null;
    } catch {
      rec.json = undefined;
    }
    req.json = rec.json;
    fake.requests.push(rec);
    fake.emit('request', rec);
    const qi = fake.queue.findIndex(
      (q) =>
        (q.path ? (q.path instanceof RegExp ? q.path.test(req.url.pathname) : q.path === req.url.pathname) : req.url.pathname !== '/aiodos/v1/info') &&
        (typeof q.when !== 'function' || q.when({ ...rec })),
    );
    if (qi >= 0) {
      const { response } = fake.queue.splice(qi, 1)[0];
      return { ...response, headers: { ...(response.headers || {}) } };
    }
    if (fake.mode === 'ollama') {
      return req.url.pathname === '/' ? { status: 200, headers: {}, text: 'Ollama is running' } : { status: 404, headers: { 'Content-Type': 'text/plain' }, text: '404 page not found' };
    }
    if (fake.mode === 'old' && req.url.pathname.startsWith('/aiodos/')) {
      return structuredClone(contractInteraction('info', "ancienne version d'AIODOS : la route est inconnue").response);
    }
    // les pages web n'atteignent pas la passerelle
    const site = req.headers['sec-fetch-site'];
    const dest = req.headers['sec-fetch-dest'];
    if (req.headers.origin || site === 'cross-site' || site === 'same-site' || (dest && dest !== 'empty')) return fail('origin_refused');
    if (req.method === 'POST' && req.url.pathname.startsWith('/aiodos/v1/') && !/^application\/json/i.test(req.headers['content-type'] || '')) {
      return fail('unsupported_media_type', { message: 'Corps non JSON.' });
    }
    if (rec.json === undefined) return fail('invalid_request', { message: 'Corps JSON illisible.' });
    return route(req);
  };

  return fake;
}

function isFake(x) {
  return Boolean(x && x[FAKE_MARK]);
}

function lowerHeaders(h) {
  const out = {};
  if (!h) return out;
  if (typeof h.forEach === 'function' && !Array.isArray(h)) {
    h.forEach((v, k) => {
      out[String(k).toLowerCase()] = String(v);
    });
    return out;
  }
  for (const [k, v] of Array.isArray(h) ? h : Object.entries(h)) if (v != null) out[String(k).toLowerCase()] = Array.isArray(v) ? v.join(', ') : String(v);
  return out;
}

function netError(code, message) {
  return new TypeError('fetch failed', { cause: Object.assign(new Error(message), { code }) });
}

function abortError() {
  return new DOMException('This operation was aborted', 'AbortError');
}

/**
 * Un fetch sans réseau, qui sert le faux AIODOS à son adresse (fake.url) et seulement là.
 * Il imite celui de Node : TypeError « fetch failed » avec cause.code (ECONNREFUSED…),
 * redirect: 'error', annulation par signal (le flux est fermé, fake.closed le compte).
 */
export function createFakeFetch(fakeOrOptions = {}) {
  const fake = isFake(fakeOrOptions) ? fakeOrOptions : createFake(fakeOrOptions);
  async function fakeFetch(input, init = {}) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? String(input) : input.url);
    const signal = init.signal;
    if (signal && signal.aborted) throw signal.reason ?? abortError();
    const base = new URL(fake.url);
    if (url.origin !== base.origin) {
      fake.foreign.push(url.href);
      throw netError('ECONNREFUSED', `connect ECONNREFUSED ${url.host}`);
    }
    if (!fake.listening) throw netError('ECONNREFUSED', `connect ECONNREFUSED ${url.host}`);
    const out = await fake.handle({
      method: String(init.method || 'GET').toUpperCase(),
      url,
      headers: lowerHeaders(init.headers),
      body: init.body == null ? null : String(init.body),
    });
    if (out.hang) {
      await new Promise((resolve, reject) => {
        if (!signal) return;
        signal.addEventListener('abort', () => reject(signal.reason ?? abortError()), { once: true });
      });
    }
    if (out.status >= 300 && out.status < 400 && init.redirect === 'error') throw new TypeError('fetch failed', { cause: new Error('unexpected redirect') });
    return toResponse(out, signal, fake);
  }
  fakeFetch.fake = fake;
  return fakeFetch;
}

function toResponse(out, signal, fake) {
  const headers = new Headers(out.headers || {});
  if (out.sse) {
    if (!headers.has('content-type')) headers.set('content-type', 'text/event-stream');
    const enc = new TextEncoder();
    let i = 0;
    let timer = null;
    let closed = false;
    let onAbort = null;
    const stream = new ReadableStream({
      start(controller) {
        onAbort = () => {
          if (closed) return;
          closed = true;
          clearTimeout(timer);
          fake.closed += 1;
          fake.emit('closed');
          controller.error(signal.reason ?? abortError());
        };
        if (signal) signal.addEventListener('abort', onAbort, { once: true });
        const push = () => {
          if (closed) return;
          if (out.stallAfter != null && i >= out.stallAfter) return; // se tait sans fermer
          if (out.cutAfter != null && i >= out.cutAfter) {
            // connexion coupée en route, comme undici la rapporte
            closed = true;
            if (signal) signal.removeEventListener('abort', onAbort);
            controller.error(new TypeError('terminated', { cause: Object.assign(new Error('other side closed'), { code: 'UND_ERR_SOCKET' }) }));
            return;
          }
          if (i >= out.sse.length) {
            closed = true;
            if (signal) signal.removeEventListener('abort', onAbort);
            controller.close();
            return;
          }
          controller.enqueue(enc.encode(`${out.sse[i++]}\n\n`));
          if (i >= out.sse.length && out.stallAfter == null && out.cutAfter == null) {
            // le dernier événement : le flux se ferme aussitôt, comme celui de la passerelle
            closed = true;
            if (signal) signal.removeEventListener('abort', onAbort);
            controller.close();
            return;
          }
          timer = setTimeout(push, out.delayMs || 0);
        };
        timer = setTimeout(push, out.firstDelayMs ?? out.delayMs ?? 0);
      },
      cancel() {
        if (closed) return;
        closed = true;
        clearTimeout(timer);
        if (signal && onAbort) signal.removeEventListener('abort', onAbort);
        fake.closed += 1;
        fake.emit('closed');
      },
    });
    return new Response(stream, { status: out.status, headers });
  }
  let body = null;
  if (out.json !== undefined && out.json !== null) {
    if (!headers.has('content-type')) headers.set('content-type', 'application/json');
    body = JSON.stringify(out.json);
  } else if (out.json === null && out.text == null) {
    body = null;
  } else if (out.text != null) {
    body = String(out.text);
  }
  if (out.status === 204 || out.status === 304) body = null;
  return new Response(body, { status: out.status, headers });
}

/**
 * Un vrai serveur HTTP sur 127.0.0.1 (port libre), qui sert le faux AIODOS.
 * @returns {Promise<{url: string, port: number, fake: object, close: () => Promise<void>}>}
 */
export async function startFakeServer(fakeOrOptions = {}) {
  const fake = isFake(fakeOrOptions) ? fakeOrOptions : createFake(fakeOrOptions);
  const sockets = new Set();
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const url = new URL(req.url, `http://${req.headers.host || '127.0.0.1'}`);
    let out;
    try {
      out = await fake.handle({ method: req.method, url, headers: lowerHeaders(req.headers), body: chunks.length ? Buffer.concat(chunks).toString('utf8') : null });
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'text/plain' });
      res.end(String(err && err.message));
      return;
    }
    if (out.hang) return; // ne répond jamais : le client finira par fermer
    const headers = { ...(out.headers || {}) };
    if (out.sse) {
      headers['Content-Type'] = headers['Content-Type'] || 'text/event-stream';
      res.writeHead(out.status, headers);
      let i = 0;
      let timer = null;
      let done = false;
      res.on('close', () => {
        clearTimeout(timer);
        if (!done) {
          fake.closed += 1;
          fake.emit('closed');
        }
      });
      const push = () => {
        if (res.destroyed) return;
        if (out.stallAfter != null && i >= out.stallAfter) return;
        if (out.cutAfter != null && i >= out.cutAfter) {
          done = true; // coupé par le serveur, pas fermé par le client
          res.socket?.destroy();
          return;
        }
        if (i >= out.sse.length) {
          done = true;
          res.end();
          return;
        }
        res.write(`${out.sse[i++]}\n\n`);
        if (i >= out.sse.length && out.stallAfter == null && out.cutAfter == null) {
          done = true;
          res.end();
          return;
        }
        timer = setTimeout(push, out.delayMs || 0);
      };
      timer = setTimeout(push, out.firstDelayMs ?? out.delayMs ?? 0);
      return;
    }
    let body = '';
    if (out.json !== undefined && out.json !== null) {
      headers['Content-Type'] = headers['Content-Type'] || 'application/json';
      body = JSON.stringify(out.json);
    } else if (out.text != null) body = String(out.text);
    res.writeHead(out.status, headers);
    res.end(body);
  });
  server.on('connection', (s) => {
    sockets.add(s);
    s.on('close', () => sockets.delete(s));
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const { port } = server.address();
  fake.url = `http://127.0.0.1:${port}`;
  return {
    url: fake.url,
    port,
    fake,
    server,
    close: async () => {
      for (const s of sockets) s.destroy();
      await new Promise((resolve) => server.close(() => resolve()));
    },
  };
}

/**
 * Un faux gateway.json dans un dossier temporaire (jamais le vrai). `state` : open, closed,
 * error, stopped ou headless (discovery.json). Le pid est le nôtre (vivant), sauf `pid`.
 * @returns {Promise<{file: string, dir: string, data: object, env: object, write: (patch: object) => Promise<void>, remove: () => Promise<void>}>}
 */
export async function writeFakeDiscovery({ state = 'open', dir, url, pid, instance, running, gateway } = {}) {
  const files = loadContract('discovery').files;
  if (!files[state]) throw new Error(`faux gateway.json : état « ${state} » inconnu`);
  const where = dir ?? (await mkdtemp(path.join(tmpdir(), 'aiodos-fake-')));
  await mkdir(where, { recursive: true });
  const data = structuredClone(files[state]);
  data.pid = pid ?? process.pid;
  if (instance) data.instance = instance;
  if (running != null) data.running = running;
  if (url) data.gateway.url = url;
  if (gateway) Object.assign(data.gateway, gateway);
  const file = path.join(where, 'gateway.json');
  const write = async (patch = {}) => {
    const { gateway: g, ...rest } = patch;
    Object.assign(data, rest);
    if (g) Object.assign(data.gateway, g);
    await writeFile(file, JSON.stringify(data, null, 2));
  };
  await write();
  return { file, dir: where, data, env: { AIODOS_DISCOVERY_FILE: file }, write, remove: () => rm(where, { recursive: true, force: true }) };
}

/**
 * Un faux AIODOS_HOME (copie de développement) dont l'interpréteur (AIODOS_PYTHON) est un
 * script Node. Il note chaque lancement dans calls.jsonl ; « --app-request » lit la demande
 * sur l'entrée standard et répond la ligne du contrat (helper : 'ok', 'error' ou 'hang') ;
 * « --background » s'arrête aussitôt.
 */
export async function createFakeHome({ helper = 'ok', url, dir } = {}) {
  const d = loadContract('discovery').app_request;
  const where = dir ?? (await mkdtemp(path.join(tmpdir(), 'aiodos-home-')));
  await mkdir(path.join(where, 'aiodos'), { recursive: true });
  await writeFile(path.join(where, 'aiodos', '__init__.py'), '');
  const script = path.join(where, 'fake-python.mjs');
  const ok = { ...d.stdout_ok, ...(url ? { url } : {}) };
  await writeFile(
    script,
    [
      '// faux interpréteur d\'AIODOS pour les essais (écrit par aiodos-client/fake)',
      "import { appendFileSync, readFileSync } from 'node:fs';",
      "import { join } from 'node:path';",
      'const args = process.argv.slice(2);',
      `const dir = ${JSON.stringify(where)};`,
      "const mode = process.env.FAKE_AIODOS_HELPER || " + JSON.stringify(helper) + ';',
      "const request = args.includes('--app-request');",
      "const stdin = request ? readFileSync(0, 'utf8') : '';",
      "appendFileSync(join(dir, 'calls.jsonl'), JSON.stringify({ args, cwd: process.cwd(), stdin }) + '\\n');",
      'if (request) {',
      "  if (mode === 'hang') { setInterval(() => {}, 1000); }",
      "  else if (mode === 'error') { process.stdout.write(" + JSON.stringify(JSON.stringify(d.stdout_error)) + " + '\\n'); process.exitCode = " + d.exit_error + '; }',
      "  else { process.stdout.write(" + JSON.stringify(JSON.stringify(ok)) + " + '\\n'); process.exitCode = " + d.exit_ok + '; }',
      '}',
      '',
    ].join('\n'),
  );
  const callsFile = path.join(where, 'calls.jsonl');
  return {
    dir: where,
    script,
    env: { AIODOS_HOME: where, AIODOS_PYTHON: script },
    stdoutOk: ok,
    calls: async () => {
      try {
        return (await readFile(callsFile, 'utf8'))
          .split('\n')
          .filter(Boolean)
          .map((l) => JSON.parse(l));
      } catch {
        return [];
      }
    },
    remove: () => rm(where, { recursive: true, force: true }),
  };
}

/**
 * Un client branché sur un faux fetch : pas de fichier de découverte (AIODOS_DISCOVERY_FILE
 * vaut « - »), pas d'installation (AIODOS_HOME vide), la clé d'essai du contrat déjà rangée
 * (key: null pour partir sans clé ; key: { get, set } pour donner son propre coffre).
 * @returns {{client: object, fake: object, fetch: Function, keyStore: object}}
 */
export function createFakeClient({ fake: fakeOptions = {}, key = FAKE_KEY, env = {}, ...clientOptions } = {}) {
  const fetch = createFakeFetch(fakeOptions);
  const keyStore = key && typeof key === 'object' && typeof key.get === 'function' ? key : memoryKeyStore(key);
  const client = createClient({
    app: 'ARKLIGHT PC',
    appId: 'arkane.arklight',
    version: '0.0.0-essai',
    key: keyStore,
    url: fetch.fake.url,
    fetch,
    env: { AIODOS_DISCOVERY_FILE: '-', AIODOS_HOME: path.join(tmpdir(), 'aiodos-client-aucune-installation'), ...env },
    ...clientOptions,
  });
  return { client, fake: fetch.fake, fetch, keyStore };
}
