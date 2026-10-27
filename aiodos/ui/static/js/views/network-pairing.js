// Les fenêtres d'appairage : celui commencé ici (réseau local ou invitation), la demande reçue
// d'une autre machine, la présentation reçue (appairage par groupe). Appelées depuis la vue
// Réseau et, pour les demandes reçues, quelle que soit la vue ouverte (app.js). Après une
// réponse, la vue Réseau se redessine (« network:reload »).
import { post } from '../api.js';
import { t } from '../i18n.js';
import { state, emit } from '../state.js';
import { modal, toast, errorToast } from '../ui.js';

const reload = () => emit('network:reload');

// Ce qu'est l'autre machine pour celle-ci (network/sharing.py) : choisi sur chacune. Des
// clés de textes, traduites à l'ouverture de la fenêtre (trustChoices)
const TRUST_CHOICES = [
  { value: 'mine', label: 'network.trust_mine', help: 'network.trust_mine_help' },
  { value: 'friend', label: 'network.trust_friend', help: 'network.trust_friend_help' },
];
const trustChoices = () => TRUST_CHOICES.map(c => ({ value: c.value, label: t(c.label), help: t(c.help) }));

/** Appairer une machine détectée sur le réseau local. */
export async function startPairing(v) {
  let pending;
  try { pending = await post('/network/pair', { node_id: v.node_id }); } catch (e) { errorToast(e); return; }
  await confirmPairing(pending, t('network.pair_check'));
}

let pairOpen = 0;                     // fenêtres d'appairage ouvertes en ce moment

/** Ce qu'une fenêtre d'appairage dit en plus de l'autre machine : par où elle est jointe,
 *  si elle est déjà appairée (accepter remplace sa clé), et si une autre demande est ouverte
 *  en même temps (quelqu'un d'autre tente peut-être de s'appairer). Texte brut : modal l'échappe. */
function pairDetails(p) {
  const parts = [];
  if (p.internet) parts.push(t('network.details_internet'));
  else if (p.address) parts.push(t('network.details_address', { address: p.address }));
  if (p.known) parts.push(t('network.details_known', { name: p.known }));
  const others = pairOpen + (state.network?.pairing || [])
    .filter(o => o.node_id !== p.node_id && !o.local_ok).length;
  if (others) parts.push(t('network.details_crowd'));
  return parts.map(part => ` ${part}`).join('');
}

/** La réponse vise la demande montrée (ref) ; replace : remplacer la clé d'une machine déjà appairée. */
const pairAnswer = (p, r) => post('/network/pair/answer', { node_id: p.node_id, accept: r.ok,
  trust: r.choice || 'mine', ref: p.ref, replace: !!p.known && r.ok });

/** Appairage commencé ici (réseau local ou invitation) : code vérifié, confiance choisie. */
export async function confirmPairing(pending, text) {
  const details = pairDetails(pending);
  pairOpen += 1;
  let r;
  try {
    r = await modal({
      title: t('network.pair_with', { name: pending.name }), iconName: 'shield', code: pending.code,
      text: text + details, choices: trustChoices(), danger: !!pending.known,
      confirm: t(pending.known ? 'network.codes_match_replace' : 'network.codes_match'),
      cancel: t('ui.cancel'),
    });
  } finally { pairOpen -= 1; }
  try {
    const snap = await pairAnswer(pending, r);
    const done = snap.peers.some(p => p.node_id === pending.node_id);
    if (r.ok && !done) toast(t('network.confirmed_here'), 'info', 8000);
  } catch (e) { errorToast(e); }
  reload();
}

/** Présentation reçue (appairage par groupe) : une autre machine de la même personne, présentée
 *  par une machine déjà appairée. Fermée sans répondre, elle attend dans « Machines appairées »
 *  (3 minutes) ; une seule question à la fois, les autres attendent là aussi. */
let introOpen = false;

export async function answerIntroduction(ev) {
  if (introOpen) return;
  introOpen = true;
  let r;
  try {
    r = await modal({
      title: t('network.intro_title'), iconName: 'network',
      text: t('network.intro_text', { presenter: ev.presenter, machine: ev.machine }),
      check: t('network.intro_always'),
      confirm: t('network.accept'), cancel: t('network.refuse'), cautious: true,
    });
  } finally { introOpen = false; }
  if (r.how === 'dismiss') return;
  try {
    // La réponse est l'état du réseau, à jour : la suivante s'y trouve, s'il en reste une
    state.network = await post('/network/introductions/answer',
      { intro: ev.intro, accept: r.ok, always: r.ok && r.checked });
  } catch (e) { errorToast(e); reload(); return; }   // elle reste dans « Machines appairées »
  reload();
  const next = (state.network?.introductions || [])[0];
  if (next) answerIntroduction(next);
}

/** Demande reçue d'une autre machine (affichée quelle que soit la vue ouverte). */
export async function answerPairRequest(ev) {
  const details = pairDetails(ev);
  pairOpen += 1;
  let r;
  try {
    r = await modal({
      title: t('network.request_title'), iconName: 'shield', code: ev.code,
      text: t('network.request_text', { name: ev.name }) + details,
      choices: trustChoices(), danger: !!ev.known, cautious: true,
      confirm: t(ev.known ? 'network.accept_replace' : 'network.accept'), cancel: t('network.refuse'),
    });
  } finally { pairOpen -= 1; }
  try {
    await pairAnswer(ev, r);
  } catch (e) { errorToast(e); }
  reload();
}
