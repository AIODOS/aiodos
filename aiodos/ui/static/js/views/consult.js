// Chercher dans les bibliothèques que mes machines et mes amis m'ouvrent
// (network/remote_search.py) : la question part chez eux, seuls les passages reviennent.
import { get, post } from '../api.js';
import { $, h, esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { errorToast } from '../ui.js';

const SHOWN = 700;                       // caractères d'un passage affichés d'abord
let box;
let machines = [];

export function mountConsult(container) {
  box = h(`
    <div class="card consult" style="margin-top:20px">
      <h2>${icon('search')}${esc(t('consult.title'))}</h2>
      <p class="hint" id="consult-where">…</p>
      <div class="toolbar" style="margin-bottom:0">
        <input class="input" id="consult-q" placeholder="${esc(t('consult.placeholder'))}">
        <button class="btn primary" id="consult-go">${icon('search')}${esc(t('consult.search'))}</button>
      </div>
      <div id="consult-results"></div>
    </div>`);
  container.append(box);
  $('#consult-go', box).onclick = run;
  $('#consult-q', box).onkeydown = e => { if (e.key === 'Enter') run(); };
}

// Les machines nommées, en texte brut (à échapper) : « Portable », « PC de Léa » (ami)
const named = list => list.map(m =>
  t(m.friend ? 'consult.friend_named' : 'consult.machine_named', { name: m.name })).join(', ');

/** Chez qui partirait la question (machines qui m'ouvrent quelque chose). */
export async function refreshConsult() {
  if (!box) return;
  try { ({ machines } = await get('/consult')); } catch { return; }
  $('#consult-where', box).textContent = machines.length
    ? t('consult.where', { machines: named(machines) }) : t('consult.nobody_opens');
  $('#consult-go', box).disabled = !machines.length;
}

async function run() {
  const question = $('#consult-q', box).value.trim();
  if (!question || !machines.length) return;
  const out = $('#consult-results', box);
  out.innerHTML = `<div class="faint consult-status">${esc(t('consult.searching', { machines: named(machines) }))}</div>`;
  $('#consult-go', box).disabled = true;
  // Seulement chez les machines annoncées ci-dessus : une machine arrivée depuis n'est pas
  // interrogée sans que tu l'aies vue
  try { render(await post('/consult', { question, machines: machines.map(m => m.node_id) })); }
  catch (e) { out.innerHTML = ''; errorToast(e); }
  finally { $('#consult-go', box).disabled = !machines.length; }
}

function machineLine(m) {
  const status = m.error ? t('consult.status_error', { error: m.error })
    : m.open === 0 ? t('consult.status_closed')
      : m.count ? t('consult.status_count', { n: m.count }) : t('consult.status_nothing');
  return `<span>${t(m.friend ? 'consult.line_friend_html' : 'consult.line_html',
    { name: esc(m.name), status: esc(status) })}</span>`;
}

function render({ passages, machines: asked }) {
  const out = $('#consult-results', box);
  out.innerHTML = `<div class="consult-status">${asked.map(machineLine).join(' · ') || esc(t('consult.nobody'))}</div>`;
  const list = h('<div class="consult-hits"></div>');
  for (const p of passages) {
    const long = p.text.length > SHOWN;
    const hit = h(`
      <div class="consult-hit">
        <div class="where">${icon('file')}<b>${esc(p.document)}</b>
          <span>${esc(t('consult.hit_library', { library: p.library }))}</span>
          <span class="pill">${icon('network')}${esc(t('consult.hit_peer', { peer: p.peer }))}</span></div>
        <div class="text">${esc(long ? `${p.text.slice(0, SHOWN)}…` : p.text)}</div>
        ${long ? `<button class="btn ghost sm" data-more>${esc(t('consult.whole_passage'))}</button>` : ''}
      </div>`);
    hit.querySelector('[data-more]')?.addEventListener('click', e => {
      hit.querySelector('.text').textContent = p.text;
      e.currentTarget.remove();
    });
    list.append(hit);
  }
  out.append(list);
}
