// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — les entrées des travaux (docs/api-applications.md § 5.7) : chaque
// fichier part une fois, par son empreinte. HEAD /aiodos/v1/inputs/{sha256} (200 : cette appli
// l'a déjà envoyé ; 404 : non), puis PUT, corps brut, s'il manque.
//
// - une entrée : des octets (Buffer, Uint8Array, ArrayBuffer), un chemin (texte ou URL file:),
//   { data | path, type }, ou { sha256 } pour une entrée déjà envoyée ;
// - 100 Mo au plus (la borne de la passerelle, 100 × 1024 × 1024 octets), vérifiés avant tout
//   envoi (InputTooLarge) ; une entrée vide est refusée aussi ;
// - le genre : celui donné, sinon reconnu par les premiers octets ; AIODOS le relit de toute
//   façon, et refuse un genre qu'il ne connaît pas (415) ;
// - HEAD n'a pas de corps : un refus s'explique par l'info avec la clé (clé retirée, droit
//   engines:comfyui manquant, AIODOS fermé aux applications).

import { createHash } from 'node:crypto';
import { readFile, stat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

import { AiodosError, clientError, errorFromResponse, makeError } from './errors.js';
import { sniffType } from './media.js';
import { codeText } from './texts.js';
import { isPlainObject } from './util.js';

/** 100 Mo au plus par entrée (la borne de la passerelle : 100 × 1024 × 1024 octets). */
export const MAX_INPUT_BYTES = 100 * 1024 * 1024;
/** L'identifiant d'une entrée dans un travail (le jeton « @in:<id> »). */
export const INPUT_ID_RE = /^[A-Za-z0-9_.-]{1,64}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
const HEAD_TIMEOUT = 15_000;
const CAPABILITY = 'engines:comfyui';

function invalid(lang, fr, en) {
  return makeError('invalid_request', lang === 'en' ? en : fr, { param: 'inputs' });
}

/** Le SHA-256 d'octets, en hexadécimal minuscule. */
export function sha256Hex(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

/**
 * Ce que l'appli donne pour une entrée → { bytes } ou { path } (avec son genre s'il est donné),
 * ou { sha256 } (déjà envoyée). Lève invalid_request pour tout le reste, avant tout envoi.
 */
export function inputSource(value, lang = 'fr', type = null) {
  if (type != null && (typeof type !== 'string' || !/^[\w.+-]+\/[\w.+-]+$/.test(type))) {
    throw invalid(lang, `Genre d'entrée invalide : « ${String(type).slice(0, 40)} » (par exemple image/png).`, `Invalid input type: “${String(type).slice(0, 40)}” (for example image/png).`);
  }
  if (value instanceof Uint8Array) return { bytes: value, type }; // Buffer compris
  if (value instanceof ArrayBuffer) return { bytes: new Uint8Array(value), type };
  if (ArrayBuffer.isView(value)) return { bytes: new Uint8Array(value.buffer, value.byteOffset, value.byteLength), type };
  if (typeof value === 'string' && value) return { path: value, type };
  if (value instanceof URL && value.protocol === 'file:') return { path: fileURLToPath(value), type };
  if (isPlainObject(value)) {
    const t = value.type ?? type;
    if (value.data != null) return inputSource(value.data, lang, t);
    if (value.path != null) return inputSource(value.path, lang, t);
    if (typeof value.sha256 === 'string' && SHA256_RE.test(value.sha256)) return { sha256: value.sha256, type: t };
  }
  throw invalid(lang, 'Entrée illisible : des octets, un chemin, { data | path, type } ou { sha256 }.', 'Unreadable input: bytes, a path, { data | path, type } or { sha256 }.');
}

/** La taille d'une entrée, avant tout envoi (celle du fichier sur le disque pour un chemin). */
async function sizeOf(src, lang) {
  if (src.bytes) return src.bytes.length;
  let s;
  try {
    s = await stat(src.path);
  } catch {
    s = null;
  }
  if (!s || !s.isFile()) throw invalid(lang, `Entrée introuvable : ${src.path}`, `Input not found: ${src.path}`);
  return s.size;
}

/** Vide, ou plus de 100 Mo : refusée sans rien envoyer. */
function checkSize(size, lang) {
  if (size === 0) throw invalid(lang, 'Entrée vide.', 'Empty input.');
  if (size > MAX_INPUT_BYTES) {
    throw clientError('input_too_large', lang, {
      textKey: 'input_size',
      vars: { size: Math.ceil(size / (1024 * 1024)) },
      detail: { bytes: size, max_bytes: MAX_INPUT_BYTES },
    });
  }
}

/** Les octets d'une entrée (lus sur le disque pour un chemin, vérifiés encore une fois). */
async function bytesOf(src, lang) {
  if (src.bytes) return src.bytes;
  const data = await readFile(src.path);
  checkSize(data.length, lang); // le fichier a pu changer depuis
  return data;
}

/** HEAD n'a pas de corps : l'info avec la clé dit pourquoi la passerelle refuse. */
async function explainRefusal(ctx, r, signal) {
  const lang = ctx.lang();
  const of = (code, detail) => makeError(code, codeText(code, lang) ?? code, { status: r.status, detail });
  if (r.status === 401 || r.status === 403) {
    let info = null;
    try {
      info = await ctx.session.appInfo({ signal });
    } catch (err) {
      if (err instanceof AiodosError && err.code === 'cancelled') throw err;
    }
    if (info) {
      if (info.auth === 'revoked') return of('key_revoked');
      if (info.auth === 'expired') return of('key_expired');
      if (info.auth !== 'valid') return of('invalid_api_key');
      if (info.access && info.access.apps === false) return of('apps_closed');
      const scopes = info.app && Array.isArray(info.app.scopes) ? info.app.scopes : [];
      if (!scopes.includes(CAPABILITY)) return of('scope_denied', { scope: CAPABILITY });
    }
  }
  return errorFromResponse({ status: r.status, json: null, headers: r.headers, lang });
}

/** Envoie ces octets si cette appli ne l'a pas déjà fait (HEAD, puis PUT) ; rend leur sha256. */
async function upload(ctx, bytes, type, sha256, signal) {
  const path = `/aiodos/v1/inputs/${sha256}`;
  const head = await ctx.session.keyed({ method: 'HEAD', path, signal, timeout: HEAD_TIMEOUT, feature: 'inputs' });
  if (head.status === 200) return sha256;
  if (head.status !== 404) throw await explainRefusal(ctx, head, signal);
  const res = await ctx.session.keyed({
    method: 'PUT',
    path,
    body: bytes,
    contentType: type || sniffType(bytes.subarray(0, 16)) || 'application/octet-stream',
    signal,
    timeout: ctx.timeout,
    feature: 'inputs',
  });
  // 201 (rangée) ou 200 (déjà là) ; une empreinte fausse aurait été refusée (input_hash_mismatch)
  ctx.transport.expectOk(res);
  return sha256;
}

/**
 * Les entrées d'un travail (submit, plan) : toutes vérifiées avant le premier envoi (forme,
 * identifiant, taille), puis une à une lues, hachées et envoyées si AIODOS ne les a pas.
 * Rend { id: { sha256 } }, ou undefined s'il n'y en a pas.
 */
export async function prepareInputs(ctx, inputs, { signal } = {}) {
  const lang = ctx.lang();
  if (inputs == null) return undefined;
  if (!isPlainObject(inputs)) throw invalid(lang, '« inputs » : { identifiant: entrée }.', '“inputs”: { id: input }.');
  const sources = [];
  for (const [id, value] of Object.entries(inputs)) {
    if (!INPUT_ID_RE.test(id)) {
      throw invalid(lang, `Identifiant d'entrée invalide : « ${id.slice(0, 70)} » ([A-Za-z0-9_.-], 64 caractères au plus).`, `Invalid input id: “${id.slice(0, 70)}” ([A-Za-z0-9_.-], 64 characters at most).`);
    }
    const src = inputSource(value, lang);
    if (!src.sha256) checkSize(await sizeOf(src, lang), lang);
    sources.push([id, src]);
  }
  if (!sources.length) return undefined;
  if (sources.some(([, s]) => !s.sha256)) await ctx.session.requireFeature('inputs', { signal });
  const out = {};
  const sent = new Set(); // les mêmes octets sous deux identifiants ne partent qu'une fois
  for (const [id, src] of sources) {
    if (src.sha256) {
      out[id] = { sha256: src.sha256 };
      continue;
    }
    const bytes = await bytesOf(src, lang);
    const sha256 = sha256Hex(bytes);
    if (!sent.has(sha256)) {
      await upload(ctx, bytes, src.type, sha256, signal);
      sent.add(sha256);
    }
    out[id] = { sha256 };
  }
  return out;
}

export function createInputs(ctx) {
  return Object.freeze({
    /**
     * Envoie une entrée (octets ou chemin) si cette appli ne l'a pas déjà fait ; rend son
     * sha256, à citer ensuite dans un travail ({ sha256 }).
     */
    put: async (data, { type, signal } = {}) => {
      await ctx.session.requireFeature('inputs', { signal });
      const lang = ctx.lang();
      const src = inputSource(data, lang, type ?? null);
      if (src.sha256) throw invalid(lang, 'Entrée illisible : des octets ou un chemin.', 'Unreadable input: bytes or a path.');
      checkSize(await sizeOf(src, lang), lang);
      const bytes = await bytesOf(src, lang);
      return upload(ctx, bytes, src.type, sha256Hex(bytes), signal);
    },
  });
}
