# Publier aiodos-client (PyPI et npm)

<!-- SPDX-License-Identifier: Apache-2.0 -->

Décision de l'auteur (20/10/2026) : les deux bibliothèques clientes sont publiées dès
maintenant, sous Apache-2.0. Les noms `aiodos-client` étaient libres sur PyPI et sur npm le
21/10/2026. La publication passe par GitHub Actions en « trusted publishing » : aucun jeton
n'est créé ni gardé sur un disque ; PyPI et npm ne font confiance qu'au workflow
`.github/workflows/clients-publish.yml` du dépôt `AIODOS/aiodos`, et npm attache la
provenance du paquet.

## Une fois (l'auteur)

1. **Comptes** : un compte sur https://pypi.org et un sur https://www.npmjs.com, avec la
   double authentification (exigée pour publier).
2. **PyPI → « Publishing » → « Add a new pending publisher »** : projet `aiodos-client`,
   propriétaire `AIODOS`, dépôt `aiodos`, workflow `clients-publish.yml`, environnement `pypi`.
3. **npm** : le paquet doit exister pour qu'on règle son éditeur de confiance ; la première
   version se publie donc à la main (`npm publish --access public` dans `clients/js`, après
   `npm login`), puis **Settings → Trusted Publisher → GitHub Actions** : propriétaire
   `AIODOS`, dépôt `aiodos`, workflow `clients-publish.yml`, environnement `npm`. Ensuite,
   « Publishing access » : exiger la double authentification et refuser les jetons.
4. **GitHub → AIODOS/aiodos → Settings → Environments** : créer `pypi` et `npm`, avec
   « Required reviewers » = l'auteur (chaque publication attend son clic) et la règle « Only
   tags matching clients-v* ».

## À chaque version

1. Mettre la même version partout : `clients/python/pyproject.toml`,
   `clients/python/src/aiodos_client/__init__.py` (`__version__`), `clients/js/package.json`,
   `clients/js/src/index.js` (`VERSION`), et une ligne dans `clients/CHANGELOG.md`.
2. Sur le PC : `py -3.14 tools/publish_clients.py` (versions, licences, aucune ligne reprise
   de PANDORA ni d'ARKLIGHT PC, essais, construction). Il ne publie rien.
3. Étiqueter et pousser : `git tag -a clients-v0.1.0 -m "aiodos-client 0.1.0"` puis
   `git push origin clients-v0.1.0`. Le workflow vérifie de nouveau, attend l'accord dans
   l'environnement, puis publie.

## Dans les applis

- PANDORA : `aiodos-client==0.1.0` dans `requirements.txt`, avec son empreinte
  (`--require-hashes`).
- ARKLIGHT PC : `npm i aiodos-client@0.1.0 --save-exact` ; `package-lock.json` garde
  l'empreinte.
