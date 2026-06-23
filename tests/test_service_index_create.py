"""Tests for ``POST /schema/index/create`` (WP-S3c).

The endpoint creates the inverted index an ``IndexAdvisory`` recommends so the
NL workbench can offer one-click acceleration of fuzzy name matching. It is
session-authenticated (mutates the connected DB), reconstructs the index spec
server-side from validated fields, and is idempotent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from arango_cypher.service import _get_session, _Session, app

client = TestClient(app)


def _install_session(
    *,
    collections: set[str] | None = None,
    existing_indexes: list[dict[str, Any]] | None = None,
    add_index_return: dict[str, Any] | None = None,
) -> MagicMock:
    existing = collections or set()
    db = MagicMock()
    db.name = "testdb"
    db.has_collection.side_effect = lambda name: name in existing

    coll = MagicMock()
    coll.indexes.return_value = existing_indexes or []
    coll.add_index.return_value = add_index_return or {
        "id": "Node/123",
        "name": "idx_fuzzy_name",
        "type": "inverted",
    }
    db.collection.return_value = coll

    session = _Session.__new__(_Session)
    session.token = "test-token"
    session.db = db
    session.client = MagicMock()
    session.created_at = 0.0
    session.last_used = 0.0
    session.tenant_id = None
    session.tenant_key = None
    session.is_admin = False
    app.dependency_overrides[_get_session] = lambda: session
    return session


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.pop(_get_session, None)


class TestCreateIndex:
    def test_requires_session(self):
        resp = client.post(
            "/schema/index/create",
            json={"collection": "Node", "field": "name"},
        )
        assert resp.status_code == 401

    def test_creates_inverted_index(self):
        session = _install_session(collections={"Node"})
        resp = client.post(
            "/schema/index/create",
            json={"collection": "Node", "field": "name"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] is True
        assert body["collection"] == "Node"
        assert body["field"] == "name"
        # Spec is reconstructed server-side: inverted index, text analyzer.
        spec = session.db.collection.return_value.add_index.call_args[0][0]
        assert spec["type"] == "inverted"
        assert spec["fields"] == [{"name": "name", "analyzer": "text_en"}]
        assert spec["name"] == "idx_fuzzy_name"

    def test_custom_analyzer_and_name(self):
        session = _install_session(collections={"Node"})
        resp = client.post(
            "/schema/index/create",
            json={
                "collection": "Node",
                "field": "title",
                "analyzer": "text_de",
                "name": "my_idx",
            },
        )
        assert resp.status_code == 200, resp.text
        spec = session.db.collection.return_value.add_index.call_args[0][0]
        assert spec["name"] == "my_idx"
        assert spec["fields"] == [{"name": "title", "analyzer": "text_de"}]

    def test_idempotent_when_index_exists(self):
        session = _install_session(
            collections={"Node"},
            existing_indexes=[
                {
                    "id": "Node/999",
                    "name": "idx_fuzzy_name",
                    "type": "inverted",
                    "fields": [{"name": "name", "analyzer": "text_en"}],
                }
            ],
        )
        resp = client.post(
            "/schema/index/create",
            json={"collection": "Node", "field": "name"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] is False
        assert "already" in body["message"].lower()
        # No new index attempted.
        session.db.collection.return_value.add_index.assert_not_called()

    def test_unknown_collection_404(self):
        _install_session(collections=set())
        resp = client.post(
            "/schema/index/create",
            json={"collection": "Missing", "field": "name"},
        )
        assert resp.status_code == 404

    def test_invalid_collection_name_400(self):
        _install_session(collections={"Node"})
        resp = client.post(
            "/schema/index/create",
            json={"collection": "bad name!", "field": "name"},
        )
        assert resp.status_code == 400

    def test_blank_field_400(self):
        _install_session(collections={"Node"})
        resp = client.post(
            "/schema/index/create",
            json={"collection": "Node", "field": "   "},
        )
        assert resp.status_code == 400
