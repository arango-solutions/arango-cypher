"""Tests for the named-graph scoping endpoints (PRD §17):

* ``GET /graphs`` — list the connected database's named graphs and their
  vertex / edge / orphan collections so the UI's scope selector can offer
  "scope to N collections".
* ``POST /session/graph`` — bind (or clear) the active session's named-graph
  scope after analysis, validating that the graph exists.
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
    graphs: list[dict[str, Any]] | None = None,
    existing_graphs: set[str] | None = None,
    graph_name: str | None = None,
) -> MagicMock:
    db = MagicMock()
    db.name = "testdb"
    db.graphs.return_value = graphs or []
    known = existing_graphs if existing_graphs is not None else set()
    db.has_graph.side_effect = lambda name: name in known

    session = _Session.__new__(_Session)
    session.token = "test-token"
    session.db = db
    session.client = MagicMock()
    session.created_at = 0.0
    session.last_used = 0.0
    session.tenant_id = None
    session.tenant_key = None
    session.is_admin = False
    session.graph_name = graph_name
    app.dependency_overrides[_get_session] = lambda: session
    return session


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.pop(_get_session, None)


class TestListGraphs:
    def test_requires_session(self):
        resp = client.get("/graphs")
        assert resp.status_code == 401

    def test_empty_database_returns_empty_list(self):
        _install_session(graphs=[])
        resp = client.get("/graphs")
        assert resp.status_code == 200
        assert resp.json() == {"graphs": []}

    def test_lists_graphs_with_collection_membership(self):
        _install_session(
            graphs=[
                {
                    "name": "FinReflectKG",
                    "edge_definitions": [
                        {
                            "edge_collection": "relations",
                            "from_vertex_collections": ["Node"],
                            "to_vertex_collections": ["Node"],
                        }
                    ],
                    "orphan_collections": ["Lookup"],
                }
            ]
        )
        resp = client.get("/graphs")
        assert resp.status_code == 200
        graphs = resp.json()["graphs"]
        assert len(graphs) == 1
        g = graphs[0]
        assert g["name"] == "FinReflectKG"
        assert g["vertexCollections"] == ["Lookup", "Node"]
        assert g["orphanCollections"] == ["Lookup"]
        assert g["edgeDefinitions"] == [{"edgeCollection": "relations", "from": ["Node"], "to": ["Node"]}]
        # Node + Lookup (vertex) + relations (edge) = 3 distinct collections.
        assert g["collectionCount"] == 3

    def test_graphs_sorted_by_name(self):
        _install_session(
            graphs=[
                {"name": "Zeta", "edge_definitions": [], "orphan_collections": []},
                {"name": "Alpha", "edge_definitions": [], "orphan_collections": []},
            ]
        )
        names = [g["name"] for g in client.get("/graphs").json()["graphs"]]
        assert names == ["Alpha", "Zeta"]


class TestBindSessionGraph:
    def test_requires_session(self):
        resp = client.post("/session/graph", json={"graphName": "G"})
        assert resp.status_code == 401

    def test_bind_sets_graph_on_session(self):
        session = _install_session(existing_graphs={"FinReflectKG"})
        resp = client.post("/session/graph", json={"graphName": "FinReflectKG"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["bound"] is True
        assert body["graph_name"] == "FinReflectKG"
        assert session.graph_name == "FinReflectKG"

    def test_clearing_graph_unbinds_session(self):
        session = _install_session(graph_name="FinReflectKG")
        resp = client.post("/session/graph", json={"graphName": None})
        assert resp.status_code == 200
        body = resp.json()
        assert body["bound"] is False
        assert body["graph_name"] is None
        assert session.graph_name is None

    def test_unknown_graph_is_404(self):
        session = _install_session(existing_graphs={"FinReflectKG"}, graph_name="FinReflectKG")
        resp = client.post("/session/graph", json={"graphName": "ghost"})
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "unknown_graph"
        # Binding must be left untouched on refusal.
        assert session.graph_name == "FinReflectKG"
