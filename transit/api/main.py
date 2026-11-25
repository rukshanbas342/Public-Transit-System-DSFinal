"""FastAPI app: uvicorn transit.api.main:app

Routers are mounted under /api, GET /api/health is for container health checks, and the built
React dashboard (env DASHBOARD_DIST, default dashboard/dist) is served at /.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from transit.api import auth
from transit.api.deps import get_session
from transit.api.routes import metrics, network, recommendations
from transit.common.logging_setup import setup_logging

setup_logging("api")

app = FastAPI(title="Transit operator API", version="1.0")
for module in (auth, network, metrics, recommendations):
    app.include_router(module.router, prefix="/api")


@app.get("/api/health", tags=["health"])
def health() -> JSONResponse:
    try:
        session = next(get_session())
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 - report any DB failure as unhealthy
        return JSONResponse({"status": "error", "database": str(exc)}, status_code=503)
    return JSONResponse({"status": "ok"})


DIST = Path(os.environ.get("DASHBOARD_DIST", Path(__file__).resolve().parents[2] / "dashboard" / "dist"))

if (DIST / "index.html").exists():
    if (DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def dashboard(path: str):
        target = (DIST / path).resolve()
        if path and target.is_file() and DIST.resolve() in target.parents:
            return FileResponse(target)
        return FileResponse(DIST / "index.html")
else:
    @app.get("/", include_in_schema=False)
    def no_dashboard() -> JSONResponse:
        return JSONResponse({"message": "API is running; the dashboard isn't built "
                                        "(cd dashboard && npm install && npm run build). See /docs."})
