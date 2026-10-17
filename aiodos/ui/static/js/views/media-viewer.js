// Une image ou une vidéo créée, en grand : sa description, ses réglages, où est le fichier, et
// de quoi l'enregistrer, la reprendre ou la supprimer. Ouverte depuis une conversation comme
// depuis la galerie (vue Images et vidéos).
import { del, fileUrl } from '../api.js';
import { h, esc, ago } from '../dom.js';
import { icon } from '../icons.js';
import { toast, errorToast, confirmDanger } from '../ui.js';

// L'adresse porte le nom du fichier : un numéro réutilisé après une suppression ne ressort
// pas l'ancienne image du cache du navigateur
export const fileOf = img => `${fileUrl(`/images/${img.id}/file`)}&v=${encodeURIComponent(img.file)}`;
export const isVideo = img => img.media === 'video';
export const length = img => (img.frames && img.fps ? `${(img.frames / img.fps).toFixed(1).replace('.', ',')} s` : '');
// La fenêtre native d'AIODOS (pywebview) : « Enregistrer sous… » et « Ouvrir le dossier »
export const desktop = () => !!window.pywebview?.api?.save_image;

/** onReuse(img) : « Reprendre » (la description revient dans le message) ; onDeleted(img) ;
 *  onConversation(img) : « Voir la conversation » où elle a été demandée. */
export function openMedia(img, { onReuse, onDeleted, onConversation } = {}) {
  if (!img) return;
  const ext = isVideo(img) ? 'webm' : 'png';
  const media = isVideo(img)
    ? `<video src="${fileOf(img)}" controls autoplay loop playsinline></video>`
    : `<img src="${fileOf(img)}" alt="${esc(img.prompt)}">`;
  const frames = isVideo(img) && img.frames ? ` · ${img.frames} images à ${img.fps} i/s (${length(img)})` : '';
  const back = h(`
    <div class="modal-back img-view" role="dialog" aria-modal="true">
      <div class="img-view-box">
        ${media}
        <div class="img-view-side">
          <div class="img-view-prompt">${esc(img.prompt)}</div>
          <div class="faint">${img.width} × ${img.height}${frames} · graine ${img.seed} · ${img.steps} pas<br>
            ${esc(img.machine || 'cette machine')}${img.seconds ? ` · ${Math.round(img.seconds)} s` : ''} · ${ago(img.created)}</div>
          ${desktop() && img.path ? `<div class="faint img-path" title="${esc(img.path)}">${esc(img.path)}</div>` : ''}
          <div class="img-view-actions">
            ${desktop()
              ? `<button class="btn sm" data-act="save">${icon('file')}Enregistrer sous…</button>
                 <button class="btn sm" data-act="folder">${icon('folder')}Ouvrir le dossier</button>`
              : `<a class="btn sm" href="${fileOf(img)}" download="aiodos-${img.id}.${ext}">${icon('file')}Enregistrer</a>`}
            ${onReuse ? `<button class="btn sm" data-act="reuse">${icon('retry')}Reprendre</button>` : ''}
            ${onConversation && img.conversation_id ? `<button class="btn sm" data-act="talk">${icon('chat')}Voir la conversation</button>` : ''}
            <button class="btn sm danger" data-act="delete">${icon('trash')}Supprimer</button>
            <button class="btn sm ghost" data-act="close">${icon('x')}Fermer</button>
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
      if (where) toast(`Enregistrée : ${where}`, 'ok', 6000);
    } catch (err) { errorToast(err); }
  });
  back.querySelector('[data-act="folder"]')?.addEventListener('click', () => window.pywebview.api.show_image_file(img.id));
  back.querySelector('[data-act="reuse"]')?.addEventListener('click', () => { close(); onReuse(img); });
  back.querySelector('[data-act="talk"]')?.addEventListener('click', () => { close(); onConversation(img); });
  back.querySelector('[data-act="delete"]').onclick = async () => {
    const what = isVideo(img) ? 'cette vidéo' : 'cette image';
    if (!await confirmDanger(`Supprimer ${what} ?`, 'Le fichier est effacé de cette machine.')) return;
    try { await del(`/images/${img.id}`); } catch (err) { errorToast(err); return; }
    close();
    onDeleted?.(img);
  };
  document.body.append(back);
}
