"""FastAPI entry point."""

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.features.db_migration import router as db_migration
from app.wiring import discover_source


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version="0.1.0")
    # Modules do not import each other; this is where they are connected.
    app.state.discover_source = discover_source

    @app.get("/health", tags=["system"])
    def health() -> dict:
        """Process liveness only. Does not check cloud, DB, or Kafka connectivity."""
        return {"status": "ok", "scope": "process"}

    # Register feature routers here once they have real implementations.
    app.include_router(db_migration.router)
    db_migration.add_exception_handlers(app)

    from app.features.assessment.router import router as assessment_router
    from app.features.finops.router import router as finops_router
    from app.features.security.router import router as security_router
    from app.features.workflows.live import router as live_router
    from app.features.workflows.router import router as workflows_router

    app.include_router(assessment_router)
    app.include_router(workflows_router)
    app.include_router(live_router)
    app.include_router(finops_router)
    app.include_router(security_router)
    _wire_security_topology()
    return app


def _wire_security_topology() -> None:
    """security 가 workflows 를 import 하지 않도록, run 토폴로지를 돌려주는 함수를 여기서 넣어 줍니다."""
    from app.features.security.router import get_security_service
    from app.features.workflows.router import get_engine
    from app.features.workflows.store import RunNotFound
    from app.features.workflows.topology import build_topology

    def run_topology(run_id: str) -> dict | None:
        try:
            return build_topology(get_engine().get(run_id))
        except RunNotFound:
            return None

    get_security_service().set_topology_provider(run_topology)


app = create_app()