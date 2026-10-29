"""Application FastAPI du nœud : routes /api, flux d'événements, interface web."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from aiodos import instance, paths, render
from aiodos.api.events import EventBus
from aiodos.api.security import Guard
from aiodos.api.routes import (attachments, autostart, chat, cluster, comfy_creations,
                               conversations, creations, engines, gateway, gateway_apps, i18n,
                               images, jobs, knowledge, legal, model_install, network, node,
                               system, updates)
from aiodos.api.routes import lent                       # le travail fait pour d'autres
from aiodos.api.routes import model_menu                 # le menu des modèles
from aiodos.chat import attachments as chat_attachments
from aiodos.engines import comfy_instance
from aiodos.gateway import access_flow, apps, retention
from aiodos.gateway.server import Gateway
from aiodos.i18n.middleware import Language
from aiodos.imaging import engine as images_engine
from aiodos.jobs import kinds  # noqa: F401  (enregistre les types de travaux)
from aiodos.jobs.executor import Executor
from aiodos.jobs.kinds import reindex
from aiodos.api.routes import library_folders           # dossiers suivis, modèle d'embeddings
from aiodos.knowledge import embed_model, folder_watch
from aiodos.network import lent_history
from aiodos.network.manager import NetworkManager
from aiodos.node import identity, keep_awake
from aiodos.updates.service import UpdateService


# Types des fichiers de l'interface, sans demander au registre de Windows : il dit parfois
# text/plain pour .js ou .css (un éditeur qui s'y est inscrit), et les modules, comme les
# feuilles de style sous X-Content-Type-Options: nosniff (api/security.py), seraient refusés
TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".html": "text/html; charset=utf-8"}


class Revalidated(StaticFiles):
    """Fichiers de l'interface revalidés à chaque chargement (réponse 304 s'ils n'ont pas
    changé) : après une mise à jour, un navigateur ouvert sur le même port (téléphone,
    nœud sans fenêtre) ne garde pas l'ancien code de l'interface en cache."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        served = getattr(response, "path", None) or path       # index.html pour « / »
        kind = TYPES.get(Path(str(served)).suffix.lower())
        if kind and "content-type" in response.headers:
            response.headers["Content-Type"] = kind
        return response


def create_app(token: str, hosts: set, network_options: dict | None = None,
               slots: dict | None = None, windowed: bool = False,
               show_key: str | None = None) -> FastAPI:
    """show_key : clé qui permet à AIODOS relancé de faire revenir la fenêtre de cette
    instance (POST /instance/show, cf. instance.wake_running) ; None : aucune."""
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app):
        bus.bind(asyncio.get_running_loop())
        # Un calcul pour d'autres commence ou finit : la vue Tâches, « Pour d'autres », suit
        lent_history.listen(bus.publish)
        node_id = await asyncio.to_thread(identity.node_id)
        executor = Executor(node_id, bus.publish, slots)
        app.state.executor = executor
        app.state.network = NetworkManager(node_id, bus, network_options, executor)
        await executor.start()
        # Modèle d'embedding changé (réglage, mise à jour) ou réindexation à reprendre
        if await asyncio.to_thread(reindex.schedule, resume=True):
            executor.wake()
        # Modèle d'embeddings absent, revenu ou pris d'office : la vue Tâches et les
        # bibliothèques le disent, la file repart (knowledge/embed_model.py)
        unlisten = embed_model.on_change(lambda: (bus.publish({"type": "jobs_reset"}),
                                                  bus.publish({"type": "knowledge"}),
                                                  executor.wake()))
        # Dossiers des bibliothèques suivis : relus peu après l'ouverture, puis régulièrement
        watching = asyncio.create_task(folder_watch.loop(executor, bus),
                                       name="aiodos-suivi-dossiers")
        await app.state.network.apply()          # ne démarre que si le réseau est activé
        app.state.updates = UpdateService(bus, executor, app.state.network, windowed)
        app.state.updates.start()
        app.state.gateway = Gateway(app.state)    # applis et autres outils : si ouvert
        access_flow.configure(approval_ui=windowed, on_new=instance.show_window,
                              on_open=app.state.gateway.apply_from_thread, publish=bus.publish)
        await asyncio.to_thread(apps.purge_session_keys)   # « jusqu'à la fermeture »
        await app.state.gateway.apply()
        keeping = asyncio.create_task(retention.loop(), name="aiodos-garde-fichiers")
        tidying = asyncio.create_task(chat_attachments.loop(), name="aiodos-pieces-jointes")
        keep_awake.start()                     # pas de veille pendant un travail d'appli
        yield
        keep_awake.stop()
        watching.cancel()
        unlisten()
        keeping.cancel()
        tidying.cancel()
        await asyncio.to_thread(comfy_instance.stop_instance)   # le ComfyUI lancé par AIODOS
        await app.state.gateway.shutdown()
        app.state.updates.stop()
        await app.state.network.shutdown()
        await executor.stop()
        await asyncio.to_thread(images_engine.stop)      # sd-server : mémoire rendue
        lent_history.listen(None)

    app = FastAPI(title="AIODOS", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bus = bus
    app.state.show_key = show_key
    for module in (attachments, autostart, chat, cluster, comfy_creations, conversations,
                   creations, engines, gateway, gateway_apps, i18n, images, knowledge, jobs,
                   legal, model_install, network, node, system, updates):
        app.include_router(module.router)
    app.include_router(system.window_router)
    app.include_router(library_folders.router)
    app.include_router(lent.router)
    app.include_router(model_menu.router)

    css = render.pygments_css()

    @app.get("/pygments.css", include_in_schema=False)
    def pygments_css():
        return Response(css, media_type="text/css")

    @app.get("/logo.png", include_in_schema=False)
    def logo():
        # Revalidé à chaque fois (réponse 304 s'il n'a pas changé) : après le
        # renommage, la fenêtre gardait l'ancien logo en cache
        return FileResponse(paths.LOGO_PNG, headers={"Cache-Control": "no-cache"})

    app.mount("/", Revalidated(directory=paths.STATIC_DIR, html=True), name="ui")
    app.add_middleware(Language)              # les textes d'une requête, dans sa langue
    return Guard(app, token, hosts)
