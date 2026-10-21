// Téléphone (écran étroit) : « Plus » dans la barre du bas pour les vues moins courantes ; le
// bouton Retour d'Android ; la barre cachée tant que le clavier est ouvert.
import { $ } from './dom.js';
import { t } from './i18n.js';
import { state, on, emit } from './state.js';
import { menu } from './ui.js';

const MORE = ['library', 'node', 'settings'];        // libellés : nav.json, comme la barre
const phone = matchMedia('(max-width: 760px)');
let listShown = false;          // liste des conversations ouverte par-dessus
let leaving = false;            // retour demandé par l'interface, pas par le bouton Retour

export function mountMobile() {
  const more = $('#rail-more');
  more.onclick = () => menu(more, MORE.map(view => (
    { label: t(`nav.${view}`), icon: view, action: () => emit('view:show', view) })));
  on('view:shown', view => {
    more.classList.toggle('active', MORE.includes(view));
    sync();
  });
  on('chat:list-shown', shown => { listShown = shown; sync(); });
  addEventListener('popstate', () => {
    if (leaving) { leaving = false; return; }
    if (state.view !== 'chat') emit('view:show', 'chat');
    if (listShown) emit('chat:list', false);
  });
  watchKeyboard();
}

// L'écran d'accueil est la conversation. Ailleurs (une autre vue, la liste ouverte), une
// entrée d'historique : le bouton Retour d'Android (WebView.goBack) y ramène.
function sync() {
  const away = state.view !== 'chat' || listShown;
  const pushed = history.state?.aiodos === 'away';
  if (away && !pushed && phone.matches) {
    history.pushState({ aiodos: 'away' }, '');
  } else if (!away && pushed) {
    leaving = true;
    history.back();
  }
}

// Clavier ouvert (écran tactile) : la page rétrécit nettement, à largeur égale. La barre du bas
// laisse alors la place au message, même si le clavier se ferme sans que le champ perde le focus.
function watchKeyboard() {
  if (!matchMedia('(hover: none)').matches) return;     // souris : une fenêtre qu'on redimensionne
  let width = innerWidth;
  let tallest = innerHeight;
  addEventListener('resize', () => {
    if (innerWidth !== width) { width = innerWidth; tallest = innerHeight; }   // rotation
    tallest = Math.max(tallest, innerHeight);
    document.body.classList.toggle('keyboard', innerHeight < tallest * 0.75);
  });
}
