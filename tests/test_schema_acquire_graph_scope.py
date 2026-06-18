"""Tests for optional named-graph scoping in the schema-acquisition layer
(PRD §17).

Covers the three acquire-layer primitives:

* ``graph_collections(db, name)`` — resolve a named graph's vertex / edge
  collection membership (and refuse an unknown graph).
* ``_filter_bundle_to_graph(bundle, vertex, edges)`` — prune a whole-database
  ``MappingBundle`` down to a graph's collections across physical mapping,
  conceptual schema, and statistics.
* ``_graph_scoped_cache_key`` + ``get_mapping(..., graph_name=...)`` — each
  named graph and the unscoped "all collections" view get an independent cache
  slot and never alias one another.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from arango_cypher import schema_acquire as sa
from arango_query_core import CoreError, MappingBundle


def _full_bundle() -> MappingBundle:
    """A two-collection bundle: a domain `Node` graph + a `chunks` side store."""
    return MappingBundle(
        conceptual_schema={
            "entities": [{"name": "Node"}, {"name": "Chunk"}],
            "relationships": [
                {"type": "REL", "fromEntity": "Node", "toEntity": "Node"},
                {"type": "OTHER", "fromEntity": "Chunk", "toEntity": "Chunk"},
            ],
        },
        physical_mapping={
            "entities": {
                "Node": {"style": "GENERIC_WITH_TYPE", "collectionName": "Node"},
                "Chunk": {"style": "COLLECTION", "collectionName": "chunks"},
            },
            "relationships": {
                "REL": {"edgeCollectionName": "relations"},
                "OTHER": {"edgeCollectionName": "other_edges"},
            },
        },
        metadata={
            "statistics": {
                "entities": {"Node": {"estimated_count": 10}, "Chunk": {"estimated_count": 5}},
                "relationships": {"REL": {"edge_count": 3}, "OTHER": {"edge_count": 7}},
            }
        },
    )


class TestGraphCollections:
    def test_resolves_vertex_and_edge_membership(self):
        db = MagicMock()
        db.has_graph.return_value = True
        graph = MagicMock()
        # python-arango's Graph exposes the full vertex set (incl. orphans)
        # via vertex_collections(); there is no orphan_collections() method.
        graph.vertex_collections.return_value = ["Node", "LoneColl"]
        graph.edge_definitions.return_value = [
            {
                "edge_collection": "relations",
                "from_vertex_collections": ["Node"],
                "to_vertex_collections": ["Node"],
            }
        ]
        db.graph.return_value = graph

        vertex, edges = sa.graph_collections(db, "FinReflectKG")
        assert vertex == {"Node", "LoneColl"}
        assert edges == {"relations"}

    def test_unknown_graph_raises(self):
        db = MagicMock()
        db.has_graph.return_value = False
        with pytest.raises(CoreError) as exc:
            sa.graph_collections(db, "ghost")
        assert exc.value.code == "UNKNOWN_GRAPH"


class TestFilterBundleToGraph:
    def test_prunes_to_graph_collections(self):
        f = sa._filter_bundle_to_graph(_full_bundle(), {"Node"}, {"relations"})
        assert list(f.physical_mapping["entities"]) == ["Node"]
        assert list(f.physical_mapping["relationships"]) == ["REL"]
        assert [e["name"] for e in f.conceptual_schema["entities"]] == ["Node"]
        assert [r["type"] for r in f.conceptual_schema["relationships"]] == ["REL"]
        assert set(f.metadata["statistics"]["entities"]) == {"Node"}
        assert set(f.metadata["statistics"]["relationships"]) == {"REL"}

    def test_does_not_mutate_input(self):
        bundle = _full_bundle()
        sa._filter_bundle_to_graph(bundle, {"Node"}, {"relations"})
        # Original bundle is untouched — both collections still present.
        assert set(bundle.physical_mapping["entities"]) == {"Node", "Chunk"}

    def test_empty_membership_yields_empty_mapping(self):
        f = sa._filter_bundle_to_graph(_full_bundle(), set(), set())
        assert f.physical_mapping["entities"] == {}
        assert f.physical_mapping["relationships"] == {}


class TestGraphScopedCacheKey:
    def test_none_graph_is_unchanged(self):
        assert sa._graph_scoped_cache_key("mapping", None) == "mapping"

    def test_graph_appends_namespace(self):
        assert sa._graph_scoped_cache_key("mapping", "G") == "mapping::graph::G"

    def test_sanitises_unsafe_chars(self):
        assert sa._graph_scoped_cache_key("mapping", "a/b c") == "mapping::graph::a_b_c"


class TestGetMappingGraphScoping:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        sa._mapping_cache.clear()
        yield
        sa._mapping_cache.clear()

    def _patch(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        db = MagicMock()
        db.name = "testdb"
        monkeypatch.setattr(sa, "_shape_fingerprint", lambda _db: "shape")
        monkeypatch.setattr(sa, "_full_fingerprint", lambda _db: "full")
        monkeypatch.setattr(sa, "_safe_refresh_statistics", lambda _db, b: b)
        monkeypatch.setattr(sa, "acquire_mapping_bundle", lambda _db, **_k: _full_bundle())
        monkeypatch.setattr(sa, "graph_collections", lambda _db, _name: ({"Node"}, {"relations"}))
        return db

    def test_unscoped_returns_all_collections(self, monkeypatch):
        db = self._patch(monkeypatch)
        bundle = sa.get_mapping(db, cache_collection=None)
        assert set(bundle.physical_mapping["entities"]) == {"Node", "Chunk"}

    def test_scoped_returns_only_graph_collections(self, monkeypatch):
        db = self._patch(monkeypatch)
        bundle = sa.get_mapping(db, cache_collection=None, graph_name="G")
        assert set(bundle.physical_mapping["entities"]) == {"Node"}

    def test_scoped_and_unscoped_use_distinct_cache_slots(self, monkeypatch):
        db = self._patch(monkeypatch)
        sa.get_mapping(db, cache_collection=None)
        sa.get_mapping(db, cache_collection=None, graph_name="G")
        assert "testdb" in sa._mapping_cache
        assert "testdb::graph::G" in sa._mapping_cache
        # The two slots hold different (scoped vs full) bundles.
        full_bundle = sa._mapping_cache["testdb"][0]
        scoped_bundle = sa._mapping_cache["testdb::graph::G"][0]
        assert set(full_bundle.physical_mapping["entities"]) == {"Node", "Chunk"}
        assert set(scoped_bundle.physical_mapping["entities"]) == {"Node"}
