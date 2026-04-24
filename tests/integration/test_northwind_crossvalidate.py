"""Cross-validate translated AQL against Neo4j for the Northwind corpus.

Mirrors :mod:`tests.integration.test_movies_crossvalidate` but for the
Northwind PG-style dataset (one collection per label, one edge collection
per relationship type).

Activation: requires both ``RUN_INTEGRATION=1`` and ``RUN_CROSS=1``.
Same Neo4j container as the movies test (``docker-compose.neo4j.yml``);
the seeder wipes the graph and reloads from
``tests/fixtures/datasets/northwind/pg-data.json``.

Result equivalence is delegated to the helper from
``test_movies_crossvalidate`` so we have a single source of truth for
column-count, row-count, and ordered/unordered comparison semantics.
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
from tests.integration.datasets import seed_northwind_dataset
from tests.integration.test_movies_crossvalidate import assert_result_equivalent


def _cross_enabled() -> bool:
    return os.environ.get("RUN_INTEGRATION") == "1" and os.environ.get("RUN_CROSS") == "1"


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if isinstance(v, str) and v else default


def _load_corpus() -> list[dict[str, Any]]:
    root = Path(__file__).resolve().parents[2]
    p = root / "tests" / "fixtures" / "datasets" / "northwind" / "query-corpus.yml"
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
        if not sys_db.has_database("northwind_cross_test"):
            sys_db.create_database("northwind_cross_test")
        db = client.db("northwind_cross_test", username=user, password=pw)
        seed_northwind_dataset(db)
        _arango_db = db
    return _arango_db


def _get_neo4j_driver() -> Any:
    global _neo4j_driver  # noqa: PLW0603
    from tests.integration.neo4j_reference import (
        ensure_dataset,
        get_driver,
        seed_neo4j_northwind,
    )

    if _neo4j_driver is None:
        _neo4j_driver = get_driver()
    ensure_dataset(_neo4j_driver, "northwind", seed_neo4j_northwind)
    return _neo4j_driver


pytestmark = [
    pytest.mark.integration,
    pytest.mark.cross,
    pytest.mark.skipif(
        not _cross_enabled(),
        reason="Set RUN_INTEGRATION=1 and RUN_CROSS=1 to run Neo4j cross-validation",
    ),
]


@pytest.mark.parametrize("query", _corpus, ids=lambda q: q["id"])
def test_northwind_crossvalidate(query: dict[str, Any]) -> None:
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
    mapping = mapping_bundle_for("northwind_pg")
    out = translate(cypher, mapping=mapping)

    try:
        aql_rows = list(AqlExecutor(db).execute(out.to_aql_query()))
    except Exception as e:
        if divergence:
            pytest.skip(
                f"[{qid}] divergence flagged (AQL execution failed with {type(e).__name__}): {divergence}"
            )
        raise

    if divergence:
        pytest.skip(f"[{qid}] divergence flagged: {divergence}")

    assert_result_equivalent(neo_rows, aql_rows, query)
