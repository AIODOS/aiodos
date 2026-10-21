// Les applications qui ont accès à AIODOS (Réglages → Applications et autres outils) :
// pour chacune, ses droits, ses cercles, son programme, ce qu'elle a fait (journal sans
// contenu), et de quoi la modifier ou la retirer d'un clic. Plus les clés « à la main » pour
// un outil, montrées une seule fois, et les programmes bloqués.
import { del, get, patch, post } from '../api.js';
import { ago, esc } from '../dom.js';
import { icon } from '../icons.js';
import { emit } from '../state.js';
import { modal, toast, errorToast, confirmDanger } from '../ui.js';

const SCOPE_LABELS = { chat: 'Conversation', embed: 'Embeddings', libraries: 'Bibliothèques' };
const CIRCLE_LABELS = { here: 'ce PC', mine: 'mes machines', friends: 'mes amis', public: 'inconnus' };
const KIND_LABELS = { app: 'Application', manual: 'Outil (clé à la main)' };
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
  return list.map(v => `<span class="chip on">${esc(labels[v] || v)}</span>`).join(' ');
}

function appRow(a) {
  const u = a.usage || {};
  const unused = a.last_used && Date.now() / 1000 - a.last_used > UNUSED_DAYS * 86400;
  const alerts = [
    u.friend_week ? `calculé chez un ami : ${u.friend_week} (7 jours)` : '',
    u.unexpected_program ? `clé utilisée par ${u.unexpected_program.program}` : '',
    unused ? 'inutilisée depuis 90 jours : la retirer ?' : '',
  ].filter(Boolean);
  return `
    <div class="peer" data-app="${esc(a.id)}">
      <div class="peer-head"><b>${esc(a.name)}</b>
        <span class="faint">${esc(a.version || '')} · ${esc(KIND_LABELS[a.kind] || a.kind)}</span>
        <span style="flex:1"></span>
        <button class="btn sm" data-act="log">Journal</button>
        <button class="btn sm" data-act="edit">Modifier</button>
        <button class="btn sm danger" data-act="revoke">Retirer</button></div>
      <div class="help">${chips(a.scopes, SCOPE_LABELS)} · calculs : ${chips(a.where, CIRCLE_LABELS)}
        ${a.priority === 'background' ? ' · priorité basse' : ''}</div>
      <div class="help">${a.exe_path ? `Programme : <code class="mono">${esc(a.exe_path)}</code>${a.exe_pinned ? ' (clé liée)' : ''}` : 'Programme inconnu'}
        · accordé ${esc(ago(a.created))} · ${a.last_used ? `dernière utilisation ${esc(ago(a.last_used))}` : 'jamais utilisé'}
        · ${esc(String(u.today || 0))} aujourd'hui, ${esc(String(u.week || 0))} en 7 jours${u.errors_week ? `, ${esc(String(u.errors_week))} refusées` : ''}</div>
      ${alerts.length ? `<div class="help warn">${alerts.map(esc).join(' · ')}</div>` : ''}
    </div>`;
}

export function appsCard() {
  const list = cache.apps.length ? cache.apps.map(appRow).join('')
    : '<div class="empty" style="padding:14px">Aucune application n\'a encore accès à AIODOS. Elle le '
      + 'demande d\'elle-même (« Se connecter à AIODOS ») : une carte s\'affiche alors ici.</div>';
  const blocked = cache.blocked.length ? `<div class="field"><label>Programmes bloqués</label>
    ${cache.blocked.map(b => `<div class="copy-row"><code class="mono">${esc(b.exe_path)}</code>
      <span class="faint">${esc(b.name || '')}</span>
      <button class="btn sm" data-unblock="${esc(b.exe_path)}">Débloquer</button></div>`).join('')}</div>` : '';
  return `
    <div class="field"><label>Applications et outils qui ont accès</label>
      <div class="peers">${list}</div>
      <div class="inline" style="margin-top:8px"><button class="btn sm" type="button" id="gateway-add-tool">
        ${icon('plus')}Ajouter un outil (clé à la main)</button></div>
      <div class="help">Une appli qui fuit sa clé ne compromet qu'elle : retire-la ici, l'effet est immédiat
        (ses réponses en cours s'arrêtent). Le journal ne garde jamais le contenu des demandes.</div></div>
    ${blocked}`;
}

function checks(name, options, chosen) {
  return Object.entries(options).map(([value, label]) => `
    <label class="choice"><input type="checkbox" name="${name}" value="${esc(value)}" ${chosen.includes(value) ? 'checked' : ''}>
      <span><b>${esc(label)}</b></span></label>`).join('');
}

async function editApp(a) {
  const back = document.createElement('div');
  back.className = 'modal-back';
  back.innerHTML = `<div class="modal" tabindex="-1"><h3>${icon('settings')}${esc(a.name)}</h3>
    <div class="text">Les changements valent tout de suite, avec la même clé.</div>
    <div class="text" style="margin-top:12px"><b>Droits</b></div><div class="choices">${checks('scope', SCOPE_LABELS, a.scopes)}</div>
    <div class="text" style="margin-top:12px"><b>Où ses calculs peuvent se faire</b></div>
    <div class="choices">${checks('where', { here: 'Ce PC', mine: 'Mes machines', friends: 'Mes amis', public: 'Des inconnus du réseau public' }, a.where)}</div>
    <label class="check"><input type="checkbox" name="background" ${a.priority === 'background' ? 'checked' : ''}> Priorité basse (après mes travaux de fond)</label>
    <label class="check"><input type="checkbox" name="pin" ${a.exe_pinned ? 'checked' : ''} ${a.exe_path ? '' : 'disabled'}> Lier la clé à son programme</label>
    <div class="buttons"><button class="btn ghost" data-act="no">Annuler</button><button class="btn primary" data-act="yes">Enregistrer</button></div></div>`;
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
  if (!body.scopes.length || !body.where.length) { toast('Garde au moins un droit et un cercle.', 'err'); return; }
  try { await patch(`/gateway/apps/${encodeURIComponent(a.id)}`, body); toast('Accès modifié.', 'ok'); } catch (e) { errorToast(e); }
  loadApps();
}

async function showLog(a) {
  let rows = [];
  try { rows = await get(`/gateway/apps/${encodeURIComponent(a.id)}/log`); } catch (e) { errorToast(e); return; }
  const text = rows.length ? rows.map(r => `${new Date(r.at * 1000).toLocaleString('fr-FR')}  ${r.route}  `
    + `${r.status}${r.code ? ` ${r.code}` : ''}  ${r.model || ''}${r.machine ? ` · ${r.machine}` : ''}`
    + `${r.place ? ` (${r.place})` : ''}  ${r.ms} ms${r.program ? `  programme : ${r.program}` : ''}`).join('\n')
    : 'Rien pour l\'instant.';
  await modal({ title: `Journal de ${a.name}`, iconName: 'book', quote: text, cancel: null, confirm: 'Fermer' });
}

async function addTool() {
  const name = await modal({ title: 'Ajouter un outil', iconName: 'plug', input: '', placeholder: 'Nom de l\'outil (Open WebUI…)',
    text: 'Une clé à la main pour un outil qui parle « OpenAI » ou « Ollama » : conversation sur ce PC et mes machines, '
      + 'sans les amis. Elle ne sera montrée qu\'une fois.', confirm: 'Créer la clé' });
  if (!name.ok || !name.value.trim()) return;
  try {
    const made = await post('/gateway/apps', { name: name.value.trim(), scopes: ['chat', 'embed'], where: ['here', 'mine'] });
    await modal({ title: 'Clé de l\'outil', iconName: 'lock', text: 'Copie-la maintenant dans l\'outil (comme « clé d\'API ») : '
      + 'AIODOS ne la montrera plus.', quote: made.key, cancel: null, confirm: 'C\'est copié' });
    loadApps();
  } catch (e) { errorToast(e); }
}

export function bindApps(box) {
  box.querySelectorAll('[data-app]').forEach(row => {
    const a = cache.apps.find(x => x.id === row.dataset.app);
    row.querySelector('[data-act="edit"]').onclick = () => editApp(a);
    row.querySelector('[data-act="log"]').onclick = () => showLog(a);
    row.querySelector('[data-act="revoke"]').onclick = async () => {
      if (!await confirmDanger(`Retirer l'accès de ${a.name} ?`, 'Ses demandes en cours s\'arrêtent. Pour revenir, '
        + `${a.name} devra redemander l'accès.`, 'Retirer')) return;
      try { await del(`/gateway/apps/${encodeURIComponent(a.id)}`); toast('Accès retiré.', 'ok'); } catch (e) { errorToast(e); }
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
