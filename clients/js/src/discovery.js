// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — trouver AIODOS : le fichier de découverte, l'installation, le
// lancement et le programme d'aide « --app-request » (docs/api-applications.md § 1 et § 4.4).
//
// Rien de ce qu'on lit n'est exécuté : du fichier de découverte, seule l'adresse est
// retenue, et seulement si son hôte est local. Les programmes lancés sont calculés d'après
// l'installation (registre, dossier par défaut, AIODOS_HOME), sans shell, avec une liste
// d'arguments, et seulement sur appel explicite de l'appli.

import { execFile, spawn } from 'node:child_process';
import { readFile, stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';

import { clientError, makeError } from './errors.js';
import { isLocalUrl, localOrigin } from './transport.js';

/** Identifiant d'Inno Setup de l'installeur d'AIODOS (tools/installer/aiodos.iss). */
export const UNINSTALL_KEY =
  'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{8E1BB2BE-7BA8-4AF2-ACC7-463BA169FCF3}_is1';

const MAX_DISCOVERY_BYTES = 64 * 1024;
const MAX_HELPER_OUTPUT = 64 * 1024;

function pathFor(platform) {
  return platform === 'win32' ? path.win32 : path.posix;
}

/**
 * Le chemin du fichier de découverte, ou null (AIODOS_DISCOVERY_FILE vaut « - »).
 * Windows : %LOCALAPPDATA%\AIODOS\gateway.json ; ailleurs : $XDG_STATE_HOME/aiodos/gateway.json.
 */
export function discoveryPath({ env = process.env, platform = process.platform } = {}) {
  const forced = env.AIODOS_DISCOVERY_FILE;
  if (forced === '-') return null;
  if (forced) return forced;
  const P = pathFor(platform);
  if (platform === 'win32') {
    const local = env.LOCALAPPDATA || P.join(homedir(), 'AppData', 'Local');
    return P.join(local, 'AIODOS', 'gateway.json');
  }
  const state = env.XDG_STATE_HOME || P.join(env.HOME || homedir(), '.local', 'state');
  return P.join(state, 'aiodos', 'gateway.json');
}

/**
 * Le contenu du fichier de découverte s'il est lisible et vient d'AIODOS, sinon null.
 * Jamais d'erreur : un fichier absent, trop gros ou mal formé vaut « pas de fichier ».
 */
export async function readDiscovery({ env = process.env, platform = process.platform } = {}) {
  const file = discoveryPath({ env, platform });
  if (!file) return null;
  try {
    const st = await stat(file);
    if (!st.isFile() || st.size > MAX_DISCOVERY_BYTES) return null;
    const data = JSON.parse(await readFile(file, 'utf8'));
    if (!data || typeof data !== 'object' || data.product !== 'aiodos') return null;
    if (typeof data.format === 'number' && data.format < 1) return null;
    if (data.gateway != null && typeof data.gateway !== 'object') return null;
    return data;
  } catch {
    return null;
  }
}

/** L'adresse donnée par le fichier de découverte, si son hôte est local ; sinon null. */
export function discoveryUrl(disc) {
  const url = disc && disc.gateway && disc.gateway.url;
  if (typeof url !== 'string' || !isLocalUrl(url)) return null;
  return localOrigin(url);
}

/**
 * Le programme `pid` vit-il ? true, false, ou null (on ne sait pas).
 * process.kill(pid, 0) ne fait que tester (sous Windows aussi, pour Node) ; un pid qui n'est
 * pas un entier positif n'est jamais passé (0 ou négatif viseraient un groupe de programmes).
 */
export function pidAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return null;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    if (err && err.code === 'ESRCH') return false;
    if (err && err.code === 'EPERM') return true; // il existe, sous un autre compte
    return null;
  }
}

/** La valeur InstallLocation dans la sortie de « reg query », ou null. */
export function parseRegQuery(stdout) {
  const m = /^\s*InstallLocation\s+REG_(?:EXPAND_)?SZ\s+(.+?)\s*$/im.exec(String(stdout || ''));
  return m ? m[1] : null;
}

function execFileText(file, args, options) {
  return new Promise((resolve) => {
    execFile(file, args, options, (err, stdout) => resolve(err ? null : String(stdout)));
  });
}

async function exists(p) {
  try {
    await stat(p);
    return true;
  } catch {
    return false;
  }
}

/** La racine indiquée par le registre (HKCU puis HKLM), lue par « reg query » sans shell. */
async function registryRoots({ env, run }) {
  const reg = path.win32.join(env.SystemRoot || env.windir || 'C:\\Windows', 'System32', 'reg.exe');
  const roots = [];
  for (const hive of ['HKCU', 'HKLM']) {
    const out = await run(reg, ['query', `${hive}\\${UNINSTALL_KEY}`, '/v', 'InstallLocation'], {
      windowsHide: true,
      timeout: 5000,
      encoding: 'utf8',
    });
    const loc = parseRegQuery(out);
    if (loc) roots.push(loc);
  }
  return roots;
}

/**
 * L'installation d'AIODOS, ou null.
 * Ordre : AIODOS_HOME (seul, s'il est donné) ; sinon, sous Windows, le registre (HKCU, HKLM)
 * puis %LOCALAPPDATA%\Programs\AIODOS.
 * @returns {Promise<null | {root: string, kind: 'installed'|'dev', cwd: string, python: string|null, pythonw: string|null}>}
 */
export async function findInstall({ env = process.env, platform = process.platform, execFile: run = execFileText } = {}) {
  const P = pathFor(platform);
  const roots = [];
  if (env.AIODOS_HOME) {
    roots.push(env.AIODOS_HOME);
  } else if (platform === 'win32') {
    roots.push(...(await registryRoots({ env, run })));
    const local = env.LOCALAPPDATA || P.join(homedir(), 'AppData', 'Local');
    roots.push(P.join(local, 'Programs', 'AIODOS'));
  }
  for (const root of roots) {
    if (await exists(P.join(root, 'aiodos', '__init__.py'))) {
      // copie de développement : l'interpréteur vient d'AIODOS_PYTHON, sinon rien n'est lancé
      const py = env.AIODOS_PYTHON || null;
      return { root, kind: 'dev', cwd: root, python: py, pythonw: py };
    }
    const runtime = P.join(root, 'runtime');
    const python = P.join(runtime, 'python.exe');
    const pythonw = P.join(runtime, 'pythonw.exe');
    if ((await exists(python)) || (await exists(pythonw))) {
      return { root, kind: 'installed', cwd: P.join(root, 'app'), python, pythonw };
    }
  }
  return null;
}

/**
 * La commande à lancer pour un interpréteur. Un script Node (.mjs, .cjs, .js) donné par
 * AIODOS_PYTHON est lancé par Node lui-même : c'est ainsi que les essais remplacent Python
 * par un faux programme d'aide.
 */
function interpreter(exe) {
  if (/\.(mjs|cjs|js)$/i.test(exe)) {
    const extra = process.versions.electron ? { ELECTRON_RUN_AS_NODE: '1' } : {};
    return { command: process.execPath, args: [exe], env: extra };
  }
  return { command: exe, args: [], env: {} };
}

async function programFor(o, which, lang) {
  const inst = await findInstall(o);
  if (!inst) throw clientError('not_installed', lang);
  const exe = which === 'gui' ? inst.pythonw : inst.python;
  if (inst.kind === 'dev' && !exe) throw clientError('not_installed', lang, { textKey: 'dev_copy', vars: { path: inst.root } });
  // l'installé : pythonw.exe pour lancer AIODOS ; python.exe (sans fenêtre) pour le programme d'aide
  let chosen = exe;
  if (inst.kind === 'installed' && !(await exists(chosen))) {
    const other = which === 'gui' ? inst.python : inst.pythonw;
    if (await exists(other)) chosen = other;
    else throw clientError('not_installed', lang, { textKey: 'program_missing', vars: { path: chosen } });
  }
  return { inst, ...interpreter(chosen) };
}

/**
 * Lance AIODOS en arrière-plan (pythonw.exe -m aiodos --background), détaché, sans shell
 * ni fenêtre. Seulement sur un clic de l'utilisateur ; rien n'est jamais installé.
 */
export async function launchAiodos({ env = process.env, platform = process.platform, execFile: run, lang = 'fr' } = {}) {
  const o = { env, platform, execFile: run };
  const disc = await readDiscovery(o);
  if (disc && disc.running !== false && pidAlive(disc.pid) === true) {
    return { launched: false, alreadyRunning: true, pid: disc.pid };
  }
  const prog = await programFor(o, 'gui', lang);
  const child = spawn(prog.command, [...prog.args, '-m', 'aiodos', '--background'], {
    cwd: prog.inst.cwd,
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
    shell: false,
    env: { ...env, ...prog.env },
  });
  await new Promise((resolve, reject) => {
    child.once('spawn', resolve);
    child.once('error', reject);
  }).catch((err) => {
    throw clientError('not_installed', lang, { textKey: 'program_missing', vars: { path: prog.command }, cause: err });
  });
  child.unref();
  return { launched: true, alreadyRunning: false, pid: child.pid, kind: prog.inst.kind };
}

/**
 * Le programme d'aide (§ 4.4) : python.exe -m aiodos --app-request, depuis <racine>\app, sans
 * shell ni fenêtre ; la demande en une ligne JSON sur l'entrée standard ; une ligne JSON sur
 * la sortie standard ; code 0 (succès) ou 1 (erreur).
 * @returns {Promise<{deviceCode: string, userCode: string, interval: number, expiresIn: number, requestId: string|null, url: string|null}>}
 */
export async function runAppRequest(body, { env = process.env, platform = process.platform, execFile: run, lang = 'fr', signal, timeout = 90_000, log = () => {} } = {}) {
  const prog = await programFor({ env, platform, execFile: run }, 'console', lang);
  if (signal && signal.aborted) throw clientError('cancelled', lang);
  const child = spawn(prog.command, [...prog.args, '-m', 'aiodos', '--app-request'], {
    cwd: prog.inst.cwd,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
    shell: false,
    env: { ...env, ...prog.env },
  });
  let out = '';
  let errText = '';
  let tooBig = false;
  child.stdout.setEncoding('utf8');
  child.stderr.setEncoding('utf8');
  child.stdout.on('data', (s) => {
    if (out.length + s.length > MAX_HELPER_OUTPUT) tooBig = true;
    else out += s;
  });
  child.stderr.on('data', (s) => {
    if (errText.length < 4096) errText += s;
  });
  let stopped = null;
  const kill = (why) => {
    if (stopped) return;
    stopped = why;
    try {
      child.kill();
    } catch {
      /* déjà fini */
    }
  };
  const timer = setTimeout(() => kill('timeout'), timeout);
  const onAbort = () => kill('cancelled');
  if (signal) signal.addEventListener('abort', onAbort, { once: true });
  child.stdin.on('error', () => {}); // programme parti avant d'avoir tout lu
  child.stdin.end(`${JSON.stringify(body)}\n`);

  let exitCode;
  try {
    exitCode = await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.once('close', (code) => resolve(code));
    });
  } catch (err) {
    throw clientError('not_installed', lang, { textKey: 'program_missing', vars: { path: prog.command }, cause: err });
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onAbort);
  }
  if (stopped === 'cancelled') throw clientError('cancelled', lang);
  if (stopped === 'timeout') throw clientError('timeout', lang);
  if (errText) log('debug', `programme d'aide : ${errText.slice(0, 300)}`);

  const line = tooBig ? null : out.split(/\r?\n/).map((s) => s.trim()).find((s) => s.startsWith('{'));
  let data = null;
  try {
    data = line ? JSON.parse(line) : null;
  } catch {
    data = null;
  }
  if (exitCode !== 0) {
    const e = data && data.error && typeof data.error === 'object' ? data.error : null;
    if (e && typeof e.code === 'string') throw makeError(e.code, e.message || e.code, { detail: e.detail });
    throw clientError('protocol', lang, { detail: { exitCode } });
  }
  if (!data || typeof data.device_code !== 'string' || typeof data.user_code !== 'string') {
    throw clientError('protocol', lang);
  }
  return {
    deviceCode: data.device_code,
    userCode: data.user_code,
    interval: Number(data.interval) || 2,
    expiresIn: Number(data.expires_in) || 600,
    requestId: typeof data.request_id === 'string' ? data.request_id : null,
    url: typeof data.url === 'string' && isLocalUrl(data.url) ? localOrigin(data.url) : null,
  };
}
