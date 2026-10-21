// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — un fichier produit pour l'appli (docs/api-applications.md § 5.7) : la
// fiche (GET /aiodos/v1/files/{id}), le contenu (…/content, par plages), l'effacement.
//
// - content() rend les octets ; save(chemin) les écrit au fil de l'eau dans « chemin.part »,
//   puis le renomme : un fichier partiel est repris par Range (bytes=N-) au lieu d'être
//   recommencé, après une coupure (3 essais par appel) comme à l'appel suivant ;
// - l'empreinte SHA-256 est toujours vérifiée, et un fichier faux n'est jamais gardé ; plus
//   d'octets que la taille annoncée sont refusés ;
// - un fichier servi jusqu'à son dernier octet est « récupéré » : AIODOS le garde encore 7 jours ;
// - link() (lien signé) rejette NotSupported : AIODOS ne le sert pas encore.

import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { mkdir, open, rename, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { AiodosError, clientError, makeError } from './errors.js';
import { sha256Hex } from './inputs.js';
import { camelize, notYet } from './util.js';

/** (interne) Le nouvel état d'une fiche reçu d'AIODOS. */
export const APPLY = Symbol('aiodos.file.apply');
const INSPECT = Symbol.for('nodejs.util.inspect.custom');
const ATTEMPTS = 3;
const CARD_TIMEOUT = 30_000;

/** Le chemin d'un fichier, l'identifiant vérifié avant tout envoi. */
export function filePath(id, lang = 'fr', suffix = '') {
  if (typeof id !== 'string' || !id || id.length > 200) {
    throw makeError('invalid_request', lang === 'en' ? 'Invalid file id.' : 'Identifiant de fichier invalide.', { param: 'id' });
  }
  return `/aiodos/v1/files/${encodeURIComponent(id)}${suffix}`;
}

/** Le SHA-256 d'un fichier, lu au fil de l'eau. */
async function hashFile(p) {
  const h = createHash('sha256');
  for await (const chunk of createReadStream(p)) h.update(chunk);
  return h.digest('hex');
}

/** Le fichier est-il déjà là, entier et juste ? */
async function sameFile(p, size, sha256) {
  try {
    const s = await stat(p);
    return s.isFile() && s.size === size && (await hashFile(p)) === sha256;
  } catch {
    return false;
  }
}

/** Un puits en mémoire (content()). */
function memorySink() {
  let chunks = [];
  let size = 0;
  return {
    size: async () => size,
    reset: async () => {
      chunks = [];
      size = 0;
    },
    write: async (c) => {
      chunks.push(Buffer.from(c));
      size += c.length;
    },
    matches: async (sha256) => sha256Hex(Buffer.concat(chunks)) === sha256,
    close: async () => {},
    bytes: () => Buffer.concat(chunks),
  };
}

/** Un puits sur le disque : le fichier « .part », écrit à la suite. */
function fileSink(part) {
  let handle = null;
  const close = async () => {
    if (!handle) return;
    const h = handle;
    handle = null;
    await h.close();
  };
  return {
    size: async () => {
      try {
        return (await stat(part)).size;
      } catch {
        return 0;
      }
    },
    reset: async () => {
      await close();
      await rm(part, { force: true });
    },
    write: async (c) => {
      if (!handle) handle = await open(part, 'a');
      for (let done = 0; done < c.length; ) done += (await handle.write(c, done, c.length - done)).bytesWritten;
    },
    matches: async (sha256) => {
      await close();
      try {
        await stat(part);
      } catch {
        await writeFile(part, ''); // un fichier vide
      }
      return (await hashFile(part)) === sha256;
    },
    close,
  };
}

/** Le début d'une réponse 206 (« bytes 10-73/74 » → 10), ou null. */
function rangeStart(header) {
  const m = /^bytes\s+(\d+)-\d+\/(?:\d+|\*)$/i.exec(String(header || '').trim());
  return m ? Number(m[1]) : null;
}

/**
 * Une requête du contenu à partir de `offset`, écrite dans le puits ; rend le début réel (0 si
 * AIODOS a tout renvoyé). Une coupure en route lève unreachable : le puits garde ce qui est
 * arrivé, l'essai suivant reprend là.
 */
async function receive(ctx, file, sink, offset, signal) {
  const lang = ctx.lang();
  const headers = offset > 0 ? { Range: `bytes=${offset}-` } : undefined;
  const h = await ctx.session.keyed({ path: filePath(file.id, lang, '/content'), headers, accept: '*/*', stream: true, signal, timeout: ctx.timeout, feature: 'files' });
  if (!h.stream) {
    if (h.status === 416) {
      await sink.reset(); // la plage n'existe pas : le début gardé n'est pas celui de ce fichier
      return offset;
    }
    ctx.transport.expectOk(h); // l'erreur d'AIODOS (FileNotFound…)
    throw clientError('protocol', lang);
  }
  let start = offset;
  try {
    if (offset > 0 && h.res.status === 206) {
      const at = rangeStart(h.res.headers.get('content-range'));
      if (at != null && at !== offset) {
        // une autre plage que celle demandée : tout recommencer
        h.abort();
        await sink.reset();
        return receive(ctx, file, sink, 0, signal);
      }
    } else if (offset > 0) {
      await sink.reset(); // la plage n'a pas été suivie : tout le fichier arrive
      start = 0;
    }
    const want = file.bytes - start;
    let got = 0;
    const reader = h.res.body ? h.res.body.getReader() : null; // un fichier vide peut n'avoir aucun corps
    while (reader) {
      let part;
      try {
        part = await reader.read();
      } catch (err) {
        throw h.fail(err); // coupure, délai, arrêt
      }
      if (part.done) break;
      h.timer.reset();
      got += part.value.length;
      if (got > want) {
        await sink.reset();
        throw clientError('protocol', lang, { textKey: 'file_size', detail: { file: file.id } });
      }
      await sink.write(part.value);
    }
    if (got < want) throw clientError('unreachable', lang, { textKey: 'connection_lost', vars: { host: h.url.host } });
    return start;
  } catch (err) {
    h.abort();
    if (signal && signal.aborted) throw clientError('cancelled', lang);
    throw err;
  } finally {
    h.done();
  }
}

/** Les octets du fichier vers un puits, repris par Range, l'empreinte vérifiée à la fin. */
async function transfer(ctx, file, sink, signal) {
  const lang = ctx.lang();
  let fresh = false; // déjà recommencé depuis le début, une fois
  for (let attempt = 1; ; attempt += 1) {
    let offset = await sink.size();
    if (offset > file.bytes) {
      await sink.reset();
      offset = 0;
    }
    try {
      if (offset < file.bytes || file.bytes === 0) offset = await receive(ctx, file, sink, offset, signal);
      if (await sink.matches(file.sha256)) return;
      await sink.reset(); // jamais un fichier faux
      if (offset > 0 && !fresh) {
        fresh = true; // le début gardé était peut-être faux : tout depuis 0
        continue;
      }
      throw clientError('protocol', lang, { textKey: 'file_hash', detail: { file: file.id } });
    } catch (err) {
      if (signal && signal.aborted) throw clientError('cancelled', lang);
      const cut = err instanceof AiodosError && (err.code === 'unreachable' || err.code === 'timeout');
      if (cut && attempt < ATTEMPTS) continue; // reprise là où la coupure s'est faite
      throw err;
    }
  }
}

/** Le chemin où ranger un fichier (texte ou URL file:). */
function destination(target, lang) {
  if (typeof target === 'string' && target) return path.resolve(target);
  if (target instanceof URL && target.protocol === 'file:') return path.resolve(fileURLToPath(target));
  throw makeError('invalid_request', lang === 'en' ? 'Where to save the file: a path.' : 'Où ranger le fichier : un chemin.', { param: 'path' });
}

/** Un fichier produit pour l'appli. */
export class JobFile {
  #ctx;
  #data = null;

  constructor(ctx, data) {
    this.#ctx = ctx;
    this[APPLY](data);
  }

  [APPLY](data) {
    if (!data || typeof data !== 'object' || typeof data.id !== 'string' || (this.#data && data.id !== this.#data.id)) {
      throw clientError('protocol', this.#ctx.lang());
    }
    this.#data = camelize(data);
    return this;
  }

  get id() {
    return this.#data.id;
  }
  /** L'indice de l'unité qui l'a produit. */
  get unit() {
    return this.#data.unit ?? null;
  }
  /** Le nœud de sortie (SaveImage…) du graphe de l'appli. */
  get node() {
    return this.#data.node ?? null;
  }
  get type() {
    return this.#data.type ?? null;
  }
  /** La taille, en octets. */
  get bytes() {
    return typeof this.#data.bytes === 'number' ? this.#data.bytes : 0;
  }
  get sha256() {
    return this.#data.sha256 ?? null;
  }
  get width() {
    return this.#data.width ?? null;
  }
  get height() {
    return this.#data.height ?? null;
  }
  /** La machine qui l'a calculé (null pour un inconnu du réseau public). */
  get machine() {
    return this.#data.machine ?? null;
  }
  /** here, mine, friend ou public. */
  get place() {
    return this.#data.place ?? null;
  }
  /** « comfyui 0.35.1 ». */
  get engine() {
    return this.#data.engine ?? null;
  }
  get verified() {
    return this.#data.verified ?? null;
  }
  /** Récupéré (servi jusqu'au dernier octet) : AIODOS le garde encore 7 jours. */
  get fetched() {
    return this.#data.fetched === true;
  }
  /** L'échéance de la garde après récupération (secondes Unix), ou null. */
  get expiresAt() {
    return this.#data.expiresAt ?? null;
  }
  get createdAt() {
    return this.#data.createdAt ?? null;
  }

  /** Relit la fiche (GET /aiodos/v1/files/{id}). */
  async refresh({ signal } = {}) {
    const ctx = this.#ctx;
    await ctx.session.requireFeature('files', { signal });
    const res = await ctx.session.keyed({ path: filePath(this.id, ctx.lang()), signal, timeout: CARD_TIMEOUT, feature: 'files' });
    return this[APPLY](ctx.transport.expectOk(res));
  }

  /** Les octets du fichier (Buffer), l'empreinte vérifiée. */
  async content({ signal } = {}) {
    const ctx = this.#ctx;
    await ctx.session.requireFeature('files', { signal });
    const sink = memorySink();
    await transfer(ctx, this, sink, signal);
    this.#data.fetched = true;
    return sink.bytes();
  }

  /**
   * Écrit le fichier à ce chemin, au fil de l'eau, repris s'il est partiel (« chemin.part »)
   * et vérifié par son empreinte. Déjà là, entier et juste : rien n'est redemandé. Rend le
   * chemin absolu.
   */
  async save(target, { signal } = {}) {
    const ctx = this.#ctx;
    const lang = ctx.lang();
    await ctx.session.requireFeature('files', { signal });
    const dest = destination(target, lang);
    await mkdir(path.dirname(dest), { recursive: true });
    if (await sameFile(dest, this.bytes, this.sha256)) return dest;
    const part = `${dest}.part`;
    const sink = fileSink(part);
    try {
      await transfer(ctx, this, sink, signal);
    } finally {
      await sink.close();
    }
    await rename(part, dest);
    this.#data.fetched = true;
    return dest;
  }

  /** Efface le fichier dans AIODOS (DELETE /aiodos/v1/files/{id}). */
  async delete({ signal } = {}) {
    const ctx = this.#ctx;
    await ctx.session.requireFeature('files', { signal });
    const res = await ctx.session.keyed({ method: 'DELETE', path: filePath(this.id, ctx.lang()), signal, timeout: CARD_TIMEOUT, feature: 'files' });
    ctx.transport.expectOk(res);
    return { id: this.id, deleted: true };
  }

  /** Un lien signé : pas encore servi par AIODOS (NotSupported). */
  async link({ signal } = {}) {
    return notYet(this.#ctx, { needs: 'files', feature: 'files.link', signal });
  }

  toJSON() {
    return { ...this.#data };
  }

  toString() {
    return `[fichier ${this.id} · ${this.type ?? '?'} · ${this.bytes} o]`;
  }

  [INSPECT]() {
    return `JobFile ${JSON.stringify(this.toJSON())}`;
  }
}

/** Un fichier de l'appli, par son identifiant (GET /aiodos/v1/files/{id}). */
export async function getFile(ctx, id, { signal } = {}) {
  await ctx.session.requireFeature('files', { signal });
  const res = await ctx.session.keyed({ path: filePath(id, ctx.lang()), signal, timeout: CARD_TIMEOUT, feature: 'files' });
  return new JobFile(ctx, ctx.transport.expectOk(res));
}
