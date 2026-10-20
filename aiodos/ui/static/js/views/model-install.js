// Installer un modèle de conversation : les modèles conseillés pour cet appareil (téléphone ou
// PC, carte graphique), aux licences libres, puis le téléchargement par Ollama, dont
// l'avancement arrive par l'événement « model_install » (services/model_install.py).
import { get, post, del } from '../api.js';
import { bytes, esc } from '../dom.js';
import { icon } from '../icons.js';
import { modal, toast, errorToast } from '../ui.js';
import { state } from '../state.js';

export const isPhone = () => !!state.node?.os?.startsWith('Android');
export const here = () => (isPhone() ? 'ce téléphone' : 'ce PC');

function fitText(m, phone) {
  if (m.fits === 'gpu') return ' Tient dans ta carte graphique : rapide.';
  if (m.fits === 'ram') return phone ? ' Tourne sur le processeur du téléphone : lent, mais il répond.'
    : ' Tourne sur le processeur : plus lent qu\'avec une carte graphique.';
  return ' Lourd pour cette machine : très lent.';
}

/** La fenêtre de choix, puis l'installation (son avancement se lit dans progressBar). */
export async function chooseModel() {
  let found;
  try { found = await get('/models/install'); } catch (e) { errorToast(e); return; }
  if (found.progress?.running) {
    toast(`Installation de ${found.progress.model} déjà en cours.`);
    return;
  }
  if (!found.ollama) {
    await modal({
      title: 'Ollama ne répond pas', iconName: 'warn', cancel: null, confirm: 'OK',
      text: found.phone
        ? 'Ollama fait tourner les modèles sur le téléphone : relance AIODOS depuis son application, '
          + 'qui le démarre avec le nœud, puis réessaie.'
        : 'Ollama fait tourner les modèles : lance-le (ou installe-le depuis ollama.com), puis réessaie. '
          + 'AIODOS le retrouve tout seul.',
    });
    return;
  }
  const offered = found.models.filter(m => !m.installed);
  if (!offered.length) { toast('Les modèles conseillés sont déjà installés.'); return; }
  const r = await modal({
    title: 'Installer un modèle', iconName: 'plus', confirm: 'Installer',
    text: `Modèles conseillés pour ${found.phone ? 'ce téléphone' : 'ce PC'}, aux licences libres. `
      + 'Le téléchargement passe par Ollama ; tu peux continuer à te servir d\'AIODOS pendant ce temps.',
    choices: offered.map(m => ({
      value: m.name,
      label: `${m.label}${m.recommended ? ' · conseillé' : ''} · ${bytes(m.size)}`,
      help: `${m.about} Licence ${m.license}.${fitText(m, found.phone)}`,
    })),
  });
  if (!r.ok || !r.choice) return;
  try { await post('/models/install', { name: r.choice }); }
  catch (e) { errorToast(e); }
}

/** Ligne d'avancement (dans le bandeau de la conversation) ; '' quand rien n'est en cours. */
export function progressBar(ev) {
  if (!ev?.running) return '';
  const share = ev.total ? Math.min(100, Math.round((ev.completed / ev.total) * 100)) : 0;
  const amount = ev.total ? ` · ${bytes(ev.completed)} sur ${bytes(ev.total)}` : '';
  return `<div class="banner info install-bar">${icon('plus')}<div style="flex:1">Installation de
    <b>${esc(ev.model)}</b> : ${share} %${amount}<div class="install-track"><i style="width:${share}%"></i></div></div>
    <button class="btn sm ghost" data-install-cancel>Annuler</button></div>`;
}

export async function cancelInstall() {
  try { await del('/models/install'); } catch (e) { errorToast(e); }
}

/** Fin d'une installation : un mot, puis la liste des modèles se relit. → vrai si fini. */
export function finished(ev, reload) {
  if (ev?.running || ev?.done === null || ev?.done === undefined) return false;
  if (ev.done) {
    toast(/^embeddinggemma/.test(ev.model)
      ? `${ev.model} installé : tes bibliothèques fonctionnent sur ${here()}.`
      : `${ev.model} installé : il répond maintenant sur ${here()}.`, 'ok', 6000);
    reload();
  } else if (ev.error) {
    toast(`Installation de ${ev.model} : ${ev.error}`, 'err', 8000);
  }
  return true;
}
