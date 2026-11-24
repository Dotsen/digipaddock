"""The cabinet's HTTP application (FastAPI): ``/api/...`` and, in dev, the built PWA with an SPA fallback.

In production nginx serves the PWA's files and passes ``/api`` and ``/ws`` here.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from kd.cabinet.web import (
    agents,
    auth,
    cards,
    decisions,
    deploys,
    errors,
    feed,
    files,
    hindsight,
    llm,
    monitor_settings,
    overview,
    servers,
    settings,
    stepup,
    ws,
)

DIST = Path(__file__).resolve().parents[3] / "cabinet" / "web" / "dist"


def create_app(svc, *, dist: Path = DIST) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.svc = svc
    errors.install(app)

    @app.get("/api/health", include_in_schema=False)
    async def health():
        """For cabinet_setup.sh check: the web answers (no session, nothing about the board)."""
        return {"ok": True}

    for module in (auth, stepup, settings, cards, decisions, files, feed, agents, hindsight, llm, overview,
                   monitor_settings, servers, deploys, ws):
        app.include_router(module.router)
    if svc.cfg.dev:
        from kd.cabinet.web import dev  # an agent's write for e2e; never in production

        app.include_router(dev.router)
        _static(app, Path(dist))
    return app


def _static(app: FastAPI, dist: Path):
    """The built frontend: a file that exists, else index.html (the client-side router); never above dist."""
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/") or path == "api":
            return JSONResponse(
                errors.body("not_found", "no such API"), status_code=404
            )
        target = (root / path).resolve()
        if path and target.is_file() and root in target.parents:
            return FileResponse(target)
        index = root / "index.html"
        if not index.is_file():
            return JSONResponse(
                errors.body("not_found", "the frontend is not built"), status_code=404
            )
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
