// Approbation des versions sur un deuxième appareil (updates/approval.py) : la clé de cet
// appareil, les versions qui attendent chez mes machines, et le bouton « Approuver ».
import { get, post } from '../api.js';
import { esc, bytes } from '../dom.js';
import { icon } from '../icons.js';
import { toast, errorToast } from '../ui.js';

export async function renderApproval(box) {
  if (!box) return;
  let a;
  try { a = await get('/updates/approval'); } catch { box.innerHTML = ''; return; }
  const waiting = a.waiting_here.length
    ? `<small class="warn">En attente d'approbation sur cette machine : ${a.waiting_here.map(esc).join(', ')}.
       Approuve-la depuis ton appareil d'approbation (Réglages, Mises à jour) : d'ici là, elle ne part
       chez personne.</small>` : '';
  if (!a.key) {
    box.innerHTML = `<div class="setting-row"><div class="txt"><b>Approbation sur un deuxième appareil</b>
      <small>${a.required
        ? 'Chaque version doit aussi être approuvée par un appareil d\'approbation, en général ton téléphone.'
        : 'Aucun appareil d\'approbation déclaré : la signature de l\'auteur suffit.'}
      Sur ton téléphone, ce bouton crée sa clé d'approbation, qui ne le quitte jamais. Ajoute ensuite
      la ligne qu'il affiche dans aiodos/updates/keys.py.</small>${waiting}</div>
      <button class="btn sm" id="approval-key" type="button">${icon('check')}Approuver depuis cet appareil</button></div>`;
    box.querySelector('#approval-key').onclick = async () => {
      try {
        await post('/updates/approval/key');
        toast('Clé d\'approbation créée sur cet appareil.', 'ok');
        renderApproval(box);
      } catch (e) { errorToast(e); }
    };
    return;
  }
  const declared = a.key.declared
    ? 'Cet appareil approuve les versions d\'AIODOS. Avant d\'approuver, vérifie que c\'est bien toi qui viens de publier.'
    : 'Clé créée, pas encore déclarée : ajoute cette ligne dans APPROVER_KEYS (aiodos/updates/keys.py), fais un commit, puis publie.';
  box.innerHTML = `<div class="setting-row"><div class="txt"><b>Appareil d'approbation · ${esc(a.key.id)}</b>
      <small>${declared}</small>
      ${a.key.declared ? '' : `<code style="display:block;white-space:pre-wrap;word-break:break-all;user-select:all">${esc(a.key.line.trim())}</code>`}
      ${waiting}</div>
      <button class="btn sm" id="approval-look" type="button">${icon('retry')}Versions en attente</button></div>
    <div id="approval-list"></div>`;
  box.querySelector('#approval-look').onclick = () => listPending(box);
}

async function listPending(box) {
  const list = box.querySelector('#approval-list');
  list.innerHTML = '<div class="help">Recherche chez tes machines…</div>';
  let pending;
  try { pending = (await get('/updates/approval/pending')).pending; } catch (e) {
    list.innerHTML = '';
    errorToast(e);
    return;
  }
  if (!pending.length) {
    list.innerHTML = '<div class="help">Aucune version en attente chez tes machines en ligne.</div>';
    return;
  }
  list.innerHTML = pending.map((p, i) => `
    <div class="setting-row"><div class="txt"><b>Version ${esc(p.version)} · ${esc(p.machine)}</b>
      <small>${p.notes ? `${esc(p.notes)} · ` : ''}commit ${esc(p.commit.slice(0, 10))}${p.published
        ? ` · publiée le ${new Date(p.published * 1000).toLocaleString('fr-FR')}` : ''}</small>
      <small>${p.files.map(f => `${esc(f.kind)} : ${esc(f.name)} (${bytes(f.size)}, empreinte ${esc(f.sha256.slice(0, 12))}…)`).join('<br>')}</small></div>
      <button class="btn sm primary" data-approve="${i}" type="button">${icon('check')}Approuver</button></div>`).join('');
  list.querySelectorAll('[data-approve]').forEach(button => {
    button.onclick = async () => {
      const p = pending[Number(button.dataset.approve)];
      button.disabled = true;
      try {
        await post('/updates/approval/approve', { node_id: p.node_id, version: p.version });
        toast(`Version ${p.version} approuvée : elle peut partir chez tes machines.`, 'ok', 6000);
        listPending(box);
      } catch (e) { button.disabled = false; errorToast(e); }
    };
  });
}
