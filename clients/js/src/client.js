// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — createClient() : le client d'une appli, sans état global.
//
// La clé est lue par key.get() à chaque appel et rangée par key.set() (ARKLIGHT la garde
// par safeStorage d'Electron) ; le client ne la garde jamais, ne la met dans aucun journal,
// aucun message, aucun toString ni JSON.

import { connect, disconnect, requestAccess } from './access.js';
import { chat } from './chat.js';
import { createComfy, engines } from './comfy.js';
import { discoveryUrl, launchAiodos, readDiscovery } from './discovery.js';
import { embed } from './embed.js';
import { redact, redactDeep } from './errors.js';
import { assertNotRenderer } from './guard.js';
import { createInputs } from './inputs.js';
import { createJobs } from './jobs.js';
import { models } from './models.js';
import { openInAiodos } from './open.js';
import { createSession } from './session.js';
import { status, waitReady } from './status.js';
import { normalizeLang } from './texts.js';
import { DEFAULT_URL, createTransport, localOrigin } from './transport.js';
import { VERSION } from './version.js';

const INSPECT = Symbol.for('nodejs.util.inspect.custom');

/**
 * Un coffre en mémoire, pour les essais et les scripts : la clé est perdue à la fin du
 * programme. Une vraie appli donne le sien (safeStorage d'Electron, DPAPI…).
 */
export function memoryKeyStore(initial = null) {
  let value = initial;
  return Object.freeze({
    get: () => value,
    set: (k) => {
      value = k ?? null;
    },
    toJSON: () => ({ store: 'memory' }),
    toString: () => '[coffre en mémoire]',
    [INSPECT]: () => '[coffre en mémoire]',
  });
}

function positive(v, fallback) {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : fallback;
}

function makeLog(fn) {
  return (level, message, data) => {
    if (typeof fn !== 'function') return;
    try {
      fn(level, redact(String(message)), data === undefined ? undefined : redactDeep(data));
    } catch {
      /* le journal de l'appli ne doit jamais casser un appel */
    }
  };
}

/**
 * Le client.
 * @param {object} [options]
 * @param {string} options.app        nom montré dans AIODOS (1 à 40 caractères)
 * @param {string} options.appId      identifiant stable ([a-z0-9.-], 2 à 40)
 * @param {string} [options.version]  version de l'appli
 * @param {{get: () => any, set: (key: string|null) => any}} [options.key]  le coffre de l'appli
 * @param {string|(() => string)} [options.lang]  « fr » (défaut) ou « en »
 * @param {string|(() => string)} [options.url]   adresse de la passerelle (locale seulement)
 * @param {Function} [options.fetch]  un fetch à la place de globalThis.fetch (essais)
 * @param {number} [options.timeout]     délai d'inactivité (ms), 240 000 par défaut
 * @param {number} [options.loadTimeout] délai de chargement d'un modèle (ms), 900 000 par défaut
 * @param {(level: string, message: string, data?: object) => void} [options.log]
 * @param {object} [options.env]       variables d'environnement (essais), process.env par défaut
 * @param {string} [options.platform]  plateforme (essais), process.platform par défaut
 */
export function createClient(options = {}) {
  assertNotRenderer();
  const o = options && typeof options === 'object' ? options : {};
  let keyStore = o.key;
  if (keyStore == null) keyStore = memoryKeyStore();
  if (typeof keyStore !== 'object' || typeof keyStore.get !== 'function' || typeof keyStore.set !== 'function') {
    throw new TypeError('aiodos-client : « key » doit être un coffre { get(), set(clé) }');
  }
  if (o.fetch != null && typeof o.fetch !== 'function') throw new TypeError('aiodos-client : « fetch » doit être une fonction');
  if (o.log != null && typeof o.log !== 'function') throw new TypeError('aiodos-client : « log » doit être une fonction');

  const ctx = {
    app: o.app ?? null,
    appId: o.appId ?? null,
    version: o.version ?? null,
    keyStore: {
      get: async () => keyStore.get(),
      set: async (k) => keyStore.set(k),
    },
    env: o.env && typeof o.env === 'object' ? o.env : process.env,
    platform: typeof o.platform === 'string' ? o.platform : process.platform,
    timeout: positive(o.timeout, 240_000),
    loadTimeout: positive(o.loadTimeout, 900_000),
    access: { pending: null, denied: false },
    loaded: new Map(),
    lang: () => {
      try {
        return normalizeLang(typeof o.lang === 'function' ? o.lang() : o.lang);
      } catch {
        return 'fr';
      }
    },
    getFetch: () => o.fetch ?? globalThis.fetch,
    log: makeLog(o.log),
  };
  ctx.transport = createTransport(ctx);
  ctx.session = createSession(ctx);
  // après une erreur de connexion, la preuve d'identité est refaite avant d'envoyer la clé
  ctx.connectionLost = () => ctx.session.forget();
  /** L'adresse : le paramètre de l'appli, AIODOS_URL, le fichier de découverte, le défaut. */
  ctx.resolveOrigin = async () => {
    const lang = ctx.lang();
    const given = typeof o.url === 'function' ? await o.url() : o.url;
    if (given) return { origin: localOrigin(given, lang), source: 'option' };
    if (ctx.env.AIODOS_URL) return { origin: localOrigin(ctx.env.AIODOS_URL, lang), source: 'env' };
    const found = discoveryUrl(await readDiscovery({ env: ctx.env, platform: ctx.platform }));
    if (found) return { origin: found, source: 'discovery' };
    return { origin: DEFAULT_URL, source: 'default' };
  };

  const jobs = createJobs(ctx);
  const describe = () => `[aiodos-client ${VERSION}${ctx.app ? ` · ${ctx.app}` : ''}]`;

  return Object.freeze({
    /** L'état d'AIODOS pour cette appli ; ne rejette jamais. */
    status: (opts) => status(ctx, opts),
    /** Attend qu'AIODOS réponde (ou soit prêt, until: 'ready'). */
    waitReady: (opts) => waitReady(ctx, opts),
    /** Lance AIODOS en arrière-plan, sur un clic de l'utilisateur. */
    launch: () => launchAiodos({ env: ctx.env, platform: ctx.platform, lang: ctx.lang() }),
    /** Se connecter : demande, code, relève, clé rangée. Sur un clic de l'utilisateur. */
    connect: (opts) => connect(ctx, opts),
    /** La demande d'accès par la passerelle ouverte (en dessous de connect). */
    requestAccess: (opts) => requestAccess(ctx, opts),
    /** Se déconnecter : retrait dans AIODOS, puis key.set(null). */
    disconnect: (opts) => disconnect(ctx, opts),
    /** GET /aiodos/v1/info sans clé. */
    info: async ({ signal } = {}) => ctx.session.getInfo((await ctx.resolveOrigin()).origin, { signal, fresh: true }),
    /** Les modèles permis à l'appli (kind: 'chat' ou 'embed'). */
    models: (opts) => models(ctx, opts),
    /** Une conversation, toujours en flux. */
    chat: (opts) => chat(ctx, opts),
    /** Des embeddings, sur ce PC. */
    embed: (opts) => embed(ctx, opts),
    /** Ramène la fenêtre d'AIODOS sur une page (« L'installer dans AIODOS »). */
    openInAiodos: (opts) => openInAiodos(ctx, opts),
    /** Les moteurs (L2). */
    engines: (opts) => engines(ctx, opts),
    /** ComfyUI (L2). */
    comfy: createComfy(ctx),
    /** Les travaux (L2). */
    jobs: jobs.jobs,
    /** Un travail (L2). */
    job: jobs.job,
    /** Les entrées des travaux (L2). */
    inputs: createInputs(ctx),
    toJSON: () => ({ client: 'aiodos-client', version: VERSION, app: ctx.app }),
    toString: describe,
    [INSPECT]: describe,
  });
}
