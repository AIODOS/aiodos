// Réseau → Internet : la connexion par le compagnon aiodos-net, les invitations d'un ami (la
// créer, y répondre, coller sa réponse), la clé de cette machine. Une section repliable de la
// vue Réseau (network.js) : son résumé d'une ligne, son contenu.
import { post, del } from '../api.js';
import { $, esc, ago } from '../dom.js';
import { t, locale } from '../i18n.js';
import { icon } from '../icons.js';
import { state, emit } from '../state.js';
import { modal, toast, errorToast } from '../ui.js';
import { confirmPairing } from './network-pairing.js';

const reload = () => emit('network:reload');

// Relais de n0 : leur région suffit (« euc1-1 ») ; un relais à soi garde son nom entier.
function host(url) {
  try {
    const u = new URL(url);
    return /\.iroh\.link\.?$/.test(u.hostname) ? u.hostname.split('.')[0] : u.host;
  } catch { return url; }
}

/** L'état de la connexion par Internet, en texte brut : « Prête · relais … · clé … ». */
function internetStatus(inet) {
  if (!inet.enabled) return t('network.internet_off');
  if (!inet.running) return inet.error || t('network.internet_starting');
  const parts = [t('network.internet_ready')];
  if (inet.relays?.length) {
    parts.push(t('network.internet_relays', { n: inet.relays.length, relays: inet.relays.map(host).join(', ') }));
  }
  // « (essai : relais seulement) » suit ce qui précède, sans séparateur
  if (inet.relay_only) parts.push(`${parts.pop()} ${t('network.internet_relay_only')}`);
  if (inet.id) parts.push(t('network.internet_key', { key: inet.id.slice(0, 10) }));
  for (const r of inet.denied_by || []) parts.push(t('network.internet_denied', { relay: host(r) }));
  return parts.join(' · ');
}

/** Le résumé de la ligne de titre (HTML) : « prêt, relais euc1-1 · 1 invitation en cours ». */
export function internetSummary(net) {
  const inet = net.internet || {};
  const invitations = (net.invitations || []).length;
  const parts = [];
  if (!inet.enabled) parts.push(esc(t('network.sub_off')));
  else if (!inet.running) {
    parts.push(inet.error ? `<span class="warn">${esc(inet.error)}</span>` : esc(t('network.sub_starting')));
  } else {
    parts.push(esc(inet.relays?.length
      ? t('network.internet_sub_relays', { relays: inet.relays.map(host).join(', ') })
      : t('network.internet_sub_ready')));
  }
  if (invitations) parts.push(`<span class="pill run">${esc(t('network.invitations_pending', { n: invitations }))}</span>`);
  return parts.join(' · ');
}

/** Ce qui ouvre la section d'office : une erreur, une invitation en cours. */
export function internetAttention(net) {
  const inet = net.internet || {};
  const error = inet.enabled && !inet.running && inet.error ? inet.error : '';
  return [error, ...(net.invitations || []).map(inv => inv.id)].filter(Boolean).join('|');
}

function invitationRow(inv) {
  const until = new Date(inv.expires * 1000).toLocaleString(locale(), { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  const name = inv.name || '?';
  const text = inv.role === 'received'
    ? t('network.invitation_received', { name, ago: ago(inv.created) })
    : inv.answered
      ? t('network.invitation_answered', { name })
      : t('network.invitation_sent', { ago: ago(inv.created), until });
  return `<div class="peer"><div class="peer-head">${icon('link')}<span class="net-text">${esc(text)}</span>
    ${inv.role === 'sent' && !inv.answered ? `<button class="btn sm" data-reply="${esc(inv.id)}">${esc(t('network.paste_reply'))}</button>` : ''}
    <button class="icon-btn danger" data-cancel="${esc(inv.id)}" title="${esc(t('network.cancel_invitation'))}">${icon('x')}</button></div></div>`;
}

/** Le contenu de la section : l'état en entier, inviter un ami, les invitations en cours. */
export function renderInternet(box, net) {
  const inet = net.internet || {};
  box.innerHTML = `
    <p class="hint">${esc(internetStatus(inet))}</p>
    <h3 class="net-sub">${icon('link')}${esc(t('network.invite_title'))}</h3>
    <p class="hint">${esc(t('network.invite_hint'))}</p>
    <div class="toolbar net-tools">
      <button class="btn sm" id="inv-create" ${inet.running ? '' : 'disabled'}>${icon('plus')}${esc(t('network.create_invitation'))}</button>
      <button class="btn sm" id="inv-accept" ${inet.running ? '' : 'disabled'}>${icon('link')}${esc(t('network.accept_invitation'))}</button>
      ${inet.running && inet.id ? `<button class="btn sm ghost" id="inet-key" title="${esc(t('network.copy_key_title'))}">${icon('copy')}${esc(t('network.copy_key'))}</button>` : ''}
    </div>
    <div class="peer-list">${(net.invitations || []).map(invitationRow).join('')}</div>`;
  $('#inv-create', box).onclick = createInvitation;
  $('#inet-key', box)?.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(inet.id); toast(t('network.key_copied'), 'ok'); }
    catch { toast(inet.id, 'info', 15000); }
  });
  $('#inv-accept', box).onclick = acceptInvitation;
  box.querySelectorAll('[data-reply]').forEach(b => { b.onclick = () => pasteReply(Number(b.dataset.reply)); });
  box.querySelectorAll('[data-cancel]').forEach(b => {
    b.onclick = async () => {
      try { state.network = await del(`/network/invitations/${b.dataset.cancel}`); reload(); }
      catch (e) { errorToast(e); }
    };
  });
}

/** L'interrupteur de la ligne de titre : relier mes machines par Internet. */
export async function toggleInternet(e) {
  try { await post('/network/internet', { enabled: e.target.checked }); }
  catch (err) { errorToast(err); }
  reload();                       // l'interrupteur reprend l'état réel, réussi ou non
}

async function copyCode(title, text, code, done) {
  const r = await modal({ title, iconName: 'link', text, textarea: code,
    confirm: t('network.copy'), cancel: t('network.close') });
  if (!r.ok) return;
  try { await navigator.clipboard.writeText(code); toast(done, 'ok', 8000); }
  catch { toast(t('network.copy_failed'), 'err', 6000); }
}

async function createInvitation() {
  let created;
  try { created = await post('/network/invitations'); } catch (e) { errorToast(e); return; }
  reload();
  await copyCode(t('network.invitation_title'), t('network.invitation_text'),
    created.code, t('network.invitation_copied'));
}

async function acceptInvitation() {
  const r = await modal({ title: t('network.accept_invitation'), iconName: 'link', textarea: '',
    placeholder: 'AIODOS1-…', text: t('network.accept_text'), confirm: t('network.continue') });
  if (!r.ok || !r.value.trim()) return;
  let answer;
  try { answer = await post('/network/invitations/accept', { code: r.value }); } catch (e) { errorToast(e); return; }
  reload();
  await copyCode(t('network.reply_title', { name: answer.name }), t('network.reply_text', { name: answer.name }),
    answer.reply, t('network.reply_copied'));
}

async function pasteReply(invitationId) {
  const r = await modal({ title: t('network.paste_title'), iconName: 'link', textarea: '', placeholder: 'AIODOS1-…',
    text: t('network.paste_text'), confirm: t('network.pair') });
  if (!r.ok || !r.value.trim()) return;
  let pending;
  try { pending = await post(`/network/invitations/${invitationId}/reply`, { code: r.value }); }
  catch (e) { errorToast(e); return; }
  await confirmPairing(pending, t('network.paste_check'));
}
