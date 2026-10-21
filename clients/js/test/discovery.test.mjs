// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : trouver AIODOS (gateway.json, pid, registre, installation),
// le lancer (launch) et le programme d'aide (--app-request), sans jamais toucher au vrai.

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { createFakeClient, createFakeHome, writeFakeDiscovery } from '../src/fake.js';
import { discoveryPath, discoveryUrl, findInstall, parseRegQuery, pidAlive, readDiscovery, runAppRequest, UNINSTALL_KEY } from '../src/discovery.js';

const contract = JSON.parse(readFileSync(new URL('../../contract/discovery.json', import.meta.url), 'utf8'));

test('le chemin du fichier de découverte', () => {
  assert.equal(discoveryPath({ platform: 'win32', env: { LOCALAPPDATA: 'C:\\Users\\moi\\AppData\\Local' } }), 'C:\\Users\\moi\\AppData\\Local\\AIODOS\\gateway.json');
  assert.equal(discoveryPath({ platform: 'linux', env: { XDG_STATE_HOME: '/home/moi/.state' } }), '/home/moi/.state/aiodos/gateway.json');
  assert.equal(discoveryPath({ platform: 'linux', env: { HOME: '/home/moi' } }), '/home/moi/.local/state/aiodos/gateway.json');
  assert.equal(discoveryPath({ platform: 'win32', env: { AIODOS_DISCOVERY_FILE: 'D:\\essai\\g.json' } }), 'D:\\essai\\g.json');
  assert.equal(discoveryPath({ env: { AIODOS_DISCOVERY_FILE: '-' } }), null);
});

test('readDiscovery : chaque état du contrat ; un fichier douteux vaut « pas de fichier »', async () => {
  for (const state of Object.keys(contract.files)) {
    const d = await writeFakeDiscovery({ state });
    try {
      const got = await readDiscovery({ env: d.env });
      assert.equal(got.product, 'aiodos');
      assert.deepEqual(got.gateway, contract.files[state].gateway);
      assert.equal(got.running, contract.files[state].running);
    } finally {
      await d.remove();
    }
  }
  const dir = await mkdtemp(path.join(tmpdir(), 'aiodos-disc-'));
  try {
    const file = path.join(dir, 'gateway.json');
    for (const content of ['{pas du json', JSON.stringify({ product: 'ollama' }), JSON.stringify({ product: 'aiodos', format: 0 }), JSON.stringify({ product: 'aiodos', gateway: 'ouvert' }), ' '.repeat(70_000)]) {
      await writeFile(file, content);
      assert.equal(await readDiscovery({ env: { AIODOS_DISCOVERY_FILE: file } }), null, content.slice(0, 30));
    }
    assert.equal(await readDiscovery({ env: { AIODOS_DISCOVERY_FILE: path.join(dir, 'absent.json') } }), null);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('discoveryUrl : seulement une adresse locale, jamais rien d’autre du fichier', () => {
  assert.equal(discoveryUrl(contract.files.open), 'http://127.0.0.1:11435');
  assert.equal(discoveryUrl({ gateway: { url: 'http://localhost:12000/x' } }), 'http://localhost:12000');
  assert.equal(discoveryUrl({ gateway: { url: 'http://exemple.fr:11435' } }), null);
  assert.equal(discoveryUrl({ gateway: { url: 42 } }), null);
  assert.equal(discoveryUrl(null), null);
});

test('pidAlive : process.kill(pid, 0) ne fait que tester ; jamais pour un pid douteux', async () => {
  assert.equal(pidAlive(process.pid), true);
  const child = spawn(process.execPath, ['-e', ''], { stdio: 'ignore', windowsHide: true });
  await once(child, 'exit');
  assert.equal(pidAlive(child.pid), false);
  const saved = process.kill;
  const seen = [];
  process.kill = (pid, sig) => {
    seen.push([pid, sig]);
    return true;
  };
  try {
    for (const bad of [0, -1, -4000, 1.5, '1234', NaN, null, undefined, 2 ** 60]) assert.equal(pidAlive(bad), null, String(bad));
    assert.deepEqual(seen, []); // 0 ou négatif viseraient un groupe de programmes
  } finally {
    process.kill = saved;
  }
});

test('parseRegQuery : InstallLocation dans la sortie de « reg query »', () => {
  const out = `\r\nHKEY_CURRENT_USER\\${UNINSTALL_KEY}\r\n    InstallLocation    REG_SZ    C:\\Users\\moi\\AppData\\Local\\Programs\\AIODOS\\\r\n\r\n`;
  assert.equal(parseRegQuery(out), 'C:\\Users\\moi\\AppData\\Local\\Programs\\AIODOS\\');
  assert.equal(parseRegQuery('ERREUR : le système n’a pas trouvé la clé'), null);
  assert.equal(parseRegQuery(null), null);
});

test('findInstall : AIODOS_HOME (développement ou installé), registre, dossier par défaut', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'aiodos-inst-'));
  try {
    // rien
    assert.equal(await findInstall({ env: { AIODOS_HOME: path.join(root, 'absent') } }), null);
    assert.equal(await findInstall({ platform: 'linux', env: {} }), null);
    // copie de développement : l'interpréteur vient d'AIODOS_PYTHON
    const dev = path.join(root, 'dev');
    await mkdir(path.join(dev, 'aiodos'), { recursive: true });
    await writeFile(path.join(dev, 'aiodos', '__init__.py'), '');
    assert.deepEqual(await findInstall({ env: { AIODOS_HOME: dev, AIODOS_PYTHON: 'C:\\Python313\\python.exe' } }), {
      root: dev,
      kind: 'dev',
      cwd: dev,
      python: 'C:\\Python313\\python.exe',
      pythonw: 'C:\\Python313\\python.exe',
    });
    assert.equal((await findInstall({ env: { AIODOS_HOME: dev } })).python, null);
    // copie installée : runtime\python.exe, depuis <racine>\app
    const inst = path.join(root, 'Programs', 'AIODOS');
    await mkdir(path.join(inst, 'runtime'), { recursive: true });
    await writeFile(path.join(inst, 'runtime', 'python.exe'), '');
    await writeFile(path.join(inst, 'runtime', 'pythonw.exe'), '');
    const P = path.win32;
    const want = { root: inst, kind: 'installed', cwd: P.join(inst, 'app'), python: P.join(inst, 'runtime', 'python.exe'), pythonw: P.join(inst, 'runtime', 'pythonw.exe') };
    // par le registre (HKCU), lu sans shell
    const queried = [];
    const reg = async (file, args, opts) => {
      queried.push({ file, args, shell: opts.shell });
      return args[1].startsWith('HKCU') ? `    InstallLocation    REG_SZ    ${inst}\\\r\n` : null;
    };
    const viaReg = await findInstall({ platform: 'win32', env: { LOCALAPPDATA: path.join(root, 'ailleurs'), SystemRoot: 'C:\\Windows' }, execFile: reg });
    assert.equal(viaReg.kind, 'installed');
    assert.equal(path.resolve(viaReg.root), path.resolve(inst));
    assert.equal(queried[0].file, 'C:\\Windows\\System32\\reg.exe');
    assert.deepEqual(queried[0].args, ['query', `HKCU\\${UNINSTALL_KEY}`, '/v', 'InstallLocation']);
    assert.notEqual(queried[0].shell, true);
    // sans registre : %LOCALAPPDATA%\Programs\AIODOS
    const none = async () => null;
    assert.deepEqual(await findInstall({ platform: 'win32', env: { LOCALAPPDATA: root }, execFile: none }), want);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('launch() : -m aiodos --background, détaché, depuis la racine ; rien si AIODOS tourne', async () => {
  const home = await createFakeHome();
  try {
    const { client } = createFakeClient({ env: home.env });
    const r = await client.launch();
    assert.equal(r.launched, true);
    assert.equal(r.kind, 'dev');
    assert.ok(Number.isInteger(r.pid));
    let calls = [];
    for (let i = 0; i < 100 && calls.length === 0; i++) {
      await new Promise((res) => setTimeout(res, 50));
      calls = await home.calls();
    }
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].args, ['-m', 'aiodos', '--background']);
    assert.equal(calls[0].cwd.toLowerCase(), home.dir.toLowerCase());
    // AIODOS tourne déjà (pid vivant) : rien n'est lancé
    const disc = await writeFakeDiscovery({ state: 'open' });
    try {
      const running = createFakeClient({ env: { ...home.env, ...disc.env } }).client;
      assert.deepEqual(await running.launch(), { launched: false, alreadyRunning: true, pid: process.pid });
    } finally {
      await disc.remove();
    }
  } finally {
    await home.remove();
  }
});

test('launch() : copie de développement sans AIODOS_PYTHON, ou rien d’installé : NotInstalled', async () => {
  const home = await createFakeHome();
  try {
    const dev = createFakeClient({ env: { AIODOS_HOME: home.dir } }).client;
    await assert.rejects(dev.launch(), (e) => e.code === 'not_installed' && e.name === 'NotInstalled' && /AIODOS_PYTHON/.test(e.message));
  } finally {
    await home.remove();
  }
  await assert.rejects(createFakeClient().client.launch(), (e) => e.code === 'not_installed' && e.message === "AIODOS n'est pas installé sur ce PC.");
});

test('runAppRequest : la sortie du contrat (succès, erreur), délai, annulation', async () => {
  const body = contract.app_request.stdin;
  const ok = await createFakeHome({ helper: 'ok' });
  const bad = await createFakeHome({ helper: 'error' });
  const slow = await createFakeHome({ helper: 'hang' });
  try {
    const out = await runAppRequest(body, { env: { ...process.env, ...ok.env } });
    const s = contract.app_request.stdout_ok;
    assert.deepEqual(out, { deviceCode: s.device_code, userCode: s.user_code, interval: s.interval, expiresIn: s.expires_in, requestId: s.request_id, url: s.url });
    assert.deepEqual(JSON.parse((await ok.calls())[0].stdin), body);

    await assert.rejects(runAppRequest(body, { env: { ...process.env, ...bad.env } }), (e) => e.code === contract.app_request.stdout_error.error.code && e.message === contract.app_request.stdout_error.error.message);

    await assert.rejects(runAppRequest(body, { env: { ...process.env, ...slow.env }, timeout: 300 }), (e) => e.code === 'timeout');
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 200);
    await assert.rejects(runAppRequest(body, { env: { ...process.env, ...slow.env }, signal: ctrl.signal }), (e) => e.code === 'cancelled' && e.message === 'Arrêté');
  } finally {
    await ok.remove();
    await bad.remove();
    await slow.remove();
  }
});
