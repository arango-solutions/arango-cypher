"""Tests for the Wave 4m HTTP surface: /schema/status + /schema/invalidate-cache.

These tests override the session dependency so no real Arango connection is
required; the fake db mirrors ``tests/test_schema_change_detection.py`` so
the two suites cover the Python API and the HTTP API against identical
test doubles.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from arango_cypher.schema_acquire import _mapping_cache
from arango_cypher.service import _get_session, _Session, app
from arango_query_core import MappingBundle, MappingSource

client = TestClient(app)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self.docs.get(key)

    def insert(self, doc: dict[str, Any], *, overwrite: bool = False, silent: bool = False) -> dict[str, Any]:
        key = doc["_key"]
        if key in self.docs and not overwrite:
            raise RuntimeError("duplicate key")
        self.docs[key] = dict(doc)
        return {"_key": key}

    def update(self, doc: dict[str, Any], *, merge: bool = True, keep_none: bool = True) -> dict[str, Any]:
        key = doc["_key"]
        if merge and key in self.docs:
            self.docs[key] = {**self.docs[key], **doc}
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


class _MutableFakeDb:
    """Wraps a MagicMock db + the backing state, exposed so tests can
    mutate collection lists / counts / indexes between HTTP calls to
    simulate live schema drift.
    """

    def __init__(
        self,
        collections: list[dict[str, Any]],
        counts: dict[str, int],
        indexes: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._collections = list(collections)
        self.counts = dict(counts)
        self.indexes = dict(indexes)
        self.store: dict[str, _FakeCollection] = {}

        db = MagicMock()
        db.name = "testdb"
        db.collections.side_effect = lambda: list(self._collections)

        def _has_collection(name: str) -> bool:
            return name in self.store

        def _create_collection(name: str, **kw: Any) -> _FakeCollection:
            self.store[name] = _FakeCollection()
            return self.store[name]

        def _collection(name: str) -> Any:
            if name in self.store:
                return self.store[name]
            m = MagicMock()
            m.count.side_effect = lambda: self.counts.get(name, 0)
            m.indexes.side_effect = lambda: list(self.indexes.get(name, []))
            return m

        db.has_collection.side_effect = _has_collection
        db.create_collection.side_effect = _create_collection
        db.collection.side_effect = _collection
        self.db = db

    def set_collections(self, collections: list[dict[str, Any]]) -> None:
        self._collections = list(collections)


def _seed_cache(fake: _MutableFakeDb, *, stat_marker: int = 0) -> None:
    from arango_cypher.schema_acquire import (
        _cache_key,
        _full_fingerprint,
        _shape_fingerprint,
    )
    from arango_cypher.schema_acquire import (
        _mapping_cache as _mc,
    )
    from arango_cypher.schema_cache import ArangoSchemaCache

    bundle = MappingBundle(
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
    shape_fp = _shape_fingerprint(fake.db)
    full_fp = _full_fingerprint(fake.db)
    _mc[_cache_key(fake.db)] = (bundle, time.time(), shape_fp, full_fp)
    ArangoSchemaCache().set(
        fake.db, bundle=bundle, shape_fingerprint=shape_fp, full_fingerprint=full_fp
    )


@pytest.fixture
def fake_session_factory():
    """Install a fake session and return the mutable fake-db wrapper.

    The wrapper's ``counts`` / ``indexes`` dicts are live — mutating them
    between HTTP calls simulates schema drift against the same session.
    """

    def _factory(**db_kwargs: Any) -> _MutableFakeDb:
        fake = _MutableFakeDb(
            collections=db_kwargs.get("collections", []),
            counts=db_kwargs.get("counts", {}),
            indexes=db_kwargs.get("indexes", {}),
        )
        session = _Session.__new__(_Session)
        session.token = "test-token"
        session.db = fake.db
        session.client = MagicMock()
        session.created_at = 0.0
        session.last_used = 0.0
        app.dependency_overrides[_get_session] = lambda: session
        return fake

    yield _factory
    app.dependency_overrides.pop(_get_session, None)
    _mapping_cache.clear()


# ---------------------------------------------------------------------------
# GET /schema/status
# ---------------------------------------------------------------------------


class TestSchemaStatus:
    def test_requires_session(self):
        resp = client.get("/schema/status")
        assert resp.status_code == 401

    def test_no_cache_status_on_first_call(self, fake_session_factory):
        fake_session_factory(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 0},
            indexes={"users": []},
        )
        resp = client.get("/schema/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "no_cache"
        assert body["unchanged"] is False
        assert body["needs_full_rebuild"] is True
        assert body["cached_shape_fingerprint"] is None
        assert body["cached_full_fingerprint"] is None
        assert body["current_shape_fingerprint"]
        assert body["current_full_fingerprint"]

    def test_unchanged_when_cache_matches(self, fake_session_factory):
        fake = fake_session_factory(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
            indexes={"users": []},
        )
        _seed_cache(fake)

        resp = client.get("/schema/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "unchanged"
        assert body["unchanged"] is True
        assert body["needs_full_rebuild"] is False
        assert body["cached_shape_fingerprint"] == body["current_shape_fingerprint"]
        assert body["cached_full_fingerprint"] == body["current_full_fingerprint"]

    def test_stats_changed_when_only_counts_differ(self, fake_session_factory):
        fake = fake_session_factory(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
            indexes={"users": []},
        )
        _seed_cache(fake)

        fake.counts["users"] = 500

        resp = client.get("/schema/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "stats_changed"
        assert body["needs_full_rebuild"] is False
        assert body["cached_shape_fingerprint"] == body["current_shape_fingerprint"]
        assert body["cached_full_fingerprint"] != body["current_full_fingerprint"]

    def test_shape_changed_when_index_added(self, fake_session_factory):
        fake = fake_session_factory(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
            indexes={"users": []},
        )
        _seed_cache(fake)

        fake.indexes["users"] = [
            {"type": "persistent", "fields": ["email"], "unique": True}
        ]

        resp = client.get("/schema/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "shape_changed"
        assert body["needs_full_rebuild"] is True
        assert body["cached_shape_fingerprint"] != body["current_shape_fingerprint"]

    def test_shape_changed_when_collection_added(self, fake_session_factory):
        fake = fake_session_factory(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
            indexes={"users": []},
        )
        _seed_cache(fake)

        fake.set_collections(
            [
                {"name": "users", "type": 2},
                {"name": "orders", "type": 2},
            ]
        )
        fake.counts["orders"] = 5

        resp = client.get("/schema/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "shape_changed"
        assert body["needs_full_rebuild"] is True


# ---------------------------------------------------------------------------
# POST /schema/invalidate-cache
# ---------------------------------------------------------------------------


class TestInvalidateCache:
    def test_requires_session(self):
        resp = client.post("/schema/invalidate-cache")
        assert resp.status_code == 401

    def test_clears_both_tiers_by_default(self, fake_session_factory):
        fake = fake_session_factory(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
            indexes={"users": []},
        )
        _seed_cache(fake)

        pre = client.get("/schema/status").json()
        assert pre["status"] == "unchanged"

        resp = client.post("/schema/invalidate-cache")
        assert resp.status_code == 200
        assert resp.json() == {"invalidated": True, "persistent": True}

        post = client.get("/schema/status").json()
        assert post["status"] == "no_cache"

    def test_persistent_false_preserves_tier_2(self, fake_session_factory):
        fake = fake_session_factory(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 10},
            indexes={"users": []},
        )
        _seed_cache(fake)

        resp = client.post("/schema/invalidate-cache?persistent=false")
        assert resp.status_code == 200
        assert resp.json() == {"invalidated": True, "persistent": False}

        # Persistent cache survives, so the next probe still reports unchanged
        # (describe_schema_change falls through to tier 2 when tier 1 is empty).
        post = client.get("/schema/status").json()
        assert post["status"] == "unchanged"

    def test_invalidate_on_empty_cache_is_noop(self, fake_session_factory):
        fake_session_factory(
            collections=[{"name": "users", "type": 2}],
            counts={"users": 0},
            indexes={"users": []},
        )
        resp = client.post("/schema/invalidate-cache")
        assert resp.status_code == 200
        assert resp.json() == {"invalidated": True, "persistent": True}
