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
        "entities": [
            {"labels": ["User"], "name": "User", "properties": []}
        ],
        "relationships": [],
    },
    "physicalMapping": {
        "entities": {
            "User": {"collectionName": "users", "style": "COLLECTION"}
        },
        "relationships": {},
    },
    "metadata": {"timestamp": "2026-01-01T00:00:00Z"},
}


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
        resp = client.post("/translate", json={
            "cypher": "MATCH (n:User) RETURN n.name AS name",
            "mapping": PG_MAPPING,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "FOR n IN" in data["aql"]
        assert "RETURN" in data["aql"]
        assert "@collection" in data["bind_vars"]

    def test_translate_with_where(self):
        resp = client.post("/translate", json={
            "cypher": "MATCH (n:User) WHERE n.age > 21 RETURN n.name AS name",
            "mapping": PG_MAPPING,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "FILTER" in data["aql"]

    def test_translate_with_params(self):
        resp = client.post("/translate", json={
            "cypher": "MATCH (n:User) WHERE n.name = $name RETURN n",
            "mapping": PG_MAPPING,
            "params": {"name": "Alice"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["bind_vars"]["name"] == "Alice"

    def test_translate_with_extensions(self):
        resp = client.post("/translate", json={
            "cypher": "MATCH (n:User) RETURN arango.bm25(n) AS score",
            "mapping": PG_MAPPING,
            "extensions_enabled": True,
        })
        assert resp.status_code == 200
        assert "BM25(n)" in resp.json()["aql"]

    def test_translate_missing_mapping_rejected(self):
        resp = client.post("/translate", json={
            "cypher": "MATCH (n:User) RETURN n",
        })
        assert resp.status_code == 400

    def test_translate_invalid_cypher_rejected(self):
        resp = client.post("/translate", json={
            "cypher": "NOT VALID CYPHER !!!",
            "mapping": PG_MAPPING,
        })
        assert resp.status_code == 422

    def test_translate_empty_cypher_rejected(self):
        resp = client.post("/translate", json={
            "cypher": "",
            "mapping": PG_MAPPING,
        })
        assert resp.status_code == 422


class TestValidate:
    def test_syntax_only(self):
        resp = client.post("/validate", json={
            "cypher": "MATCH (n:User) RETURN n",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_invalid_syntax(self):
        resp = client.post("/validate", json={
            "cypher": "NOT VALID CYPHER !!!",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert len(resp.json()["errors"]) > 0

    def test_with_mapping(self):
        resp = client.post("/validate", json={
            "cypher": "MATCH (n:User) RETURN n.name AS name",
            "mapping": PG_MAPPING,
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestExecuteWithoutSession:
    def test_no_auth_header(self):
        resp = client.post("/execute", json={
            "cypher": "MATCH (n:User) RETURN n",
            "mapping": PG_MAPPING,
        })
        assert resp.status_code == 401

    def test_invalid_token(self):
        resp = client.post("/execute", json={
            "cypher": "MATCH (n:User) RETURN n",
            "mapping": PG_MAPPING,
        }, headers={"Authorization": "Bearer invalid_token"})
        assert resp.status_code == 401


class TestDisconnectWithoutSession:
    def test_no_auth(self):
        resp = client.post("/disconnect")
        assert resp.status_code == 401


class TestExplainWithoutSession:
    def test_no_auth_header(self):
        resp = client.post("/explain", json={
            "cypher": "MATCH (n:User) RETURN n",
            "mapping": PG_MAPPING,
        })
        assert resp.status_code == 401

    def test_invalid_token(self):
        resp = client.post("/explain", json={
            "cypher": "MATCH (n:User) RETURN n",
            "mapping": PG_MAPPING,
        }, headers={"Authorization": "Bearer bad_token"})
        assert resp.status_code == 401


class TestAqlProfileWithoutSession:
    def test_no_auth_header(self):
        resp = client.post("/aql-profile", json={
            "cypher": "MATCH (n:User) RETURN n",
            "mapping": PG_MAPPING,
        })
        assert resp.status_code == 401

    def test_invalid_token(self):
        resp = client.post("/aql-profile", json={
            "cypher": "MATCH (n:User) RETURN n",
            "mapping": PG_MAPPING,
        }, headers={"Authorization": "Bearer bad_token"})
        assert resp.status_code == 401


class TestConnections:
    def test_list_empty(self):
        resp = client.get("/connections")
        assert resp.status_code == 200
        data = resp.json()
        assert "active" in data
        assert "sessions" in data
