// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — les erreurs : AiodosError, ses sous-classes, et la lecture des
// réponses d'erreur de la passerelle (enveloppe d'OpenAI, forme d'Ollama, forme d'OAuth).
//
// Une clé n'apparaît jamais dans une erreur : les textes venus du serveur passent par
// redact(), et toJSON() ne rend que des champs sûrs.

import { codeText, hintText } from './texts.js';

// une clé d'appli (aiodos_app_<id>_<secret>) ou la clé générale (aiodos-<43 caractères>)
const APP_KEY_RE = /aiodos_app_([a-z0-9]{8})_[A-Za-z0-9_-]{20,}/g;
const GENERAL_KEY_RE = /aiodos-[A-Za-z0-9_-]{40,}/g;
const BEARER_RE = /(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}/gi;

/** Masque toute clé dans un texte (l'identifiant public d'une clé d'appli reste lisible). */
export function redact(value) {
  if (typeof value !== 'string') return value;
  return value.replace(APP_KEY_RE, 'aiodos_app_$1_…').replace(GENERAL_KEY_RE, 'aiodos-…').replace(BEARER_RE, '$1…');
}

/** redact() appliqué à toutes les chaînes d'une valeur JSON. */
export function redactDeep(value, depth = 0) {
  if (typeof value === 'string') return redact(value);
  if (!value || typeof value !== 'object' || depth > 8) return value;
  if (Array.isArray(value)) return value.map((v) => redactDeep(v, depth + 1));
  const out = {};
  for (const [k, v] of Object.entries(value)) out[k] = redactDeep(v, depth + 1);
  return out;
}

/** L'erreur commune : un code stable, un message (français par défaut), un conseil, un détail. */
export class AiodosError extends Error {
  /**
   * @param {string} code code stable (docs/api-applications.md § 3)
   * @param {string} message
   * @param {object} [o]
   */
  constructor(code, message, o = {}) {
    super(redact(String(message ?? code)), o.cause !== undefined ? { cause: o.cause } : undefined);
    this.name = new.target.name;
    this.code = code;
    this.status = o.status ?? null;
    this.hint = o.hint == null ? null : redact(String(o.hint));
    this.retryable = Boolean(o.retryable);
    this.detail = o.detail && typeof o.detail === 'object' ? redactDeep(o.detail) : {};
    this.retryAfter = o.retryAfter ?? null;
    this.param = o.param ?? null;
    this.type = o.type ?? null;
  }

  /** Ce qui sort d'un JSON.stringify : jamais de clé, jamais la cause brute. */
  toJSON() {
    return {
      name: this.name,
      code: this.code,
      status: this.status,
      message: this.message,
      hint: this.hint,
      retryable: this.retryable,
      detail: this.detail,
      retryAfter: this.retryAfter,
      param: this.param,
      type: this.type,
    };
  }
}

// --- les sous-classes, par famille ---------------------------------------------------------

/** AIODOS n'est pas installé sur ce PC. */
export class NotInstalled extends AiodosError {}
/** AIODOS est installé mais ne tourne pas. */
export class NotRunning extends AiodosError {}
/** Connexion refusée ou coupée (le message contient « Connexion refusée »). */
export class Unreachable extends AiodosError {}
/** AIODOS tourne, sa passerelle est fermée. */
export class GatewayClosed extends AiodosError {}
/** Le port répond, mais pas comme AIODOS. */
export class NotAiodos extends AiodosError {}
/** Ancienne version d'AIODOS. */
export class TooOld extends AiodosError {}
/** Preuve d'identité fausse : la clé n'a pas été envoyée. */
export class Untrusted extends AiodosError {}
/** Adresse non locale, ou requête qui passerait par un proxy. */
export class NonLocalUrl extends AiodosError {}
/** Délai d'inactivité dépassé. */
export class Timeout extends AiodosError {}
/** Arrêté par l'appli (message « Arrêté »). */
export class Cancelled extends AiodosError {}
/** Réponse incomprise. */
export class ProtocolError extends AiodosError {}

/** L'appli n'a pas (encore) de clé valide. */
export class NeedsAccess extends AiodosError {}
/** L'utilisateur a refusé l'accès. */
export class AccessDenied extends AiodosError {}
/** La demande d'accès a expiré (ou est inconnue). */
export class AccessExpired extends AiodosError {}
/** L'accès a été retiré, ou a expiré, dans AIODOS. */
export class Revoked extends AiodosError {}
/** Droit manquant (detail.scope). */
export class ScopeDenied extends AiodosError {
  get scope() {
    return this.detail.scope ?? null;
  }
}
/** Lieu de calcul plus large que l'accord. */
export class WhereDenied extends AiodosError {}
/** AIODOS n'est ouvert qu'aux autres outils. */
export class AppsClosed extends AiodosError {}
/** AIODOS n'accepte pas de nouvelle demande d'accès. */
export class RequestsClosed extends AiodosError {}
/** Programme bloqué par l'utilisateur. */
export class BlockedProgram extends AiodosError {}
/** La demande vient d'un autre compte Windows. */
export class OtherAccount extends AiodosError {}
/** Clé liée à un autre programme. */
export class ProgramMismatch extends AiodosError {}
/** Page web ou en-têtes d'un navigateur. */
export class OriginRefused extends AiodosError {}
/** En-tête Host inattendu. */
export class HostRefused extends AiodosError {}

/** Modèle inconnu (ou hors du lieu permis). */
export class ModelNotFound extends AiodosError {}
/** Aucune machine permise n'a le modèle en ce moment. */
export class NoMachine extends AiodosError {}
/** Fonction absente de cette version d'AIODOS (ou de ce client). */
export class NotSupported extends AiodosError {}

/** La demande dépasse le contexte (needed, limit quand ils sont connus). */
export class ContextTooLong extends AiodosError {
  get needed() {
    return this.detail.needed ?? null;
  }
  get limit() {
    return this.detail.limit ?? null;
  }
}
/** Outils demandés, aucune machine permise ne les reçoit. */
export class ToolsUnavailable extends AiodosError {}
/** Images envoyées à un modèle qui ne les voit pas. */
export class VisionUnsupported extends AiodosError {}
/** La demande ne se calcule que sur ce PC, qui n'a pas le modèle. */
export class LocalOnly extends AiodosError {}
/** Paramètre standard qu'AIODOS ne traite pas (param). */
export class UnsupportedParameter extends AiodosError {}
/** Corps ou champ invalide (param). */
export class InvalidRequest extends AiodosError {}
/** Corps trop gros. */
export class FileTooLarge extends AiodosError {}
/** Corps non JSON. */
export class UnsupportedMediaType extends AiodosError {}

/** Trop de demandes (retryAfter). */
export class RateLimited extends AiodosError {}
/** AIODOS occupé (retryAfter). */
export class Busy extends AiodosError {}
/** Échec de la machine appairée choisie. */
export class PeerFailed extends AiodosError {}
/** Échec du moteur. */
export class EngineFailed extends AiodosError {}
/** Moteur en panne ou en démarrage (retryAfter). */
export class EngineUnavailable extends AiodosError {}
/** AIODOS tourne sans fenêtre : personne ne peut accorder l'accès. */
export class ApprovalUnavailable extends AiodosError {}

/** Code stable → sous-classe. Un code inconnu donne une AiodosError. */
const CLASSES = {
  not_installed: NotInstalled,
  not_running: NotRunning,
  unreachable: Unreachable,
  gateway_closed: GatewayClosed,
  not_aiodos: NotAiodos,
  too_old: TooOld,
  untrusted: Untrusted,
  non_local_url: NonLocalUrl,
  timeout: Timeout,
  cancelled: Cancelled,
  protocol: ProtocolError,
  needs_access: NeedsAccess,
  invalid_api_key: NeedsAccess,
  access_denied: AccessDenied,
  expired_token: AccessExpired,
  invalid_grant: AccessExpired,
  key_revoked: Revoked,
  key_expired: Revoked,
  scope_denied: ScopeDenied,
  where_denied: WhereDenied,
  apps_closed: AppsClosed,
  requests_closed: RequestsClosed,
  blocked_program: BlockedProgram,
  other_account: OtherAccount,
  program_mismatch: ProgramMismatch,
  origin_refused: OriginRefused,
  host_refused: HostRefused,
  model_not_found: ModelNotFound,
  no_machine: NoMachine,
  not_supported: NotSupported,
  context_length_exceeded: ContextTooLong,
  tools_unavailable: ToolsUnavailable,
  vision_unsupported: VisionUnsupported,
  local_only: LocalOnly,
  unsupported_parameter: UnsupportedParameter,
  invalid_request: InvalidRequest,
  file_too_large: FileTooLarge,
  unsupported_media_type: UnsupportedMediaType,
  rate_limited: RateLimited,
  busy: Busy,
  peer_failed: PeerFailed,
  engine_failed: EngineFailed,
  engine_unavailable: EngineUnavailable,
  approval_unavailable: ApprovalUnavailable,
};

const RETRYABLE = new Set([
  'unreachable',
  'timeout',
  'rate_limited',
  'busy',
  'peer_failed',
  'engine_failed',
  'engine_unavailable',
  'no_machine',
]);

/** La sous-classe d'un code (AiodosError s'il est inconnu). */
export function errorClassFor(code) {
  return CLASSES[code] ?? AiodosError;
}

/** Une erreur du bon type pour ce code. */
export function makeError(code, message, o = {}) {
  const Cls = errorClassFor(code);
  return new Cls(code, message, { retryable: RETRYABLE.has(code), ...o });
}

/**
 * Une erreur propre au client, avec son message et son conseil dans la langue voulue.
 * `textKey` choisit un autre texte que celui du code (par ex. « proxy » pour non_local_url).
 */
export function clientError(code, lang, { vars = {}, textKey = code, ...o } = {}) {
  const message = codeText(textKey, lang, vars) ?? code;
  const hint = o.hint !== undefined ? o.hint : hintText(textKey, lang, vars);
  return makeError(code, message, { ...o, hint });
}

/** Le délai de Retry-After, en secondes (nombre de secondes, ou date HTTP). */
export function parseRetryAfter(value) {
  if (value == null || value === '') return null;
  const s = String(value).trim();
  if (/^\d+(\.\d+)?$/.test(s)) return Number(s);
  const at = Date.parse(s);
  if (Number.isNaN(at)) return null;
  return Math.max(0, Math.round((at - Date.now()) / 1000));
}

const OAUTH_ERRORS = new Set([
  'authorization_pending',
  'slow_down',
  'access_denied',
  'expired_token',
  'invalid_grant',
  'invalid_request',
  'invalid_client',
  'unsupported_grant_type',
]);

/** Le code à défaut, d'après le statut HTTP seul. */
function codeForStatus(status) {
  if (status === 400) return 'invalid_request';
  if (status === 401) return 'invalid_api_key';
  if (status === 404) return 'not_supported';
  if (status === 413) return 'file_too_large';
  if (status === 415) return 'unsupported_media_type';
  if (status === 429) return 'rate_limited';
  if (status === 502) return 'engine_failed';
  if (status === 503) return 'engine_unavailable';
  return 'protocol';
}

/**
 * Lit une réponse d'erreur, quelle qu'en soit la forme.
 * @param {object} r
 * @param {number} r.status
 * @param {any} r.json le corps lu en JSON (ou null)
 * @param {{ get(name: string): string | null }} [r.headers]
 * @param {string} [r.lang]
 */
export function errorFromResponse({ status, json, headers, lang = 'fr' }) {
  let code = null;
  let message = null;
  let hint = null;
  let retryable = null;
  let detail = {};
  let param = null;
  let type = null;
  const e = json && typeof json === 'object' ? json.error : undefined;
  if (e && typeof e === 'object') {
    // enveloppe d'OpenAI, plus l'objet aiodos
    code = typeof e.code === 'string' ? e.code : null;
    message = typeof e.message === 'string' ? e.message : null;
    param = e.param ?? null;
    type = typeof e.type === 'string' ? e.type : null;
    const a = e.aiodos;
    if (a && typeof a === 'object') {
      hint = typeof a.hint === 'string' ? a.hint : null;
      if (typeof a.retryable === 'boolean') retryable = a.retryable;
      if (a.detail && typeof a.detail === 'object') detail = a.detail;
    }
  } else if (typeof e === 'string') {
    if (typeof json.error_description === 'string' || (OAUTH_ERRORS.has(e) && !json.code)) {
      // forme d'OAuth (relève de la clé)
      code = e;
      message = typeof json.error_description === 'string' ? json.error_description : null;
    } else {
      // forme d'Ollama : {"error": "…", "code": "…", "hint": "…"}
      code = typeof json.code === 'string' ? json.code : null;
      message = e;
      hint = typeof json.hint === 'string' ? json.hint : null;
    }
  }
  const derived = !code;
  if (derived) code = codeForStatus(status);
  if (!message) {
    // un statut inattendu, sans corps lisible : le message le dit avec son numéro
    message = derived && code === 'protocol' ? codeText('http', lang, { status }) : codeText(code, lang, { status }) ?? codeText('http', lang, { status });
  }
  const retryAfter = parseRetryAfter(headers?.get?.('retry-after'));
  const o = { status, hint, detail, param, type };
  if (retryAfter != null) o.retryAfter = retryAfter;
  if (retryable != null) o.retryable = retryable;
  return makeError(code, message, o);
}

/** Vrai pour une AiodosError (même venue d'une autre copie du module). */
export function isAiodosError(err) {
  return err instanceof AiodosError || Boolean(err && typeof err === 'object' && typeof err.code === 'string' && 'retryable' in err && 'hint' in err);
}
