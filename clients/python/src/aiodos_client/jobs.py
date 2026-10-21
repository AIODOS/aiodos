# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""Les travaux et leurs fichiers (livraison 2 ; conception « partager la puissance », § 6.7,
6.8 et 6.10) : `ai.jobs()`, `ai.job(id)`, `Job`, `File`.

AIODOS ne les sert pas encore : tant que `features` n'annonce pas « jobs » (et « files »),
chaque fonction lève NotSupported sans rien essayer ; annoncées, c'est ce client (0.1.0)
qui est à mettre à jour.
"""

FEATURE = "jobs"
FILES = "files"


def list_jobs(client, active=True, client_ref=None, status=None):
    """Les travaux de l'appli (pour les retrouver après un redémarrage)."""
    client._refuse_unserved(FEATURE)


def get_job(client, job_id):
    """Un travail par son identifiant (« job_… »)."""
    client._refuse_unserved(FEATURE)


class Job:
    """Un travail d'AIODOS."""

    def __init__(self, client, job_id, data=None):
        self._client = client
        self.id = job_id
        self.data = dict(data or {})

    def __repr__(self):
        return f"Job({self.id!r})"

    def _unserved(self):
        self._client._refuse_unserved(FEATURE)

    def watch(self, cancel=None, timeout=None):
        """Les événements du travail ; appelle cancel() du travail si `cancel()` devient vrai."""
        self._unserved()

    def wait(self, cancel=None, timeout=None):
        """Attend la fin du travail."""
        self._unserved()

    def pause(self, when="after_current"):
        self._unserved()

    def resume(self):
        self._unserved()

    def cancel(self):
        self._unserved()

    def retry_failed(self):
        self._unserved()

    def set_priority(self, priority):
        self._unserved()

    def delete(self, files=False):
        self._unserved()

    def report(self, unit, reason):
        self._unserved()

    def files(self, unfetched=True):
        """Les fichiers produits (File)."""
        self._unserved()

    def download(self, dest_dir, stem=None, on_file=None):
        """Télécharge les fichiers produits (avec reprise) ; rend leurs chemins."""
        self._unserved()


class File:
    """Un fichier produit par un travail."""

    def __init__(self, client, file_id, data=None):
        self._client = client
        self.id = file_id
        self.data = dict(data or {})

    def __repr__(self):
        return f"File({self.id!r})"

    def _unserved(self):
        self._client._refuse_unserved(FILES)

    def content(self):
        self._unserved()

    def save(self, path):
        self._unserved()

    def link(self, ttl=900, uses=3):
        """Un lien signé, sans clé (60 min et 5 usages au plus)."""
        self._unserved()

    def delete(self):
        self._unserved()
