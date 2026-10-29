// L'audio dans la conversation (chat/audio_creations.py, docs/conception/audio.md) : une voix
// qui lit un texte, une musique, un effet sonore. Le mode « Créer » (chat-create.js) les propose
// à côté des images et des vidéos ; ce module dit ce qui leur est propre : les genres, les
// durées proposées, les voix d'un modèle, et la réponse de l'assistant, avec un lecteur par son,
// le texte lu ou la description, les paroles d'une chanson et la durée. Les modèles :
// GET /audio/models (api/routes/audio.py).
import { esc, duration } from '../dom.js';
import { decimal, t } from '../i18n.js';
import { icon } from '../icons.js';
import { fileOf } from './media-viewer.js';

// Les genres et leur icône ; leurs textes : chat_create.kind_voice, create_voice…
export const AUDIO_KINDS = { voice: { icon: 'mic' }, music: { icon: 'audio' }, sound: { icon: 'speaker' } };
export const isAudioKind = kind => Object.hasOwn(AUDIO_KINDS, kind || '');
// « Automatique » : les durées valables pour tout modèle de ce genre (secondes)
const SECONDS = { music: [10, 20, 30, 60], sound: [3, 5, 10, 20] };
const DEFAULT_SECONDS = { music: 30, sound: 5 };

/** Les durées proposées : celles du modèle choisi, sinon celles du genre. → [[s, libellé]] */
export function secondsFor(kind, model) {
  if (model?.seconds?.length) return model.seconds;
  return (SECONDS[kind] || []).map(n => [n, t('format.seconds', { n })]);
}

export function defaultSeconds(kind, model) {
  return model?.default_seconds || DEFAULT_SECONDS[kind] || 10;
}

/** La durée d'un son : « 9,9 s », « 1 min 05 ». */
export function soundLength(seconds) {
  if (!seconds) return '';
  return seconds < 60 ? t('format.seconds', { n: decimal(Number(seconds).toFixed(1)) }) : duration(seconds);
}

/** Les sons d'une création dans la réponse de l'assistant : le texte lu (ou la description et
 *  les paroles), puis un lecteur par son arrivé, et une place qui attend pour chacun des autres. */
export function audioHtml(media, rows, waiting) {
  const voice = media.kind === 'voice';
  const words = `<div class="audio-words${voice ? ' said' : ''}">${esc(media.prompt || '')}</div>`;
  const lyrics = media.lyrics ? `<details class="audio-lyrics"><summary>${esc(t('chat_audio.lyrics'))}</summary>
      <div>${esc(media.lyrics)}</div></details>` : '';
  const takes = rows.map(row => `
      <div class="audio-take">
        <audio controls preload="metadata" src="${fileOf(row)}"></audio>
        <span class="faint">${esc(soundLength(row.duration))}</span>
      </div>`).join('');
  const slots = `<div class="audio-take audio-slot">${icon(AUDIO_KINDS[media.kind]?.icon || 'audio')}
      <span class="faint">${esc(t('chat_audio.coming'))}</span></div>`.repeat(waiting);
  return `${words}${lyrics}${takes}${slots}`;
}
