// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — le genre d'un fichier : reconnu par ses premiers octets (pour le
// Content-Type d'une entrée envoyée sans genre), et l'extension des fichiers reçus.
//
// AIODOS relit lui-même le genre d'une entrée dans ses premiers octets : celui que le client
// déclare n'est qu'une indication. Il prend les images PNG, JPEG, WebP, GIF, les vidéos MP4,
// WebM et les sons WAV, MP3, FLAC, OGG (docs/api-applications.md § 5.7).

const ascii = (bytes, start, end) => String.fromCharCode(...bytes.subarray(start, end));

/** Le genre d'après les premiers octets (16 suffisent), ou null. */
export function sniffType(bytes) {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
  if (b.length >= 8 && b[0] === 0x89 && ascii(b, 1, 4) === 'PNG' && b[4] === 0x0d && b[5] === 0x0a && b[6] === 0x1a && b[7] === 0x0a) {
    return 'image/png';
  }
  if (b.length >= 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg';
  const head = ascii(b, 0, Math.min(b.length, 12));
  if (head.startsWith('GIF87a') || head.startsWith('GIF89a')) return 'image/gif';
  if (head.startsWith('RIFF') && head.slice(8, 12) === 'WEBP') return 'image/webp';
  if (head.startsWith('RIFF') && head.slice(8, 12) === 'WAVE') return 'audio/wav';
  if (b.length >= 4 && b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3) return 'video/webm';
  if (head.slice(4, 8) === 'ftyp') return 'video/mp4';
  if (head.startsWith('fLaC')) return 'audio/flac';
  if (head.startsWith('OggS')) return 'audio/ogg';
  if (head.startsWith('ID3')) return 'audio/mpeg';
  if (b.length >= 2 && b[0] === 0xff && (b[1] === 0xfb || b[1] === 0xf3 || b[1] === 0xf2)) return 'audio/mpeg';
  return null;
}

const EXTENSIONS = {
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'image/webp': '.webp',
  'image/gif': '.gif',
  'video/mp4': '.mp4',
  'video/webm': '.webm',
  'audio/wav': '.wav',
  'audio/mpeg': '.mp3',
  'audio/flac': '.flac',
  'audio/ogg': '.ogg',
  'model/gltf-binary': '.glb',
};

/** L'extension d'un fichier reçu, d'après son genre (« .bin » s'il est inconnu). */
export function extensionOf(type) {
  const t = String(type || '').split(';')[0].trim().toLowerCase();
  return EXTENSIONS[t] ?? '.bin';
}
