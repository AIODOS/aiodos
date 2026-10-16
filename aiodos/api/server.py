"""Application FastAPI du nœud : routes /api, flux d'événements, interface web."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from aiodos import paths, render
from aiodos.api.events import EventBus
from aiodos.api.security import Guard
from aiodos.api.routes import chat, conversations, jobs, knowledge, network, node, system
from aiodos.jobs import kinds  # noqa: F401  (enregistre les types de travaux)
from aiodos.jobs.executor import Executor
from aiodos.jobs.kinds import reindex
from aiodos.network.manager import NetworkManager
from aiodos.node import identity


def create_app(token: str, hosts: set, network_options: dict | None = None,
               slots: dict | None = None) -> FastAPI:
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
        yield
        await app.state.network.shutdown()
        await executor.stop()

    app = FastAPI(title="Aiodos", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bus = bus
    for module in (chat, conversations, knowledge, jobs, network, node, system):
        app.include_router(module.router)

    css = render.pygments_css()

    @app.get("/pygments.css", include_in_schema=False)
    def pygments_css():
        return Response(css, media_type="text/css")

    @app.get("/logo.png", include_in_schema=False)
    def logo():
        # Revalidé à chaque fois (réponse 304 s'il n'a pas changé) : après le
        # renommage, la fenêtre gardait l'ancien logo en cache
        return FileResponse(paths.LOGO_PNG, headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=paths.STATIC_DIR, html=True), name="ui")
    return Guard(app, token, hosts)
