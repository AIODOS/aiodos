// Approbation des versions sur un deuxième appareil (updates/approval.py) : la clé de cet
// appareil, les versions qui attendent chez mes machines, et le bouton « Approuver ».
// Chaque ligne montre l'empreinte du texte du manifeste (« digest ») et le clic l'envoie :
// seul le texte montré est signé, même si la machine en montre un autre sous le même
// numéro. Ce cas (« conflict ») est anormal : il est dit en rouge, et l'approbation
// demande une confirmation.
import { get, post } from '../api.js';
import { esc, bytes } from '../dom.js';
import { locale, t } from '../i18n.js';
import { icon } from '../icons.js';
import { toast, errorToast, confirmDanger } from '../ui.js';

const digestOf = p => (typeof p.digest === 'string' ? p.digest : '');
// L'avertissement du nœud, sinon le nôtre (nœud plus ancien)
const warningOf = p => p.warning || t('approval.conflict_short');

export async function renderApproval(box) {
  if (!box) return;
  let a;
  try { a = await get('/updates/approval'); } catch { box.innerHTML = ''; return; }
  const waiting = a.waiting_here.length
    ? `<small class="warn">${esc(t('approval.waiting_here', {
      versions: a.waiting_here.join(', '), n: a.waiting_here.length }))}</small>` : '';
  if (!a.key) {
    box.innerHTML = `<div class="setting-row"><div class="txt"><b>${esc(t('approval.title'))}</b>
      <small>${esc(t(a.required ? 'approval.required' : 'approval.not_required'))}
      ${esc(t('approval.create_key_help'))}</small>${waiting}</div>
      <button class="btn sm" id="approval-key" type="button">${icon('check')}${esc(t('approval.create_key'))}</button></div>`;
    box.querySelector('#approval-key').onclick = async () => {
      try {
        await post('/updates/approval/key');
        toast(t('approval.key_created'), 'ok');
        renderApproval(box);
      } catch (e) { errorToast(e); }
    };
    return;
  }
  const declared = t(a.key.declared ? 'approval.declared' : 'approval.not_declared');
  box.innerHTML = `<div class="setting-row"><div class="txt"><b>${esc(t('approval.device', { id: a.key.id }))}</b>
      <small>${esc(declared)}</small>
      ${a.key.declared ? '' : `<code style="display:block;white-space:pre-wrap;word-break:break-all;user-select:all">${esc(a.key.line.trim())}</code>`}
      ${waiting}</div>
      <button class="btn sm" id="approval-look" type="button">${icon('retry')}${esc(t('approval.look'))}</button></div>
    <div id="approval-list"></div>`;
  box.querySelector('#approval-look').onclick = () => listPending(box);
}

// Notes, commit et date de publication d'une version en attente, sur une ligne
function about(p) {
  const parts = [t('approval.commit', { commit: p.commit.slice(0, 10) })];
  if (p.notes) parts.unshift(p.notes);
  if (p.published) {
    parts.push(t('approval.published', { date: new Date(p.published * 1000).toLocaleString(locale()) }));
  }
  return parts.join(' · ');
}

async function listPending(box) {
  const list = box.querySelector('#approval-list');
  list.innerHTML = `<div class="help">${esc(t('approval.searching'))}</div>`;
  let pending;
  try { pending = (await get('/updates/approval/pending')).pending; } catch (e) {
    list.innerHTML = '';
    errorToast(e);
    return;
  }
  if (!pending.length) {
    list.innerHTML = `<div class="help">${esc(t('approval.none_pending'))}</div>`;
    return;
  }
  list.innerHTML = pending.map((p, i) => `
    <div class="setting-row"><div class="txt"><b>${esc(t('approval.line', { version: p.version, machine: p.machine }))}</b>
      ${p.conflict ? `<small class="warn">${esc(warningOf(p))}</small>` : ''}
      <small>${esc(about(p))}</small>
      <small>${esc(t('approval.digest'))} <code title="${esc(digestOf(p))}">${esc(digestOf(p).slice(0, 16))}…</code></small>
      <small>${p.files.map(f => esc(t('approval.file', { kind: f.kind, name: f.name, size: bytes(f.size),
        digest: f.sha256.slice(0, 12) }))).join('<br>')}</small></div>
      <button class="btn sm${p.conflict ? '' : ' primary'}" data-approve="${i}" type="button">${icon('check')}${esc(t('approval.approve'))}</button></div>`).join('');
  list.querySelectorAll('[data-approve]').forEach(button => {
    button.onclick = async () => {
      const p = pending[Number(button.dataset.approve)];
      if (p.conflict && !await confirmDanger(t('approval.confirm_title'),
        t('approval.confirm_text', { warning: warningOf(p), digest: digestOf(p) }),
        t('approval.confirm_button'))) return;
      button.disabled = true;
      try {
        // L'empreinte de la ligne choisie : le nœud ne signe que ce texte-là
        await post('/updates/approval/approve', { node_id: p.node_id, version: p.version,
          digest: digestOf(p) });
        toast(t('approval.approved', { version: p.version }), 'ok', 6000);
        listPending(box);
      } catch (e) { button.disabled = false; errorToast(e); }
    };
  });
}
