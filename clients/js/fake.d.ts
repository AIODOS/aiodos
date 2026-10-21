// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — les types du faux AIODOS (import … from 'aiodos-client/fake').

import type { AiodosClient, ClientOptions, KeyStore } from './index.js';

/** La clé de l'appli d'essai du contrat (aiodos_app_k3j7m2qa_ suivi de 43 « A »). */
export declare const FAKE_KEY: string;
/** Tout ce que sert le faux, travaux ComfyUI compris : à donner dans `features` pour les annoncer. */
export declare const FAKE_FEATURES: readonly string[];
/** Le petit PNG de l'essai (8 × 8, 74 octets, le même que png() des essais d'AIODOS) : chaque sortie du faux. */
export declare const FAKE_PNG: Uint8Array;

export interface FakeRequest {
  method: string;
  path: string;
  /** En-têtes reçus, en minuscules. */
  headers: Record<string, string>;
  /** Le corps lu en JSON (null pour un corps brut, comme une entrée envoyée). */
  json: unknown;
  /** La taille du corps reçu, en octets. */
  size: number;
  at: number;
}

export interface FakeResponse {
  status: number;
  headers?: Record<string, string>;
  json?: unknown;
  text?: string;
  /** Des octets (le contenu d'un fichier). */
  bytes?: Uint8Array;
  /** Couper la connexion après ces octets (bytes). */
  cutAt?: number;
  /** Les lignes d'un flux SSE (« data: … », « event: … » ou « : commentaire »). */
  sse?: string[];
  /** Un flux en direct : écrit, ferme ou coupe par le puits donné ; rend de quoi s'arrêter. */
  live?: (sink: { send(text: string): void; close(): void; cut(): void }) => (() => void) | void;
  delayMs?: number;
  firstDelayMs?: number;
  /** Se taire après n événements, sans fermer. */
  stallAfter?: number;
  /** Couper la connexion après n événements. */
  cutAfter?: number;
  /** Ne jamais répondre. */
  hang?: boolean;
}

/** Le ComfyUI simulé par le faux (celui de ce PC). */
export interface FakeComfy {
  enabled: boolean;
  version: string;
  vramMib: number;
  machine: string;
  /** Les modèles installés : un travail qui en cite un autre reçoit model_unavailable. */
  models: Array<{ folder: string; name: string }>;
  /** Les classes de nœuds d'un service en ligne (forbidden_node). */
  cloudNodes: string[];
}

/** Un travail du faux, tel qu'il le garde (fake.jobs). */
export interface FakeJob {
  id: string;
  app: string;
  label: string | null;
  clientRef: string | null;
  priority: string;
  where: string[];
  /** queued, running, paused, done, failed ou cancelled (l'état en base, comme AIODOS). */
  status: string;
  pauseMode: 'after_current' | 'now' | null;
  cancelledBy: string | null;
  error: string | null;
  units: Array<{ index: number; graph: number; label: string | null; seed: number | null; status: string; stage: string | null; files: string[]; seconds: number | null; error: { code: string; message: string } | null }>;
  files: string[];
  failUnits: Set<number>;
  filesPerUnit: number;
  [field: string]: unknown;
}

export interface FakeOptions {
  url?: string;
  version?: string;
  api?: number;
  instance?: string;
  features?: string[];
  access?: { apps?: boolean; requests?: boolean; approval_ui?: boolean };
  /** « old » : un ancien AIODOS ; « ollama » : Ollama sur le port. */
  mode?: 'aiodos' | 'old' | 'ollama';
  /** false : connexion refusée. */
  listening?: boolean;
  /** true : une preuve fausse (un imposteur). */
  wrongProof?: boolean;
  /** Intervalle de relève (s). */
  interval?: number;
  /** Durée d'une demande (s). */
  expiresIn?: number;
  chatDelayMs?: number;
  /** La clé connue d'avance (FAKE_KEY par défaut ; null : aucune). */
  key?: string | null;
  /** Ses droits (chat et embed par défaut ; « engines:comfyui » pour les travaux). */
  scopes?: string[];
  where?: string[];
  /** « auto » (défaut : une étape toutes les jobStepMs) ou « manual » (step, finish). */
  jobMode?: 'auto' | 'manual';
  /** L'intervalle des étapes en mode auto, en ms (5). */
  jobStepMs?: number;
  /** L'intervalle des « : ping » des flux d'événements, en ms (15 000). */
  pingMs?: number;
  /** Les travaux actifs par appli (50) ; au-delà, quota_exceeded. */
  maxActiveJobs?: number;
  comfy?: Partial<FakeComfy>;
}

export interface FakeAiodos {
  url: string;
  version: string;
  api: number;
  instance: string;
  features: string[];
  access: { apps: boolean; requests: boolean; approval_ui: boolean };
  mode: 'aiodos' | 'old' | 'ollama';
  listening: boolean;
  wrongProof: boolean;
  interval: number;
  expiresIn: number;
  chatDelayMs: number;
  models: { chat: Array<Record<string, unknown>>; embed: Array<Record<string, unknown>> };
  keys: Map<string, { id: string; name: string; scopes: string[]; where: string[]; priority: string; state: 'valid' | 'revoked' | 'expired' }>;
  requests: FakeRequest[];
  foreign: string[];
  /** Les flux fermés par le client avant la fin. */
  closed: number;
  pending: null | { deviceCode: string; userCode: string; requestId: string; state: string; scopes: string[]; where: string[]; polls: number[] };
  addKey(key: string, options?: { scopes?: string[]; where?: string[]; state?: 'valid' | 'revoked' | 'expired'; name?: string; priority?: string }): string;
  setKeyState(key: string, state: 'valid' | 'revoked' | 'expired'): void;
  revokeKey(key?: string): void;
  approve(options?: { scopes?: string[]; where?: string[] }): void;
  deny(): void;
  expire(): void;
  slowDown(n?: number): void;
  adoptRequest(request: { device_code: string; user_code: string; request_id?: string; scope?: string; where?: string[] | string }): unknown;
  failNext(code: string, options?: { status?: number; message?: string; hint?: string; retryable?: boolean; detail?: Record<string, unknown>; param?: string; retryAfter?: number; path?: string | RegExp }): void;
  respondNext(response: FakeResponse, options?: { path?: string | RegExp; when?: (request: FakeRequest) => boolean }): void;
  redirectNext(location?: string, options?: { path?: string | RegExp }): void;
  nextChat(spec: string | (Omit<FakeResponse, 'status'> & { status?: number; sse: string[] })): void;
  received(path: string): FakeRequest[];
  on(listener: (kind: 'request' | 'closed', data?: FakeRequest) => void): () => void;
  handle(request: { method: string; url: URL; headers: Record<string, string>; body: string | Uint8Array | null }): Promise<FakeResponse>;

  // --- les travaux ComfyUI ---
  jobMode: 'auto' | 'manual';
  jobStepMs: number;
  pingMs: number;
  maxActiveJobs: number;
  comfy: FakeComfy;
  /** Les entrées reçues, par appli : identifiant de l'appli → (sha256 → entrée). */
  inputs: Map<string, Map<string, { sha256: string; size: number; type: string; bytes: Uint8Array }>>;
  /** Les travaux, par identifiant (toutes applis confondues). */
  jobs: Map<string, FakeJob>;
  /** Les fichiers produits, par identifiant. */
  files: Map<string, { id: string; app: string; job: string; unit: number; node: string; type: string; content: Uint8Array; sha256: string; fetchedAt: number | null; served: number; [field: string]: unknown }>;
  /** Le prochain travail déposé : ses unités en échec (indices), ses fichiers par unité (1). */
  nextJob(script?: { failUnits?: number[]; filesPerUnit?: number }): void;
  /** Le prochain flux d'événements : coupé (connexion perdue) ou muet après n événements. */
  nextEvents(spec?: { cutAfter?: number; stallAfter?: number }): void;
  /** Le prochain contenu de fichier : coupé après n octets. */
  nextContent(spec?: { cutAt?: number }): void;
  /** Coupe tous les flux d'événements ouverts (AIODOS qui s'arrête). */
  cutStreams(): void;
  /** Une étape du travail (le dernier déposé sans id) : démarrer une unité, ou finir celles en cours. */
  step(id?: string): boolean;
  /** Toutes les étapes jusqu'à la fin (ou la pause) ; rend l'objet travail. */
  finish(id?: string): Record<string, unknown> | null;
  /** L'objet travail tel que l'appli le voit. */
  jobObject(id?: string): Record<string, unknown> | null;
}

export declare function createFake(options?: FakeOptions): FakeAiodos;
export declare function createFakeFetch(options?: FakeOptions | FakeAiodos): typeof globalThis.fetch & { fake: FakeAiodos };
export declare function startFakeServer(options?: FakeOptions | FakeAiodos): Promise<{ url: string; port: number; fake: FakeAiodos; server: unknown; close(): Promise<void> }>;
export declare function writeFakeDiscovery(options?: {
  state?: 'open' | 'closed' | 'error' | 'stopped' | 'headless';
  dir?: string;
  url?: string;
  pid?: number;
  instance?: string;
  running?: boolean;
  gateway?: Record<string, unknown>;
}): Promise<{ file: string; dir: string; data: Record<string, unknown>; env: { AIODOS_DISCOVERY_FILE: string }; write(patch?: Record<string, unknown>): Promise<void>; remove(): Promise<void> }>;
export declare function createFakeHome(options?: { helper?: 'ok' | 'error' | 'hang'; url?: string; dir?: string }): Promise<{
  dir: string;
  script: string;
  env: { AIODOS_HOME: string; AIODOS_PYTHON: string };
  stdoutOk: Record<string, unknown>;
  calls(): Promise<Array<{ args: string[]; cwd: string; stdin: string }>>;
  remove(): Promise<void>;
}>;
export declare function createFakeClient(
  options?: Partial<ClientOptions> & { fake?: FakeOptions; key?: string | null | KeyStore },
): { client: AiodosClient; fake: FakeAiodos; fetch: typeof globalThis.fetch & { fake: FakeAiodos }; keyStore: KeyStore };
export declare function loadContract(name: string): any;
export declare function contractInteraction(file: string, name: string): any;
