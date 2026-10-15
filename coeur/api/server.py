"""Application FastAPI du nœud : routes /api, flux d'événements, interface web."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from coeur import paths, render
from coeur.api.events import EventBus
from coeur.api.security import Guard
from coeur.api.routes import chat, conversations, jobs, knowledge, node, system
from coeur.jobs import kinds  # noqa: F401  (enregistre les types de travaux)
from coeur.jobs.executor import Executor
from coeur.node import identity


def create_app(token: str, hosts: set) -> FastAPI:
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app):
        bus.bind(asyncio.get_running_loop())
        executor = Executor(await asyncio.to_thread(identity.node_id), bus.publish)
        app.state.executor = executor
        await executor.start()
        yield
        await executor.stop()

    app = FastAPI(title="Cœur IA", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bus = bus
    for module in (chat, conversations, knowledge, jobs, node, system):
        app.include_router(module.router)

    css = render.pygments_css()

    @app.get("/pygments.css", include_in_schema=False)
    def pygments_css():
        return Response(css, media_type="text/css")

    @app.get("/logo.png", include_in_schema=False)
    def logo():
        return FileResponse(paths.LOGO_PNG)

    app.mount("/", StaticFiles(directory=paths.STATIC_DIR, html=True), name="ui")
    return Guard(app, token, hosts)
