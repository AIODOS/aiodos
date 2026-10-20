// Cette machine : profil du nœud, modèles, préférences de partage de puissance.
import { get, post, put } from '../api.js';
import { $, h, esc, bytes, duration, powerText } from '../dom.js';
import { icon } from '../icons.js';
import { state, on, emit } from '../state.js';
import { ask, toast, errorToast } from '../ui.js';
import { loadNetwork } from './network.js';
import { chooseModel, here, integrated } from './model-install.js';

const CAPS = { vision: 'vision', thinking: 'réflexion', tools: 'outils', embedding: 'embeddings' };
const RESOURCE_ICONS = { gpu: 'image', embed: 'layers', cpu: 'cpu', net: 'globe' };
let root;
let shareMobile = null;            // préférences dessinées pour un téléphone ?
let trayRow = null;                // réglage de la zone de notification dessiné ?
let autostart = null;              // démarrage avec l'appareil (node/autostart.py)

export function mountNode(container) {
  root = h(`
    <div class="page"><div class="page-inner">
      <div class="page-head">
        <div><h1>Cette machine</h1>
          <div class="sub">Ce que ce nœud peut offrir. Ce profil est envoyé à tes machines appairées
          pour que chaque unité de travail parte là où elle peut tourner.</div></div>
      </div>
      <div class="grid-2">
        <div class="card" id="node-machine"></div>
        <div class="card" id="node-share"></div>
      </div>
      <div class="card" id="node-background" style="margin-top:16px"></div>
      <div class="card" id="node-models" style="margin-top:16px"></div>
      <div class="card" id="node-network" style="margin-top:16px"></div>
    </div></div>`);
  container.append(root);
  on('view:shown', view => { if (view === 'node') { loadNode(); loadExecutor(); loadAutostart(); } });
  on('settings:loaded', () => { renderShare(); renderBackground(); });
  on('network:loaded', () => { renderNetworkSummary(); renderShareStatus(); });
  on('executor:changed', () => { renderShareStatus(); renderBackgroundStatus(); });
  window.addEventListener('pywebviewready', renderBackground);
  // Fenêtre cachée ou réduite : rien à rafraîchir (le profil lance nvidia-smi)
  setInterval(() => { if (state.view === 'node' && !document.hidden) { loadNode(); loadNetwork(); } }, 5000);
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
  const idle = n.idle_seconds == null ? (n.power?.mobile ? 'Non mesurable sur un téléphone' : '—')
    : n.idle_seconds < 60 ? 'En cours d\'utilisation' : `Inactive depuis ${duration(n.idle_seconds)}`;
  const power = powerText(n);
  $('#node-machine', root).innerHTML = `
    <h2>${icon('node')}${esc(n.name)}
      <button class="icon-btn" id="rename-node" title="Renommer ce nœud">${icon('edit')}</button></h2>
    <p class="hint">Nœud <span class="mono">${esc(n.node_id.slice(0, 12))}</span> · AIODOS ${esc(n.version)}</p>
    <dl class="facts">
      <dt>Système</dt><dd>${esc(n.os)}</dd>
      <dt>Processeur</dt><dd>${esc(n.cpu.name)} <span class="faint">· ${n.cpu.threads} threads</span></dd>
      <dt>Mémoire</dt><dd>${gauge(mem.total - mem.available, mem.total, '')}</dd>
      <dt>GPU</dt><dd>${gpus}</dd>
      <dt>Alimentation</dt><dd>${power}</dd>
      <dt>Activité</dt><dd>${idle}</dd>
      <dt>${integrated() ? 'Moteur' : 'Ollama'}</dt><dd>${n.ollama.version
        ? (integrated() ? `${esc(n.ollama.version)} (intégré)` : `version ${esc(n.ollama.version)}`)
        : '<span style="color:var(--err)">ne répond pas</span>'}</dd>
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
      Une de tes machines qui a la mémoire nécessaire peut alors répondre à sa place, ou plusieurs peuvent
      se le partager.</p>
    ${rows ? `<table class="models"><thead><tr><th>Modèle</th><th>Paramètres</th><th>Disque</th><th>Capacités</th><th>VRAM</th></tr></thead>
      <tbody>${rows}</tbody></table>` : `<div class="empty">Aucun modèle sur ${here()}${integrated() ? '' : ' (ou Ollama ne répond pas)'}.</div>`}
    <button class="btn sm" data-install>${icon('plus')}Installer un modèle</button>`;
  $('#node-models', root).querySelector('[data-install]').onclick = chooseModel;

  renderNetworkSummary();
  // Les préférences ne sont reconstruites qu'à leur changement (settings:loaded) :
  // les redessiner toutes les 5 s fermerait une liste ouverte et perdrait un clic
  if (!$('#share-status', root) || shareMobile !== !!n.power?.mobile) renderShare();
  if (!$('#background-status', root) || trayRow !== windowsWindow()) renderBackground();
}

// État de la file (dont la cadence du travail de fond), s'il n'est pas encore arrivé
// par le flux d'événements
async function loadExecutor() {
  if (state.executor) return;
  try {
    state.executor = (await get('/jobs')).executor;
    emit('executor:changed');
  } catch (e) { /* nœud indisponible : on réessaiera */ }
}

// Travail de fond : priorité basse toujours, et sur batterie, selon le réglage
function renderBackground() {
  const box = root && $('#node-background', root);
  if (!box || !state.settings) return;
  const mode = state.settings.battery_mode || 'eco';
  const options = [['eco', 'Ménager la batterie (conseillé)'], ['pause', 'Attendre le secteur'],
    ['normal', 'Pleine vitesse']];
  trayRow = windowsWindow();
  box.innerHTML = `
    <h2>${icon('cpu')}Travail de fond</h2>
    <div id="background-status"></div>
    <p class="hint">Indexations et calculs pour tes machines passent après toi : priorité basse, toujours
      (et sous Windows, mode efficacité du processeur). Pendant une conversation sur cette machine, le calcul
      sur la carte graphique attend la fin de la réponse.</p>
    <div class="setting-row"><div class="txt"><b>Sur batterie</b><small>Ménager : une unité à la fois par
      ressource (carte graphique, processeur…), moins de fils de calcul, et des pauses (calcul la moitié du
      temps). Attendre : rien ne tourne en fond avant le retour du secteur. Les conversations restent servies
      dans tous les cas.</small></div>
      <select class="select wide" id="battery-mode">${options.map(([v, l]) =>
        `<option value="${v}" ${v === mode ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
    ${windowsWindow() ? shareRow('close_to_tray', 'Garder AIODOS ouvert quand je ferme la fenêtre',
      "Il reste dans la zone de notification, près de l'horloge : le nœud continue de servir tes machines "
      + "et d'avancer le travail de fond. Clic sur l'icône pour rouvrir, clic droit pour quitter.") : ''}
    <div id="autostart-row"></div>`;
  $('#battery-mode', box).onchange = e => save({ battery_mode: e.target.value });
  box.querySelectorAll('input[data-key]').forEach(input => {
    input.onchange = () => save({ [input.dataset.key]: input.checked });
  });
  renderBackgroundStatus();
  renderAutostart();
}

async function loadAutostart() {
  try { autostart = await get('/node/autostart'); } catch { autostart = null; }
  renderAutostart();
}

/** Démarrer avec l'appareil, en arrière-plan : Windows (raccourci du dossier Démarrage) ou
 *  téléphone (l'appli AIODOS démarre son nœud, node/android_app.py). */
function renderAutostart() {
  const box = root && $('#autostart-row', root);
  if (!box) return;
  if (!autostart?.supported) { box.innerHTML = ''; return; }
  const phone = autostart.device === 'android';
  box.innerHTML = `<div class="setting-row"><div class="txt">
      <b>${phone ? 'Démarrer avec le téléphone' : 'Démarrer avec Windows, en arrière-plan'}</b>
      <small>${phone
        ? "Le nœud démarre quand le téléphone s'allume, sans ouvrir l'appli AIODOS : il tourne en "
          + "arrière-plan, avec sa notification (Android l'exige)."
        : "À l'ouverture de ta session, AIODOS démarre sans ouvrir sa fenêtre : le nœud sert tes machines "
          + "et avance le travail de fond ; l'icône près de l'horloge rouvre la fenêtre."}</small></div>
    <label class="switch"><input type="checkbox" id="autostart-toggle" ${autostart.enabled ? 'checked' : ''}>
      <span></span></label></div>`;
  $('#autostart-toggle', box).onchange = async e => {
    try {
      autostart = await post('/node/autostart', { enabled: e.target.checked });
      toast(autostart.enabled ? 'AIODOS démarrera avec cet appareil, en arrière-plan.'
        : 'AIODOS ne démarrera plus avec cet appareil.', 'ok', 5000);
    } catch (err) {
      e.target.checked = !e.target.checked;
      errorToast(err);
    }
  };
}

// La fenêtre de Windows (pywebview) : la seule qui a une icône dans la zone de notification
function windowsWindow() {
  return !!window.pywebview && /^windows/i.test(state.node?.os || '');
}

function renderBackgroundStatus() {
  const box = root && $('#background-status', root);
  if (!box) return;
  const p = state.executor?.pacing;
  if (!p) { box.innerHTML = ''; return; }
  const [cls, label, detail] = p.chatting
    ? ['warn', 'Conversation en cours', 'Le calcul sur la carte graphique attend la fin de la réponse.']
    : p.mode === 'pause' ? ['warn', 'En pause', p.detail]
      : p.mode === 'eco' ? ['ok', 'Ménagé', p.detail] : ['ok', 'Pleine vitesse', p.detail];
  box.innerHTML = `<div class="share-status"><span class="pill ${cls}">${label}</span>
    <span class="muted">${esc(detail)}</span></div>`;
}

function renderNetworkSummary() {
  const box = root && $('#node-network', root);
  if (!box) return;
  const net = state.network;
  const peers = net?.peers || [];
  const online = peers.filter(p => p.online).length;
  const summary = !net?.enabled ? 'Désactivé'
    : peers.length ? `${peers.length} machine(s) appairée(s), ${online} en ligne`
      : 'Activé, aucune machine appairée pour l\'instant';
  box.innerHTML = `
    <h2>${icon('network')}Réseau local</h2>
    <div class="setting-row" style="border:0;padding:0">
      <div class="txt"><b>${summary}</b>
        <small>Découverte des autres nœuds, appairage par code, échanges chiffrés.</small></div>
      <button class="btn sm" id="go-network">${icon('network')}Ouvrir</button>
    </div>`;
  $('#go-network', box).onclick = () => emit('view:show', 'network');
}

function shareRow(key, title, text) {
  const on = !!state.settings?.[key];
  return `<div class="setting-row"><div class="txt"><b>${title}</b><small>${text}</small></div>
    <label class="switch"><input type="checkbox" data-key="${key}" ${on ? 'checked' : ''}><span></span></label></div>`;
}

// État du partage, en direct : prêt, en pause (pourquoi), unités calculées ici pour d'autres
function renderShareStatus() {
  const box = root && $('#share-status', root);
  if (!box) return;
  const net = state.network;
  if (!net?.enabled) {
    box.innerHTML = `<div class="phase-note">${icon('info')}<div>Le réseau local est désactivé : ce PC ne
      prête rien et ne confie rien. <a href="#" id="share-go-network">Activer le réseau</a></div></div>`;
    $('#share-go-network', box).onclick = e => { e.preventDefault(); emit('view:show', 'network'); };
    return;
  }
  const sharing = net.sharing;
  if (!sharing) { box.innerHTML = ''; return; }
  const working = state.executor?.remote || sharing.working || [];
  const [cls, label] = working.length ? ['run', 'Calcule pour tes machines']
    : sharing.active ? ['ok', 'Prêt'] : sharing.state === 'off' ? ['idle', 'Désactivé'] : ['warn', 'En pause'];
  box.innerHTML = `
    <div class="share-status"><span class="pill ${cls}">${label}</span><span class="muted">${esc(sharing.detail)}</span></div>
    ${working.map(w => `<div class="remote-item">${icon(RESOURCE_ICONS[w.resource] || 'cpu')}
      <span>Pour « ${esc(w.peer_name)} » : <span class="mono">${esc(w.label)}</span></span></div>`).join('')}`;
}

function renderShare() {
  const box = root && $('#node-share', root);
  if (!box || !state.settings) return;
  const s = state.settings;
  // Téléphone : pas d'inactivité mesurable ; en charge, assez chargé, pas trop chaud
  const mobile = shareMobile = !!state.node?.power?.mobile;
  const device = mobile ? 'ce téléphone' : 'ce PC';
  const idleRows = `
    ${shareRow('share_when_idle', "Seulement quand je n'utilise pas ce PC", "Comme un économiseur d'écran : le travail s'arrête dès que tu reviens. Désactivé, ce PC prête aussi pendant que tu l'utilises : en priorité basse, jamais sur batterie, la carte graphique la moitié du temps au plus, et tes conversations passent devant.")}
    <div class="setting-row"><div class="txt"><b>Délai d'inactivité</b><small>Sans clavier ni souris pendant ce temps, ce PC est considéré comme libre : le prêt n'est plus retenu (avec le réglage ci-dessus, il ne commence qu'à ce moment).</small></div>
      <select class="select short" data-num="idle_minutes">${[2, 5, 10, 15, 30, 60].map(m =>
        `<option value="${m}" ${m === s.idle_minutes ? 'selected' : ''}>${m} min</option>`).join('')}</select></div>`;
  const phoneRows = `
    <div class="setting-row"><div class="txt"><b>Batterie minimale</b><small>Pour commencer à calculer (ensuite, pause 5 % plus bas) : en dessous, le téléphone se recharge d'abord.</small></div>
      <select class="select short" data-num="share_min_battery">${[50, 60, 70, 80, 90].map(p =>
        `<option value="${p}" ${p === s.share_min_battery ? 'selected' : ''}>${p} %</option>`).join('')}</select></div>
    <div class="setting-row"><div class="txt"><b>Température maximale de la batterie</b><small>Au-delà, pause jusqu'à ce qu'elle ait perdu 3 °C.</small></div>
      <select class="select short" data-num="share_max_temp">${[35, 38, 40, 42].map(t =>
        `<option value="${t}" ${t === s.share_max_temp ? 'selected' : ''}>${t} °C</option>`).join('')}</select></div>`;
  box.innerHTML = `
    <h2>${icon('bolt')}Partage de puissance</h2>
    <div id="share-status"></div>
    ${shareRow('offload_enabled', 'Me faire aider par mes autres machines', `Les unités de tes tâches peuvent être calculées sur tes machines appairées, sauf les bibliothèques gardées sur ${device}. Les résultats sont enregistrés ici.`)}
    ${shareRow('share_enabled', 'Prêter ma puissance à mes autres machines', `${mobile ? 'Ce téléphone' : 'Ce PC'} calcule pour elles sans rien garder : les fichiers reçus sont effacés aussitôt.`)}
    ${shareRow('lend_models', 'Prêter mes modèles pour leurs conversations', `Elles pourront faire répondre leurs conversations par les modèles de ${device}. Le texte passe ici, chiffré, le temps de la réponse, sans être enregistré ; ton travail de fond se met en pause pendant ce temps.`)}
    ${mobile ? '' : shareRow('lend_gpu', 'Prêter ma carte graphique pour les gros modèles', "Un modèle trop gros pour une seule machine est découpé entre mes machines : celle-ci en garde une tranche le temps d'une conversation, par un tunnel chiffré du compagnon. Seulement pour mes machines, jamais pour un ami ; il faut llama.cpp de la même version des deux côtés.")}
    ${mobile ? '' : shareRow('lend_images_friends', 'Prêter ma carte graphique à mes amis pour les images', "Quand ce PC prête sa puissance, il peut aussi calculer les images et les vidéos de tes amis qui le lui demandent. Leur description et le résultat passent ici le temps du calcul, sans être gardés.")}
    ${mobile ? '' : idleRows}
    ${shareRow('share_on_ac_only', mobile ? 'Seulement en charge' : 'Seulement sur secteur',
      mobile ? "Jamais sur batterie. L'inactivité d'un téléphone n'est pas mesurable : c'est la charge qui compte." : 'Jamais sur batterie.')}
    ${mobile ? phoneRows : ''}
    <div class="setting-row"><div class="txt"><b>${mobile ? 'Calcul prêté au maximum' : 'GPU prêté au maximum'}</b><small>${mobile
      ? 'En dessous de 100 %, ce téléphone fait des pauses entre deux unités : il chauffe moins.'
      : 'Part du temps de la carte graphique : en dessous de 100 %, ce PC fait des pauses entre deux unités.'}</small></div>
      <select class="select short" data-num="share_max_gpu">${[25, 50, 75, 100].map(p =>
        `<option value="${p}" ${p === s.share_max_gpu ? 'selected' : ''}>${p} %</option>`).join('')}</select></div>`;
  box.querySelectorAll('input[data-key]').forEach(input => {
    input.onchange = () => save({ [input.dataset.key]: input.checked });
  });
  box.querySelectorAll('select[data-num]').forEach(sel => {
    sel.onchange = () => save({ [sel.dataset.num]: Number(sel.value) });
  });
  renderShareStatus();
}

async function save(values) {
  try {
    state.settings = await put('/settings', values);
    emit('settings:loaded');
    loadNetwork();                 // l'état du partage en dépend
  } catch (e) { errorToast(e); }
}

async function renameNode() {
  const name = await ask('Nom de ce nœud', 'Le nom qui identifiera cette machine sur ton réseau :', state.node.name);
  if (!name) return;
  await save({ node_name: name });
  toast('Nœud renommé.', 'ok');
  loadNode();
}
