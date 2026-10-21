// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — le catalogue : GET /aiodos/v1/models?kind=chat|embed
// (docs/api-applications.md § 5.2), rendu au format qu'ARKLIGHT attend déjà
// ({ id, size, caps, params }), plus machine, context, loaded, here, default, where.
// `default` : le modèle qu'AIODOS prend lui-même (ses conversations, ses bibliothèques).

import { clientError, makeError } from './errors.js';

const MODELS_TIMEOUT = 15_000;
const KINDS = new Set(['chat', 'embed']);

/** « 9B » pour « qwen3.5:9b », « 35B » pour « qwen3.6:35b-a3b », sinon ''. */
export function paramsOf(id) {
  const s = String(id || '').toLowerCase();
  const tag = s.includes(':') ? s.slice(s.lastIndexOf(':') + 1) : '';
  const m = /^(\d+(?:\.\d+)?)([bm])(?![a-z])/.exec(tag) || /(?:^|[-_:])(\d+(?:\.\d+)?)([bm])(?=$|[-_:])/.exec(s);
  return m ? `${m[1]}${m[2].toUpperCase()}` : '';
}

/** Les capacités, avec les noms d'Ollama : completion, tools, vision, thinking, embedding. */
export function capsOf(m) {
  if (m.kind === 'embed') return ['embedding'];
  const caps = ['completion'];
  if (m.tools) caps.push('tools');
  if (m.vision) caps.push('vision');
  if (m.thinking) caps.push('thinking');
  return caps;
}

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const bool = (v) => (typeof v === 'boolean' ? v : null);
const str = (v) => (typeof v === 'string' ? v : null);

/** Une fiche du catalogue d'AIODOS, au format du client. */
export function toModel(m) {
  const where = (Array.isArray(m.where) ? m.where : [])
    .filter((w) => w && typeof w === 'object' && typeof w.id === 'string')
    .map((w) => ({
      id: w.id,
      machine: str(w.machine),
      place: str(w.place),
      loaded: Boolean(w.loaded),
      context: num(w.context),
      tools: bool(w.tools),
      vision: bool(w.vision),
    }));
  const main = where.find((w) => w.place === 'here') ?? where[0] ?? null;
  const lic = m.license && typeof m.license === 'object' ? { id: str(m.license.id), restrictive: Boolean(m.license.restrictive) } : null;
  return {
    id: m.id,
    label: str(m.label) ?? m.id,
    kind: str(m.kind),
    size: num(m.size_bytes) ?? 0,
    caps: capsOf(m),
    params: paramsOf(m.id),
    machine: main ? main.machine : null,
    context: num(m.context),
    loaded: Boolean(m.loaded),
    here: Boolean(m.here),
    default: m.default === true,
    installed: m.installed !== false,
    tools: Boolean(m.tools),
    vision: Boolean(m.vision),
    thinking: Boolean(m.thinking),
    dimensions: num(m.dimensions),
    license: lic,
    where,
  };
}

/**
 * Les modèles permis à l'appli, d'un genre (chat par défaut, ou embed).
 * `use` est accepté comme synonyme de `kind`.
 */
export async function models(ctx, { kind, use, signal, timeout = MODELS_TIMEOUT } = {}) {
  const lang = ctx.lang();
  const k = kind ?? use ?? 'chat';
  if (!KINDS.has(k)) {
    throw makeError('invalid_request', lang === 'en' ? 'Unknown kind of model (chat, embed).' : 'Genre de modèle inconnu (chat, embed).', { param: 'kind' });
  }
  const res = await ctx.session.keyed({ path: `/aiodos/v1/models?kind=${k}`, signal, timeout, feature: 'models' });
  const d = ctx.transport.expectOk(res);
  if (!d || !Array.isArray(d.models)) throw clientError('protocol', lang);
  const out = d.models.filter((m) => m && typeof m === 'object' && typeof m.id === 'string').map(toModel);
  // « chargé ou non », pour le délai de chargement de la conversation
  const now = Date.now();
  for (const m of out) {
    ctx.loaded.set(m.id, { loaded: m.loaded, at: now });
    for (const w of m.where) ctx.loaded.set(w.id, { loaded: w.loaded, at: now });
  }
  return out;
}
