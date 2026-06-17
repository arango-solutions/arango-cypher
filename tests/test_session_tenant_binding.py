"""Layer 1 / Wave 7 — session-bound ``tenantId`` regression tests.

Pins the multi-tenant Phase 1 invariants from
``docs/multitenant_prd.md`` §4:

* ``/connect`` accepts ``tenantId`` / ``tenantKey`` / ``isAdmin`` and
  validates the tenant exists in the ``Tenant`` collection when one is
  present in the connected database. Unknown tenants are refused with
  HTTP 403 (``error="unknown_tenant"``).
* The created session carries ``tenant_id`` / ``tenant_key`` /
  ``is_admin`` so Layer 5 can verify the bind-var ``@tenantId`` came
  from the authenticated session rather than the request body.
* In *workbench* mode (``ARANGO_CYPHER_WORKBENCH=1``) the
  body-supplied ``tenant_context`` on ``/nl2cypher`` / ``/nl2aql`` is
  honored verbatim.
* In *tenant-user* mode (env unset) a body-supplied ``tenant_context``
  whose value differs from the session-bound tenant is silently
  overridden, and a WARN log records the override for audit.

The fakes here mirror ``test_service_connect_diagnostics.py``'s shape
contract for ``arango_cypher.service.ArangoClient`` and only model the
methods exercised by the routes under test. They never touch a real
ArangoDB server.
"""

from __future__ import annotations

import importlib
import logging
import sys
from contextlib import contextmanager
from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fakes — minimal python-arango shape ``connect.py`` consumes
# ---------------------------------------------------------------------------


class _FakeCollection:
    def __init__(self, docs: dict[str, dict[str, Any]] | None = None):
        self._docs = docs or {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._docs.get(key)


class _FakeDb:
    """In-memory shape-match of ``StandardDatabase`` for connect tests."""

    def __init__(
        self,
        *,
        has_tenant_collection: bool,
        tenants: dict[str, dict[str, Any]] | None = None,
        databases: list[str] | None = None,
    ):
        self._has_tenant = has_tenant_collection
        self._tenant_coll = _FakeCollection(tenants or {})
        self._databases = databases or ["_system"]

    def version(self) -> str:
        return "3.12.0"

    def has_collection(self, name: str) -> bool:
        return name == "Tenant" and self._has_tenant

    def collection(self, name: str) -> _FakeCollection:
        if name == "Tenant":
            return self._tenant_coll
        raise KeyError(name)

    def databases(self) -> list[str]:
        return list(self._databases)


def _make_fake_client(db: _FakeDb):
    """Build a python-arango ``ArangoClient`` test double bound to *db*.

    Calls to ``client.db("_system", ...)`` return a system-DB stub whose
    only role is to enumerate databases for the connect response.
    """

    class _FakeClient:
        def __init__(self, hosts):
            self.hosts = hosts
            self.closed = False

        def db(self, name: str, username: str = "", password: str = ""):
            if name == "_system":
                return _FakeDb(has_tenant_collection=False, databases=db._databases)
            return db

        def close(self):
            self.closed = True

    return _FakeClient


def _fresh_service():
    """Return the *live* ``arango_cypher.service`` module from
    ``sys.modules``, re-importing if a previous test removed it.

    Other test files (notably ``test_service_hardening.py``) reload
    the service module via ``importlib.import_module`` and replace
    ``sys.modules["arango_cypher.service"]``. A top-level
    ``from arango_cypher import service`` captures the pre-reload
    object; this helper re-resolves on every call so our tests always
    patch the same module instance the routes actually import.
    """
    if "arango_cypher.service" not in sys.modules:
        return importlib.import_module("arango_cypher.service")
    return sys.modules["arango_cypher.service"]


@contextmanager
def _patched_arango_client(fake_client_factory):
    """Patch ``arango_cypher.service.ArangoClient`` on *every* live
    package object that holds a reference to it.

    The test_service_hardening fixture reloads the service module via
    ``importlib.import_module``, after which two distinct objects can
    both claim to be ``arango_cypher.service``:

    * ``sys.modules["arango_cypher.service"]`` — the version restored
      by the fixture's teardown (the *saved* original).
    * ``arango_cypher.service`` (attribute on the parent package) —
      the *reloaded* module, which the autouse fixture monkeypatched
      and whose ``ArangoClient`` may still be the test stub.

    The ``/connect`` endpoint does ``from arango_cypher import service
    as _svc``, which reads the parent-package attribute — i.e. the
    reloaded module. To make the test deterministic regardless of which
    test ran before us, we override ``ArangoClient`` on every live
    candidate; cleanup restores the original references.
    """
    parent = sys.modules.get("arango_cypher")
    candidates: list[Any] = []
    sys_mod = sys.modules.get("arango_cypher.service")
    if sys_mod is not None:
        candidates.append(sys_mod)
    parent_attr = getattr(parent, "service", None) if parent is not None else None
    if parent_attr is not None and not any(parent_attr is c for c in candidates):
        candidates.append(parent_attr)

    if not candidates:
        # Force-resolve when neither view exists yet.
        candidates.append(importlib.import_module("arango_cypher.service"))

    saved: list[tuple[Any, Any]] = []
    for mod in candidates:
        saved.append((mod, getattr(mod, "ArangoClient", None)))
        mod.ArangoClient = fake_client_factory
    try:
        yield
    finally:
        for mod, orig in saved:
            if orig is None:
                if hasattr(mod, "ArangoClient"):
                    delattr(mod, "ArangoClient")
            else:
                mod.ArangoClient = orig


def _app():
    """Resolve the FastAPI app from the *current* service module."""
    return _fresh_service().app


# ---------------------------------------------------------------------------
# /connect — Layer 1 tenant validation
# ---------------------------------------------------------------------------


class TestConnectTenantValidation:
    """``/connect`` must validate tenantId against the Tenant collection."""

    def test_connect_with_known_tenant_returns_session_bound_fields(self):
        """Happy path: ``tenantId`` exists → session carries it."""
        fake_db = _FakeDb(
            has_tenant_collection=True,
            tenants={
                "tenant-A-uuid": {"_key": "tenant-A-uuid", "NAME": "Acme"},
            },
        )
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-A-uuid",
                    "tenantKey": "tenant-A-uuid",
                    "isAdmin": False,
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "tenant-A-uuid"
        assert body["tenant_key"] == "tenant-A-uuid"
        assert body["is_admin"] is False
        assert "token" in body and body["token"]

        sess = _fresh_service()._sessions[body["token"]]
        assert sess.tenant_id == "tenant-A-uuid"
        assert sess.tenant_key == "tenant-A-uuid"
        assert sess.is_admin is False

    def test_connect_with_unknown_tenant_refused_403(self):
        """Tenant collection exists but ``tenantKey`` is missing → 403."""
        fake_db = _FakeDb(
            has_tenant_collection=True,
            tenants={"tenant-A-uuid": {"_key": "tenant-A-uuid"}},
        )
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-B-rogue",
                    "tenantKey": "tenant-B-rogue",
                },
            )

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "unknown_tenant"
        assert detail["tenantId"] == "tenant-B-rogue"
        assert detail["tenantKey"] == "tenant-B-rogue"

    def test_connect_without_tenant_collection_accepts_id_verbatim(self):
        """Single-tenant DB (no ``Tenant`` collection): ``/connect`` accepts
        any ``tenantId`` verbatim. Layer 5 will refuse tenant-scoped reads
        downstream — see ``test_tenant_plan_validator.py``.
        """
        fake_db = _FakeDb(has_tenant_collection=False)
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-anything",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "tenant-anything"
        assert body["tenant_key"] == "tenant-anything"

    def test_connect_without_tenant_id_yields_no_binding(self):
        """No ``tenantId`` supplied → session.tenant_id is ``None``.

        This is the pre-Wave-7 baseline behaviour, preserved for
        single-tenant / workbench deployments. Layer 5 sees a
        ``None`` tenant_id and refuses any tenant-scoped read; queries
        over purely satellite / global collections still execute.
        """
        fake_db = _FakeDb(has_tenant_collection=True, tenants={})
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] is None
        assert body["tenant_key"] is None
        assert body["is_admin"] is False

        sess = _fresh_service()._sessions[body["token"]]
        assert sess.tenant_id is None
        assert sess.tenant_key is None
        assert sess.is_admin is False

    def test_connect_admin_flag_persists_on_session(self):
        """``isAdmin=true`` is mirrored on the created session.

        Admin sessions still go through Layer 5 unchanged in this WP;
        the actual cross-tenant bypass is Wave 9 (MT-7) territory.
        """
        fake_db = _FakeDb(has_tenant_collection=False)
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-A-uuid",
                    "isAdmin": True,
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_admin"] is True
        sess = _fresh_service()._sessions[body["token"]]
        assert sess.is_admin is True

    def test_connect_uses_tenant_id_as_default_tenant_key(self):
        """``tenantKey`` defaults to ``tenantId`` when omitted."""
        fake_db = _FakeDb(
            has_tenant_collection=True,
            tenants={"tenant-A-uuid": {"_key": "tenant-A-uuid"}},
        )
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-A-uuid",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "tenant-A-uuid"
        assert body["tenant_key"] == "tenant-A-uuid"


# ---------------------------------------------------------------------------
# /nl2cypher and /nl2aql — workbench vs tenant-user mode
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_nl_session(monkeypatch: pytest.MonkeyPatch):
    """Inject a fake session with a known ``tenant_id`` and stub the NL
    pipeline so the routes execute without an LLM call.

    Yields ``(session_token, captured_tenant_context_ref)`` — the second
    element is a dict that ``nl_to_cypher`` / ``nl_to_aql`` write the
    received ``tenant_context`` into so the test can assert what the
    pipeline saw.
    """
    from arango_cypher import nl2cypher as nl_pkg
    from arango_cypher.nl2cypher import _core as nl_core

    fake_db = _FakeDb(has_tenant_collection=False)
    with _patched_arango_client(_make_fake_client(fake_db)):
        client = TestClient(_app())
        resp = client.post(
            "/connect",
            json={
                "url": "http://example.invalid",
                "database": "test",
                "username": "root",
                "password": "",
                "tenantId": "tenant-A-uuid",
            },
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]

    captured: dict[str, Any] = {}

    class _Result:
        cypher = "MATCH (n) RETURN n LIMIT 0"
        aql = "FOR n IN @@coll RETURN n"
        bind_vars: dict[str, Any] = {}
        explanation = ""
        confidence = 1.0
        method = "stub"
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        cached_tokens = 0
        retries = 0

    def _fake_nl_to_cypher(*_args, tenant_context=None, **_kwargs):
        captured["tenant_context"] = tenant_context
        return _Result()

    def _fake_nl_to_aql(*_args, tenant_context=None, **_kwargs):
        captured["tenant_context"] = tenant_context
        return _Result()

    monkeypatch.setattr(nl_pkg, "nl_to_cypher", _fake_nl_to_cypher, raising=True)
    monkeypatch.setattr(nl_pkg, "nl_to_aql", _fake_nl_to_aql, raising=True)
    monkeypatch.setattr(nl_core, "nl_to_cypher", _fake_nl_to_cypher, raising=False)
    monkeypatch.setattr(nl_core, "nl_to_aql", _fake_nl_to_aql, raising=False)

    yield token, captured

    _fresh_service()._sessions.pop(token, None)


class TestWorkbenchVsTenantUserMode:
    """``ARANGO_CYPHER_WORKBENCH`` flips body-vs-session-bound tenant precedence."""

    def test_workbench_mode_honors_body_tenant_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nl_session,
    ):
        """``ARANGO_CYPHER_WORKBENCH=1`` → body's ``tenant_context`` wins."""
        token, captured = fake_nl_session
        monkeypatch.setenv("ARANGO_CYPHER_WORKBENCH", "1")

        client = TestClient(_app())
        resp = client.post(
            "/nl2cypher",
            headers={"X-Arango-Session": token},
            json={
                "question": "list employees",
                "use_llm": False,
                "tenant_context": {
                    "property": "TENANT_HEX_ID",
                    "value": "tenant-WORKBENCH",
                },
            },
        )

        assert resp.status_code == 200, resp.text
        seen = captured["tenant_context"]
        assert seen is not None
        assert seen.value == "tenant-WORKBENCH"
        assert seen.property == "TENANT_HEX_ID"

    def test_tenant_user_mode_overrides_body_with_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nl_session,
        caplog: pytest.LogCaptureFixture,
    ):
        """No ``ARANGO_CYPHER_WORKBENCH`` → session-bound tenant wins; WARN logged."""
        token, captured = fake_nl_session
        monkeypatch.delenv("ARANGO_CYPHER_WORKBENCH", raising=False)

        client = TestClient(_app())
        with caplog.at_level(logging.WARNING, logger="arango_cypher.service.routes.nl"):
            resp = client.post(
                "/nl2cypher",
                headers={"X-Arango-Session": token},
                json={
                    "question": "list employees",
                    "use_llm": False,
                    "tenant_context": {
                        "property": "TENANT_HEX_ID",
                        "value": "tenant-B-rogue",
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        seen = captured["tenant_context"]
        assert seen is not None
        assert seen.value == "tenant-A-uuid"
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("tenant-B-rogue" in m and "tenant-A-uuid" in m for m in warning_messages), (
            f"expected override-warning in logs; got {warning_messages!r}"
        )

    def test_tenant_user_mode_injects_when_body_omits_tenant(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nl_session,
    ):
        """Body has no ``tenant_context`` but session is bound → inject session."""
        token, captured = fake_nl_session
        monkeypatch.delenv("ARANGO_CYPHER_WORKBENCH", raising=False)

        client = TestClient(_app())
        resp = client.post(
            "/nl2cypher",
            headers={"X-Arango-Session": token},
            json={"question": "list employees", "use_llm": False},
        )

        assert resp.status_code == 200, resp.text
        seen = captured["tenant_context"]
        assert seen is not None
        assert seen.value == "tenant-A-uuid"

    def test_tenant_user_mode_passes_through_when_body_matches_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nl_session,
        caplog: pytest.LogCaptureFixture,
    ):
        """Body's tenant matches session → session is used, no WARN emitted."""
        token, captured = fake_nl_session
        monkeypatch.delenv("ARANGO_CYPHER_WORKBENCH", raising=False)

        client = TestClient(_app())
        with caplog.at_level(logging.WARNING, logger="arango_cypher.service.routes.nl"):
            resp = client.post(
                "/nl2aql",
                headers={"X-Arango-Session": token},
                json={
                    "question": "list employees",
                    "tenant_context": {
                        "property": "TENANT_HEX_ID",
                        "value": "tenant-A-uuid",
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        seen = captured["tenant_context"]
        assert seen is not None
        assert seen.value == "tenant-A-uuid"
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("ignored" in m and "tenant-A-uuid" in m for m in warning_messages), (
            f"unexpected override warning when body == session: {warning_messages!r}"
        )


# ---------------------------------------------------------------------------
# /execute — tenant refusals must surface as 403, never masked as 500
# ---------------------------------------------------------------------------


class TestExecuteTenantViolationStatusCode:
    """Regression: a ``TenantScopeViolation`` raised by Layer 5/6 inside the
    ``_translate_errors`` block must reach the client as a structured HTTP
    403, not a generic 500.

    The bug: ``_translate_errors`` (a context manager wrapping
    ``safe_execute_aql``) caught the violation as a plain ``Exception`` and
    converted it to 500 before the route's own ``except TenantScopeViolation``
    handler could run — hiding an actionable "connect with a tenant" refusal
    behind "Internal Server Error".
    """

    def _connect_unbound(self) -> str:
        fake_db = _FakeDb(has_tenant_collection=False)
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                },
            )
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    def test_tenant_violation_surfaces_as_403(self, monkeypatch: pytest.MonkeyPatch):
        from arango_cypher.service.routes import cypher as cypher_routes
        from arango_cypher.tenant_plan_validator import TenantScopeViolation

        token = self._connect_unbound()

        class _Transpiled:
            aql = "FOR n IN @@coll RETURN n"
            bind_vars: dict[str, Any] = {}
            warnings: list[Any] = []

        # Reach safe_execute_aql without needing a real schema/translation.
        monkeypatch.setattr(cypher_routes, "_mapping_from_dict", lambda _m: object())
        monkeypatch.setattr(cypher_routes, "translate", lambda *_a, **_k: _Transpiled())

        def _raise_violation(**_kwargs):
            raise TenantScopeViolation(
                code="NO_SESSION_TENANT",
                message="session has no tenant_id; cannot validate tenant-scoped query",
                aql_digest="a" * 64,
                plan_digest="b" * 64,
            )

        monkeypatch.setattr(cypher_routes, "safe_execute_aql", _raise_violation)

        client = TestClient(_app())
        resp = client.post(
            "/execute",
            headers={"X-Arango-Session": token},
            json={"cypher": "MATCH (n:User) RETURN n", "mapping": {"any": "thing"}},
        )

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "tenant_scope_violation"
        assert detail["code"] == "NO_SESSION_TENANT"
        assert detail["aql_digest"] == "a" * 16
        _fresh_service()._sessions.pop(token, None)

    def test_generic_execute_error_still_500(self, monkeypatch: pytest.MonkeyPatch):
        """A non-tenant failure (e.g. a driver error) must still map to 500
        via ``_translate_errors`` — the 403 path is scoped to tenant refusals.
        """
        from arango_cypher.service.routes import cypher as cypher_routes

        token = self._connect_unbound()

        class _Transpiled:
            aql = "FOR n IN @@coll RETURN n"
            bind_vars: dict[str, Any] = {}
            warnings: list[Any] = []

        monkeypatch.setattr(cypher_routes, "_mapping_from_dict", lambda _m: object())
        monkeypatch.setattr(cypher_routes, "translate", lambda *_a, **_k: _Transpiled())

        def _raise_runtime(**_kwargs):
            raise RuntimeError("arango driver exploded")

        monkeypatch.setattr(cypher_routes, "safe_execute_aql", _raise_runtime)

        client = TestClient(_app())
        resp = client.post(
            "/execute",
            headers={"X-Arango-Session": token},
            json={"cypher": "MATCH (n:User) RETURN n", "mapping": {"any": "thing"}},
        )

        assert resp.status_code == 500, resp.text
        _fresh_service()._sessions.pop(token, None)
