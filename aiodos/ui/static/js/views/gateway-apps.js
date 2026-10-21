// Les applications qui ont accès à AIODOS (Réglages → Applications et autres outils) :
// pour chacune, ses droits, ses cercles, son programme, ce qu'elle a fait (journal sans
// contenu), et de quoi la modifier ou la retirer d'un clic. Plus les clés « à la main » pour
// un outil, montrées une seule fois, et les programmes bloqués.
import { del, get, patch, post } from '../api.js';
import { ago, esc } from '../dom.js';
import { locale, t } from '../i18n.js';
import { icon } from '../icons.js';
import { emit } from '../state.js';
import { modal, toast, errorToast, confirmDanger } from '../ui.js';

// Libellés : des clés de textes (i18n/*/gateway_apps.json), traduites à l'affichage
const SCOPE_LABELS = {
  chat: 'gateway_apps.scope_chat', embed: 'gateway_apps.scope_embed', libraries: 'gateway_apps.scope_libraries',
};
const CIRCLE_LABELS = {
  here: 'gateway_apps.circle_here', mine: 'gateway_apps.circle_mine',
  friends: 'gateway_apps.circle_friends', public: 'gateway_apps.circle_public',
};
const KIND_LABELS = { app: 'gateway_apps.kind_app', manual: 'gateway_apps.kind_manual' };
const UNUSED_DAYS = 90;

let cache = { apps: [], blocked: [] };

export async function loadApps() {
  try {
    const [apps, blocked] = await Promise.all([get('/gateway/apps'), get('/gateway/blocked')]);
    cache = { apps, blocked };
  } catch { /* la carte dit « indisponible » */ }
  emit('gateway-apps:loaded');
}

function chips(list, labels) {
  return list.map(v => `<span class="chip on">${esc(labels[v] ? t(labels[v]) : v)}</span>`).join(' ');
}

function appRow(a) {
  const u = a.usage || {};
  const unused = a.last_used && Date.now() / 1000 - a.last_used > UNUSED_DAYS * 86400;
  const alerts = [
    u.friend_week ? t('gateway_apps.alert_friend_week', { n: u.friend_week }) : '',
    u.unexpected_program ? t('gateway_apps.alert_program', { program: u.unexpected_program.program }) : '',
    unused ? t('gateway_apps.alert_unused', { days: UNUSED_DAYS }) : '',
  ].filter(Boolean);
  return `
    <div class="peer" data-app="${esc(a.id)}">
      <div class="peer-head"><b>${esc(a.name)}</b>
        <span class="faint">${esc(a.version || '')} · ${esc(KIND_LABELS[a.kind] ? t(KIND_LABELS[a.kind]) : a.kind)}</span>
        <span style="flex:1"></span>
        <button class="btn sm" data-act="log">${esc(t('gateway_apps.show_log'))}</button>
        <button class="btn sm" data-act="edit">${esc(t('gateway_apps.edit'))}</button>
        <button class="btn sm danger" data-act="revoke">${esc(t('gateway_apps.revoke'))}</button></div>
      <div class="help">${chips(a.scopes, SCOPE_LABELS)} · ${esc(t('gateway_apps.runs_on'))} ${chips(a.where, CIRCLE_LABELS)}
        ${a.priority === 'background' ? ` · ${esc(t('gateway_apps.low_priority'))}` : ''}</div>
      <div class="help">${a.exe_path ? t(a.exe_pinned ? 'gateway_apps.program_pinned_html' : 'gateway_apps.program_html',
        { program: `<code class="mono">${esc(a.exe_path)}</code>` }) : esc(t('gateway_apps.program_unknown'))}
        · ${esc(t('gateway_apps.granted', { when: ago(a.created) }))} · ${esc(a.last_used
          ? t('gateway_apps.last_used', { when: ago(a.last_used) }) : t('gateway_apps.never_used'))}
        · ${esc(t(u.errors_week ? 'gateway_apps.usage_refused' : 'gateway_apps.usage',
          { today: u.today || 0, week: u.week || 0, refused: u.errors_week, n: u.errors_week }))}</div>
      ${alerts.length ? `<div class="help warn">${alerts.map(esc).join(' · ')}</div>` : ''}
    </div>`;
}

export function appsCard() {
  const list = cache.apps.length ? cache.apps.map(appRow).join('')
    : `<div class="empty" style="padding:14px">${esc(t('gateway_apps.empty'))}</div>`;
  const blocked = cache.blocked.length ? `<div class="field"><label>${esc(t('gateway_apps.blocked'))}</label>
    ${cache.blocked.map(b => `<div class="copy-row"><code class="mono">${esc(b.exe_path)}</code>
      <span class="faint">${esc(b.name || '')}</span>
      <button class="btn sm" data-unblock="${esc(b.exe_path)}">${esc(t('gateway_apps.unblock'))}</button></div>`).join('')}</div>` : '';
  return `
    <div class="field"><label>${esc(t('gateway_apps.with_access'))}</label>
      <div class="peers">${list}</div>
      <div class="inline" style="margin-top:8px"><button class="btn sm" type="button" id="gateway-add-tool">
        ${icon('plus')}${esc(t('gateway_apps.add_tool'))}</button></div>
      <div class="help">${esc(t('gateway_apps.help'))}</div></div>
    ${blocked}`;
}

// options : { valeur: clé de son libellé }
function checks(name, options, chosen) {
  return Object.entries(options).map(([value, label]) => `
    <label class="choice"><input type="checkbox" name="${name}" value="${esc(value)}" ${chosen.includes(value) ? 'checked' : ''}>
      <span><b>${esc(t(label))}</b></span></label>`).join('');
}

async function editApp(a) {
  const back = document.createElement('div');
  back.className = 'modal-back';
  back.innerHTML = `<div class="modal" tabindex="-1"><h3>${icon('settings')}${esc(a.name)}</h3>
    <div class="text">${esc(t('gateway_apps.edit_intro'))}</div>
    <div class="text" style="margin-top:12px"><b>${esc(t('gateway_apps.edit_scopes'))}</b></div><div class="choices">${checks('scope', SCOPE_LABELS, a.scopes)}</div>
    <div class="text" style="margin-top:12px"><b>${esc(t('gateway_apps.edit_where'))}</b></div>
    <div class="choices">${checks('where', { here: 'gateway_apps.where_here', mine: 'gateway_apps.where_mine',
      friends: 'gateway_apps.where_friends', public: 'gateway_apps.where_public' }, a.where)}</div>
    <label class="check"><input type="checkbox" name="background" ${a.priority === 'background' ? 'checked' : ''}> ${esc(t('gateway_apps.edit_background'))}</label>
    <label class="check"><input type="checkbox" name="pin" ${a.exe_pinned ? 'checked' : ''} ${a.exe_path ? '' : 'disabled'}> ${esc(t('gateway_apps.edit_pin'))}</label>
    <div class="buttons"><button class="btn ghost" data-act="no">${esc(t('ui.cancel'))}</button><button class="btn primary" data-act="yes">${esc(t('gateway_apps.save'))}</button></div></div>`;
  document.body.append(back);
  const ok = await new Promise(resolve => back.addEventListener('click', e => {
    const act = e.target.closest('[data-act]')?.dataset.act;
    if (act || e.target === back) resolve(act === 'yes');
  }));
  const picked = name => [...back.querySelectorAll(`input[name="${name}"]:checked`)].map(i => i.value);
  const body = { scopes: picked('scope'), where: picked('where'),
    priority: back.querySelector('input[name="background"]').checked ? 'background' : 'interactive',
    exe_pinned: back.querySelector('input[name="pin"]').checked };
  back.remove();
  if (!ok) return;
  if (!body.scopes.length || !body.where.length) { toast(t('gateway_apps.keep_one'), 'err'); return; }
  try { await patch(`/gateway/apps/${encodeURIComponent(a.id)}`, body); toast(t('gateway_apps.changed'), 'ok'); } catch (e) { errorToast(e); }
  loadApps();
}

async function showLog(a) {
  let rows = [];
  try { rows = await get(`/gateway/apps/${encodeURIComponent(a.id)}/log`); } catch (e) { errorToast(e); return; }
  const text = rows.length ? rows.map(r => `${new Date(r.at * 1000).toLocaleString(locale())}  ${r.route}  `
    + `${r.status}${r.code ? ` ${r.code}` : ''}  ${r.model || ''}${r.machine ? ` · ${r.machine}` : ''}`
    + `${r.place ? ` (${r.place})` : ''}  ${r.ms} ms${r.program ? `  ${t('gateway_apps.log_program', { program: r.program })}` : ''}`).join('\n')
    : t('gateway_apps.log_empty');
  await modal({ title: t('gateway_apps.log_title', { name: a.name }), iconName: 'book', quote: text, cancel: null,
    confirm: t('gateway_apps.close') });
}

async function addTool() {
  const name = await modal({ title: t('gateway_apps.add_title'), iconName: 'plug', input: '',
    placeholder: t('gateway_apps.add_placeholder'), text: t('gateway_apps.add_text'), confirm: t('gateway_apps.add_confirm') });
  if (!name.ok || !name.value.trim()) return;
  try {
    const made = await post('/gateway/apps', { name: name.value.trim(), scopes: ['chat', 'embed'], where: ['here', 'mine'] });
    await modal({ title: t('gateway_apps.key_title'), iconName: 'lock', text: t('gateway_apps.key_text'),
      quote: made.key, cancel: null, confirm: t('gateway_apps.key_confirm') });
    loadApps();
  } catch (e) { errorToast(e); }
}

export function bindApps(box) {
  box.querySelectorAll('[data-app]').forEach(row => {
    const a = cache.apps.find(x => x.id === row.dataset.app);
    row.querySelector('[data-act="edit"]').onclick = () => editApp(a);
    row.querySelector('[data-act="log"]').onclick = () => showLog(a);
    row.querySelector('[data-act="revoke"]').onclick = async () => {
      if (!await confirmDanger(t('gateway_apps.revoke_title', { name: a.name }),
        t('gateway_apps.revoke_text', { name: a.name }), t('gateway_apps.revoke'))) return;
      try { await del(`/gateway/apps/${encodeURIComponent(a.id)}`); toast(t('gateway_apps.revoked'), 'ok'); } catch (e) { errorToast(e); }
      loadApps();
    };
  });
  box.querySelectorAll('[data-unblock]').forEach(b => {
    b.onclick = async () => {
      try { await post('/gateway/blocked/remove', { exe_path: b.dataset.unblock }); } catch (e) { errorToast(e); }
      loadApps();
    };
  });
  const add = box.querySelector('#gateway-add-tool');
  if (add) add.onclick = addTool;
}
