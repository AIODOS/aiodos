// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — les embeddings : POST /v1/embeddings, sur ce PC seulement
// (docs/api-applications.md § 5.6).

import { clientError, makeError } from './errors.js';
import { models } from './models.js';

/** Un vecteur : une liste de nombres, ou du base64 (float32 petit-boutiste). */
function vectorOf(e, lang) {
  if (Array.isArray(e)) return e.map(Number);
  if (typeof e === 'string') {
    const b = Buffer.from(e, 'base64');
    if (b.length % 4) throw clientError('protocol', lang);
    const out = new Array(b.length / 4);
    for (let i = 0; i < out.length; i++) out[i] = b.readFloatLE(i * 4);
    return out;
  }
  throw clientError('protocol', lang);
}

/** Le modèle d'une appli sans préférence : celui d'AIODOS (`default`) s'il est sur ce PC,
 * sinon le premier d'ici ; null s'il n'y en a pas. */
async function choice(ctx, signal, without = null) {
  const list = (await models(ctx, { kind: 'embed', signal })).filter((m) => m.id !== without);
  const here = list.filter((m) => m.here && m.installed);
  return here.find((m) => m.default) ?? here[0] ?? list[0] ?? null;
}

/**
 * Les vecteurs d'un ou plusieurs textes. Sans `model`, celui qu'AIODOS prend pour ses
 * bibliothèques s'il est sur ce PC (`default`), sinon le premier d'ici : des vecteurs d'un même
 * modèle d'un appel à l'autre. Rend { embeddings: number[][], model, dimensions, usage } :
 * garder `model` avec les vecteurs, ceux de deux modèles ne se comparent pas.
 */
export async function embed(ctx, { input, model, signal, timeout = ctx.timeout } = {}) {
  const lang = ctx.lang();
  const single = typeof input === 'string';
  const texts = single ? [input] : Array.isArray(input) ? input : null;
  if (!texts || !texts.length || texts.some((t) => typeof t !== 'string')) {
    throw makeError('invalid_request', lang === 'en' ? 'input: a text, or a list of texts.' : 'input : un texte, ou une liste de textes.', { param: 'input' });
  }
  const send = async (name) => {
    const res = await ctx.session.keyed({
      method: 'POST',
      path: '/v1/embeddings',
      json: { model: name, input: single ? input : texts },
      signal,
      timeout,
      feature: 'embed',
    });
    return ctx.transport.expectOk(res);
  };
  let name = typeof model === 'string' && model ? model : null;
  let d;
  if (name) {
    d = await send(name);
  } else {
    const chosen = await choice(ctx, signal);
    if (!chosen) {
      throw makeError('model_not_found', lang === 'en' ? 'No embedding model on this PC.' : "Aucun modèle d'embedding sur ce PC.", { param: 'model' });
    }
    name = chosen.id;
    try {
      d = await send(name);
    } catch (e) {
      // AIODOS vient d'écarter ce modèle (un format que son moteur ne charge pas) : le
      // suivant, une fois. Un modèle demandé par l'appli, lui, n'est jamais remplacé.
      const other = e && e.code === 'model_not_found' ? await choice(ctx, signal, name) : null;
      if (!other) throw e;
      name = other.id;
      d = await send(name);
    }
  }
  if (!d || !Array.isArray(d.data)) throw clientError('protocol', lang);
  const rows = [...d.data].sort((a, b) => (a && a.index ? a.index : 0) - (b && b.index ? b.index : 0));
  const embeddings = rows.map((row) => vectorOf(row && row.embedding, lang));
  const u = d.usage && typeof d.usage === 'object' ? d.usage : null;
  return {
    embeddings,
    model: typeof d.model === 'string' ? d.model : name,
    dimensions: embeddings.length ? embeddings[0].length : null,
    usage: u ? { promptTokens: u.prompt_tokens ?? null, totalTokens: u.total_tokens ?? null } : null,
  };
}
