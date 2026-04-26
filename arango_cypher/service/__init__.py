"""FastAPI HTTP service for arango-cypher-py.

Provides REST endpoints for Cypher translation, execution, and connection
management. Serves as the backend for the Cypher Workbench UI (§4.4).

Usage::

    uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8000

Package layout (post audit-v2 #8 split):

* :mod:`.app` — FastAPI() instance, CORS guard, ARANGO_CYPHER_PUBLIC_MODE
  flag, ``_require_analyzer_unless_opted_out`` startup guard.
* :mod:`.security` — sessions, rate-limit token buckets, SSRF guard,
  error / log redaction, the Pydantic 422 handler.
* :mod:`.models` — every request / response Pydantic model + the
  ``_MAX_*`` length constants.
* :mod:`.mapping` — ``_mapping_from_dict`` HTTP-shape adapter.
* :mod:`.registry` — process-wide ExtensionRegistry singleton.
* :mod:`.routes` — endpoint cluster modules (connect, cypher, schema,
  tools, nl, owl, corrections, health). Importing the package
  triggers all 35-odd ``@app.route(...)`` registrations via decorator
  side effects.
* :mod:`.ui` — UI freshness check + ``/ui`` / ``/frontend`` / ``/assets``
  / per-icon mounts. Imported last so its WARNING lines surface after
  the route registration log noise.

This file's job is to (a) execute the imports in the order required to
preserve the historical side-effect sequence, and (b) re-export every
public + underscore-prefixed name that external callers (tests,
``main.py``, ``scripts/benchmark_translate``) depend on. The re-export
list is deliberately exhaustive so ``from arango_cypher.service import X``
keeps working byte-for-byte across the split.
"""

from __future__ import annotations

import sys as _sys
import time  # noqa: F401  (re-export for monkeypatch surface — see note below)

# Force submodule re-execution on package reload. Several tests
# (tests/test_service_hardening.py::reload_service_clean,
# tests/test_service_startup.py) flip an env var, ``sys.modules.pop()``
# this package, then ``importlib.import_module`` it again expecting
# every CORS / analyzer / public-mode guard inside the submodules to
# re-run. Python's import system caches submodules independently of
# the parent package, so without this purge a reload re-runs only
# this ``__init__.py`` while ``.app`` / ``.security`` / ``.routes.*``
# stay frozen at the values from the very first import. The first
# import is a no-op (no submodules in sys.modules yet); subsequent
# reloads pick up the env-var change as intended. Closes the
# regression introduced by the audit-v2 #8 split that surfaced as
# 26 hardening test failures pre-fix. Must run BEFORE the submodule
# imports below or it would purge the modules we just imported.
for _name in [n for n in list(_sys.modules) if n.startswith("arango_cypher.service.")]:
    del _sys.modules[_name]
del _sys

# ``time`` (above) is kept as a top-level re-export because the
# pre-split monolithic ``service.py`` had ``import time`` at the top
# and ``tests/test_service_middleware.py::TestTokenBucket`` still does
# ``monkeypatch.setattr(svc.time, "time", fake_time)`` against it.
# Removing the re-export would break the test's patch surface despite
# zero behaviour change in the runtime.

# 1) App factory + startup guards (runs the analyzer / CORS guards as a side
#    effect of import). ``ArangoClient`` is re-exposed off this package so
#    the ``monkeypatch.setattr("arango_cypher.service.ArangoClient", ...)``
#    pattern in tests/test_service_hardening.py keeps working — the connect
#    route reads it lazily via ``arango_cypher.service.ArangoClient``.
from arango import ArangoClient  # noqa: F401  (re-export)

# Backward-compat re-export: the corrections / nl-corrections store
# modules used to be imported into the service module's namespace as
# ``_corrections`` / ``_nl_corrections`` so external callers could reach
# them via ``arango_cypher.service._corrections``. Preserve those names.
from .. import corrections as _corrections  # noqa: F401, E402
from .. import nl_corrections as _nl_corrections  # noqa: F401, E402
from .._env import read_arango_password  # noqa: F401  (re-export, used by tests)

# 6) Routes — importing the subpackage runs every endpoint's
#    ``@app.route(...)`` decorator and registers it on
#    :data:`.app.app`.
from . import routes as _routes  # noqa: F401
from .app import (
    _PUBLIC_MODE,
    _cors_credentials,
    _cors_origins,
    _require_analyzer_unless_opted_out,
    _svc_logger,
    app,
)

# 4) HTTP-shape mapping helper — re-exported because
#    ``scripts/benchmark_translate`` imports it via this module path.
from .mapping import _mapping_from_dict

# 3) Pydantic models + length constants. Pure data — no side effects.
from .models import (
    _MAX_AQL_LENGTH,
    _MAX_CYPHER_LENGTH,
    _MAX_FIELD_LENGTH,
    _MAX_NL_QUESTION_LENGTH,
    _MAX_NOTE_LENGTH,
    _MAX_RETRY_HINT_LENGTH,
    _MAX_TURTLE_LENGTH,
    ConnectRequest,
    ConnectResponse,
    CorrectionRequest,
    ErrorResponse,
    ExecuteAqlRequest,
    ExecuteRequest,
    ExecuteResponse,
    NL2AqlRequest,
    NL2CypherRequest,
    NLCorrectionRequest,
    NLSuggestRequest,
    OwlExportRequest,
    OwlImportRequest,
    SuggestIndexesRequest,
    TenantContextPayload,
    ToolCallRequest,
    TranslateRequest,
    TranslateResponse,
    ValidateRequest,
    ValidateResponse,
)

# 5) Process-wide ExtensionRegistry singleton (built at import time so the
#    translate / execute / explain endpoints share one instance).
from .registry import _build_registry, _default_registry

# 2) Security primitives (sessions, rate limit, SSRF, error redaction, 422
#    handler). Imports :data:`.app.app` and registers the exception handler
#    via decorator side effect.
from .security import (
    _AUTH_HEADER_RE,
    _BLOCK_METADATA_HOSTS,
    _BLOCK_METADATA_IPS,
    _COLLECTION_NAME_RE,
    _CRED_RE,
    _HOST_PORT_RE,
    _PRIVATE_NETWORKS,
    _PROXY_ENV_VARS,
    _URL_RE,
    COMPUTE_RATE_LIMIT_PER_MINUTE,
    MAX_SESSIONS,
    NL_RATE_LIMIT_PER_MINUTE,
    SESSION_TTL_SECONDS,
    _check_compute_rate_limit,
    _check_connect_target,
    _check_nl_rate_limit,
    _client_key,
    _compute_bucket,
    _connect_allowed_hosts,
    _describe_connect_error,
    _evict_lru,
    _get_session,
    _nl_bucket,
    _prune_expired,
    _redact_value,
    _require_session_in_public_mode,
    _sanitize_error,
    _sanitize_pydantic_errors,
    _Session,
    _sessions,
    _TokenBucket,
    _translate_errors,
    _validation_error_handler,
    _walk_cause_chain,
)

# 7) UI mount block — must come after routes so its WARNING log lines
#    appear last in startup output (operator triage relies on that order).
from .ui import (
    _UI_DIR,
    _UI_SRC_DIR,
    _check_ui_dist_freshness,
)

__all__ = [
    "app",
    # Re-exported request/response models
    "ConnectRequest",
    "ConnectResponse",
    "CorrectionRequest",
    "ErrorResponse",
    "ExecuteAqlRequest",
    "ExecuteRequest",
    "ExecuteResponse",
    "NL2AqlRequest",
    "NL2CypherRequest",
    "NLCorrectionRequest",
    "NLSuggestRequest",
    "OwlExportRequest",
    "OwlImportRequest",
    "SuggestIndexesRequest",
    "TenantContextPayload",
    "ToolCallRequest",
    "TranslateRequest",
    "TranslateResponse",
    "ValidateRequest",
    "ValidateResponse",
]
