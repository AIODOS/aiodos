// Autorisations de la conversation, sous la zone de saisie : bibliothèques, celles des autres,
// mémoire, web et réflexion. Un bouton (avec le nombre d'autorisations actives) ouvre un petit
// panneau où chacune s'active ou se coupe d'un interrupteur, avec sa courte explication.
// Chacune reste un réglage du nœud (PUT /settings), comme quand elles étaient dans l'en-tête.
// En dessous, « Sur mes machines seulement », propre à la conversation (chat-public.js) ; un
// cadenas sur le bouton le dit quand il est activé.
import { put } from '../api.js';
import { h, esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, on } from '../state.js';
import { errorToast } from '../ui.js';
import { bindMineOnly, mineOnly, mineOnlyRow } from './chat-public.js';

// Réglage, icône, nom et explication (chat.json)
const FLAGS = [
  { flag: 'knowledge_enabled', ico: 'book', label: 'chat.libraries', help: 'chat.libraries_title' },
  { flag: 'peer_libraries_enabled', ico: 'network', label: 'chat.peers_chip', help: 'chat.peers_chip_title' },
  { flag: 'conversations_enabled', ico: 'memory', label: 'chat.memory', help: 'chat.memory_title' },
  { flag: 'web_search_enabled', ico: 'globe', label: 'chat.web', help: 'chat.web_title' },
  { flag: 'think', ico: 'think', label: 'chat.thinking', help: 'chat.think_title' },
];

let root = null;              // le bouton et son panneau
let opened = false;

/** Le bouton « Autorisations » et son panneau, au début de `row` (sous la zone de saisie). */
export function mountPermissions(row) {
  root = h(`
    <div class="perm">
      <button class="perm-btn" type="button" aria-haspopup="true" aria-expanded="false"
        title="${esc(t('chat.permissions_title'))}">${icon('shield')}<span class="perm-label">${esc(t('chat.permissions'))}</span><span class="perm-count"></span><span class="perm-lock" hidden title="${esc(t('chat.mine_only_on'))}">${icon('lock')}</span>${icon('chev-right', 'perm-chev')}</button>
      <div class="perm-panel" role="group" aria-label="${esc(t('chat.permissions'))}" hidden>
        <div class="perm-head">${esc(t('chat.permissions'))}</div>
        ${FLAGS.map(f => `
        <label class="perm-row" data-flag="${f.flag}">${icon(f.ico)}
          <span class="txt"><b>${esc(t(f.label))}</b><small>${esc(t(f.help))}</small></span>
          <span class="switch"><input type="checkbox"><span></span></span></label>`).join('')}
        ${mineOnlyRow()}
      </div>
    </div>`);
  row.prepend(root);
  const button = root.querySelector('.perm-btn');
  button.onclick = () => show(!opened);
  bindMineOnly(root, sync);
  root.querySelectorAll('.perm-row[data-flag]').forEach(line => {
    const input = line.querySelector('input');
    input.onchange = () => setFlag(line.dataset.flag, input.checked);
  });
  // Un clic ou le clavier ailleurs le ferme ; Échap aussi, avant tout le reste (une réponse
  // en cours continue, une modification en cours aussi)
  document.addEventListener('pointerdown', e => { if (opened && !root.contains(e.target)) show(false); });
  root.addEventListener('focusout', e => {
    if (opened && e.relatedTarget && !root.contains(e.relatedTarget)) show(false);
  });
  document.addEventListener('keydown', e => {
    if (!opened || e.key !== 'Escape') return;
    e.preventDefault();
    e.stopPropagation();
    show(false);
    button.focus();
  }, true);
  on('settings:loaded', sync);
  sync();
}

function show(open) {
  opened = open;
  root.querySelector('.perm-panel').hidden = !open;
  root.querySelector('.perm-btn').setAttribute('aria-expanded', String(open));
  if (open) sync();
}

/** Les interrupteurs et le bouton d'après les réglages du nœud. */
function sync() {
  if (!root) return;
  const settings = state.settings;
  let active = 0;
  root.querySelector('.perm-lock').hidden = !(settings?.use_public && mineOnly());
  root.querySelectorAll('.perm-row[data-flag]').forEach(line => {
    const enabled = !!settings?.[line.dataset.flag];
    line.querySelector('input').checked = enabled;
    line.classList.toggle('on', enabled);
    active += enabled ? 1 : 0;
  });
  const count = root.querySelector('.perm-count');
  const params = { n: active, total: FLAGS.length };
  count.textContent = settings ? t('chat.permissions_count', params) : '';
  count.title = settings ? t('chat.permissions_active', params) : '';
  count.classList.toggle('some-off', !!settings && active < FLAGS.length);
}

/** Active ou coupe une autorisation ; refusée, l'interrupteur revient où il était. */
async function setFlag(flag, value) {
  if (state.settings && FLAGS.some(f => f.flag === flag)) {
    try { state.settings = await put('/settings', { [flag]: value }); } catch (e) { errorToast(e); }
  }
  sync();
}
