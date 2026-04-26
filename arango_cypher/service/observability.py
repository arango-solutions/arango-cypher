"""Structured observability for ``arango_cypher.service``.

Closes audit-v2 finding #6 (``docs/audits/2026-04-28-post-hardening-audit.md``
§6 — "structured logging is effectively absent"). The pre-PR baseline
was 7 ``logging.*`` calls across the entire service package and zero
request-correlation, zero per-endpoint timing in the log stream, zero
LLM cost telemetry.

Three concerns, one module:

1. **Per-request correlation ID.** :class:`CorrelationIdMiddleware` mints
   a UUID4 on absent ``X-Request-Id``, echoes the inbound value when
   present (sanitised to ``[A-Za-z0-9-]{1,128}`` so a hostile caller
   can't poison the log stream with newlines / shell escapes), stores
   it in :data:`correlation_id_var` (a :class:`contextvars.ContextVar`)
   so every ``logging.*`` call inside the request handler — sync or
   async, in this package or any downstream module — picks it up via
   :class:`CorrelationIdLogFilter` without a single signature change.
   The header is echoed back on the response so callers can correlate
   their client-side logs with the server trail.

2. **:func:`log_endpoint_timing`.** Single helper called by every
   endpoint on the success path. Emits one INFO record with
   ``endpoint``, ``elapsed_ms``, ``status`` (default ``"ok"``), and
   any caller-supplied ``extras`` (e.g. ``rows`` for ``/execute``,
   ``cypher_len`` for ``/translate``). Failures are still surfaced via
   the existing ``HTTPException`` path; a caller that wants an explicit
   error line can pass ``status="error"`` and the relevant ``extras``.

3. **:func:`log_llm_call`.** Emits one INFO record per LLM round-trip
   with ``(provider, model, prompt_tokens, completion_tokens,
   cached_tokens, cost_usd, elapsed_ms)``. Cost is a best-effort lookup
   against :data:`_PRICING_PER_1K_TOKENS` (manually maintained from the
   provider pricing pages); unknown ``(provider, model)`` returns
   ``0.0`` so a new model name doesn't crash the log line — operators
   should treat ``cost_usd=0.0`` as "unpriced", not free.

Output format defaults to plain ``key=value`` pairs::

    2026-05-03T18:14:22.011Z INFO arango_cypher.service correlation_id=4f9c… endpoint=/translate elapsed_ms=12.4 status=ok cypher_len=128

Set ``ARANGO_CYPHER_LOG_JSON=1`` to flip to single-line JSON for
log-aggregation pipelines (Datadog / Loki / Splunk):

    {"ts": "2026-05-03T18:14:22.011Z", "level": "INFO", "logger": "arango_cypher.service", "correlation_id": "4f9c…", "msg": "endpoint_timing", "endpoint": "/translate", "elapsed_ms": 12.4, "status": "ok", "cypher_len": 128}

All ``extras`` values are run through
:func:`arango_cypher.service.security._sanitize_error` (URL / IP /
credential redaction) before emit so a stray identifier can't leak
through the new surface.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

# ---------------------------------------------------------------------------
# Correlation ID — contextvar + ASGI middleware + logging filter
# ---------------------------------------------------------------------------

# ``ContextVar`` rather than threadlocal because FastAPI runs handlers in an
# asyncio event loop where threadlocals collapse work across tasks. The
# default value is ``"-"`` so log lines emitted *outside* a request (startup
# guards, eviction prune, etc.) still render cleanly rather than blowing up
# with a ``LookupError`` from an unset contextvar.
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

# Inbound ``X-Request-Id`` is sanitised against this character class before
# being stored — without it, a hostile caller could send a newline-laden
# value and inject fake records into the log stream. Conservative: alnum +
# dash, length-bounded so the log line stays readable. Anything that fails
# the match falls through to a freshly-minted UUID4.
_INBOUND_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")


def _normalise_request_id(raw: str | None) -> str:
    """Return ``raw`` if it matches the safe character class, else mint a UUID4.

    Centralised so :class:`CorrelationIdMiddleware` and
    :func:`tests.test_observability` agree on the validation contract.
    """
    if raw and _INBOUND_REQUEST_ID_RE.fullmatch(raw):
        return raw
    return str(uuid.uuid4())


class CorrelationIdMiddleware:
    """ASGI middleware: mint or accept ``X-Request-Id``, propagate via contextvar.

    Implemented as a raw ASGI callable rather than a Starlette
    :class:`BaseHTTPMiddleware` subclass for two reasons: (a) the latter
    eagerly buffers the response body, breaking streaming endpoints; and
    (b) the ContextVar's :meth:`set` token has to be released after the
    response is fully sent, which is awkward inside the BaseHTTPMiddleware
    request/response loop. The raw-ASGI shape keeps the contextvar's
    lifetime exactly equal to the request lifetime.

    Echoes the (validated) request id back on the response in the same
    ``X-Request-Id`` header so client-side logs can correlate with the
    server trail.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]):
        self.app = app

    async def __call__(
        self, scope: dict[str, Any], receive: Callable, send: Callable
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound_raw: str | None = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                try:
                    inbound_raw = value.decode("latin-1")
                except UnicodeDecodeError:
                    inbound_raw = None
                break
        request_id = _normalise_request_id(inbound_raw)
        token = correlation_id_var.set(request_id)

        async def _send_with_header(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, _send_with_header)
        finally:
            correlation_id_var.reset(token)


class CorrelationIdLogFilter(logging.Filter):
    """Inject :data:`correlation_id_var` into every :class:`LogRecord`.

    Attached to the root ``arango_cypher`` logger by
    :func:`configure_observability` so every downstream module
    (``arango_cypher.service.*``, ``arango_cypher.nl2cypher.*``,
    ``arango_cypher.schema_acquire``, …) inherits the filter without
    each one having to reach for it explicitly.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True


# ---------------------------------------------------------------------------
# Formatters — key=value (default) and JSON (env-gated)
# ---------------------------------------------------------------------------


class _KeyValueFormatter(logging.Formatter):
    """Emit ``ts level logger correlation_id=… msg=… k=v k=v …`` lines.

    Designed for ``tail -f``-style operator triage: the message slot
    stays the first key-value pair after the prefix so a casual eye-scan
    works, and any structured fields (``endpoint``, ``elapsed_ms``,
    LLM telemetry) trail in stable order. ``ts`` is ISO-8601 UTC with
    millisecond precision so log rotation and de-duplication tools
    handle the timestamps unambiguously.

    Reserved record attributes (the standard logging ones plus
    ``correlation_id`` injected by :class:`CorrelationIdLogFilter`) are
    not re-emitted as ``extras`` — only the ``extras`` dict explicitly
    passed via ``logger.info(..., extra={...})`` shows up in the tail.
    """

    _RESERVED = frozenset(
        # The default LogRecord attributes we don't want to re-emit as
        # structured fields. Keep this list in sync with the Python
        # ``logging`` module (3.11 baseline) — anything new on a future
        # Python release just shows up as an extra and we live with it.
        {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "message",
            "module",
            "msecs",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "taskName",
            "correlation_id",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        ts = f"{ts}.{int(record.msecs):03d}Z"
        cid = getattr(record, "correlation_id", "-")
        msg = record.getMessage()
        prefix = (
            f"{ts} {record.levelname} {record.name} correlation_id={cid} msg={msg!r}"
        )
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in self._RESERVED and not k.startswith("_")
        }
        if not extras:
            return prefix
        kv = " ".join(f"{k}={_format_kv_value(v)}" for k, v in sorted(extras.items()))
        return f"{prefix} {kv}"


def _format_kv_value(v: Any) -> str:
    """Render a structured value safely for the ``key=value`` formatter.

    Strings get :func:`json.dumps`'d so embedded spaces / quotes don't
    break the parse contract; numbers and bools are stringified; complex
    objects fall through to ``repr`` and are bracketed in quotes to
    survive a downstream regex split on whitespace.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    return json.dumps(repr(v), ensure_ascii=False)


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per record, stable key order.

    Same reserved-attribute filter as :class:`_KeyValueFormatter` so the
    two formatters carry identical structured surface — the choice
    between them is purely an output-shape decision driven by the
    log-aggregation pipeline (tail vs. Loki/Datadog/Splunk).
    """

    _RESERVED = _KeyValueFormatter._RESERVED

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        ts = f"{ts}.{int(record.msecs):03d}Z"
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith("_"):
                continue
            payload[k] = _json_safe(v)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_safe(v: Any) -> Any:
    """Coerce a value into something :func:`json.dumps` accepts losslessly."""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_json_safe(item) for item in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(item) for k, item in v.items()}
    return repr(v)


# ---------------------------------------------------------------------------
# configure_observability — idempotent setup hook called from service.app
# ---------------------------------------------------------------------------

# Module-level guard: installing two filters / two handlers on the
# ``arango_cypher`` root logger would duplicate every record. Tests reload
# ``arango_cypher.service`` repeatedly, so configuration has to be safe to
# call N times.
_CONFIGURED = False


def configure_observability(*, force: bool = False) -> None:
    """Install :class:`CorrelationIdLogFilter` + the chosen formatter.

    Idempotent — second and subsequent calls are no-ops unless
    ``force=True``. Tests pass ``force=True`` to exercise the setup
    path against a freshly-cleared root logger.

    Reads two env vars:

    * ``ARANGO_CYPHER_LOG_LEVEL`` — log level for the ``arango_cypher``
      root logger (default ``INFO``). Endpoint timing and LLM call lines
      are emitted at ``INFO``; flipping to ``WARNING`` silences them
      while keeping CORS / SSRF / 422 warnings.
    * ``ARANGO_CYPHER_LOG_JSON`` — when ``"1" / "true" / "yes"`` (case
      insensitive), uses :class:`_JsonFormatter`. Otherwise the
      key=value formatter.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    root = logging.getLogger("arango_cypher")
    if force:
        for h in list(root.handlers):
            root.removeHandler(h)
        for f in list(root.filters):
            root.removeFilter(f)

    # The filter is attached to the handler rather than the logger
    # because Python's logging machinery only runs a logger's filters
    # on records *originating* from that logger — records propagated
    # up from child loggers (``arango_cypher.service.*``) skip the
    # parent's filter list and go straight to its handlers. Attaching
    # at the handler level makes the correlation_id show up on every
    # record routed through our StreamHandler regardless of where the
    # ``logging.getLogger(...)`` call lives.
    correlation_filter = CorrelationIdLogFilter()
    root.addFilter(correlation_filter)

    json_mode = os.getenv("ARANGO_CYPHER_LOG_JSON", "").lower() in ("1", "true", "yes")
    formatter: logging.Formatter = (
        _JsonFormatter() if json_mode else _KeyValueFormatter()
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(correlation_filter)
    root.addHandler(handler)

    level_name = os.getenv("ARANGO_CYPHER_LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    # Propagation is left on so test fixtures (pytest's ``caplog``,
    # downstream test suites that attach a handler to the root logger)
    # still see our records. In production the Python root logger
    # typically has no handlers attached — uvicorn configures
    # ``uvicorn.access`` / ``uvicorn.error`` directly, not root — so
    # there's no duplication risk under the standard deploy. Operators
    # who *do* call :func:`logging.basicConfig` on the root logger and
    # want to suppress duplicate output should set
    # ``logging.getLogger("arango_cypher").propagate = False`` after
    # importing the service.

    _CONFIGURED = True


# ---------------------------------------------------------------------------
# log_endpoint_timing — single line per endpoint success
# ---------------------------------------------------------------------------

_endpoint_logger = logging.getLogger("arango_cypher.service.endpoint")


def log_endpoint_timing(
    endpoint: str,
    elapsed_ms: float,
    *,
    status: str = "ok",
    **extras: Any,
) -> None:
    """Emit one INFO line for an endpoint round-trip.

    Caller contract:

    * ``endpoint`` — the route path (``"/translate"``,
      ``"/schema/introspect"``, …). Stable across the call site so log
      aggregation can group by endpoint without parsing the access log.
    * ``elapsed_ms`` — wall-clock milliseconds, rounded to 1 decimal
      (matches the value already returned in the response body where
      applicable).
    * ``status`` — ``"ok"`` (default), ``"error"``, or any other
      caller-defined token. The error path is *not* automatic; callers
      that catch and re-raise should pass ``status="error"`` explicitly
      so log-volume stays predictable.
    * ``extras`` — endpoint-specific structured fields. Strings are
      sanitised through :func:`arango_cypher.service.security._sanitize_error`
      before emit so a stray URL / credential can't leak via this
      surface; non-string values pass through unchanged.

    Reserved keys (``correlation_id``, ``msg``, ``level``, …) are
    silently dropped to keep the formatter contract clean — see
    :data:`_KeyValueFormatter._RESERVED`.
    """
    safe_extras = {
        k: _sanitize_extra_value(v)
        for k, v in extras.items()
        if k not in _KeyValueFormatter._RESERVED
    }
    _endpoint_logger.info(
        "endpoint_timing",
        extra={
            "endpoint": endpoint,
            "elapsed_ms": elapsed_ms,
            "status": status,
            **safe_extras,
        },
    )


def _sanitize_extra_value(v: Any) -> Any:
    """Run string values through the existing service redactor.

    Non-strings pass through. The lazy import avoids the
    ``service.security → service.observability → service.security``
    circular at package init (security is imported into the package
    namespace before observability, but observability is imported by
    app.py *before* security; reading via :data:`sys.modules` at call
    time sidesteps the ordering question entirely).
    """
    if not isinstance(v, str):
        return v
    import sys

    sec = sys.modules.get("arango_cypher.service.security")
    if sec is not None:
        sanitiser = getattr(sec, "_sanitize_error", None)
        if sanitiser is not None:
            try:
                return sanitiser(v)
            except Exception:
                # Sanitiser failure must not crash the log line —
                # preserve raw value and accept the (unlikely) leak risk.
                return v
    return v


# ---------------------------------------------------------------------------
# log_llm_call — one line per LLM round-trip with token + cost telemetry
# ---------------------------------------------------------------------------

_llm_logger = logging.getLogger("arango_cypher.service.llm")

# Per-(provider, model) USD cost per 1k input / output tokens. Manually
# maintained from the provider pricing pages — last refreshed
# 2026-05-03 against:
#
# * OpenAI:    https://openai.com/pricing
# * Anthropic: https://www.anthropic.com/pricing
# * OpenRouter: pass-through; we don't price these (the OpenRouter
#   markup is small and per-model, not worth tracking here — log
#   ``cost_usd=0.0`` and let downstream aggregation use OpenRouter's
#   own usage API for precise figures).
#
# Unknown ``(provider, model)`` pairs return ``0.0`` rather than raising
# so a new model name added upstream doesn't crash the request — the
# audit calls for ``cost`` on the log line, not for cost accuracy. Treat
# ``cost_usd=0.0`` as "unpriced" not "free" when scanning logs.
_PRICING_PER_1K_TOKENS: dict[tuple[str, str], tuple[float, float]] = {
    # (provider, model) -> (input_$/1k, output_$/1k)
    ("openai", "gpt-4o"): (0.0025, 0.010),
    ("openai", "gpt-4o-mini"): (0.00015, 0.0006),
    ("openai", "gpt-4-turbo"): (0.010, 0.030),
    ("anthropic", "claude-3-5-sonnet-20241022"): (0.003, 0.015),
    ("anthropic", "claude-3-5-sonnet-latest"): (0.003, 0.015),
    ("anthropic", "claude-3-5-haiku-20241022"): (0.0008, 0.004),
    ("anthropic", "claude-3-5-haiku-latest"): (0.0008, 0.004),
    ("anthropic", "claude-3-opus-20240229"): (0.015, 0.075),
}


def estimate_llm_cost_usd(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Return USD cost estimate, or ``0.0`` for unknown ``(provider, model)``.

    Lookup keyed by lowercased ``(provider, model)`` so casing
    differences between the env var (``LLM_PROVIDER=OpenAI``) and the
    pricing table key don't cause a miss.
    """
    key = (provider.lower(), model.lower())
    if key not in _PRICING_PER_1K_TOKENS:
        return 0.0
    input_rate, output_rate = _PRICING_PER_1K_TOKENS[key]
    return round(
        (prompt_tokens / 1000.0) * input_rate
        + (completion_tokens / 1000.0) * output_rate,
        6,
    )


def log_llm_call(
    *,
    endpoint: str,
    provider: str | None,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    elapsed_ms: float | None = None,
    method: str | None = None,
    **extras: Any,
) -> None:
    """Emit one INFO line for an LLM round-trip.

    Called from ``/nl2cypher`` and ``/nl2aql`` after the ``nl_to_*``
    function returns. Provider / model are nullable so the rule-based
    fallback path (which does not hit an LLM) can call the helper too
    with ``method="rule_based"`` and zero tokens — gives a uniform
    grep target for "every NL request, regardless of LLM-or-not".

    Cost is computed via :func:`estimate_llm_cost_usd`; unknown models
    log ``cost_usd=0.0``.
    """
    cost_usd = (
        estimate_llm_cost_usd(provider, model, prompt_tokens, completion_tokens)
        if provider and model
        else 0.0
    )
    _llm_logger.info(
        "llm_call",
        extra={
            "endpoint": endpoint,
            "provider": provider or "-",
            "model": model or "-",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
            "cost_usd": cost_usd,
            "elapsed_ms": elapsed_ms if elapsed_ms is not None else 0.0,
            "method": method or "-",
            **{k: _sanitize_extra_value(v) for k, v in extras.items()},
        },
    )


def current_llm_provider_and_model() -> tuple[str | None, str | None]:
    """Best-effort read of the configured LLM provider + model.

    Reads from environment variables rather than instantiating a provider
    (which would consume API tokens for nothing). Returns
    ``(None, None)`` when neither is set — the caller logs ``-`` in
    that case.

    Provider preference: ``LLM_PROVIDER`` env (canonical, used by
    :func:`arango_cypher.nl2cypher.providers.get_default_provider`),
    falling back to inferring from which model env is set.
    """
    provider = os.getenv("LLM_PROVIDER", "").strip().lower() or None
    model: str | None = None
    if provider == "anthropic":
        model = os.getenv("ANTHROPIC_MODEL") or os.getenv("OPENAI_MODEL")
    elif provider == "openrouter":
        model = os.getenv("OPENROUTER_MODEL") or os.getenv("OPENAI_MODEL")
    elif provider == "openai":
        model = os.getenv("OPENAI_MODEL")
    else:
        # Provider unspecified — try to infer from which model env is set.
        if os.getenv("ANTHROPIC_MODEL"):
            provider, model = "anthropic", os.getenv("ANTHROPIC_MODEL")
        elif os.getenv("OPENROUTER_MODEL"):
            provider, model = "openrouter", os.getenv("OPENROUTER_MODEL")
        elif os.getenv("OPENAI_MODEL"):
            provider, model = "openai", os.getenv("OPENAI_MODEL")
    return provider, model


# ---------------------------------------------------------------------------
# Convenience timer — used by routes that didn't previously track elapsed_ms
# ---------------------------------------------------------------------------


class _EndpointTimer:
    """Context manager that times a block and emits one log line on exit.

    Used by routes that don't return ``elapsed_ms`` in the response
    body. Captures wall-clock at ``__enter__``, computes elapsed at
    ``__exit__``, and calls :func:`log_endpoint_timing` with the
    accumulated extras. On exception, ``status`` flips to ``"error"``
    automatically and the exception type name is added under
    ``error_type`` so log scans can group by failure mode without
    re-parsing the message.
    """

    __slots__ = ("endpoint", "extras", "_start", "status")

    def __init__(self, endpoint: str, **extras: Any):
        self.endpoint = endpoint
        self.extras: dict[str, Any] = dict(extras)
        self._start = 0.0
        self.status = "ok"

    def __enter__(self) -> _EndpointTimer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: Any) -> None:
        elapsed_ms = round((time.perf_counter() - self._start) * 1000, 1)
        if exc_type is not None:
            self.status = "error"
            self.extras.setdefault("error_type", exc_type.__name__)
        log_endpoint_timing(
            self.endpoint, elapsed_ms, status=self.status, **self.extras
        )

    def add(self, **extras: Any) -> None:
        """Attach extras inside the ``with`` block (e.g. ``timer.add(rows=42)``).

        Useful when a caller computes the structured field *during* the
        block (rowcount, AQL length, …) and needs to associate it with
        the timing line on exit.
        """
        self.extras.update(extras)


def time_endpoint(endpoint: str, **extras: Any) -> _EndpointTimer:
    """Public name for :class:`_EndpointTimer` — use as a context manager.

    Pattern::

        with time_endpoint("/foo", session_token=token) as t:
            result = do_work()
            t.add(rows=len(result))
        return result
    """
    return _EndpointTimer(endpoint, **extras)
