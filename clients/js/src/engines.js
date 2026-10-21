// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — les moteurs : GET /aiodos/v1/engines (docs/api-applications.md § 5.7),
// selon les cercles de l'appli : qui a ComfyUI, sa version, sa mémoire vidéo, ses modèles,
// l'object_info disponible. Rendus en camelCase (vram_mib → vramMib, object_info → objectInfo).
// Rejette NotSupported tant que `features` n'annonce pas « engines ».

import { clientError } from './errors.js';
import { camelize } from './util.js';

const ENGINES_TIMEOUT = 30_000;

/** La liste des moteurs : [{ id, label, here, machines, models, objectInfo, recipes }]. */
export async function engines(ctx, { signal } = {}) {
  await ctx.session.requireFeature('engines', { signal });
  const res = await ctx.session.keyed({ path: '/aiodos/v1/engines', signal, timeout: ENGINES_TIMEOUT, feature: 'engines' });
  const d = ctx.transport.expectOk(res);
  if (!d || !Array.isArray(d.engines)) throw clientError('protocol', ctx.lang());
  return d.engines.filter((e) => e && typeof e === 'object' && typeof e.id === 'string').map((e) => camelize(e));
}
