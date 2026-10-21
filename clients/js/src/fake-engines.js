// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — le faux AIODOS, partie travaux ComfyUI (docs/api-applications.md
// § 5.7), tout en mémoire : moteurs, pré-vol, entrées par appli, travaux qui avancent (en file,
// en cours, fini), événements SSE en direct, fichiers par plages (le petit PNG du contrat),
// commandes, idempotence par client_ref, isolation entre applis, erreurs du contrat.
//
// Réglages, sur l'objet du faux :
// - jobMode : « auto » (une étape toutes les jobStepMs, 5 ms par défaut) ou « manual »
//   (step(), finish()) ; une étape démarre une unité, ou finit celles en cours ;
// - comfy : { enabled, version, vramMib, machine, models, cloudNodes } ;
// - nextJob({ failUnits, filesPerUnit }) : le prochain travail déposé (unités en échec,
//   fichiers par unité) ;
// - nextEvents({ cutAfter, stallAfter }) : le prochain flux d'événements, coupé (connexion
//   perdue) ou muet après n événements ; cutStreams() coupe tous les flux ouverts ;
// - nextContent({ cutAt }) : le prochain contenu de fichier, coupé après n octets ;
// - pingMs (« : ping », 15 000 ms), maxActiveJobs (50).
// Comme la passerelle : une appli ne voit que ses entrées, ses travaux et ses fichiers.

import { createHash, randomBytes } from 'node:crypto';

import { sniffType } from './media.js';

/** Le petit PNG de l'essai (8 × 8, le même que png() des essais d'AIODOS) : chaque sortie. */
export const FAKE_PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR4nGM4UaGBFTEMLQkAUtVaAV8W4QQAAAAASUVORK5CYII=', 'base64');

const CAPABILITY = 'engines:comfyui';
const CIRCLES = ['here', 'mine', 'friends', 'public'];
const SERVED = ['here']; // les cercles que cette version sert aux travaux ComfyUI
const FIELDS = new Set(['engine', 'label', 'client_ref', 'where', 'priority', 'graph', 'graphs', 'inputs', 'models', 'items', 'repeat', 'requires', 'bounds', 'strict', 'recipe']);
const ACTIVE = new Set(['queued', 'running', 'paused']);
const SHOWN = { queued: 'queued', running: 'in_progress', paused: 'paused', done: 'completed', failed: 'failed', cancelled: 'cancelled' };
const UNIT_SHOWN = { running: 'in_progress', done: 'completed' };
const FINISHED = new Set(['completed', 'failed', 'cancelled']);
const LOADERS = { LoadImage: 'image', LoadImageMask: 'image', LoadVideo: 'file', LoadAudio: 'audio' };
const OUTPUT_NODES = new Set(['SaveImage', 'SaveAnimatedWEBP', 'SaveAnimatedPNG', 'SaveVideo', 'SaveWEBM', 'SaveAudio', 'SaveAudioMP3', 'SaveAudioOpus', 'SaveGLB']);
const INPUT_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'video/mp4', 'video/webm', 'audio/wav', 'audio/mpeg', 'audio/flac', 'audio/ogg']);
const MODEL_EXT = /\.(safetensors|ckpt|pt|pth|bin|gguf|sft)$/i;
const ANNOTATION = /\[\s*(output|input|temp)\s*\]\s*$/i;
const SHA256_RE = /^[0-9a-f]{64}$/;
const INPUT_ID = /^[A-Za-z0-9_.-]{1,64}$/;
const BOUNDS = new Set(['unit_seconds', 'output_mb', 'wait_s']);
const MAX_INPUT = 100 * 1024 * 1024;
const MAX_GRAPH = 2 * 1024 * 1024;
const KEEP_DAYS = 7;

const isObj = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
const now = () => Date.now() / 1000;
const sha256 = (b) => createHash('sha256').update(b).digest('hex');
const PNG_SHA = sha256(FAKE_PNG);
const isLink = (v, graph) => Array.isArray(v) && v.length === 2 && typeof v[0] === 'string' && Number.isInteger(v[1]) && v[0] in graph;

function parseWhere(value) {
  const items = Array.isArray(value) ? value : typeof value === 'string' ? value.split(/[\s,]+/) : null;
  if (!items || !items.every((c) => typeof c === 'string')) return null;
  const list = items.map((c) => c.trim()).filter(Boolean);
  if (!list.length || list.some((c) => !CIRCLES.includes(c))) return null;
  return CIRCLES.filter((c) => list.includes(c));
}

/** Les valeurs de texte d'une entrée de nœud (listes comprises, liens exclus). */
function* texts(value, graph) {
  if (typeof value === 'string') yield value;
  else if (Array.isArray(value) && !isLink(value, graph)) for (const v of value) yield* texts(v, graph);
}

/**
 * La partie ComfyUI du faux. `kit` : les outils de fake.js (api, ok, fail, authOf, keyError).
 * Rend { route(requête) } : une réponse pour les routes des travaux, ou null.
 */
export function createFakeEngines(fake, kit, options = {}) {
  const { api, ok, fail, authOf, keyError } = kit;
  const jobScripts = [];
  const eventScripts = [];
  const contentScripts = [];
  const liveStreams = new Set();
  let seq = 0;

  fake.jobMode = options.jobMode ?? 'auto';
  fake.jobStepMs = options.jobStepMs ?? 5;
  fake.pingMs = options.pingMs ?? 15_000;
  fake.maxActiveJobs = options.maxActiveJobs ?? 50;
  fake.comfy = {
    enabled: true,
    version: '0.35.1',
    vramMib: 8187,
    machine: 'PC-BUREAU',
    models: [{ folder: 'checkpoints', name: 'sd_xl_base_1.0.safetensors' }],
    cloudNodes: ['OpenAIDalle3', 'KlingTextToVideoNode'],
    ...(options.comfy || {}),
  };
  /** Les entrées reçues, par appli : appli → Map(sha256 → { sha256, size, type, bytes }). */
  fake.inputs = new Map();
  /** Les travaux, par identifiant (toutes applis confondues). */
  fake.jobs = new Map();
  /** Les fichiers produits, par identifiant. */
  fake.files = new Map();
  fake.nextJob = (script = {}) => jobScripts.push(script);
  fake.nextEvents = (spec = {}) => eventScripts.push(spec);
  fake.nextContent = (spec = {}) => contentScripts.push(spec);
  fake.cutStreams = () => {
    for (const s of [...liveStreams]) s.cut();
  };
  const pick = (id) => (id != null ? fake.jobs.get(id) : [...fake.jobs.values()].at(-1)) ?? null;
  /** Une étape du travail (le dernier déposé sans `id`) ; faux s'il n'y avait rien à faire. */
  fake.step = (id) => {
    const job = pick(id);
    return job ? step(job) : false;
  };
  /** Toutes les étapes jusqu'à la fin (ou la pause) ; rend l'objet travail. */
  fake.finish = (id) => {
    const job = pick(id);
    if (!job) return null;
    while (step(job));
    return jobObject(job);
  };
  /** L'objet travail tel que l'appli le voit. */
  fake.jobObject = (id) => {
    const job = pick(id);
    return job ? jobObject(job) : null;
  };

  // --- les réponses ---------------------------------------------------------------------------

  // comme la passerelle : un conseil toujours en texte (vide s'il n'y en a pas)
  const err = (code, message, o = {}) => fail(code, { message, hint: '', ...o, detail: o.detail ?? {} });
  const notFound = (what) => (what === 'job' ? err('job_not_found', 'Travail introuvable.') : err('file_not_found', 'Fichier introuvable.'));

  function guard(req) {
    const a = authOf(req);
    if (a.state !== 'valid') return { error: keyError(a) };
    if (!fake.access.apps) return { error: err('apps_closed', "AIODOS n'est ouvert qu'aux autres outils : ouvre-le aussi aux applications.") };
    if (!a.entry.scopes.includes(CAPABILITY)) {
      return {
        error: err('scope_denied', `Cette application n'a pas le droit « ${CAPABILITY} » : demande-le dans AIODOS.`, {
          hint: 'Demande ce droit en plus (connect avec ce droit).',
          detail: { scope: CAPABILITY },
        }),
      };
    }
    return { entry: a.entry, app: a.entry.id };
  }

  /** Les cercles de la requête : l'accord, resserré par X-Aiodos-Where, puis ceux servis. */
  function circlesOf(req, entry) {
    let granted = CIRCLES.filter((c) => entry.where.includes(c));
    const header = req.headers['x-aiodos-where'];
    if (header) {
      const asked = parseWhere(header);
      if (!asked) return { error: err('invalid_request', 'Cercle inconnu (connus : here, mine, friends, public).', { param: 'X-Aiodos-Where' }) };
      const wider = asked.filter((c) => !granted.includes(c));
      if (wider.length) {
        return { error: err('where_denied', `Lieu de calcul refusé : cette application n'a pas accès à « ${wider[0]} ».`, { detail: { allowed: granted } }) };
      }
      granted = asked;
    }
    return { circles: granted.filter((c) => SERVED.includes(c)) };
  }

  const appInputs = (app) => {
    if (!fake.inputs.has(app)) fake.inputs.set(app, new Map());
    return fake.inputs.get(app);
  };

  // --- vérifier un travail (comme engine.normalize) --------------------------------------------

  function shapeError(graph, i) {
    if (isObj(graph) && Array.isArray(graph.nodes) && 'links' in graph) {
      return err('graph_not_api', "Graphe au format de l'éditeur : exporte-le au format API (« Exporter (API) » dans ComfyUI).", {
        param: 'graphs',
        hint: 'Exporte le graphe au format API dans ComfyUI.',
        detail: { graph: i },
      });
    }
    if (!isObj(graph) || !Object.keys(graph).length) return err('graph_invalid', 'Graphe vide ou illisible.', { param: 'graphs', detail: { graph: i } });
    if (JSON.stringify(graph).length > MAX_GRAPH) return err('graph_too_large', 'Graphe trop gros (2 Mo par graphe, 8 Mo en tout).', { param: 'graphs', detail: { graph: i } });
    for (const [id, node] of Object.entries(graph)) {
      if (!isObj(node) || typeof node.class_type !== 'string' || !node.class_type) {
        return err('graph_invalid', 'Nœud sans class_type.', { param: 'graphs', detail: { graph: i, node: id } });
      }
      if (node.inputs != null && !isObj(node.inputs)) return err('graph_invalid', '« inputs » doit être un objet.', { param: 'graphs', detail: { graph: i, node: id } });
      for (const [name, v] of Object.entries(node.inputs || {})) {
        if (Array.isArray(v) && v.length === 2 && typeof v[0] === 'string' && Number.isInteger(v[1]) && !(v[0] in graph)) {
          return err('graph_invalid', `Lien vers un nœud absent : ${v[0]}.`, { param: 'graphs', detail: { graph: i, node: id, input: name } });
        }
      }
    }
    return null;
  }

  /** Une valeur interdite dans l'entrée d'un nœud, ou null. */
  function valueError(graph, id, name, value, where) {
    const node = graph[id];
    if (LOADERS[node.class_type] === name && !(typeof value === 'string' && value.startsWith('@in:'))) {
      return err('forbidden_value', "Un fichier d'entrée se donne par un jeton @in:.", { param: where, detail: { node: id, input: name, reason: 'file_input' } });
    }
    for (const t of texts(value, graph)) {
      if (ANNOTATION.test(t)) return err('forbidden_value', 'Annotation de dossier refusée.', { param: where, detail: { node: id, input: name, reason: 'annotation' } });
    }
    return null;
  }

  function itemsOf(body, graphs) {
    if (body.items != null && body.repeat != null) return { error: err('invalid_request', '« items » ou « repeat », pas les deux.', { param: 'items' }) };
    if (body.repeat != null) {
      const r = body.repeat;
      const index = isObj(r) ? (r.graph ?? 0) : -1;
      if (!isObj(r) || !Number.isInteger(r.count) || r.count < 1 || r.count > 256 || !Number.isInteger(index) || !graphs[index]) {
        return { error: err('invalid_request', '« repeat » : {count, vary, graph}.', { param: 'repeat' }) };
      }
      const vary = Array.isArray(r.vary) ? r.vary : [];
      for (const v of vary) {
        const current = isObj(v) && isObj(graphs[index][v.node]) ? (graphs[index][v.node].inputs || {})[v.input] : undefined;
        if (typeof current !== 'number' || typeof v.step !== 'number') {
          return { error: err('patch_invalid', '« vary » : un nombre déjà présent dans le graphe, et un pas.', { param: 'repeat', detail: { node: v && v.node, input: v && v.input } }) };
        }
      }
      return {
        items: Array.from({ length: r.count }, (_, i) => ({
          graph: index,
          label: null,
          seed: null,
          set: vary.map((v) => [v.node, v.input, graphs[index][v.node].inputs[v.input] + i * v.step]),
        })),
      };
    }
    if (body.items == null) return { items: graphs.map((_, i) => ({ graph: i, label: null, seed: null, set: [] })) };
    if (!Array.isArray(body.items) || !body.items.length || body.items.length > 256) {
      return { error: err('invalid_request', "« items » : une liste d'éléments (256 au plus).", { param: 'items' }) };
    }
    const out = [];
    for (const [n, it] of body.items.entries()) {
      const g = isObj(it) ? (it.graph ?? 0) : -1;
      const extra = isObj(it) ? Object.keys(it).filter((k) => !['graph', 'label', 'seed', 'set'].includes(k)) : [];
      const seedOk = !isObj(it) || it.seed == null || (Number.isInteger(it.seed) && it.seed >= 0);
      if (!isObj(it) || extra.length || !Number.isInteger(g) || !graphs[g] || !seedOk || (it.label != null && typeof it.label !== 'string')) {
        return { error: err('invalid_request', 'Élément illisible : {graph, label, seed, set}.', { param: 'items', detail: { item: n } }) };
      }
      out.push({ graph: g, label: it.label ?? null, seed: it.seed ?? null, set: Array.isArray(it.set) ? it.set : [] });
    }
    return { items: out };
  }

  /** La requête vérifiée → { params }, ou { error } (la réponse d'erreur). */
  function normalize(body, app) {
    if (!isObj(body)) return { error: err('invalid_request', 'Corps JSON attendu : un objet.') };
    const unknown = Object.keys(body).filter((k) => !FIELDS.has(k)).sort();
    if (unknown.length) return { error: err('invalid_request', `Champ inconnu : « ${unknown[0]} ».`, { param: unknown[0] }) };
    if ((body.engine ?? 'comfyui') !== 'comfyui') {
      return { error: err('engine_not_found', `Moteur inconnu : « ${body.engine} » (connu : comfyui).`, { param: 'engine' }) };
    }
    if ('graph' in body && 'graphs' in body) return { error: err('invalid_request', '« graph » ou « graphs », pas les deux.', { param: 'graphs' }) };
    if ('recipe' in body) {
      return { error: err('not_supported', "Les recettes arriveront avec une version suivante d'AIODOS : envoie un graphe au format API.", { param: 'recipe' }) };
    }
    const graphs = 'graph' in body ? [body.graph] : body.graphs;
    if (!Array.isArray(graphs) || !graphs.length) return { error: err('invalid_request', 'Un graphe au moins (« graph » ou « graphs »).', { param: 'graphs' }) };
    if (graphs.length > 64) return { error: err('graph_too_large', '64 graphes au plus par travail.', { param: 'graphs' }) };
    for (const [i, g] of graphs.entries()) {
      const e = shapeError(g, i);
      if (e) return { error: e };
    }
    const given = body.inputs ?? {};
    if (!isObj(given)) return { error: err('invalid_request', '« inputs » : {identifiant: {sha256}}.', { param: 'inputs' }) };
    const store = appInputs(app);
    const inputs = {};
    for (const [key, v] of Object.entries(given)) {
      if (!INPUT_ID.test(key) || !isObj(v) || !SHA256_RE.test(String(v.sha256))) {
        return { error: err('invalid_request', 'Entrée illisible : {identifiant: {"sha256": …}}.', { param: 'inputs', detail: { input: key } }) };
      }
      if (!store.has(v.sha256)) {
        return { error: err('input_missing', `Entrée « ${key} » jamais envoyée : PUT /aiodos/v1/inputs/${v.sha256} d'abord.`, { param: 'inputs', detail: { input: key } }) };
      }
      inputs[key] = { sha256: v.sha256 };
    }
    const it = itemsOf(body, graphs);
    if (it.error) return it;
    const declared = body.models ?? 'auto';
    if (declared !== 'auto' && !isObj(declared)) return { error: err('invalid_request', '« models » : "auto" ou {clé: {folder, name}}.', { param: 'models' }) };
    const models = [];
    const addModel = (folder, name) => {
      if (!models.some((m) => m.folder === folder && m.name === name)) models.push({ folder, name });
    };
    if (isObj(declared)) {
      for (const [key, m] of Object.entries(declared)) {
        if (!isObj(m) || typeof m.folder !== 'string' || typeof m.name !== 'string' || !m.name) {
          return { error: err('model_unresolved', 'Modèle déclaré sans dossier ni nom : {"folder", "name"}.', { param: 'models', status: 409, detail: { model: key } }) };
        }
      }
    }
    for (const [i, graph] of graphs.entries()) {
      let outputs = 0;
      for (const [id, node] of Object.entries(graph)) {
        if (fake.comfy.cloudNodes.includes(node.class_type)) {
          return { error: err('forbidden_node', "Nœud d'un service en ligne : refusé.", { param: 'graphs', detail: { graph: i, node: id, class: node.class_type } }) };
        }
        if (OUTPUT_NODES.has(node.class_type)) outputs += 1;
        for (const [name, value] of Object.entries(node.inputs || {})) {
          const bad = valueError(graph, id, name, value, 'graphs');
          if (bad) return { error: bad };
          for (const t of texts(value, graph)) {
            if (t.startsWith('@in:') && !(t.slice(4) in inputs)) {
              return { error: err('input_missing', `Jeton ${t} sans entrée déclarée dans « inputs ».`, { param: 'inputs', detail: { graph: i, input: t.slice(4) } }) };
            }
            if (t.startsWith('@model:')) {
              const key = t.slice(7);
              const m = isObj(declared) ? declared[key] : null;
              if (!m) return { error: err('model_unresolved', `Jeton ${t} sans modèle déclaré dans « models ».`, { param: 'models', status: 409, detail: { graph: i, model: key } }) };
              addModel(m.folder, m.name);
            } else if (declared === 'auto' && name !== LOADERS[node.class_type] && !t.startsWith('@') && MODEL_EXT.test(t)) {
              addModel(null, t.replaceAll('\\', '/'));
            }
          }
        }
      }
      if (!outputs) return { error: err('graph_invalid', "Ce graphe n'a aucun nœud de sortie (SaveImage…).", { param: 'graphs', detail: { graph: i } }) };
    }
    for (const [n, item] of it.items.entries()) {
      const graph = graphs[item.graph];
      for (const patch of item.set) {
        const [id, name, value] = Array.isArray(patch) && patch.length === 3 ? patch : [];
        const current = isObj(graph[id]) ? (graph[id].inputs || {})[name] : undefined;
        const sameType = current !== undefined && typeof current === typeof value && Array.isArray(current) === Array.isArray(value);
        if (current === undefined || isLink(current, graph) || !sameType) {
          return { error: err('patch_invalid', '« set » remplace une valeur déjà présente, du même type, jamais un lien.', { param: 'items', detail: { item: n, node: id, input: name } }) };
        }
        const bad = valueError(graph, id, name, value, 'items');
        if (bad) return { error: bad };
      }
      const seeded = Object.values(graph).some((node) => Object.values(node.inputs || {}).some((v) => v === '@seed'));
      if (seeded && item.seed == null) item.seed = randomBytes(4).readUInt32BE(0);
    }
    const priority = body.priority ?? 'normal';
    if (!['high', 'normal', 'low'].includes(priority)) return { error: err('invalid_request', '« priority » : high, normal ou low.', { param: 'priority' }) };
    if (body.label != null && (typeof body.label !== 'string' || body.label.length > 200)) {
      return { error: err('invalid_request', 'Libellé : un texte de 200 caractères au plus.', { param: 'label' }) };
    }
    const ref = body.client_ref;
    if (ref != null && (typeof ref !== 'string' || !ref || ref.length > 200)) {
      return { error: err('invalid_request', '« client_ref » : un texte de 200 caractères au plus.', { param: 'client_ref' }) };
    }
    if (body.bounds != null && (!isObj(body.bounds) || Object.keys(body.bounds).some((k) => !BOUNDS.has(k)))) {
      return { error: err('invalid_request', '« bounds » : {unit_seconds, output_mb, wait_s}.', { param: 'bounds' }) };
    }
    return {
      params: { graphs, inputs, items: it.items, models, priority, label: body.label ?? null, clientRef: ref ?? null, strict: Boolean(body.strict) },
    };
  }

  const missingModels = (params) =>
    params.models.filter((m) => !fake.comfy.models.some((x) => x.name === m.name && (!m.folder || x.folder === m.folder))).map((m) => ({ folder: m.folder, name: m.name }));

  // --- les travaux --------------------------------------------------------------------------

  function fileCard(f) {
    return {
      id: f.id,
      unit: f.unit,
      node: f.node,
      type: f.type,
      bytes: f.content.length,
      sha256: f.sha256,
      width: f.width,
      height: f.height,
      machine: fake.comfy.machine,
      place: 'here',
      engine: `comfyui ${fake.comfy.version}`,
      verified: 'none',
      fetched: f.fetchedAt != null,
      expires_at: f.fetchedAt != null ? Math.floor(f.fetchedAt + KEEP_DAYS * 86400) : null,
      created_at: Math.floor(f.createdAt),
    };
  }

  /** L'objet travail, comme jobs_api.job_object. */
  function jobObject(job) {
    const counts = { queued: 0, running: 0, done: 0, failed: 0, cancelled: 0 };
    const machines = new Map();
    const stages = [];
    const units = job.units.map((u) => {
      counts[u.status] += 1;
      const busy = u.status === 'running' || u.status === 'done';
      if (busy) {
        const m = machines.get(fake.comfy.machine) ?? { name: fake.comfy.machine, place: 'here', running: 0, done: 0 };
        m[u.status === 'running' ? 'running' : 'done'] += 1;
        machines.set(fake.comfy.machine, m);
      }
      if (u.status === 'running') stages.push(u.stage);
      const out = {
        index: u.index,
        label: u.label,
        status: UNIT_SHOWN[u.status] ?? u.status,
        place: busy ? 'here' : null,
        machine: busy ? fake.comfy.machine : null,
        files: [...u.files],
        seconds: u.seconds,
        stage: u.status === 'running' ? u.stage : u.status === 'queued' ? 'waiting_machine' : null,
      };
      if (u.status === 'failed' && u.error) out.error = { ...u.error };
      return out;
    });
    const total = units.length;
    let status = SHOWN[job.status];
    if (job.status === 'paused' && counts.running) status = 'pausing';
    if (job.status === 'cancelled' && counts.running) status = 'cancelling';
    const percent = total ? Math.floor((100 * (counts.done + counts.failed)) / total) : 100;
    let message = `${counts.done} sur ${total}`;
    if (status === 'completed') message += ' : fini';
    else if (status === 'paused' || status === 'pausing') message += ' · en pause';
    else if (status === 'cancelled' || status === 'cancelling') message += ' · annulé';
    else if (counts.running) message += ` · ${counts.running} en cours sur ${machines.size} machine${machines.size > 1 ? 's' : ''}`;
    return {
      id: job.id,
      object: 'aiodos.job',
      engine: 'comfyui',
      label: job.label,
      client_ref: job.clientRef,
      status,
      pause: job.status === 'paused' ? { when: job.pauseMode || 'after_current', pending: counts.running } : null,
      priority: job.priority,
      where: [...job.where],
      where_effective: ['here'],
      where_reason: null,
      progress: {
        total,
        done: counts.done,
        running: counts.running,
        queued: counts.queued,
        failed: counts.failed,
        percent,
        estimated: true,
        elapsed_s: job.startedAt ? Math.floor(now() - job.startedAt) : 0,
        eta_s: null,
        stage: FINISHED.has(status) ? 'done' : stages.length ? stages.at(-1) : 'queued',
        machines: [...machines.values()],
        message,
      },
      waiting: null,
      units,
      files: job.files.map((id) => fake.files.get(id)).filter(Boolean).map(fileCard),
      error: job.error ? { code: 'all_failed', message: job.error } : null,
      cancelled_by: job.cancelledBy,
      created_at: Math.floor(job.createdAt),
      started_at: job.startedAt ? Math.floor(job.startedAt) : null,
      completed_at: job.completedAt ? Math.floor(job.completedAt) : null,
    };
  }

  const notify = (job) => {
    for (const fn of [...job.listeners]) fn();
  };

  function produce(job, u) {
    const graph = job.graphs[u.graph] || {};
    const node = Object.keys(graph).find((id) => OUTPUT_NODES.has(graph[id].class_type)) ?? '9';
    for (let k = 0; k < job.filesPerUnit; k += 1) {
      const id = `file_${randomBytes(16).toString('base64url')}`;
      fake.files.set(id, { id, app: job.app, job: job.id, unit: u.index, node, type: 'image/png', content: FAKE_PNG, sha256: PNG_SHA, width: 8, height: 8, createdAt: now(), fetchedAt: null, served: 0 });
      u.files.push(id);
      job.files.push(id);
    }
    u.status = 'done';
    u.stage = null;
    u.seconds = 0.1;
  }

  /** La fin, quand plus rien n'est en file ni en cours. */
  function settle(job) {
    const left = job.units.some((u) => u.status === 'queued' || u.status === 'running');
    if (left || !ACTIVE.has(job.status)) {
      if (job.status === 'cancelled' && !job.units.some((u) => u.status === 'running')) job.completedAt ??= now();
      return;
    }
    const done = job.units.some((u) => u.status === 'done');
    job.status = done ? 'done' : 'failed';
    job.completedAt = now();
    if (!done) job.error = "Aucune unité n'a pu se faire.";
  }

  /** Une étape : finir les unités en cours, sinon en démarrer une. Faux s'il n'y avait rien. */
  function step(job) {
    if (!fake.jobs.has(job.id)) return false;
    const running = job.units.filter((u) => u.status === 'running');
    if (running.length) {
      for (const u of running) {
        u.stage = null;
        if (job.status === 'cancelled') u.status = 'cancelled';
        else if (job.failUnits.has(u.index)) {
          u.status = 'failed';
          u.error = { code: 'engine_error', message: "engine_error : KSampler a échoué : mémoire vidéo insuffisante (nœud 3)" };
        } else produce(job, u);
      }
    } else if (job.status === 'queued' || job.status === 'running') {
      const next = job.units.find((u) => u.status === 'queued');
      if (!next) return false;
      next.status = 'running';
      next.stage = 'running';
      job.status = 'running';
      job.startedAt ??= now();
    } else return false;
    settle(job);
    notify(job);
    return true;
  }

  const moving = (job) => fake.jobs.has(job.id) && (job.status === 'queued' || job.status === 'running' || job.units.some((u) => u.status === 'running'));

  function schedule(job) {
    if (fake.jobMode !== 'auto' || job.timer || !moving(job)) return;
    job.timer = setTimeout(() => {
      job.timer = null;
      if (step(job)) schedule(job);
    }, fake.jobStepMs);
  }

  function createJob(app, p, circles) {
    const script = jobScripts.shift() || {};
    seq += 1;
    const job = {
      id: `job_${randomBytes(12).toString('base64url')}`,
      seq,
      app,
      label: p.label,
      clientRef: p.clientRef,
      priority: p.priority,
      where: circles,
      status: 'queued',
      pauseMode: null,
      cancelledBy: null,
      error: null,
      createdAt: now(),
      startedAt: null,
      completedAt: null,
      graphs: p.graphs,
      units: p.items.map((it, index) => ({ index, graph: it.graph, label: it.label, seed: it.seed, status: 'queued', stage: null, files: [], seconds: null, error: null })),
      files: [],
      listeners: new Set(),
      timer: null,
      failUnits: new Set(script.failUnits || []),
      filesPerUnit: script.filesPerUnit ?? 1,
    };
    fake.jobs.set(job.id, job);
    schedule(job);
    return job;
  }

  const ownJob = (g, id) => {
    const job = fake.jobs.get(id);
    return job && job.app === g.app ? job : null;
  };
  const ownFile = (g, id) => {
    const f = fake.files.get(id);
    return f && f.app === g.app ? f : null;
  };

  // --- les routes ----------------------------------------------------------------------------

  function enginesList(req, g) {
    const c = circlesOf(req, g.entry);
    if (c.error) return c.error;
    const here = c.circles.includes('here');
    const on = here && fake.comfy.enabled;
    return ok({
      object: 'list',
      engines: [
        {
          id: 'comfyui',
          label: 'ComfyUI',
          here: here ? (on ? { enabled: true, version: fake.comfy.version, vram_mib: fake.comfy.vramMib } : { enabled: false, version: null, vram_mib: null }) : null,
          machines: { here: on ? 1 : 0, mine: null, friends: null, public: null },
          models: on
            ? fake.comfy.models.map((m) => ({ folder: m.folder, name: m.name, sha256: null, size: null, where: { here: true, mine: [], friends: null, public: null }, license: null }))
            : [],
          object_info: { available: on, machine: on ? fake.comfy.machine : null, digest: on ? 'c0a1f3e29b7d4e51' : null },
          recipes: 0,
        },
      ],
    });
  }

  function plan(req, g) {
    if (!isObj(req.json)) return err('invalid_request', 'Corps JSON attendu : un objet.');
    const c = circlesOf(req, g.entry);
    if (c.error) return c.error;
    const { where: _where, ...body } = req.json;
    const n = normalize(body, g.app);
    if (n.error) return n.error;
    const found = c.circles.includes('here') && fake.comfy.enabled;
    const missing = found ? missingModels(n.params).map((m) => ({ ...m, machine: fake.comfy.machine })) : [];
    return ok({
      ok: found && !missing.length,
      where_effective: found ? ['here'] : [],
      where_reason: found ? null : "ComfyUI n'est pas ouvert ou pas accordé sur ce PC",
      machines: { here: found, mine: null, friends: null, public: null },
      missing,
      refused: {},
      licenses: [],
      units: n.params.items.length,
      estimate: null,
    });
  }

  function submit(req, g) {
    const raw = req.json;
    if (!isObj(raw)) return err('invalid_request', 'Corps JSON attendu : un objet.');
    const key = raw.client_ref || req.headers['idempotency-key'] || null;
    const body = key && !raw.client_ref ? { ...raw, client_ref: String(key).slice(0, 200) } : raw;
    if (body.client_ref) {
      const old = [...fake.jobs.values()].find((j) => j.app === g.app && j.clientRef === body.client_ref);
      if (old) return ok(jobObject(old), 200);
    }
    const c = circlesOf(req, g.entry);
    if (c.error) return c.error;
    let circles = c.circles;
    if (body.where != null) {
      const asked = parseWhere(body.where);
      if (!asked) return err('invalid_request', '« where » : une liste de cercles (here, mine, friends, public).', { param: 'where' });
      const wider = asked.filter((x) => !circles.includes(x));
      if (wider.length) return err('where_denied', "Lieu de calcul refusé : plus large que l'accès de cette application.", { detail: { allowed: circles } });
      circles = asked;
    }
    const active = [...fake.jobs.values()].filter((j) => j.app === g.app && ACTIVE.has(j.status)).length;
    if (active >= fake.maxActiveJobs) {
      return err('quota_exceeded', `${fake.maxActiveJobs} travaux actifs au plus par application.`, { detail: { quota: 'active_jobs', max: fake.maxActiveJobs } });
    }
    const { where: _where, ...rest } = body;
    const n = normalize(rest, g.app);
    if (n.error) return n.error;
    if (!circles.includes('here') || !fake.comfy.enabled) {
      return err('engine_unavailable', "Aucune machine permise n'a ComfyUI ouvert et accordé à AIODOS (sur ce PC : Réglages → Moteurs).", {
        status: 409,
        detail: { engine: 'comfyui', where: circles },
      });
    }
    const missing = missingModels(n.params);
    if (missing.length) {
      return err('model_unavailable', `Il manque des modèles au ComfyUI de ce PC : ${missing.map((m) => m.name).join(', ')}.`, {
        hint: 'Installe les modèles qui manquent (detail.models), ou élargis le lieu de calcul.',
        detail: { models: missing },
      });
    }
    const job = createJob(g.app, n.params, circles);
    return ok(jobObject(job), 202, { Location: `/aiodos/v1/jobs/${job.id}` });
  }

  function listJobs(req, g) {
    const q = req.url.searchParams;
    let jobs = [...fake.jobs.values()].filter((j) => j.app === g.app);
    const active = q.get('active');
    if (active === '1' || active === 'true') jobs = jobs.filter((j) => ACTIVE.has(j.status));
    else if (active === '0' || active === 'false') jobs = jobs.filter((j) => !ACTIVE.has(j.status));
    if (q.get('client_ref')) jobs = jobs.filter((j) => j.clientRef === q.get('client_ref'));
    const wanted = new Set((q.get('status') || '').split(',').map((s) => s.trim()).filter(Boolean));
    if (wanted.size) jobs = jobs.filter((j) => wanted.has(SHOWN[j.status] ?? j.status));
    const limit = Math.max(1, Math.min(200, Number.parseInt(q.get('limit') || '50', 10) || 50));
    jobs.sort((a, b) => b.seq - a.seq);
    return ok({ object: 'list', jobs: jobs.slice(0, limit).map(jobObject) });
  }

  /**
   * Les octets servis : à son dernier octet, et une fois servis autant d'octets que sa taille
   * (en une fois, ou par plages), récupéré. Ceux d'une réponse coupée comptent aussi : un
   * téléchargement repris jusqu'au bout est récupéré (conception, partage-puissance.md § 5.3).
   */
  function served(f, start, end) {
    f.served += end - start + 1;
    if (f.fetchedAt == null && end + 1 >= f.content.length && f.served >= f.content.length) f.fetchedAt = now();
  }

  function content(req, f) {
    const size = f.content.length;
    const headers = api({
      'Accept-Ranges': 'bytes',
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'X-Aiodos-Keep-Until': f.fetchedAt == null ? 'pending' : String(Math.floor(f.fetchedAt + KEEP_DAYS * 86400)),
      ETag: `"${f.sha256}"`,
      'Content-Type': f.type,
    });
    let span = null;
    const m = /^bytes=(\d*)-(\d*)$/.exec(String(req.headers.range || '').trim());
    if (m && (m[1] || m[2])) {
      span = m[1] ? [Number(m[1]), m[2] ? Math.min(Number(m[2]), size - 1) : size - 1] : [Math.max(0, size - Number(m[2])), size - 1];
      if (span[0] >= size || span[1] < span[0]) {
        delete headers['Content-Type'];
        return { status: 416, headers: { ...headers, 'Content-Range': `bytes */${size}` }, text: '' };
      }
    }
    const [start, end] = span || [0, size - 1];
    if (span) headers['Content-Range'] = `bytes ${start}-${end}/${size}`;
    const bytes = f.content.subarray(start, end + 1);
    const cut = contentScripts.shift();
    const cutAt = cut && cut.cutAt != null && cut.cutAt < bytes.length ? cut.cutAt : null;
    if (cutAt == null) served(f, start, end);
    else f.served += cutAt; // coupée avant le dernier octet : comptés, jamais récupéré par elle seule
    return { status: span ? 206 : 200, headers, bytes, cutAt };
  }

  /** Le flux d'événements d'un travail : en direct, jusqu'à « end ». */
  function events(job) {
    const spec = eventScripts.shift() || {};
    return {
      status: 200,
      headers: api({ 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' }),
      live: (sink) => {
        let last = null;
        let sent = 0;
        let over = false;
        const seen = new Set();
        const quiet = () => spec.stallAfter != null && sent >= spec.stallAfter;
        const stream = { cut: () => drop(() => sink.cut()) };
        const stop = () => {
          over = true;
          job.listeners.delete(push);
          clearInterval(ping);
          liveStreams.delete(stream);
        };
        const drop = (how) => {
          if (over) return;
          stop();
          how();
        };
        const write = (kind, data) => {
          if (over || quiet()) return;
          if (spec.cutAfter != null && sent >= spec.cutAfter) {
            drop(() => sink.cut());
            return;
          }
          sink.send(`event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`);
          sent += 1;
        };
        function push() {
          if (over) return;
          if (!fake.jobs.has(job.id)) {
            write('end', { id: job.id, status: 'deleted' });
            drop(() => sink.close());
            return;
          }
          const obj = jobObject(job);
          for (const f of obj.files) {
            if (seen.has(f.id)) continue;
            seen.add(f.id);
            if (last !== null) write('file', f);
          }
          const text = JSON.stringify(obj);
          if (text !== last) {
            last = text;
            write('job', obj);
          }
          if (FINISHED.has(obj.status)) {
            write('end', { id: job.id, status: obj.status });
            if (!quiet()) drop(() => sink.close());
          }
        }
        const ping = setInterval(() => {
          if (!over && !quiet()) sink.send(': ping\n\n');
        }, fake.pingMs);
        if (typeof ping.unref === 'function') ping.unref();
        job.listeners.add(push);
        liveStreams.add(stream);
        push();
        return stop; // le client a fermé
      },
    };
  }

  function command(req, g, job, action) {
    const active = ACTIVE.has(job.status);
    if (action === 'pause') {
      const when = isObj(req.json) && req.json.when != null ? req.json.when : 'after_current';
      if (when !== 'after_current' && when !== 'now') return err('invalid_request', '« when » : after_current ou now.', { param: 'when' });
      if (!active) return err('job_not_pausable', 'Ce travail est déjà fini.');
      job.status = 'paused';
      job.pauseMode = when;
      if (when === 'now') {
        for (const u of job.units) {
          if (u.status === 'running') {
            u.status = 'queued'; // rendue sans compter d'essai
            u.stage = null;
          }
        }
      }
    } else if (action === 'resume') {
      if (!active) return err('job_not_pausable', 'Ce travail est déjà fini.');
      if (job.status === 'paused') {
        job.status = job.startedAt ? 'running' : 'queued';
        job.pauseMode = null;
      }
    } else if (action === 'cancel') {
      if (!active) return err('job_not_cancellable', 'Ce travail est déjà fini.');
      job.status = 'cancelled';
      job.cancelledBy = 'app';
      for (const u of job.units) if (u.status === 'queued') u.status = 'cancelled';
    } else if (action === 'retry') {
      const failed = job.units.filter((u) => u.status === 'failed');
      for (const u of failed) {
        u.status = 'queued';
        u.error = null;
        job.failUnits.delete(u.index);
      }
      if (failed.length && !ACTIVE.has(job.status)) {
        job.status = job.startedAt ? 'running' : 'queued';
        job.completedAt = null;
        job.error = null;
      }
    }
    settle(job);
    notify(job);
    schedule(job);
    return ok(jobObject(job), 202);
  }

  function forget(req, g, job) {
    if (ACTIVE.has(job.status)) return err('job_not_cancellable', "Annule le travail avant de l'oublier.");
    if (req.url.searchParams.get('files') === 'delete') for (const id of job.files) fake.files.delete(id);
    fake.jobs.delete(job.id);
    if (job.timer) clearTimeout(job.timer);
    notify(job);
    return ok({ id: job.id, deleted: true });
  }

  function route(req) {
    const p = req.url.pathname;
    const m = req.method;
    const engineRoute = p.startsWith('/aiodos/v1/engines') || p.startsWith('/aiodos/v1/inputs/') || p.startsWith('/aiodos/v1/jobs') || p.startsWith('/aiodos/v1/files/');
    if (!engineRoute) return null;
    const g = guard(req);
    if (g.error) return g.error;
    let r;
    if (p === '/aiodos/v1/engines' && m === 'GET') return enginesList(req, g);
    if (p === '/aiodos/v1/engines/comfyui/plan' && m === 'POST') return plan(req, g);
    if ((r = /^\/aiodos\/v1\/inputs\/([^/]+)$/.exec(p)) && (m === 'HEAD' || m === 'PUT')) {
      const sha = decodeURIComponent(r[1]);
      const store = appInputs(g.app);
      if (m === 'HEAD') {
        const known = store.get(sha);
        return known ? { status: 200, headers: api({ 'X-Aiodos-Size': String(known.size), 'X-Aiodos-Type': known.type }) } : { status: 404, headers: api() };
      }
      if (!SHA256_RE.test(sha)) return err('invalid_request', 'Empreinte attendue : 64 caractères hexadécimaux en minuscules (SHA-256).', { param: 'sha256' });
      const known = store.get(sha);
      if (known) return ok({ sha256: sha, size: known.size, type: known.type }, 200);
      const bytes = req.bytes || Buffer.alloc(0);
      if (bytes.length > MAX_INPUT) return err('input_too_large', 'Entrée trop lourde : 100 Mo au plus.', { detail: { max_bytes: MAX_INPUT } });
      const actual = sha256(bytes);
      if (actual !== sha) {
        return err('input_hash_mismatch', "L'empreinte du fichier reçu ne correspond pas à celle de l'adresse.", { hint: 'Calcule le SHA-256 des octets envoyés.', detail: { sha256: actual } });
      }
      const type = sniffType(bytes.subarray(0, 16));
      if (!INPUT_TYPES.has(type)) {
        return err('unsupported_media_type', 'Genre de fichier non reconnu : images (PNG, JPEG, WebP, GIF), vidéos (MP4, WebM) ou sons (WAV, MP3, FLAC, OGG).', {
          detail: { declared: req.headers['content-type'] ?? null },
        });
      }
      store.set(sha, { sha256: sha, size: bytes.length, type, bytes: Buffer.from(bytes) });
      return ok({ sha256: sha, size: bytes.length, type }, 201);
    }
    if (p === '/aiodos/v1/jobs' && m === 'POST') return submit(req, g);
    if (p === '/aiodos/v1/jobs' && m === 'GET') return listJobs(req, g);
    if ((p === '/aiodos/v1/jobs/pause' || p === '/aiodos/v1/jobs/resume') && m === 'POST') {
      const pausing = p.endsWith('/pause');
      const when = isObj(req.json) && req.json.when != null ? req.json.when : 'after_current';
      if (pausing && when !== 'after_current' && when !== 'now') return err('invalid_request', '« when » : after_current ou now.', { param: 'when' });
      const mine = [...fake.jobs.values()].filter((j) => j.app === g.app && (pausing ? j.status === 'queued' || j.status === 'running' : j.status === 'paused'));
      for (const job of mine) command({ json: { when } }, g, job, pausing ? 'pause' : 'resume');
      return ok(pausing ? { paused: mine.length } : { resumed: mine.length });
    }
    if ((r = /^\/aiodos\/v1\/jobs\/([^/]+)(?:\/(events|files|pause|resume|cancel|retry))?$/.exec(p))) {
      const job = ownJob(g, decodeURIComponent(r[1]));
      const what = r[2] ?? null;
      if (!job) return notFound('job');
      if (!what && m === 'GET') return ok(jobObject(job));
      if (!what && m === 'DELETE') return forget(req, g, job);
      if (what === 'events' && m === 'GET') return events(job);
      if (what === 'files' && m === 'GET') {
        const unfetched = ['1', 'true'].includes(req.url.searchParams.get('unfetched'));
        const files = job.files.map((id) => fake.files.get(id)).filter((f) => f && (!unfetched || f.fetchedAt == null));
        return ok({ object: 'list', files: files.map(fileCard) });
      }
      if (what && what !== 'events' && what !== 'files' && m === 'POST') return command(req, g, job, what);
    }
    if ((r = /^\/aiodos\/v1\/files\/([^/]+)(\/content)?$/.exec(p))) {
      const f = ownFile(g, decodeURIComponent(r[1]));
      if (!f) return notFound('file');
      if (!r[2] && m === 'GET') return ok(fileCard(f));
      if (!r[2] && m === 'DELETE') {
        fake.files.delete(f.id);
        return ok({ id: f.id, deleted: true });
      }
      if (r[2] && m === 'GET') return content(req, f);
    }
    return err('not_supported', `Route inconnue : ${m} ${p}`);
  }

  return { route };
}
