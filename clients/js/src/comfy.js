// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — ComfyUI par AIODOS (docs/api-applications.md § 5.7) : le pré-vol
// (plan), les jetons d'entrée (tokenize), le dépôt d'un travail (submit), et run(), qui dépose,
// attend et télécharge.
//
// AIODOS ne remplace pas le ComfyUI de l'appli : il rejoue ses graphes, au format API et déjà
// remplis, dans le ComfyUI accordé de ce PC (puis de mes machines), et garde les fichiers pour
// elle. Chaque fonction rejette NotSupported tant que `features` n'annonce pas
// « engines.comfyui » (puis « jobs », « inputs », « files » selon ce qu'elle touche), sans rien
// essayer d'autre. objectInfo et template (relais, L2c), recipes, recipeFor et
// submit({ recipe }) (L4) rejettent NotSupported : AIODOS ne les sert pas encore.

import { clientError, makeError } from './errors.js';
import { prepareInputs } from './inputs.js';
import { Job } from './job.js';
import { jobBody, placeHeaders } from './jobspec.js';
import { codeText } from './texts.js';
import { tokenizeGraph } from './tokenize.js';
import { camelize, notYet } from './util.js';

const CANCEL_TIMEOUT = 10_000;
// les fonctions pas encore servies, sous le nom qu'AIODOS leur donnera dans `features`
const LATER = { objectInfo: 'engines.comfyui.object_info', template: 'engines.comfyui.templates', recipes: 'recipes' };

/** Les entrées envoyées (celles qui manquent), puis le corps vers `path` ; rend la réponse. */
async function deposit(ctx, path, o, features) {
  const lang = ctx.lang();
  await ctx.session.requireFeature(features, { signal: o.signal });
  const body = jobBody(o, lang); // vérifié avant tout envoi
  const headers = await placeHeaders(ctx, o, lang);
  const inputs = await prepareInputs(ctx, o.inputs, { signal: o.signal });
  if (inputs) body.inputs = inputs;
  const res = await ctx.session.keyed({ method: 'POST', path, json: body, headers, signal: o.signal, timeout: ctx.timeout, feature: features });
  return ctx.transport.expectOk(res);
}

/** Pré-vol : le corps d'un travail, vérifié comme au dépôt ; rien n'est mis en file. */
export async function plan(ctx, o = {}) {
  await ctx.session.requireFeature('engines.comfyui', { signal: o.signal });
  if (o.recipe != null) return notYet(ctx, { needs: 'engines.comfyui', feature: LATER.recipes, signal: o.signal });
  const d = await deposit(ctx, '/aiodos/v1/engines/comfyui/plan', o, ['engines.comfyui']);
  if (!d || typeof d !== 'object' || typeof d.ok !== 'boolean') throw clientError('protocol', ctx.lang());
  return camelize(d);
}

/**
 * Dépose un travail : les entrées hachées, celles qui manquent envoyées, puis le travail.
 * Le même clientRef rend le même travail. Rend un Job.
 */
export async function submit(ctx, o = {}) {
  await ctx.session.requireFeature('engines.comfyui', { signal: o.signal });
  if (o.recipe != null) return notYet(ctx, { needs: 'engines.comfyui', feature: LATER.recipes, signal: o.signal });
  return new Job(ctx, await deposit(ctx, '/aiodos/v1/jobs', o, ['engines.comfyui', 'jobs']));
}

/** L'erreur d'un travail fini sans résultat (run). */
function jobFailure(job, lang) {
  const units = job.units.filter((u) => u && u.error).map((u) => ({ index: u.index ?? null, label: u.label ?? null, error: u.error }));
  const detail = { job: job.id, status: job.status, error: job.error, units };
  if (job.status === 'cancelled') return clientError('job_failed', lang, { textKey: 'job_cancelled', detail });
  const reason =
    (job.error && job.error.message) || (units[0] && units[0].error.message) || (lang === 'en' ? 'no unit could be done.' : "aucune unité n'a pu se faire.");
  return clientError('job_failed', lang, { vars: { reason }, detail });
}

/**
 * Dépose, attend la fin, télécharge dans `destDir` : rend les chemins des fichiers.
 * `onEvent(ev)` reçoit chaque événement du suivi. Arrêté par `signal`, le travail est aussi
 * annulé dans AIODOS. Un travail en échec, ou annulé ailleurs : JobFailed.
 */
export async function run(ctx, o = {}) {
  const lang = ctx.lang();
  const { destDir, stem, onFile, onEvent, timeout, ...rest } = o;
  const { signal } = o;
  await ctx.session.requireFeature('engines.comfyui', { signal });
  if (o.recipe != null) return notYet(ctx, { needs: 'engines.comfyui', feature: LATER.recipes, signal });
  await ctx.session.requireFeature(['jobs', 'files'], { signal });
  if (!(typeof destDir === 'string' && destDir) && !(destDir instanceof URL)) {
    throw makeError('invalid_request', lang === 'en' ? 'destDir: the folder for the files.' : 'destDir : le dossier où ranger les fichiers.', { param: 'destDir' });
  }
  const job = await submit(ctx, rest);
  let end = null;
  try {
    for await (const ev of job.watch({ signal, timeout })) {
      if (typeof onEvent === 'function') await onEvent(ev);
      if (ev.type === 'end') {
        end = ev.status;
        break;
      }
    }
  } catch (err) {
    if (signal && signal.aborted) {
      // l'appli ne veut plus du résultat : le calcul s'arrête aussi dans AIODOS
      try {
        await job.cancel({ signal: AbortSignal.timeout(CANCEL_TIMEOUT) });
      } catch {
        /* déjà fini, ou AIODOS injoignable : il reste dans la file de l'appli */
      }
    }
    throw err;
  }
  if (end === 'deleted') throw makeError('job_not_found', codeText('job_not_found', lang), { detail: { job: job.id } });
  if (job.status === 'failed' || job.status === 'cancelled') throw jobFailure(job, lang);
  return job.download(destDir, { stem, onFile, signal });
}

export function createComfy(ctx) {
  const later = (feature, o) => notYet(ctx, { needs: 'engines.comfyui', feature, signal: o && o.signal });
  return Object.freeze({
    /** Pré-vol d'un travail ComfyUI, sans rien mettre en file. */
    plan: (o) => plan(ctx, o),
    /** L'object_info relayé (L2c) : pas encore servi. */
    objectInfo: async (cls = null, o = {}) => later(LATER.objectInfo, o),
    /** Un gabarit relayé (L2c) : pas encore servi. */
    template: async (name, o = {}) => later(LATER.template, o),
    /** Les recettes servies aux inconnus (L4) : pas encore servies. */
    recipes: async (o = {}) => later(LATER.recipes, o),
    /** La recette qui correspond à un gabarit (L4) : pas encore servie. */
    recipeFor: async (template, o = {}) => later(LATER.recipes, o),
    /** Met les jetons @in: dans les nœuds de chargement donnés ; rend { graph, inputs }. */
    tokenize: async (graph, files, o = {}) => {
      await ctx.session.requireFeature('engines.comfyui', { signal: o && o.signal });
      return tokenizeGraph(graph, files, ctx.lang());
    },
    /** Envoie les entrées absentes, puis le travail ; rend un Job. */
    submit: (o) => submit(ctx, o),
    /** submit, puis l'attente de la fin, puis download : rend les chemins. */
    run: (o) => run(ctx, o),
  });
}
