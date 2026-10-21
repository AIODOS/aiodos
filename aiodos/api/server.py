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
from aiodos.api.routes import (autostart, chat, cluster, conversations, creations, gateway,
                               gateway_apps, i18n, images, jobs, knowledge, model_install,
                               network, node, system, updates)
from aiodos.gateway import access_flow, apps
from aiodos.gateway.server import Gateway
from aiodos.i18n.middleware import Language
from aiodos.imaging import engine as images_engine
from aiodos.jobs import kinds  # noqa: F401  (enregistre les types de travaux)
from aiodos.jobs.executor import Executor
from aiodos.jobs.kinds import reindex
from aiodos.network.manager import NetworkManager
from aiodos.node import identity
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
        node_id = await asyncio.to_thread(identity.node_id)
        executor = Executor(node_id, bus.publish, slots)
        app.state.executor = executor
        app.state.network = NetworkManager(node_id, bus, network_options, executor)
        await executor.start()
        # Modèle d'embedding changé (réglage, mise à jour) ou réindexation à reprendre
        if await asyncio.to_thread(reindex.schedule, resume=True):
            executor.wake()
        await app.state.network.apply()          # ne démarre que si le réseau est activé
        app.state.updates = UpdateService(bus, executor, app.state.network, windowed)
        app.state.updates.start()
        app.state.gateway = Gateway(app.state)    # applis et autres outils : si ouvert
        access_flow.configure(approval_ui=windowed, on_new=instance.show_window,
                              on_open=app.state.gateway.apply_from_thread, publish=bus.publish)
        await asyncio.to_thread(apps.purge_session_keys)   # « jusqu'à la fermeture »
        await app.state.gateway.apply()
        yield
        await app.state.gateway.shutdown()
        app.state.updates.stop()
        await app.state.network.shutdown()
        await executor.stop()
        await asyncio.to_thread(images_engine.stop)      # sd-server : mémoire rendue

    app = FastAPI(title="AIODOS", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bus = bus
    app.state.show_key = show_key
    for module in (autostart, chat, cluster, conversations, creations, gateway, gateway_apps,
                   i18n, images, knowledge, jobs, model_install, network, node, system,
                   updates):
        app.include_router(module.router)
    app.include_router(system.window_router)

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
