// Le modèle d'embeddings (knowledge/embed_model.py) vu par la bibliothèque et la vue Tâches :
// absent de cette machine, il retient l'indexation ; ce bandeau dit quoi faire, avec un bouton
// pour chaque modèle d'embeddings déjà là, le téléchargement de celui du réglage (après un
// clic et la confirmation de sa taille et de sa licence), et le chemin vers le réglage. Pris
// d'office au premier indexage, il le dit aussi, jusqu'à ce qu'on ferme la notice.
import { get, put, post, del } from '../api.js';
import { bytes, esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, emit } from '../state.js';
import { modal, toast, errorToast } from '../ui.js';

const SHOWN = 3;                     // modèles proposés d'un clic, au plus

/** L'état du modèle d'embeddings (state.embed), relu au nœud ; « embed:loaded » ensuite. */
export async function loadEmbed() {
  try { state.embed = await get('/knowledge/embed'); } catch { return; }
  emit('embed:loaded');
}

/** Le chemin vers le réglage : Réglages, Connaissances, ouverte. */
export function openEmbedSetting() {
  emit('view:show', 'settings');
  emit('settings:focus', 'knowledge');
}

/** Le bandeau dans `box` (vide s'il n'y a rien à dire). */
export function renderEmbedBanner(box) {
  if (!box) return;
  const e = state.embed;
  box.innerHTML = '';
  if (!e) return;
  if (e.blocked || e.present === false) box.append(missingBanner(e));
  if (e.notice) box.append(noticeBanner(e.notice));
}

function missingBanner(e) {
  const model = e.blocked || e.model;
  const offered = (e.candidates || []).slice(0, SHOWN);
  const lines = [t('embed_model.banner', { model })];
  if (e.indexed) lines.push(t('embed_model.banner_vectors'));
  if (!offered.length) lines.push(t('embed_model.no_other'));
  const box = document.createElement('div');
  box.className = 'banner warn wrap';
  box.innerHTML = `${icon('warn')}<div class="banner-text">${lines.map(l => esc(l)).join('<br>')}</div>
    <div class="banner-actions">
      ${offered.map(name => `<button class="btn sm" data-use="${esc(name)}">${icon('check')}${esc(t('embed_model.use', { model: name }))}</button>`).join('')}
      ${e.download ? `<button class="btn sm" data-download>${icon('download')}${esc(t('embed_model.download', { model: e.download.name }))}</button>` : ''}
      <button class="btn sm ghost" data-settings>${icon('settings')}${esc(t('embed_model.settings'))}</button>
    </div>`;
  box.querySelectorAll('[data-use]').forEach(b => { b.onclick = () => useModel(b.dataset.use, e); });
  box.querySelector('[data-download]')?.addEventListener('click', () => download(e.download));
  box.querySelector('[data-settings]').onclick = openEmbedSetting;
  return box;
}

function noticeBanner(notice) {
  const box = document.createElement('div');
  box.className = 'banner info wrap';
  box.innerHTML = `${icon('info')}<div class="banner-text">${esc(t('embed_model.auto', { missing: notice.missing, model: notice.model }))}</div>
    <div class="banner-actions">
      <button class="btn sm" data-ok>${icon('check')}${esc(t('embed_model.auto_ok'))}</button>
      <button class="btn sm ghost" data-settings>${icon('settings')}${esc(t('embed_model.settings'))}</button>
    </div>`;
  box.querySelector('[data-ok]').onclick = async () => {
    try { await del('/knowledge/embed/notice'); } catch (err) { errorToast(err); }
    loadEmbed();
  };
  box.querySelector('[data-settings]').onclick = openEmbedSetting;
  return box;
}

// Un modèle déjà là devient le modèle d'embeddings (le nœud lance la réindexation s'il faut)
async function useModel(name, e) {
  if (e.indexed) {
    const r = await modal({ title: t('embed_model.use_title'), iconName: 'retry',
      text: t('embed_model.use_text', { model: name }), confirm: t('embed_model.use_confirm') });
    if (!r.ok) return;
  }
  try {
    state.settings = await put('/settings', { embed_model: name });
    emit('settings:loaded');
    toast(t('embed_model.used', { model: name }), 'ok');
  } catch (err) { errorToast(err); }
  loadEmbed();
}

// Le téléchargement : seulement après ce clic et la confirmation (taille, licence)
async function download(offer) {
  const r = await modal({ title: t('embed_model.download_title', { model: offer.name }),
    iconName: 'download', confirm: t('embed_model.download_confirm'),
    text: t('embed_model.download_text', { size: bytes(offer.size), license: offer.license }) });
  if (!r.ok) return;
  try {
    await post('/models/install', { name: offer.name });
    toast(t('embed_model.download_started', { model: offer.name }), 'ok', 6000);
  } catch (err) { errorToast(err); }
}
