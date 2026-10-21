// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — ouvrir une page d'AIODOS : POST /aiodos/v1/open
// (docs/api-applications.md § 5.3). Par exemple « L'installer dans AIODOS » : l'utilisateur
// choisit et installe le modèle dans AIODOS, où la licence est affichée ; rien n'est
// jamais téléchargé par l'appli.

import { makeError } from './errors.js';

const PAGES = new Set(['applications', 'models', 'jobs', 'gallery']);

/** Ramène la fenêtre d'AIODOS sur une page. Rend { opened } (faux sans fenêtre). */
export async function openInAiodos(ctx, { page = 'applications', model = null, signal } = {}) {
  const lang = ctx.lang();
  if (!PAGES.has(page)) {
    throw makeError('invalid_request', lang === 'en' ? 'Unknown page (applications, models, jobs, gallery).' : 'Page inconnue (applications, models, jobs, gallery).', { param: 'page' });
  }
  const res = await ctx.session.keyed({
    method: 'POST',
    path: '/aiodos/v1/open',
    json: { page, model: model ?? null },
    signal,
    timeout: 10_000,
    feature: 'open',
  });
  const d = ctx.transport.expectOk(res);
  return { opened: Boolean(d && d.opened) };
}
