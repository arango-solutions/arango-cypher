from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import translate
from arango_query_core.exec import AqlExecutor
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.integration.seed import seed_social_dataset


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

MODES: list[tuple[str, str, str]] = [
    ("pg", "cypher_pg_fixture", "pg"),
    ("lpg", "cypher_lpg_fixture", "lpg"),
    ("hybrid", "cypher_hybrid_fixture", "hybrid"),
]


def _run(
    *,
    mode: str,
    db_name: str,
    mapping_fixture: str,
    cypher: str,
    params: dict[str, Any] | None = None,
) -> list[Any]:
    db = _connect_db(db_name)
    seed_social_dataset(db, mode=mode)
    out = translate(cypher, mapping=mapping_bundle_for(mapping_fixture), params=params)
    cur = AqlExecutor(db).execute(out.to_aql_query())
    return list(cur)


def _pairs(rows: list[dict[str, Any]]) -> list[tuple[str | None, str | None]]:
    return sorted((r.get("id"), r.get("v_id")) for r in rows)


@pytest.mark.integration
@pytest.mark.parametrize("mode,db_name,mapping_fixture", MODES)
def test_execute_translated_node_filter_query(mode: str, db_name: str, mapping_fixture: str):
    rows = _run(
        mode=mode,
        db_name=db_name,
        mapping_fixture=mapping_fixture,
        cypher="MATCH (n:User) WHERE n.id = $id RETURN n.name",
        params={"id": "u1"},
    )
    assert rows == ["Alice"]


ScenarioCheck = Callable[[list[Any]], None]


def check_pairs(expected: list[tuple[str, str]]) -> ScenarioCheck:
    def _check(rows: list[Any]) -> None:
        assert _pairs(rows) == expected

    return _check


def check_scalar_set(expected: set[str]) -> ScenarioCheck:
    def _check(rows: list[Any]) -> None:
        assert set(rows) == expected

    return _check


def check_type_and_pairs(expected: list[tuple[str, str]]) -> ScenarioCheck:
    def _check(rows: list[Any]) -> None:
        assert rows, "expected at least one row"
        assert all(isinstance(r, dict) for r in rows)
        assert all(r.get("type") == "FOLLOWS" for r in rows)
        assert _pairs(rows) == expected

    return _check


def check_exact_rows(expected: list[Any]) -> ScenarioCheck:
    def _check(rows: list[Any]) -> None:
        assert rows == expected

    return _check


def check_counter(expected: list[Any]) -> ScenarioCheck:
    def _check(rows: list[Any]) -> None:
        assert Counter(rows) == Counter(expected)

    return _check


def check_set_of_tuples(key1: str, key2: str, expected: set[tuple[Any, Any]]) -> ScenarioCheck:
    def _check(rows: list[Any]) -> None:
        assert {((r or {}).get(key1), (r or {}).get(key2)) for r in rows} == expected

    return _check


def check_avg_by_state_rows() -> ScenarioCheck:
    def _check(rows: list[Any]) -> None:
        assert [r.get("s") for r in rows] == ["MA", "NY", "CA"]
        assert abs(rows[0].get("a") - 31.0) < 1e-9
        assert abs(rows[1].get("a") - 21.5) < 1e-9
        assert abs(rows[2].get("a") - 20.0) < 1e-9

    return _check


OUTBOUND = [("u1", "u2"), ("u1", "u3"), ("u2", "u3")]
INBOUND = [("u2", "u1"), ("u3", "u1"), ("u3", "u2")]

QUERY_CASES: list[tuple[str, dict[str, Any] | None, ScenarioCheck]] = [
    ("MATCH (u:User)-[:FOLLOWS]->(v:User) RETURN u.id, v.id", None, check_pairs(OUTBOUND)),
    ("MATCH (u:User)<-[:FOLLOWS]-(v:User) RETURN u.id, v.id", None, check_pairs(INBOUND)),
    (
        "MATCH (u:User)-[:FOLLOWS]->(v:User)-[:FOLLOWS]->(w:User) RETURN u.id, w.id ORDER BY u.id, w.id",
        None,
        check_exact_rows([{"id": "u1", "w_id": "u3"}]),
    ),
    (
        "MATCH (u:User)\nWITH u, null AS x\nWHERE x IS NULL\nRETURN u.id\nORDER BY u.id\nLIMIT 2",
        None,
        check_exact_rows(["u1", "u2"]),
    ),
    (
        "MATCH (u:User) RETURN DISTINCT coalesce(u.city, \"X\") AS c ORDER BY c",
        None,
        check_exact_rows(["Boston", "NYC", "SF"]),
    ),
    ("MATCH (u:User)-[:FOLLOWS]->(v:User) WHERE u.id = $id RETURN v.id", {"id": "u1"}, check_scalar_set({"u2", "u3"})),
    ("MATCH (u:User)-[:FOLLOWS]->(v:User) WHERE v.city = \"SF\" RETURN u.id, v.id", None, check_pairs([("u1", "u2")])),
    ("MATCH (u:User)-[:FOLLOWS]->(v:User) RETURN DISTINCT v.city", None, check_scalar_set({"SF", "Boston"})),
    (
        "MATCH (u:User)-[:FOLLOWS]->(v:User) WHERE u.active = true AND v.active = true RETURN u.id, v.id",
        None,
        check_pairs([("u1", "u2")]),
    ),
    ("MATCH (u:User)-[r:FOLLOWS]->(v:User) RETURN type(r), u.id, v.id", None, check_type_and_pairs(OUTBOUND)),

    # WITH + aggregation coverage (C019-C026 equivalents)
    (
        "MATCH (u:User)\nWITH u.city AS city, count(*) AS c\nRETURN city, c\nORDER BY c DESC\nLIMIT 10",
        None,
        check_exact_rows(
            [
            {"city": "Boston", "c": 3},
            {"city": "NYC", "c": 2},
            {"city": "SF", "c": 1},
            ]
        ),
    ),
    (
        "MATCH (u:User)-[:FOLLOWS]->(v:User)\nWITH u, count(v) AS degree\nRETURN u.id, degree\nORDER BY degree DESC\nLIMIT 10",
        None,
        check_exact_rows([{"id": "u1", "degree": 2}, {"id": "u2", "degree": 1}]),
    ),
    (
        "MATCH (u:User)\nWITH u.city AS city\nWHERE city IS NOT NULL\nRETURN city",
        None,
        check_counter(["Boston", "Boston", "Boston", "NYC", "NYC", "SF"]),
    ),
    (
        "MATCH (u:User)\nWITH DISTINCT u.city AS city\nRETURN city\nORDER BY city",
        None,
        check_exact_rows(["Boston", "NYC", "SF"]),
    ),
    (
        "MATCH (u:User)\nWITH u.city AS city, collect(u.id) AS ids\nRETURN city, size(ids) AS n",
        None,
        check_set_of_tuples("city", "n", {("Boston", 3), ("NYC", 2), ("SF", 1)}),
    ),
    (
        "MATCH (u:User)\nWITH count(*) AS n\nRETURN n",
        None,
        check_exact_rows([6]),
    ),
    (
        "MATCH (u:User)\nWITH u.state AS s, avg(u.age) AS a\nRETURN s, a\nORDER BY a DESC",
        None,
        check_avg_by_state_rows(),
    ),
    (
        "MATCH (u:User)-[:FOLLOWS]->(v:User)\nWITH v.city AS city, count(*) AS c\nRETURN city, c",
        None,
        check_set_of_tuples("city", "c", {("Boston", 2), ("SF", 1)}),
    ),

    # Multi-stage WITH + SKIP + broader aggregates (C033-C036 equivalents)
    (
        "MATCH (u:User)\nWITH u.city AS city, count(*) AS c\nWITH city, c WHERE c > 1\nRETURN city, c\nORDER BY c DESC\nSKIP 1\nLIMIT 1",
        None,
        check_exact_rows([{"city": "NYC", "c": 2}]),
    ),
    (
        "MATCH (u:User)\nWITH u.city AS city, collect(u.id) AS ids\nWITH city, size(ids) AS n\nRETURN city, n\nORDER BY n DESC\nSKIP 0\nLIMIT 2",
        None,
        check_exact_rows([{"city": "Boston", "n": 3}, {"city": "NYC", "n": 2}]),
    ),
    (
        "MATCH (u:User)\nWITH u.state AS s, sum(u.age) AS total\nRETURN s, total\nORDER BY total DESC\nLIMIT 2",
        None,
        check_exact_rows([{"s": "MA", "total": 93}, {"s": "NY", "total": 43}]),
    ),
    (
        "MATCH (u:User)\nWITH min(u.age) AS mn, max(u.age) AS mx\nRETURN mn, mx",
        None,
        check_exact_rows([{"mn": 20, "mx": 40}]),
    ),

    # WITH then MATCH tail (C037-C039 equivalents)
    (
        "MATCH (u:User)\nWITH u\nMATCH (u)-[:FOLLOWS]->(v:User)\nRETURN u.id, v.id\nORDER BY u.id, v.id",
        None,
        check_exact_rows([{"id": "u1", "v_id": "u2"}, {"id": "u1", "v_id": "u3"}, {"id": "u2", "v_id": "u3"}]),
    ),
    (
        "MATCH (u:User)\nWITH u WHERE u.active = true\nMATCH (u)-[:FOLLOWS]->(v:User)\nRETURN DISTINCT v.city\nORDER BY v.city\nSKIP 1\nLIMIT 1",
        None,
        check_exact_rows(["SF"]),
    ),
    (
        "MATCH (u:User)\nWITH u.name AS name, u\nMATCH (u)-[:FOLLOWS]->(v:User)\nRETURN name, v.name\nORDER BY name, v.name\nLIMIT 5",
        None,
        check_exact_rows([{"name": "Alice", "v_name": "Bob"}, {"name": "Alice", "v_name": "Cara"}, {"name": "Bob", "v_name": "Cara"}]),
    ),
    (
        "MATCH (u:User)-[:FOLLOWS]->(v:User)\nWITH u, count(v) AS degree\nMATCH (u)-[:FOLLOWS]->(w:User)\nRETURN u.id, degree, w.id\nORDER BY u.id, w.id",
        None,
        check_exact_rows(
            [
                {"id": "u1", "degree": 2, "w_id": "u2"},
                {"id": "u1", "degree": 2, "w_id": "u3"},
                {"id": "u2", "degree": 1, "w_id": "u3"},
            ]
        ),
    ),
    (
        "MATCH (u:User)\nWITH DISTINCT u.city AS city\nMATCH (x:User)\nWHERE x.city = city\nRETURN city, x.id\nORDER BY city, x.id",
        None,
        check_exact_rows(
            [
                {"city": "Boston", "id": "u1"},
                {"city": "Boston", "id": "u3"},
                {"city": "Boston", "id": "u6"},
                {"city": "NYC", "id": "u4"},
                {"city": "NYC", "id": "u5"},
                {"city": "SF", "id": "u2"},
            ]
        ),
    ),
    (
        "MATCH (u:User)\nWITH DISTINCT u.city AS city\nMATCH (x:User)-[:FOLLOWS]->(y:User)\nWHERE x.city = city\nRETURN city, y.id\nORDER BY city, y.id",
        None,
        check_exact_rows(
            [
                {"city": "Boston", "id": "u2"},
                {"city": "Boston", "id": "u3"},
                {"city": "SF", "id": "u3"},
            ]
        ),
    ),
    (
        "MATCH (u:User)\nWITH u\nMATCH (u)-[r:FOLLOWS]->(v:User)\nRETURN type(r), u.id, v.id\nORDER BY u.id, v.id",
        None,
        check_exact_rows(
            [
                {"type": "FOLLOWS", "id": "u1", "v_id": "u2"},
                {"type": "FOLLOWS", "id": "u1", "v_id": "u3"},
                {"type": "FOLLOWS", "id": "u2", "v_id": "u3"},
            ]
        ),
    ),
    (
        "MATCH (u:User)\nWITH u\nMATCH (u)-[:FOLLOWS]->(v:User)-[:FOLLOWS]->(w:User)\nRETURN u.id, w.id\nORDER BY u.id, w.id",
        None,
        check_exact_rows([{"id": "u1", "w_id": "u3"}]),
    ),
    (
        "MATCH (u:User), (v:User)\nRETURN u.id, v.id\nORDER BY u.id, v.id\nLIMIT 3",
        None,
        check_exact_rows(
            [
                {"id": "u1", "v_id": "u1"},
                {"id": "u1", "v_id": "u2"},
                {"id": "u1", "v_id": "u3"},
            ]
        ),
    ),
    (
        "MATCH (u:User), (u)-[:FOLLOWS]->(v:User)\nRETURN u.id, v.id\nORDER BY u.id, v.id\nLIMIT 5",
        None,
        check_exact_rows([{"id": "u1", "v_id": "u2"}, {"id": "u1", "v_id": "u3"}, {"id": "u2", "v_id": "u3"}]),
    ),
    (
        "MATCH (u:User)-[:FOLLOWS]->(v:User), (v)-[:FOLLOWS]->(w:User)\nRETURN u.id, w.id\nORDER BY u.id, w.id",
        None,
        check_exact_rows([{"id": "u1", "w_id": "u3"}]),
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize("mode,db_name,mapping_fixture", MODES)
@pytest.mark.parametrize("cypher,params,check", QUERY_CASES)
def test_execute_translated_queries_matrix(
    mode: str,
    db_name: str,
    mapping_fixture: str,
    cypher: str,
    params: dict[str, Any] | None,
    check: ScenarioCheck,
):
    rows = _run(mode=mode, db_name=db_name, mapping_fixture=mapping_fixture, cypher=cypher, params=params)
    check(rows)
