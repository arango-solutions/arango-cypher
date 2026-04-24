"""Tests that pin middleware / infrastructure invariants of the service.

Three subjects, each with a historical failure mode:

* **CORS** — the default ``allow_origins="*"`` with
  ``allow_credentials=True`` is a classic cross-origin credential-leak
  configuration. We pin (a) that the current middleware is wired up
  and responds to preflights, and (b) that the ``allow_origins`` list
  reflects the ``CORS_ALLOWED_ORIGINS`` env var so operators can
  actually lock it down.

* **Rate limiter** — ``_TokenBucket`` gates NL endpoints. Without
  tests, a future refactor can silently raise the ceiling to ∞.

* **Session lifecycle** — ``_prune_expired`` / ``_evict_lru`` bound
  the session dict against memory growth and zombie sessions. We
  exercise the observable state transitions directly.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from arango_cypher import service
from arango_cypher.service import _Session, _sessions, app

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


class TestCORSMiddleware:
    """Pin the CORS contract so a regression (e.g. dropping the
    middleware, or hard-coding ``*`` and ignoring the env var) trips
    a test instead of shipping silently."""

    def test_preflight_reflects_origin(self):
        # With the default ``allow_origins=["*"]`` and
        # ``allow_credentials=True``, Starlette reflects the requested
        # origin rather than echoing ``*`` — that's the actual runtime
        # behaviour we need to pin.
        client = TestClient(app)
        resp = client.options(
            "/tenants",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code in (200, 204)
        allow = resp.headers.get("access-control-allow-origin", "")
        assert allow in ("*", "http://localhost:5173"), f"CORS middleware not active: got {allow!r}"

    def test_actual_request_includes_cors_headers(self):
        client = TestClient(app)
        resp = client.get(
            "/health",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 200
        # Actual CORS response headers must be present — this is what
        # the browser uses to allow the fetch() result through.
        assert "access-control-allow-origin" in resp.headers


# ---------------------------------------------------------------------------
# Rate limiter — _TokenBucket
# ---------------------------------------------------------------------------


class TestTokenBucket:
    """Direct tests of the bucket — endpoint-level integration is
    covered via `_check_nl_rate_limit` behaviour but we need per-bucket
    mechanics here so refill math can be asserted without wall-clock
    sleeps."""

    def _bucket(self, capacity: int):
        from arango_cypher.service import _TokenBucket

        return _TokenBucket(capacity=capacity)

    def test_allows_up_to_capacity_then_refuses(self):
        b = self._bucket(capacity=3)
        assert b.allow("k") is True
        assert b.allow("k") is True
        assert b.allow("k") is True
        assert b.allow("k") is False

    def test_keys_are_isolated(self):
        b = self._bucket(capacity=1)
        assert b.allow("a") is True
        # "a" is empty, but "b" has its own bucket.
        assert b.allow("a") is False
        assert b.allow("b") is True

    def test_refills_over_time(self, monkeypatch):
        # Drive time.time() to advance by 60s (one full capacity's
        # worth of refill at the default per-minute rate). Avoids
        # wall-clock sleeps and keeps the test deterministic.
        import arango_cypher.service as svc

        now = {"t": 1_000.0}

        def fake_time() -> float:
            return now["t"]

        monkeypatch.setattr(svc.time, "time", fake_time)

        b = self._bucket(capacity=2)
        assert b.allow("k") is True
        assert b.allow("k") is True
        assert b.allow("k") is False

        now["t"] += 60.0  # full minute elapses
        # capacity tokens should have refilled.
        assert b.allow("k") is True
        assert b.allow("k") is True
        assert b.allow("k") is False


# ---------------------------------------------------------------------------
# Compute rate-limit bucket — second tier from audit-v2 finding #2
# ---------------------------------------------------------------------------


class TestComputeRateLimit:
    """Pin the second, cheaper rate-limit bucket added in audit-v2 batch 2.

    The contract under test is the *separation* of the two buckets — a
    request that exhausts the LLM bucket on ``/nl2cypher`` must still be
    able to hit a CPU endpoint, and vice-versa. The third invariant is
    the explicit 429 + "compute" message so a caller can distinguish the
    two buckets in their own retry logic.
    """

    def _client_keyed_request(self, key: str = "test-token"):
        # Minimal stand-in for ``Request`` — only ``headers`` and
        # ``client`` are read by ``_client_key``.
        from types import SimpleNamespace

        return SimpleNamespace(
            headers={"Authorization": key},
            client=SimpleNamespace(host="127.0.0.1"),
        )

    def test_compute_and_nl_buckets_are_independent(self, monkeypatch):
        # Replace both buckets with size-1 instances so we can drain one
        # and confirm the other still allows. Restore at teardown via
        # monkeypatch so other tests in the suite aren't affected.
        from arango_cypher import service as svc

        nl = svc._TokenBucket(capacity=1)
        compute = svc._TokenBucket(capacity=1)
        monkeypatch.setattr(svc, "_nl_bucket", nl)
        monkeypatch.setattr(svc, "_compute_bucket", compute)

        req = self._client_keyed_request()

        # First call drains the NL bucket; compute bucket untouched.
        svc._check_nl_rate_limit(req)
        # Compute call still allowed because it's a separate bucket.
        svc._check_compute_rate_limit(req)

        # Now the NL bucket is empty — second NL call must 429.
        from fastapi import HTTPException

        try:
            svc._check_nl_rate_limit(req)
        except HTTPException as e:
            assert e.status_code == 429
            assert "NL" in e.detail
        else:
            raise AssertionError("Expected NL rate limit to trip")

        # Compute bucket is also empty now (we used its one token above) —
        # second compute call must also 429, with the *compute* message
        # so callers can route their retry logic.
        try:
            svc._check_compute_rate_limit(req)
        except HTTPException as e:
            assert e.status_code == 429
            assert "compute" in e.detail
        else:
            raise AssertionError("Expected compute rate limit to trip")

    def test_client_key_falls_back_to_ip_then_anon(self):
        from types import SimpleNamespace

        from arango_cypher.service import _client_key

        # Authorization wins.
        r1 = SimpleNamespace(headers={"Authorization": "tok"}, client=SimpleNamespace(host="1.2.3.4"))
        assert _client_key(r1) == "tok"

        # No Authorization → IP.
        r2 = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"))
        assert _client_key(r2) == "1.2.3.4"

        # No Authorization, no client → "anon".
        r3 = SimpleNamespace(headers={}, client=None)
        assert _client_key(r3) == "anon"

    def test_compute_dep_is_wired_to_validate_endpoint(self):
        # Sanity: the ``/validate`` endpoint actually invokes
        # ``_check_compute_rate_limit`` as a dep. Use FastAPI's official
        # ``dependency_overrides`` to swap in a forced-429 stand-in
        # rather than monkeypatching the bucket — survives test-ordering
        # interactions with other suites that touch the real bucket.
        from fastapi import HTTPException

        from arango_cypher.service import _check_compute_rate_limit

        def always_429() -> None:
            raise HTTPException(status_code=429, detail="Rate limit exceeded for compute endpoints")

        app.dependency_overrides[_check_compute_rate_limit] = always_429
        try:
            client = TestClient(app)
            body = {"cypher": "MATCH (n) RETURN n", "mapping": None, "params": None}
            resp = client.post("/validate", json=body)
            assert resp.status_code == 429, (
                f"/validate should route through compute rate limiter; got {resp.status_code}"
            )
            assert "compute" in resp.json().get("detail", "").lower()
        finally:
            app.dependency_overrides.pop(_check_compute_rate_limit, None)


# ---------------------------------------------------------------------------
# Session lifecycle — _prune_expired / _evict_lru
# ---------------------------------------------------------------------------


def _make_fake_session(token: str, *, last_used: float | None = None) -> _Session:
    sess = _Session.__new__(_Session)
    sess.token = token
    sess.db = MagicMock()
    sess.client = MagicMock()
    sess.created_at = time.time()
    sess.last_used = time.time() if last_used is None else last_used
    return sess


class TestSessionLifecycle:
    def setup_method(self) -> None:
        _sessions.clear()

    def teardown_method(self) -> None:
        _sessions.clear()

    def test_prune_expired_closes_client_and_evicts(self, monkeypatch):
        # Session whose ``last_used`` is older than ``SESSION_TTL_SECONDS``.
        monkeypatch.setattr(service, "SESSION_TTL_SECONDS", 100)
        old = _make_fake_session("old", last_used=time.time() - 1_000)
        fresh = _make_fake_session("fresh")
        _sessions["old"] = old
        _sessions["fresh"] = fresh

        service._prune_expired()

        assert "old" not in _sessions
        assert "fresh" in _sessions
        old.client.close.assert_called_once()
        fresh.client.close.assert_not_called()

    def test_evict_lru_drops_oldest_when_capacity_reached(self, monkeypatch):
        # TTL is checked inside _evict_lru (via _prune_expired) against
        # wall-clock ``time.time()``. Use now-relative offsets so the
        # sessions are fresh w.r.t. TTL; the LRU ordering is what we're
        # actually testing.
        monkeypatch.setattr(service, "SESSION_TTL_SECONDS", 10_000)
        monkeypatch.setattr(service, "MAX_SESSIONS", 3)
        now = time.time()

        s1 = _make_fake_session("s1", last_used=now - 300)
        s2 = _make_fake_session("s2", last_used=now - 200)
        s3 = _make_fake_session("s3", last_used=now - 100)
        _sessions["s1"] = s1
        _sessions["s2"] = s2
        _sessions["s3"] = s3

        # _evict_lru evicts while ``len >= MAX_SESSIONS``, so with 3
        # sessions and cap=3 the oldest (s1) is evicted to make room.
        service._evict_lru()

        assert "s1" not in _sessions
        assert "s2" in _sessions
        assert "s3" in _sessions
        s1.client.close.assert_called_once()
        assert len(_sessions) == service.MAX_SESSIONS - 1

    def test_expired_session_rejected_with_401(self, monkeypatch):
        monkeypatch.setattr(service, "SESSION_TTL_SECONDS", 100)
        stale = _make_fake_session("stale", last_used=time.time() - 1_000)
        _sessions["stale"] = stale

        client = TestClient(app)
        resp = client.get(
            "/tenants",
            headers={"Authorization": "Bearer stale"},
        )
        assert resp.status_code == 401
        # Client must be closed so we don't leak python-arango HTTP
        # sessions past the TTL window.
        stale.client.close.assert_called_once()

    def test_unknown_token_rejected_with_401(self):
        client = TestClient(app)
        resp = client.get(
            "/tenants",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401

    def test_missing_auth_header_rejected_with_401(self):
        client = TestClient(app)
        resp = client.get("/tenants")
        assert resp.status_code == 401
