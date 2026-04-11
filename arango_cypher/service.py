"""FastAPI HTTP service for arango-cypher-py.

Provides REST endpoints for Cypher translation, execution, and connection
management. Serves as the backend for the Cypher Workbench UI (§4.4).

Usage::

    uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Any

from arango import ArangoClient
from arango.database import StandardDatabase
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from arango_query_core import (
    CoreError,
    ExtensionPolicy,
    ExtensionRegistry,
    MappingBundle,
    MappingSource,
)

from .api import TranspiledQuery, execute, get_cypher_profile, translate, validate_cypher_profile
from .extensions import register_all_extensions

app = FastAPI(
    title="Arango Cypher Transpiler",
    description="Cypher → AQL translation service for ArangoDB",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))

_sessions: dict[str, _Session] = {}


class _Session:
    __slots__ = ("token", "db", "client", "created_at", "last_used")

    def __init__(self, token: str, db: StandardDatabase, client: ArangoClient):
        self.token = token
        self.db = db
        self.client = client
        self.created_at = time.time()
        self.last_used = time.time()

    def touch(self) -> None:
        self.last_used = time.time()

    @property
    def expired(self) -> bool:
        return (time.time() - self.last_used) > SESSION_TTL_SECONDS


def _prune_expired() -> None:
    expired = [k for k, v in _sessions.items() if v.expired]
    for k in expired:
        s = _sessions.pop(k, None)
        if s:
            s.client.close()


def _get_session(request: Request) -> _Session:
    _prune_expired()
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth[7:]
    session = _sessions.get(token)
    if session is None or session.expired:
        if session and session.expired:
            _sessions.pop(token, None)
            session.client.close()
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    session.touch()
    return session


def _build_registry() -> ExtensionRegistry:
    reg = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_all_extensions(reg)
    return reg


_default_registry = _build_registry()


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    host: str = Field(default="localhost")
    port: int = Field(default=8529)
    database: str = Field(default="_system")
    username: str = Field(default="root")
    password: str = Field(default="")
    protocol: str = Field(default="http")


class ConnectResponse(BaseModel):
    token: str
    databases: list[str]


class TranslateRequest(BaseModel):
    cypher: str
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    extensions_enabled: bool = True


class TranslateResponse(BaseModel):
    aql: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]]


class ExecuteRequest(BaseModel):
    cypher: str
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    extensions_enabled: bool = True


class ExecuteResponse(BaseModel):
    results: list[Any]
    aql: str
    bind_vars: dict[str, Any]


class ValidateRequest(BaseModel):
    cypher: str
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class ValidateResponse(BaseModel):
    ok: bool
    errors: list[dict[str, str]]


class ErrorResponse(BaseModel):
    error: str
    code: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mapping_from_dict(d: dict[str, Any] | None) -> MappingBundle | None:
    if d is None:
        return None
    return MappingBundle(
        conceptual_schema=d.get("conceptual_schema") or d.get("conceptualSchema") or {},
        physical_mapping=d.get("physical_mapping") or d.get("physicalMapping") or {},
        metadata=d.get("metadata", {}),
        source=MappingSource(kind="explicit", notes="supplied via HTTP"),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/connect", response_model=ConnectResponse)
def connect(req: ConnectRequest):
    """Authenticate to ArangoDB; returns a session token."""
    try:
        url = f"{req.protocol}://{req.host}:{req.port}"
        client = ArangoClient(hosts=url)
        db = client.db(req.database, username=req.username, password=req.password)
        db.version()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {e}")

    token = secrets.token_urlsafe(32)
    _sessions[token] = _Session(token=token, db=db, client=client)

    try:
        databases = [d for d in client.db("_system", username=req.username, password=req.password).databases()]
    except Exception:
        databases = [req.database]

    return ConnectResponse(token=token, databases=databases)


@app.post("/disconnect")
def disconnect(session: _Session = Depends(_get_session)):
    """Tear down session and release the python-arango client."""
    _sessions.pop(session.token, None)
    session.client.close()
    return {"status": "disconnected"}


@app.get("/connections")
def list_connections():
    """List active sessions (admin/debug)."""
    _prune_expired()
    return {
        "active": len(_sessions),
        "sessions": [
            {
                "token_prefix": s.token[:8] + "...",
                "created_at": s.created_at,
                "last_used": s.last_used,
                "expired": s.expired,
            }
            for s in _sessions.values()
        ],
    }


@app.get("/connect/defaults")
def connect_defaults():
    """Return .env default values for pre-filling the connection dialog.

    Never returns the password.
    """
    return {
        "host": os.getenv("ARANGO_HOST", "localhost"),
        "port": int(os.getenv("ARANGO_PORT", "8529")),
        "database": os.getenv("ARANGO_DB", "_system"),
        "username": os.getenv("ARANGO_USER", "root"),
        "protocol": os.getenv("ARANGO_PROTOCOL", "http"),
    }


@app.get("/cypher-profile")
def cypher_profile():
    """Return the Arango Cypher profile manifest."""
    return get_cypher_profile()


@app.post("/translate", response_model=TranslateResponse)
def translate_endpoint(req: TranslateRequest):
    """Translate Cypher to AQL."""
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")

    registry = _default_registry if req.extensions_enabled else None
    try:
        result = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
    except CoreError as e:
        raise HTTPException(status_code=422, detail={"error": str(e), "code": e.code})

    return TranslateResponse(
        aql=result.aql,
        bind_vars=result.bind_vars,
        warnings=result.warnings,
    )


@app.post("/execute", response_model=ExecuteResponse)
def execute_endpoint(req: ExecuteRequest, session: _Session = Depends(_get_session)):
    """Translate Cypher to AQL and execute against the connected ArangoDB."""
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")

    registry = _default_registry if req.extensions_enabled else None
    try:
        transpiled = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
    except CoreError as e:
        raise HTTPException(status_code=422, detail={"error": str(e), "code": e.code})

    try:
        cursor = session.db.aql.execute(transpiled.aql, bind_vars=transpiled.bind_vars)
        results = list(cursor)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AQL execution failed: {e}")

    return ExecuteResponse(
        results=results,
        aql=transpiled.aql,
        bind_vars=transpiled.bind_vars,
    )


@app.post("/validate", response_model=ValidateResponse)
def validate_endpoint(req: ValidateRequest):
    """Validate Cypher against the translator profile."""
    mapping = _mapping_from_dict(req.mapping)
    result = validate_cypher_profile(
        req.cypher,
        mapping=mapping,
        params=req.params,
    )
    return ValidateResponse(
        ok=result.ok,
        errors=[dict(e) for e in result.errors],
    )


@app.post("/explain")
def explain_endpoint(req: TranslateRequest, session: _Session = Depends(_get_session)):
    """Translate Cypher to AQL, then run AQL EXPLAIN to get the execution plan."""
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")

    registry = _default_registry if req.extensions_enabled else None
    try:
        transpiled = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
    except CoreError as e:
        raise HTTPException(status_code=422, detail={"error": str(e), "code": e.code})

    try:
        plan = session.db.aql.explain(transpiled.aql, bind_vars=transpiled.bind_vars)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AQL EXPLAIN failed: {e}")

    return {
        "aql": transpiled.aql,
        "bind_vars": transpiled.bind_vars,
        "plan": plan,
    }


@app.post("/aql-profile")
def aql_profile_endpoint(req: TranslateRequest, session: _Session = Depends(_get_session)):
    """Translate Cypher to AQL, execute with profiling, return runtime stats + results."""
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")

    registry = _default_registry if req.extensions_enabled else None
    try:
        transpiled = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
    except CoreError as e:
        raise HTTPException(status_code=422, detail={"error": str(e), "code": e.code})

    try:
        cursor = session.db.aql.execute(
            transpiled.aql,
            bind_vars=transpiled.bind_vars,
            profile=True,
        )
        results = list(cursor)
        stats = cursor.statistics()
        profile_data = cursor.profile() if hasattr(cursor, "profile") else None
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AQL profiled execution failed: {e}")

    return {
        "aql": transpiled.aql,
        "bind_vars": transpiled.bind_vars,
        "results": results,
        "statistics": stats,
        "profile": profile_data,
    }


# ---------------------------------------------------------------------------
# Static file serving for the Cypher Workbench UI
# ---------------------------------------------------------------------------

_UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "dist"

if _UI_DIR.is_dir():

    @app.get("/ui/{full_path:path}")
    async def _spa_fallback(full_path: str):
        """Serve index.html for any UI route that is not a static asset."""
        file = _UI_DIR / full_path
        if file.is_file():
            return FileResponse(file)
        return FileResponse(_UI_DIR / "index.html")

    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
