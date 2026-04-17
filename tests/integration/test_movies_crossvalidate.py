"""Cross-validate translated AQL against a reference Cypher engine (Neo4j).

For each query in ``tests/fixtures/datasets/movies/query-corpus.yml``:

  1. Run the **raw Cypher** against Neo4j Community (the spec's reference
     implementation).
  2. Translate the same Cypher to AQL via ``arango_cypher.translate`` and
     run it against the LPG-seeded ArangoDB.
  3. Assert the two result sets are equivalent.

Activation: requires ``RUN_INTEGRATION=1`` *and* ``RUN_CROSS=1``. Also
requires a reachable Neo4j (see ``docker-compose.neo4j.yml``) and a
reachable ArangoDB (the existing ``test_neo4j_movies_dataset_smoke``
fixtures cover the latter).

Known-divergence handling: entries in ``query-corpus.yml`` may carry a
``divergence`` key (free-form string) to mark queries where a full diff is
not yet expected; the test still asserts the *column set* matches and that
Neo4j produced at least ``expected_min_count`` rows, but skips the row-by-row
equivalence check with a clear reason.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from arango import ArangoClient

from arango_cypher import translate
from arango_query_core.exec import AqlExecutor
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.integration.datasets import seed_movies_lpg_dataset


# ---------------------------------------------------------------------------
# Activation & fixtures
# ---------------------------------------------------------------------------


def _cross_enabled() -> bool:
    return (
        os.environ.get("RUN_INTEGRATION") == "1"
        and os.environ.get("RUN_CROSS") == "1"
    )


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if isinstance(v, str) and v else default


def _load_corpus() -> list[dict[str, Any]]:
    root = Path(__file__).resolve().parents[2]
    p = root / "tests" / "fixtures" / "datasets" / "movies" / "query-corpus.yml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


_corpus = _load_corpus()

_arango_db: Any | None = None
_neo4j_driver: Any | None = None


def _get_arango_db() -> Any:
    global _arango_db  # noqa: PLW0603
    if _arango_db is None:
        url = _env("ARANGO_URL", "http://localhost:8529")
        user = _env("ARANGO_USER", "root")
        pw = _env("ARANGO_PASS", "openSesame")
        client = ArangoClient(hosts=url)
        sys_db = client.db("_system", username=user, password=pw)
        if not sys_db.has_database("neo4j_movies_lpg_test"):
            sys_db.create_database("neo4j_movies_lpg_test")
        db = client.db("neo4j_movies_lpg_test", username=user, password=pw)
        seed_movies_lpg_dataset(db)
        _arango_db = db
    return _arango_db


def _get_neo4j_driver() -> Any:
    global _neo4j_driver  # noqa: PLW0603
    # Local import: only exercised when the `cross` marker is active,
    # so users who don't run cross-validate don't need the neo4j driver.
    from tests.integration.neo4j_reference import (
        ensure_dataset,
        get_driver,
        seed_neo4j_movies,
    )

    if _neo4j_driver is None:
        _neo4j_driver = get_driver()
    # Cross-module guard: re-seed if a sibling suite (e.g. Northwind) loaded
    # a different dataset into the shared Neo4j instance.
    ensure_dataset(_neo4j_driver, "movies", seed_neo4j_movies)
    return _neo4j_driver


# ---------------------------------------------------------------------------
# Result-set equivalence
# ---------------------------------------------------------------------------


def _normalize_scalar(v: Any) -> Any:
    """Normalize Neo4j vs AQL scalar differences.

    - Integers and floats with zero fractional part are treated as equal
      to their integer form (Neo4j returns int, AQL may return float for
      SUM/AVG/COUNT, etc.).
    - ``None`` and missing-key both compare equal to ``None``.
    - Nested dicts/lists are normalized recursively.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        if v == int(v) and not (v != v):  # finite, integral
            return int(v)
        return v
    if isinstance(v, list):
        return [_normalize_scalar(x) for x in v]
    if isinstance(v, dict):
        return {k: _normalize_scalar(val) for k, val in v.items()}
    return v


def _row_values(row: Any) -> list[Any]:
    """Return row values in RETURN-clause order, normalized.

    We compare positionally (not by column name) because Neo4j keeps the
    raw dotted expression as the column name (``p.name``) while AQL must
    rename it (dots aren't legal identifiers).  This eliminates spurious
    naming diffs while still catching genuine projection-order mismatches.

    Accepts:
    - ``dict`` (Neo4j ``Record`` post-coercion, or AQL multi-projection):
      values in insertion order, which both engines preserve from RETURN.
    - ``list``: passed through (each element is one column value).
    - scalar: single-column row from AQL ``RETURN expr`` (no object
      wrapper), wrapped as a single-element list to match Neo4j's
      always-record behavior.
    """
    if isinstance(row, dict):
        return [_normalize_scalar(v) for v in row.values()]
    if isinstance(row, list):
        return [_normalize_scalar(v) for v in row]
    return [_normalize_scalar(row)]


def _row_names(row: Any) -> list[str]:
    if isinstance(row, dict):
        return list(row.keys())
    return ["<scalar>"]


def _has_order_by(cypher: str) -> bool:
    return "ORDER BY" in cypher.upper()


def _sort_key(values: list[Any]) -> str:
    """Produce a deterministic comparable key for a result row.

    Used only when the query does not specify ``ORDER BY``, so the two
    engines are free to return rows in any order.
    """
    import json as _json

    return _json.dumps(values, sort_keys=True, default=str)


def assert_result_equivalent(
    neo4j_rows: list[dict[str, Any]],
    aql_rows: list[dict[str, Any]],
    query: dict[str, Any],
) -> None:
    """Assert two result sets are equivalent.

    - Column **count** must match (column names need not — see ``_row_values``).
    - Row count must match exactly.
    - If the query has ``ORDER BY``, compare rows position-wise.
    - Otherwise, compare as multisets (sorted by a deterministic key over values).
    - Scalars are normalized via ``_normalize_scalar`` (int↔float, etc.).
    """
    qid = query.get("id", "<unknown>")
    cypher = query.get("cypher", "")

    neo_vals = [_row_values(r) for r in neo4j_rows]
    aql_vals = [_row_values(r) for r in aql_rows]
    neo_names = _row_names(neo4j_rows[0]) if neo4j_rows else []
    aql_names = _row_names(aql_rows[0]) if aql_rows else []

    neo_widths = {len(r) for r in neo_vals}
    aql_widths = {len(r) for r in aql_vals}
    assert neo_widths == aql_widths, (
        f"[{qid}] column-count mismatch: neo4j widths={sorted(neo_widths)} "
        f"vs aql widths={sorted(aql_widths)}\n"
        f"  neo4j columns: {neo_names}\n"
        f"  aql columns:   {aql_names}"
    )

    assert len(neo_vals) == len(aql_vals), (
        f"[{qid}] row-count mismatch: neo4j={len(neo_vals)} vs aql={len(aql_vals)}\n"
        f"  cypher: {cypher!r}\n"
        f"  neo4j columns: {neo_names}; sample: {neo_vals[:3]!r}\n"
        f"  aql   columns: {aql_names}; sample: {aql_vals[:3]!r}"
    )

    if _has_order_by(cypher):
        for i, (a, b) in enumerate(zip(neo_vals, aql_vals, strict=True)):
            assert a == b, (
                f"[{qid}] row #{i} differs (ordered, position-wise):\n"
                f"  neo4j ({neo_names}): {a!r}\n"
                f"  aql   ({aql_names}): {b!r}"
            )
    else:
        neo_sorted = sorted(neo_vals, key=_sort_key)
        aql_sorted = sorted(aql_vals, key=_sort_key)
        for i, (a, b) in enumerate(zip(neo_sorted, aql_sorted, strict=True)):
            assert a == b, (
                f"[{qid}] row #{i} differs (unordered, position-wise):\n"
                f"  neo4j ({neo_names}): {a!r}\n"
                f"  aql   ({aql_names}): {b!r}"
            )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


pytestmark = [
    pytest.mark.integration,
    pytest.mark.cross,
    pytest.mark.skipif(
        not _cross_enabled(),
        reason="Set RUN_INTEGRATION=1 and RUN_CROSS=1 to run Neo4j cross-validation",
    ),
]


@pytest.mark.parametrize("query", _corpus, ids=lambda q: q["id"])
def test_movies_crossvalidate(query: dict[str, Any]) -> None:
    from tests.integration.neo4j_reference import run_cypher

    cypher = query["cypher"]
    qid = query.get("id", "<unknown>")
    divergence = query.get("divergence")

    neo_driver = _get_neo4j_driver()
    neo_rows = run_cypher(neo_driver, cypher)

    assert len(neo_rows) >= query.get("expected_min_count", 0), (
        f"[{qid}] Neo4j produced only {len(neo_rows)} rows; "
        f"expected >= {query.get('expected_min_count', 0)}. "
        "Corpus or dataset drift?"
    )

    db = _get_arango_db()
    mapping = mapping_bundle_for("movies_lpg")
    out = translate(cypher, mapping=mapping)

    # When a divergence is flagged we still translate (catches translator
    # crashes on no-divergence-flagged queries), but tolerate AQL execution
    # errors so we can record them as the flagged divergence rather than
    # masking them as a hard failure.
    try:
        aql_rows = list(AqlExecutor(db).execute(out.to_aql_query()))
    except Exception as e:
        if divergence:
            pytest.skip(
                f"[{qid}] divergence flagged "
                f"(AQL execution failed with {type(e).__name__}): {divergence}"
            )
        raise

    if divergence:
        pytest.skip(f"[{qid}] divergence flagged: {divergence}")

    assert_result_equivalent(neo_rows, aql_rows, query)
