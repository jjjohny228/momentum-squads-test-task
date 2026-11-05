from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_sessionmaker
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.tracing import configure_tracing, flush_traces
from app.health.router import router as health_router
from app.knowledge.router import router as kb_router
from app.tickets.router import router as tickets_router
from app.tickets.webhook import router as webhook_router
from app.ui.router import router as ui_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    configure_tracing(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        yield
        flush_traces()
        await engine.dispose()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(webhook_router)
    app.include_router(tickets_router)
    app.include_router(kb_router)
    app.include_router(ui_router)
    return app


app = create_app()
