// La charte et les conditions d'utilisation (aiodos/legal, GET /api/legal) : à accepter à la
// première ouverture quand l'installeur Windows ne l'a pas fait (appli Android, copie lancée
// depuis les sources), et de nouveau à chaque nouvelle version des textes ; à relire dans
// Réglages → À propos.
import { get, post } from '../api.js';
import { $, h, esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on } from '../state.js';
import { errorToast } from '../ui.js';

const load = () => get('/legal');
const LOGO = '/logo.png?v=serpents';

/** Les textes, l'un après l'autre (déjà rendus par le nœud : render.to_html). */
const texts = docs => docs.map(d => `<section><h1>${esc(d.title)}</h1>${d.html}</section>`).join('');

/** À l'ouverture : l'accueil, puis la charte et les conditions, tant qu'elles ne sont pas
 *  acceptées. Par-dessus tout le reste, sans autre issue que les accepter. */
export async function checkTerms() {
  let legal;
  try { legal = await load(); } catch { return; }
  if (legal.accepted >= legal.version) return;
  const first = !legal.accepted;
  const back = h(`
    <div class="terms-back" role="dialog" aria-modal="true" aria-labelledby="terms-title">
      <div class="terms">
        <div class="terms-head">
          <img class="terms-logo" src="${LOGO}" alt="">
          <div><h2 id="terms-title">${esc(t(first ? 'legal.welcome' : 'legal.changed'))}</h2>
            <p>${esc(t(first ? 'legal.intro' : 'legal.changed_intro'))}</p></div>
        </div>
        <div class="terms-text md" tabindex="0">${texts(legal.docs)}</div>
        <label class="check"><input type="checkbox"> ${esc(t('legal.agree'))}</label>
        <div class="buttons"><button class="btn primary" disabled>${esc(t('legal.continue'))}</button></div>
      </div>
    </div>`);
  const agree = $('input', back);
  const yes = $('button', back);
  agree.onchange = () => { yes.disabled = !agree.checked; };
  yes.onclick = async () => {
    yes.disabled = true;
    try {
      await post('/legal/accept', { version: legal.version });
    } catch (err) {
      errorToast(err);
      back.remove();
      checkTerms();                     // les textes ont changé entre-temps : les nouveaux
      return;
    }
    back.remove();
  };
  document.body.append(back);
  $('.terms-text', back).focus();
}

/** Un des textes à relire (« charter », « terms »). */
async function read(id) {
  let legal;
  try { legal = await load(); } catch (err) { errorToast(err); return; }
  const doc = legal.docs.find(d => d.id === id);
  if (!doc) return;
  const back = h(`
    <div class="terms-back" role="dialog" aria-modal="true">
      <div class="terms">
        <div class="terms-text md" tabindex="0">${texts([doc])}</div>
        <div class="buttons"><button class="btn primary">${esc(t('legal.close'))}</button></div>
      </div>
    </div>`);
  const close = () => { back.remove(); document.removeEventListener('keydown', onKey, true); };
  const onKey = e => { if (e.key === 'Escape') { e.preventDefault(); close(); } };
  $('button', back).onclick = close;
  back.addEventListener('click', e => { if (e.target === back) close(); });
  document.addEventListener('keydown', onKey, true);
  document.body.append(back);
  $('.terms-text', back).focus();
}

/** Réglages → À propos : la version, la licence, et les deux textes à relire. */
export function mountAbout(box) {
  if (!box) return;
  const render = accepted => {
    const version = state.node?.version;
    box.innerHTML = `
      <div class="about">
        <img class="about-logo" src="${LOGO}" alt="">
        <div>
          ${version ? `<b>${esc(t('legal.about_version', { version }))}</b>` : ''}
          <p class="hint">${esc(t('legal.license'))}</p>
          ${accepted ? `<p class="hint">${esc(t('legal.accepted_version', { version: accepted }))}</p>` : ''}
        </div>
      </div>
      <div class="row-buttons">
        <button class="btn" data-legal="charter">${icon('shield')}${esc(t('legal.read_charter'))}</button>
        <button class="btn" data-legal="terms">${icon('book')}${esc(t('legal.read_terms'))}</button>
      </div>`;
    box.querySelectorAll('[data-legal]').forEach(b => { b.onclick = () => read(b.dataset.legal); });
  };
  let accepted = 0;
  render(accepted);
  load().then(legal => { accepted = legal.accepted; render(accepted); }).catch(() => {});
  on('node:loaded', () => { if (box.isConnected) render(accepted); });
}
