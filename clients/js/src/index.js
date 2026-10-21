// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — ce qu'une appli importe : createClient, pickModel, les erreurs, les
// textes, la conversion des messages, VERSION et API_VERSION.
//
// Le client tourne dans Node ou dans le processus principal d'Electron, jamais dans une page :
// guard.js, importé en premier, lève une erreur claire dans un rendu.

import './guard.js';

export { VERSION, API_VERSION } from './version.js';
export { createClient, memoryKeyStore } from './client.js';
export { pickModel } from './pick.js';
export { texts } from './texts.js';
export {
  toOpenAIMessages,
  fromOpenAIMessages,
  toOpenAIMessage,
  fromOpenAIMessage,
  toOpenAITools,
  fromOpenAITools,
  parseToolArguments,
  imageDataUrl,
} from './neutral.js';
export {
  AiodosError,
  isAiodosError,
  errorClassFor,
  NotInstalled,
  NotRunning,
  Unreachable,
  GatewayClosed,
  NotAiodos,
  TooOld,
  Untrusted,
  NonLocalUrl,
  Timeout,
  Cancelled,
  ProtocolError,
  NeedsAccess,
  AccessDenied,
  AccessExpired,
  Revoked,
  ScopeDenied,
  WhereDenied,
  AppsClosed,
  RequestsClosed,
  BlockedProgram,
  OtherAccount,
  ProgramMismatch,
  OriginRefused,
  HostRefused,
  ModelNotFound,
  NoMachine,
  NotSupported,
  ContextTooLong,
  ToolsUnavailable,
  VisionUnsupported,
  LocalOnly,
  UnsupportedParameter,
  InvalidRequest,
  FileTooLarge,
  UnsupportedMediaType,
  RateLimited,
  Busy,
  PeerFailed,
  EngineFailed,
  EngineUnavailable,
  ApprovalUnavailable,
  GraphInvalid,
  GraphTooLarge,
  ForbiddenValue,
  ForbiddenNode,
  InputMissing,
  InputHashMismatch,
  InputTooLarge,
  EngineNotFound,
  InputNotFound,
  JobNotFound,
  FileNotFound,
  ModelUnavailable,
  ModelUnresolved,
  ModelAmbiguous,
  JobNotCancellable,
  JobNotPausable,
  QuotaExceeded,
  JobFailed,
} from './errors.js';
