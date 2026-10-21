// État partagé entre les vues + petit système d'abonnement.
export const state = {
  settings: null,
  models: [],
  modelsLoaded: false,
  node: null,
  tree: { folders: [], conversations: [] },
  currentConversation: null,
  jobs: new Map(),
  executor: null,
  view: null,
};

const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
}

export function emit(event, data) {
  listeners.get(event)?.forEach(fn => fn(data));
}

// Préférences de confort propres à cette fenêtre (jamais d'état important ici)
export const prefs = {
  get(key, fallback) {
    try {
      // « coeur. » : préférences enregistrées avant le renommage du projet
      const v = localStorage.getItem('aiodos.' + key) ?? localStorage.getItem('coeur.' + key);
      return v === null ? fallback : JSON.parse(v);
    }
    catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem('aiodos.' + key, JSON.stringify(value)); } catch { /* stockage indisponible */ }
  },
};

// Consentements valables le temps de la session (cases « ne plus demander »)
// peerConsent : les amis chez qui la question peut partir sans redemander (« Chez les autres »)
export const session = { webConsent: false, peerConsent: new Set() };
