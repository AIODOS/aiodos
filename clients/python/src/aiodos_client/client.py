# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""Aiodos : le client d'une appli pour la passerelle locale d'AIODOS.

Sans état global modifiable et sûr entre threads : chaque requête a sa propre connexion, et
ce que le client retient (la dernière preuve d'identité, la demande d'accès en cours, le
catalogue récent) est gardé par un verrou. Bloquant : à appeler depuis un fil de travail
(QThread) ; les rappels (on_text, on_code…) sont appelés dans ce fil-là. Seul `cancel` est
interrogé par un fil de surveillance : il doit être sûr entre threads.

L'adresse : le paramètre `url`, puis AIODOS_URL, puis le fichier de découverte, puis
http://127.0.0.1:11435 ; un hôte autre que 127.0.0.1, localhost ou [::1] est refusé avant
tout envoi. La clé : celle du coffre de l'appli (`keystore`), sinon AIODOS_KEY (essais et
développement ; une variable d'environnement passe aux programmes enfants). Elle n'est
envoyée qu'après une preuve d'identité juste (docs/api-applications.md § 5.1), refaite quand
l'instance change, après une erreur de connexion, et au-delà de 60 s.
"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from . import API_VERSION, __version__, _discovery, _proof, _transport, errors, texts
from . import access as _access
from . import chat as _chat
from . import models as _models
from . import status as _status
from .comfy import Comfy
from .inputs import Inputs
from .jobs import get_job, list_jobs
from .keystore import Keystore

log = logging.getLogger("aiodos_client")

DEFAULT_URL = "http://127.0.0.1:11435"
PROOF_MAX_AGE = 60.0
MODELS_MAX_AGE = 120.0
INFO_TIMEOUT = 10.0
INFO_LIMIT = 1024 * 1024
ERROR_LIMIT = 1024 * 1024
_APP_ID = re.compile(r"^[a-z0-9.-]{2,40}$")


@dataclass(frozen=True)
class _Trust:
    """La dernière preuve juste : pour quelle adresse, quelle instance, quelle clé, quand."""
    endpoint: object
    instance: str | None
    fingerprint: str
    at: float
    info: dict


class Aiodos:
    """Le client d'une appli pour AIODOS.

    `app` : le nom montré dans AIODOS (« PANDORA ») ; `app_id` : l'identifiant de l'appli
    (« arkane.pandora », `[a-z0-9.-]{2,40}`) ; `version` : sa version ; `keystore` : son coffre
    de clé (Keystore.dpapi(…), .file(…), .memory(), ou un objet à get/set/delete) ; `lang` :
    « fr » ou « en » ; `url` : une autre adresse d'AIODOS ; `timeout` : le délai d'inactivité
    d'une réponse (s) ; `load_timeout` : celui d'avant le premier morceau d'une conversation,
    quand le modèle n'est pas chargé.
    """

    def __init__(self, app, app_id, version, keystore=None, lang="fr", url=None, timeout=240,
                 load_timeout=900):
        name = "".join(ch for ch in str(app or "") if ch.isprintable()).strip()
        if not 1 <= len(name) <= 40:
            raise ValueError("app : de 1 à 40 caractères imprimables")
        if not isinstance(app_id, str) or not _APP_ID.match(app_id):
            raise ValueError("app_id : de 2 à 40 caractères parmi a-z, 0-9, « . » et « - »")
        version = str(version if version is not None else "")
        if len(version) > 40:
            raise ValueError("version : 40 caractères au plus")
        self.app = name
        self.app_id = app_id
        self.version = version
        self.lang = texts.lang_of(lang)
        self.timeout = float(timeout)
        self.load_timeout = float(load_timeout)
        if self.timeout <= 0 or self.load_timeout <= 0:
            raise ValueError("timeout et load_timeout : des secondes, plus que zéro")
        store = keystore if keystore is not None else Keystore.memory()
        for method in ("get", "set", "delete"):
            if not callable(getattr(store, method, None)):
                raise TypeError("keystore : un objet avec get(), set(clé) et delete()")
        if isinstance(store, Keystore):
            store.bind(app_id)
        self._keystore = store
        self._url = _transport.Endpoint(url, self.lang) if url else None
        env_url = os.environ.get("AIODOS_URL")
        self._env_url = (_transport.Endpoint(env_url, self.lang)
                         if self._url is None and env_url and env_url.strip() else None)
        self._lock = threading.Lock()
        self._trusted = None
        self._pending = None
        self._denied = False
        self._models = {}
        self.comfy = Comfy(self)
        self.inputs = Inputs(self)

    def __repr__(self):
        fixed = self._url or self._env_url
        return (f"Aiodos(app={self.app!r}, app_id={self.app_id!r}, "
                f"url={fixed.url if fixed else 'auto'!r})")

    # --- État, lancement --------------------------------------------------------------
    def status(self):
        """L'état d'AIODOS pour cette appli (Status) ; ne lève jamais."""
        return _status.status(self)

    def wait_ready(self, timeout=60, cancel=None):
        """Attend qu'AIODOS réponde (au plus `timeout` s) et rend son état ; ne lève jamais."""
        return _status.wait_ready(self, timeout, cancel)

    def launch(self, wait=60, cancel=None):
        """Lance AIODOS en arrière-plan, sur un clic de l'utilisateur seulement
        (`pythonw.exe -m aiodos --background`, sans shell ni fenêtre), puis attend qu'il
        réponde (`wait` s, 0 : sans attendre). Rend son état. Rien n'est téléchargé ni
        installé : NotInstalled si AIODOS n'est pas là."""
        disc = _discovery.read_discovery()
        if not (disc is not None and disc.running and _discovery.pid_alive(disc.pid)):
            inst = _discovery.installation()
            if inst is None:
                raise errors.make("not_installed", self.lang)
            _discovery.launch(inst, self.lang)
        return self.wait_ready(wait, cancel) if wait else self.status()

    # --- Accès ------------------------------------------------------------------------
    def connect(self, scopes, where=None, purpose=None, on_code=None, cancel=None, timeout=600):
        """Connecte l'appli à AIODOS (sur un clic de l'utilisateur) et range la clé.

        Passerelle ouverte : une demande d'accès ; fermée, ou AIODOS arrêté : le programme
        d'aide d'AIODOS. `on_code(code)` reçoit le code à afficher en grand (« 482 197 »),
        puis la clé est relevée dès que l'utilisateur autorise l'appli dans AIODOS. Déjà
        connectée avec ces droits : rend l'accès actuel sans rien demander. Rend un Access.
        """
        return _access.connect(self, scopes, where, purpose, on_code, cancel, timeout)

    def request_access(self, scopes, where=None, purpose=None, cancel=None):
        """La demande d'accès seule (sous connect()) : rend un AccessRequest, dont `user_code`
        est à afficher et `wait()` relève la clé."""
        return _access.request_access(self, scopes, where, purpose, cancel)

    def disconnect(self):
        """Se déconnecter d'AIODOS : retire la clé dans AIODOS, puis l'efface du coffre.
        Rend vrai si AIODOS a confirmé le retrait (la clé est effacée dans tous les cas)."""
        return _access.disconnect(self)

    # --- Modèles, conversation, embeddings ---------------------------------------------
    def models(self, kind="chat"):
        """Le catalogue des modèles permis (`kind` : « chat » ou « embed ») : ModelList."""
        return _models.list_models(self, kind)

    def chat(self, messages, model=None, system=None, tools=None, think=None, max_tokens=None,
             temperature=None, top_p=None, stop=None, seed=None, response_format=None,
             where=None, batch=False, require_tools=True, on_text=None, on_thinking=None,
             on_loading=None, cancel=None, timeout=None):
        """Une conversation, toujours en flux : rend un ChatResult (voir chat.py)."""
        return _chat.chat(self, messages, model=model, system=system, tools=tools, think=think,
                          max_tokens=max_tokens, temperature=temperature, top_p=top_p,
                          stop=stop, seed=seed, response_format=response_format, where=where,
                          batch=batch, require_tools=require_tools, on_text=on_text,
                          on_thinking=on_thinking, on_loading=on_loading, cancel=cancel,
                          timeout=timeout)

    def embed(self, texts, model=None):
        """Les vecteurs de `texts` (un texte ou une liste) : list[list[float]], sur ce PC."""
        return _chat.embed(self, texts, model)

    def open_in_aiodos(self, page, model=None):
        """Ramène la fenêtre d'AIODOS sur une page (« applications », « models », « jobs »,
        « gallery ») ; rend vrai si une fenêtre l'a montrée."""
        return _chat.open_in_aiodos(self, page, model)

    # --- Livraison 2 : présentes, NotSupported tant que features ne les annonce pas ------
    def engines(self):
        """Les moteurs (ComfyUI…) des machines permises (L2)."""
        self._refuse_unserved("engines")

    def jobs(self, active=True, client_ref=None, status=None):
        """Les travaux de l'appli, pour les retrouver après un redémarrage (L2)."""
        return list_jobs(self, active, client_ref, status)

    def job(self, job_id):
        """Un travail par son identifiant (L2)."""
        return get_job(self, job_id)

    # --- Adresse --------------------------------------------------------------------
    def _resolve(self):
        """(adresse, découverte lue, la découverte vaut-elle pour cette adresse ?)"""
        disc = _discovery.read_discovery()
        fixed = self._url or self._env_url
        if fixed is not None:
            applies = (disc is not None and disc.url is not None
                       and _transport.Endpoint(disc.url) == fixed)
            return fixed, disc, applies
        if disc is not None and disc.url is not None:
            return _transport.Endpoint(disc.url), disc, True
        return _transport.Endpoint(DEFAULT_URL), disc, False

    def _target(self):
        """L'adresse à appeler, et la découverte si elle vaut pour elle ; lève NotRunning ou
        GatewayClosed quand la découverte le dit, sans rien essayer."""
        endpoint, disc, applies = self._resolve()
        if not applies:
            return endpoint, None
        if not disc.running or not _discovery.pid_alive(disc.pid):
            raise errors.make("not_running", self.lang)
        if disc.state == "closed":
            raise errors.make("gateway_closed", self.lang)
        if disc.state == "error":
            raise errors.make("gateway_closed", self.lang,
                              message=disc.error or texts.state("gateway_error", self.lang))
        return endpoint, disc

    # --- Clé ------------------------------------------------------------------------
    def _stored_key(self):
        """La clé du coffre (ou d'AIODOS_KEY), ou None ; ne lève jamais."""
        try:
            key = self._keystore.get()
        except Exception as exc:
            log.warning("Coffre de la clé illisible (%s) : clé ignorée.", type(exc).__name__)
            key = None
        if not key:
            key = os.environ.get("AIODOS_KEY") or None
        if isinstance(key, bytes):
            key = key.decode("utf-8", "replace")
        if not isinstance(key, str):
            return None
        return key.strip() or None

    def _read_key(self):
        """La clé d'appli, ou NeedsAccess (absente, ou d'une forme inattendue)."""
        key = self._stored_key()
        if not _proof.key_id(key):
            if key:
                log.warning("Clé d'une forme inattendue : elle n'est pas envoyée.")
                raise errors.make("needs_access", self.lang,
                                  message=texts.error("bad_key", self.lang),
                                  hint=texts.hint("bad_key", self.lang))
            raise errors.make("needs_access", self.lang)
        return key

    def _store_key(self, key):
        self._keystore.set(key)
        with self._lock:
            self._trusted = None
            self._denied = False

    def _delete_key(self):
        try:
            self._keystore.delete()
        finally:
            self._forget_trust()

    def _headers(self, key=None, *, json_body=False, accept="application/json", extra=None):
        headers = {"Accept": accept, "Accept-Language": self.lang,
                   "User-Agent": f"aiodos-client-python/{__version__}",
                   "X-Aiodos-Client": f"python/{__version__}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if key:
            headers["Authorization"] = "Bearer " + key
        if extra:
            headers.update(extra)
        return headers

    # --- Info et preuve d'identité ---------------------------------------------------
    def _info(self, endpoint, *, key=None, key_id=None, challenge=None,
              connect_timeout=_transport.CONNECT_TIMEOUT, timeout=INFO_TIMEOUT):
        """`GET /aiodos/v1/info` (avec un défi, ou avec la clé) : la réponse vérifiée.
        Lève NotAiodos, TooOld, Unreachable, Timeout, ou l'erreur d'AIODOS."""
        path = "/aiodos/v1/info"
        if key_id and challenge:
            path += "?" + urlencode({"key_id": key_id, "challenge": challenge})
        try:
            resp = _transport.request(endpoint, "GET", path, headers=self._headers(key),
                                      timeout=timeout, connect_timeout=connect_timeout,
                                      lang=self.lang, limit=INFO_LIMIT)
        except errors.Unreachable:
            self._forget_trust()
            raise
        except errors.ProtocolError:
            raise errors.make("not_aiodos", self.lang, host=endpoint.netloc) from None
        marked = resp.header("X-Aiodos-Api") is not None
        if resp.status == 401 and not marked:
            raise errors.make("too_old", self.lang)
        if resp.status != 200:
            if marked and resp.status >= 400:
                raise errors.from_response(resp.status, resp.headers, resp.body, self.lang)
            raise errors.make("not_aiodos", self.lang, host=endpoint.netloc)
        try:
            data = resp.json(self.lang)
        except errors.ProtocolError:
            raise errors.make("not_aiodos", self.lang, host=endpoint.netloc) from None
        if not isinstance(data, dict) or data.get("product") != "aiodos":
            raise errors.make("not_aiodos", self.lang, host=endpoint.netloc)
        api = data.get("api")
        if not isinstance(api, int) or isinstance(api, bool) or api < API_VERSION:
            raise errors.make("too_old", self.lang)
        with self._lock:
            trusted = self._trusted
            if (trusted is not None and trusted.endpoint == endpoint
                    and data.get("instance") != trusted.instance):
                self._trusted = None                # AIODOS a redémarré : preuve à refaire
        return data

    def _trust(self, endpoint, disc=None):
        """La clé et la dernière réponse d'info, après une preuve juste (refaite si besoin).
        Lève NeedsAccess (pas de clé) ou Untrusted (preuve fausse : la clé n'est pas envoyée)."""
        key = self._read_key()
        fingerprint = _proof.fingerprint(key)
        instance = disc.instance if disc is not None else None
        with self._lock:
            trusted = self._trusted
        if (trusted is not None and trusted.endpoint == endpoint
                and trusted.fingerprint == fingerprint
                and time.monotonic() - trusted.at <= PROOF_MAX_AGE
                and (instance is None or instance == trusted.instance)):
            return key, trusted.info
        challenge = _proof.challenge()
        info = self._info(endpoint, key_id=_proof.key_id(key), challenge=challenge)
        if not _proof.verify(key, challenge, info.get("proof")):
            self._forget_trust()
            log.warning("Preuve d'identité fausse : la clé n'est pas envoyée.")
            raise errors.make("untrusted", self.lang, host=endpoint.netloc)
        self._remember_trust(endpoint, key, info)
        return key, info

    def _remember_trust(self, endpoint, key, info):
        with self._lock:
            self._trusted = _Trust(endpoint, info.get("instance"), _proof.fingerprint(key),
                                   time.monotonic(), info)

    def _forget_trust(self):
        with self._lock:
            self._trusted = None

    # --- Fonctions annoncées (features) ---------------------------------------------
    def _require(self, info, feature):
        """NotSupported, sans rien essayer, si AIODOS n'annonce pas cette fonction."""
        features = info.get("features") if isinstance(info, dict) else None
        if isinstance(features, list) and feature not in features:
            raise errors.make("not_supported", self.lang, feature=feature,
                              detail={"feature": feature})

    def _serves(self, feature):
        """AIODOS annonce-t-il `feature` ? (None : on ne sait pas) ; ne lève jamais."""
        with self._lock:
            trusted = self._trusted
        info = trusted.info if (trusted is not None
                                and time.monotonic() - trusted.at <= PROOF_MAX_AGE) else None
        if info is None:
            try:
                endpoint, _ = self._target()
                info = self._info(endpoint, timeout=5.0)
            except errors.AiodosError:
                return None
        features = info.get("features")
        return feature in features if isinstance(features, list) else None

    def _refuse_unserved(self, feature):
        """Les fonctions de la livraison 2 : NotSupported, sans rien essayer. Si AIODOS les
        annonce déjà, c'est ce client (0.1.0) qui est à mettre à jour."""
        if self._serves(feature):
            raise errors.make("not_supported", self.lang,
                              message=texts.error("client_too_old", self.lang, feature=feature,
                                                  version=__version__),
                              hint=texts.hint("client_too_old", self.lang),
                              detail={"feature": feature, "client": __version__})
        raise errors.make("not_supported", self.lang, feature=feature,
                          detail={"feature": feature})

    # --- Appels -----------------------------------------------------------------------
    def _check(self, status, headers, body, endpoint=None):
        if 200 <= status < 300:
            return
        if 300 <= status < 400:                     # AIODOS ne redirige jamais
            raise errors.make("protocol", self.lang, detail={"status": status},
                              message=texts.error("redirect", self.lang,
                                                  host=endpoint.netloc if endpoint else "?"),
                              hint=texts.hint("redirect", self.lang))
        exc = errors.from_response(status, headers, body, self.lang)
        if isinstance(exc, (errors.NeedsAccess, errors.Revoked)):
            self._forget_trust()
        raise exc

    def _call(self, method, path, *, body=None, feature=None, headers=None, timeout=None,
              cancel=None):
        """Une requête avec la clé (après la preuve) : la réponse JSON, ou l'erreur."""
        endpoint, disc = self._target()
        key, info = self._trust(endpoint, disc)
        if feature:
            self._require(info, feature)
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            resp = _transport.request(endpoint, method, path,
                                      headers=self._headers(key, json_body=body is not None,
                                                            extra=headers),
                                      body=payload, timeout=timeout or self.timeout,
                                      cancel=cancel, lang=self.lang)
        except errors.Unreachable:
            self._forget_trust()
            raise
        self._check(resp.status, resp.headers, resp.body, endpoint)
        return resp.json(self.lang)

    def _stream(self, path, body, *, headers=None, first_timeout=None, timeout=None,
                cancel=None, feature=None):
        """Une requête avec la clé dont la réponse se lit en flux : rend l'échange ouvert
        (Exchange, à fermer), sa réponse déjà vérifiée."""
        endpoint, disc = self._target()
        key, info = self._trust(endpoint, disc)
        if feature:
            self._require(info, feature)
        idle = float(timeout or self.timeout)
        exchange = _transport.Exchange(endpoint, timeout=idle, first_timeout=first_timeout or idle,
                                       cancel=cancel, lang=self.lang)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            resp = exchange.send("POST", path,
                                 self._headers(key, json_body=True, accept="text/event-stream",
                                               extra=headers), payload)
            if not 200 <= resp.status < 300:
                self._check(resp.status, resp.headers, exchange.read_body(ERROR_LIMIT),
                            endpoint)
        except errors.Unreachable:
            exchange.close()
            self._forget_trust()
            raise
        except BaseException:
            exchange.close()
            raise
        return exchange

    # --- Ce que le client retient ---------------------------------------------------
    def _remember_models(self, kind, models):
        with self._lock:
            self._models[kind] = (time.monotonic(), models)

    def _known_model(self, model_id, kind="chat"):
        """Le modèle tel que le dit le dernier catalogue (s'il est récent), ou None."""
        with self._lock:
            entry = self._models.get(kind)
        if entry is None or time.monotonic() - entry[0] > MODELS_MAX_AGE:
            return None
        return entry[1].by_id.get(model_id)

    def _active_request(self):
        with self._lock:
            request = self._pending
            if request is not None and not request.active:
                self._pending = request = None
        return request

    def _set_pending(self, request):
        with self._lock:
            self._pending = request
            self._denied = False

    def _clear_pending(self, request):
        with self._lock:
            if self._pending is request:
                self._pending = None

    def _was_denied(self):
        with self._lock:
            return self._denied

    def _set_denied(self, value):
        with self._lock:
            self._denied = bool(value)
