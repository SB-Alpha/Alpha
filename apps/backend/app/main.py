"""FastAPI entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.local_access import ensure_api_token
from app.core.logging import configure_logging
from app.features.db_migration import router as db_migration


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    ensure_api_token()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=_lifespan)

    @app.get("/health", tags=["system"])
    def health() -> dict:
        """Process liveness only. Does not check cloud, DB, or Kafka connectivity."""
        return {"status": "ok", "scope": "process"}

    # Register feature routers here once they have real implementations.
    app.include_router(db_migration.router)
    db_migration.add_exception_handlers(app)

    from app.features.assessment.router import router as assessment_router
    from app.features.workflows.router import router as workflows_router

    app.include_router(assessment_router)
    app.include_router(workflows_router)
    return app


app = create_app()