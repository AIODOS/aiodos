// Une image ou une vidéo créée, en grand : sa description, ses réglages, où est le fichier, et
// de quoi l'enregistrer, la reprendre ou la supprimer. Ouverte depuis une conversation comme
// depuis la galerie (vue Images et vidéos).
import { del, fileUrl } from '../api.js';
import { h, esc, ago } from '../dom.js';
import { decimal, t } from '../i18n.js';
import { icon } from '../icons.js';
import { toast, errorToast, confirmDanger } from '../ui.js';

// L'adresse porte le nom du fichier : un numéro réutilisé après une suppression ne ressort
// pas l'ancienne image du cache du navigateur
export const fileOf = img => `${fileUrl(`/images/${img.id}/file`)}&v=${encodeURIComponent(img.file)}`;
// Une vignette (WebP, 768 pixels au plus de côté) pour une tuile : le PNG entier ne sert
// qu'en grand et pour l'enregistrer
export const thumbOf = (img, size = 768) => (img.media === 'video' ? fileOf(img) : `${fileOf(img)}&size=${size}`);
export const isVideo = img => img.media === 'video';
export const length = img => (img.frames && img.fps
  ? t('format.seconds', { n: decimal((img.frames / img.fps).toFixed(1)) }) : '');
// La fenêtre native d'AIODOS (pywebview) : « Enregistrer sous… » et « Ouvrir le dossier »
export const desktop = () => !!window.pywebview?.api?.save_image;

/** onReuse(img) : « Reprendre » (la description revient dans le message) ; onDeleted(img) ;
 *  onConversation(img) : « Voir la conversation » où elle a été demandée. */
export function openMedia(img, { onReuse, onDeleted, onConversation } = {}) {
  if (!img) return;
  // L'extension du fichier gardé : une création de ComfyUI peut être un MP4, un WebP…
  const ext = /\.([a-z0-9]{2,5})$/i.exec(img.file || '')?.[1] || (isVideo(img) ? 'webm' : 'png');
  const media = isVideo(img)
    ? `<video src="${fileOf(img)}" controls autoplay loop playsinline></video>`
    : `<img src="${fileOf(img)}" alt="${esc(img.prompt)}">`;
  const frames = isVideo(img) && img.frames
    ? ` · ${esc(t('media_viewer.frames', { frames: img.frames, fps: img.fps, length: length(img) }))}` : '';
  const back = h(`
    <div class="modal-back img-view" role="dialog" aria-modal="true">
      <div class="img-view-box">
        ${media}
        <div class="img-view-side">
          <div class="img-view-prompt">${esc(img.prompt)}</div>
          <div class="faint">${img.width} × ${img.height}${frames} · ${esc(t('media_viewer.seed', { seed: img.seed }))} · ${esc(t('media_viewer.steps', { n: img.steps }))}<br>
            ${esc(img.machine || t('chat.this_machine'))}${img.seconds ? ` · ${esc(t('format.seconds', { n: Math.round(img.seconds) }))}` : ''} · ${ago(img.created)}</div>
          ${desktop() && img.path ? `<div class="faint img-path" title="${esc(img.path)}">${esc(img.path)}</div>` : ''}
          <div class="img-view-actions">
            ${desktop()
              ? `<button class="btn sm" data-act="save">${icon('file')}${esc(t('media_viewer.save_as'))}</button>
                 <button class="btn sm" data-act="folder">${icon('folder')}${esc(t('media_viewer.open_folder'))}</button>`
              : `<a class="btn sm" href="${fileOf(img)}" download="aiodos-${img.id}.${ext}">${icon('file')}${esc(t('media_viewer.save'))}</a>`}
            ${onReuse ? `<button class="btn sm" data-act="reuse">${icon('retry')}${esc(t('media_viewer.reuse'))}</button>` : ''}
            ${onConversation && img.conversation_id ? `<button class="btn sm" data-act="talk">${icon('chat')}${esc(t('media_viewer.see_conversation'))}</button>` : ''}
            <button class="btn sm danger" data-act="delete">${icon('trash')}${esc(t('ui.delete'))}</button>
            <button class="btn sm ghost" data-act="close">${icon('x')}${esc(t('media_viewer.close'))}</button>
          </div>
        </div>
      </div>
    </div>`);
  const close = () => { back.remove(); document.removeEventListener('keydown', onKey, true); };
  const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  document.addEventListener('keydown', onKey, true);
  back.onclick = e => { if (e.target === back) close(); };
  back.querySelector('[data-act="close"]').onclick = close;
  back.querySelector('[data-act="save"]')?.addEventListener('click', async () => {
    try {
      const where = await window.pywebview.api.save_image(img.id);
      if (where) toast(t('media_viewer.saved', { where }), 'ok', 6000);
    } catch (err) { errorToast(err); }
  });
  back.querySelector('[data-act="folder"]')?.addEventListener('click', () => window.pywebview.api.show_image_file(img.id));
  back.querySelector('[data-act="reuse"]')?.addEventListener('click', () => { close(); onReuse(img); });
  back.querySelector('[data-act="talk"]')?.addEventListener('click', () => { close(); onConversation(img); });
  back.querySelector('[data-act="delete"]').onclick = async () => {
    const title = t(isVideo(img) ? 'media_viewer.delete_video' : 'media_viewer.delete_image');
    if (!await confirmDanger(title, t('media_viewer.delete_text'))) return;
    try { await del(`/images/${img.id}`); } catch (err) { errorToast(err); return; }
    close();
    onDeleted?.(img);
  };
  document.body.append(back);
}
