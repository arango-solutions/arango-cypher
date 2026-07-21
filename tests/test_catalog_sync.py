"""Tests for arango_cypher.catalog.sync — the out-of-band catalog refresher."""

from __future__ import annotations

from arango_query_core import MappingBundle, MappingSource

import arango_cypher.catalog.sync as sync_mod
from arango_cypher.catalog.registry import CatalogRegistry, DatabaseEntry
from arango_cypher.catalog.sync import sync_entry, sync_once


def _bundle(n_entities: int = 2, n_rels: int = 1) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={
            "entities": [{"name": f"E{i}"} for i in range(n_entities)],
            "relationships": [{"type": f"R{i}"} for i in range(n_rels)],
        },
        physical_mapping={"entities": {}, "relationships": {}},
        metadata={},
        source=MappingSource(kind="schema_analyzer_export"),
    )


def _entry(graphs=()) -> DatabaseEntry:
    return DatabaseEntry(
        name="kg",
        url="https://host/",
        database="KG",
        username="root",
        password="pw",
        graphs=tuple(graphs),
    )


class TestSyncEntry:
    def test_sync_whole_db_and_each_graph(self, monkeypatch):
        calls: list[str | None] = []

        def fake_get_mapping(db, *, force_refresh, graph_name=None, **kw):
            assert force_refresh is True
            calls.append(graph_name)
            return _bundle()

        monkeypatch.setattr(sync_mod, "_connect", lambda entry: object())
        monkeypatch.setattr(
            "arango_cypher.schema_acquire.get_mapping", fake_get_mapping
        )

        results = sync_entry(_entry(graphs=["G1", "G2"]))

        # Unscoped + each named graph.
        assert calls == [None, "G1", "G2"]
        assert all(r.ok for r in results)
        assert [r.graph for r in results] == [None, "G1", "G2"]
        assert results[0].entities == 2
        assert results[0].relationships == 1
        assert results[0].source == "schema_analyzer_export"

    def test_connect_failure_yields_single_failed_result(self, monkeypatch):
        def boom(entry):
            raise RuntimeError("no route to host")

        monkeypatch.setattr(sync_mod, "_connect", boom)
        results = sync_entry(_entry())
        assert len(results) == 1
        assert results[0].ok is False
        assert "connect failed" in results[0].error

    def test_one_target_failure_does_not_abort_others(self, monkeypatch):
        def fake_get_mapping(db, *, force_refresh, graph_name=None, **kw):
            if graph_name == "G1":
                raise ValueError("analyzer exploded")
            return _bundle()

        monkeypatch.setattr(sync_mod, "_connect", lambda entry: object())
        monkeypatch.setattr(
            "arango_cypher.schema_acquire.get_mapping", fake_get_mapping
        )

        results = sync_entry(_entry(graphs=["G1", "G2"]))
        by_graph = {r.graph: r for r in results}
        assert by_graph[None].ok is True
        assert by_graph["G1"].ok is False
        assert "analyzer exploded" in by_graph["G1"].error
        assert by_graph["G2"].ok is True


class TestSyncOnce:
    def test_iterates_all_databases(self, monkeypatch):
        seen: list[str] = []

        def fake_get_mapping(db, *, force_refresh, graph_name=None, **kw):
            seen.append(getattr(db, "_name", "?"))
            return _bundle()

        class _DB:
            def __init__(self, name):
                self._name = name

        registry = CatalogRegistry(
            databases=(
                DatabaseEntry(name="a", url="u", database="A", username="r", password="p"),
                DatabaseEntry(name="b", url="u", database="B", username="r", password="p"),
            ),
            interval_seconds=10,
            source="test",
        )
        monkeypatch.setattr(sync_mod, "_connect", lambda entry: _DB(entry.database))
        monkeypatch.setattr(
            "arango_cypher.schema_acquire.get_mapping", fake_get_mapping
        )

        results = sync_once(registry)
        assert len(results) == 2
        assert all(r.ok for r in results)
        assert set(seen) == {"A", "B"}

    def test_empty_registry_returns_empty(self):
        registry = CatalogRegistry(databases=(), source="empty")
        assert sync_once(registry) == []
