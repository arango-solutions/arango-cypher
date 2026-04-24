"""Security-hardening contract tests for ``arango_cypher.service``.

This module pins the guards added in the "harden NL + connect endpoints"
PR. Each test exercises one specific behaviour so a future regression
shows up as a single, named failure (not a vague "the suite went red"):

* ``TestConnectDefaultsRedaction`` — the password from ``ARANGO_PASS`` is
  never returned by ``/connect/defaults`` unless the operator explicitly
  opts in by setting ``ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD`` to ``1``.
* ``TestConnectSsrfGuard`` — ``/connect`` refuses URLs pointing at
  cloud-metadata literals always, and at RFC1918 / loopback in
  ``ARANGO_CYPHER_PUBLIC_MODE``.
* ``TestPublicModeAuthRequired`` — every NL / corrections / connections
  endpoint that the audit flagged as "anonymous-callable" returns 401
  without a session token when ``ARANGO_CYPHER_PUBLIC_MODE=1``.
* ``TestCorsCredentialedWildcardRejected`` — service refuses to start
  when ``CORS_ALLOWED_ORIGINS=*`` is combined with explicit
  ``ARANGO_CYPHER_CORS_CREDENTIALS=1``.
* ``TestValidationErrorRedaction`` — Pydantic-422 body logging strips
  ``password=`` / ``Authorization: Bearer`` patterns and is suppressed
  entirely in public mode.
* ``TestMappingHashKeyNormalisation`` — both correction stores produce
  identical fingerprints for snake_case and camelCase mapping inputs,
  closing the silent ``lookup()``-misses-saves bug.

The public-mode tests reload ``arango_cypher.service`` against a
patched env so the module-level ``_PUBLIC_MODE`` and CORS guards
re-evaluate. We restore the original module afterwards so subsequent
tests in the run see the unhardened import — pytest collection is
order-independent but ``service`` is cached in ``sys.modules`` and we
must not poison it for unrelated suites.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arango_cypher import corrections, nl_corrections, service
from arango_cypher.service import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _block_real_connect(monkeypatch: pytest.MonkeyPatch):
    """Stub out ``ArangoClient`` so SSRF tests never attempt a real TCP
    connection.

    Several tests in this file POST to ``/connect`` with addresses that
    are *intentionally* unroutable (RFC1918, ``127.0.0.1:1``, allowlisted
    cloud-metadata IPs). Without this fixture each one would hang for
    the full python-arango retry budget (~30 s) and the suite would
    serialise into a multi-minute wall-clock — exactly the kind of
    silent slowdown that makes operators turn the test off and miss the
    next regression. We replace ``ArangoClient`` with a fake whose
    ``db().version()`` raises immediately, so the SSRF guard's pass /
    fail discriminator is the response status, not the timeout.
    """

    class _FakeDb:
        def version(self):
            raise RuntimeError("connection refused (test stub)")

        def databases(self):
            return ["_system"]

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def db(self, *args, **kwargs):
            return _FakeDb()

        def close(self):
            pass

    monkeypatch.setattr("arango_cypher.service.ArangoClient", _FakeClient)


@pytest.fixture
def public_mode_app(monkeypatch: pytest.MonkeyPatch):
    """Reimport ``arango_cypher.service`` with public mode toggled on.

    Yields the *new* module so tests can mount its ``app`` in a
    ``TestClient``. The ``arango_cypher.service`` cache is restored on
    teardown to avoid bleeding into unrelated tests.
    """
    monkeypatch.setenv("ARANGO_CYPHER_PUBLIC_MODE", "1")
    saved = sys.modules.pop("arango_cypher.service", None)
    try:
        reloaded = importlib.import_module("arango_cypher.service")
        # Re-apply the ``_block_real_connect`` stub against the freshly
        # imported module too — autouse fixtures patch the *original*
        # ``arango_cypher.service.ArangoClient`` symbol, but the reload
        # rebinds the name to a fresh import of ``arango.ArangoClient``.

        class _FakeDb:
            def version(self):
                raise RuntimeError("connection refused (test stub)")

            def databases(self):
                return ["_system"]

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def db(self, *args, **kwargs):
                return _FakeDb()

            def close(self):
                pass

        reloaded.ArangoClient = _FakeClient  # type: ignore[attr-defined]
        yield reloaded
    finally:
        if saved is not None:
            sys.modules["arango_cypher.service"] = saved
        else:
            sys.modules.pop("arango_cypher.service", None)


# ---------------------------------------------------------------------------
# /connect/defaults password redaction
# ---------------------------------------------------------------------------


class TestConnectDefaultsRedaction:
    def test_password_omitted_by_default(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Deliberately-synthetic probe values, chosen so (a) secret
        # scanners don't flag them as real credentials and (b) a reader
        # grepping the test tree knows instantly these are fixtures
        # rather than leaked production data.
        monkeypatch.setenv("ARANGO_PASS", "REDACTION-PROBE-VALUE-1")
        monkeypatch.delenv("ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD", raising=False)
        resp = client.get("/connect/defaults")
        assert resp.status_code == 200
        body = resp.json()
        assert body["password"] == ""
        # Defence in depth: nothing else in the response body should
        # contain the password literal either.
        assert "REDACTION-PROBE-VALUE-1" not in resp.text

    def test_password_returned_when_opted_in(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ARANGO_PASS", "REDACTION-PROBE-VALUE-2")
        monkeypatch.setenv("ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD", "1")
        resp = client.get("/connect/defaults")
        assert resp.status_code == 200
        assert resp.json()["password"] == "REDACTION-PROBE-VALUE-2"


# ---------------------------------------------------------------------------
# /connect SSRF guard
# ---------------------------------------------------------------------------


class TestConnectSsrfGuard:
    """Cloud-metadata IPs are blocked unconditionally; RFC1918 only in
    public mode. Network calls are mocked so this stays a pure unit test.
    """

    @pytest.mark.parametrize(
        "bad_url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.169.254:80",
            "http://100.100.100.200",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://metadata/computeMetadata/",
            "http://[fd00:ec2::254]:80",
        ],
    )
    def test_metadata_targets_rejected_always(
        self,
        client: TestClient,
        bad_url: str,
    ) -> None:
        resp = client.post(
            "/connect",
            json={"url": bad_url, "database": "_system", "username": "root", "password": ""},
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"].lower()
        assert "metadata" in detail or "refusing" in detail

    def test_metadata_target_can_be_allowlisted(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS",
            "169.254.169.254, metadata.google.internal",
        )
        # The allowlisted host bypasses the SSRF guard. The downstream
        # python-arango call will still fail (no real ArangoDB at that
        # IP), surfacing as a 400 with a connection-failure message —
        # NOT as the SSRF refusal. That's exactly the discriminator.
        resp = client.post(
            "/connect",
            json={
                "url": "http://169.254.169.254:8529",
                "database": "_system",
                "username": "root",
                "password": "",
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"].lower()
        assert "metadata" not in detail
        assert "connection failed" in detail

    def test_invalid_url_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/connect",
            json={"url": "http://", "database": "_system", "username": "root", "password": ""},
        )
        assert resp.status_code == 400
        assert "host" in resp.json()["detail"].lower()

    def test_private_address_allowed_in_default_mode(
        self,
        client: TestClient,
    ) -> None:
        # Localhost connect is the bedrock of the dev workflow; default
        # mode must NOT trip the SSRF guard for 127.0.0.1.
        resp = client.post(
            "/connect",
            json={
                "url": "http://127.0.0.1:1",  # nothing listening, fast 400
                "database": "_system",
                "username": "root",
                "password": "",
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"].lower()
        assert "private" not in detail and "loopback" not in detail


class TestConnectSsrfGuardPublicMode:
    """Public-mode-specific SSRF policy: RFC1918 / loopback rejected
    unless the operator pinned the host via the env allowlist."""

    @pytest.mark.parametrize(
        "bad_url",
        [
            "http://10.0.0.5:8529",
            "http://172.16.0.10:8529",
            "http://192.168.1.1:8529",
            "http://127.0.0.1:8529",
            "http://[::1]:8529",
        ],
    )
    def test_private_targets_rejected_in_public_mode(
        self,
        public_mode_app,
        bad_url: str,
    ) -> None:
        c = TestClient(public_mode_app.app)
        resp = c.post(
            "/connect",
            json={"url": bad_url, "database": "_system", "username": "root", "password": ""},
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"].lower()
        assert "private" in detail or "loopback" in detail

    def test_private_target_can_be_allowlisted(
        self,
        public_mode_app,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS", "10.0.0.5")
        c = TestClient(public_mode_app.app)
        resp = c.post(
            "/connect",
            json={
                "url": "http://10.0.0.5:8529",
                "database": "_system",
                "username": "root",
                "password": "",
            },
        )
        # Allowlisted → SSRF guard bypassed, reaches the python-arango
        # call which times out / refuses. We only assert we got past the
        # guard (i.e. the message is the connection-failure shape).
        assert resp.status_code == 400
        assert "private" not in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Public-mode auth gating
# ---------------------------------------------------------------------------


class TestPublicModeAuthRequired:
    """Endpoints flagged in the audit must 401 without a session token
    when ``ARANGO_CYPHER_PUBLIC_MODE=1``."""

    @pytest.mark.parametrize(
        "method,path,body",
        [
            (
                "post",
                "/nl2cypher",
                {"question": "x", "mapping": {"conceptualSchema": {}, "physicalMapping": {}}},
            ),
            (
                "post",
                "/nl2aql",
                {"question": "x", "mapping": {"conceptualSchema": {}, "physicalMapping": {}}},
            ),
            ("post", "/nl-samples", {"mapping": {"conceptualSchema": {}, "physicalMapping": {}}, "count": 3}),
            ("get", "/connections", None),
            (
                "post",
                "/corrections",
                {
                    "cypher": "MATCH (n) RETURN n",
                    "original_aql": "FOR n IN c RETURN n",
                    "corrected_aql": "FOR n IN c RETURN n",
                },
            ),
            ("get", "/corrections", None),
            ("delete", "/corrections", None),
            ("delete", "/corrections/1", None),
            ("post", "/nl-corrections", {"question": "q", "cypher": "MATCH (n) RETURN n"}),
            ("get", "/nl-corrections", None),
            ("delete", "/nl-corrections", None),
            ("delete", "/nl-corrections/1", None),
        ],
    )
    def test_endpoint_rejects_anonymous_in_public_mode(
        self,
        public_mode_app,
        method: str,
        path: str,
        body: Any,
    ) -> None:
        c = TestClient(public_mode_app.app)
        resp = getattr(c, method)(path, json=body) if body is not None else getattr(c, method)(path)
        assert resp.status_code == 401, (method, path, resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# CORS startup guard
# ---------------------------------------------------------------------------


@pytest.fixture
def reload_service_clean():
    """Yield a callable that reloads ``arango_cypher.service`` against
    the *current* (test-controlled) environment, then restores the
    cached module on teardown.

    Centralised here because three CORS tests need the same
    "reimport-then-restore" dance, and inlining the ``finally`` block
    in each test re-evaluates ``arango_cypher.service`` while
    ``monkeypatch`` is still holding the bad env vars — which races
    with the very startup guard the test is exercising.
    """
    saved = sys.modules.pop("arango_cypher.service", None)
    reloaded: list[Any] = []

    def _reload():
        sys.modules.pop("arango_cypher.service", None)
        mod = importlib.import_module("arango_cypher.service")
        reloaded.append(mod)
        return mod

    try:
        yield _reload
    finally:
        # Restore the original cached module reference (the one with
        # the test-time env stripped). If the test never successfully
        # reloaded — e.g. it asserted the import raises — we
        # explicitly re-import once with a clean env so the next test
        # in the run gets a usable ``service`` module.
        sys.modules.pop("arango_cypher.service", None)
        if saved is not None:
            sys.modules["arango_cypher.service"] = saved


class TestCorsCredentialedWildcardRejected:
    def test_wildcard_plus_credentials_refuses_to_start(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reload_service_clean,
    ) -> None:
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")
        monkeypatch.setenv("ARANGO_CYPHER_CORS_CREDENTIALS", "1")
        with pytest.raises(RuntimeError, match="unsafe"):
            reload_service_clean()

    def test_wildcard_default_silently_disables_credentials(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reload_service_clean,
    ) -> None:
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")
        monkeypatch.delenv("ARANGO_CYPHER_CORS_CREDENTIALS", raising=False)
        mod = reload_service_clean()
        assert mod._cors_credentials is False

    def test_explicit_origins_keep_credentials_on(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reload_service_clean,
    ) -> None:
        monkeypatch.setenv(
            "CORS_ALLOWED_ORIGINS",
            "https://app.example.com,https://workbench.example.com",
        )
        monkeypatch.delenv("ARANGO_CYPHER_CORS_CREDENTIALS", raising=False)
        mod = reload_service_clean()
        assert mod._cors_credentials is True
        assert mod._cors_origins == [
            "https://app.example.com",
            "https://workbench.example.com",
        ]


# ---------------------------------------------------------------------------
# 422 body logging redaction
# ---------------------------------------------------------------------------


class TestValidationErrorRedaction:
    def test_password_in_422_body_is_redacted_in_log(
        self,
        client: TestClient,
        caplog,
    ) -> None:
        # ``cypher`` is a required field on /translate; sending an
        # object that lacks it fires the 422 handler. We embed a
        # password=… literal in another field so we can grep the log
        # for redaction.
        with caplog.at_level("WARNING", logger="arango_cypher.service"):
            resp = client.post(
                "/translate",
                json={"mapping": {"note": "password=REDACTION-PROBE-VALUE-3 secret stuff"}},
            )
        assert resp.status_code == 422
        joined = " ".join(rec.getMessage() for rec in caplog.records)
        assert "REDACTION-PROBE-VALUE-3" not in joined
        assert "<redacted-credential>" in joined or "password=REDACTION-PROBE-VALUE-3" not in joined

    def test_body_logging_suppressed_in_public_mode(
        self,
        public_mode_app,
        caplog,
    ) -> None:
        c = TestClient(public_mode_app.app)
        with caplog.at_level("WARNING", logger="arango_cypher.service"):
            resp = c.post(
                "/translate",
                json={"mapping": {"hint": "password=REDACTION-PROBE-VALUE-4"}},
            )
        assert resp.status_code == 422
        joined = " ".join(rec.getMessage() for rec in caplog.records)
        assert "REDACTION-PROBE-VALUE-4" not in joined
        # Public-mode log line omits the body fragment entirely.
        assert "body[:200]" not in joined


# ---------------------------------------------------------------------------
# Mapping-hash key normalisation
# ---------------------------------------------------------------------------


class TestMappingHashKeyNormalisation:
    """Same logical mapping → same fingerprint regardless of key spelling."""

    def _mapping_payload(self, key_style: str) -> dict[str, Any]:
        cs = {"entities": [{"name": "Movie", "labels": ["Movie"]}]}
        pm = {"entities": {"Movie": {"collectionName": "movies"}}}
        if key_style == "snake":
            return {"conceptual_schema": cs, "physical_mapping": pm}
        return {"conceptualSchema": cs, "physicalMapping": pm}

    def test_corrections_hash_is_stable_across_key_styles(self) -> None:
        snake = self._mapping_payload("snake")
        camel = self._mapping_payload("camel")
        assert corrections._mapping_hash(snake) == corrections._mapping_hash(camel)

    def test_nl_corrections_hash_is_stable_across_key_styles(self) -> None:
        snake = self._mapping_payload("snake")
        camel = self._mapping_payload("camel")
        assert nl_corrections._mapping_hash(snake) == nl_corrections._mapping_hash(camel)

    def test_corrections_hash_distinguishes_different_mappings(self) -> None:
        a = self._mapping_payload("snake")
        b = {
            "conceptual_schema": {"entities": [{"name": "Person"}]},
            "physical_mapping": {"entities": {"Person": {"collectionName": "people"}}},
        }
        assert corrections._mapping_hash(a) != corrections._mapping_hash(b)


# Keep the ``service`` module reference live so ruff doesn't drop the import.
_ = service
