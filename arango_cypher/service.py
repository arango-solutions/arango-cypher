"""FastAPI HTTP service for arango-cypher-py.

Provides REST endpoints for Cypher translation, execution, and connection
management. Serves as the backend for the Cypher Workbench UI (§4.4).

Usage::

    uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
    MappingResolver,
    MappingSource,
)

from .api import TranspiledQuery, execute, get_cypher_profile, translate, validate_cypher_profile
from .extensions import register_all_extensions

app = FastAPI(
    title="Arango Cypher Transpiler",
    description="Cypher → AQL translation service for ArangoDB",
    version="0.1.0",
)

_cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "*")
_cors_origins = (
    ["*"]
    if _cors_origins_raw.strip() == "*"
    else [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import logging as _logging
_svc_logger = _logging.getLogger("arango_cypher.service")

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    _svc_logger.warning(
        "Pydantic 422 on %s %s: %s | body[:200]=%s",
        request.method, request.url.path, exc.errors(), body[:200],
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

NL_RATE_LIMIT_PER_MINUTE = int(os.getenv("NL_RATE_LIMIT_PER_MINUTE", "10"))

SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "100"))

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


def _evict_lru() -> None:
    """If session count exceeds MAX_SESSIONS, evict least-recently-used."""
    _prune_expired()
    while len(_sessions) >= MAX_SESSIONS:
        oldest_key = min(_sessions, key=lambda k: _sessions[k].last_used)
        s = _sessions.pop(oldest_key, None)
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


class _TokenBucket:
    """Simple per-key in-memory token bucket for rate limiting."""

    __slots__ = ("_capacity", "_tokens", "_last_refill")

    def __init__(self, capacity: int):
        self._capacity = capacity
        self._tokens: dict[str, float] = {}
        self._last_refill: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        last = self._last_refill.get(key, now)
        elapsed = now - last
        current = self._tokens.get(key, float(self._capacity))
        current = min(self._capacity, current + elapsed * (self._capacity / 60.0))
        self._last_refill[key] = now
        if current >= 1.0:
            self._tokens[key] = current - 1.0
            return True
        self._tokens[key] = current
        return False


_nl_bucket = _TokenBucket(NL_RATE_LIMIT_PER_MINUTE)


def _check_nl_rate_limit(request: Request) -> None:
    session_key = request.headers.get("Authorization", request.client.host if request.client else "anon")
    if not _nl_bucket.allow(session_key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded for NL endpoints")


def _build_registry() -> ExtensionRegistry:
    reg = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_all_extensions(reg)
    return reg


_default_registry = _build_registry()


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    url: str = Field(default="http://localhost:8529")
    database: str = Field(default="_system")
    username: str = Field(default="root")
    password: str = Field(default="")


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
    elapsed_ms: float | None = None


class ExecuteRequest(BaseModel):
    cypher: str
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    extensions_enabled: bool = True


class ExecuteResponse(BaseModel):
    results: list[Any]
    aql: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]] = []
    exec_ms: float | None = None


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

_URL_RE = re.compile(r"https?://[^\s,;'\")\]}>]+", re.IGNORECASE)
_HOST_PORT_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_CRED_RE = re.compile(
    r"(?:password|passwd|token|secret|api[_-]?key|authorization)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def _sanitize_error(msg: str) -> str:
    """Strip URLs, IP addresses, and credential-like patterns from an error message."""
    msg = _URL_RE.sub("<redacted-url>", msg)
    msg = _HOST_PORT_RE.sub("<redacted-host>", msg)
    msg = _CRED_RE.sub("<redacted-credential>", msg)
    return msg


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
        url = req.url.rstrip("/")
        client = ArangoClient(hosts=url)
        db = client.db(req.database, username=req.username, password=req.password)
        db.version()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {_sanitize_error(str(e))}")

    _evict_lru()
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


_PUBLIC_MODE = os.getenv("ARANGO_CYPHER_PUBLIC_MODE", "").lower() in ("true", "1", "yes")


@app.get("/connect/defaults")
def connect_defaults():
    """Return .env default values for pre-filling the connection dialog.

    Uses ARANGO_URL directly if set, otherwise builds from
    ARANGO_HOST/ARANGO_PORT/ARANGO_PROTOCOL.
    Disabled when ARANGO_CYPHER_PUBLIC_MODE=true.
    """
    if _PUBLIC_MODE:
        raise HTTPException(status_code=404, detail="Not available in public mode")

    arango_url = os.getenv("ARANGO_URL", "")
    if not arango_url:
        host = os.getenv("ARANGO_HOST", "localhost")
        port = os.getenv("ARANGO_PORT", "8529")
        protocol = os.getenv("ARANGO_PROTOCOL", "http")
        arango_url = f"{protocol}://{host}:{port}"

    return {
        "url": arango_url.rstrip("/"),
        "database": os.getenv("ARANGO_DB", "_system"),
        "username": os.getenv("ARANGO_USER", "root"),
        "password": os.getenv("ARANGO_PASS", ""),
    }


@app.get("/cypher-profile")
def cypher_profile():
    """Return the Arango Cypher profile manifest."""
    return get_cypher_profile()


@app.post("/translate", response_model=TranslateResponse)
def translate_endpoint(req: TranslateRequest):
    """Translate Cypher to AQL."""
    import logging as _log
    _log.getLogger("arango_cypher.service").info(
        "translate request: cypher=%r, mapping_keys=%s",
        req.cypher[:80] if req.cypher else "(empty)",
        list(req.mapping.keys()) if req.mapping else "(none)",
    )
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")

    registry = _default_registry if req.extensions_enabled else None
    t0 = time.perf_counter()
    try:
        result = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
    except CoreError as e:
        _log.getLogger("arango_cypher.service").warning(
            "translate CoreError: %s (code=%s) for cypher=%r", e, e.code, req.cypher[:80],
        )
        raise HTTPException(status_code=422, detail={"error": _sanitize_error(str(e)), "code": e.code})
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    correction = _corrections.lookup(req.cypher, req.mapping)
    if correction:
        return TranslateResponse(
            aql=correction.corrected_aql,
            bind_vars=correction.bind_vars or result.bind_vars,
            warnings=[{"message": f"Using learned correction #{correction.id}"}] + list(result.warnings or []),
            elapsed_ms=elapsed_ms,
        )

    return TranslateResponse(
        aql=result.aql,
        bind_vars=result.bind_vars,
        warnings=result.warnings,
        elapsed_ms=elapsed_ms,
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
        raise HTTPException(status_code=422, detail={"error": _sanitize_error(str(e)), "code": e.code})

    correction = _corrections.lookup(req.cypher, req.mapping)
    run_aql = correction.corrected_aql if correction else transpiled.aql
    run_bind = (correction.bind_vars or transpiled.bind_vars) if correction else transpiled.bind_vars
    warnings = list(transpiled.warnings or [])
    if correction:
        warnings.insert(0, {"message": f"Using learned correction #{correction.id}"})

    try:
        t_exec = time.perf_counter()
        cursor = session.db.aql.execute(run_aql, bind_vars=run_bind)
        results = list(cursor)
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AQL execution failed: {_sanitize_error(str(e))}")

    return ExecuteResponse(
        results=results,
        aql=run_aql,
        bind_vars=run_bind,
        warnings=warnings,
        exec_ms=exec_ms,
    )


class ExecuteAqlRequest(BaseModel):
    aql: str
    bind_vars: dict[str, Any] = Field(default_factory=dict)


@app.post("/execute-aql")
def execute_aql_endpoint(req: ExecuteAqlRequest, session: _Session = Depends(_get_session)):
    """Execute a raw AQL query directly (used by NL→AQL direct path)."""
    try:
        t_exec = time.perf_counter()
        cursor = session.db.aql.execute(req.aql, bind_vars=req.bind_vars)
        results = list(cursor)
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AQL execution failed: {_sanitize_error(str(e))}")

    return ExecuteResponse(
        results=results,
        aql=req.aql,
        bind_vars=req.bind_vars,
        warnings=[],
        exec_ms=exec_ms,
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
        raise HTTPException(status_code=422, detail={"error": _sanitize_error(str(e)), "code": e.code})

    try:
        plan = session.db.aql.explain(transpiled.aql, bind_vars=transpiled.bind_vars)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AQL EXPLAIN failed: {_sanitize_error(str(e))}")

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
        raise HTTPException(status_code=422, detail={"error": _sanitize_error(str(e)), "code": e.code})

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
        raise HTTPException(status_code=500, detail=f"AQL profiled execution failed: {_sanitize_error(str(e))}")

    return {
        "aql": transpiled.aql,
        "bind_vars": transpiled.bind_vars,
        "results": results,
        "statistics": stats,
        "profile": profile_data,
    }


# ---------------------------------------------------------------------------
# Schema introspection endpoints
# ---------------------------------------------------------------------------


def _sample_properties(db: StandardDatabase, collection_name: str, sample_size: int = 100) -> dict[str, dict[str, Any]]:
    """Sample documents from a collection and infer property names and types."""
    try:
        cursor = db.aql.execute(
            "FOR doc IN @@col LIMIT @n RETURN doc",
            bind_vars={"@col": collection_name, "n": sample_size},
        )
        docs = list(cursor)
    except Exception:
        return {}

    if not docs:
        return {}

    field_types: dict[str, dict[str, int]] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if key.startswith("_"):
                continue
            if key not in field_types:
                field_types[key] = {}
            t = _infer_type(val)
            field_types[key][t] = field_types[key].get(t, 0) + 1

    result: dict[str, dict[str, Any]] = {}
    for name, types in field_types.items():
        dominant = max(types, key=types.get)  # type: ignore[arg-type]
        result[name] = {
            "field": name,
            "type": dominant,
            "required": len([d for d in docs if isinstance(d, dict) and name in d]) == len(docs),
        }
    return result


def _infer_type(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, int | float):
        return "number"
    if isinstance(val, str):
        return "string"
    if isinstance(val, list):
        return "array"
    if isinstance(val, dict):
        return "object"
    return "string"


def _infer_edge_endpoints(
    db: StandardDatabase, edge_collection: str, limit: int = 20,
) -> tuple[str | None, str | None]:
    """Sample _from/_to in an edge collection to determine which document collections it connects."""
    try:
        cursor = db.aql.execute(
            "FOR e IN @@col LIMIT @n RETURN { f: e._from, t: e._to }",
            bind_vars={"@col": edge_collection, "n": limit},
        )
        from_cols: set[str] = set()
        to_cols: set[str] = set()
        for doc in cursor:
            f, t = doc.get("f", ""), doc.get("t", "")
            if "/" in f:
                from_cols.add(f.split("/", 1)[0])
            if "/" in t:
                to_cols.add(t.split("/", 1)[0])
        domain = sorted(from_cols)[0] if len(from_cols) == 1 else None
        range_ = sorted(to_cols)[0] if len(to_cols) == 1 else None
        return domain, range_
    except Exception:
        return None, None


@app.get("/schema/introspect")
def schema_introspect(
    sample: int = 50,
    force: bool = False,
    session: _Session = Depends(_get_session),
):
    """Discover collections, edge collections, and their properties from the connected database.

    Delegates to ``get_mapping(db)`` which uses the 3-tier strategy:
    analyzer first (all schema types), heuristic fallback if the analyzer
    is not installed.

    Pass ``force=true`` to bypass the 5-minute mapping cache.
    """
    db = session.db
    from .schema_acquire import get_mapping as _get_mapping, _mapping_cache, _cache_key

    if force:
        key = _cache_key(db)
        _mapping_cache.pop(key, None)

    bundle = _get_mapping(db)

    resolver = MappingResolver(bundle)
    result = resolver.schema_summary()

    # Build collection→label lookup from entities
    col_to_label: dict[str, str] = {}
    for ent in result.get("entities", []):
        col_to_label[ent.get("collection", "")] = ent.get("label", "")

    # For PG schemas, fill in missing domain/range by sampling _from/_to
    for rel in result.get("relationships", []):
        if rel.get("domain") and rel.get("range"):
            continue
        edge_col = rel.get("edgeCollection", "")
        if not edge_col:
            continue
        from_col, to_col = _infer_edge_endpoints(db, edge_col)
        if from_col and not rel.get("domain"):
            rel["domain"] = col_to_label.get(from_col, from_col)
        if to_col and not rel.get("range"):
            rel["range"] = col_to_label.get(to_col, to_col)

    return result


@app.get("/schema/properties")
def schema_properties(
    collection: str,
    sample: int = 100,
    session: _Session = Depends(_get_session),
):
    """Infer properties for a specific collection by sampling documents."""
    props = _sample_properties(session.db, collection, sample)
    return {"collection": collection, "sample_size": sample, "properties": props}


@app.get("/schema/summary")
def schema_summary(req: TranslateRequest):
    """Return a structured summary of the mapping for the visual graph editor."""
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")
    resolver = MappingResolver(mapping)
    return resolver.schema_summary()


@app.get("/schema/statistics")
def schema_statistics(
    session: _Session = Depends(_get_session),
):
    """Compute and return cardinality statistics for the connected database.

    Returns collection counts, per-entity estimated counts, per-relationship
    fan-out/fan-in metrics, cardinality patterns, and selectivity ratios.
    """
    from .schema_acquire import compute_statistics as _compute_stats, get_mapping as _get_mapping

    t0 = time.perf_counter()
    bundle = _get_mapping(session.db)
    stats = _compute_stats(session.db, bundle)
    elapsed = round(time.perf_counter() - t0, 3)
    return {"statistics": stats, "elapsed_seconds": elapsed}


# ---------------------------------------------------------------------------
# Sample queries (query corpus files)
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


@app.get("/sample-queries")
def sample_queries(dataset: str | None = None):
    """Return sample Cypher queries from the query corpus files.

    Optionally filter by dataset name (e.g., 'movies', 'social').
    """
    import yaml

    corpora: list[dict[str, Any]] = []
    datasets_dir = _FIXTURES_DIR / "datasets"
    if not datasets_dir.is_dir():
        return {"queries": []}

    for corpus_file in sorted(datasets_dir.rglob("query-corpus.yml")):
        ds_name = corpus_file.parent.name
        if dataset and ds_name != dataset:
            continue
        try:
            entries = yaml.safe_load(corpus_file.read_text(encoding="utf-8")) or []
        except Exception:
            continue
        for entry in entries:
            if isinstance(entry, dict):
                entry["dataset"] = ds_name
                corpora.append(entry)

    return {"queries": corpora}


# ---------------------------------------------------------------------------
# Agentic tool endpoints
# ---------------------------------------------------------------------------


@app.get("/tools/schemas")
def tools_schemas():
    """Return OpenAI-compatible function schemas for all agentic tools."""
    from .tools import get_tool_schemas
    return {"tools": get_tool_schemas()}


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


@app.post("/tools/call")
def tools_call(req: ToolCallRequest):
    """Dispatch a tool call by name with arguments."""
    from .tools import call_tool
    return call_tool(req.name, req.arguments)


class SuggestIndexesRequest(BaseModel):
    mapping: dict[str, Any]


@app.post("/suggest-indexes")
def suggest_indexes(req: SuggestIndexesRequest):
    """Suggest indexes for the given mapping."""
    from .tools import suggest_indexes_tool
    return suggest_indexes_tool({"mapping": req.mapping})


# ---------------------------------------------------------------------------
# NL-to-Cypher
# ---------------------------------------------------------------------------


class NL2CypherRequest(BaseModel):
    question: str
    mapping: dict[str, Any] | None = None
    use_llm: bool = True
    use_fewshot: bool = True
    use_entity_resolution: bool = True
    session_token: str | None = None


@app.post("/nl2cypher")
def nl2cypher_endpoint(req: NL2CypherRequest, _: None = Depends(_check_nl_rate_limit)):
    """Translate a natural language question into Cypher.

    When ``session_token`` is supplied and entity resolution is enabled,
    the session's live ``StandardDatabase`` is passed through to
    :func:`nl_to_cypher` so mentions in the question can be rewritten to
    their database-correct form (WP-25.2).  Without a token the resolver
    is silently disabled and the prompt falls back to its pre-WP-25.2
    shape.
    """
    from .nl2cypher import nl_to_cypher

    db = None
    if req.use_entity_resolution and req.session_token:
        sess = _sessions.get(req.session_token)
        if sess is not None:
            db = sess.db
            sess.touch()

    t0 = time.perf_counter()
    result = nl_to_cypher(
        req.question,
        mapping=req.mapping,
        use_llm=req.use_llm,
        use_fewshot=req.use_fewshot,
        use_entity_resolution=req.use_entity_resolution,
        db=db,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "cypher": result.cypher,
        "explanation": result.explanation,
        "confidence": result.confidence,
        "method": result.method,
        "elapsed_ms": elapsed_ms,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
    }


class NLSuggestRequest(BaseModel):
    mapping: dict[str, Any] | None = None
    count: int = Field(default=8, ge=1, le=20)
    use_llm: bool = True


@app.post("/nl-samples")
def nl_samples_endpoint(req: NLSuggestRequest):
    """Return a representative set of NL questions for the given schema.

    Used by the UI to seed the "Ask" history after schema mapping. Falls back
    to rule-based generation when no LLM provider is configured.
    """
    from .nl2cypher import suggest_nl_queries

    t0 = time.perf_counter()
    queries = suggest_nl_queries(
        req.mapping,
        count=req.count,
        use_llm=req.use_llm,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {"queries": queries, "elapsed_ms": elapsed_ms}


class NL2AqlRequest(BaseModel):
    question: str
    mapping: dict[str, Any] | None = None


@app.post("/nl2aql")
def nl2aql_endpoint(req: NL2AqlRequest, _: None = Depends(_check_nl_rate_limit)):
    """Translate a natural language question directly into AQL (bypassing Cypher)."""
    from .nl2cypher import nl_to_aql

    t0 = time.perf_counter()
    result = nl_to_aql(
        req.question,
        mapping=req.mapping,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "aql": result.aql,
        "bind_vars": result.bind_vars,
        "explanation": result.explanation,
        "confidence": result.confidence,
        "method": result.method,
        "elapsed_ms": elapsed_ms,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
    }


# ---------------------------------------------------------------------------
# OWL Turtle export / import
# ---------------------------------------------------------------------------


class OwlExportRequest(BaseModel):
    mapping: dict[str, Any]


class OwlImportRequest(BaseModel):
    turtle: str


@app.post("/mapping/export-owl")
def export_owl(req: OwlExportRequest):
    """Convert a mapping to OWL/Turtle format."""
    from arango_query_core.owl_turtle import mapping_to_turtle

    bundle = _mapping_from_dict(req.mapping)
    if bundle is None:
        raise HTTPException(status_code=400, detail="mapping is required")
    return {"turtle": mapping_to_turtle(bundle)}


@app.post("/mapping/import-owl")
def import_owl(req: OwlImportRequest):
    """Parse OWL/Turtle into a MappingBundle (as JSON)."""
    from arango_query_core.owl_turtle import turtle_to_mapping

    bundle = turtle_to_mapping(req.turtle)
    return {
        "conceptualSchema": bundle.conceptual_schema,
        "physicalMapping": bundle.physical_mapping,
        "metadata": bundle.metadata,
    }


# ---------------------------------------------------------------------------
# Corrections (local learning) endpoints
# ---------------------------------------------------------------------------

from . import corrections as _corrections


class CorrectionRequest(BaseModel):
    cypher: str
    mapping: dict[str, Any] = Field(default_factory=dict)
    database: str = ""
    original_aql: str
    corrected_aql: str
    bind_vars: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


@app.post("/corrections")
def save_correction(req: CorrectionRequest):
    """Save a user-corrected AQL query for future reuse."""
    row_id = _corrections.save(
        cypher=req.cypher,
        mapping=req.mapping,
        database=req.database,
        original_aql=req.original_aql,
        corrected_aql=req.corrected_aql,
        bind_vars=req.bind_vars,
        note=req.note,
    )
    return {"id": row_id, "status": "saved"}


@app.get("/corrections")
def list_corrections(limit: int = 100):
    """List stored corrections, most recent first."""
    items = _corrections.list_all(limit=limit)
    return {
        "corrections": [
            {
                "id": c.id,
                "cypher": c.cypher,
                "mapping_hash": c.mapping_hash,
                "database": c.database,
                "original_aql": c.original_aql,
                "corrected_aql": c.corrected_aql,
                "bind_vars": c.bind_vars,
                "created_at": c.created_at,
                "note": c.note,
            }
            for c in items
        ]
    }


@app.delete("/corrections/{correction_id}")
def delete_correction(correction_id: int):
    """Delete a single correction."""
    found = _corrections.delete(correction_id)
    if not found:
        raise HTTPException(status_code=404, detail="Correction not found")
    return {"status": "deleted"}


@app.delete("/corrections")
def delete_all_corrections():
    """Delete all corrections."""
    count = _corrections.delete_all()
    return {"status": "deleted", "count": count}


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
