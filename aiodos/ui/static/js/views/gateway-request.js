// La carte d'accord : une application de ce PC demande à utiliser AIODOS
// (docs/api-applications.md § 4.3). Le nom et la raison sont ceux que donne le programme :
// AIODOS ne peut pas les vérifier, il montre donc aussi le programme vu par Windows et le
// code à 6 chiffres, à comparer avec celui que l'appli affiche. L'utilisateur peut décocher
// des droits ou des cercles, jamais en ajouter ; « Autoriser » ne devient cliquable qu'après
// 1,5 s (pas d'accord par une touche tapée ailleurs). Une seule carte à la fois.
// renderRequest est pure et exportée : l'essai d'échappement s'en sert (tests/test_ui_gateway.py).
import { get, post } from '../api.js';
import { esc, h } from '../dom.js';
import { locale, t } from '../i18n.js';
import { icon } from '../icons.js';
import { emit } from '../state.js';
import { toast, errorToast } from '../ui.js';

const ARM_MS = 1500;
// Libellés : des clés de textes (i18n/*/gateway_request.json), traduites à l'affichage
const SCOPES = {
  chat: 'gateway_request.scope_chat',
  embed: 'gateway_request.scope_embed',
  libraries: 'gateway_request.scope_libraries',
};
const CIRCLES = {                    // [libellé, aide]
  here: ['gateway_request.circle_here', ''],
  mine: ['gateway_request.circle_mine', 'gateway_request.circle_mine_help'],
  friends: ['gateway_request.circle_friends', 'gateway_request.circle_friends_help'],
  public: ['gateway_request.circle_public', ''],
};
const CONTENT_NOTICE = 'gateway_request.content_notice';

function served(req, circle) {
  return Object.values(req.served || {}).some(list => list.includes(circle));
}

// Le HTML de la carte, tout échappé (nom, raison, programme viennent de l'appli ou du système).
// name, déjà échappé, va tel quel dans le HTML ; un texte traduit reçoit req.client_name et
// s'échappe en entier
export function renderRequest(req) {
  const name = esc(req.client_name);
  const version = req.client_version ? ` ${esc(req.client_version)}` : '';
  const program = req.program
    ? t('gateway_request.program_seen_html', { program: `<code class="mono">${esc(req.program)}</code>` })
    : `<span class="warn">${esc(t('gateway_request.program_unknown'))}</span>`;
  const generic = req.program_generic
    ? `<div class="warn">${esc(t('gateway_request.program_generic'))}</div>` : '';
  const upgrade = req.upgrade;
  const already = new Set(upgrade ? upgrade.scopes : []);
  const scopes = req.scopes.map(s => `
    <label class="choice"><input type="checkbox" data-scope="${esc(s)}" checked ${already.has(s) ? 'disabled' : ''}>
      <span><b>${esc(SCOPES[s] ? t(SCOPES[s]) : s)}</b>${already.has(s) ? `<small>${esc(t('gateway_request.already_granted'))}</small>` : ''}</span></label>`).join('');
  const circles = upgrade ? '' : req.where.map(c => {
    const [label, help] = CIRCLES[c] ? CIRCLES[c].map(key => (key ? t(key) : '')) : [c, ''];
    const later = served(req, c) ? '' : t('gateway_request.not_served_yet');
    const small = [help, later].filter(Boolean).join(t('gateway_request.notes_join'));
    return `<label class="choice"><input type="checkbox" data-where="${esc(c)}" checked>
      <span><b>${esc(label)}</b>${small ? `<small>${esc(small)}</small>` : ''}</span></label>`;
  }).join('');
  const replace = req.existing ? `<label class="check"><input type="checkbox" data-replace checked>
    ${esc(t('gateway_request.replace', { name: req.client_name,
      date: new Date(req.existing.created * 1000).toLocaleDateString(locale()) }))}</label>` : '';
  const purpose = req.purpose ? `<div class="quote">${esc(t('gateway_request.purpose',
    { name: req.client_name, purpose: req.purpose }))}</div>` : '';
  return `
    <h3>${icon('shield')}${esc(upgrade ? t('gateway_request.title_upgrade', { name: req.client_name })
      : t('gateway_request.title'))}</h3>
    <div class="text"><b>${name}</b>${version}. ${esc(t('gateway_request.name_unverified'))}
${program}</div>${generic}
    <div class="pair-code">${esc(req.user_code)}</div>
    <div class="text">${esc(t('gateway_request.code_check', { name: req.client_name }))}</div>
    ${purpose}
    <div class="text" style="margin-top:14px"><b>${esc(t('gateway_request.can_do', { name: req.client_name }))}</b></div>
    <div class="choices">${scopes}</div>
    ${circles ? `<div class="text" style="margin-top:14px"><b>${esc(t('gateway_request.where_title'))}</b></div>
    <div class="choices">${circles}</div><div class="help">${esc(t(CONTENT_NOTICE))}</div>` : ''}
    <details class="more"><summary>${esc(t('gateway_request.more_settings'))}</summary>
      <label class="check"><input type="checkbox" data-background> ${esc(t('gateway_request.background'))}</label>
      <label class="check">${esc(t('gateway_request.duration'))}
        <select class="input" data-duration>
          <option value="forever">${esc(t('gateway_request.duration_forever'))}</option>
          <option value="30d">${esc(t('gateway_request.duration_30d'))}</option>
          <option value="session">${esc(t('gateway_request.duration_session'))}</option>
        </select></label>
      <label class="check"><input type="checkbox" data-pin ${req.program && !req.program_generic ? '' : 'disabled'}>
        ${esc(t('gateway_request.pin'))}</label>
    </details>
    ${replace}
    <div class="help">${esc(t('gateway_request.opens_apps'))}</div>
    <div class="buttons">
      <button class="btn ghost" data-act="deny">${esc(t('gateway_request.deny'))}</button>
      <button class="btn ghost" data-act="block">${esc(t('gateway_request.deny_block'))}</button>
      <button class="btn primary" data-act="allow" disabled>${esc(t('gateway_request.allow'))}</button>
    </div>
    <div class="help" data-expires></div>`;
}

let showing = null;                  // id de la carte affichée

function remaining(req) {
  const left = Math.max(0, Math.round(req.expires - Date.now() / 1000));
  return t('gateway_request.expires', { min: Math.floor(left / 60), sec: String(left % 60).padStart(2, '0') });
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
    toast(t('gateway_request.allowed', { name: req.client_name }), 'ok', 5000);
  } else if (r.how === 'deny' || r.how === 'block') {
    await post(`/gateway/requests/${encodeURIComponent(req.id)}/deny`, { block: r.how === 'block' });
    toast(t(r.how === 'block' ? 'gateway_request.denied_blocked' : 'gateway_request.denied',
      { name: req.client_name }), 'info');
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
