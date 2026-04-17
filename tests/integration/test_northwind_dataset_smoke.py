"""Integration tests for the Northwind e-commerce dataset."""
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


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if isinstance(v, str) and v else default


def _connect_db(db_name: str):
    url = _env("ARANGO_URL", "http://localhost:8529")
    user = _env("ARANGO_USER", "root")
    pw = _env("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=url)
    sys_db = client.db("_system", username=user, password=pw)
    if not sys_db.has_database(db_name):
        sys_db.create_database(db_name)
    return client.db(db_name, username=user, password=pw)


def _load_query_corpus() -> list[dict[str, Any]]:
    root = Path(__file__).resolve().parents[2]
    p = root / "tests" / "fixtures" / "datasets" / "northwind" / "query-corpus.yml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


_corpus = _load_query_corpus()
_db = None


def _get_db():
    global _db  # noqa: PLW0603
    if _db is None:
        _db = _connect_db("northwind_test")
        seed_northwind_dataset(_db)
    return _db


@pytest.mark.integration
@pytest.mark.parametrize("query", _corpus, ids=lambda q: q["id"])
def test_northwind_query(query: dict[str, Any]):
    db = _get_db()
    mapping = mapping_bundle_for("northwind_pg")
    out = translate(query["cypher"], mapping=mapping)
    rows = list(AqlExecutor(db).execute(out.to_aql_query()))
    assert len(rows) >= query["expected_min_count"], (
        f"[{query['id']}] expected >= {query['expected_min_count']} rows, got {len(rows)}: {rows!r}"
    )
