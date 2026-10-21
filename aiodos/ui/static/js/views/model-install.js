// Installer un modèle de conversation : les modèles conseillés pour cet appareil (téléphone ou
// PC, carte graphique), aux licences libres, puis le téléchargement (Hugging Face pour le
// moteur intégré, Ollama sinon), dont l'avancement arrive par l'événement « model_install »
// (services/model_install.py).
import { get, post, del } from '../api.js';
import { bytes, esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { modal, toast, errorToast } from '../ui.js';
import { state } from '../state.js';

export const isPhone = () => !!state.node?.os?.startsWith('Android');
export const here = () => t(isPhone() ? 'node.this_phone' : 'node.this_pc');
/** Les modèles tournent dans le moteur intégré (llama.cpp, llm/), pas dans Ollama. */
export const integrated = () => state.node?.ollama?.engine === 'llama.cpp';

function fitText(m, phone) {
  if (m.fits === 'gpu') return t('model_install.fit_gpu');
  if (m.fits === 'moe') return t('model_install.fit_moe');
  if (m.fits === 'ram') return t(phone ? 'model_install.fit_ram_phone' : 'model_install.fit_ram');
  return t('model_install.fit_heavy');
}

// D'où vient le fichier, en une phrase (sur un téléphone : de préférence en Wi-Fi)
function downloadText(phone) {
  if (integrated()) return t(phone ? 'model_install.download_hf_phone' : 'model_install.download_hf');
  return t(phone ? 'model_install.download_ollama_phone' : 'model_install.download_ollama');
}

/** La fenêtre de choix, puis l'installation (son avancement se lit dans progressBar). */
export async function chooseModel() {
  let found;
  try { found = await get('/models/install'); } catch (e) { errorToast(e); return; }
  if (found.progress?.running) {
    toast(t('model_install.already_running', { model: found.progress.model }));
    return;
  }
  if (!found.ollama) {
    await modal({
      title: t(integrated() ? 'model_install.engine_down_title' : 'model_install.ollama_down_title'),
      iconName: 'warn', cancel: null, confirm: t('ui.ok'),
      text: t(integrated() ? 'model_install.engine_down_text'
        : found.phone ? 'model_install.ollama_down_phone' : 'model_install.ollama_down_text'),
    });
    return;
  }
  const offered = found.models.filter(m => !m.installed);
  if (!offered.length) { toast(t('model_install.all_installed')); return; }
  const device = t(found.phone ? 'node.this_phone' : 'node.this_pc');
  const r = await modal({
    title: t('model_install.title'), iconName: 'plus', confirm: t('model_install.install'),
    text: `${t('model_install.intro', { device })} ${downloadText(found.phone)}`,
    choices: offered.map(m => ({
      value: m.name,
      label: `${m.label}${m.recommended ? ` · ${t('model_install.recommended')}` : ''} · ${bytes(m.size)}`,
      help: [m.about, t('model_install.license', { license: m.license }), fitText(m, found.phone)].join(' '),
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
  const amount = ev.total
    ? ` · ${esc(t('model_install.amount', { done: bytes(ev.completed), total: bytes(ev.total) }))}` : '';
  return `<div class="banner info install-bar">${icon('plus')}<div style="flex:1">${
    t('model_install.progress_html', { model: esc(ev.model), percent: share })}${amount}<div class="install-track"><i style="width:${share}%"></i></div></div>
    <button class="btn sm ghost" data-install-cancel>${esc(t('ui.cancel'))}</button></div>`;
}

export async function cancelInstall() {
  try { await del('/models/install'); } catch (e) { errorToast(e); }
}

/** Fin d'une installation : un mot, puis la liste des modèles se relit. → vrai si fini. */
export function finished(ev, reload) {
  if (ev?.running || ev?.done === null || ev?.done === undefined) return false;
  if (ev.done) {
    toast(t(/^embeddinggemma/.test(ev.model) ? 'model_install.installed_embedding'
      : 'model_install.installed_chat', { model: ev.model, here: here() }), 'ok', 6000);
    reload();
  } else if (ev.error) {
    toast(t('model_install.failed', { model: ev.model, error: ev.error }), 'err', 8000);
  }
  return true;
}
