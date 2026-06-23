"""Tests for the on-demand background warm scheduler (catalog A.2).

These pin the dedupe + lifecycle contract of :mod:`arango_cypher.catalog.warm`:
a catalog miss schedules at most one analysis per (database, graph) at a time,
the in-flight flag clears when the warm finishes (success *or* failure), and a
handle without a resolvable name is rejected rather than crashing.

The real analyzer is never invoked: ``schema_acquire.get_mapping`` is stubbed
with a barrier so the test controls exactly when the background warm completes.
"""

from __future__ import annotations

import threading

import pytest

from arango_cypher.catalog import warm


class _FakeDB:
    def __init__(self, name: str = "demo"):
        self.name = name


@pytest.fixture(autouse=True)
def _clear_inflight():
    """Each test starts and ends with an empty in-flight set."""
    with warm._lock:  # noqa: SLF001 - test needs to reset module state
        warm._inflight.clear()
    yield
    with warm._lock:  # noqa: SLF001
        warm._inflight.clear()


def test_warm_key_scopes_by_graph():
    assert warm._warm_key("db", None) != warm._warm_key("db", "g1")  # noqa: SLF001
    assert warm._warm_key("db", "g1") == warm._warm_key("db", "g1")  # noqa: SLF001


def test_schedule_warm_runs_get_mapping(monkeypatch):
    from arango_cypher import schema_acquire

    done = threading.Event()
    calls: list[dict] = []

    def _fake_get_mapping(db, *, force_refresh=False, graph_name=None):
        calls.append(
            {"db": db, "force_refresh": force_refresh, "graph_name": graph_name}
        )
        done.set()
        return object()

    monkeypatch.setattr(schema_acquire, "get_mapping", _fake_get_mapping)

    db = _FakeDB("demo")
    started = warm.schedule_warm(db, "g1")
    assert started is True
    assert done.wait(timeout=5.0), "warm thread never invoked get_mapping"

    # Wait for the thread to clear the in-flight flag in its finally block.
    deadline = threading.Event()
    for _ in range(50):
        if not warm.is_warming("demo", "g1"):
            break
        deadline.wait(0.02)
    assert not warm.is_warming("demo", "g1")

    assert len(calls) == 1
    assert calls[0]["force_refresh"] is True
    assert calls[0]["graph_name"] == "g1"


def test_schedule_warm_dedupes_concurrent_targets(monkeypatch):
    from arango_cypher import schema_acquire

    release = threading.Event()
    entered = threading.Event()
    call_count = {"n": 0}

    def _blocking_get_mapping(db, *, force_refresh=False, graph_name=None):
        call_count["n"] += 1
        entered.set()
        release.wait(timeout=5.0)
        return object()

    monkeypatch.setattr(schema_acquire, "get_mapping", _blocking_get_mapping)

    db = _FakeDB("demo")
    first = warm.schedule_warm(db, None)
    assert first is True
    assert entered.wait(timeout=5.0)
    assert warm.is_warming("demo", None)

    # A second schedule for the same target while the first is in flight is a
    # no-op (deduped) — no second analyzer pass.
    second = warm.schedule_warm(db, None)
    assert second is False

    # A different graph scope is a distinct target and *does* start.
    third = warm.schedule_warm(db, "other")
    assert third is True

    release.set()
    for _ in range(100):
        if not warm.is_warming("demo", None) and not warm.is_warming("demo", "other"):
            break
        threading.Event().wait(0.02)
    assert not warm.is_warming("demo", None)
    # demo+None ran once; demo+other ran once → exactly two analyzer passes.
    assert call_count["n"] == 2


def test_schedule_warm_clears_flag_on_failure(monkeypatch):
    from arango_cypher import schema_acquire

    done = threading.Event()

    def _boom(db, *, force_refresh=False, graph_name=None):
        try:
            raise RuntimeError("analyzer exploded")
        finally:
            done.set()

    monkeypatch.setattr(schema_acquire, "get_mapping", _boom)

    assert warm.schedule_warm(_FakeDB("demo"), None) is True
    assert done.wait(timeout=5.0)
    for _ in range(50):
        if not warm.is_warming("demo", None):
            break
        threading.Event().wait(0.02)
    assert not warm.is_warming("demo", None)


def test_schedule_warm_rejects_nameless_handle():
    class _Nameless:
        @property
        def name(self):
            raise RuntimeError("no name")

    assert warm.schedule_warm(_Nameless(), None) is False


def test_schedule_warm_rejects_empty_name():
    assert warm.schedule_warm(_FakeDB(""), None) is False
