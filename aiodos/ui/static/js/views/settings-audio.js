// Réglages → Modèles, l'audio (docs/conception/audio.md) : les modèles de voix, de musique et
// d'effets sonores, du plus puissant au plus modeste dans chaque genre, prêts ou à télécharger
// (Hugging Face, adresse épinglée, empreinte vérifiée) ; une licence restrictive est lue et
// acceptée avant le téléchargement. Les données : GET /audio/models (api/routes/audio.py).
import { get, post, del } from '../api.js';
import { bytes, esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { emit } from '../state.js';
import { errorToast, modal } from '../ui.js';

const GENRES = ['voice', 'music', 'sound'];
const ICONS = { voice: 'mic', music: 'audio', sound: 'speaker' };

let info = null;                 // modèles d'audio (GET /audio/models), null : pas encore lus

/** Les modèles relus ; la section se redessine (le même événement que les images). */
export async function loadAudioModels() {
  try { info = await get('/audio/models'); } catch { info = null; return; }
  emit('image-models:loaded');
}

/** Un téléchargement d'audio en cours (l'avancement se relit). */
export const audioDownloading = () => !!info?.download?.running;

const downloading = m => info?.download?.running && info.download.model === m.id;

function license(m) {
  return `<span class="pill${m.notice ? ' warn' : ''}"${m.notice ? ` title="${esc(m.notice)}"` : ''}>${esc(m.license)}</span>`;
}

function readyRow(m) {
  const where = info.engine.device ? ` · ${esc(t('settings_models.computing_on', { device: info.engine.device }))}` : '';
  return `<div class="img-model ok">${icon('check')}<div><b>${esc(m.label)}</b>
    <span class="pill ok">${esc(t('settings_models.ready'))}</span> ${license(m)} <span class="faint">${bytes(m.size)}${where}</span>
    ${m.helpers.length ? `<div class="faint">${esc(t('settings_models.helpers', { names: m.helpers.join(', ') }))}</div>` : ''}</div></div>`;
}

function toDownload(m) {
  const dl = info.download?.model === m.id ? info.download : null;
  let action;
  if (dl?.running) {
    const pct = dl.total ? Math.floor(100 * dl.done / dl.total) : 0;
    action = `<div class="img-dl"><div class="bar"><i class="fill" style="width:${pct}%"></i></div>
      <span class="faint">${esc(t('settings_models.download_progress', { done: bytes(dl.done),
        total: bytes(dl.total), rate: bytes(dl.rate), source: dl.source || 'Hugging Face' }))}</span>
      <button class="btn sm ghost" type="button" data-audio-cancel>${icon('x')}${esc(t('settings_models.stop'))}</button></div>`;
  } else if (m.state === 'checking') {
    action = `<span class="faint">${esc(t('settings_models.checking'))}</span>`;
  } else {
    const label = m.state === 'bad' ? t('settings_models.download_again')
      : t('settings_models.download_hf', { size: bytes(m.missing_bytes) });
    action = `<div class="img-dl"><button class="btn sm" type="button" data-audio-download="${esc(m.id)}">${icon('download')}${esc(label)}</button>
      ${dl?.error ? `<span class="err-text">${esc(dl.error)}</span>` : ''}</div>`;
  }
  const memory = m.vram ? t(m.fits_here ? 'settings_audio.vram_fits' : 'settings_audio.vram_heavy',
    { size: bytes(m.vram * 1048576) }) : '';
  return `<div class="model-dl">
    <div class="model-dl-head">${icon(ICONS[m.genre] || 'audio')}<b>${esc(m.label)}</b>${license(m)}
      ${m.experimental ? `<span class="pill">${esc(t('settings_audio.experimental'))}</span>` : ''}</div>
    <p class="hint">${esc(m.about)} ${esc(t('settings_models.about_size', { size: bytes(m.size) }))}${memory ? ` ${esc(memory)}` : ''}</p>
    ${m.helpers.length ? `<div class="faint">${esc(t('settings_models.helpers', { names: m.helpers.join(', ') }))}</div>` : ''}
    ${action}</div>`;
}

/** La section : par genre, les modèles prêts, puis ceux à télécharger. */
export function audioModelsHtml() {
  if (!info) return `<div class="faint">${esc(t('settings_models.reading'))}</div>`;
  const engine = info.engine.found ? '' : `
    <div class="banner warn">${icon('warn')}<div>${esc(t('settings_audio.engine_missing'))}</div></div>`;
  return engine + GENRES.map(genre => {
    const models = info.models.filter(m => m.genre === genre).sort((a, b) => a.rank - b.rank);
    const ready = models.filter(m => m.state === 'ready' && !downloading(m));
    const rest = models.filter(m => !ready.includes(m));
    // settings_audio.genre_voice, settings_audio.genre_music, settings_audio.genre_sound
    return `<div class="settings-sub2">${icon(ICONS[genre])}${esc(t(`settings_audio.genre_${genre}`))}</div>
      ${ready.map(readyRow).join('')}${rest.map(toDownload).join('')}`;
  }).join('');
}

export function bindAudio(box) {
  if (!box) return;
  box.querySelectorAll('[data-audio-download]').forEach(b => {
    b.onclick = () => download(b.dataset.audioDownload);
  });
  box.querySelectorAll('[data-audio-cancel]').forEach(b => {
    b.onclick = async () => {
      try { await del('/audio/models/download'); } catch (err) { errorToast(err); }
      loadAudioModels();
    };
  });
}

async function download(id) {
  const model = info?.models.find(m => m.id === id);
  if (model?.notice) {          // licence restrictive : lue et acceptée avant de télécharger
    const { ok } = await modal({ title: t('settings_models.license_title', { name: model.label }),
      text: model.notice, iconName: 'warn', confirm: t('settings_models.license_confirm'),
      cancel: t('ui.cancel') });
    if (!ok) return;
  }
  try { info.download = await post(`/audio/models/${encodeURIComponent(id)}/download`, {}); }
  catch (err) { errorToast(err); return; }
  emit('image-models:loaded');
}
