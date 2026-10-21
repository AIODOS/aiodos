// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — les numéros de version du client et de l'API qu'il parle.

/** Version de la bibliothèque (la même que dans package.json : un essai le vérifie). */
export const VERSION = '0.1.0';

/** Version de l'API des applications d'AIODOS que ce client parle (docs/api-applications.md). */
export const API_VERSION = 1;

/** Valeur de l'en-tête X-Aiodos-Client (journal d'AIODOS). */
export const CLIENT_HEADER = `js/${VERSION}`;
