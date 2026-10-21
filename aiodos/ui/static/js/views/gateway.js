// Applications et autres outils (Réglages) : la passerelle d'AIODOS, sur cette machine
// seulement. Les applications (PANDORA, ARKLIGHT PC…) y ont chacune leur clé, accordée par
// la carte d'accord (gateway-request.js) et gérée ici (gateway-apps.js) ; les autres outils
// (Open WebUI, Continue, assistants MCP) passent par la clé générale, à copier ou renouveler.
import { get, post } from '../api.js';
import { esc } from '../dom.js';
import { t } from '../i18n.js';
import { icon } from '../icons.js';
import { state, emit } from '../state.js';
import { modal, toast, errorToast } from '../ui.js';
import { appsCard, bindApps, loadApps } from './gateway-apps.js';

export async function loadGateway() {
  try { state.gateway = await get('/gateway'); } catch { return; }
  emit('gateway:loaded');
  loadApps();
}

// L'erreur vient du nœud, déjà dans la langue de la requête
function stateLine(g) {
  if (g.error) return `<small class="warn">${esc(g.error)}</small>`;
  if (g.running) return `<small class="ok">${esc(t('gateway.state_open', { url: g.urls.ollama }))}</small>`;
  return `<small>${esc(t('gateway.state_closed'))}</small>`;
}

function switchRow(id, title, help, on) {
  return `<div class="setting-row"><div class="txt"><b>${esc(title)}</b><small>${esc(help)}</small></div>
    <label class="switch"><input type="checkbox" id="${id}" ${on ? 'checked' : ''}><span></span></label></div>`;
}

export function appsSection() {
  const g = state.gateway;
  if (!g) return '';
  return `
    ${switchRow('gateway-apps', t('gateway.apps_title'), t('gateway.apps_help'), g.apps)}
    ${switchRow('gateway-requests', t('gateway.requests_title'), t('gateway.requests_help'), g.requests)}
    ${switchRow('gateway-same-account', t('gateway.same_account_title'), t('gateway.same_account_help'),
      g.same_account)}
    <div id="gateway-apps-box">${appsCard()}</div>`;
}

function copyRow(label, value, id) {
  return `<div class="copy-row"><span class="copy-label">${esc(label)}</span>
    <code class="mono" id="${id}">${esc(value)}</code>
    <button class="btn sm" type="button" data-copy="${id}" title="${esc(t('gateway.copy'))}">${icon('copy')}${esc(t('gateway.copy'))}</button></div>`;
}

export function gatewayCard() {
  const g = state.gateway;
  if (!g) return `<div class="help">${esc(t('gateway.unavailable'))}</div>`;
  const config = JSON.stringify(g.mcp_stdio, null, 2);
  const general = g.enabled ? `
    <div class="field"><label for="gateway-key">${esc(t('gateway.general_key'))}</label>
      <div class="inline"><input class="input mono" type="password" id="gateway-key" readonly value="${esc(g.key || '')}">
        <button class="btn sm" type="button" id="gateway-key-show">${esc(t('gateway.show'))}</button>
        <button class="btn sm" type="button" data-copy="gateway-key">${icon('copy')}${esc(t('gateway.copy'))}</button>
        <button class="btn sm danger" type="button" id="gateway-key-renew">${icon('retry')}${esc(t('gateway.renew'))}</button></div>
      <div class="help">${esc(t('gateway.general_key_help'))}</div></div>
    <div class="field"><label>${esc(t('gateway.addresses'))}</label>
      ${copyRow(t('gateway.openai_compatible'), g.urls.openai, 'gateway-url-openai')}
      ${copyRow(t('gateway.ollama_compatible'), g.urls.ollama, 'gateway-url-ollama')}
      ${copyRow(t('gateway.mcp_server'), g.urls.mcp, 'gateway-url-mcp')}
      <div class="help">${esc(t('gateway.addresses_help'))}</div></div>
    <div class="field"><label>${esc(t('gateway.stdio_title'))}</label>
      <pre class="config mono" id="gateway-stdio">${esc(config)}</pre>
      <div class="inline"><button class="btn sm" type="button" data-copy="gateway-stdio">${icon('copy')}${esc(t('gateway.copy_config'))}</button></div>
      <div class="help">${esc(t('gateway.stdio_help'))}</div></div>` : '';
  return `
    <div class="gateway-status help">${stateLine(g)}</div>
    ${appsSection()}
    <div class="setting-row"><div class="txt"><b>${esc(t('gateway.general_title'))}</b>
      <small>${esc(t('gateway.general_help'))}</small></div>
      <label class="switch"><input type="checkbox" id="gateway-enabled" ${g.enabled ? 'checked' : ''}><span></span></label></div>
    <div class="field"><label for="gateway-port">${esc(t('gateway.port'))}</label>
      <div class="inline"><input class="input" type="number" id="gateway-port" min="1024" max="65535" value="${esc(g.port)}">
        <button class="btn" type="button" id="gateway-port-apply">${esc(t('gateway.apply'))}</button></div>
      <div class="help">${esc(t('gateway.port_help'))}</div></div>
    ${general}`;
}

async function change(body) {
  try {
    state.gateway = await post('/gateway', body);
    emit('gateway:loaded');
    if (state.gateway.error) toast(state.gateway.error, 'err', 6000);
    else if ('enabled' in body) toast(t(body.enabled ? 'gateway.general_opened' : 'gateway.general_closed'), 'ok');
    else if ('apps' in body) toast(t(body.apps ? 'gateway.apps_opened' : 'gateway.apps_closed'), 'ok');
    else if ('port' in body) toast(t('gateway.port_changed'), 'ok');
    else toast(t('gateway.setting_saved'), 'ok');
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
    e.target.textContent = t(key.type === 'password' ? 'gateway.show' : 'gateway.hide');
  };
  const renew = box.querySelector('#gateway-key-renew');
  if (renew) renew.onclick = async () => {
    const r = await modal({
      title: t('gateway.renew_title'), iconName: 'warn', danger: true,
      text: t('gateway.renew_text'),
      confirm: t('gateway.renew'),
    });
    if (!r.ok) return;
    try {
      state.gateway = await post('/gateway/key');
      emit('gateway:loaded');
      toast(t('gateway.key_created'), 'ok');
    } catch (e) { errorToast(e); }
  };
  box.querySelectorAll('[data-copy]').forEach(b => {
    b.onclick = () => {
      const source = box.querySelector(`#${b.dataset.copy}`);
      const text = source.value ?? source.textContent;
      navigator.clipboard.writeText(text).then(() => toast(t('gateway.copied'), 'ok', 1500), errorToast);
    };
  });
}
