// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — un travail ComfyUI (docs/api-applications.md § 5.7 ; conception,
// partage-puissance.md § 2.5) : l'objet travail en camelCase, et ce qu'on en fait.
//
// - les champs (id, status, label, clientRef, progress, units, error, cancelledBy…) suivent
//   le dernier état reçu (refresh, commandes, watch) ; `state` le donne en entier, avec ses
//   fichiers (state.files) et sa pause (state.pause) : files() et pause() sont des commandes ;
// - refresh(), watch() (les événements, en itérateur asynchrone), wait() ;
// - les commandes : pause (après l'unité en cours, par défaut, ou tout de suite), resume,
//   cancel (ce calcul seulement, jamais ceux de l'utilisateur ; les fichiers rendus restent),
//   retryFailed, delete ;
// - files({ unfetched }) et download(dossier) : les fichiers, repris et vérifiés.
// Une appli n'agit que sur ses propres travaux : celui d'une autre est introuvable.

import { clientError, makeError } from './errors.js';
import { downloadJob } from './download.js';
import { APPLY as APPLY_FILE, JobFile } from './file.js';
import { camelize } from './util.js';
import { watchJob } from './watch.js';

const INSPECT = Symbol.for('nodejs.util.inspect.custom');
const APPLY = Symbol('aiodos.job.apply');
const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
const PAUSE_MODES = new Set(['after_current', 'now']);
const COMMAND_TIMEOUT = 30_000;

/** Le chemin d'un travail, l'identifiant vérifié avant tout envoi. */
export function jobPath(id, lang = 'fr', suffix = '') {
  if (typeof id !== 'string' || !id || id.length > 200) {
    throw makeError('invalid_request', lang === 'en' ? 'Invalid job id.' : 'Identifiant de travail invalide.', { param: 'id' });
  }
  return `/aiodos/v1/jobs/${encodeURIComponent(id)}${suffix}`;
}

const isCard = (f) => f && typeof f === 'object' && typeof f.id === 'string';

/** Un travail de l'appli, tel qu'AIODOS le décrit. */
export class Job {
  #ctx;
  #state = null;
  #files = new Map(); // id → JobFile : un même fichier reste le même objet
  #deleted = false;

  constructor(ctx, data) {
    this.#ctx = ctx;
    this[APPLY](data);
  }

  /** (interne) Le nouvel état reçu d'AIODOS. */
  [APPLY](data) {
    if (!data || typeof data !== 'object' || typeof data.id !== 'string' || (this.#state && data.id !== this.#state.id)) {
      throw clientError('protocol', this.#ctx.lang());
    }
    const { files, ...rest } = data;
    const state = camelize(rest);
    state.files = (Array.isArray(files) ? files : []).filter(isCard).map((f) => this.#file(f));
    this.#state = state;
    return this;
  }

  #file(raw) {
    let f = this.#files.get(raw.id);
    if (f) f[APPLY_FILE](raw);
    else {
      f = new JobFile(this.#ctx, raw);
      this.#files.set(raw.id, f);
    }
    return f;
  }

  get id() {
    return this.#state.id;
  }
  get engine() {
    return this.#state.engine ?? null;
  }
  get label() {
    return this.#state.label ?? null;
  }
  get clientRef() {
    return this.#state.clientRef ?? null;
  }
  /** queued, in_progress, pausing, paused, cancelling, completed, failed ou cancelled. */
  get status() {
    return this.#state.status ?? null;
  }
  get priority() {
    return this.#state.priority ?? null;
  }
  get where() {
    return this.#state.where ?? null;
  }
  get whereEffective() {
    return this.#state.whereEffective ?? [];
  }
  get whereReason() {
    return this.#state.whereReason ?? null;
  }
  /** { total, done, running, queued, failed, percent, estimated, elapsedS, etaS, stage, machines, message } */
  get progress() {
    return this.#state.progress ?? null;
  }
  get waiting() {
    return this.#state.waiting ?? null;
  }
  /** [{ index, label, status, place, machine, files, seconds, stage, error }] */
  get units() {
    return this.#state.units ?? [];
  }
  get error() {
    return this.#state.error ?? null;
  }
  /** « app » (cette appli), ou qui l'a annulé dans AIODOS ; null sinon. */
  get cancelledBy() {
    return this.#state.cancelledBy ?? null;
  }
  get createdAt() {
    return this.#state.createdAt ?? null;
  }
  get startedAt() {
    return this.#state.startedAt ?? null;
  }
  get completedAt() {
    return this.#state.completedAt ?? null;
  }
  /** Tout le dernier état reçu, fichiers (JobFile) et pause ({ when, pending }) compris. */
  get state() {
    return this.#state;
  }
  /** Fini : completed, failed ou cancelled. */
  get done() {
    return TERMINAL.has(this.status);
  }
  /** Oublié par delete(). */
  get deleted() {
    return this.#deleted;
  }

  async #call(o, features) {
    const ctx = this.#ctx;
    await ctx.session.requireFeature(features, { signal: o.signal });
    const res = await ctx.session.keyed({ timeout: COMMAND_TIMEOUT, ...o, feature: features });
    return ctx.transport.expectOk(res);
  }

  async #command(name, { json, signal }, features = ['jobs']) {
    const d = await this.#call({ method: 'POST', path: jobPath(this.id, this.#ctx.lang(), `/${name}`), json, signal }, features);
    return this[APPLY](d);
  }

  /** Relit l'état (GET /aiodos/v1/jobs/{id}). */
  async refresh({ signal } = {}) {
    return this[APPLY](await this.#call({ path: jobPath(this.id, this.#ctx.lang()), signal }, ['jobs']));
  }

  /**
   * Les événements du travail, en itérateur asynchrone : for await (const ev of job.watch()).
   * Reconnexion automatique ; `signal` ferme vraiment la connexion ; `timeout` (ms) borne
   * toute l'attente.
   */
  watch(options = {}) {
    const hands = { path: jobPath(this.id, this.#ctx.lang(), '/events'), apply: (d) => this[APPLY](d), file: (raw) => this.#file(raw) };
    return watchJob(this.#ctx, this, hands, options);
  }

  /** Attend la fin du travail (par ses événements) ; rend le travail, quel que soit son état final. */
  async wait(options = {}) {
    for await (const ev of this.watch(options)) if (ev.type === 'end') break;
    return this;
  }

  /** Pause : après l'unité en cours (rien ne se perd, par défaut), ou tout de suite (« now »). */
  async pause({ when = 'after_current', signal } = {}) {
    await this.#ctx.session.requireFeature(['jobs', 'jobs.pause'], { signal });
    if (!PAUSE_MODES.has(when)) {
      throw makeError('invalid_request', this.#ctx.lang() === 'en' ? '“when”: after_current or now.' : '« when » : after_current ou now.', { param: 'when' });
    }
    return this.#command('pause', { json: { when }, signal }, ['jobs', 'jobs.pause']);
  }

  /** Reprend un travail en pause. */
  async resume({ signal } = {}) {
    return this.#command('resume', { signal }, ['jobs', 'jobs.pause']);
  }

  /** Annule : le calcul en cours s'arrête dans ComfyUI ; les fichiers déjà rendus restent. */
  async cancel({ signal } = {}) {
    return this.#command('cancel', { signal });
  }

  /** Relance les unités en échec. */
  async retryFailed({ signal } = {}) {
    return this.#command('retry', { signal });
  }

  /**
   * Oublie un travail fini (un travail en cours s'annule d'abord) ; `files: true` efface aussi
   * ses fichiers, sinon ils suivent leur garde.
   */
  async delete({ files = false, signal } = {}) {
    const mode = files === true || files === 'delete' ? 'delete' : 'keep';
    const d = await this.#call({ method: 'DELETE', path: jobPath(this.id, this.#ctx.lang(), `?files=${mode}`), signal }, ['jobs']);
    this.#deleted = true;
    return { id: this.id, deleted: !(d && d.deleted === false) };
  }

  /** Ses fichiers (GET …/files) : ceux pas encore récupérés (par défaut), ou tous (unfetched: false). */
  async files({ unfetched = true, signal } = {}) {
    const d = await this.#call({ path: jobPath(this.id, this.#ctx.lang(), unfetched ? '/files?unfetched=1' : '/files'), signal }, ['jobs', 'files']);
    if (!d || !Array.isArray(d.files)) throw clientError('protocol', this.#ctx.lang());
    return d.files.filter(isCard).map((f) => this.#file(f));
  }

  /** Télécharge tous ses fichiers dans `destDir` ; rend leurs chemins. */
  download(destDir, options = {}) {
    return downloadJob(this.#ctx, this, destDir, options);
  }

  toJSON() {
    return { ...this.#state, files: this.#state.files.map((f) => f.toJSON()) };
  }

  toString() {
    const p = this.progress && typeof this.progress.percent === 'number' ? ` ${this.progress.percent} %` : '';
    return `[travail ${this.id} · ${this.status ?? '?'}${p}]`;
  }

  [INSPECT]() {
    return `Job ${JSON.stringify({ id: this.id, status: this.status, label: this.label, clientRef: this.clientRef, progress: this.progress })}`;
  }
}
