// Chercher dans les bibliothèques que mes machines et mes amis m'ouvrent
// (network/remote_search.py) : la question part chez eux, seuls les passages reviennent.
import { get, post } from '../api.js';
import { $, h, esc } from '../dom.js';
import { icon } from '../icons.js';
import { errorToast } from '../ui.js';

const SHOWN = 700;                       // caractères d'un passage affichés d'abord
let box;
let machines = [];

export function mountConsult(container) {
  box = h(`
    <div class="card consult" style="margin-top:20px">
      <h2>${icon('search')}Chercher chez mes machines et mes amis</h2>
      <p class="hint" id="consult-where">…</p>
      <div class="toolbar" style="margin-bottom:0">
        <input class="input" id="consult-q" placeholder="Ta question, ex. : quand la fusée lunaire décolle-t-elle ?"
          style="flex:1;min-width:240px">
        <button class="btn primary" id="consult-go">${icon('search')}Chercher</button>
      </div>
      <div id="consult-results"></div>
    </div>`);
  container.append(box);
  $('#consult-go', box).onclick = run;
  $('#consult-q', box).onkeydown = e => { if (e.key === 'Enter') run(); };
}

const named = list => list.map(m => `« ${esc(m.name)} »${m.friend ? ' (ami)' : ''}`).join(', ');

/** Chez qui partirait la question (machines qui m'ouvrent quelque chose). */
export async function refreshConsult() {
  if (!box) return;
  try { ({ machines } = await get('/consult')); } catch { return; }
  $('#consult-where', box).innerHTML = machines.length
    ? `Ta question partira chez ${named(machines)} : chacune cherche dans ce qu'elle t'ouvre et ne renvoie que les
       passages trouvés, jamais ses fichiers. Elle n'est envoyée nulle part ailleurs.`
    : 'Aucune de tes machines appairées ne t\'ouvre de bibliothèque pour l\'instant. Chacune le règle dans son '
      + 'menu ⋯ de bibliothèque, « Consultable par… » ; c\'est visible ici dans la minute.';
  $('#consult-go', box).disabled = !machines.length;
}

async function run() {
  const question = $('#consult-q', box).value.trim();
  if (!question || !machines.length) return;
  const out = $('#consult-results', box);
  out.innerHTML = `<div class="faint consult-status">Recherche chez ${named(machines)}…</div>`;
  $('#consult-go', box).disabled = true;
  try { render(await post('/consult', { question })); }
  catch (e) { out.innerHTML = ''; errorToast(e); }
  finally { $('#consult-go', box).disabled = !machines.length; }
}

function machineLine(m) {
  const text = m.error ? `n'a pas pu chercher : ${esc(m.error)}`
    : m.open === 0 ? 'ne t\'ouvre plus rien'
      : m.count ? `${m.count} passage(s)` : 'rien de pertinent';
  return `<span><b>${esc(m.name)}</b>${m.friend ? ' (ami)' : ''} : ${text}</span>`;
}

function render({ passages, machines: asked }) {
  const out = $('#consult-results', box);
  out.innerHTML = `<div class="consult-status">${asked.map(machineLine).join(' · ') || 'Personne à qui demander.'}</div>`;
  const list = h('<div class="consult-hits"></div>');
  for (const p of passages) {
    const long = p.text.length > SHOWN;
    const hit = h(`
      <div class="consult-hit">
        <div class="where">${icon('file')}<b>${esc(p.document)}</b>
          <span>bibliothèque « ${esc(p.library)} »</span>
          <span class="pill">${icon('network')}chez « ${esc(p.peer)} »</span></div>
        <div class="text">${esc(long ? `${p.text.slice(0, SHOWN)}…` : p.text)}</div>
        ${long ? '<button class="btn ghost sm" data-more>Tout le passage</button>' : ''}
      </div>`);
    hit.querySelector('[data-more]')?.addEventListener('click', e => {
      hit.querySelector('.text').textContent = p.text;
      e.currentTarget.remove();
    });
    list.append(hit);
  }
  out.append(list);
}
