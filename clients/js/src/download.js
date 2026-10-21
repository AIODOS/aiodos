// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — job.download(dossier) : tous les fichiers d'un travail, rangés sous
// des noms stables, <stem>-<unité>[-<n>].<ext> (« mood-03.png », puis « mood-03-2.png » pour
// un deuxième fichier de l'unité 3), chacun repris s'il est partiel et vérifié par son
// empreinte (file.js). Un fichier déjà là, entier et juste, n'est pas redemandé : relancer
// download() après une coupure ne refait que ce qui manque.

import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { makeError } from './errors.js';
import { extensionOf } from './media.js';

const RESERVED = /^(con|prn|aux|nul|com\d|lpt\d)$/i;

/** Un début de nom de fichier sûr : ni séparateur, ni caractère que Windows refuse. */
export function safeStem(stem) {
  let s = String(stem ?? '')
    .normalize('NFC')
    .replace(/[\u0000-\u001f\u007f<>:"/\\|?*]/g, '_')
    .replace(/[. ]+$/, '')
    .trim();
  if (!s) s = 'aiodos';
  if (RESERVED.test(s)) s = `_${s}`;
  return s.slice(0, 120);
}

/** Les noms des fichiers d'un travail (id → nom), stables quand d'autres fichiers arrivent. */
export function fileNames(job, files, stem) {
  const base = safeStem(stem);
  const total = Math.max(1, (job.progress && job.progress.total) || 0, ...files.map((f) => (Number.isInteger(f.unit) ? f.unit + 1 : 0)));
  const width = String(total).length;
  const seen = new Map(); // unité → fichiers déjà nommés
  const names = new Map();
  for (const f of files) {
    const unit = Number.isInteger(f.unit) ? f.unit : null;
    const k = seen.get(unit) ?? 0;
    seen.set(unit, k + 1);
    const n = unit == null ? 'x' : String(unit + 1).padStart(width, '0');
    names.set(f.id, `${base}-${n}${k ? `-${k + 1}` : ''}${extensionOf(f.type)}`);
  }
  return names;
}

/**
 * Télécharge les fichiers du travail dans `destDir` (créé au besoin) ; `onFile(chemin, fichier)`
 * après chacun. Rend les chemins, dans l'ordre des fichiers du travail.
 */
export async function downloadJob(ctx, job, destDir, { stem, onFile, signal } = {}) {
  const lang = ctx.lang();
  await ctx.session.requireFeature(['jobs', 'files'], { signal });
  let dir = null;
  if (typeof destDir === 'string' && destDir) dir = path.resolve(destDir);
  else if (destDir instanceof URL && destDir.protocol === 'file:') dir = path.resolve(fileURLToPath(destDir));
  if (!dir) throw makeError('invalid_request', lang === 'en' ? 'destDir: the folder for the files.' : 'destDir : le dossier où ranger les fichiers.', { param: 'destDir' });
  await job.refresh({ signal }); // les fichiers arrivés depuis le dernier état
  const files = job.state.files;
  const names = fileNames(job, files, stem ?? job.id);
  await mkdir(dir, { recursive: true });
  const paths = [];
  for (const f of files) {
    const p = await f.save(path.join(dir, names.get(f.id)), { signal });
    paths.push(p);
    if (typeof onFile === 'function') await onFile(p, f);
  }
  return paths;
}
