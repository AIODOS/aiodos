// Autres outils (Réglages) : la passerelle d'AIODOS, API compatible OpenAI et Ollama et
// serveur MCP, sur cette machine seulement ; sa clé d'accès à copier ou à renouveler.
import { get, post } from '../api.js';
import { esc } from '../dom.js';
import { icon } from '../icons.js';
import { state, emit } from '../state.js';
import { modal, toast, errorToast } from '../ui.js';

export async function loadGateway() {
  try { state.gateway = await get('/gateway'); } catch { return; }
  emit('gateway:loaded');
}

function stateLine(g) {
  if (g.error) return `<small class="warn">${esc(g.error)}</small>`;
  if (g.running) return `<small class="ok">Ouverte sur ${esc(g.urls.ollama)}, pour cette machine seulement.</small>`;
  return '<small>Fermée : aucun autre outil ne peut joindre AIODOS.</small>';
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
  return `
    <div class="setting-row gateway-status"><div class="txt"><b>Ouvrir AIODOS aux autres outils</b>
      <small>Open WebUI, AnythingLLM, Continue, un éditeur de code ou un assistant MCP (Claude Desktop…)
      utilisent alors les modèles de cette machine et ceux que te prêtent tes machines appairées, et
      cherchent dans tes bibliothèques. Les passages trouvés partent vers l'assistant, et vers son modèle :
      dans le cloud si c'en est un.</small>
      ${stateLine(g)}</div>
      <label class="switch"><input type="checkbox" id="gateway-enabled" ${g.enabled ? 'checked' : ''}><span></span></label></div>
    <div class="field"><label for="gateway-port">Port (sur 127.0.0.1)</label>
      <div class="inline"><input class="input" type="number" id="gateway-port" min="1024" max="65535" value="${esc(g.port)}">
        <button class="btn" type="button" id="gateway-port-apply">Appliquer</button></div>
      <div class="help">Fixe : l'adresse ne change pas d'un lancement à l'autre (11435 par défaut, à côté d'Ollama).</div></div>
    <div class="field"><label for="gateway-key">Clé d'accès</label>
      <div class="inline"><input class="input mono" type="password" id="gateway-key" readonly value="${esc(g.key)}">
        <button class="btn sm" type="button" id="gateway-key-show">Afficher</button>
        <button class="btn sm" type="button" data-copy="gateway-key">${icon('copy')}Copier</button>
        <button class="btn sm danger" type="button" id="gateway-key-renew">${icon('retry')}Renouveler</button></div>
      <div class="help">À donner à l'outil comme « clé d'API » (il l'envoie en « Authorization: Bearer … »).
        Elle reste la même d'un lancement à l'autre ; renouvelée, l'ancienne cesse aussitôt de marcher.</div></div>
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
        l'adresse et la clé. AIODOS doit tourner, la passerelle ouverte.</div></div>`;
}

async function change(body) {
  try {
    state.gateway = await post('/gateway', body);
    emit('gateway:loaded');
    if (state.gateway.error) toast(state.gateway.error, 'err', 6000);
    else if ('enabled' in body) toast(body.enabled ? 'Passerelle ouverte.' : 'Passerelle fermée.', 'ok');
    else toast('Port changé.', 'ok');
  } catch (e) { errorToast(e); loadGateway(); }
}

export function bindGateway(box) {
  const enabled = box.querySelector('#gateway-enabled');
  if (!enabled) return;
  enabled.onchange = () => change({ enabled: enabled.checked });
  box.querySelector('#gateway-port-apply').onclick = () =>
    change({ port: Number(box.querySelector('#gateway-port').value) });
  const key = box.querySelector('#gateway-key');
  box.querySelector('#gateway-key-show').onclick = e => {
    key.type = key.type === 'password' ? 'text' : 'password';
    e.target.textContent = key.type === 'password' ? 'Afficher' : 'Masquer';
  };
  box.querySelector('#gateway-key-renew').onclick = async () => {
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
