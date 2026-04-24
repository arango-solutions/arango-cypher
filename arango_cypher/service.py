"""FastAPI HTTP service for arango-cypher-py.

Provides REST endpoints for Cypher translation, execution, and connection
management. Serves as the backend for the Cypher Workbench UI (§4.4).

Usage::

    uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import ipaddress
import logging as _logging
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from arango import ArangoClient
from arango.database import StandardDatabase
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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

from .api import get_cypher_profile, translate, validate_cypher_profile
from .extensions import register_all_extensions


def _require_analyzer_unless_opted_out() -> None:
    """Refuse to start the service when the schema analyzer is unavailable.

    The service depends on ``arangodb-schema-analyzer`` for accurate mapping
    of hybrid / LPG schemas. A silent heuristic fallback at deploy time
    produces incorrect labels (file-extension false positives, missing
    relationship kinds) whose downstream effect — unrecoverable Translate
    parse errors — is indistinguishable from an outright service outage.
    See ``docs/schema_inference_bugfix_prd.md`` §4.2 for the full analysis.

    Operators who deliberately accept a degraded heuristic mapping (e.g. in
    local dev against a toy schema) can set ``ARANGO_CYPHER_ALLOW_HEURISTIC=1``
    to bypass this guard. The library and CLI surfaces are unaffected —
    the check runs only at service-import time.
    """
    if os.environ.get("ARANGO_CYPHER_ALLOW_HEURISTIC") == "1":
        return
    try:
        import schema_analyzer  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "arango-cypher-py service requires arangodb-schema-analyzer. "
            "Install it (`pip install arangodb-schema-analyzer`) or set "
            "ARANGO_CYPHER_ALLOW_HEURISTIC=1 to accept degraded mappings."
        ) from exc


_require_analyzer_unless_opted_out()

app = FastAPI(
    title="Arango Cypher Transpiler",
    description="Cypher → AQL translation service for ArangoDB",
    version="0.1.0",
    root_path=os.getenv("ROOT_PATH", ""),
)

# ``ARANGO_CYPHER_PUBLIC_MODE`` is the single switch that flips the service
# from "single-user / local-dev / inside-trusted-network" defaults to
# "shared / multi-user / public-internet" defaults. It exists because the
# UI Workbench is happy without auth on a developer laptop, but the same
# code path on a public host needs every sensitive endpoint locked behind
# a session token. Flag is read once at import time so the surface stays
# deterministic for an operator inspecting the running config.
_PUBLIC_MODE = os.getenv("ARANGO_CYPHER_PUBLIC_MODE", "").lower() in ("true", "1", "yes")

_svc_logger = _logging.getLogger("arango_cypher.service")

_cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "*")
_cors_origins = (
    ["*"]
    if _cors_origins_raw.strip() == "*"
    else [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
)

# CORS credentialed-wildcard guardrail. The pre-existing default
# (``allow_origins=["*"]`` + ``allow_credentials=True``) is unsafe — modern
# browsers refuse the combination, but Starlette's CORSMiddleware *does*
# echo back ``Access-Control-Allow-Origin: <Origin>`` with credentials,
# which silently downgrades the policy to "any origin can carry the
# session cookie" if a non-browser caller obeys the header. Three
# behaviours follow:
#
#   1. ``CORS_ALLOWED_ORIGINS=*`` and ``ARANGO_CYPHER_CORS_CREDENTIALS`` not
#      explicitly set: silently downgrade ``allow_credentials`` to False.
#      The legacy local-dev workflow (no credentialed XHR) keeps working;
#      no operator action required.
#   2. ``CORS_ALLOWED_ORIGINS=*`` with ``ARANGO_CYPHER_CORS_CREDENTIALS=1``:
#      refuse to start. The combination is never safe and we want the
#      operator to discover it at deploy time, not after a session leaks.
#   3. Explicit origin list: honour ``ARANGO_CYPHER_CORS_CREDENTIALS``
#      (default True for back-compat with the existing UI flow).
_cors_credentials_raw = os.getenv("ARANGO_CYPHER_CORS_CREDENTIALS")
_cors_is_wildcard = _cors_origins == ["*"]
if _cors_is_wildcard and _cors_credentials_raw and _cors_credentials_raw.lower() in ("1", "true", "yes"):
    raise RuntimeError(
        "Refusing to start: CORS_ALLOWED_ORIGINS='*' combined with "
        "ARANGO_CYPHER_CORS_CREDENTIALS=true is unsafe. Pin an explicit "
        "origin list (e.g. CORS_ALLOWED_ORIGINS=https://app.example.com) "
        "or unset ARANGO_CYPHER_CORS_CREDENTIALS."
    )
if _cors_is_wildcard:
    _cors_credentials = False
    if _cors_credentials_raw is None:
        _svc_logger.warning(
            "CORS_ALLOWED_ORIGINS='*' detected; allow_credentials forced off. "
            "Pin an explicit origin list to enable credentialed CORS."
        )
else:
    _cors_credentials = True
    if _cors_credentials_raw is not None:
        _cors_credentials = _cors_credentials_raw.lower() in ("1", "true", "yes")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sanitize_pydantic_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip credential-shaped tokens from a Pydantic ``errors()`` list.

    Pydantic v2 echoes the offending ``input`` value back in every
    validation error, which is invaluable for client debugging *and*
    catastrophic for any payload that happened to embed a password
    (saved-correction notes, NL questions that quote a connection
    string, etc.). We can't drop ``input`` wholesale — the UI's
    "tell me what you sent that broke" affordance depends on it —
    so we walk the structure and run the same scalar-level
    :func:`_sanitize_error` redaction on every string value found,
    one nesting level deep (which is enough for every Pydantic input
    shape we currently emit).
    """
    cleaned: list[dict[str, Any]] = []
    for err in errors:
        new = dict(err)
        if "input" in new:
            new["input"] = _redact_value(new["input"])
        if "msg" in new and isinstance(new["msg"], str):
            new["msg"] = _sanitize_error(new["msg"])
        cleaned.append(new)
    return cleaned


def _redact_value(val: Any) -> Any:
    """Recursive credential-pattern redaction on arbitrary JSON-shaped data."""
    if isinstance(val, str):
        return _sanitize_error(val)
    if isinstance(val, dict):
        return {k: _redact_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_redact_value(v) for v in val]
    return val


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    # Body fragments help diagnose UI ↔ service contract drift but they
    # routinely contain credentials (saved-correction payloads echo
    # ``ARANGO_PASS``, ``Authorization`` headers leak via
    # ``X-Arango-Session`` typos, etc.). Always run the same redaction
    # the error-translator uses, and skip body logging entirely in
    # public mode where the operator has signalled a hostile audience.
    safe_errors = _sanitize_pydantic_errors(exc.errors())
    if _PUBLIC_MODE:
        _svc_logger.warning(
            "Pydantic 422 on %s %s: %s",
            request.method,
            request.url.path,
            safe_errors,
        )
    else:
        body = await request.body()
        body_preview = body[:200].decode("utf-8", errors="replace") if body else ""
        body_preview = _sanitize_error(body_preview) if body_preview else ""
        _svc_logger.warning(
            "Pydantic 422 on %s %s: %s | body[:200]=%s",
            request.method,
            request.url.path,
            safe_errors,
            body_preview,
        )
    return JSONResponse(status_code=422, content={"detail": safe_errors})


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
    # Prefer X-Arango-Session: the ArangoDB platform proxy replaces the standard
    # Authorization header with its own platform JWT before forwarding to the
    # BYOC container, making Bearer tokens unusable for app-level session auth.
    token = request.headers.get("X-Arango-Session", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
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


def _require_session_in_public_mode(request: Request) -> _Session | None:
    """Dependency: enforce session auth iff ``ARANGO_CYPHER_PUBLIC_MODE``.

    Used by endpoints that have a legitimate "no auth needed on a single-
    user dev box" mode (NL translation, correction CRUD, /connections)
    but must be locked down on a shared host. Returns the resolved
    session in public mode so callers that want to *use* the session
    (e.g. to bind the NL request to the authenticated DB) don't need a
    second dependency lookup; returns ``None`` in default mode so
    callers that don't need the session aren't forced to thread it.
    """
    if not _PUBLIC_MODE:
        return None
    return _get_session(request)


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
    # Wall-clock time spent in the Cypher → AQL transpiler on this
    # request. Surfaced separately from `exec_ms` so the UI can show
    # both badges side-by-side after a Run; otherwise users lose
    # visibility into translation cost the moment they execute.
    translate_ms: float | None = None


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
# Key-value credential forms (``password=hunter2``, ``api_key: sk-live-…``).
# ``\S+`` stops at whitespace so we don't eat the rest of the log line.
_CRED_RE = re.compile(
    r"(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
# ``Authorization: Bearer <token>`` (and ``Basic`` / ``Digest``) is handled
# separately because the value follows the *scheme* after a whitespace, and
# ``_CRED_RE`` would only consume "Bearer" and let the actual token leak.
_AUTH_HEADER_RE = re.compile(
    r"authorization\s*[:=]\s*(?:bearer|basic|digest|token)\s+\S+",
    re.IGNORECASE,
)

# ArangoDB collection name grammar: 1–256 chars, starts with a letter or
# underscore (system collections), rest may be letters / digits / underscore /
# hyphen. We validate any caller-supplied collection identifier against this
# before embedding it in an AQL f-string (e.g. `FOR t IN \`{name}\``), because
# backtick-interpolation is not a parameterisation boundary — a stray backtick
# or newline in the input would break out of the quote.
_COLLECTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,255}$")


def _sanitize_error(msg: str) -> str:
    """Strip URLs, IP addresses, and credential-like patterns from an error message."""
    msg = _URL_RE.sub("<redacted-url>", msg)
    msg = _HOST_PORT_RE.sub("<redacted-host>", msg)
    # ``_AUTH_HEADER_RE`` first so its multi-token match wins over
    # ``_CRED_RE``'s single-token "authorization: Bearer" prefix match.
    msg = _AUTH_HEADER_RE.sub("<redacted-credential>", msg)
    msg = _CRED_RE.sub("<redacted-credential>", msg)
    return msg


@contextmanager
def _translate_errors(prefix: str, status_code: int = 500):
    """Convert any ``Exception`` raised inside the block into an ``HTTPException``.

    The detail is ``f"{prefix}: {_sanitize_error(str(exc))}"``. Existing
    ``HTTPException``\\ s are re-raised unchanged, so nested validators that
    already produced a richer status code (e.g. 400 / 422) are not masked
    to 500. Use this at every endpoint boundary that runs AQL or otherwise
    touches the DB, to keep the error-surface uniform and sanitised.
    """
    try:
        yield
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status_code,
            detail=f"{prefix}: {_sanitize_error(str(e))}",
        ) from e


_PROXY_ENV_VARS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "https_proxy",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)


# ---------------------------------------------------------------------------
# /connect SSRF guard
# ---------------------------------------------------------------------------
#
# The /connect endpoint takes a caller-supplied URL and opens a TCP
# connection to it. That's intentional — the UI's connect dialog is the
# whole point of this product — but it also means an unauthenticated POST
# can probe arbitrary internal infrastructure from the service host (the
# class textbook SSRF). We can't fully block private targets without
# breaking the legitimate dev workflow ("connect to my localhost ArangoDB"),
# so the policy splits along trust:
#
#   * Always reject cloud-metadata literals (AWS / Azure / OpenStack /
#     GCP / Alibaba). There is no plausible reason to point this service
#     at 169.254.169.254, and the cost of a misclick is full IAM
#     credential exfiltration.
#   * In ``ARANGO_CYPHER_PUBLIC_MODE``, additionally reject any
#     literal-IP host inside RFC1918 / loopback / link-local / ULA so a
#     public deployment can't be coerced into reaching internal services.
#     Operators who deliberately allow private targets (e.g. a co-located
#     ArangoDB on the same VPC) opt in via
#     ``ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS``.
#
# We intentionally do *not* perform DNS resolution here. Resolving the
# host opens a second SSRF vector (DNS rebinding, slow targets used as a
# blocking-IO probe), and operators using DNS in front of private
# infra should pin via ``ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS`` anyway.

_BLOCK_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",  # AWS / Azure / OpenStack / DigitalOcean
        "100.100.100.200",  # Alibaba Cloud
        "metadata.google.internal",
        "metadata.goog",
        "metadata",  # GCP shorthand
    }
)
_BLOCK_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 metadata
    }
)
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _connect_allowed_hosts() -> frozenset[str]:
    """Operator-supplied allowlist of host strings that bypass the SSRF guard.

    Re-read on every call (not cached) so tests can monkeypatch the env
    var per case. The cost is one ``os.environ.get`` plus a string split
    per ``/connect`` request, which is negligible next to the TCP
    handshake the endpoint is about to perform.
    """
    raw = os.environ.get("ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _check_connect_target(url: str) -> None:
    """Raise ``HTTPException(400)`` if ``url`` points at a forbidden target.

    See the module-level comment block above for the policy. The check
    is best-effort by design — a determined caller can wrap a forbidden
    address behind a public DNS name we can't see without resolving.
    The intent is to refuse the obvious foot-guns (literal cloud-metadata
    IPs, hard-coded RFC1918 hostnames in a public deployment) without
    pretending to provide complete network isolation; that's the job of
    the surrounding network/SG configuration.
    """
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid connection URL") from exc
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="Connection URL is missing a host")

    allowlist = _connect_allowed_hosts()
    if host in allowlist:
        return

    # Strip IPv6 brackets that ``urlparse`` already removes from
    # ``hostname`` but a raw user-supplied IPv6 literal might still carry.
    bare = host.strip("[]")

    if host in _BLOCK_METADATA_HOSTS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Refusing to connect to cloud metadata service host. Set "
                "ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS if this is intentional."
            ),
        )

    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return

    if ip in _BLOCK_METADATA_IPS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Refusing to connect to cloud metadata IP. Set "
                "ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS if this is intentional."
            ),
        )

    if not _PUBLIC_MODE:
        return

    for net in _PRIVATE_NETWORKS:
        if ip in net:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Refusing to connect to private/loopback address {ip} in public mode. "
                    "Pin the literal via ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS to override."
                ),
            )


def _walk_cause_chain(exc: BaseException) -> list[BaseException]:
    """Return the chain of exceptions from outer to root via __cause__/__context__."""
    seen: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in seen:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return seen


def _describe_connect_error(exc: BaseException) -> str:
    """Build a diagnostic message for a /connect failure.

    The python-arango client wraps low-level transport failures (proxy
    rejections, DNS, TLS, credential rejection) in a generic
    ``ClientConnectionError`` ("Can't connect to host(s) within limit (N)"),
    which is not actionable. Walk the ``__cause__`` / ``__context__`` chain
    to surface the most specific root cause. When a proxy-tunnel failure
    is detected, also report which proxy env vars are set so the operator
    can correlate with a misconfigured sandbox / corporate proxy.
    """
    chain = _walk_cause_chain(exc)
    root_msg = str(chain[-1]) if chain else str(exc)
    top_msg = str(chain[0]) if chain else str(exc)

    parts: list[str] = [top_msg]
    if len(chain) > 1 and root_msg and root_msg != top_msg:
        parts.append(f"root cause: {root_msg}")

    joined = " | ".join(parts)
    lowered = joined.lower()

    if "tunnel connection failed" in lowered or "proxy" in lowered:
        proxies_set = [v for v in _PROXY_ENV_VARS if os.environ.get(v)]
        if proxies_set:
            parts.append(
                "hint: this process has proxy env vars set ("
                + ", ".join(proxies_set)
                + "). Unset them or add the ArangoDB host to NO_PROXY and restart."
            )
        else:
            parts.append(
                "hint: proxy tunnel rejected the connection but no proxy env vars "
                "are set in this process; check system / IDE sandbox proxy config."
            )

    return _sanitize_error(" | ".join(parts))


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


# Liveness / readiness probe for container orchestrators (Arango Platform's
# Container Manager, Kubernetes, docker-compose healthchecks, etc.). Cheap,
# unauthenticated, no DB call -- returning 200 proves the process is up and
# the FastAPI event loop is serving. Actual DB reachability is tested per
# session via POST /connect, which is where a connection failure should
# surface (not at startup).
@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "arango-cypher-py",
        "version": app.version,
    }


@app.post("/connect", response_model=ConnectResponse)
def connect(req: ConnectRequest):
    """Authenticate to ArangoDB; returns a session token."""
    _check_connect_target(req.url)
    try:
        url = req.url.rstrip("/")
        client = ArangoClient(hosts=url)
        db = client.db(req.database, username=req.username, password=req.password)
        db.version()
    except Exception as e:
        detail = _describe_connect_error(e)
        _svc_logger.warning(
            "connect failed for db=%r user=%r: %s",
            req.database,
            req.username,
            detail,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Connection failed: {detail}",
        ) from e

    _evict_lru()
    token = secrets.token_urlsafe(32)
    _sessions[token] = _Session(token=token, db=db, client=client)

    try:
        databases = [
            d for d in client.db("_system", username=req.username, password=req.password).databases()
        ]
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
def list_connections(_auth: _Session | None = Depends(_require_session_in_public_mode)):
    """List active sessions (admin/debug). Requires auth in public mode."""
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

    Uses ARANGO_URL directly if set, otherwise builds from
    ARANGO_HOST/ARANGO_PORT/ARANGO_PROTOCOL.

    Disabled entirely when ``ARANGO_CYPHER_PUBLIC_MODE=true``. The
    password is omitted from the response by default — the field is
    still present (the UI's connect dialog binds against it) but the
    value is the empty string so a curious anonymous caller can't
    pull ``ARANGO_PASS`` out of the .env on a single-user dev box.
    Operators who want the legacy "auto-fill the password" convenience
    on a trusted laptop can set ``ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD``
    to ``1``.
    """
    if _PUBLIC_MODE:
        raise HTTPException(status_code=404, detail="Not available in public mode")

    arango_url = os.getenv("ARANGO_URL", "")
    if not arango_url:
        host = os.getenv("ARANGO_HOST", "localhost")
        port = os.getenv("ARANGO_PORT", "8529")
        protocol = os.getenv("ARANGO_PROTOCOL", "http")
        arango_url = f"{protocol}://{host}:{port}"

    expose_pw = os.getenv("ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD", "").lower() in (
        "1",
        "true",
        "yes",
    )
    return {
        "url": arango_url.rstrip("/"),
        "database": os.getenv("ARANGO_DB", "_system"),
        "username": os.getenv("ARANGO_USER", "root"),
        "password": os.getenv("ARANGO_PASS", "") if expose_pw else "",
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
            "translate CoreError: %s (code=%s) for cypher=%r",
            e,
            e.code,
            req.cypher[:80],
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(e)), "code": e.code},
        ) from e
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    correction = _corrections.lookup(req.cypher, req.mapping)
    if correction:
        return TranslateResponse(
            aql=correction.corrected_aql,
            bind_vars=correction.bind_vars or result.bind_vars,
            warnings=[{"message": f"Using learned correction #{correction.id}"}]
            + list(result.warnings or []),
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
        t_translate = time.perf_counter()
        transpiled = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
        translate_ms = round((time.perf_counter() - t_translate) * 1000, 1)
    except CoreError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(e)), "code": e.code},
        ) from e

    correction = _corrections.lookup(req.cypher, req.mapping)
    run_aql = correction.corrected_aql if correction else transpiled.aql
    run_bind = (correction.bind_vars or transpiled.bind_vars) if correction else transpiled.bind_vars
    warnings = list(transpiled.warnings or [])
    if correction:
        warnings.insert(0, {"message": f"Using learned correction #{correction.id}"})

    with _translate_errors("AQL execution failed"):
        t_exec = time.perf_counter()
        cursor = session.db.aql.execute(run_aql, bind_vars=run_bind)
        results = list(cursor)
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)

    return ExecuteResponse(
        results=results,
        aql=run_aql,
        bind_vars=run_bind,
        warnings=warnings,
        exec_ms=exec_ms,
        translate_ms=translate_ms,
    )


class ExecuteAqlRequest(BaseModel):
    aql: str
    bind_vars: dict[str, Any] = Field(default_factory=dict)


@app.post("/execute-aql")
def execute_aql_endpoint(req: ExecuteAqlRequest, session: _Session = Depends(_get_session)):
    """Execute a raw AQL query directly (used by NL→AQL direct path)."""
    with _translate_errors("AQL execution failed"):
        t_exec = time.perf_counter()
        cursor = session.db.aql.execute(req.aql, bind_vars=req.bind_vars)
        results = list(cursor)
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)

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
        t_translate = time.perf_counter()
        transpiled = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
        translate_ms = round((time.perf_counter() - t_translate) * 1000, 1)
    except CoreError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(e)), "code": e.code},
        ) from e

    with _translate_errors("AQL EXPLAIN failed"):
        plan = session.db.aql.explain(transpiled.aql, bind_vars=transpiled.bind_vars)

    return {
        "aql": transpiled.aql,
        "bind_vars": transpiled.bind_vars,
        "plan": plan,
        "translate_ms": translate_ms,
    }


@app.post("/aql-profile")
def aql_profile_endpoint(req: TranslateRequest, session: _Session = Depends(_get_session)):
    """Translate Cypher to AQL, execute with profiling, return runtime stats + results."""
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")

    registry = _default_registry if req.extensions_enabled else None
    try:
        t_translate = time.perf_counter()
        transpiled = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
        translate_ms = round((time.perf_counter() - t_translate) * 1000, 1)
    except CoreError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(e)), "code": e.code},
        ) from e

    with _translate_errors("AQL profiled execution failed"):
        cursor = session.db.aql.execute(
            transpiled.aql,
            bind_vars=transpiled.bind_vars,
            profile=True,
        )
        results = list(cursor)
        stats = cursor.statistics()
        profile_data = cursor.profile() if hasattr(cursor, "profile") else None

    return {
        "aql": transpiled.aql,
        "bind_vars": transpiled.bind_vars,
        "results": results,
        "statistics": stats,
        "profile": profile_data,
        "translate_ms": translate_ms,
    }


# ---------------------------------------------------------------------------
# Schema introspection endpoints
# ---------------------------------------------------------------------------


def _sample_properties(
    db: StandardDatabase, collection_name: str, sample_size: int = 100
) -> dict[str, dict[str, Any]]:
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
    db: StandardDatabase,
    edge_collection: str,
    limit: int = 20,
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

    Pass ``force=true`` to bypass *both* mapping cache tiers (in-process
    and persistent ``_schemas`` collection). Previously this only
    popped the in-process tier, which meant a stale entry in the
    persistent cache would keep getting served forever — the user's
    only recourse was to manually call ``/schema/invalidate-cache``,
    which is non-discoverable. Forwarding ``force_refresh=True`` to
    ``get_mapping`` makes the flag do what its name says.
    """
    db = session.db
    from .schema_acquire import get_mapping as _get_mapping

    bundle = _get_mapping(db, force_refresh=force)

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

    result["warnings"] = (bundle.metadata or {}).get("warnings") or []
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
    from .schema_acquire import compute_statistics as _compute_stats
    from .schema_acquire import get_mapping as _get_mapping

    t0 = time.perf_counter()
    bundle = _get_mapping(session.db)
    stats = _compute_stats(session.db, bundle)
    elapsed = round(time.perf_counter() - t0, 3)
    return {"statistics": stats, "elapsed_seconds": elapsed}


@app.get("/schema/status")
def schema_status(
    cache_collection: str | None = None,
    cache_key: str | None = None,
    session: _Session = Depends(_get_session),
):
    """Report whether the schema has changed since the cached mapping was built.

    Cheap probe: runs ``db.collections()`` + per-collection ``count()`` +
    ``indexes()``. No document sampling, no AQL, no LLM call. Typical cost
    ~20 ms for a 50-collection schema.

    ``status`` values:

    * ``"unchanged"`` — the cached mapping is fully valid.
    * ``"stats_changed"`` — shape matches but counts differ; calling a
      mapping-consuming endpoint (e.g. ``/schema/introspect``) will refresh
      only the statistics block.
    * ``"shape_changed"`` — collection set, collection type, or an index
      set has changed; a mapping-consuming endpoint will re-introspect.
    * ``"no_cache"`` — no prior fingerprint recorded (e.g. first call
      after service start or after ``POST /schema/invalidate-cache``).

    Response also includes ``unchanged`` and ``needs_full_rebuild``
    convenience booleans and the four fingerprints (current + cached,
    shape + full) so callers can build their own diff UIs.

    Use this to skip expensive prompt rebuilds / view cache busts /
    downstream notifications when nothing has actually changed.
    """
    from .schema_acquire import (
        DEFAULT_CACHE_COLLECTION,
        DEFAULT_CACHE_KEY,
    )
    from .schema_acquire import (
        describe_schema_change as _describe,
    )

    report = _describe(
        session.db,
        cache_collection=cache_collection or DEFAULT_CACHE_COLLECTION,
        cache_key=cache_key or DEFAULT_CACHE_KEY,
    )
    return {
        "status": report.status,
        "unchanged": report.unchanged,
        "needs_full_rebuild": report.needs_full_rebuild,
        "current_shape_fingerprint": report.current_shape_fingerprint,
        "current_full_fingerprint": report.current_full_fingerprint,
        "cached_shape_fingerprint": report.cached_shape_fingerprint,
        "cached_full_fingerprint": report.cached_full_fingerprint,
    }


@app.post("/schema/invalidate-cache")
def schema_invalidate_cache(
    cache_collection: str | None = None,
    cache_key: str | None = None,
    persistent: bool = True,
    session: _Session = Depends(_get_session),
):
    """Drop the in-memory and (optionally) persistent mapping cache.

    The next call to ``/schema/introspect`` — or any other mapping-consuming
    endpoint — will re-introspect the schema unconditionally.

    Query parameters:

    * ``cache_collection`` — name of the persistent cache collection
      (default: ``arango_cypher_schema_cache``). Used only when
      ``persistent=true``.
    * ``cache_key`` — key inside the cache collection (default:
      ``mapping``). Used only when ``persistent=true``.
    * ``persistent`` — when ``true`` (default), both the in-memory and the
      persistent cache are dropped. When ``false``, only the in-memory
      (process-local) cache is dropped; the persistent cache survives
      and will be re-read on the next call from a cold process.

    Use ``persistent=false`` for targeted in-process invalidation (e.g.
    after an administrative action that you know only affects the current
    replica's view, not the shared database state).
    """
    from .schema_acquire import (
        DEFAULT_CACHE_COLLECTION,
        DEFAULT_CACHE_KEY,
    )
    from .schema_acquire import (
        invalidate_cache as _invalidate,
    )

    _invalidate(
        session.db,
        cache_collection=(cache_collection or DEFAULT_CACHE_COLLECTION) if persistent else None,
        cache_key=cache_key or DEFAULT_CACHE_KEY,
    )
    return {"invalidated": True, "persistent": persistent}


@app.post("/schema/force-reacquire")
def schema_force_reacquire(session: _Session = Depends(_get_session)):
    """Drop any cached mapping and rebuild from scratch via the analyzer.

    Operational tool for recovering from a poisoned cache: the previous
    ``get_mapping`` call fell back to the heuristic because the analyzer
    was not installed at that moment, the degraded bundle got persisted,
    and subsequent ``force=true`` introspects re-served the same cached
    bundle because the shape fingerprint did not change. This endpoint
    calls ``get_mapping(..., strategy="analyzer", force_refresh=True)`` —
    the hard form — which raises ``ImportError`` (surfaced as HTTP 503) if
    the analyzer is still unavailable instead of silently falling back.
    """
    from .schema_acquire import get_mapping as _get_mapping

    try:
        bundle = _get_mapping(session.db, force_refresh=True, strategy="analyzer")
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "arangodb-schema-analyzer is not installed; cannot force a "
                "fresh analyzer mapping. Install it and retry, or call "
                "/schema/invalidate-cache to drop the cached entry and let "
                "the heuristic fallback run again. Underlying error: "
                f"{exc}"
            ),
        ) from exc

    source_kind = bundle.source.kind if bundle.source is not None else None
    source_notes = bundle.source.notes if bundle.source is not None else None
    warnings = (bundle.metadata or {}).get("warnings") or []
    return {
        "source": {"kind": source_kind, "notes": source_notes},
        "warnings": warnings,
        "entity_count": len(bundle.conceptual_schema.get("entities") or []),
        "relationship_count": len(bundle.conceptual_schema.get("relationships") or []),
    }


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


class TenantContextPayload(BaseModel):
    """Ambient tenant scope applied to NL translations in a session.

    Mirrors :class:`arango_cypher.nl2cypher.tenant_guardrail.TenantContext`
    on the wire. See ``/tenants`` for how the UI sources this.
    """

    property: str = Field(
        ...,
        description=(
            "Physical property name on the Tenant entity (e.g. 'TENANT_HEX_ID', 'NAME', 'SUBDOMAIN')."
        ),
    )
    value: str = Field(..., description="Exact value to match.")
    display: str | None = Field(
        default=None,
        description="Optional human-readable label for prompts / UI.",
    )


class NL2CypherRequest(BaseModel):
    question: str
    mapping: dict[str, Any] | None = None
    use_llm: bool = True
    use_fewshot: bool = True
    use_entity_resolution: bool = True
    session_token: str | None = None
    tenant_context: TenantContextPayload | None = None
    # WP-29 Part 4: WP-30 hand-off contract. When supplied, the NL
    # retry loop seeds ``PromptBuilder.retry_context`` on the very
    # first attempt with the caller-provided hint (typically the
    # parse / EXPLAIN error from a prior translate). WP-30 wires
    # this from the UI's "Regenerate from NL with error hint"
    # button; without a caller it stays ``None`` and the prompt is
    # byte-identical to the pre-WP-29 shape for zero-shot bare-name
    # schemas.
    retry_context: str | None = None


@app.post("/nl2cypher")
def nl2cypher_endpoint(
    req: NL2CypherRequest,
    _: None = Depends(_check_nl_rate_limit),
    auth_session: _Session | None = Depends(_require_session_in_public_mode),
):
    """Translate a natural language question into Cypher.

    When ``session_token`` is supplied and entity resolution is enabled,
    the session's live ``StandardDatabase`` is passed through to
    :func:`nl_to_cypher` so mentions in the question can be rewritten to
    their database-correct form (WP-25.2).  Without a token the resolver
    is silently disabled and the prompt falls back to its pre-WP-25.2
    shape.

    In ``ARANGO_CYPHER_PUBLIC_MODE`` the request body's
    ``session_token`` field is ignored — the authenticated session
    (resolved from ``X-Arango-Session`` / ``Authorization``) is used
    instead, so a caller cannot point one user's NL request at another
    user's database by guessing the body field.
    """
    from .nl2cypher import nl_to_cypher
    from .nl2cypher.tenant_guardrail import TenantContext

    db = None
    if _PUBLIC_MODE:
        if auth_session is not None and req.use_entity_resolution:
            db = auth_session.db
            auth_session.touch()
    elif req.use_entity_resolution and req.session_token:
        sess = _sessions.get(req.session_token)
        if sess is not None:
            db = sess.db
            sess.touch()

    tenant_ctx = None
    if req.tenant_context is not None:
        tenant_ctx = TenantContext(
            property=req.tenant_context.property,
            value=req.tenant_context.value,
            display=req.tenant_context.display,
        )

    t0 = time.perf_counter()
    result = nl_to_cypher(
        req.question,
        mapping=req.mapping,
        use_llm=req.use_llm,
        use_fewshot=req.use_fewshot,
        use_entity_resolution=req.use_entity_resolution,
        db=db,
        tenant_context=tenant_ctx,
        retry_context=req.retry_context,
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
        "cached_tokens": result.cached_tokens,
        "retries": result.retries,
    }


class NLSuggestRequest(BaseModel):
    mapping: dict[str, Any] | None = None
    count: int = Field(default=8, ge=1, le=20)
    use_llm: bool = True


@app.post("/nl-samples")
def nl_samples_endpoint(
    req: NLSuggestRequest,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
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
    tenant_context: TenantContextPayload | None = None


@app.post("/nl2aql")
def nl2aql_endpoint(
    req: NL2AqlRequest,
    _: None = Depends(_check_nl_rate_limit),
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Translate a natural language question directly into AQL (bypassing Cypher)."""
    from .nl2cypher import nl_to_aql
    from .nl2cypher.tenant_guardrail import TenantContext

    tenant_ctx = None
    if req.tenant_context is not None:
        tenant_ctx = TenantContext(
            property=req.tenant_context.property,
            value=req.tenant_context.value,
            display=req.tenant_context.display,
        )

    t0 = time.perf_counter()
    result = nl_to_aql(
        req.question,
        mapping=req.mapping,
        tenant_context=tenant_ctx,
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
        "cached_tokens": result.cached_tokens,
    }


# ---------------------------------------------------------------------------
# Tenant catalog (multi-tenant graphs)
# ---------------------------------------------------------------------------

# Maximum number of tenants to surface in a single catalog response. 10k is
# ample headroom for the target schemas (Dagster-style graphs top out around
# 10³); clients that need more should paginate via a follow-up API.
_TENANT_CATALOG_LIMIT = 10000


@app.get("/tenants")
def tenants_endpoint(
    collection: str | None = None,
    session: _Session = Depends(_get_session),
):
    """Return the list of tenants in the connected database, if any.

    The optional ``collection`` query parameter lets the UI tell the
    server which ArangoDB collection backs the conceptual ``Tenant``
    entity (typically derived client-side from
    ``physical_mapping.entities.Tenant.collectionName``). When omitted,
    the endpoint falls back to the literal name ``Tenant`` — the
    pre-Wave-4r behaviour, kept for compatibility with stale UIs.

    Why a query param instead of POST-with-mapping? Three reasons:

    1. POST-with-body for a pure read trips CORS preflights in
       cross-origin deployments.
    2. A new UI bundle deployed against an older service (the common
       case during rolling deploys) would 405 on POST and silently
       hide the selector with no diagnostic.
    3. The mapping already lives in the UI's state; sending it back
       just so the server can pluck a single string out wastes a
       megabyte of payload per call on real schemas.

    The response includes ``collection`` (the resolved name we
    queried) and ``source`` (``"client"`` when the caller supplied
    the name, ``"heuristic"`` when we fell back to ``"Tenant"``)
    so the UI can show *why* detection succeeded or failed.
    """
    db = session.db
    if collection:
        resolved, source = collection, "client"
    else:
        resolved, source = "Tenant", "heuristic"

    # Defence-in-depth against AQL identifier injection: the resolved name is
    # interpolated into the AQL f-string below inside backticks, so anything
    # that isn't a valid ArangoDB collection identifier must be rejected at
    # the edge. `has_collection()` returns False for names that don't exist
    # but does *not* reject syntactically invalid names on all client
    # versions, hence the explicit gate.
    if not _COLLECTION_NAME_RE.fullmatch(resolved):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid collection name: must be 1–256 characters, start "
                "with a letter or underscore, and contain only letters, "
                "digits, underscore, or hyphen."
            ),
        )

    with _translate_errors("Failed to inspect collections"):
        has_collection = db.has_collection(resolved)

    if not has_collection:
        return {
            "detected": False,
            "tenants": [],
            "collection": resolved,
            "source": source,
        }

    aql = (
        f"FOR t IN `{resolved}` "
        f"LIMIT {_TENANT_CATALOG_LIMIT} "
        "SORT t.NAME "
        "RETURN { "
        # `id` (full _id, e.g. 'Tenant/<uuid>') is the canonical
        # tenant identifier — what the guardrail uses to scope
        # generated Cypher. `key` is exposed too for tooltips and
        # for the Cypher `{_key: '...'}` shorthand. The schema-
        # specific NAME / SUBDOMAIN / TENANT_HEX_ID fields are
        # surfaced for human display and search but are not
        # required to exist; the LIMIT-projection tolerates nulls.
        "id: t._id, "
        "key: t._key, "
        "name: t.NAME, "
        "subdomain: t.SUBDOMAIN, "
        "hex_id: t.TENANT_HEX_ID "
        "}"
    )
    with _translate_errors("Tenant catalog query failed"):
        cursor = db.aql.execute(aql)
        tenants = list(cursor)

    return {
        "detected": True,
        "tenants": tenants,
        "collection": resolved,
        "source": source,
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

from . import corrections as _corrections  # noqa: E402


class CorrectionRequest(BaseModel):
    cypher: str
    mapping: dict[str, Any] = Field(default_factory=dict)
    database: str = ""
    original_aql: str
    corrected_aql: str
    bind_vars: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


@app.post("/corrections")
def save_correction(
    req: CorrectionRequest,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
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
def list_corrections(
    limit: int = 100,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
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
def delete_correction(
    correction_id: int,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Delete a single correction."""
    found = _corrections.delete(correction_id)
    if not found:
        raise HTTPException(status_code=404, detail="Correction not found")
    return {"status": "deleted"}


@app.delete("/corrections")
def delete_all_corrections(
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Delete all corrections."""
    count = _corrections.delete_all()
    return {"status": "deleted", "count": count}


# ---------------------------------------------------------------------------
# NL-corrections (few-shot feedback loop) endpoints
# ---------------------------------------------------------------------------
#
# Cypher→AQL corrections (above) fix the transpiler's output for a specific
# (cypher, mapping) pair. NL corrections operate one layer higher: they
# capture approved ``(natural_language_question, cypher)`` pairs and feed
# them into the FewShotIndex BM25 corpus so future similar questions
# benefit. The two stores are deliberately separate — they have different
# lookup keys, different lifecycle triggers, and different callers.

from . import nl_corrections as _nl_corrections  # noqa: E402


class NLCorrectionRequest(BaseModel):
    question: str
    cypher: str
    mapping: dict[str, Any] = Field(default_factory=dict)
    database: str = ""
    note: str = ""


@app.post("/nl-corrections")
def save_nl_correction(
    req: NLCorrectionRequest,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Save an approved (NL question → Cypher) pair for few-shot retrieval.

    The pair is appended to the BM25 corpus the next time
    ``POST /nl2cypher`` builds (or rebuilds) its default
    :class:`FewShotIndex`. The FewShotIndex cache is invalidated
    synchronously before this endpoint returns, so the improvement takes
    effect on the very next translation request.
    """
    try:
        row_id = _nl_corrections.save(
            question=req.question,
            cypher=req.cypher,
            mapping=req.mapping or None,
            database=req.database,
            note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": row_id, "status": "saved"}


@app.get("/nl-corrections")
def list_nl_corrections(
    limit: int = 100,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """List stored NL corrections, most recent first."""
    items = _nl_corrections.list_all(limit=limit)
    return {
        "corrections": [
            {
                "id": c.id,
                "question": c.question,
                "cypher": c.cypher,
                "mapping_hash": c.mapping_hash,
                "database": c.database,
                "created_at": c.created_at,
                "note": c.note,
            }
            for c in items
        ]
    }


@app.delete("/nl-corrections/{correction_id}")
def delete_nl_correction(
    correction_id: int,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Delete a single NL correction."""
    found = _nl_corrections.delete(correction_id)
    if not found:
        raise HTTPException(status_code=404, detail="NL correction not found")
    return {"status": "deleted"}


@app.delete("/nl-corrections")
def delete_all_nl_corrections(
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Delete all NL corrections."""
    count = _nl_corrections.delete_all()
    return {"status": "deleted", "count": count}


# ---------------------------------------------------------------------------
# Static file serving for the Cypher Workbench UI
# ---------------------------------------------------------------------------

_UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "dist"

if _UI_DIR.is_dir():
    # Cache policy:
    #   - index.html (the SPA shell) MUST always revalidate. Without this,
    #     Chrome's heuristic cache will pin a stale shell that keeps replaying
    #     stale `/connect` calls or pointing at an old hashed asset bundle,
    #     and the only fix is "Application → Clear site data" — exactly the
    #     situation this comment is meant to prevent.
    #   - /assets/* files are content-hashed by Vite, so they are safe to
    #     mark immutable for a year.
    _HTML_NO_CACHE = "no-cache, no-store, must-revalidate"
    _ASSET_IMMUTABLE = "public, max-age=31536000, immutable"

    def _html_response(path: Path) -> FileResponse:
        return FileResponse(path, headers={"Cache-Control": _HTML_NO_CACHE})

    def _spa_serve(full_path: str) -> FileResponse:
        """Serve a UI asset if it exists, otherwise fall back to index.html.

        Used by both the legacy ``/ui`` and AMP ``/frontend`` mounts so the
        cache-headers contract (HTML revalidates, hashed assets immutable) is
        identical across both prefixes — pinned by ``TestUiCacheHeaders``.
        """
        file = _UI_DIR / full_path
        if file.is_file():
            # Non-hashed files (e.g. an icon copied next to index.html) —
            # revalidate too. Hashed assets are served by the dedicated
            # _ImmutableAssets mount below at /assets.
            headers = {"Cache-Control": _HTML_NO_CACHE} if file.suffix == ".html" else None
            return FileResponse(file, headers=headers) if headers else FileResponse(file)
        return _html_response(_UI_DIR / "index.html")

    # Legacy mount: /ui (local dev, existing bookmarks). Kept alongside the
    # AMP /frontend mount below so backward-compat doesn't depend on a
    # follow-up sweep through every doc, runbook, or operator workflow.
    @app.api_route("/ui", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/ui/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _ui_index() -> FileResponse:
        return _html_response(_UI_DIR / "index.html")

    @app.api_route("/ui/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _ui_spa_fallback(full_path: str) -> FileResponse:
        return _spa_serve(full_path)

    # AMP mount: /frontend. Required by the ArangoDB platform proxy which
    # routes /frontend (not /ui) to the BYOC container. The bare /frontend
    # (no trailing slash) handler is critical: Starlette's default StaticFiles
    # mount issues a 307 redirect to /frontend/ which the AMP proxy does NOT
    # forward to the container, surfacing as a platform-level 404. We use
    # explicit handlers (not app.mount + StaticFiles) so we can apply the
    # same cache-headers contract as /ui without a StaticFiles subclass.
    @app.api_route("/frontend", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/frontend/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _frontend_index() -> FileResponse:
        return _html_response(_UI_DIR / "index.html")

    @app.api_route(
        "/frontend/{full_path:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def _frontend_spa_fallback(full_path: str) -> FileResponse:
        return _spa_serve(full_path)

    # The Vite build emits root-relative URLs (`/assets/...`, `/favicon.svg`,
    # `/icons.svg`) to match its dev server (`port: 5173`, no `base: '/ui/'`).
    # Mount them at the app root so the production-mode `/ui` page can load
    # its JS / CSS / icons without a rebuild.
    _UI_ASSETS = _UI_DIR / "assets"
    if _UI_ASSETS.is_dir():

        class _ImmutableAssets(StaticFiles):
            """StaticFiles subclass that marks hashed Vite assets immutable."""

            async def get_response(self, path, scope):  # type: ignore[override]
                response = await super().get_response(path, scope)
                if response.status_code == 200:
                    response.headers["Cache-Control"] = _ASSET_IMMUTABLE
                return response

        app.mount(
            "/assets",
            _ImmutableAssets(directory=str(_UI_ASSETS)),
            name="ui_assets",
        )

    for _icon in ("favicon.svg", "icons.svg"):
        _icon_path = _UI_DIR / _icon
        if _icon_path.is_file():

            def _make_icon_route(path: Path):
                async def _serve_icon() -> FileResponse:
                    return FileResponse(path)

                return _serve_icon

            app.add_api_route(
                f"/{_icon}",
                _make_icon_route(_icon_path),
                include_in_schema=False,
            )
