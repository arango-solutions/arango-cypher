"""Tests for arango_cypher.schema_acquire — classify, acquire, get_mapping, caching."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from arango_cypher.schema_acquire import (
    CACHE_TTL_SECONDS,
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

        bundle1 = get_mapping(db, strategy="heuristic")

        key = _cache_key(db)
        assert key in _mapping_cache
        _, ts, fp = _mapping_cache[key]
        _mapping_cache[key] = (bundle1, ts, "stale-fingerprint")

        bundle2 = get_mapping(db, strategy="heuristic")
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


class TestSchemaFingerprint:
    def test_empty_on_exception(self):
        db = MagicMock()
        db.collections.side_effect = Exception("fail")
        from arango_cypher.schema_acquire import _schema_fingerprint
        assert _schema_fingerprint(db) == ""

    def test_deterministic(self):
        from arango_cypher.schema_acquire import _schema_fingerprint
        db = MagicMock()
        db.name = "testdb"
        db.collections.return_value = [
            {"name": "users", "type": 2},
            {"name": "orders", "type": 2},
        ]
        col_mock = MagicMock()
        col_mock.count.return_value = 100
        col_mock.indexes.return_value = [{"type": "persistent"}]
        db.collection.return_value = col_mock

        fp1 = _schema_fingerprint(db)
        fp2 = _schema_fingerprint(db)
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_changes_with_count(self):
        from arango_cypher.schema_acquire import _schema_fingerprint
        db = MagicMock()
        db.name = "testdb"
        db.collections.return_value = [{"name": "users", "type": 2}]
        col_mock = MagicMock()
        col_mock.count.return_value = 100
        col_mock.indexes.return_value = []
        db.collection.return_value = col_mock

        fp1 = _schema_fingerprint(db)

        col_mock.count.return_value = 200
        fp2 = _schema_fingerprint(db)
        assert fp1 != fp2
