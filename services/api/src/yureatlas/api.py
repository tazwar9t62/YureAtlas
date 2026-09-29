from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import settings
from .db import engine

app = FastAPI(title="YureAtlas", version="0.1.0")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "api", "testserver"])


@app.middleware("http")
async def local_requests(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins.split(","):
            return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site mutation rejected"}, status_code=403)
        if request.headers.get("x-yureatlas-request") != "1":
            return JSONResponse({"detail": "X-YureAtlas-Request: 1 is required"}, status_code=403)
    return await call_next(request)


class Health(BaseModel):
    status: str
    service: str = "YureAtlas"


@app.get("/health", response_model=Health)
def health():
    return Health(status="ok")


@app.get("/ready", response_model=Health)
def ready():
    with engine.connect() as conn:
        conn.execute(text("SELECT version_num FROM alembic_version"))
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    return Health(status="ready")
