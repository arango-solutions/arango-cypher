"""Tests for the connect-error diagnostic helpers in ``arango_cypher.service``.

The python-arango client wraps low-level transport failures (proxy
rejection, DNS, TLS, credential mismatch) inside a generic
``ClientConnectionError`` whose ``str()`` is "Can't connect to host(s)
within limit (N)". That message is not actionable on its own, which was
demonstrated in the field when a misconfigured HTTPS proxy silently
returned 403 to every CONNECT and the operator could not tell whether
the ArangoDB host was down, the credentials were wrong, or the proxy
was at fault. These tests pin the diagnostic so that future regressions
are caught in CI instead of in a support thread.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from arango_cypher import service
from arango_cypher.service import (
    _describe_connect_error,
    _walk_cause_chain,
    app,
)


class TestWalkCauseChain:
    def test_single_exception(self):
        exc = RuntimeError("solo")
        chain = _walk_cause_chain(exc)
        assert chain == [exc]

    def test_explicit_cause(self):
        root = OSError("Tunnel connection failed: 403 Forbidden")
        try:
            raise RuntimeError("wrapper") from root
        except RuntimeError as e:
            wrapper = e
        chain = _walk_cause_chain(wrapper)
        assert chain[0] is wrapper
        assert chain[-1] is root

    def test_implicit_context(self):
        try:
            try:
                raise OSError("network down")
            except OSError as inner:
                raise RuntimeError("wrapper") from inner
        except RuntimeError as e:
            outer = e
        chain = _walk_cause_chain(outer)
        assert chain[0] is outer
        assert isinstance(chain[-1], OSError)
        assert "network down" in str(chain[-1])

    def test_terminates_on_cycle(self):
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        chain = _walk_cause_chain(a)
        assert len(chain) == 2
        assert chain[0] is a
        assert chain[1] is b


class TestDescribeConnectError:
    def test_plain_error_passthrough(self):
        exc = RuntimeError("database not found")
        msg = _describe_connect_error(exc)
        assert "database not found" in msg
        assert "root cause" not in msg
        assert "hint:" not in msg

    def test_surfaces_root_cause_from_chain(self):
        try:
            try:
                raise OSError("SSL handshake failed")
            except OSError as inner:
                raise RuntimeError("Can't connect to host(s) within limit (3)") from inner
        except RuntimeError as e:
            top = e
        msg = _describe_connect_error(top)
        assert "Can't connect to host(s) within limit (3)" in msg
        assert "root cause" in msg
        assert "SSL handshake failed" in msg

    def test_proxy_detection_with_env_var(self, monkeypatch: pytest.MonkeyPatch):
        for var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy.example.invalid:3128")

        try:
            try:
                raise OSError("Tunnel connection failed: 403 Forbidden")
            except OSError as inner:
                raise RuntimeError("Can't connect to host(s) within limit (3)") from inner
        except RuntimeError as e:
            top = e

        msg = _describe_connect_error(top)
        assert "hint:" in msg
        assert "HTTPS_PROXY" in msg
        assert "NO_PROXY" in msg or "Unset them" in msg
        # Only env var names are listed in the hint — the proxy URL itself
        # is never read out of the environment, so it cannot leak here.
        assert "corp-proxy.example.invalid" not in msg

    def test_proxy_detection_without_env_var(self, monkeypatch: pytest.MonkeyPatch):
        for var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            monkeypatch.delenv(var, raising=False)

        exc = RuntimeError("Can't connect to host(s): OSError('Tunnel connection failed: 403')")
        msg = _describe_connect_error(exc)
        assert "hint:" in msg
        assert "no proxy env vars are set" in msg

    def test_credentials_never_leak_through_diagnostic(self):
        exc = RuntimeError("auth failed (password=hunter2; token=abcdef12345)")
        msg = _describe_connect_error(exc)
        assert "hunter2" not in msg
        assert "abcdef12345" not in msg
        assert "<redacted-credential>" in msg


class TestConnectEndpointSurfaces:
    """End-to-end: POST /connect must include the root cause + proxy hint."""

    def test_proxy_failure_returns_actionable_detail(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy.example.invalid:3128")

        class _FakeDb:
            def version(self):
                raise _simulate_client_connection_error()

        class _FakeClient:
            def __init__(self, hosts):
                self.hosts = hosts

            def db(self, *_args, **_kwargs):
                return _FakeDb()

            def close(self):
                pass

        with mock.patch.object(service, "ArangoClient", _FakeClient):
            client = TestClient(app)
            resp = client.post(
                "/connect",
                json={
                    "url": "https://example-arango.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "secret",
                },
            )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "Can't connect to host(s) within limit (3)" in detail
        assert "root cause" in detail
        assert "Tunnel connection failed" in detail
        assert "hint:" in detail
        assert "HTTPS_PROXY" in detail


def _simulate_client_connection_error() -> Exception:
    """Shape-match python-arango's ClientConnectionError wrapping pattern.

    We don't import ``ClientConnectionError`` directly because the public
    surface of ``python-arango`` changes between versions and this test
    is about our diagnostic helper, not about pinning the upstream type.
    Any ``Exception`` subclass with a proxy-tunnel OSError in its
    ``__cause__`` chain is sufficient to exercise the branch.
    """
    try:
        try:
            raise OSError("Tunnel connection failed: 403 Forbidden")
        except OSError as inner:
            raise RuntimeError("Can't connect to host(s) within limit (3)") from inner
    except RuntimeError as e:
        return e
    raise AssertionError("unreachable")
