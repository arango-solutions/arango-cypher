from __future__ import annotations

import os
import uuid

import pytest

try:
    from arango import ArangoClient
except ImportError:
    ArangoClient = None  # type: ignore[misc, assignment]

from arango_cypher import translate
from arango_query_core import MappingBundle, MappingSource


def _make_mapping(collection: str, edge_collection: str) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={
            "entities": [{"name": "Person", "labels": ["Person"], "properties": []}],
            "relationships": [
                {"type": "KNOWS", "fromEntity": "Person", "toEntity": "Person", "properties": []},
            ],
        },
        physical_mapping={
            "entities": {
                "Person": {"collectionName": collection, "style": "COLLECTION"},
            },
            "relationships": {
                "KNOWS": {"edgeCollectionName": edge_collection, "style": "DEDICATED_COLLECTION"},
            },
        },
        metadata={"confidence": 1.0},
        source=MappingSource(kind="explicit"),
    )


@pytest.mark.integration
def test_create_node_and_read_back():
    if ArangoClient is None:
        pytest.skip("python-arango not installed")

    url = os.environ.get("ARANGO_URL", "http://localhost:8529")
    user = os.environ.get("ARANGO_USER", "root")
    pw = os.environ.get("ARANGO_PASS", "openSesame")
    db_name = os.environ.get("ARANGO_DB", "_system")

    client = ArangoClient(hosts=url)
    db = client.db(db_name, username=user, password=pw)

    suffix = uuid.uuid4().hex[:8]
    coll_name = f"test_persons_{suffix}"
    edge_coll_name = f"test_knows_{suffix}"

    try:
        db.create_collection(coll_name)
        db.create_collection(edge_coll_name, edge=True)

        mapping = _make_mapping(coll_name, edge_coll_name)

        out = translate(
            'CREATE (n:Person {name: "Alice", age: 30})',
            mapping=mapping,
        )
        db.aql.execute(out.aql, bind_vars=out.bind_vars)

        cursor = db.aql.execute(
            "FOR d IN @@c RETURN d",
            bind_vars={"@c": coll_name},
        )
        docs = list(cursor)
        assert len(docs) == 1
        assert docs[0]["name"] == "Alice"
        assert docs[0]["age"] == 30

        out2 = translate(
            'MATCH (a:Person {name: "Alice"}) CREATE (a)-[:KNOWS]->(b:Person {name: "Bob"})',
            mapping=mapping,
        )
        db.aql.execute(out2.aql, bind_vars=out2.bind_vars)

        cursor = db.aql.execute(
            "FOR d IN @@c RETURN d",
            bind_vars={"@c": coll_name},
        )
        all_persons = list(cursor)
        assert len(all_persons) == 2
        names = {d["name"] for d in all_persons}
        assert names == {"Alice", "Bob"}

        cursor = db.aql.execute(
            "FOR e IN @@c RETURN e",
            bind_vars={"@c": edge_coll_name},
        )
        edges = list(cursor)
        assert len(edges) == 1
        assert edges[0]["_from"].startswith(f"{coll_name}/")
        assert edges[0]["_to"].startswith(f"{coll_name}/")

    finally:
        if db.has_collection(coll_name):
            db.delete_collection(coll_name)
        if db.has_collection(edge_coll_name):
            db.delete_collection(edge_coll_name)
