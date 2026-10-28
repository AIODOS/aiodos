// Modèles de conversation vus par l'interface : chaque modèle une fois, avec le mode
// automatique (« auto:<modèle> » : la machine qui répond se choisit au moment d'envoyer,
// cf. network/placement.py) et les machines qui l'ont, pour en désigner une.
import { locale, t } from '../i18n.js';
import { state } from '../state.js';

export const AUTO = 'auto:';
const CLUSTER = 'reparti:';
const PUBLIC = 'public:';          // une machine du réseau public (network/public_chat.py)
// « Automatique · réseau public » : le meilleur modèle d'une machine du réseau public, quand ni
// cette machine ni mes machines n'en ont (network/placement.py, PUBLIC_BEST)
export const PUBLIC_AUTO = 'auto:*';

/** Le réseau public dans le choix du modèle (chat/public_consent.py, entry) : { id, ready,
 *  models } quand aucune de mes machines n'a de modèle de conversation et que mes
 *  conversations peuvent partir chez un inconnu (Réglages) ; null sinon, ou si la conversation
 *  reste sur mes machines (mine : « Sur mes machines seulement », chat-public.js). */
export function publicChoice(mine = false) {
  if (mine) return null;
  return state.models.find(m => m.group === 'public' && m.id === PUBLIC_AUTO) || null;
}

/** [{ name, size, auto: identifiant automatique ou null, machines: [{ id, label, slow }] }],
 *  du plus puissant au moins puissant (le plus gros d'abord). */
export function catalog() {
  const byName = new Map();
  const entry = name => {
    if (!byName.has(name)) byName.set(name, { name, size: 0, auto: null, machines: [] });
    return byName.get(name);
  };
  for (const m of state.models) {
    if (m.group === 'auto') {
      const e = entry(m.label);
      e.auto = m.id;
      e.size ||= m.size;
    } else if (m.group === 'local') {
      const e = entry(m.label);
      e.size = m.size;
      e.machines.unshift({ id: m.id, label: t('models.this_machine'), local: true, size: m.size });
    } else if (m.group === 'remote') {
      const e = entry(m.label);
      e.size ||= m.size;
      e.machines.push({ id: m.id, label: m.trust === 'friend' ? t('models.peer_friend', { name: m.peer }) : m.peer,
                        slow: m.fits_vram === false });
    }
  }
  return [...byName.values()].sort(byPower);
}

/** Du plus puissant au moins puissant : le plus gros d'abord, puis par nom (dans l'ordre de la
 *  langue de l'interface). */
export function byPower(a, b) {
  return (b.size || 0) - (a.size || 0)
    || String(a.name || a.label || '').localeCompare(String(b.name || b.label || ''), locale());
}

/** Nom du modèle désigné par un identifiant : « auto:x », « peer:<machine>:x »,
 *  « public:<clé>:x » (une machine du réseau public), « reparti:x » (gros modèle réparti entre
 *  mes machines) ou « x ». */
export function modelName(id) {
  if (!id) return '';
  if (id.startsWith(AUTO)) return id.slice(AUTO.length);
  if (id.startsWith(CLUSTER)) return id.slice(CLUSTER.length);
  if (id.startsWith('peer:') || id.startsWith(PUBLIC)) return id.split(':').slice(2).join(':');
  return id;
}

/**
 * Libellé d'un modèle : « qwen3.6 · PC fixe » s'il est servi par une machine appairée.
 * here : préciser « cette machine » pour un modèle local (conversation en mode automatique).
 */
export function modelLabel(id, here = false) {
  if (id === PUBLIC_AUTO) return t('models.public_auto');
  if (id?.startsWith(AUTO)) return t('models.label_auto', { name: modelName(id) });
  if (id?.startsWith(PUBLIC)) return t('models.label_public', { name: modelName(id) });
  const m = state.models.find(x => x.id === id);
  if (m) {
    if (m.peer) return `${m.label} · ${m.peer}`;
    if (m.group === 'cluster') return t('models.label_split', { name: m.label });
    return here && m.group === 'local' ? t('models.label_here', { name: m.label }) : m.label;
  }
  if (id?.startsWith('peer:')) return t('models.label_other', { name: modelName(id) });
  if (id?.startsWith(CLUSTER)) return t('models.label_split', { name: modelName(id) });
  return here && id ? t('models.label_here', { name: id }) : (id || '');
}

/** Identifiant à utiliser pour ce modèle quand on ne désigne pas de machine. */
export function defaultChoice(entry) {
  return entry.auto || entry.machines[0]?.id || '';
}

/** Modèle par défaut réglé avant le mode automatique : le nom seul y désignait le modèle,
 *  pas la machine ; il vaut désormais « automatique ». */
export function asDefault(id) {
  return id && !/^(auto|peer|reparti|public):/.test(id) ? AUTO + id : id;
}
