// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — essais : le client refuse de tourner dans une page (rendu).

import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { test } from 'node:test';

import { assertNotRenderer, rendererProblem } from '../src/guard.js';

test('une page sans Node est refusée, Node et le processus principal passent', () => {
  assert.match(rendererProblem({ window: {}, document: {} }), /processus principal/);
  assert.match(rendererProblem({ document: {} }), /jamais dans une page/);
  assert.equal(rendererProblem({ process: { versions: { node: '20.3.0' } } }), null);
  assert.equal(rendererProblem({ window: {}, process: { versions: { node: '24.0.0' } } }), null);
  // rendu d'Electron, même avec nodeIntegration
  assert.ok(rendererProblem({ window: {}, process: { versions: { node: '24.0.0', electron: '44.0.0' }, type: 'renderer' } }));
  // processus principal d'Electron
  assert.equal(rendererProblem({ process: { versions: { node: '24.0.0', electron: '44.0.0' }, type: 'browser' } }), null);
  assert.throws(() => assertNotRenderer({ window: {} }), /aiodos-client tourne dans Node/);
});

test("importer le client dans un rendu lève une erreur claire", async () => {
  const index = new URL('../src/index.js', import.meta.url).href;
  const script = `process.type = 'renderer'; try { await import(${JSON.stringify(index)}); console.log('importé'); } catch (e) { console.log('refusé : ' + e.message); }`;
  const out = await new Promise((resolve, reject) => {
    execFile(process.execPath, ['--input-type=module', '-e', script], { windowsHide: true, timeout: 20_000 }, (err, stdout) => (err ? reject(err) : resolve(stdout)));
  });
  assert.match(out, /^refusé : aiodos-client tourne dans Node/);
});
