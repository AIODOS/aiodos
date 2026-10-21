// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — les types du faux AIODOS (import … from 'aiodos-client/fake').

import type { AiodosClient, ClientOptions, KeyStore } from './index.js';

/** La clé de l'appli d'essai du contrat (aiodos_app_k3j7m2qa_ suivi de 43 « A »). */
export declare const FAKE_KEY: string;

export interface FakeRequest {
  method: string;
  path: string;
  /** En-têtes reçus, en minuscules. */
  headers: Record<string, string>;
  json: unknown;
  at: number;
}

export interface FakeResponse {
  status: number;
  headers?: Record<string, string>;
  json?: unknown;
  text?: string;
  /** Les lignes d'un flux SSE (« data: … » ou « : commentaire »). */
  sse?: string[];
  delayMs?: number;
  firstDelayMs?: number;
  /** Se taire après n événements, sans fermer. */
  stallAfter?: number;
  /** Couper la connexion après n événements. */
  cutAfter?: number;
  /** Ne jamais répondre. */
  hang?: boolean;
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
  scopes?: string[];
  where?: string[];
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
  handle(request: { method: string; url: URL; headers: Record<string, string>; body: string | null }): Promise<FakeResponse>;
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
