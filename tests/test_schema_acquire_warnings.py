"""Tests for analyzer-unavailable visibility plumbing (WP-28 / defect D2).

Covers the ``_attach_warning`` helper, the ``ImportError`` fallback branch
inside :func:`arango_cypher.schema_acquire._build_fresh_bundle`, the
``_bundle_needs_reacquire`` cache-bust predicate, and the
``get_mapping``-level retry that drops a cached ``ANALYZER_NOT_INSTALLED``
bundle once the analyzer becomes importable again.

All tests are offline — the analyzer is simulated by patching
``sys.modules["schema_analyzer"]`` rather than talking to a real DB.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from arango_cypher import schema_acquire
from arango_cypher.schema_acquire import (
    _attach_warning,
    _build_fresh_bundle,
    _bundle_needs_reacquire,
    _cache_key,
    _mapping_cache,
    get_mapping,
)
from arango_cypher.schema_cache import bundle_from_doc, bundle_to_doc
from arango_query_core import MappingBundle, MappingSource

# ---------------------------------------------------------------------------
# Mock db helper — small enough to duplicate rather than import from
# test_schema_acquire.py (keeps the warnings suite self-contained).
# ---------------------------------------------------------------------------


def _make_db(
    *,
    doc_collections: list[str] | None = None,
    edge_collections: list[str] | None = None,
    docs_by_collection: dict[str, list[dict[str, Any]]] | None = None,
) -> MagicMock:
    db = MagicMock()
    cols: list[dict[str, Any]] = []
    for name in doc_collections or []:
        cols.append({"name": name, "type": 2})
    for name in edge_collections or []:
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
    db.name = "warnings_mock_db"

    col_mock = MagicMock()
    col_mock.count.return_value = 0
    col_mock.indexes.return_value = []
    db.collection.return_value = col_mock
    return db


def _mock_analyzer_modules(*, label: str = "User", collection: str = "users") -> dict[str, Any]:
    """Build a ``sys.modules`` patch dict that makes ``schema_analyzer`` importable."""
    mock_metadata = MagicMock()
    mock_metadata.model_dump.return_value = {
        "confidence": 0.95,
        "detectedPatterns": [],
        "warnings": [],
        "assumptions": [],
    }

    mock_result = MagicMock()
    mock_result.conceptual_schema = {
        "entities": [{"name": label, "labels": [label], "properties": []}],
        "relationships": [],
    }
    mock_result.physical_mapping = {
        "entities": {label: {"style": "COLLECTION", "collectionName": collection}},
        "relationships": {},
    }
    mock_result.metadata = mock_metadata

    mock_analyzer_cls = MagicMock()
    mock_analyzer_cls.return_value.analyze_physical_schema.return_value = mock_result

    def mock_export(analysis_dict: dict[str, Any], target: str = "cypher") -> dict[str, Any]:
        return {
            "conceptualSchema": analysis_dict["conceptualSchema"],
            "physicalMapping": analysis_dict["physicalMapping"],
            "metadata": analysis_dict["metadata"],
        }

    mock_module = MagicMock()
    mock_module.AgenticSchemaAnalyzer = mock_analyzer_cls
    mock_module.export_mapping = mock_export
    # Fingerprint helpers are called by _shape_fingerprint / _full_fingerprint
    # when schema_analyzer is importable. Return deterministic strings so
    # cache lookups are stable within a test.
    mock_module.fingerprint_physical_shape = MagicMock(return_value="mock-shape")
    mock_module.fingerprint_physical_counts = MagicMock(return_value="mock-full")

    return {"schema_analyzer": mock_module}


# ---------------------------------------------------------------------------
# _attach_warning
# ---------------------------------------------------------------------------


class TestAttachWarning:
    def test_attach_warning_roundtrip(self):
        """Warnings survive bundle -> dict -> bundle round-trips (cache path)."""
        bundle = MappingBundle(
            conceptual_schema={"entities": [], "relationships": []},
            physical_mapping={"entities": {}, "relationships": {}},
            metadata={"statistics": {}},
            source=MappingSource(kind="heuristic"),
        )
        augmented = _attach_warning(
            bundle,
            code="ANALYZER_NOT_INSTALLED",
            message="analyzer missing",
            install_hint="pip install arangodb-schema-analyzer",
        )

        assert bundle is not augmented
        assert bundle.metadata.get("warnings") is None
        warnings = augmented.metadata["warnings"]
        assert len(warnings) == 1
        assert warnings[0] == {
            "code": "ANALYZER_NOT_INSTALLED",
            "message": "analyzer missing",
            "install_hint": "pip install arangodb-schema-analyzer",
        }
        assert augmented.metadata.get("statistics") == {}

        rehydrated = bundle_from_doc(bundle_to_doc(augmented))
        assert rehydrated.metadata.get("warnings") == warnings

        twice = _attach_warning(augmented, code="OTHER", message="second warning")
        assert len(twice.metadata["warnings"]) == 2
        assert twice.metadata["warnings"][1] == {
            "code": "OTHER",
            "message": "second warning",
        }


# ---------------------------------------------------------------------------
# _build_fresh_bundle ImportError branch
# ---------------------------------------------------------------------------


class TestBuildFreshBundleImportErrorBranch:
    def test_importerror_branch_attaches_warning(self):
        db = _make_db(
            doc_collections=["customers"],
            docs_by_collection={"customers": [{"name": "Alice"}, {"name": "Bob"}]},
        )
        counter_before = schema_acquire._heuristic_fallback_counter

        with patch.dict(
            "sys.modules",
            {"schema_analyzer": None, "schema_analyzer.owl_export": None},
        ):
            bundle = _build_fresh_bundle(db, strategy="auto", include_owl=False)

        warnings = (bundle.metadata or {}).get("warnings") or []
        assert any(w.get("code") == "ANALYZER_NOT_INSTALLED" for w in warnings)
        only = next(w for w in warnings if w["code"] == "ANALYZER_NOT_INSTALLED")
        assert "schema-analyzer" in only["message"].lower() or "analyzer" in only["message"].lower()
        assert only["install_hint"].startswith("pip install")
        assert bundle.source is not None
        assert bundle.source.kind == "heuristic"
        assert schema_acquire._heuristic_fallback_counter == counter_before + 1


# ---------------------------------------------------------------------------
# _bundle_needs_reacquire
# ---------------------------------------------------------------------------


class TestBundleNeedsReacquire:
    def _heuristic_bundle_with_warning(self) -> MappingBundle:
        base = MappingBundle(
            conceptual_schema={"entities": [], "relationships": []},
            physical_mapping={"entities": {}, "relationships": {}},
            metadata={},
            source=MappingSource(kind="heuristic"),
        )
        return _attach_warning(
            base,
            code="ANALYZER_NOT_INSTALLED",
            message="analyzer missing",
            install_hint="pip install arangodb-schema-analyzer",
        )

    def test_bundle_needs_reacquire_when_analyzer_available(self):
        bundle = self._heuristic_bundle_with_warning()
        with patch.dict("sys.modules", _mock_analyzer_modules()):
            assert _bundle_needs_reacquire(bundle) is True

    def test_bundle_needs_reacquire_false_when_analyzer_missing(self):
        bundle = self._heuristic_bundle_with_warning()
        with patch.dict("sys.modules", {"schema_analyzer": None}):
            assert _bundle_needs_reacquire(bundle) is False

    def test_bundle_without_warning_never_reacquires(self):
        bundle = MappingBundle(
            conceptual_schema={"entities": [], "relationships": []},
            physical_mapping={"entities": {}, "relationships": {}},
            metadata={"statistics": {}},
            source=MappingSource(kind="heuristic"),
        )
        with patch.dict("sys.modules", _mock_analyzer_modules()):
            assert _bundle_needs_reacquire(bundle) is False


# ---------------------------------------------------------------------------
# get_mapping cache-bust integration
# ---------------------------------------------------------------------------


class TestGetMappingReacquires:
    def setup_method(self) -> None:
        _mapping_cache.clear()

    def teardown_method(self) -> None:
        _mapping_cache.clear()

    def test_get_mapping_busts_cache_when_needs_reacquire(self):
        """A cached ANALYZER_NOT_INSTALLED bundle must be dropped once the
        analyzer is importable again, and the next call must return an
        analyzer-built bundle rather than the cached degraded one.
        """
        db = _make_db(
            doc_collections=["users"],
            docs_by_collection={"users": [{"name": "Alice"}]},
        )

        # Build a stub cached bundle carrying the warning. Use the same
        # fingerprint strings the mock module returns so `get_mapping`
        # treats the cache as shape-stable.
        cached = _attach_warning(
            MappingBundle(
                conceptual_schema={
                    "entities": [{"label": "User"}],
                    "relationships": [],
                },
                physical_mapping={
                    "entities": {
                        "User": {"style": "COLLECTION", "collectionName": "users"},
                    },
                    "relationships": {},
                },
                metadata={},
                source=MappingSource(kind="heuristic"),
            ),
            code="ANALYZER_NOT_INSTALLED",
            message="analyzer missing",
            install_hint="pip install arangodb-schema-analyzer",
        )

        # Pre-seed the in-memory cache with fingerprints that match the
        # mocked analyzer's fingerprint helpers — otherwise the shape
        # check will report "shape changed" and force a rebuild for the
        # wrong reason.
        key = _cache_key(db)
        import time as _time

        _mapping_cache[key] = (cached, _time.time(), "mock-shape", "mock-full")

        with patch.dict("sys.modules", _mock_analyzer_modules()):
            fresh = get_mapping(db, strategy="auto", cache_collection=None)

        # The analyzer fingerprint helpers returned deterministic strings,
        # so shape matches, `_bundle_needs_reacquire` fires, the cache is
        # dropped, and the resulting bundle comes from the mocked analyzer.
        assert fresh is not cached
        assert fresh.source is not None
        assert fresh.source.kind == "schema_analyzer_export"
        fresh_warnings = (fresh.metadata or {}).get("warnings") or []
        assert not any(w.get("code") == "ANALYZER_NOT_INSTALLED" for w in fresh_warnings)
