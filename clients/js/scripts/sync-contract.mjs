// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — recopie clients/contract/*.json dans clients/js/contract/ (le faux
// serveur publié avec le paquet sert cette copie ; un essai vérifie qu'elle est à jour).

import { copyFileSync, mkdirSync, readdirSync, rmSync } from 'node:fs';

const from = new URL('../../contract/', import.meta.url);
const to = new URL('../contract/', import.meta.url);
mkdirSync(to, { recursive: true });
for (const f of readdirSync(to)) if (f.endsWith('.json')) rmSync(new URL(f, to));
const files = readdirSync(from).filter((f) => f.endsWith('.json'));
for (const f of files) copyFileSync(new URL(f, from), new URL(f, to));
process.stdout.write(`${files.length} fichiers du contrat recopiés.\n`);
