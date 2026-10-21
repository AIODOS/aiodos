// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — la conversation : POST /v1/chat/completions, toujours en flux
// (docs/api-applications.md § 5.5).
//
// - messages et outils au format neutre d'ARKLIGHT (neutral.js) ;
// - think: false → reasoning_effort "none" ; where → X-Aiodos-Where ;
//   batch → X-Aiodos-Priority: background ; requireTools: false → X-Aiodos-Tools: optional ;
// - délai d'inactivité remis à zéro à chaque morceau et à chaque « : aiodos loading N » ;
//   avant le premier, loadTimeout si le modèle n'est pas chargé ;
// - morceaux de tool_calls rassemblés par index, arguments analysés en JSON (illisibles :
//   args null, le texte gardé dans raw) ;
// - une erreur en route (data: {"error": …}) devient une AiodosError ; l'annulation ferme
//   vraiment la connexion.

import { AiodosError, clientError, errorFromResponse, makeError } from './errors.js';
import { models } from './models.js';
import { parseToolArguments, toOpenAIMessages, toOpenAITools } from './neutral.js';
import { loadingSeconds, readSse } from './sse.js';
import { checkWhere } from './util.js';

const LOADED_TTL = 30_000;
const PROBE_TIMEOUT = 4000;

/** Une erreur levée par un rappel de l'appli (onText…) : rendue telle quelle. */
class CallbackFailure {
  constructor(err) {
    this.err = err;
  }
}

function call(fn, arg) {
  if (typeof fn !== 'function') return;
  try {
    fn(arg);
  } catch (err) {
    throw new CallbackFailure(err);
  }
}

function decodeHeader(v) {
  if (v == null) return null;
  try {
    return decodeURIComponent(v);
  } catch {
    return v;
  }
}

function usageOf(u) {
  if (!u || typeof u !== 'object') return null;
  return {
    promptTokens: typeof u.prompt_tokens === 'number' ? u.prompt_tokens : null,
    completionTokens: typeof u.completion_tokens === 'number' ? u.completion_tokens : null,
    totalTokens: typeof u.total_tokens === 'number' ? u.total_tokens : null,
  };
}

/** Le modèle est-il chargé ? true, false, ou null (on ne sait pas). */
async function knownLoaded(ctx, model, signal) {
  const hit = ctx.loaded.get(model);
  if (hit && Date.now() - hit.at < LOADED_TTL) return hit.loaded;
  try {
    await models(ctx, { kind: 'chat', signal, timeout: PROBE_TIMEOUT });
  } catch (err) {
    if (err && err.code === 'cancelled') throw err;
    return null; // la conversation dira ce qui ne va pas
  }
  const again = ctx.loaded.get(model);
  return again ? again.loaded : null;
}

function addToolDelta(calls, tc) {
  let index = Number.isInteger(tc.index) ? tc.index : null;
  if (index == null) {
    // sans index : un nouvel identifiant ouvre un nouvel appel, sinon on complète le dernier
    const last = calls.size ? Math.max(...calls.keys()) : -1;
    const lastCall = last >= 0 ? calls.get(last) : null;
    index = last < 0 || (tc.id && lastCall.id && tc.id !== lastCall.id) ? last + 1 : last;
  }
  let c = calls.get(index);
  if (!c) {
    c = { id: null, name: '', args: '' };
    calls.set(index, c);
  }
  if (typeof tc.id === 'string' && tc.id && !c.id) c.id = tc.id;
  const fn = tc.function || {};
  if (typeof fn.name === 'string' && fn.name && !c.name) c.name = fn.name;
  if (typeof fn.arguments === 'string') c.args += fn.arguments;
  else if (fn.arguments && typeof fn.arguments === 'object') c.args += JSON.stringify(fn.arguments);
}

function finishCalls(calls) {
  return [...calls.entries()]
    .sort((a, b) => a[0] - b[0])
    .filter(([, c]) => c.name)
    .map(([i, c]) => {
      const { args, raw } = parseToolArguments(c.args);
      return { id: c.id || `call_${i + 1}`, name: c.name, args, raw };
    });
}

/** Une réponse sans flux (un serveur qui n'aurait pas suivi stream: true). */
function fromWholeResponse(d, r, lang) {
  if (!d || typeof d !== 'object') throw clientError('protocol', lang);
  if (d.error) throw errorFromResponse({ status: null, json: d, lang });
  const choice = Array.isArray(d.choices) ? d.choices[0] : null;
  const msg = (choice && choice.message) || {};
  r.text = typeof msg.content === 'string' ? msg.content : '';
  r.thinking = typeof msg.reasoning_content === 'string' ? msg.reasoning_content : '';
  const calls = new Map();
  (Array.isArray(msg.tool_calls) ? msg.tool_calls : []).forEach((tc, i) => addToolDelta(calls, { ...tc, index: i }));
  r.finish = (choice && choice.finish_reason) || null;
  return { calls, meta: d.aiodos, model: d.model, usage: d.usage };
}

/**
 * Une conversation. Rend { text, thinking, toolCalls: [{ id, name, args, raw }], finish,
 * stoppedBy, truncated, usage, answeredBy, machine, place, contextLength }.
 */
export async function chat(ctx, o = {}) {
  const lang = ctx.lang();
  const {
    model,
    system,
    messages = [],
    tools,
    think,
    temperature,
    maxTokens,
    topP,
    stop,
    seed,
    where,
    batch,
    requireTools,
    signal,
    onText,
    onThinking,
    onLoading,
    timeout = ctx.timeout,
    loadTimeout = ctx.loadTimeout,
  } = o;
  if (typeof model !== 'string' || !model.trim()) {
    throw makeError('invalid_request', lang === 'en' ? 'Choose a model.' : 'Choisis un modèle.', { param: 'model' });
  }
  if (!Array.isArray(messages)) {
    throw makeError('invalid_request', lang === 'en' ? 'messages must be a list.' : 'messages doit être une liste.', { param: 'messages' });
  }

  const body = { model, stream: true, stream_options: { include_usage: true }, messages: toOpenAIMessages({ system, messages }) };
  if (Array.isArray(tools) && tools.length) body.tools = toOpenAITools(tools);
  if (think === false) body.reasoning_effort = 'none';
  else if (typeof think === 'string' && think) body.reasoning_effort = think;
  if (temperature != null) body.temperature = temperature;
  if (maxTokens != null) body.max_tokens = maxTokens;
  if (topP != null) body.top_p = topP;
  if (stop != null) body.stop = stop;
  if (seed != null) body.seed = seed;
  const headers = {};
  if (where != null) headers['X-Aiodos-Where'] = checkWhere(where, lang).join(',');
  if (batch) headers['X-Aiodos-Priority'] = 'background';
  if (requireTools === false) headers['X-Aiodos-Tools'] = 'optional';

  const { origin } = await ctx.resolveOrigin();
  const loaded = await knownLoaded(ctx, model, signal);
  if (loaded === false && typeof onLoading === 'function') onLoading({ seconds: 0 }); // prévenu avant d'attendre
  const firstTimeout = loaded === false ? Math.max(loadTimeout, timeout) : timeout;

  const h = await ctx.session.keyed({
    origin,
    method: 'POST',
    path: '/v1/chat/completions',
    json: body,
    headers,
    signal,
    timeout: firstTimeout,
    stream: true,
    feature: 'chat',
  });
  if (!h.stream) ctx.transport.expectOk(h); // une réponse d'erreur avant le flux

  const r = {
    text: '',
    thinking: '',
    toolCalls: [],
    finish: null,
    stoppedBy: null,
    truncated: false,
    usage: null,
    answeredBy: null,
    machine: null,
    place: null,
    contextLength: null,
  };
  const hdr = h.res.headers;
  let calls = new Map();
  let meta = null;
  let chunkModel = null;
  let usage = null;
  let complete = false;
  try {
    if (/application\/json/i.test(hdr.get('content-type') || '')) {
      let d = null;
      try {
        d = JSON.parse(await h.res.text());
      } catch (err) {
        if (err instanceof SyntaxError) throw clientError('protocol', lang);
        throw err;
      }
      const whole = fromWholeResponse(d, r, lang);
      ({ calls, meta, usage } = whole);
      chunkModel = whole.model;
      if (r.text) call(onText, r.text);
      if (r.thinking) call(onThinking, r.thinking);
      complete = true;
    } else {
      for await (const ev of readSse(h.res.body)) {
        h.timer.reset(timeout); // signe de vie
        if (ev.type === 'comment') {
          const s = loadingSeconds(ev.text);
          if (s != null) call(onLoading, { seconds: s });
          continue;
        }
        if (ev.data === '[DONE]') {
          complete = true;
          break;
        }
        let d;
        try {
          d = JSON.parse(ev.data);
        } catch {
          throw clientError('protocol', lang);
        }
        if (!d || typeof d !== 'object') throw clientError('protocol', lang);
        if (d.error) throw errorFromResponse({ status: null, json: d, lang });
        if (typeof d.model === 'string') chunkModel = d.model;
        if (d.aiodos && typeof d.aiodos === 'object') meta = d.aiodos;
        if (d.usage && typeof d.usage === 'object') usage = d.usage;
        const choice = Array.isArray(d.choices) ? d.choices[0] : null;
        if (!choice) continue;
        const delta = choice.delta || {};
        if (typeof delta.content === 'string' && delta.content) {
          r.text += delta.content;
          call(onText, delta.content);
        }
        const thought =
          typeof delta.reasoning_content === 'string' ? delta.reasoning_content : typeof delta.reasoning === 'string' ? delta.reasoning : '';
        if (thought) {
          r.thinking += thought;
          call(onThinking, thought);
        }
        if (Array.isArray(delta.tool_calls)) for (const tc of delta.tool_calls) if (tc && typeof tc === 'object') addToolDelta(calls, tc);
        if (choice.finish_reason) r.finish = choice.finish_reason;
      }
    }
  } catch (err) {
    h.abort(); // ferme la connexion : erreur en route, rappel qui a levé, annulation
    if (err instanceof CallbackFailure) throw err.err;
    if (signal && signal.aborted) throw clientError('cancelled', lang);
    if (err instanceof AiodosError) throw err;
    throw h.fail(err);
  } finally {
    h.done();
  }
  if (!complete && !r.finish) throw clientError('protocol', lang, { textKey: 'cut' });

  r.toolCalls = finishCalls(calls);
  const m = meta && typeof meta === 'object' ? meta : {};
  r.stoppedBy = typeof m.stopped_by === 'string' ? m.stopped_by : null;
  r.answeredBy = (typeof m.model === 'string' && m.model) || decodeHeader(hdr.get('x-aiodos-model')) || chunkModel || null;
  r.machine = (typeof m.machine === 'string' && m.machine) || decodeHeader(hdr.get('x-aiodos-machine')) || null;
  r.place = (typeof m.place === 'string' && m.place) || hdr.get('x-aiodos-place') || null;
  r.contextLength = typeof m.context_length === 'number' ? m.context_length : null;
  r.usage = usageOf(usage);
  r.truncated = r.finish === 'length';
  return r;
}
