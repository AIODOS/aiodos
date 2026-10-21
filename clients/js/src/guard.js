// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — le refus de tourner dans une page (navigateur, rendu d'Electron).
//
// La passerelle d'AIODOS refuse les pages web, et la clé d'une appli ne doit jamais vivre
// dans un rendu. Le client vit dans Node ou dans le processus principal d'Electron.

const MESSAGE =
  "aiodos-client tourne dans Node ou dans le processus principal d'Electron, jamais dans une page " +
  "(navigateur ou rendu d'Electron) : la passerelle d'AIODOS refuse les pages web et la clé ne doit pas " +
  "y vivre. Appelle-le depuis le serveur Node ou le processus principal (ipcMain). " +
  "(aiodos-client runs in Node or in Electron's main process, never in a page or renderer.)";

/**
 * Dit pourquoi le client ne doit pas tourner ici, ou null.
 * @param {object} [g] l'objet global à examiner (les essais en donnent un faux)
 */
export function rendererProblem(g = globalThis) {
  const proc = g.process;
  const hasNode = Boolean(proc && proc.versions && proc.versions.node);
  const hasDom = typeof g.window !== 'undefined' || typeof g.document !== 'undefined';
  if (hasDom && !hasNode) return MESSAGE;
  // rendu d'Electron, même avec nodeIntegration (process.type vaut alors « renderer »)
  if (proc && proc.type === 'renderer') return MESSAGE;
  return null;
}

/** Lève une erreur claire si l'on est dans une page. */
export function assertNotRenderer(g = globalThis) {
  const problem = rendererProblem(g);
  if (problem) throw new Error(problem);
}

// dès le chargement du module : index.js l'importe en premier
assertNotRenderer();
