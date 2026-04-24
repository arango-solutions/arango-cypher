"""Tests for translation caching."""

from __future__ import annotations

import time

from arango_cypher import translate
from arango_cypher.api import _translate_cache, clear_translate_cache
from tests.helpers.mapping_fixtures import mapping_bundle_for


def test_cache_hit_returns_same_result() -> None:
    clear_translate_cache()
    mapping = mapping_bundle_for("movies_pg")
    cypher = "MATCH (p:Person) RETURN p.name"

    r1 = translate(cypher, mapping=mapping)
    r2 = translate(cypher, mapping=mapping)

    assert r1.aql == r2.aql
    assert r1.bind_vars == r2.bind_vars
    assert r1 is r2  # same object from cache


def test_different_queries_different_cache_entries() -> None:
    clear_translate_cache()
    mapping = mapping_bundle_for("movies_pg")

    r1 = translate("MATCH (p:Person) RETURN p.name", mapping=mapping)
    r2 = translate("MATCH (m:Movie) RETURN m.title", mapping=mapping)

    assert r1.aql != r2.aql
    assert len(_translate_cache) == 2


def test_cache_clear() -> None:
    clear_translate_cache()
    mapping = mapping_bundle_for("movies_pg")
    translate("MATCH (p:Person) RETURN p.name", mapping=mapping)
    assert len(_translate_cache) == 1

    evicted = clear_translate_cache()
    assert evicted == 1
    assert len(_translate_cache) == 0


def test_cached_translation_faster() -> None:
    clear_translate_cache()
    mapping = mapping_bundle_for("movies_pg")
    cypher = "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) WHERE m.released > 2000 RETURN p.name, m.title"

    start = time.perf_counter()
    for _ in range(100):
        clear_translate_cache()
        translate(cypher, mapping=mapping)
    uncached_time = time.perf_counter() - start

    clear_translate_cache()
    translate(cypher, mapping=mapping)  # prime the cache

    start = time.perf_counter()
    for _ in range(100):
        translate(cypher, mapping=mapping)
    cached_time = time.perf_counter() - start

    assert cached_time < uncached_time, (
        f"Cached ({cached_time:.4f}s) should be faster than uncached ({uncached_time:.4f}s)"
    )
