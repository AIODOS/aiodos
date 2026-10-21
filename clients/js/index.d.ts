// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — les types de toute la surface (Node 20.3 et plus : les types de
// fetch, Response et AbortSignal viennent de @types/node ou de la bibliothèque « dom »).

/** Version de la bibliothèque. */
export declare const VERSION: '0.1.0';
/** Version de l'API des applications d'AIODOS que ce client parle. */
export declare const API_VERSION: 1;

export type Lang = 'fr' | 'en';
/** Les cercles de calcul. */
export type Circle = 'here' | 'mine' | 'friends' | 'public';
/** Où une réponse a été calculée. */
export type Place = 'here' | 'mine' | 'friend' | 'public';
export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

/** Le coffre de l'appli : la clé est lue à chaque appel et rangée après l'accord. */
export interface KeyStore {
  get(): string | null | undefined | Promise<string | null | undefined>;
  set(key: string | null): void | Promise<void>;
}

export interface ClientOptions {
  /** Nom montré dans AIODOS (1 à 40 caractères). */
  app: string;
  /** Identifiant stable de l'appli ([a-z0-9.-], 2 à 40). */
  appId: string;
  /** Version de l'appli (40 caractères au plus). */
  version?: string;
  /** Le coffre de l'appli ; sans lui, un coffre en mémoire (essais, scripts). */
  key?: KeyStore;
  /** « fr » (défaut) ou « en », ou une fonction relue à chaque appel. */
  lang?: string | (() => string);
  /** Adresse de la passerelle (127.0.0.1, localhost ou [::1] seulement). */
  url?: string | (() => string | Promise<string>);
  /** Un fetch à la place de globalThis.fetch (essais). */
  fetch?: typeof globalThis.fetch;
  /** Délai d'inactivité, en ms (240 000 par défaut). */
  timeout?: number;
  /** Délai de chargement d'un modèle avant le premier morceau, en ms (900 000 par défaut). */
  loadTimeout?: number;
  /** Journal facultatif ; jamais de clé dedans. */
  log?: (level: LogLevel, message: string, data?: unknown) => void;
  /** Variables d'environnement (essais) ; process.env par défaut. */
  env?: Record<string, string | undefined>;
  /** Plateforme (essais) ; process.platform par défaut. */
  platform?: string;
}

export type State =
  | 'not_installed'
  | 'not_running'
  | 'gateway_closed'
  | 'gateway_error'
  | 'apps_closed'
  | 'not_aiodos'
  | 'too_old'
  | 'untrusted'
  | 'headless'
  | 'requests_closed'
  | 'needs_access'
  | 'pending'
  | 'denied'
  | 'revoked'
  | 'other_account'
  | 'ready';

export interface AppInfo {
  id: string | null;
  name: string | null;
  kind: string | null;
  scopes: string[];
  where: string[];
  priority: string | null;
  expires: number | null;
}

/** Les limites de l'appli (null quand AIODOS ne les connaît pas, par exemple streams pour la clé générale). */
export interface Limits {
  requestsPerMinute?: number | null;
  streams?: number | null;
  maxBodyBytes?: number | null;
  [name: string]: unknown;
}

export interface Status {
  state: State;
  /** Le message conseillé, dans la langue du client. */
  message: string;
  /** Le code sous-jacent (unreachable, key_revoked…), ou null. */
  code: string | null;
  url: string | null;
  installed: boolean | null;
  running: boolean | null;
  version: string | null;
  api: number | null;
  instance: string | null;
  features: string[];
  access: { apps: boolean | null; requests: boolean | null; approvalUi: boolean | null } | null;
  gateway: {
    state: 'open' | 'closed' | 'error' | null;
    url: string | null;
    apps: boolean | null;
    requests: boolean | null;
    approvalUi: boolean | null;
    error: string | null;
  } | null;
  app: AppInfo | null;
  limits: Limits | null;
  scopes: string[];
  where: string[];
  /** Le code à afficher en grand pendant une demande (state « pending »). */
  userCode: string | null;
  requestId: string | null;
  expiresAt: number | null;
}

/** GET /aiodos/v1/info, tel quel. */
export interface Info {
  object: 'aiodos.info';
  product: 'aiodos';
  version: string;
  api: number;
  instance: string;
  features: string[];
  access: { apps: boolean; requests: boolean; approval_ui: boolean };
  auth: 'none' | 'valid' | 'revoked' | 'expired' | 'invalid';
  [field: string]: unknown;
}

/** Les droits accordés. */
export interface Grant {
  /** Vrai pour des droits en plus accordés à la même clé (aucune clé neuve). */
  upgraded: boolean;
  scopes: string[];
  where: string[];
  appId: string | null;
  priority: string | null;
  expires: number | null;
}

export interface AccessRequestOptions {
  /** Les droits : chat, embed, libraries. */
  scopes: string[] | string;
  /** Les cercles demandés (absent : les quatre) ; l'utilisateur peut en décocher. */
  where?: Circle[] | string;
  /** Pourquoi l'appli demande l'accès (200 caractères au plus). */
  purpose?: string;
  signal?: AbortSignal;
}

export interface ConnectOptions extends AccessRequestOptions {
  /** Reçoit le code à afficher en grand (« 482 197 »). */
  onCode?: (userCode: string, info: { requestId: string | null; expiresIn: number; message: string | null; url: string }) => void | Promise<void>;
  /** Délai total de l'attente, en ms (par défaut : expires_in). */
  timeout?: number;
}

/** Une demande d'accès en cours ; le device_code n'est jamais exposé. */
export interface AccessRequest {
  readonly userCode: string;
  readonly requestId: string | null;
  readonly expiresIn: number;
  readonly interval: number;
  readonly expiresAt: number;
  readonly message: string | null;
  readonly url: string;
  readonly upgrade: boolean;
  /** Relève la clé (RFC 8628) et la range par key.set(). */
  wait(options?: { signal?: AbortSignal; timeout?: number }): Promise<Grant>;
}

export interface ModelLocation {
  id: string;
  machine: string | null;
  place: string | null;
  loaded: boolean;
  context: number | null;
  tools: boolean | null;
  vision: boolean | null;
}

/** Un modèle, au format qu'ARKLIGHT attend déjà ({ id, size, caps, params }), et plus. */
export interface Model {
  id: string;
  label: string;
  kind: 'chat' | 'embed' | string | null;
  /** Taille en octets (0 si inconnue). */
  size: number;
  /** completion, tools, vision, thinking ; ou embedding. */
  caps: string[];
  /** « 9B » d'après le nom, sinon ''. */
  params: string;
  machine: string | null;
  context: number | null;
  loaded: boolean;
  here: boolean;
  /** Le modèle qu'AIODOS prend lui-même (ses conversations, ses bibliothèques). */
  default: boolean;
  installed: boolean;
  tools: boolean;
  vision: boolean;
  thinking: boolean;
  dimensions: number | null;
  license: { id: string | null; restrictive: boolean } | null;
  where: ModelLocation[];
}

export interface NeutralToolCall {
  id: string;
  name: string;
  /** Les arguments analysés ; null s'ils sont illisibles (le texte est alors dans raw). */
  args: Record<string, unknown> | null;
  raw?: string;
}

/** Le format neutre des messages (celui d'ARKLIGHT). */
export type NeutralMessage =
  | { role: 'system'; content: string }
  | { role: 'user'; content: string | unknown[]; images?: Array<string | Uint8Array> }
  | { role: 'assistant'; content: string | null; toolCalls?: NeutralToolCall[] }
  | { role: 'tool'; toolCallId: string; name?: string; content: unknown };

export interface NeutralTool {
  name: string;
  description?: string;
  /** Un JSON Schema de type « object ». */
  parameters?: Record<string, unknown>;
}

export interface ChatOptions {
  model: string;
  system?: string;
  messages: Array<NeutralMessage | Record<string, unknown>>;
  tools?: Array<NeutralTool | Record<string, unknown>>;
  /** false : reasoning_effort « none » (conseillé) ; une chaîne : reasoning_effort tel quel. */
  think?: boolean | string;
  temperature?: number;
  maxTokens?: number;
  topP?: number;
  stop?: string | string[];
  seed?: number;
  /** Resserre le lieu de calcul (X-Aiodos-Where). */
  where?: Circle | Circle[] | string;
  /** Baisse la priorité (X-Aiodos-Priority: background). */
  batch?: boolean;
  /** false : une réponse sans outils plutôt qu'une erreur tools_unavailable. */
  requireTools?: boolean;
  signal?: AbortSignal;
  onText?: (delta: string) => void;
  onThinking?: (delta: string) => void;
  /** Le modèle se charge (seconds : depuis combien de temps, 0 si on le sait d'avance). */
  onLoading?: (info: { seconds: number }) => void;
  timeout?: number;
  loadTimeout?: number;
}

export interface ChatResult {
  text: string;
  thinking: string;
  toolCalls: NeutralToolCall[];
  /** finish_reason : stop, length, tool_calls… */
  finish: string | null;
  /** stop, max_tokens, context ou tool_calls. */
  stoppedBy: 'stop' | 'max_tokens' | 'context' | 'tool_calls' | string | null;
  truncated: boolean;
  usage: { promptTokens: number | null; completionTokens: number | null; totalTokens: number | null } | null;
  /** L'identifiant concret qui a répondu. */
  answeredBy: string | null;
  machine: string | null;
  place: Place | string | null;
  contextLength: number | null;
}

export interface EmbedOptions {
  input: string | string[];
  /** Sans modèle : celui des bibliothèques d'AIODOS (`default`), sinon le premier de ce PC. */
  model?: string;
  signal?: AbortSignal;
  timeout?: number;
}

export interface EmbedResult {
  embeddings: number[][];
  /** Le modèle qui a calculé les vecteurs : à garder avec eux (deux modèles ne se comparent pas). */
  model: string;
  dimensions: number | null;
  usage: { promptTokens: number | null; totalTokens: number | null } | null;
}

/** ComfyUI (livraison L2 d'AIODOS) : rejette NotSupported tant que features ne l'annonce pas. */
export interface Comfy {
  plan(options?: Record<string, unknown> & { signal?: AbortSignal }): Promise<never>;
  objectInfo(cls?: string | null, options?: { signal?: AbortSignal }): Promise<never>;
  template(name: string, options?: { signal?: AbortSignal }): Promise<never>;
  recipes(options?: { signal?: AbortSignal }): Promise<never>;
  recipeFor(template: unknown, options?: { signal?: AbortSignal }): Promise<never>;
  tokenize(graph: unknown, inputs: unknown, options?: { signal?: AbortSignal }): Promise<never>;
  submit(options?: Record<string, unknown> & { signal?: AbortSignal }): Promise<never>;
  run(options?: Record<string, unknown> & { signal?: AbortSignal }): Promise<never>;
}

export interface AiodosClient {
  /** L'état d'AIODOS pour cette appli ; ne rejette jamais. */
  status(options?: { signal?: AbortSignal }): Promise<Status>;
  /** Attend qu'AIODOS réponde (until: 'running') ou soit prêt (until: 'ready'). */
  waitReady(options?: { timeout?: number; interval?: number; until?: 'running' | 'ready'; signal?: AbortSignal }): Promise<Status>;
  /** Lance AIODOS en arrière-plan, sur un clic de l'utilisateur. */
  launch(): Promise<{ launched: boolean; alreadyRunning: boolean; pid?: number; kind?: 'installed' | 'dev' }>;
  /** Se connecter (demande, code, relève, clé rangée), sur un clic de l'utilisateur. */
  connect(options: ConnectOptions): Promise<Grant>;
  /** La demande par la passerelle ouverte (en dessous de connect). */
  requestAccess(options: AccessRequestOptions): Promise<AccessRequest>;
  /** Se déconnecter : retrait dans AIODOS, puis key.set(null). */
  disconnect(options?: { signal?: AbortSignal }): Promise<{ revoked: boolean }>;
  /** GET /aiodos/v1/info sans clé. */
  info(options?: { signal?: AbortSignal }): Promise<Info>;
  /** Les modèles permis à l'appli. */
  models(options?: { kind?: 'chat' | 'embed'; use?: 'chat' | 'embed'; signal?: AbortSignal; timeout?: number }): Promise<Model[]>;
  /** Une conversation, toujours en flux. */
  chat(options: ChatOptions): Promise<ChatResult>;
  /** Des embeddings, sur ce PC. */
  embed(options: EmbedOptions): Promise<EmbedResult>;
  /** Ramène la fenêtre d'AIODOS sur une page. */
  openInAiodos(options?: { page?: 'applications' | 'models' | 'jobs' | 'gallery'; model?: string | null; signal?: AbortSignal }): Promise<{ opened: boolean }>;
  /** Les moteurs (L2). */
  engines(options?: { signal?: AbortSignal }): Promise<never>;
  /** ComfyUI (L2). */
  readonly comfy: Comfy;
  /** Ses travaux (L2). */
  jobs(options?: { active?: boolean | null; status?: string; clientRef?: string; signal?: AbortSignal }): Promise<never>;
  /** Un travail (L2). */
  job(id: string, options?: { signal?: AbortSignal }): Promise<never>;
  /** Les entrées des travaux (L2). */
  readonly inputs: { put(data: Uint8Array, options?: { type?: string; signal?: AbortSignal }): Promise<never> };
  toJSON(): { client: 'aiodos-client'; version: string; app: string | null };
  toString(): string;
}

/** Le client d'une appli, sans état global. */
export declare function createClient(options: ClientOptions): AiodosClient;
/** Un coffre en mémoire (essais, scripts). */
export declare function memoryKeyStore(initial?: string | null): KeyStore;
/** Le meilleur modèle pour un usage, ou null. */
export declare function pickModel<M extends { id: string; caps?: string[]; size?: number; here?: boolean; default?: boolean; loaded?: boolean; installed?: boolean; context?: number | null }>(
  list: M[] | null | undefined,
  options?: { needs?: string[] | string; vision?: boolean; preferLocal?: boolean; minContext?: number },
): M | null;

// --- la conversion des messages ---------------------------------------------------------
export declare function toOpenAIMessages(input: { system?: string | null; messages?: Array<NeutralMessage | Record<string, unknown>> }): Array<Record<string, unknown>>;
export declare function toOpenAIMessage(message: NeutralMessage | Record<string, unknown>): Record<string, unknown>;
export declare function fromOpenAIMessages(messages: Array<Record<string, unknown>>): { system: string | null; messages: NeutralMessage[] };
export declare function fromOpenAIMessage(message: Record<string, unknown>): NeutralMessage;
export declare function toOpenAITools(tools: Array<NeutralTool | Record<string, unknown>>): Array<{ type: 'function'; function: { name: string; description?: string; parameters: Record<string, unknown> } }>;
export declare function fromOpenAITools(tools: Array<Record<string, unknown>>): NeutralTool[];
export declare function parseToolArguments(value: unknown): { args: Record<string, unknown> | null; raw: string };
export declare function imageDataUrl(image: string | Uint8Array): string;

// --- les textes ---------------------------------------------------------------------------
export declare const texts: {
  readonly states: Readonly<Record<Lang, Readonly<Record<State, string>>>>;
  readonly codes: Readonly<Record<Lang, Readonly<Record<string, string>>>>;
  readonly hints: Readonly<Record<Lang, Readonly<Record<string, string>>>>;
  state(state: State, lang?: string, vars?: Record<string, unknown>): string;
  code(code: string, lang?: string, vars?: Record<string, unknown>): string | null;
  hint(code: string, lang?: string, vars?: Record<string, unknown>): string | null;
  lang(lang: unknown): Lang;
};

// --- les erreurs --------------------------------------------------------------------------
export declare class AiodosError extends Error {
  constructor(code: string, message: string, options?: { status?: number | null; hint?: string | null; retryable?: boolean; detail?: Record<string, unknown>; retryAfter?: number | null; param?: string | null; type?: string | null; cause?: unknown });
  /** Code stable (docs/api-applications.md § 3). */
  readonly code: string;
  readonly status: number | null;
  readonly hint: string | null;
  readonly retryable: boolean;
  readonly detail: Record<string, unknown>;
  /** Secondes de Retry-After (429, 503). */
  readonly retryAfter: number | null;
  readonly param: string | null;
  readonly type: string | null;
  toJSON(): { name: string; code: string; status: number | null; message: string; hint: string | null; retryable: boolean; detail: Record<string, unknown>; retryAfter: number | null; param: string | null; type: string | null };
}
export declare function isAiodosError(err: unknown): err is AiodosError;
export declare function errorClassFor(code: string): typeof AiodosError;
export declare class NotInstalled extends AiodosError {}
export declare class NotRunning extends AiodosError {}
/** Connexion refusée ou coupée : le message contient « Connexion refusée ». */
export declare class Unreachable extends AiodosError {}
export declare class GatewayClosed extends AiodosError {}
export declare class NotAiodos extends AiodosError {}
export declare class TooOld extends AiodosError {}
export declare class Untrusted extends AiodosError {}
export declare class NonLocalUrl extends AiodosError {}
export declare class Timeout extends AiodosError {}
/** Arrêté par l'appli : le message est « Arrêté ». */
export declare class Cancelled extends AiodosError {}
export declare class ProtocolError extends AiodosError {}
export declare class NeedsAccess extends AiodosError {}
export declare class AccessDenied extends AiodosError {}
export declare class AccessExpired extends AiodosError {}
export declare class Revoked extends AiodosError {}
export declare class ScopeDenied extends AiodosError {
  readonly scope: string | null;
}
export declare class WhereDenied extends AiodosError {}
export declare class AppsClosed extends AiodosError {}
export declare class RequestsClosed extends AiodosError {}
export declare class BlockedProgram extends AiodosError {}
export declare class OtherAccount extends AiodosError {}
export declare class ProgramMismatch extends AiodosError {}
export declare class OriginRefused extends AiodosError {}
export declare class HostRefused extends AiodosError {}
export declare class ModelNotFound extends AiodosError {}
export declare class NoMachine extends AiodosError {}
export declare class NotSupported extends AiodosError {}
export declare class ContextTooLong extends AiodosError {
  readonly needed: number | null;
  readonly limit: number | null;
}
export declare class ToolsUnavailable extends AiodosError {}
export declare class VisionUnsupported extends AiodosError {}
export declare class LocalOnly extends AiodosError {}
export declare class UnsupportedParameter extends AiodosError {}
export declare class InvalidRequest extends AiodosError {}
export declare class FileTooLarge extends AiodosError {}
export declare class UnsupportedMediaType extends AiodosError {}
export declare class RateLimited extends AiodosError {}
export declare class Busy extends AiodosError {}
export declare class PeerFailed extends AiodosError {}
export declare class EngineFailed extends AiodosError {}
export declare class EngineUnavailable extends AiodosError {}
export declare class ApprovalUnavailable extends AiodosError {}
