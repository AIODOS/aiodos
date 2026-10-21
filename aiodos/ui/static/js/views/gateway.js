// Applications et autres outils (Réglages) : la passerelle d'AIODOS, sur cette machine
// seulement. Les applications (PANDORA, ARKLIGHT PC…) y ont chacune leur clé, accordée par
// la carte d'accord (gateway-request.js) et gérée ici (gateway-apps.js) ; les autres outils
// (Open WebUI, Continue, assistants MCP) passent par la clé générale, à copier ou renouveler.
import { get, post } from '../api.js';
import { esc } from '../dom.js';
import { icon } from '../icons.js';
import { state, emit } from '../state.js';
import { modal, toast, errorToast } from '../ui.js';
import { appsCard, bindApps, loadApps } from './gateway-apps.js';

export async function loadGateway() {
  try { state.gateway = await get('/gateway'); } catch { return; }
  emit('gateway:loaded');
  loadApps();
}

function stateLine(g) {
  if (g.error) return `<small class="warn">${esc(g.error)}</small>`;
  if (g.running) return `<small class="ok">Ouverte sur ${esc(g.urls.ollama)}, pour cette machine seulement.</small>`;
  return '<small>Fermée : aucune application ni aucun outil ne peut joindre AIODOS.</small>';
}

function switchRow(id, title, help, on) {
  return `<div class="setting-row"><div class="txt"><b>${esc(title)}</b><small>${esc(help)}</small></div>
    <label class="switch"><input type="checkbox" id="${id}" ${on ? 'checked' : ''}><span></span></label></div>`;
}

export function appsSection() {
  const g = state.gateway;
  if (!g) return '';
  return `
    ${switchRow('gateway-apps', 'Ouvrir AIODOS aux applications de ce PC',
      'Une application (PANDORA, ARKLIGHT PC…) demande l\'accès d\'elle-même : une carte s\'affiche, tu compares '
      + 'le code et tu choisis ce qu\'elle peut faire. Le premier accord ouvre AIODOS aux applications.', g.apps)}
    ${switchRow('gateway-requests', 'Accepter les nouvelles demandes d\'accès',
      'Désactivé, les applications déjà autorisées continuent, les nouvelles sont refusées sans carte.', g.requests)}
    ${switchRow('gateway-same-account', 'Refuser les applications d\'un autre compte Windows',
      'Le programme et le compte de chaque appli sont vérifiés à chaque demande.', g.same_account)}
    <div id="gateway-apps-box">${appsCard()}</div>`;
}

function copyRow(label, value, id) {
  return `<div class="copy-row"><span class="copy-label">${esc(label)}</span>
    <code class="mono" id="${id}">${esc(value)}</code>
    <button class="btn sm" type="button" data-copy="${id}" title="Copier">${icon('copy')}Copier</button></div>`;
}

export function gatewayCard() {
  const g = state.gateway;
  if (!g) return '<div class="help">État de la passerelle indisponible.</div>';
  const config = JSON.stringify(g.mcp_stdio, null, 2);
  const general = g.enabled ? `
    <div class="field"><label for="gateway-key">Clé générale</label>
      <div class="inline"><input class="input mono" type="password" id="gateway-key" readonly value="${esc(g.key || '')}">
        <button class="btn sm" type="button" id="gateway-key-show">Afficher</button>
        <button class="btn sm" type="button" data-copy="gateway-key">${icon('copy')}Copier</button>
        <button class="btn sm danger" type="button" id="gateway-key-renew">${icon('retry')}Renouveler</button></div>
      <div class="help">À donner à l'outil comme « clé d'API » (il l'envoie en « Authorization: Bearer … »).
        Elle reste la même d'un lancement à l'autre ; renouvelée, l'ancienne cesse aussitôt de marcher. Pour un
        outil, une clé à la main (plus haut) a ses propres droits : c'est mieux.</div></div>
    <div class="field"><label>Adresses à donner aux outils</label>
      ${copyRow('Compatible OpenAI', g.urls.openai, 'gateway-url-openai')}
      ${copyRow('Compatible Ollama', g.urls.ollama, 'gateway-url-ollama')}
      ${copyRow('Serveur MCP (HTTP)', g.urls.mcp, 'gateway-url-mcp')}
      <div class="help">Modèles : le nom seul (qwen3.5:9b) laisse AIODOS choisir la machine qui répond ;
        « peer:… » en désigne une. Pas à pas pour chaque outil : docs/autres-outils.md.</div></div>
    <div class="field"><label>Assistant qui lance un programme (Claude Desktop…)</label>
      <pre class="config mono" id="gateway-stdio">${esc(config)}</pre>
      <div class="inline"><button class="btn sm" type="button" data-copy="gateway-stdio">${icon('copy')}Copier la configuration</button></div>
      <div class="help">À ajouter à sa configuration MCP (claude_desktop_config.json…) : le pont lit lui-même
        l'adresse et la clé. AIODOS doit tourner, la passerelle ouverte.</div></div>` : '';
  return `
    <div class="gateway-status help">${stateLine(g)}</div>
    ${appsSection()}
    <div class="setting-row"><div class="txt"><b>Clé générale pour les autres outils</b>
      <small>Open WebUI, AnythingLLM, Continue, un éditeur de code ou un assistant MCP (Claude Desktop…)
      utilisent alors les modèles de cette machine et ceux que te prêtent tes machines appairées, et
      cherchent dans tes bibliothèques. Les passages trouvés partent vers l'assistant, et vers son modèle :
      dans le cloud si c'en est un. Les bibliothèques gardées sur ce PC (le cadenas) ne leur sont jamais
      montrées.</small></div>
      <label class="switch"><input type="checkbox" id="gateway-enabled" ${g.enabled ? 'checked' : ''}><span></span></label></div>
    <div class="field"><label for="gateway-port">Port (sur 127.0.0.1)</label>
      <div class="inline"><input class="input" type="number" id="gateway-port" min="1024" max="65535" value="${esc(g.port)}">
        <button class="btn" type="button" id="gateway-port-apply">Appliquer</button></div>
      <div class="help">Fixe : l'adresse ne change pas d'un lancement à l'autre (11435 par défaut, à côté d'Ollama).</div></div>
    ${general}`;
}

async function change(body) {
  try {
    state.gateway = await post('/gateway', body);
    emit('gateway:loaded');
    if (state.gateway.error) toast(state.gateway.error, 'err', 6000);
    else if ('enabled' in body) toast(body.enabled ? 'Clé générale ouverte aux outils.' : 'Clé générale fermée.', 'ok');
    else if ('apps' in body) toast(body.apps ? 'AIODOS ouvert aux applications.' : 'AIODOS fermé aux applications.', 'ok');
    else if ('port' in body) toast('Port changé.', 'ok');
    else toast('Réglage enregistré.', 'ok');
  } catch (e) { errorToast(e); loadGateway(); }
}

export function bindGateway(box) {
  const enabled = box.querySelector('#gateway-enabled');
  if (!enabled) return;
  enabled.onchange = () => change({ enabled: enabled.checked });
  for (const [id, field] of [['gateway-apps', 'apps'], ['gateway-requests', 'requests'],
    ['gateway-same-account', 'same_account']]) {
    const input = box.querySelector(`#${id}`);
    if (input) input.onchange = () => change({ [field]: input.checked });
  }
  const apps = box.querySelector('#gateway-apps-box');
  if (apps) bindApps(apps);
  box.querySelector('#gateway-port-apply').onclick = () =>
    change({ port: Number(box.querySelector('#gateway-port').value) });
  const key = box.querySelector('#gateway-key');
  const show = box.querySelector('#gateway-key-show');
  if (show) show.onclick = e => {
    key.type = key.type === 'password' ? 'text' : 'password';
    e.target.textContent = key.type === 'password' ? 'Afficher' : 'Masquer';
  };
  const renew = box.querySelector('#gateway-key-renew');
  if (renew) renew.onclick = async () => {
    const r = await modal({
      title: 'Renouveler la clé d\'accès ?', iconName: 'warn', danger: true,
      text: 'Les outils qui utilisent la clé actuelle ne pourront plus joindre AIODOS tant que tu ne leur '
        + 'auras pas donné la nouvelle. Le pont des assistants (Claude Desktop…) la relit tout seul.',
      confirm: 'Renouveler',
    });
    if (!r.ok) return;
    try {
      state.gateway = await post('/gateway/key');
      emit('gateway:loaded');
      toast('Nouvelle clé créée.', 'ok');
    } catch (e) { errorToast(e); }
  };
  box.querySelectorAll('[data-copy]').forEach(b => {
    b.onclick = () => {
      const source = box.querySelector(`#${b.dataset.copy}`);
      const text = source.value ?? source.textContent;
      navigator.clipboard.writeText(text).then(() => toast('Copié.', 'ok', 1500), errorToast);
    };
  });
}
