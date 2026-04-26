"""FastAPI app factory + startup-time guards for ``arango_cypher.service``.

Owns four concerns:

1. The ``app = FastAPI(...)`` instance every route module decorates.
2. The :func:`_require_analyzer_unless_opted_out` startup guard.
3. The CORS-credentialed-wildcard guardrail (refuse-to-start /
   silent-downgrade matrix; see the inline comment block on
   ``_cors_credentials_raw`` for the full table).
4. The single ``ARANGO_CYPHER_PUBLIC_MODE`` flag readout that flips
   the rest of the service from "single-user / local-dev" defaults
   to "shared / public-internet" defaults.

Re-exported from :mod:`arango_cypher.service` so the historical
``from arango_cypher.service import app`` style imports keep working
across the audit-v2 #8 split. ``ArangoClient`` is also re-exported via
the package init so the ``monkeypatch.setattr("arango_cypher.service.ArangoClient", ...)``
test pattern in ``tests/test_service_hardening.py`` continues to flow
through to the connect route's call-time lookup.
"""

from __future__ import annotations

import logging as _logging
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


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

# Audit-v2 #6 — observability spine. Imported here (rather than from the
# package init) so the middleware install happens at app-construction time,
# alongside CORS, and the logging filter / handler attachment runs before any
# route module's import-time logger.* calls. The helper is idempotent so
# tests that reload the package re-trigger setup safely.
#
# Middleware order matters: ``CorrelationIdMiddleware`` is added *after*
# ``CORSMiddleware`` above, which means it runs *first* on the inbound path
# (Starlette wraps middlewares LIFO). That's deliberate — we want the
# correlation ID minted before the CORS preflight handler emits its log
# line, not after, so even rejected preflights carry an X-Request-Id in the
# log trail for cross-referencing client / server traces.
from .observability import CorrelationIdMiddleware, configure_observability  # noqa: E402

configure_observability()
app.add_middleware(CorrelationIdMiddleware)
