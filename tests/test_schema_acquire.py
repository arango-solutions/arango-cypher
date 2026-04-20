"""Tests for arango_cypher.schema_acquire — classify, acquire, get_mapping, caching."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from arango_cypher.schema_acquire import (
    _build_heuristic_mapping,
    _cache_key,
    _mapping_cache,
    acquire_mapping_bundle,
    classify_schema,
    get_mapping,
)
from arango_query_core import CoreError, MappingBundle

# ---------------------------------------------------------------------------
# Helpers — mock db factories
# ---------------------------------------------------------------------------

def _make_mock_db(
    *,
    doc_collections: list[str] | None = None,
    edge_collections: list[str] | None = None,
    docs_by_collection: dict[str, list[dict[str, Any]]] | None = None,
) -> MagicMock:
    """Build a mock StandardDatabase with controllable collections() and aql.execute()."""
    db = MagicMock()

    cols: list[dict[str, Any]] = []
    for name in (doc_collections or []):
        cols.append({"name": name, "type": 2})
    for name in (edge_collections or []):
        cols.append({"name": name, "type": 3})

    db.collections.return_value = cols

    docs_map = docs_by_collection or {}

    def _execute(query: str, bind_vars: dict[str, Any] | None = None, **kw: Any):
        bv = bind_vars or {}
        col_name = bv.get("@col")
        if col_name and col_name in docs_map:
            return iter(docs_map[col_name])
        return iter([])

    db.aql.execute = MagicMock(side_effect=_execute)
    db.name = "mock_test_db"

    col_mock = MagicMock()
    col_mock.count.return_value = 0
    col_mock.indexes.return_value = []
    db.collection.return_value = col_mock

    return db


# ---------------------------------------------------------------------------
# classify_schema
# ---------------------------------------------------------------------------

class TestClassifySchema:
    def test_pg_schema(self):
        db = _make_mock_db(
            doc_collections=["users", "products"],
            edge_collections=["purchases"],
            docs_by_collection={
                "users": [{"name": "Alice"}, {"name": "Bob"}],
                "products": [{"title": "Widget"}, {"title": "Gadget"}],
                "purchases": [{"_from": "users/1", "_to": "products/1"}],
            },
        )
        assert classify_schema(db) == "pg"

    def test_lpg_schema(self):
        db = _make_mock_db(
            doc_collections=["entities"],
            edge_collections=["relations"],
            docs_by_collection={
                "entities": [
                    {"type": "Person", "name": "Alice"},
                    {"type": "Company", "name": "ACME"},
                    {"type": "Person", "name": "Bob"},
                ],
                "relations": [
                    {"_from": "entities/1", "_to": "entities/2", "relation": "WORKS_AT"},
                    {"_from": "entities/3", "_to": "entities/2", "relation": "WORKS_AT"},
                ],
            },
        )
        assert classify_schema(db) == "lpg"

    def test_hybrid_schema(self):
        db = _make_mock_db(
            doc_collections=["users", "entities"],
            edge_collections=[],
            docs_by_collection={
                "users": [{"name": "Alice"}, {"name": "Bob"}],
                "entities": [
                    {"type": "Person", "name": "X"},
                    {"type": "Company", "name": "Y"},
                ],
            },
        )
        assert classify_schema(db) == "hybrid"

    def test_unknown_no_collections(self):
        db = _make_mock_db(doc_collections=[], edge_collections=[])
        assert classify_schema(db) == "unknown"

    def test_unknown_on_exception(self):
        db = MagicMock()
        db.collections.side_effect = Exception("connection refused")
        assert classify_schema(db) == "unknown"

    def test_system_collections_skipped(self):
        db = MagicMock()
        db.collections.return_value = [
            {"name": "_system", "type": 2},
            {"name": "_graphs", "type": 2},
        ]
        assert classify_schema(db) == "unknown"


# ---------------------------------------------------------------------------
# _build_heuristic_mapping
# ---------------------------------------------------------------------------

class TestBuildHeuristicMapping:
    def test_pg_mapping(self):
        db = _make_mock_db(
            doc_collections=["users", "products"],
            edge_collections=["purchases"],
            docs_by_collection={
                "users": [{"name": "Alice", "age": 30}],
                "products": [{"title": "Widget"}],
                "purchases": [{"_from": "users/1", "_to": "products/1"}],
            },
        )
        bundle = _build_heuristic_mapping(db, "pg")
        assert isinstance(bundle, MappingBundle)
        assert "User" in bundle.physical_mapping["entities"]
        assert bundle.physical_mapping["entities"]["User"]["style"] == "COLLECTION"
        assert bundle.physical_mapping["entities"]["User"]["collectionName"] == "users"
        assert "PURCHASES" in bundle.physical_mapping["relationships"]
        assert bundle.source.kind == "heuristic"

    def test_lpg_mapping_with_type_field(self):
        db = _make_mock_db(
            doc_collections=["nodes"],
            edge_collections=[],
            docs_by_collection={
                "nodes": [
                    {"type": "Person", "name": "A"},
                    {"type": "Company", "name": "B"},
                    {"type": "Person", "name": "C"},
                ],
            },
        )

        # _detect_type_field needs the docs to have 80%+ with the field
        bundle = _build_heuristic_mapping(db, "lpg")
        assert isinstance(bundle, MappingBundle)

        entities = bundle.physical_mapping.get("entities", {})
        if entities:
            for _label, mapping in entities.items():
                if mapping.get("style") == "LABEL":
                    assert "typeField" in mapping
                    assert "typeValue" in mapping


# ---------------------------------------------------------------------------
# acquire_mapping_bundle — mocked analyzer
# ---------------------------------------------------------------------------

class TestAcquireMappingBundle:
    def test_import_error_when_not_installed(self):
        db = _make_mock_db(doc_collections=["test"])
        with patch.dict("sys.modules", {"schema_analyzer": None}):
            with pytest.raises(ImportError, match="arangodb-schema-analyzer is not installed"):
                acquire_mapping_bundle(db)

    def test_success_with_mocked_analyzer(self):
        db = _make_mock_db(doc_collections=["users"])

        mock_metadata = MagicMock()
        mock_metadata.model_dump.return_value = {
            "confidence": 0.9,
            "timestamp": "2025-01-01T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 0},
            "detectedPatterns": [],
            "warnings": [],
            "assumptions": [],
        }

        mock_result = MagicMock()
        mock_result.conceptual_schema = {
            "entities": [{"name": "User", "labels": ["User"], "properties": []}],
            "relationships": [],
        }
        mock_result.physical_mapping = {
            "entities": {"User": {"style": "COLLECTION", "collectionName": "users"}},
            "relationships": {},
        }
        mock_result.metadata = mock_metadata

        mock_analyzer_cls = MagicMock()
        mock_analyzer_instance = MagicMock()
        mock_analyzer_instance.analyze_physical_schema.return_value = mock_result
        mock_analyzer_cls.return_value = mock_analyzer_instance

        def mock_export(analysis_dict, target="cypher"):
            return {
                "conceptualSchema": analysis_dict["conceptualSchema"],
                "physicalMapping": analysis_dict["physicalMapping"],
                "metadata": analysis_dict["metadata"],
            }

        mock_schema_analyzer = MagicMock()
        mock_schema_analyzer.AgenticSchemaAnalyzer = mock_analyzer_cls
        mock_schema_analyzer.export_mapping = mock_export

        mock_owl_module = MagicMock()
        mock_owl_module.export_conceptual_model_as_owl_turtle.return_value = "@prefix owl: ..."

        with patch.dict("sys.modules", {
            "schema_analyzer": mock_schema_analyzer,
            "schema_analyzer.owl_export": mock_owl_module,
        }):
            bundle = acquire_mapping_bundle(db, include_owl=True)

        assert isinstance(bundle, MappingBundle)
        assert bundle.physical_mapping["entities"]["User"]["style"] == "COLLECTION"
        assert bundle.owl_turtle == "@prefix owl: ..."
        assert bundle.source.kind == "schema_analyzer_export"

    def test_success_without_owl(self):
        db = _make_mock_db(doc_collections=["users"])

        mock_metadata = MagicMock()
        mock_metadata.model_dump.return_value = {"confidence": 0.5}

        mock_result = MagicMock()
        mock_result.conceptual_schema = {"entities": [], "relationships": []}
        mock_result.physical_mapping = {"entities": {}, "relationships": {}}
        mock_result.metadata = mock_metadata

        mock_analyzer_cls = MagicMock()
        mock_analyzer_cls.return_value.analyze_physical_schema.return_value = mock_result

        def mock_export(analysis_dict, target="cypher"):
            return {
                "conceptualSchema": analysis_dict["conceptualSchema"],
                "physicalMapping": analysis_dict["physicalMapping"],
                "metadata": analysis_dict["metadata"],
            }

        mock_schema_analyzer = MagicMock()
        mock_schema_analyzer.AgenticSchemaAnalyzer = mock_analyzer_cls
        mock_schema_analyzer.export_mapping = mock_export

        mock_owl_module = MagicMock()

        with patch.dict("sys.modules", {
            "schema_analyzer": mock_schema_analyzer,
            "schema_analyzer.owl_export": mock_owl_module,
        }):
            bundle = acquire_mapping_bundle(db, include_owl=False)

        assert isinstance(bundle, MappingBundle)
        assert bundle.owl_turtle is None


# ---------------------------------------------------------------------------
# get_mapping — strategy routing
# ---------------------------------------------------------------------------

class TestGetMapping:
    def setup_method(self):
        _mapping_cache.clear()

    def test_strategy_heuristic(self):
        db = _make_mock_db(
            doc_collections=["users"],
            edge_collections=["follows"],
            docs_by_collection={
                "users": [{"name": "Alice"}],
                "follows": [{"_from": "users/1", "_to": "users/2"}],
            },
        )
        bundle = get_mapping(db, strategy="heuristic")
        assert isinstance(bundle, MappingBundle)
        assert bundle.source.kind == "heuristic"

    def test_strategy_auto_falls_back_to_heuristic_without_analyzer(self):
        db = _make_mock_db(
            doc_collections=["customers"],
            edge_collections=[],
            docs_by_collection={
                "customers": [{"name": "Alice"}, {"name": "Bob"}],
            },
        )
        with patch.dict("sys.modules", {"schema_analyzer": None, "schema_analyzer.owl_export": None}):
            bundle = get_mapping(db, strategy="auto")
        assert isinstance(bundle, MappingBundle)
        assert bundle.source.kind == "heuristic"

    def test_strategy_analyzer_raises_import_error(self):
        db = _make_mock_db(doc_collections=["test"])
        with patch.dict("sys.modules", {"schema_analyzer": None}):
            with pytest.raises(ImportError, match="arangodb-schema-analyzer"):
                get_mapping(db, strategy="analyzer")

    def test_strategy_auto_hybrid_falls_back_without_analyzer(self):
        db = _make_mock_db(
            doc_collections=["users", "entities"],
            edge_collections=[],
            docs_by_collection={
                "users": [{"name": "Alice"}, {"name": "Bob"}],
                "entities": [
                    {"type": "Person", "name": "X"},
                    {"type": "Company", "name": "Y"},
                ],
            },
        )
        with patch.dict("sys.modules", {"schema_analyzer": None}):
            bundle = get_mapping(db, strategy="auto")
        assert isinstance(bundle, MappingBundle)
        assert bundle.source.kind == "heuristic"

    def test_invalid_strategy_raises(self):
        db = _make_mock_db(doc_collections=["test"])
        with pytest.raises(CoreError, match="Invalid strategy"):
            get_mapping(db, strategy="invalid")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TestCaching:
    def setup_method(self):
        _mapping_cache.clear()

    def test_cache_hit_within_ttl(self):
        db = _make_mock_db(
            doc_collections=["users"],
            docs_by_collection={"users": [{"name": "A"}]},
        )

        bundle1 = get_mapping(db, strategy="heuristic")
        bundle2 = get_mapping(db, strategy="heuristic")

        assert bundle1 is bundle2

    def test_cache_miss_after_ttl(self):
        db = _make_mock_db(
            doc_collections=["users"],
            docs_by_collection={"users": [{"name": "A"}]},
        )

        bundle1 = get_mapping(db, strategy="heuristic", cache_collection=None)

        key = _cache_key(db)
        assert key in _mapping_cache
        _, ts, _shape_fp, _full_fp = _mapping_cache[key]
        # Stale shape fingerprint forces a full re-introspection, not a
        # stats-only refresh, so bundle2 is guaranteed to be freshly built.
        _mapping_cache[key] = (
            bundle1, ts, "stale-shape-fingerprint", "stale-full-fingerprint",
        )

        bundle2 = get_mapping(db, strategy="heuristic", cache_collection=None)
        assert bundle2 is not bundle1

    def test_cache_key_deterministic(self):
        db = _make_mock_db(doc_collections=["a", "b", "c"])
        db.name = "testdb"
        k1 = _cache_key(db)
        k2 = _cache_key(db)
        assert k1 == k2
        assert k1 == "testdb"


# ---------------------------------------------------------------------------
# _cache_key edge cases
# ---------------------------------------------------------------------------

class TestCacheKey:
    def test_empty_on_exception(self):
        db = MagicMock(spec=[])
        type(db).name = PropertyMock(side_effect=Exception("fail"))
        assert _cache_key(db) == ""

    def test_returns_db_name(self):
        db = MagicMock()
        db.name = "testdb"
        assert _cache_key(db) == "testdb"

    def test_different_db_names_produce_different_keys(self):
        db1 = MagicMock()
        db1.name = "db_alpha"

        db2 = MagicMock()
        db2.name = "db_beta"

        assert _cache_key(db1) != _cache_key(db2)


class TestSchemaFingerprints:
    """Shape and full fingerprints have distinct invalidation domains.

    Shape fp must be stable under row-count changes (ordinary writes) and
    must change when the collection set, collection types, or index shapes
    change. Full fp rolls in row counts so it detects both shape and
    content drift.
    """

    def _make_db(self, collections, indexes_by_col=None, counts_by_col=None):
        db = MagicMock()
        db.name = "testdb"
        db.collections.return_value = collections
        idx = indexes_by_col or {}
        cnt = counts_by_col or {}

        def _col(name):
            m = MagicMock()
            m.count.return_value = cnt.get(name, 0)
            m.indexes.return_value = idx.get(name, [])
            return m

        db.collection.side_effect = _col
        return db

    def test_shape_fp_empty_on_collections_failure(self):
        from arango_cypher.schema_acquire import _shape_fingerprint
        db = MagicMock()
        db.name = "testdb"
        db.collections.side_effect = Exception("fail")
        # Still emits a hash — it is just a hash of an empty set.
        # The key property is: deterministic, does not raise.
        assert isinstance(_shape_fingerprint(db), str)

    def test_shape_fp_deterministic_under_row_count_change(self):
        """The core win: writes must NOT invalidate the mapping cache."""
        from arango_cypher.schema_acquire import _shape_fingerprint
        db = self._make_db(
            [{"name": "users", "type": 2}],
            indexes_by_col={"users": [{"type": "persistent", "fields": ["email"]}]},
            counts_by_col={"users": 100},
        )
        fp_before = _shape_fingerprint(db)

        db = self._make_db(
            [{"name": "users", "type": 2}],
            indexes_by_col={"users": [{"type": "persistent", "fields": ["email"]}]},
            counts_by_col={"users": 999_999},
        )
        fp_after = _shape_fingerprint(db)

        assert fp_before == fp_after

    def test_shape_fp_changes_when_index_added(self):
        from arango_cypher.schema_acquire import _shape_fingerprint
        db_before = self._make_db(
            [{"name": "users", "type": 2}],
            indexes_by_col={"users": []},
        )
        db_after = self._make_db(
            [{"name": "users", "type": 2}],
            indexes_by_col={"users": [{"type": "persistent", "fields": ["email"]}]},
        )
        assert _shape_fingerprint(db_before) != _shape_fingerprint(db_after)

    def test_shape_fp_changes_when_index_uniqueness_flips(self):
        """Catches the pre-existing bug where only index COUNT was hashed."""
        from arango_cypher.schema_acquire import _shape_fingerprint
        db_before = self._make_db(
            [{"name": "users", "type": 2}],
            indexes_by_col={
                "users": [{"type": "persistent", "fields": ["email"], "unique": False}]
            },
        )
        db_after = self._make_db(
            [{"name": "users", "type": 2}],
            indexes_by_col={
                "users": [{"type": "persistent", "fields": ["email"], "unique": True}]
            },
        )
        assert _shape_fingerprint(db_before) != _shape_fingerprint(db_after)

    def test_shape_fp_changes_when_collection_added(self):
        from arango_cypher.schema_acquire import _shape_fingerprint
        db_before = self._make_db([{"name": "users", "type": 2}])
        db_after = self._make_db(
            [{"name": "users", "type": 2}, {"name": "orders", "type": 2}]
        )
        assert _shape_fingerprint(db_before) != _shape_fingerprint(db_after)

    def test_full_fp_changes_with_count(self):
        from arango_cypher.schema_acquire import _full_fingerprint
        db = self._make_db(
            [{"name": "users", "type": 2}], counts_by_col={"users": 100}
        )
        fp1 = _full_fingerprint(db)
        db = self._make_db(
            [{"name": "users", "type": 2}], counts_by_col={"users": 200}
        )
        fp2 = _full_fingerprint(db)
        assert fp1 != fp2

    def test_full_fp_always_differs_from_shape_fp_when_rows_present(self):
        """Defence against a future refactor that accidentally collapses the
        two fingerprints into the same hash.
        """
        from arango_cypher.schema_acquire import (
            _full_fingerprint,
            _shape_fingerprint,
        )
        db = self._make_db(
            [{"name": "users", "type": 2}], counts_by_col={"users": 5}
        )
        assert _shape_fingerprint(db) != _full_fingerprint(db)

    def test_cache_collection_itself_is_excluded(self):
        """Reading the cache collection must not perturb either fingerprint.

        Otherwise persisting the cache on write would invalidate it on the
        next read — classic self-invalidation bug.
        """
        from arango_cypher.schema_acquire import _shape_fingerprint
        from arango_cypher.schema_cache import DEFAULT_CACHE_COLLECTION
        db_without = self._make_db([{"name": "users", "type": 2}])
        db_with = self._make_db(
            [
                {"name": "users", "type": 2},
                {"name": DEFAULT_CACHE_COLLECTION, "type": 2},
            ]
        )
        assert _shape_fingerprint(db_without) == _shape_fingerprint(db_with)
