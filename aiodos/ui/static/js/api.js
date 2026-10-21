// Accès à l'API du nœud. Le jeton arrive dans l'URL (?t=…) au lancement ; on le
// garde pour la session de la fenêtre puis on l'efface de la barre d'adresse.
// Chaque requête dit la langue de l'interface : le nœud répond dans la même (i18n.js).
import { has, lang, t } from './i18n.js';

const params = new URLSearchParams(location.search);
let token = params.get('t');
if (token) {
  sessionStorage.setItem('aiodos-token', token);
  history.replaceState(null, '', '/');
} else {
  token = sessionStorage.getItem('aiodos-token') || '';
}

export class ApiError extends Error {
  constructor(status, message, data = null) {
    super(message);
    this.status = status;
    this.data = data;            // la réponse entière, quand elle dit plus que « detail »
  }
}

async function failure(res) {
  // Avant l'arrivée des textes (démarrage), le code HTTP seul
  let message = has('ui.error_status') ? t('ui.error_status', { status: res.status }) : `HTTP ${res.status}`;
  let data = null;
  try {
    data = await res.json();
    if (typeof data.detail === 'string') message = data.detail;
  } catch { /* pas de JSON */ }
  return new ApiError(res.status, message, data);
}

export async function api(method, path, body) {
  const headers = { 'X-Aiodos-Token': token, 'Accept-Language': lang };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch('/api' + path, {
    method, headers, body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw await failure(res);
  return res.json();
}

// Fichier de l'API pour une balise <img> ou un lien (pas d'en-tête possible : jeton en paramètre)
export const fileUrl = path => `/api${path}?t=${encodeURIComponent(token)}`;

/** Un fichier envoyé tel quel (corps brut), son nom dans X-File-Name : une pièce jointe. */
export async function rawUpload(path, file, name) {
  const res = await fetch('/api' + path, {
    method: 'POST', body: file,
    headers: { 'X-Aiodos-Token': token, 'Accept-Language': lang,
               'Content-Type': file.type || 'application/octet-stream',
               'X-File-Name': encodeURIComponent(name || file.name || 'fichier') },
  });
  if (!res.ok) throw await failure(res);
  return res.json();
}

export const get = path => api('GET', path);
export const post = (path, body = {}) => api('POST', path, body);
export const put = (path, body) => api('PUT', path, body);
export const patch = (path, body) => api('PATCH', path, body);
export const del = path => api('DELETE', path);

// Flux d'événements du nœud (progression des tâches, changements…)
export function listen(onEvent, onState) {
  let source;
  const open = () => {
    source = new EventSource(`/api/events?t=${encodeURIComponent(token)}&lang=${lang}`);
    source.onopen = () => onState?.(true);
    source.onmessage = e => { try { onEvent(JSON.parse(e.data)); } catch { /* ignoré */ } };
    source.onerror = () => { onState?.(false); };
  };
  open();
  return () => source.close();
}

// Réponse du chat en streaming (POST + flux SSE lu à la main)
export async function streamChat(body, onEvent, signal) {
  const res = await fetch('/api/chat', {
    method: 'POST', signal,
    headers: { 'X-Aiodos-Token': token, 'Accept-Language': lang, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await failure(res);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      for (const line of block.split('\n')) {
        if (line.startsWith('data: ')) onEvent(JSON.parse(line.slice(6)));
      }
    }
  }
}

// La fenêtre native d'AIODOS (pywebview), pas un navigateur ni l'appli Android
export const inNativeWindow = () => !!window.pywebview;

// Fenêtre native : boîtes de dialogue de fichiers (chemins complets)
function bridge() {
  if (window.pywebview?.api) return Promise.resolve(window.pywebview.api);
  return new Promise(resolve => {
    const done = () => resolve(window.pywebview?.api || null);
    window.addEventListener('pywebviewready', done, { once: true });
    setTimeout(done, 1500);
  });
}

export async function pickFiles() {
  const b = await bridge();
  return b ? b.pick_files() : null;      // null = pas de fenêtre native
}

export async function pickFolder() {
  const b = await bridge();
  return b ? b.pick_folder() : null;
}

export function openExternal(url) {
  return post('/open_url', { url });
}
