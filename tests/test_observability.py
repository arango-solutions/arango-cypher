"""Tests for the structured observability spine (audit-v2 #6).

Pin the contract on five surfaces:

1. ``CorrelationIdMiddleware`` — mints a UUID4 when ``X-Request-Id`` is
   absent, accepts a safe inbound id, rejects unsafe inbound (newlines,
   excessive length) and falls through to a fresh UUID, and echoes the
   chosen value back in the response header.
2. :data:`correlation_id_var` is populated for the duration of the
   request handler and reset on exit (no leaks across requests).
3. :class:`CorrelationIdLogFilter` injects the contextvar value into
   every :class:`LogRecord` on the ``arango_cypher`` logger tree.
4. :func:`log_endpoint_timing` and :func:`log_llm_call` produce the
   expected structured fields, run string extras through the security
   redactor, and tolerate unknown ``(provider, model)`` for the cost
   lookup without raising.
5. The ``ARANGO_CYPHER_LOG_JSON`` env-gate flips the formatter and
   emits valid single-line JSON.

Tests use the real :class:`fastapi.testclient.TestClient` against the
shared ``app`` so the middleware order (CORS → CorrelationId) is
exercised end-to-end. Logger output is captured via a private
:class:`logging.Handler` rather than ``caplog`` because the latter
attaches at the root logger — the service installs handlers on the
``arango_cypher`` logger and disables propagation, so ``caplog`` would
miss the records.
"""

from __future__ import annotations

import io
import json
import logging
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from arango_cypher.service import app
from arango_cypher.service.observability import (
    CorrelationIdLogFilter,
    _normalise_request_id,
    configure_observability,
    correlation_id_var,
    estimate_llm_cost_usd,
    log_endpoint_timing,
    log_llm_call,
)

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture
def captured_records() -> list[logging.LogRecord]:
    """Attach a list-collecting handler to the ``arango_cypher`` logger.

    We can't use ``caplog`` because :func:`configure_observability` sets
    ``propagate=False`` on the ``arango_cypher`` logger, which blocks
    record propagation to the root handler ``caplog`` instruments.
    """
    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _ListHandler(level=logging.DEBUG)
    # Filter must live on the handler (not the logger) — Python's
    # logging machinery skips parent-logger filters when a record is
    # propagated up from a child logger. See the matching comment in
    # ``configure_observability``.
    handler.addFilter(CorrelationIdLogFilter())
    root = logging.getLogger("arango_cypher")
    root.addHandler(handler)
    try:
        yield records
    finally:
        root.removeHandler(handler)


# ---------------------------------------------------------------------------
# Correlation ID — middleware + contextvar + filter
# ---------------------------------------------------------------------------


class TestCorrelationIdMiddleware:
    def test_mints_uuid4_when_header_absent(self):
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        cid = resp.headers.get("x-request-id")
        assert cid, "Middleware must emit X-Request-Id on every response"
        assert UUID4_RE.fullmatch(cid), f"Expected UUID4, got {cid!r}"

    def test_echoes_safe_inbound_id(self):
        client = TestClient(app)
        inbound = "trace-abc-123"
        resp = client.get("/health", headers={"X-Request-Id": inbound})
        assert resp.status_code == 200
        assert resp.headers.get("x-request-id") == inbound

    def test_rejects_unsafe_inbound_with_fresh_uuid(self):
        # Newline injection is the canonical log-poisoning attempt —
        # the middleware should drop the value and mint a UUID.
        client = TestClient(app)
        resp = client.get("/health", headers={"X-Request-Id": "evil\nINJECTED line"})
        cid = resp.headers.get("x-request-id", "")
        assert cid != "evil\nINJECTED line"
        assert UUID4_RE.fullmatch(cid), f"Expected UUID4 after rejection, got {cid!r}"

    def test_correlation_resets_between_requests(self):
        # Two back-to-back requests with no inbound header must each
        # get their own UUID and the contextvar must reset between them.
        client = TestClient(app)
        a = client.get("/health").headers["x-request-id"]
        b = client.get("/health").headers["x-request-id"]
        assert a != b
        # Outside any request the contextvar default applies.
        assert correlation_id_var.get() == "-"


class TestNormaliseRequestId:
    def test_accepts_safe(self):
        assert _normalise_request_id("abc-123") == "abc-123"

    def test_rejects_empty(self):
        rid = _normalise_request_id("")
        assert UUID4_RE.fullmatch(rid)

    def test_rejects_too_long(self):
        rid = _normalise_request_id("a" * 200)
        assert UUID4_RE.fullmatch(rid)

    def test_rejects_special_chars(self):
        rid = _normalise_request_id("with spaces")
        assert UUID4_RE.fullmatch(rid)


# ---------------------------------------------------------------------------
# CorrelationIdLogFilter
# ---------------------------------------------------------------------------


class TestCorrelationIdLogFilter:
    def test_injects_default_when_no_request(self, captured_records):
        logging.getLogger("arango_cypher.service").info("standalone")
        rec = next(r for r in captured_records if r.getMessage() == "standalone")
        assert rec.correlation_id == "-"

    def test_injects_request_id_inside_handler(self, captured_records):
        # Force a log emit during a request and confirm the filter
        # captures the same id the middleware just minted.
        client = TestClient(app)
        # The /translate endpoint emits an INFO line on every call; we
        # reuse it rather than mounting a test-only route.
        resp = client.post(
            "/translate",
            json={"cypher": "MATCH (n) RETURN n", "mapping": {}},
            headers={"X-Request-Id": "test-cid-xyz"},
        )
        # 422 is fine — the request ran, mapping=empty triggers an
        # HTTPException, but the log record fires first.
        assert resp.status_code in (200, 400, 422)
        assert resp.headers.get("x-request-id") == "test-cid-xyz"
        request_records = [r for r in captured_records if r.correlation_id == "test-cid-xyz"]
        assert request_records, "Expected at least one record carrying the test correlation id"


# ---------------------------------------------------------------------------
# log_endpoint_timing
# ---------------------------------------------------------------------------


class TestLogEndpointTiming:
    def test_emits_structured_fields(self, captured_records):
        log_endpoint_timing("/foo", 12.3, status="ok", rows=42)
        rec = next(r for r in captured_records if r.getMessage() == "endpoint_timing")
        assert rec.endpoint == "/foo"
        assert rec.elapsed_ms == 12.3
        assert rec.status == "ok"
        assert rec.rows == 42

    def test_redacts_string_extras(self, captured_records):
        # _sanitize_error scrubs URLs / credentials. A stray Arango URL
        # passed as an extra must be redacted before emit.
        log_endpoint_timing(
            "/foo",
            1.0,
            note="connect failed against http://root:secret@db.internal:8529",
        )
        rec = next(
            r for r in captured_records
            if r.getMessage() == "endpoint_timing" and getattr(r, "endpoint", "") == "/foo"
        )
        assert "secret" not in rec.note
        assert "db.internal" not in rec.note or rec.note == "connect failed against http://root:secret@db.internal:8529"
        # Whichever redaction shape the helper applies, the credential
        # token must not survive.
        assert "secret" not in rec.note


# ---------------------------------------------------------------------------
# log_llm_call + cost lookup
# ---------------------------------------------------------------------------


class TestLogLlmCall:
    def test_known_model_yields_nonzero_cost(self, captured_records):
        log_llm_call(
            endpoint="/nl2cypher",
            provider="openai",
            model="gpt-4o-mini",
            prompt_tokens=1000,
            completion_tokens=500,
            elapsed_ms=42.0,
        )
        rec = next(r for r in captured_records if r.getMessage() == "llm_call")
        assert rec.provider == "openai"
        assert rec.model == "gpt-4o-mini"
        # gpt-4o-mini: 0.00015 in / 0.0006 out per 1k -> 0.00015 + 0.0003 = 0.00045
        assert rec.cost_usd == pytest.approx(0.00045, rel=1e-6)

    def test_unknown_model_is_zero_not_raise(self, captured_records):
        log_llm_call(
            endpoint="/nl2cypher",
            provider="openai",
            model="gpt-99-nonexistent",
            prompt_tokens=1000,
            completion_tokens=500,
        )
        rec = next(
            r for r in captured_records
            if r.getMessage() == "llm_call" and getattr(r, "model", "") == "gpt-99-nonexistent"
        )
        assert rec.cost_usd == 0.0

    def test_cost_estimator_unknown_returns_zero(self):
        assert estimate_llm_cost_usd("provider-x", "model-y", 1000, 1000) == 0.0

    def test_rule_based_call_logs_dash(self, captured_records):
        log_llm_call(
            endpoint="/nl2cypher",
            provider=None,
            model=None,
            prompt_tokens=0,
            completion_tokens=0,
            method="rule_based",
        )
        rec = next(
            r for r in captured_records
            if r.getMessage() == "llm_call" and getattr(r, "method", "") == "rule_based"
        )
        assert rec.provider == "-"
        assert rec.model == "-"
        assert rec.cost_usd == 0.0


# ---------------------------------------------------------------------------
# JSON formatter (env-gated)
# ---------------------------------------------------------------------------


class TestJsonFormatter:
    def test_json_mode_emits_parseable_lines(self, monkeypatch):
        # Reconfigure with JSON mode and a stream we can read back.
        monkeypatch.setenv("ARANGO_CYPHER_LOG_JSON", "1")
        configure_observability(force=True)

        root = logging.getLogger("arango_cypher")
        # Locate the StreamHandler installed by configure_observability
        # and swap its stream for a StringIO so we can assert on output.
        handler = next(h for h in root.handlers if isinstance(h, logging.StreamHandler))
        buf = io.StringIO()
        handler.stream = buf

        log_endpoint_timing("/foo", 7.5, status="ok", rows=3)
        line = buf.getvalue().strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["endpoint"] == "/foo"
        assert payload["elapsed_ms"] == 7.5
        assert payload["status"] == "ok"
        assert payload["rows"] == 3
        assert payload["msg"] == "endpoint_timing"
        # correlation_id falls back to "-" outside a request context.
        assert payload["correlation_id"] == "-"

        # Reset back to the default formatter so subsequent tests aren't
        # affected by the JSON formatter still being installed.
        monkeypatch.delenv("ARANGO_CYPHER_LOG_JSON", raising=False)
        configure_observability(force=True)


# ---------------------------------------------------------------------------
# configure_observability idempotency
# ---------------------------------------------------------------------------


class TestConfigureObservability:
    def test_no_duplicate_handlers_on_repeated_call(self):
        configure_observability()
        root = logging.getLogger("arango_cypher")
        before = len([h for h in root.handlers if isinstance(h, logging.StreamHandler)])
        configure_observability()
        configure_observability()
        after = len([h for h in root.handlers if isinstance(h, logging.StreamHandler)])
        assert before == after, "Repeated configure_observability() must be idempotent"

    def test_force_resets_handlers_and_filters(self, monkeypatch):
        configure_observability(force=True)
        root = logging.getLogger("arango_cypher")
        # Single StreamHandler (the one we just installed) and exactly
        # one CorrelationIdLogFilter — both on the logger and on its
        # handler (the helper double-installs intentionally; see the
        # comment in ``configure_observability``).
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
        correlation_filters = [f for f in root.filters if isinstance(f, CorrelationIdLogFilter)]
        assert len(stream_handlers) == 1
        assert len(correlation_filters) == 1
        # Handler also carries the filter — propagated records from
        # ``arango_cypher.service.*`` rely on the handler-level filter.
        handler_filters = [
            f for f in stream_handlers[0].filters if isinstance(f, CorrelationIdLogFilter)
        ]
        assert len(handler_filters) == 1


def test_uuid4_validation_self_check():
    # Sanity: confirm the regex pattern accepts genuine UUID4s and
    # rejects UUID1s. Catches a copy-paste regression in
    # ``UUID4_RE`` itself if the fixture is ever revised.
    assert UUID4_RE.fullmatch(str(uuid.uuid4()))
    assert UUID4_RE.fullmatch(str(uuid.uuid1())) is None
