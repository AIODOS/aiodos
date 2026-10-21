// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — les embeddings : POST /v1/embeddings, sur ce PC seulement
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

/**
 * Les vecteurs d'un ou plusieurs textes. Sans `model`, le premier modèle d'embedding de ce PC.
 * Rend { embeddings: number[][], model, dimensions, usage }.
 */
export async function embed(ctx, { input, model, signal, timeout = ctx.timeout } = {}) {
  const lang = ctx.lang();
  const single = typeof input === 'string';
  const texts = single ? [input] : Array.isArray(input) ? input : null;
  if (!texts || !texts.length || texts.some((t) => typeof t !== 'string')) {
    throw makeError('invalid_request', lang === 'en' ? 'input: a text, or a list of texts.' : 'input : un texte, ou une liste de textes.', { param: 'input' });
  }
  let name = typeof model === 'string' && model ? model : null;
  if (!name) {
    const list = await models(ctx, { kind: 'embed', signal });
    const chosen = list.find((m) => m.here && m.installed) ?? list[0];
    if (!chosen) {
      throw makeError('model_not_found', lang === 'en' ? 'No embedding model on this PC.' : "Aucun modèle d'embedding sur ce PC.", { param: 'model' });
    }
    name = chosen.id;
  }
  const res = await ctx.session.keyed({
    method: 'POST',
    path: '/v1/embeddings',
    json: { model: name, input: single ? input : texts },
    signal,
    timeout,
    feature: 'embed',
  });
  const d = ctx.transport.expectOk(res);
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
