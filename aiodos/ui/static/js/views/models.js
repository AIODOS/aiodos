// Modèles de conversation vus par l'interface : chaque modèle une fois, avec le mode
// automatique (« auto:<modèle> » : la machine qui répond se choisit au moment d'envoyer,
// cf. network/placement.py) et les machines qui l'ont, pour en désigner une.
import { state } from '../state.js';

export const AUTO = 'auto:';

/** [{ name, size, auto: identifiant automatique ou null, machines: [{ id, label, slow }] }] */
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
      e.machines.unshift({ id: m.id, label: 'Cette machine', local: true, size: m.size });
    } else if (m.group === 'remote') {
      const e = entry(m.label);
      e.size ||= m.size;
      e.machines.push({ id: m.id, label: m.trust === 'friend' ? `${m.peer} (ami)` : m.peer,
                        slow: m.fits_vram === false });
    }
  }
  return [...byName.values()].sort((a, b) => a.name.localeCompare(b.name, 'fr'));
}

/** Nom du modèle désigné par un identifiant : « auto:x », « peer:<machine>:x » ou « x ». */
export function modelName(id) {
  if (!id) return '';
  if (id.startsWith(AUTO)) return id.slice(AUTO.length);
  if (id.startsWith('peer:')) return id.split(':').slice(2).join(':');
  return id;
}

/**
 * Libellé d'un modèle : « qwen3.6 · PC fixe » s'il est servi par une machine appairée.
 * here : préciser « cette machine » pour un modèle local (conversation en mode automatique).
 */
export function modelLabel(id, here = false) {
  if (id?.startsWith(AUTO)) return `${modelName(id)} · automatique`;
  const m = state.models.find(x => x.id === id);
  if (m) {
    if (m.peer) return `${m.label} · ${m.peer}`;
    return here && m.group === 'local' ? `${m.label} · cette machine` : m.label;
  }
  if (id?.startsWith('peer:')) return `${modelName(id)} · autre machine`;
  return here && id && !id.startsWith('cloud:') ? `${id} · cette machine` : (id || '');
}

/** Identifiant à utiliser pour ce modèle quand on ne désigne pas de machine. */
export function defaultChoice(entry) {
  return entry.auto || entry.machines[0]?.id || '';
}

/** Modèle par défaut réglé avant le mode automatique : le nom seul y désignait le modèle,
 *  pas la machine ; il vaut désormais « automatique ». */
export function asDefault(id) {
  return id && !/^(auto|peer|cloud):/.test(id) ? AUTO + id : id;
}
