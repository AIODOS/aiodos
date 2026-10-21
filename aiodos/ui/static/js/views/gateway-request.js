// La carte d'accord : une application de ce PC demande à utiliser AIODOS
// (docs/api-applications.md § 4.3). Le nom et la raison sont ceux que donne le programme :
// AIODOS ne peut pas les vérifier, il montre donc aussi le programme vu par Windows et le
// code à 6 chiffres, à comparer avec celui que l'appli affiche. L'utilisateur peut décocher
// des droits ou des cercles, jamais en ajouter ; « Autoriser » ne devient cliquable qu'après
// 1,5 s (pas d'accord par une touche tapée ailleurs). Une seule carte à la fois.
// renderRequest est pure et exportée : l'essai d'échappement s'en sert (tests/test_ui_gateway.py).
import { get, post } from '../api.js';
import { esc, h } from '../dom.js';
import { icon } from '../icons.js';
import { emit } from '../state.js';
import { toast, errorToast } from '../ui.js';

const ARM_MS = 1500;
const SCOPES = {
  chat: 'Converser avec tes modèles (textes, images jointes, outils de l\'application)',
  embed: 'Calculer des embeddings (représenter des textes pour les retrouver), sur ce PC',
  libraries: 'Lire des passages de tes bibliothèques',
  'engines:comfyui': 'Faire tourner ses travaux ComfyUI (images, vidéos) sur le ComfyUI de ce PC, accordé dans '
    + 'Réglages → Moteurs des applications',
};
const CIRCLES = {
  here: ['Ce PC', ''],
  mine: ['Mes machines', 'celles que j\'ai appairées comme « mes machines »'],
  friends: ['Chez mes amis qui prêtent leur puissance', 'seulement si « Mode automatique : aussi chez mes amis » est activé, ou si l\'appli désigne une machine'],
  public: ['Chez des inconnus du réseau public', ''],
};
const CONTENT_NOTICE = 'Les textes, images et documents envoyés par l\'application partent chez la machine '
  + 'qui calcule, le temps du calcul, sans y être gardés : décoche un cercle pour l\'exclure. AIODOS n\'envoie '
  + 'rien dans le cloud. Ce que l\'application fait ensuite de ce qu\'elle reçoit ne dépend que d\'elle.';

function served(req, circle) {
  return Object.values(req.served || {}).some(list => list.includes(circle));
}

// Le HTML de la carte, tout échappé (nom, raison, programme viennent de l'appli ou du système)
export function renderRequest(req) {
  const name = esc(req.client_name);
  const version = req.client_version ? ` ${esc(req.client_version)}` : '';
  const program = req.program
    ? `Programme vu par Windows : <code class="mono">${esc(req.program)}</code>, sous ton compte Windows.`
    : '<span class="warn">Programme inconnu : AIODOS n\'a pas pu voir quel programme demande.</span>';
  const generic = req.program_generic
    ? '<div class="warn">Programme générique (interpréteur) : n\'importe quel script peut s\'en servir.</div>' : '';
  const upgrade = req.upgrade;
  const already = new Set(upgrade ? upgrade.scopes : []);
  const scopes = req.scopes.map(s => `
    <label class="choice"><input type="checkbox" data-scope="${esc(s)}" checked ${already.has(s) ? 'disabled' : ''}>
      <span><b>${esc(SCOPES[s] || s)}</b>${already.has(s) ? '<small>déjà accordé</small>' : ''}</span></label>`).join('');
  const circles = upgrade ? '' : req.where.map(c => {
    const [label, help] = CIRCLES[c] || [c, ''];
    const later = served(req, c) ? '' : 'pas encore servi par cette version : il te sera redemandé à son arrivée';
    const small = [help, later].filter(Boolean).join(' ; ');
    return `<label class="choice"><input type="checkbox" data-where="${esc(c)}" checked>
      <span><b>${esc(label)}</b>${small ? `<small>${esc(small)}</small>` : ''}</span></label>`;
  }).join('');
  const replace = req.existing ? `<label class="check"><input type="checkbox" data-replace checked>
    Remplacer l'accès actuel de ${name} (accordé le ${esc(new Date(req.existing.created * 1000).toLocaleDateString('fr-FR'))})</label>` : '';
  const purpose = req.purpose ? `<div class="quote">Raison donnée par ${name} : « ${esc(req.purpose)} »</div>` : '';
  return `
    <h3>${icon('shield')}${upgrade ? `${name} demande des droits en plus` : 'Une application demande à utiliser AIODOS'}</h3>
    <div class="text"><b>${name}</b>${version}. C'est le nom que donne le programme : AIODOS ne peut pas le vérifier.
${program}</div>${generic}
    <div class="pair-code">${esc(req.user_code)}</div>
    <div class="text">Ce code doit s'afficher en ce moment dans ${name}. S'il est différent, ou si tu n'as rien demandé, refuse.</div>
    ${purpose}
    <div class="text" style="margin-top:14px"><b>Ce que ${name} pourra faire</b></div>
    <div class="choices">${scopes}</div>
    ${circles ? `<div class="text" style="margin-top:14px"><b>Où ses calculs peuvent se faire</b></div>
    <div class="choices">${circles}</div><div class="help">${esc(CONTENT_NOTICE)}</div>` : ''}
    <details class="more"><summary>Plus de réglages</summary>
      <label class="check"><input type="checkbox" data-background> Ses demandes passent après mes travaux de fond (priorité basse)</label>
      <label class="check">Durée de l'accès :
        <select class="input" data-duration>
          <option value="forever">jusqu'à ce que je le retire</option>
          <option value="30d">30 jours</option>
          <option value="session">jusqu'à la fermeture d'AIODOS</option>
        </select></label>
      <label class="check"><input type="checkbox" data-pin ${req.program && !req.program_generic ? '' : 'disabled'}>
        Lier la clé à ce programme : refuser tout autre programme qui s'en servirait</label>
    </details>
    ${replace}
    <div class="help">AIODOS s'ouvrira aux applications de ce PC (127.0.0.1 seulement).</div>
    <div class="buttons">
      <button class="btn ghost" data-act="deny">Refuser</button>
      <button class="btn ghost" data-act="block">Refuser et bloquer ce programme</button>
      <button class="btn primary" data-act="allow" disabled>Autoriser</button>
    </div>
    <div class="help" data-expires></div>`;
}

let showing = null;                  // id de la carte affichée

function remaining(req) {
  const left = Math.max(0, Math.round(req.expires - Date.now() / 1000));
  return `Expire dans ${Math.floor(left / 60)} min ${String(left % 60).padStart(2, '0')} s.`;
}

function choices(card, req) {
  const scopes = [...card.querySelectorAll('[data-scope]')]
    .filter(i => i.checked || i.disabled).map(i => i.dataset.scope);
  const where = [...card.querySelectorAll('[data-where]')].filter(i => i.checked).map(i => i.dataset.where);
  return {
    scopes, where: req.upgrade ? null : where,
    priority: card.querySelector('[data-background]').checked ? 'background' : 'interactive',
    duration: card.querySelector('[data-duration]').value,
    pin: card.querySelector('[data-pin]').checked,
    replace: card.querySelector('[data-replace]')?.checked ?? true,
  };
}

function open(req) {
  return new Promise(resolve => {
    const back = h(`<div class="modal-back" role="dialog" aria-modal="true">
      <div class="modal gateway-request" tabindex="-1">${renderRequest(req)}</div></div>`);
    const card = back.querySelector('.modal');
    const allow = card.querySelector('[data-act="allow"]');
    const expires = card.querySelector('[data-expires]');
    const tick = setInterval(() => {
      expires.textContent = remaining(req);
      if (Date.now() / 1000 >= req.expires) finish('expired');
    }, 1000);
    expires.textContent = remaining(req);
    const check = () => {                       // au moins un droit, et un cercle s'il y en a
      const c = choices(card, req);
      allow.dataset.empty = (!c.scopes.length || (c.where && !c.where.length)) ? '1' : '';
      if (allow.dataset.empty) allow.disabled = true;
      else if (!allow.dataset.waiting) allow.disabled = false;
    };
    allow.dataset.waiting = '1';
    setTimeout(() => { delete allow.dataset.waiting; check(); }, ARM_MS);
    card.querySelectorAll('input[type="checkbox"]').forEach(i => { i.onchange = check; });
    function finish(how) {
      clearInterval(tick);
      back.remove();
      resolve({ how, choices: how === 'allow' ? choices(card, req) : null });
    }
    card.addEventListener('click', e => {
      const act = e.target.closest('[data-act]')?.dataset.act;
      if (act && !(act === 'allow' && allow.disabled)) finish(act);
    });
    document.body.append(back);
    card.focus();                              // jamais Entrée pour accepter
  });
}

async function answer(req, r) {
  if (r.how === 'allow') {
    await post(`/gateway/requests/${encodeURIComponent(req.id)}/approve`, r.choices);
    toast(`${req.client_name} peut maintenant utiliser AIODOS.`, 'ok', 5000);
  } else if (r.how === 'deny' || r.how === 'block') {
    await post(`/gateway/requests/${encodeURIComponent(req.id)}/deny`, { block: r.how === 'block' });
    toast(r.how === 'block' ? `${req.client_name} refusé, et son programme bloqué.` : `${req.client_name} refusé.`, 'info');
  }
}

// À chaque demande (événement « gateway_access » du bus) : la plus ancienne qui attend
export async function showRequests() {
  if (showing) return;
  let pending;
  try { pending = await get('/gateway/requests'); } catch { return; }
  const req = pending[0];
  if (!req) return;
  showing = req.id;
  try {
    const r = await open(req);
    await answer(req, r);
  } catch (e) { errorToast(e); } finally {
    showing = null;
    emit('gateway:changed');
  }
  showRequests();                              // la suivante, s'il y en a
}
