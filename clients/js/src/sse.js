// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — la lecture d'un flux SSE (lignes « data: », commentaires « : … »).

const MAX_BUFFER = 32 * 1024 * 1024; // une ligne de plus de 32 Mio : réponse incomprise

/**
 * Les événements d'un flux SSE, dans l'ordre d'arrivée :
 *   { type: 'comment', text }             pour une ligne « : … » (« : aiodos loading 5 »)
 *   { type: 'event', event, data, id }    à chaque ligne vide qui clôt des lignes « data: »
 * Les fins de ligne \n, \r\n et \r sont prises, même coupées entre deux morceaux. À la fin
 * du flux, un événement sans ligne vide finale est rendu quand même.
 *
 * Interrompre la boucle (break, exception) annule la lecture du corps.
 * @param {ReadableStream<Uint8Array>} body
 */
export async function* readSse(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buf = '';
  let data = [];
  let event = '';
  let id = null;
  let finished = false;

  function* lineToEvents(line) {
    if (line === '') {
      if (data.length) {
        const out = { type: 'event', event: event || 'message', data: data.join('\n'), id };
        data = [];
        event = '';
        yield out;
      }
      return;
    }
    if (line.startsWith(':')) {
      yield { type: 'comment', text: line.slice(1).replace(/^ /, '') };
      return;
    }
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'data') data.push(value);
    else if (field === 'event') event = value;
    else if (field === 'id') id = value;
    // « retry » et les champs inconnus sont ignorés
  }

  try {
    while (!finished) {
      const { done, value } = await reader.read();
      if (done) {
        buf += decoder.decode();
        finished = true;
      } else {
        buf += decoder.decode(value, { stream: true });
        if (buf.length > MAX_BUFFER) throw new Error('aiodos-client : ligne SSE trop longue');
      }
      const ends = /\r\n|\r|\n/g;
      let pos = 0;
      let m;
      while ((m = ends.exec(buf)) !== null) {
        // un \r tout au bout peut être la moitié d'un \r\n : on attend le morceau suivant
        if (m[0] === '\r' && m.index === buf.length - 1 && !finished) break;
        yield* lineToEvents(buf.slice(pos, m.index));
        pos = m.index + m[0].length;
        ends.lastIndex = pos;
      }
      buf = buf.slice(pos);
    }
    if (buf) yield* lineToEvents(buf);
    buf = '';
    yield* lineToEvents('');
  } finally {
    if (!finished) {
      try {
        await reader.cancel();
      } catch {
        /* déjà fermé */
      }
    }
    try {
      reader.releaseLock();
    } catch {
      /* rien */
    }
  }
}

/** Les secondes d'un commentaire « aiodos loading N », ou null. */
export function loadingSeconds(commentText) {
  const m = /^aiodos loading(?:\s+(\d+(?:\.\d+)?))?\s*$/.exec(String(commentText || '').trim());
  if (!m) return null;
  return m[1] ? Number(m[1]) : 0;
}
