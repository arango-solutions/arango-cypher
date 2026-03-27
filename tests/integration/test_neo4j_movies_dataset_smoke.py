from __future__ import annotations

import os
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import translate
from arango_query_core.exec import AqlExecutor
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.integration.datasets import seed_movies_lpg_dataset


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


def _run(db_name: str, cypher: str, *, params: dict[str, Any] | None = None) -> list[Any]:
    db = _connect_db(db_name)
    seed_movies_lpg_dataset(db)
    out = translate(cypher, mapping=mapping_bundle_for("movies_lpg"), params=params)
    return list(AqlExecutor(db).execute(out.to_aql_query()))


@pytest.mark.integration
def test_movies_dataset_actors_in_movie_smoke():
    rows = _run(
        "neo4j_movies_lpg_fixture",
        cypher=(
            'MATCH (a:Person:Actor)-[:ACTED_IN]->(m:Movie {title: "Forrest Gump"}) '
            "RETURN a.name, m.title "
            "ORDER BY a.name"
        ),
    )
    assert rows == [
        {"name": "Robin Wright", "title": "Forrest Gump"},
        {"name": "Tom Hanks", "title": "Forrest Gump"},
    ]


@pytest.mark.integration
def test_movies_dataset_directors_and_movies_smoke():
    rows = _run(
        "neo4j_movies_lpg_fixture",
        cypher="MATCH (d:Person:Director)-[:DIRECTED]->(m:Movie) RETURN d.name, m.title ORDER BY d.name, m.title",
    )
    assert rows == [{"name": "Robert Zemeckis", "title": "Forrest Gump"}]


@pytest.mark.integration
def test_movies_dataset_actors_in_movie_by_role_smoke():
    rows = _run(
        "neo4j_movies_lpg_fixture",
        cypher=(
            'MATCH (a:Person:Actor)-[:ACTED_IN {role: "Forrest"}]->(m:Movie {title: "Forrest Gump"}) '
            "RETURN a.name "
            "ORDER BY a.name"
        ),
    )
    assert rows == ["Tom Hanks"]


@pytest.mark.integration
def test_movies_dataset_unlabeled_start_node_smoke():
    rows = _run(
        "neo4j_movies_lpg_fixture",
        cypher=(
            'MATCH (a)-[:ACTED_IN {role: "Forrest"}]->(m:Movie {title: "Forrest Gump"}) '
            "RETURN a.name "
            "ORDER BY a.name"
        ),
    )
    assert rows == ["Tom Hanks"]

