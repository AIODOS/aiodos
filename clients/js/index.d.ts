// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — les types de toute la surface (Node 20.3 et plus : les types de
// fetch, Response et AbortSignal viennent de @types/node ou de la bibliothèque « dom »).

/** Version de la bibliothèque. */
export declare const VERSION: '0.2.0';
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

// --- les travaux ComfyUI (docs/api-applications.md § 5.7) ---------------------------------
// Chaque fonction rejette NotSupported tant que `features` n'annonce pas « engines.comfyui »
// (et « jobs », « jobs.pause », « inputs », « files » selon ce qu'elle touche).

/** Des octets, ou le chemin d'un fichier (texte, ou URL file:). */
export type InputData = Uint8Array | ArrayBuffer | ArrayBufferView | string | URL;
/** Une entrée de travail : des octets, un chemin, avec son genre, ou une entrée déjà envoyée ({ sha256 }). */
export type InputValue =
  | InputData
  | { data: Uint8Array | ArrayBuffer | ArrayBufferView; type?: string }
  | { path: string | URL; type?: string }
  | { sha256: string; type?: string };
/** Pour tokenize : la même chose, et l'entrée de fichier d'un nœud de chargement que le client ne connaît pas. */
export type TokenizeValue =
  | InputData
  | { data: Uint8Array | ArrayBuffer | ArrayBufferView; type?: string; input?: string }
  | { path: string | URL; type?: string; input?: string }
  | { sha256: string; type?: string; input?: string };

/** Un nœud d'un graphe au format API. */
export interface ComfyNode {
  class_type: string;
  inputs?: Record<string, unknown>;
  _meta?: Record<string, unknown>;
}
/** Un graphe ComfyUI au format API (« Exporter (API) ») : { "<nœud>": { class_type, inputs } }. */
export type ComfyGraph = Record<string, ComfyNode>;

/** Une unité : un graphe (indice dans graphs), sa graine (@seed), ses retouches. */
export interface JobItem {
  graph?: number;
  label?: string;
  seed?: number;
  /** [nœud, entrée, valeur] : remplace une valeur déjà présente, du même type, jamais un lien. */
  set?: Array<[node: string, input: string, value: unknown]>;
}
/** À la place d'items : count variations, valeur + i × step. */
export interface JobRepeat {
  count: number;
  vary?: Array<{ node: string; input: string; step: number }>;
  graph?: number;
}
/** Bornes : durée d'une unité (s), poids des sorties (Mo), attente d'une machine (s). */
export interface JobBounds {
  unitSeconds?: number;
  outputMb?: number;
  waitS?: number;
}

export interface ComfyJobOptions {
  /** Le graphe (format API), ou graphs : 64 au plus. */
  graph?: ComfyGraph;
  graphs?: ComfyGraph[];
  /** Une unité par élément (256 au plus) ; sans items ni repeat, une unité par graphe. */
  items?: JobItem[];
  repeat?: JobRepeat;
  /** Les entrées citées par les jetons @in:<id> : hachées, envoyées si AIODOS ne les a pas. */
  inputs?: Record<string, InputValue>;
  /** « auto » (défaut : AIODOS relève les noms de modèles), ou les jetons @model:<clé>. */
  models?: 'auto' | Record<string, { folder: string; name: string; sha256?: string }>;
  /** Resserre le lieu de calcul (X-Aiodos-Where), dans l'accord de l'appli. */
  where?: Circle | Circle[] | string;
  priority?: 'high' | 'normal' | 'low';
  label?: string;
  /** La référence de l'appli, aussi clé d'idempotence : le même clientRef rend le même travail. */
  clientRef?: string;
  bounds?: JobBounds;
  strict?: boolean;
  /** Vrai : le lieu resserré à ce PC et à mes machines (here, mine), dans l'accord. */
  sensitive?: boolean;
  /** Une recette (L4) : pas encore servie, NotSupported. */
  recipe?: unknown;
  signal?: AbortSignal;
}

export interface ComfyRunOptions extends ComfyJobOptions {
  /** Le dossier des fichiers (créé au besoin). */
  destDir: string | URL;
  /** Le début des noms (<stem>-<unité>.<ext>) ; l'identifiant du travail par défaut. */
  stem?: string;
  onFile?: (path: string, file: JobFile) => void | Promise<void>;
  /** Chaque événement du suivi. */
  onEvent?: (event: JobEvent) => void | Promise<void>;
  /** Délai total de l'attente, en ms. */
  timeout?: number;
}

/** Le pré-vol (comfy.plan) : rien n'est mis en file. */
export interface Plan {
  ok: boolean;
  whereEffective: Circle[];
  whereReason: string | null;
  machines: { here: boolean; mine: number | null; friends: number | null; public: number | null };
  missing: Array<{ folder: string | null; name: string; machine: string; size?: number }>;
  refused: Record<string, Array<{ class: string; reason: string }>>;
  licenses: Array<{ sha256: string; id: string; restrictive: boolean }>;
  units: number;
  estimate: { secondsPerUnit: number; machines: number } | null;
  [field: string]: unknown;
}

/** Un moteur (engines()), selon les cercles de l'appli. */
export interface Engine {
  id: 'comfyui' | string;
  label: string;
  here: { enabled: boolean; version: string | null; vramMib: number | null } | null;
  /** Combien de machines l'ont : ce PC (0 ou 1), mes machines (null : cercle non permis). */
  machines: { here: number; mine: number | null; friends: number | null; public: number | null };
  models: Array<{
    folder: string | null;
    name: string;
    sha256: string | null;
    size: number | null;
    /** here : sur ce PC ; mine : les noms de mes machines qui l'ont. */
    where: { here: boolean; mine: string[] | null; friends: number | null; public: number | null };
    license: { id: string | null; restrictive: boolean } | null;
  }>;
  objectInfo: { available: boolean; machine: string | null; digest: string | null };
  recipes: number;
  [field: string]: unknown;
}

export type JobStatus = 'queued' | 'in_progress' | 'pausing' | 'paused' | 'cancelling' | 'completed' | 'failed' | 'cancelled';
export type UnitStage = 'queued' | 'waiting_machine' | 'sending_inputs' | 'waiting_engine' | 'loading' | 'running' | 'receiving' | 'checking' | 'done';
/** Le code d'échec d'une unité (no_machine : aucune machine ne l'a prise avant bounds.waitS). */
export type UnitErrorCode = 'engine_error' | 'engine_timeout' | 'output_invalid' | 'output_too_large' | 'no_machine' | 'lost' | 'no_progress' | 'cancelled' | string;

export interface JobProgress {
  total: number;
  done: number;
  running: number;
  queued: number;
  failed: number;
  percent: number;
  /** Toujours vrai en v1 : une estimation. */
  estimated: boolean;
  elapsedS: number;
  etaS: number | null;
  stage: UnitStage | string;
  machines: Array<{ name: string; place: Place | string; running: number; done: number; medianS?: number }>;
  /** « 1 sur 2 · 1 en cours sur 1 machine ». */
  message: string;
}

export interface JobUnit {
  index: number;
  label: string | null;
  status: 'queued' | 'in_progress' | 'completed' | 'failed' | 'cancelled' | string;
  place: Place | string | null;
  machine: string | null;
  /** Les identifiants de ses fichiers. */
  files: string[];
  seconds: number | null;
  stage?: UnitStage | string | null;
  error?: { code: UnitErrorCode; message: string };
}

/** L'objet travail en entier (job.state), en camelCase. */
export interface JobState {
  id: string;
  object: 'aiodos.job';
  engine: string;
  label: string | null;
  clientRef: string | null;
  status: JobStatus;
  /** Pendant une pause : { when, pending } (unités encore en calcul). */
  pause: { when: 'after_current' | 'now'; pending: number } | null;
  priority: 'high' | 'normal' | 'low';
  where: Circle[] | null;
  whereEffective: Circle[];
  whereReason: string | null;
  progress: JobProgress;
  waiting: Record<string, unknown> | null;
  units: JobUnit[];
  files: JobFile[];
  error: { code: string; message: string } | null;
  /** « app » : annulé par cette appli. */
  cancelledBy: string | null;
  createdAt: number;
  startedAt: number | null;
  completedAt: number | null;
  [field: string]: unknown;
}

/** Un événement de job.watch(). */
export type JobEvent =
  | { type: 'job'; job: Job }
  | { type: 'file'; file: JobFile; job: Job }
  | { type: 'end'; status: 'completed' | 'failed' | 'cancelled' | 'deleted' | string | null; job: Job }
  /** AIODOS ne répond plus : nouvel essai dans retryIn ms (reconnexion automatique). */
  | { type: 'unreachable'; error: AiodosError; attempt: number; retryIn: number; job: Job }
  /** Un événement d'une version suivante d'AIODOS, tel quel. */
  | { type: 'unit' | 'machine' | 'file_doubt'; data: Record<string, unknown>; job: Job };

export interface WatchOptions {
  /** Ferme vraiment la connexion (« Arrêté ») ; le travail continue dans AIODOS. */
  signal?: AbortSignal;
  /** Délai total, en ms (Timeout) ; sans lui, le suivi attend la fin, reconnexions comprises. */
  timeout?: number;
  /** Silence après lequel le flux est rouvert, en ms (45 000 ; AIODOS envoie « : ping » toutes les 15 s). */
  idleTimeout?: number;
}

export interface DownloadOptions {
  /** Le début des noms (<stem>-<unité>[-<n>].<ext>) ; l'identifiant du travail par défaut. */
  stem?: string;
  onFile?: (path: string, file: JobFile) => void | Promise<void>;
  signal?: AbortSignal;
}

/** Un fichier produit pour l'appli. */
export interface JobFile {
  readonly id: string;
  /** L'indice de l'unité qui l'a produit. */
  readonly unit: number | null;
  /** Le nœud de sortie (SaveImage…). */
  readonly node: string | null;
  readonly type: string | null;
  /** La taille, en octets. */
  readonly bytes: number;
  readonly sha256: string | null;
  readonly width: number | null;
  readonly height: number | null;
  /** La machine qui l'a calculé (null pour un inconnu du réseau public). */
  readonly machine: string | null;
  readonly place: Place | string | null;
  /** « comfyui 0.35.1 ». */
  readonly engine: string | null;
  readonly verified: string | null;
  /** Récupéré (servi jusqu'au dernier octet) : AIODOS le garde encore 7 jours. */
  readonly fetched: boolean;
  readonly expiresAt: number | null;
  readonly createdAt: number | null;
  /** Relit la fiche. */
  refresh(options?: { signal?: AbortSignal }): Promise<JobFile>;
  /** Les octets (un Buffer de Node), l'empreinte vérifiée. */
  content(options?: { signal?: AbortSignal }): Promise<Uint8Array>;
  /** L'écrit à ce chemin, repris s'il est partiel (« .part »), vérifié ; rend le chemin absolu. */
  save(path: string | URL, options?: { signal?: AbortSignal }): Promise<string>;
  /** L'efface dans AIODOS. */
  delete(options?: { signal?: AbortSignal }): Promise<{ id: string; deleted: true }>;
  /** Un lien signé : pas encore servi (NotSupported). */
  link(options?: { ttl?: number; uses?: number; signal?: AbortSignal }): Promise<never>;
  toJSON(): Record<string, unknown>;
  toString(): string;
}
/** Le même, sous le nom de la conception. */
export type File = JobFile;

/** Un travail de l'appli : les champs suivent le dernier état reçu (state les donne tous). */
export interface Job {
  readonly id: string;
  readonly engine: string | null;
  readonly label: string | null;
  readonly clientRef: string | null;
  readonly status: JobStatus | null;
  readonly priority: 'high' | 'normal' | 'low' | null;
  readonly where: Circle[] | null;
  readonly whereEffective: Circle[];
  readonly whereReason: string | null;
  readonly progress: JobProgress | null;
  readonly waiting: Record<string, unknown> | null;
  readonly units: JobUnit[];
  readonly error: { code: string; message: string } | null;
  readonly cancelledBy: string | null;
  readonly createdAt: number | null;
  readonly startedAt: number | null;
  readonly completedAt: number | null;
  /** Tout le dernier état, fichiers (state.files) et pause (state.pause) compris. */
  readonly state: JobState;
  /** Fini : completed, failed ou cancelled. */
  readonly done: boolean;
  /** Oublié par delete(). */
  readonly deleted: boolean;
  /** Relit l'état. */
  refresh(options?: { signal?: AbortSignal }): Promise<Job>;
  /** Les événements, en itérateur asynchrone, avec reconnexion automatique. */
  watch(options?: WatchOptions): AsyncGenerator<JobEvent, void, undefined>;
  /** Attend la fin ; rend le travail, quel que soit son état final. */
  wait(options?: WatchOptions): Promise<Job>;
  /** Après l'unité en cours (défaut : rien ne se perd), ou tout de suite (« now »). */
  pause(options?: { when?: 'after_current' | 'now'; signal?: AbortSignal }): Promise<Job>;
  resume(options?: { signal?: AbortSignal }): Promise<Job>;
  /** Le calcul en cours s'arrête dans ComfyUI (ce calcul seulement) ; les fichiers rendus restent. */
  cancel(options?: { signal?: AbortSignal }): Promise<Job>;
  /** Les unités en échec repartent. */
  retryFailed(options?: { signal?: AbortSignal }): Promise<Job>;
  /** Oublie un travail fini ; files: true efface aussi ses fichiers. */
  delete(options?: { files?: boolean | 'keep' | 'delete'; signal?: AbortSignal }): Promise<{ id: string; deleted: boolean }>;
  /** Ses fichiers : ceux pas encore récupérés (défaut), ou tous (unfetched: false). */
  files(options?: { unfetched?: boolean; signal?: AbortSignal }): Promise<JobFile[]>;
  /** Télécharge tous ses fichiers dans destDir ; rend leurs chemins. */
  download(destDir: string | URL, options?: DownloadOptions): Promise<string[]>;
  toJSON(): Record<string, unknown>;
  toString(): string;
}

/** ComfyUI par AIODOS. */
export interface Comfy {
  /** Pré-vol : le corps d'un travail vérifié comme au dépôt (ses entrées envoyées), rien en file. */
  plan(options: ComfyJobOptions): Promise<Plan>;
  /** L'object_info relayé (L2c) : pas encore servi, NotSupported. */
  objectInfo(cls?: string | null, options?: { signal?: AbortSignal }): Promise<never>;
  /** Un gabarit relayé (L2c) : pas encore servi, NotSupported. */
  template(name: string, options?: { signal?: AbortSignal }): Promise<never>;
  /** Les recettes (L4) : pas encore servies, NotSupported. */
  recipes(options?: { signal?: AbortSignal }): Promise<never>;
  recipeFor(template: unknown, options?: { signal?: AbortSignal }): Promise<never>;
  /**
   * Une copie du graphe avec « @in:<id> » dans l'entrée de fichier des nœuds donnés
   * (LoadImage, LoadImageMask : image ; LoadVideo : file ; LoadAudio : audio), et les entrées
   * à donner à submit.
   */
  tokenize<G extends ComfyGraph>(graph: G, files: Record<string, TokenizeValue>, options?: { signal?: AbortSignal }): Promise<{ graph: G; inputs: Record<string, InputValue> }>;
  /** Envoie les entrées absentes, puis le travail. */
  submit(options: ComfyJobOptions): Promise<Job>;
  /** submit, l'attente de la fin, puis download ; arrêté par signal, le travail est annulé. */
  run(options: ComfyRunOptions): Promise<string[]>;
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
  /** Les moteurs : qui a ComfyUI, sa version, ses modèles. */
  engines(options?: { signal?: AbortSignal }): Promise<Engine[]>;
  /** ComfyUI : plan, tokenize, submit, run. */
  readonly comfy: Comfy;
  /**
   * Ses travaux, les plus récents d'abord (après un redémarrage de l'appli comme d'AIODOS).
   * Sans filtre : ceux en cours ; avec clientRef ou status : tous, finis compris ;
   * active: null pour tous, false pour les finis.
   */
  jobs(options?: { active?: boolean | null; status?: JobStatus | JobStatus[] | string; clientRef?: string; limit?: number; signal?: AbortSignal }): Promise<Job[]>;
  /** Un de ses travaux (celui d'une autre appli : JobNotFound). */
  job(id: string, options?: { signal?: AbortSignal }): Promise<Job>;
  /** Un de ses fichiers, par son identifiant. */
  file(id: string, options?: { signal?: AbortSignal }): Promise<JobFile>;
  /** Les entrées des travaux : put envoie des octets (ou un fichier) s'ils manquent ; rend leur sha256. */
  readonly inputs: { put(data: InputData | { data: Uint8Array | ArrayBuffer | ArrayBufferView; type?: string } | { path: string | URL; type?: string }, options?: { type?: string; signal?: AbortSignal }): Promise<string> };
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
/** Moteur pas prêt (503, retryAfter), ou aucune machine permise n'a ComfyUI ouvert et accordé (409). */
export declare class EngineUnavailable extends AiodosError {}
export declare class ApprovalUnavailable extends AiodosError {}
/** graph_not_api (format de l'éditeur), graph_invalid, patch_invalid : `code` dit lequel. */
export declare class GraphInvalid extends AiodosError {
  readonly node: string | null;
  readonly graph: number | null;
}
export declare class GraphTooLarge extends AiodosError {}
/** Chemin, adresse ou annotation de dossier. */
export declare class ForbiddenValue extends AiodosError {
  readonly node: string | null;
  readonly input: string | null;
  readonly reason: string | null;
}
/** Nœud d'un service en ligne. */
export declare class ForbiddenNode extends AiodosError {
  readonly nodeClass: string | null;
}
/** Jeton @in: sans entrée déclarée, ou entrée jamais envoyée par cette appli. */
export declare class InputMissing extends AiodosError {
  readonly input: string | null;
}
export declare class InputHashMismatch extends AiodosError {}
/** Plus de 100 Mo : refusée avant l'envoi. */
export declare class InputTooLarge extends AiodosError {}
export declare class EngineNotFound extends AiodosError {}
export declare class InputNotFound extends AiodosError {}
/** Introuvable, ou à une autre appli. */
export declare class JobNotFound extends AiodosError {}
/** Introuvable, ou à une autre appli. */
export declare class FileNotFound extends AiodosError {}
/** Modèles absents (detail.machine : la machine la plus proche de les avoir tous). */
export declare class ModelUnavailable extends AiodosError {
  readonly models: Array<{ folder: string | null; name: string }>;
}
export declare class ModelUnresolved extends AiodosError {
  readonly model: string | null;
}
export declare class ModelAmbiguous extends AiodosError {
  readonly candidates: string[];
}
export declare class JobNotCancellable extends AiodosError {}
export declare class JobNotPausable extends AiodosError {}
/** Travaux actifs, entrées, fichiers non récupérés (quota). */
export declare class QuotaExceeded extends AiodosError {
  readonly quota: string | null;
}
/** comfy.run : travail fini en échec, ou annulé ailleurs (detail.status, detail.units). */
export declare class JobFailed extends AiodosError {
  readonly job: string | null;
}
