// Cette machine : profil du nœud, modèles, préférences de partage de puissance.
import { get, put } from '../api.js';
import { $, h, esc, bytes, duration } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { ask, toast, errorToast } from '../ui.js';

const CAPS = { vision: 'vision', thinking: 'réflexion', tools: 'outils', embedding: 'embeddings' };
let root;

export function mountNode(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head">
        <div><h1>Cette machine</h1>
          <div class="sub">Ce que ce nœud peut offrir. En phase 1, ce profil sera partagé avec tes autres machines
          pour que chaque travail parte là où il tourne le mieux.</div></div>
      </div>
      <div class="grid-2">
        <div class="card" id="node-machine"></div>
        <div class="card" id="node-share"></div>
      </div>
      <div class="card" id="node-models" style="margin-top:16px"></div>
      <div class="card" id="node-network" style="margin-top:16px"></div>
    </div></div>`);
  container.append(root);
  on('view:shown', view => { if (view === 'node') loadNode(); });
  on('settings:loaded', renderShare);
  setInterval(() => { if (state.view === 'node') loadNode(); }, 5000);
}

export async function loadNode() {
  try {
    state.node = await get('/node');
    emit('node:loaded');
    if (root) render();
  } catch (e) { /* nœud indisponible : on réessaiera */ }
}

function gauge(used, total, label) {
  const pct = total ? Math.round(100 * used / total) : 0;
  return `<div class="gauge">${label}<div class="bar"><i class="fill" style="width:${pct}%"></i></div>
    <small>${bytes(used)} / ${bytes(total)} · ${pct} %</small></div>`;
}

function render() {
  const n = state.node;
  if (!n) return;
  const mem = n.memory;
  const gpus = n.gpus.length
    ? n.gpus.map(g => gauge(g.vram_used, g.vram_total,
        `${esc(g.name)} <span class="faint">· utilisation ${g.utilization} %</span>`)).join('')
    : '<span class="muted">Aucun GPU NVIDIA détecté</span>';
  const idle = n.idle_seconds == null ? '—'
    : n.idle_seconds < 60 ? 'En cours d\'utilisation' : `Inactive depuis ${duration(n.idle_seconds)}`;
  const power = n.on_ac_power == null ? '—' : n.on_ac_power ? 'Sur secteur' : 'Sur batterie';
  $('#node-machine', root).innerHTML = `
    <h2>${icon('node')}${esc(n.name)}
      <button class="icon-btn" id="rename-node" title="Renommer ce nœud">${icon('edit')}</button></h2>
    <p class="hint">Nœud <span class="mono">${esc(n.node_id.slice(0, 12))}</span> · Cœur IA ${esc(n.version)}</p>
    <dl class="facts">
      <dt>Système</dt><dd>${esc(n.os)}</dd>
      <dt>Processeur</dt><dd>${esc(n.cpu.name)} <span class="faint">· ${n.cpu.threads} threads</span></dd>
      <dt>Mémoire</dt><dd>${gauge(mem.total - mem.available, mem.total, '')}</dd>
      <dt>GPU</dt><dd>${gpus}</dd>
      <dt>Alimentation</dt><dd>${power}</dd>
      <dt>Activité</dt><dd>${idle}</dd>
      <dt>Ollama</dt><dd>${n.ollama.version ? `version ${esc(n.ollama.version)}` : '<span style="color:var(--err)">ne répond pas</span>'}</dd>
    </dl>`;
  $('#rename-node', root).onclick = renameNode;

  const vram = Math.max(0, ...n.gpus.map(g => g.vram_total));
  const rows = n.ollama.models.map(m => `
    <tr>
      <td><b>${esc(m.name)}</b>${m.loaded ? ' <span class="pill run">chargé</span>' : ''}</td>
      <td class="muted">${esc(m.parameters || '')}</td>
      <td>${bytes(m.size)}</td>
      <td><div class="caps">${(m.caps || []).filter(c => CAPS[c]).map(c => `<span class="pill">${CAPS[c]}</span>`).join('')}</div></td>
      <td>${!vram ? '<span class="faint">—</span>' : m.fits_vram
        ? '<span class="pill ok">tient en VRAM</span>'
        : '<span class="pill warn">trop gros : processeur (lent)</span>'}</td>
    </tr>`).join('');
  $('#node-models', root).innerHTML = `
    <h2>${icon('layers')}Modèles installés</h2>
    <p class="hint">Un modèle qui dépasse la VRAM tourne en partie sur le processeur, beaucoup plus lentement.
      En phase 1, il pourra être servi par une autre machine de ton réseau qui a la mémoire nécessaire.</p>
    ${rows ? `<table class="models"><thead><tr><th>Modèle</th><th>Taille</th><th>Disque</th><th>Capacités</th><th>VRAM</th></tr></thead>
      <tbody>${rows}</tbody></table>` : '<div class="empty">Aucun modèle (Ollama ne répond pas ?)</div>'}`;

  $('#node-network', root).innerHTML = `
    <h2>${icon('network')}Réseau local</h2>
    <div class="empty" style="padding:22px 10px">${icon('network')}
      <div>Aucune autre machine pour l'instant.</div>
      <div class="faint" style="margin-top:6px;font-size:12.5px">Phase 1 : découverte automatique des nœuds Cœur IA de ton réseau,
      appairage par code, échanges chiffrés, et répartition des tâches entre tes 2-3 PC.</div></div>`;
  renderShare();
}

function shareRow(key, title, text) {
  const on = !!state.settings?.[key];
  return `<div class="setting-row"><div class="txt"><b>${title}</b><small>${text}</small></div>
    <label class="switch"><input type="checkbox" data-key="${key}" ${on ? 'checked' : ''}><span></span></label></div>`;
}

function renderShare() {
  const box = root && $('#node-share', root);
  if (!box || !state.settings) return;
  const s = state.settings;
  box.innerHTML = `
    <h2>${icon('bolt')}Partage de puissance</h2>
    <div class="phase-note">${icon('info')}<div>Tes choix sont enregistrés dès maintenant ; ils s'appliqueront
      quand le réseau entre machines sera activé (phase 1).</div></div>
    ${shareRow('share_enabled', 'Prêter ma puissance à mes autres machines', 'Elles pourront envoyer des unités de travail à ce PC.')}
    ${shareRow('share_when_idle', "Seulement quand je n'utilise pas ce PC", "Comme l'économiseur d'écran de SETI@home : le travail s'arrête dès que tu reviens.")}
    <div class="setting-row"><div class="txt"><b>Délai d'inactivité</b><small>Avant de commencer à prêter de la puissance.</small></div>
      <select class="select" data-num="idle_minutes" style="width:140px">${[2, 5, 10, 15, 30, 60].map(m =>
        `<option value="${m}" ${m === s.idle_minutes ? 'selected' : ''}>${m} min</option>`).join('')}</select></div>
    ${shareRow('share_on_ac_only', 'Seulement sur secteur', 'Jamais sur batterie.')}
    <div class="setting-row"><div class="txt"><b>GPU prêté au maximum</b><small>Part de la carte graphique laissée aux autres.</small></div>
      <select class="select" data-num="share_max_gpu" style="width:140px">${[25, 50, 75, 100].map(p =>
        `<option value="${p}" ${p === s.share_max_gpu ? 'selected' : ''}>${p} %</option>`).join('')}</select></div>`;
  box.querySelectorAll('input[data-key]').forEach(input => {
    input.onchange = () => save({ [input.dataset.key]: input.checked });
  });
  box.querySelectorAll('select[data-num]').forEach(sel => {
    sel.onchange = () => save({ [sel.dataset.num]: Number(sel.value) });
  });
}

async function save(values) {
  try {
    state.settings = await put('/settings', values);
    emit('settings:loaded');
  } catch (e) { errorToast(e); }
}

async function renameNode() {
  const name = await ask('Nom de ce nœud', 'Le nom qui identifiera cette machine sur ton réseau :', state.node.name);
  if (!name) return;
  await save({ node_name: name });
  toast('Nœud renommé.', 'ok');
  loadNode();
}
