"""Tests for the schema-change detection API and two-tier cache.

Covers
------
- :class:`SchemaChangeReport` — the structured probe result.
- :func:`describe_schema_change` — cheap, read-only probe.
- :class:`ArangoSchemaCache` — persistent collection-backed cache with
  lazy collection creation, corruption tolerance, and version gating.
- Stats-only refresh path in :func:`get_mapping` — when shape is stable
  but counts drift, the conceptual/physical mapping is reused and only
  cardinality stats are recomputed.

The persistent cache tests use an in-memory fake Arango collection (a
tiny dict-backed stand-in) so these stay pure unit tests. A separate
integration test against a live DB lives under ``tests/integration``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from arango_cypher.schema_acquire import (
    SchemaChangeReport,
    _mapping_cache,
    describe_schema_change,
    get_mapping,
    invalidate_cache,
)
from arango_cypher.schema_cache import (
    CACHE_SCHEMA_VERSION,
    DEFAULT_CACHE_COLLECTION,
    ArangoSchemaCache,
    bundle_from_doc,
    bundle_to_doc,
)
from arango_query_core import MappingBundle, MappingSource

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeCollection:
    """Minimal dict-backed stand-in for a python-arango collection handle.

    Supports the methods :class:`ArangoSchemaCache` uses: ``get``,
    ``insert`` (with ``overwrite``), ``update``, ``delete``. Good enough
    for unit-testing cache semantics without a live DB.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self.docs.get(key)

    def insert(
        self,
        doc: dict[str, Any],
        *,
        overwrite: bool = False,
        silent: bool = False,
    ) -> dict[str, Any]:
        key = doc["_key"]
        if key in self.docs and not overwrite:
            raise RuntimeError("duplicate key")
        self.docs[key] = dict(doc)
        return {"_key": key}

    def update(
        self,
        doc: dict[str, Any],
        *,
        merge: bool = True,
        keep_none: bool = True,
    ) -> dict[str, Any]:
        key = doc["_key"]
        if merge and key in self.docs:
            merged = {**self.docs[key], **doc}
            self.docs[key] = merged
        else:
            self.docs[key] = dict(doc)
        return {"_key": key}

    def delete(self, key: str, *, ignore_missing: bool = False) -> bool:
        if key in self.docs:
            del self.docs[key]
            return True
        if ignore_missing:
            return False
        raise KeyError(key)


def _make_db_with_cache_support(
    *,
    collections: list[dict[str, Any]] | None = None,
    counts: dict[str, int] | None = None,
    indexes: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[MagicMock, dict[str, _FakeCollection]]:
    """Build a mock db that supports both the fingerprint calls and the
    persistent cache calls, returning the collection store for assertions.
    """
    store: dict[str, _FakeCollection] = {}

    db = MagicMock()
    db.name = "testdb"
    db.collections.return_value = collections or []
    cnts = counts or {}
    idxs = indexes or {}

    def _has_collection(name: str) -> bool:
        return name in store

    def _create_collection(name: str, **kw: Any) -> _FakeCollection:
        store[name] = _FakeCollection()
        return store[name]

    def _collection(name: str) -> Any:
        if name in store:
            return store[name]
        # Fingerprint helpers call collection().count()/indexes() — return
        # a lightweight mock that answers those even for non-cache collections.
        m = MagicMock()
        m.count.return_value = cnts.get(name, 0)
        m.indexes.return_value = idxs.get(name, [])
        return m

    db.has_collection.side_effect = _has_collection
    db.create_collection.side_effect = _create_collection
    db.collection.side_effect = _collection
    return db, store


def _bundle_with_stats(stat_marker: int = 0) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={"entities": [{"label": "User"}]},
        physical_mapping={
            "entities": {"User": {"style": "COLLECTION", "collectionName": "users"}},
            "relationships": {},
        },
        metadata={"statistics": {"marker": stat_marker}},
        owl_turtle=None,
        source=MappingSource(
            kind="heuristic",
            fingerprint=None,
            generated_at_iso="2026-04-20T10:00:00+00:00",
            notes="test",
        ),
    )


# ---------------------------------------------------------------------------
# bundle_to_doc / bundle_from_doc
# ---------------------------------------------------------------------------


class TestBundleSerialization:
    """Round-trip guarantees are critical — the persistent cache is useless
    if deserialize(serialize(x)) loses information.
    """

    def test_roundtrip_preserves_all_fields(self):
        original = _bundle_with_stats(stat_marker=42)
        roundtripped = bundle_from_doc(bundle_to_doc(original))

        assert roundtripped.conceptual_schema == original.conceptual_schema
        assert roundtripped.physical_mapping == original.physical_mapping
        assert roundtripped.metadata == original.metadata
        assert roundtripped.owl_turtle == original.owl_turtle
        assert roundtripped.source == original.source

    def test_roundtrip_handles_none_source(self):
        bundle = MappingBundle(
            conceptual_schema={},
            physical_mapping={"entities": {}, "relationships": {}},
            metadata={},
            owl_turtle=None,
            source=None,
        )
        roundtripped = bundle_from_doc(bundle_to_doc(bundle))
        assert roundtripped.source is None

    def test_roundtrip_preserves_owl_turtle(self):
        bundle = MappingBundle(
            conceptual_schema={},
            physical_mapping={"entities": {}, "relationships": {}},
            metadata={},
            owl_turtle="@prefix ex: <http://example.org/> .",
            source=None,
        )
        roundtripped = bundle_from_doc(bundle_to_doc(bundle))
        assert roundtripped.owl_turtle == "@prefix ex: <http://example.org/> ."

    def test_from_doc_raises_on_missing_required_field(self):
        """Defensive decoding — missing required fields surface as KeyError,
        which the cache layer catches and treats as a cache miss.
        """
        with pytest.raises(KeyError):
            bundle_from_doc({"conceptual_schema": {}})  # missing the rest


# ---------------------------------------------------------------------------
# ArangoSchemaCache
# ---------------------------------------------------------------------------


class TestArangoSchemaCache:
    def test_get_returns_none_when_collection_absent(self):
        db, _store = _make_db_with_cache_support()
        cache = ArangoSchemaCache()
        assert cache.get(db) is None

    def test_get_returns_none_on_missing_document(self):
        db, store = _make_db_with_cache_support()
        store[DEFAULT_CACHE_COLLECTION] = _FakeCollection()  # exists, empty
        cache = ArangoSchemaCache()
        assert cache.get(db) is None

    def test_set_creates_collection_lazily_and_persists(self):
        db, store = _make_db_with_cache_support()
        cache = ArangoSchemaCache()
        bundle = _bundle_with_stats()

        ok = cache.set(
            db,
            bundle=bundle,
            shape_fingerprint="shape-a",
            full_fingerprint="full-a",
        )
        assert ok is True
        assert DEFAULT_CACHE_COLLECTION in store

        hit = cache.get(db)
        assert hit is not None
        cached, shape_fp, full_fp = hit
        assert shape_fp == "shape-a"
        assert full_fp == "full-a"
        assert cached.physical_mapping == bundle.physical_mapping

    def test_set_returns_false_when_collection_cannot_be_created(self):
        """Read-only users must not cause the caller to fail — the cache is
        a performance hint, not a source of truth.
        """
        db = MagicMock()
        db.has_collection.return_value = False
        db.create_collection.side_effect = Exception("permission denied")
        cache = ArangoSchemaCache()
        ok = cache.set(
            db,
            bundle=_bundle_with_stats(),
            shape_fingerprint="x",
            full_fingerprint="y",
        )
        assert ok is False

    def test_corrupt_bundle_is_treated_as_miss(self):
        db, store = _make_db_with_cache_support()
        col = _FakeCollection()
        col.docs["mapping"] = {
            "_key": "mapping",
            "schema_version": CACHE_SCHEMA_VERSION,
            "shape_fingerprint": "a",
            "full_fingerprint": "b",
            "bundle": {"conceptual_schema": {}},  # missing required fields
        }
        store[DEFAULT_CACHE_COLLECTION] = col
        cache = ArangoSchemaCache()
        assert cache.get(db) is None

    def test_stale_schema_version_is_ignored(self):
        db, store = _make_db_with_cache_support()
        col = _FakeCollection()
        col.docs["mapping"] = {
            "_key": "mapping",
            "schema_version": CACHE_SCHEMA_VERSION + 99,
            "shape_fingerprint": "a",
            "full_fingerprint": "b",
            "bundle": bundle_to_doc(_bundle_with_stats()),
        }
        store[DEFAULT_CACHE_COLLECTION] = col
        cache = ArangoSchemaCache()
        assert cache.get(db) is None

    def test_invalidate_removes_document(self):
        db, store = _make_db_with_cache_support()
        cache = ArangoSchemaCache()
        cache.set(
            db,
            bundle=_bundle_with_stats(),
            shape_fingerprint="a",
            full_fingerprint="b",
        )
        assert cache.get(db) is not None

        assert cache.invalidate(db) is True
        assert cache.get(db) is None

    def test_invalidate_on_missing_collection_is_noop(self):
        db, _store = _make_db_with_cache_support()
        cache = ArangoSchemaCache()
        assert cache.invalidate(db) is False  # nothing to delete


# ---------------------------------------------------------------------------
# describe_schema_change
# ---------------------------------------------------------------------------


class TestDescribeSchemaChange:
    def setup_method(self):
        _mapping_cache.clear()

    def test_no_cache_on_first_call(self):
        db, _ = _make_db_with_cache_support(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
        )
        report = describe_schema_change(db)
        assert isinstance(report, SchemaChangeReport)
        assert report.status == "no_cache"
        assert report.cached_shape_fingerprint is None
        assert report.cached_full_fingerprint is None
        assert report.unchanged is False
        assert report.needs_full_rebuild is True
        assert len(report.current_shape_fingerprint) == 64
        assert len(report.current_full_fingerprint) == 64

    def test_unchanged_after_get_mapping(self):
        db, _ = _make_db_with_cache_support(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
        )
        get_mapping(db, strategy="heuristic", cache_collection=None)

        report = describe_schema_change(db, cache_collection=None)
        assert report.status == "unchanged"
        assert report.unchanged is True
        assert report.needs_full_rebuild is False

    def test_stats_changed_when_counts_drift(self):
        """Row-count change without shape change must be reported as
        stats_changed — this is the whole point of the two-fingerprint split.
        """
        db, _ = _make_db_with_cache_support(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
        )
        get_mapping(db, strategy="heuristic", cache_collection=None)

        # Simulate writes: row count changed, indexes + collection set didn't.
        db.collections.return_value = [{"name": "users", "type": 2}]

        def _collection_with_new_count(name: str) -> Any:
            m = MagicMock()
            m.count.return_value = 500  # ← changed
            m.indexes.return_value = []
            return m

        db.collection.side_effect = _collection_with_new_count

        report = describe_schema_change(db, cache_collection=None)
        assert report.status == "stats_changed"
        assert report.needs_full_rebuild is False

    def test_shape_changed_when_collection_added(self):
        db, _ = _make_db_with_cache_support(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
        )
        get_mapping(db, strategy="heuristic", cache_collection=None)

        db.collections.return_value = [
            {"name": "users", "type": 2},
            {"name": "orders", "type": 2},
        ]

        report = describe_schema_change(db, cache_collection=None)
        assert report.status == "shape_changed"
        assert report.needs_full_rebuild is True

    def test_does_not_mutate_cache(self):
        """The probe must be read-only. Repeated calls must not invent cache
        entries out of thin air.
        """
        db, _ = _make_db_with_cache_support(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
        )
        report1 = describe_schema_change(db, cache_collection=None)
        report2 = describe_schema_change(db, cache_collection=None)
        assert report1.status == "no_cache"
        assert report2.status == "no_cache"


# ---------------------------------------------------------------------------
# get_mapping stats-only refresh path
# ---------------------------------------------------------------------------


class TestStatsOnlyRefresh:
    """When shape is stable but counts drift, conceptual + physical mapping
    must be reused verbatim and only the statistics block should be
    recomputed.
    """

    def setup_method(self):
        _mapping_cache.clear()

    def test_shape_stable_reuses_mapping(self):
        db, _ = _make_db_with_cache_support(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
        )
        bundle1 = get_mapping(db, strategy="heuristic", cache_collection=None)

        # Drift counts only.
        def _col_with_count(count: int):
            def _factory(name: str) -> Any:
                m = MagicMock()
                m.count.return_value = count
                m.indexes.return_value = []
                return m

            return _factory

        db.collection.side_effect = _col_with_count(9_999)
        bundle2 = get_mapping(db, strategy="heuristic", cache_collection=None)

        # Same conceptual + physical mapping content …
        assert bundle2.conceptual_schema == bundle1.conceptual_schema
        assert bundle2.physical_mapping == bundle1.physical_mapping

    def test_force_refresh_bypasses_cache(self):
        """``force_refresh=True`` must rebuild from scratch even when cached."""
        db, _ = _make_db_with_cache_support(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
        )
        bundle1 = get_mapping(db, strategy="heuristic", cache_collection=None)
        bundle2 = get_mapping(db, strategy="heuristic", cache_collection=None, force_refresh=True)
        assert bundle1 is not bundle2  # new object; cache was bypassed

    def test_cache_collection_none_disables_persistence(self):
        db, store = _make_db_with_cache_support(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
        )
        get_mapping(db, strategy="heuristic", cache_collection=None)
        assert DEFAULT_CACHE_COLLECTION not in store

    def test_cache_collection_default_persists(self):
        db, store = _make_db_with_cache_support(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
        )
        get_mapping(db, strategy="heuristic")
        assert DEFAULT_CACHE_COLLECTION in store
        assert "mapping" in store[DEFAULT_CACHE_COLLECTION].docs


# ---------------------------------------------------------------------------
# invalidate_cache
# ---------------------------------------------------------------------------


class TestInvalidateCache:
    def setup_method(self):
        _mapping_cache.clear()

    def test_clears_in_memory_cache(self):
        db, _ = _make_db_with_cache_support(collections=[{"name": "users", "type": 2}])
        get_mapping(db, strategy="heuristic", cache_collection=None)
        assert db.name in _mapping_cache

        invalidate_cache(db, cache_collection=None)
        assert db.name not in _mapping_cache

    def test_clears_persistent_cache(self):
        db, store = _make_db_with_cache_support(collections=[{"name": "users", "type": 2}])
        get_mapping(db, strategy="heuristic")
        assert "mapping" in store[DEFAULT_CACHE_COLLECTION].docs

        invalidate_cache(db)
        assert "mapping" not in store[DEFAULT_CACHE_COLLECTION].docs
