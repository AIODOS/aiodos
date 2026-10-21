// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — le format neutre des messages (celui d'ARKLIGHT) ↔ celui d'OpenAI.
//
// Neutre :
//   { role: 'system', content }
//   { role: 'user', content, images?: [data-URL] }
//   { role: 'assistant', content, toolCalls?: [{ id, name, args }] }
//   { role: 'tool', toolCallId, name?, content }
// OpenAI : parties « image_url », « tool_calls » (arguments en texte), « tool_call_id ».
// Un message déjà au format d'OpenAI passe tel quel.

import { makeError } from './errors.js';

const DATA_IMAGE_RE = /^data:image\/[a-z0-9.+-]+;base64,/i;

function invalid(param, message) {
  return makeError('invalid_request', message, { param });
}

function textOf(content) {
  if (content == null) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .filter((p) => p && (p.type === 'text' || typeof p.text === 'string'))
      .map((p) => p.text ?? '')
      .join('');
  }
  return String(content);
}

/** Le type d'une image d'après ses premiers octets (PNG, JPEG, GIF, WebP), ou null. */
function sniffImage(bytes) {
  const b = bytes;
  if (b.length >= 8 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return 'image/png';
  if (b.length >= 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg';
  if (b.length >= 6 && b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) return 'image/gif';
  if (b.length >= 12 && b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 && b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50) return 'image/webp';
  return null;
}

/**
 * Une image pour AIODOS : une data-URL, ou des octets (Buffer, Uint8Array) convertis en
 * data-URL. Jamais une adresse ni un chemin : AIODOS ne lit pas de fichier pour une appli.
 */
export function imageDataUrl(image) {
  if (typeof image === 'string') {
    if (DATA_IMAGE_RE.test(image)) return image;
    throw invalid('images', "Les images s'envoient en data-URL (data:image/…;base64,…) ou en octets, jamais par une adresse ou un chemin.");
  }
  if (image instanceof Uint8Array) {
    const type = sniffImage(image);
    if (!type) throw invalid('images', "Image d'un format inconnu (PNG, JPEG, GIF ou WebP attendu).");
    return `data:${type};base64,${Buffer.from(image.buffer, image.byteOffset, image.byteLength).toString('base64')}`;
  }
  throw invalid('images', 'Image invalide.');
}

/** Les arguments d'un appel d'outil, analysés : { args (objet, ou null si illisible), raw (texte) }. */
export function parseToolArguments(value) {
  if (value && typeof value === 'object') return { args: value, raw: JSON.stringify(value) };
  const raw = value == null ? '' : String(value);
  if (!raw.trim()) return { args: {}, raw };
  try {
    let v = JSON.parse(raw);
    // certains modèles encodent deux fois
    if (typeof v === 'string') v = JSON.parse(v);
    if (v && typeof v === 'object' && !Array.isArray(v)) return { args: v, raw };
  } catch {
    /* illisible : args null, le texte reste dans raw */
  }
  return { args: null, raw };
}

function argumentsText(call) {
  if (call.args && typeof call.args === 'object') return JSON.stringify(call.args);
  if (typeof call.raw === 'string') return call.raw;
  if (typeof call.arguments === 'string') return call.arguments;
  return '{}';
}

/** Un message neutre → OpenAI. */
export function toOpenAIMessage(m) {
  if (!m || typeof m !== 'object') throw invalid('messages', 'Message invalide.');
  switch (m.role) {
    case 'system':
    case 'developer':
      return { role: m.role, content: textOf(m.content) };
    case 'user': {
      const images = (Array.isArray(m.images) ? m.images : []).map(imageDataUrl);
      const parts = images.map((url) => ({ type: 'image_url', image_url: { url } }));
      if (Array.isArray(m.content)) return { role: 'user', content: [...m.content, ...parts] };
      if (!parts.length) return { role: 'user', content: textOf(m.content) };
      const text = textOf(m.content);
      return { role: 'user', content: text ? [{ type: 'text', text }, ...parts] : parts };
    }
    case 'assistant': {
      if (Array.isArray(m.tool_calls)) return { role: 'assistant', content: m.content ?? null, tool_calls: m.tool_calls };
      const calls = Array.isArray(m.toolCalls) ? m.toolCalls.filter((c) => c && c.name) : [];
      const text = textOf(m.content);
      const out = { role: 'assistant', content: text || (calls.length ? null : '') };
      if (calls.length) {
        out.tool_calls = calls.map((c, i) => ({
          id: c.id || `call_${i + 1}`,
          type: 'function',
          function: { name: c.name, arguments: argumentsText(c) },
        }));
      }
      return out;
    }
    case 'tool': {
      const id = m.toolCallId ?? m.tool_call_id;
      if (typeof id !== 'string' || !id) throw invalid('messages', "Un résultat d'outil doit porter toolCallId.");
      const content = typeof m.content === 'string' ? m.content : JSON.stringify(m.content ?? null);
      return { role: 'tool', tool_call_id: id, content };
    }
    default:
      throw invalid('messages', `Rôle de message inconnu : ${String(m.role)}`);
  }
}

/** La consigne système et les messages neutres → la liste « messages » d'OpenAI. */
export function toOpenAIMessages({ system, messages } = {}) {
  const out = [];
  if (system != null && String(system) !== '') out.push({ role: 'system', content: String(system) });
  for (const m of Array.isArray(messages) ? messages : []) out.push(toOpenAIMessage(m));
  return out;
}

/** Un appel d'outil d'OpenAI → neutre ({ id, name, args, raw }). */
export function toolCallFromOpenAI(c, i = 0) {
  const fn = (c && c.function) || {};
  const { args, raw } = parseToolArguments(fn.arguments);
  return { id: (c && c.id) || `call_${i + 1}`, name: fn.name || '', args, raw };
}

/** Un message d'OpenAI → neutre. */
export function fromOpenAIMessage(m) {
  if (!m || typeof m !== 'object') throw invalid('messages', 'Message invalide.');
  if (m.role === 'user') {
    if (!Array.isArray(m.content)) return { role: 'user', content: textOf(m.content) };
    const images = m.content
      .filter((p) => p && p.type === 'image_url')
      .map((p) => (typeof p.image_url === 'string' ? p.image_url : p.image_url && p.image_url.url))
      .filter((u) => typeof u === 'string');
    const out = { role: 'user', content: textOf(m.content) };
    if (images.length) out.images = images;
    return out;
  }
  if (m.role === 'assistant') {
    const out = { role: 'assistant', content: textOf(m.content) };
    const calls = Array.isArray(m.tool_calls) ? m.tool_calls.map(toolCallFromOpenAI) : [];
    if (calls.length) out.toolCalls = calls;
    return out;
  }
  if (m.role === 'tool') return { role: 'tool', toolCallId: m.tool_call_id ?? '', content: textOf(m.content) };
  if (m.role === 'system' || m.role === 'developer') return { role: 'system', content: textOf(m.content) };
  throw invalid('messages', `Rôle de message inconnu : ${String(m.role)}`);
}

/** La liste « messages » d'OpenAI → { system, messages } neutres. */
export function fromOpenAIMessages(list = []) {
  const systems = [];
  const messages = [];
  for (const m of Array.isArray(list) ? list : []) {
    const n = fromOpenAIMessage(m);
    if (n.role === 'system') systems.push(n.content);
    else messages.push(n);
  }
  return { system: systems.length ? systems.join('\n\n') : null, messages };
}

/** Outils neutres ({ name, description, parameters }) → OpenAI. Ceux déjà au format passent. */
export function toOpenAITools(tools = []) {
  return (Array.isArray(tools) ? tools : []).map((t) => {
    if (t && t.type === 'function' && t.function && typeof t.function.name === 'string') return t;
    if (!t || typeof t.name !== 'string' || !t.name) throw invalid('tools', 'Outil invalide : il lui faut un nom.');
    const fn = { name: t.name };
    if (t.description != null) fn.description = String(t.description);
    fn.parameters = t.parameters ?? t.input_schema ?? { type: 'object', properties: {} };
    return { type: 'function', function: fn };
  });
}

/** Outils d'OpenAI → neutres. */
export function fromOpenAITools(tools = []) {
  return (Array.isArray(tools) ? tools : [])
    .filter((t) => t && t.function && typeof t.function.name === 'string')
    .map((t) => {
      const out = { name: t.function.name };
      if (t.function.description != null) out.description = t.function.description;
      out.parameters = t.function.parameters ?? { type: 'object', properties: {} };
      return out;
    });
}
