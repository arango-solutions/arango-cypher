"""Tests for the FastAPI HTTP service endpoints.

These tests use FastAPI's TestClient (backed by httpx) and do not require
a running ArangoDB instance — they test the translate, validate, profile,
and connection-defaults endpoints without actually connecting.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arango_cypher.service import app

client = TestClient(app)

PG_MAPPING = {
    "conceptualSchema": {
        "entities": [{"labels": ["User"], "name": "User", "properties": []}],
        "relationships": [],
    },
    "physicalMapping": {
        "entities": {"User": {"collectionName": "users", "style": "COLLECTION"}},
        "relationships": {},
    },
    "metadata": {"timestamp": "2026-01-01T00:00:00Z"},
}


class TestHealth:
    """Liveness probe for container orchestrators.

    The endpoint must be cheap (no DB roundtrip), unauthenticated, and
    return 200 with JSON metadata that platform probes can log.
    """

    def test_returns_200_and_status_ok(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "arango-cypher-py"
        assert "version" in body and body["version"]


class TestCypherProfile:
    def test_returns_manifest(self):
        resp = client.get("/cypher-profile")
        assert resp.status_code == 200
        data = resp.json()
        assert "supported" in data
        assert "profile_schema_version" in data
        assert "MATCH" in data["supported"]["reading_clauses"]

    def test_has_extension_functions(self):
        resp = client.get("/cypher-profile")
        data = resp.json()
        assert "arango.bm25" in data["supported"]["extension_functions"]


class TestConnectDefaults:
    def test_returns_defaults(self):
        resp = client.get("/connect/defaults")
        assert resp.status_code == 200
        data = resp.json()
        assert "url" in data
        assert "database" in data
        assert "username" in data
        assert "password" in data


class TestTranslate:
    def test_basic_translate(self):
        resp = client.post(
            "/translate",
            json={
                "cypher": "MATCH (n:User) RETURN n.name AS name",
                "mapping": PG_MAPPING,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "FOR n IN" in data["aql"]
        assert "RETURN" in data["aql"]
        assert "@collection" in data["bind_vars"]

    def test_translate_with_where(self):
        resp = client.post(
            "/translate",
            json={
                "cypher": "MATCH (n:User) WHERE n.age > 21 RETURN n.name AS name",
                "mapping": PG_MAPPING,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "FILTER" in data["aql"]

    def test_translate_with_params(self):
        resp = client.post(
            "/translate",
            json={
                "cypher": "MATCH (n:User) WHERE n.name = $name RETURN n",
                "mapping": PG_MAPPING,
                "params": {"name": "Alice"},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["bind_vars"]["name"] == "Alice"

    def test_translate_with_extensions(self):
        resp = client.post(
            "/translate",
            json={
                "cypher": "MATCH (n:User) RETURN arango.bm25(n) AS score",
                "mapping": PG_MAPPING,
                "extensions_enabled": True,
            },
        )
        assert resp.status_code == 200
        assert "BM25(n)" in resp.json()["aql"]

    def test_translate_missing_mapping_rejected(self):
        resp = client.post(
            "/translate",
            json={
                "cypher": "MATCH (n:User) RETURN n",
            },
        )
        assert resp.status_code == 400

    def test_translate_invalid_cypher_rejected(self):
        resp = client.post(
            "/translate",
            json={
                "cypher": "NOT VALID CYPHER !!!",
                "mapping": PG_MAPPING,
            },
        )
        assert resp.status_code == 422

    def test_translate_empty_cypher_rejected(self):
        resp = client.post(
            "/translate",
            json={
                "cypher": "",
                "mapping": PG_MAPPING,
            },
        )
        assert resp.status_code == 422


class TestValidate:
    def test_syntax_only(self):
        resp = client.post(
            "/validate",
            json={
                "cypher": "MATCH (n:User) RETURN n",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_invalid_syntax(self):
        resp = client.post(
            "/validate",
            json={
                "cypher": "NOT VALID CYPHER !!!",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert len(resp.json()["errors"]) > 0

    def test_with_mapping(self):
        resp = client.post(
            "/validate",
            json={
                "cypher": "MATCH (n:User) RETURN n.name AS name",
                "mapping": PG_MAPPING,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestExecuteWithoutSession:
    def test_no_auth_header(self):
        resp = client.post(
            "/execute",
            json={
                "cypher": "MATCH (n:User) RETURN n",
                "mapping": PG_MAPPING,
            },
        )
        assert resp.status_code == 401

    def test_invalid_token(self):
        resp = client.post(
            "/execute",
            json={
                "cypher": "MATCH (n:User) RETURN n",
                "mapping": PG_MAPPING,
            },
            headers={"Authorization": "Bearer invalid_token"},
        )
        assert resp.status_code == 401


class TestDisconnectWithoutSession:
    def test_no_auth(self):
        resp = client.post("/disconnect")
        assert resp.status_code == 401


class TestExplainWithoutSession:
    def test_no_auth_header(self):
        resp = client.post(
            "/explain",
            json={
                "cypher": "MATCH (n:User) RETURN n",
                "mapping": PG_MAPPING,
            },
        )
        assert resp.status_code == 401

    def test_invalid_token(self):
        resp = client.post(
            "/explain",
            json={
                "cypher": "MATCH (n:User) RETURN n",
                "mapping": PG_MAPPING,
            },
            headers={"Authorization": "Bearer bad_token"},
        )
        assert resp.status_code == 401


class TestAqlProfileWithoutSession:
    def test_no_auth_header(self):
        resp = client.post(
            "/aql-profile",
            json={
                "cypher": "MATCH (n:User) RETURN n",
                "mapping": PG_MAPPING,
            },
        )
        assert resp.status_code == 401

    def test_invalid_token(self):
        resp = client.post(
            "/aql-profile",
            json={
                "cypher": "MATCH (n:User) RETURN n",
                "mapping": PG_MAPPING,
            },
            headers={"Authorization": "Bearer bad_token"},
        )
        assert resp.status_code == 401


class TestConnections:
    def test_list_empty(self):
        resp = client.get("/connections")
        assert resp.status_code == 200
        data = resp.json()
        assert "active" in data
        assert "sessions" in data


class TestUiCacheHeaders:
    """Regression tests for the SPA cache policy.

    History: an earlier version served the UI shell via Starlette's default
    StaticFiles, which omits Cache-Control. Chrome would heuristic-cache the
    shell against Last-Modified and replay a stale page after backend
    restarts — surfacing as ghost `/connect` failures that only "Application
    → Clear site data" could resolve. The shell must always revalidate; the
    Vite-hashed assets under /assets/* should be marked immutable.

    The contract is enforced on BOTH SPA mounts the service exposes:
    ``/ui`` (legacy, local dev) and ``/frontend`` (AMP / BYOC platform-proxy
    target). Parametrising over both prefixes prevents the cache-policy
    regression from silently surviving on one prefix while failing on the
    other — which is exactly how this bug class re-emerges.
    """

    def _ui_dist_present(self) -> bool:
        from arango_cypher.service import _UI_DIR  # type: ignore[attr-defined]

        return _UI_DIR.is_dir() and (_UI_DIR / "index.html").is_file()

    @pytest.mark.parametrize("prefix", ["/ui", "/frontend"])
    def test_spa_index_no_cache(self, prefix: str):
        if not self._ui_dist_present():
            pytest.skip("ui/dist not built")
        for path in (prefix, prefix + "/", prefix + "/index.html"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            cc = resp.headers.get("cache-control", "")
            assert "no-cache" in cc and "no-store" in cc and "must-revalidate" in cc, (
                f"{path} missing no-cache headers (got {cc!r})"
            )

    @pytest.mark.parametrize("prefix", ["/ui", "/frontend"])
    def test_spa_fallback_no_cache(self, prefix: str):
        if not self._ui_dist_present():
            pytest.skip("ui/dist not built")
        resp = client.get(f"{prefix}/some-deep-route-that-does-not-exist")
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("text/html")
        cc = resp.headers.get("cache-control", "")
        assert "no-cache" in cc

    def test_ui_assets_immutable(self):
        from arango_cypher.service import _UI_DIR  # type: ignore[attr-defined]

        assets_dir = _UI_DIR / "assets"
        if not assets_dir.is_dir():
            pytest.skip("ui/dist/assets not present")
        first = next((p for p in assets_dir.iterdir() if p.is_file()), None)
        if first is None:
            pytest.skip("no built assets to probe")
        resp = client.get(f"/assets/{first.name}")
        assert resp.status_code == 200
        cc = resp.headers.get("cache-control", "")
        assert "immutable" in cc and "max-age=31536000" in cc, (
            f"hashed asset missing immutable cache headers (got {cc!r})"
        )
