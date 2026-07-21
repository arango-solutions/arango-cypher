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
from arango_query_core import CoreError, MappingBundle

from arango_cypher import schema_acquire as sa


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


class TestReadCachedMappingGraphScoping:
    """``read_cached_mapping`` (the catalog request-path read) must satisfy a
    scoped request from the cached *unscoped* bundle, so selecting a named graph
    works without separately warming a (database, graph) cache slot.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        sa._mapping_cache.clear()
        yield
        sa._mapping_cache.clear()

    def _db(self) -> Any:
        db = MagicMock()
        db.name = "testdb"
        return db

    def test_scoped_miss_derives_from_cached_full_bundle(self, monkeypatch):
        db = self._db()
        monkeypatch.setattr(sa, "graph_collections", lambda _db, _name: ({"Node"}, {"relations"}))
        # Only the unscoped slot is warm (e.g. what the sidecar populated).
        sa._mapping_cache[sa._cache_key(db)] = (_full_bundle(), 0.0, "shape", "full")

        scoped = sa.read_cached_mapping(db, cache_collection=None, graph_name="G")
        assert scoped is not None
        assert set(scoped.physical_mapping["entities"]) == {"Node"}
        assert set(scoped.physical_mapping["relationships"]) == {"REL"}
        # Derived view is not persisted as its own slot — it always reflects the
        # current cached full mapping rather than a stale scoped copy.
        assert "testdb::graph::G" not in sa._mapping_cache

    def test_scoped_miss_with_no_full_cache_returns_none(self, monkeypatch):
        db = self._db()
        monkeypatch.setattr(sa, "graph_collections", lambda _db, _name: ({"Node"}, {"relations"}))
        assert sa.read_cached_mapping(db, cache_collection=None, graph_name="G") is None

    def test_unknown_graph_on_scoped_miss_returns_none(self, monkeypatch):
        db = self._db()

        def _raise(_db, _name):
            raise CoreError("nope", code="UNKNOWN_GRAPH")

        monkeypatch.setattr(sa, "graph_collections", _raise)
        sa._mapping_cache[sa._cache_key(db)] = (_full_bundle(), 0.0, "shape", "full")
        assert sa.read_cached_mapping(db, cache_collection=None, graph_name="ghost") is None

    def test_warm_scoped_slot_is_served_directly(self, monkeypatch):
        db = self._db()
        # If a scoped slot *is* warm, it is served without deriving/filtering.
        sentinel = _full_bundle()
        sa._mapping_cache[sa._graph_scoped_cache_key(sa._cache_key(db), "G")] = (
            sentinel,
            0.0,
            "shape",
            "full",
        )

        def _boom(_db, _name):  # pragma: no cover - must not be called
            raise AssertionError("graph_collections should not run on a scoped hit")

        monkeypatch.setattr(sa, "graph_collections", _boom)
        assert sa.read_cached_mapping(db, cache_collection=None, graph_name="G") is sentinel


def _tagged_bundle() -> MappingBundle:
    """A full bundle carrying analyzer-provided named-graph signals: per-entry
    ``graphs`` tags plus a ``metadata.graphMembership`` summary for graph ``G``,
    in the analyzer-native *nested* shape (per-graph entries under ``graphs``,
    with sibling ``status`` / ``graphCount`` / ``ungraphed``).
    """
    b = _full_bundle()
    b.physical_mapping["entities"]["Node"]["graphs"] = ["G"]
    b.physical_mapping["relationships"]["REL"]["graphs"] = ["G"]
    b.metadata["graphMembership"] = {
        "status": "ok",
        "graphCount": 1,
        "graphs": {
            "G": {
                "entities": ["Node"],
                "relationships": ["REL"],
                "vertexCollections": ["Node"],
                "edgeCollections": ["relations"],
            }
        },
        "ungraphed": {"entities": ["Chunk"], "relationships": ["OTHER"]},
    }
    return b


class TestReconstructGraphMembership:
    def test_builds_summary_from_per_entry_tags(self):
        pm = {
            "entities": {
                "Node": {"collectionName": "Node", "graphs": ["G", "H"]},
                "Chunk": {"collectionName": "chunks"},  # ungraphed
            },
            "relationships": {
                "REL": {"edgeCollectionName": "relations", "graphs": ["G"]},
            },
        }
        gm = sa._reconstruct_graph_membership(pm)
        assert set(gm) == {"G", "H"}
        assert gm["G"] == {"vertexCollections": ["Node"], "edgeCollections": ["relations"]}
        assert gm["H"] == {"vertexCollections": ["Node"], "edgeCollections": []}

    def test_empty_when_no_tags(self):
        pm = {"entities": {"Node": {"collectionName": "Node"}}, "relationships": {}}
        assert sa._reconstruct_graph_membership(pm) == {}


class TestGraphMembershipCollections:
    def test_reads_native_nested_membership_without_db(self):
        vertex, edges = sa._graph_membership_collections(_tagged_bundle(), "G")
        assert vertex == {"Node"}
        assert edges == {"relations"}

    def test_reads_flat_reconstructed_membership(self):
        # The reconstruction fallback emits a flat name -> entry map.
        b = _full_bundle()
        b.metadata["graphMembership"] = {
            "G": {"vertexCollections": ["Node"], "edgeCollections": ["relations"]}
        }
        assert sa._graph_membership_collections(b, "G") == ({"Node"}, {"relations"})

    def test_returns_none_for_unknown_graph(self):
        assert sa._graph_membership_collections(_tagged_bundle(), "nope") is None

    def test_returns_none_for_ungraphed_pseudo_key(self):
        # "ungraphed"/"status"/"graphCount" siblings must not resolve as graphs.
        assert sa._graph_membership_collections(_tagged_bundle(), "ungraphed") is None
        assert sa._graph_membership_collections(_tagged_bundle(), "status") is None

    def test_returns_none_when_not_graph_aware(self):
        assert sa._graph_membership_collections(_full_bundle(), "G") is None


class TestScopePrefersEmbeddedMembership:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        sa._mapping_cache.clear()
        yield
        sa._mapping_cache.clear()

    def _db(self) -> Any:
        db = MagicMock()
        db.name = "testdb"
        return db

    def test_scope_bundle_uses_membership_not_db(self, monkeypatch):
        db = self._db()

        def _boom(_db, _name):  # pragma: no cover - must not be called
            raise AssertionError("graph_collections must not run for a graph-aware bundle")

        monkeypatch.setattr(sa, "graph_collections", _boom)
        scoped = sa._scope_bundle_to_graph(db, _tagged_bundle(), "G")
        assert set(scoped.physical_mapping["entities"]) == {"Node"}
        assert set(scoped.physical_mapping["relationships"]) == {"REL"}

    def test_read_cached_prefers_membership_no_db_call(self, monkeypatch):
        db = self._db()

        def _boom(_db, _name):  # pragma: no cover - must not be called
            raise AssertionError("graph_collections must not run for a graph-aware bundle")

        monkeypatch.setattr(sa, "graph_collections", _boom)
        sa._mapping_cache[sa._cache_key(db)] = (_tagged_bundle(), 0.0, "shape", "full")

        scoped = sa.read_cached_mapping(db, cache_collection=None, graph_name="G")
        assert scoped is not None
        assert set(scoped.physical_mapping["entities"]) == {"Node"}

    def test_falls_back_to_db_when_not_graph_aware(self, monkeypatch):
        db = self._db()
        called: list[str] = []

        def _live(_db, name):
            called.append(name)
            return ({"Node"}, {"relations"})

        monkeypatch.setattr(sa, "graph_collections", _live)
        sa._mapping_cache[sa._cache_key(db)] = (_full_bundle(), 0.0, "shape", "full")

        scoped = sa.read_cached_mapping(db, cache_collection=None, graph_name="G")
        assert scoped is not None
        assert called == ["G"]  # live lookup was used as the fallback
