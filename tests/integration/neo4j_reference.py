"""Helpers for running queries against a reference Neo4j engine.

Used only by ``tests/integration/test_movies_crossvalidate.py`` (marker ``cross``).
The ``neo4j`` driver is imported lazily so the rest of the test suite does
not require it to be installed.

Connection defaults match ``docker-compose.neo4j.yml``:
  bolt://127.0.0.1:27687  neo4j/openSesame

Environment overrides: ``NEO4J_URI``, ``NEO4J_USER``, ``NEO4J_PASS``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_BOLT_URI = "bolt://127.0.0.1:27687"
DEFAULT_USER = "neo4j"
DEFAULT_PASSWORD = "openSesame"


# Tracks which dataset is currently loaded into the shared Neo4j instance.
# Cross-validation test modules call ``ensure_dataset(driver, name, seed_fn)``
# instead of seeding directly, so a pytest session that runs more than one
# corpus reseeds whenever the active dataset changes.
_active_dataset: str | None = None


def ensure_dataset(driver: Any, name: str, seed_fn: Any) -> None:
    """Re-seed the shared Neo4j instance if a different dataset is active.

    ``seed_fn`` must accept ``(driver,)`` and is responsible for wiping +
    loading its data.  Called by each cross-validation test module's
    fixture so multiple datasets can coexist in one pytest session
    without trampling each other on Neo4j Community (single writable DB).
    """
    global _active_dataset  # noqa: PLW0603
    if _active_dataset == name:
        return
    seed_fn(driver)
    _active_dataset = name


def connection_params() -> tuple[str, str, str]:
    """Return ``(uri, user, password)`` from env with compose defaults."""
    uri = os.environ.get("NEO4J_URI") or DEFAULT_BOLT_URI
    user = os.environ.get("NEO4J_USER") or DEFAULT_USER
    password = os.environ.get("NEO4J_PASS") or DEFAULT_PASSWORD
    return uri, user, password


def get_driver():  # pragma: no cover - exercised only under `cross` marker
    """Return an open Neo4j driver, importing the driver lazily.

    Raises ``RuntimeError`` if the driver is not installed or the server
    is unreachable.
    """
    try:
        from neo4j import GraphDatabase  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "neo4j driver not installed; `pip install 'neo4j>=5'` "
            "or install the `neo4j` extra."
        ) from e

    uri, user, password = connection_params()
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
    except Exception as e:
        driver.close()
        raise RuntimeError(
            f"Neo4j not reachable at {uri}. Start it with "
            f"`docker compose -f docker-compose.neo4j.yml -p arango_cypher_neo4j up -d`."
        ) from e
    return driver


# ---------------------------------------------------------------------------
# Movies dataset seeding
# ---------------------------------------------------------------------------


_STRIP_NODE_KEYS = {"_key", "type", "labels"}
_STRIP_EDGE_KEYS = {"_from", "_to", "relation", "_key"}

# Neo4j relationship type names must be unquoted identifiers in the MERGE
# clause.  The movies dataset uses SCREAMING_SNAKE_CASE values already
# (ACTED_IN, DIRECTED, PRODUCED, WROTE, REVIEWED, FOLLOWS), so we just
# validate and pass through.
_VALID_REL_TYPE = __import__("re").compile(r"^[A-Z][A-Z0-9_]*$")


def _strip_id(doc_id: str) -> str:
    """Turn an ArangoDB handle ``"nodes/TheMatrix"`` into ``"TheMatrix"``."""
    return doc_id.split("/", 1)[1] if "/" in doc_id else doc_id


def _node_props(node: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in node.items() if k not in _STRIP_NODE_KEYS}


def _edge_props(edge: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in edge.items() if k not in _STRIP_EDGE_KEYS}


def _nodes_by_label(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket nodes by their primary label for batched UNWIND inserts."""
    out: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        labels = n.get("labels") or ([n["type"]] if n.get("type") else [])
        if not labels:
            continue
        label = labels[0]
        out.setdefault(label, []).append({"_key": n["_key"], "props": _node_props(n)})
    return out


def _edges_by_type(edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for e in edges:
        rtype = e.get("relation") or e.get("type")
        if not rtype or not _VALID_REL_TYPE.match(rtype):
            continue
        out.setdefault(rtype, []).append({
            "from": _strip_id(e["_from"]),
            "to": _strip_id(e["_to"]),
            "props": _edge_props(e),
        })
    return out


def seed_neo4j_movies(driver: Any) -> None:
    """Load the same movies dataset used by the Arango integration tests.

    Idempotent: wipes existing nodes/relationships first, then bulk-loads
    via UNWIND batches.
    """
    root = Path(__file__).resolve().parents[2]
    p = root / "tests" / "fixtures" / "datasets" / "movies" / "lpg-data.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError(f"Invalid movies dataset fixture: {p}")

    nodes_by_label = _nodes_by_label(nodes)
    edges_by_type = _edges_by_type(edges)

    with driver.session() as sess:
        sess.run("MATCH (n) DETACH DELETE n").consume()

        # Unique _key per label so MERGE-by-_key is O(1).
        for label in nodes_by_label:
            sess.run(
                f"CREATE CONSTRAINT `uniq_{label}_key` IF NOT EXISTS "
                f"FOR (n:`{label}`) REQUIRE n._key IS UNIQUE"
            ).consume()

        for label, batch in nodes_by_label.items():
            sess.run(
                f"""
                UNWIND $rows AS row
                MERGE (n:`{label}` {{_key: row._key}})
                SET n += row.props
                """,
                rows=batch,
            ).consume()

        for rtype, batch in edges_by_type.items():
            sess.run(
                f"""
                UNWIND $rows AS row
                MATCH (a {{_key: row.from}})
                MATCH (b {{_key: row.to}})
                MERGE (a)-[r:`{rtype}`]->(b)
                SET r += row.props
                """,
                rows=batch,
            ).consume()

    logger.info(
        "Seeded Neo4j movies dataset: %d nodes across %d labels, %d edges across %d types",
        sum(len(v) for v in nodes_by_label.values()),
        len(nodes_by_label),
        sum(len(v) for v in edges_by_type.values()),
        len(edges_by_type),
    )


# ---------------------------------------------------------------------------
# Generic PG-style dataset seeding (one collection per label, one edge
# collection per relationship type). Used by cross-validation tests for any
# property-graph dataset whose physical layout matches the conceptual schema.
# ---------------------------------------------------------------------------


def _label_from_collection(coll: str, override: dict[str, str] | None = None) -> str:
    """Translate a Mongo-style collection name (e.g. ``customers``) to a
    Cypher label (e.g. ``Customer``).  ``override`` lets callers supply
    an explicit map taken from the mapping fixture.
    """
    if override and coll in override:
        return override[coll]
    base = coll.rstrip("s") if coll.endswith("s") else coll
    return base[:1].upper() + base[1:]


def _reltype_from_collection(coll: str, override: dict[str, str] | None = None) -> str:
    """Translate an edge-collection name (e.g. ``supplied_by``) to a Cypher
    relationship type (e.g. ``SUPPLIED_BY``)."""
    if override and coll in override:
        return override[coll]
    return coll.upper()


def seed_neo4j_pg(
    driver: Any,
    pg_data: dict[str, Any],
    *,
    label_map: dict[str, str] | None = None,
    reltype_map: dict[str, str] | None = None,
) -> None:
    """Load a PG-style fixture (``collections`` + ``edge_collections``) into Neo4j.

    Args:
        pg_data:    the parsed JSON body of an ``…/pg-data.json`` fixture.
        label_map:  optional ``{collection_name: Label}`` overrides.
        reltype_map: optional ``{edge_collection_name: REL_TYPE}`` overrides.

    Idempotent: every node label is wiped then re-loaded; relationships are
    rebuilt fresh.  Stores the original ``_key`` as a property so queries
    that filter on ``_key`` (a few of the Northwind cases) still work in
    Cypher, where there is no built-in document handle.
    """
    collections = pg_data.get("collections") or {}
    edge_collections = pg_data.get("edge_collections") or {}

    coll_to_label = {c: _label_from_collection(c, label_map) for c in collections}

    with driver.session() as sess:
        sess.run("MATCH (n) DETACH DELETE n").consume()

        for coll, label in coll_to_label.items():
            sess.run(
                f"CREATE CONSTRAINT `uniq_{label}_key` IF NOT EXISTS "
                f"FOR (n:`{label}`) REQUIRE n._key IS UNIQUE"
            ).consume()

        for coll, docs in collections.items():
            label = coll_to_label[coll]
            rows = [{"_key": d["_key"], "props": {k: v for k, v in d.items() if k != "_key"}} for d in docs]
            if not rows:
                continue
            sess.run(
                f"""
                UNWIND $rows AS row
                MERGE (n:`{label}` {{_key: row._key}})
                SET n += row.props, n._key = row._key
                """,
                rows=rows,
            ).consume()

        total_edges = 0
        for ecoll, edges in edge_collections.items():
            rtype = _reltype_from_collection(ecoll, reltype_map)
            if not _VALID_REL_TYPE.match(rtype):
                logger.warning("skipping edge collection %s (invalid rel type)", ecoll)
                continue
            rows = []
            for e in edges:
                from_coll, from_key = e["_from"].split("/", 1)
                to_coll, to_key = e["_to"].split("/", 1)
                rows.append({
                    "from_label": coll_to_label.get(from_coll, _label_from_collection(from_coll)),
                    "from_key": from_key,
                    "to_label": coll_to_label.get(to_coll, _label_from_collection(to_coll)),
                    "to_key": to_key,
                    "props": {k: v for k, v in e.items() if k not in {"_from", "_to", "_key"}},
                })
            for row in rows:
                sess.run(
                    f"""
                    MATCH (a:`{row['from_label']}` {{_key: $from_key}})
                    MATCH (b:`{row['to_label']}` {{_key: $to_key}})
                    MERGE (a)-[r:`{rtype}`]->(b)
                    SET r += $props
                    """,
                    from_key=row["from_key"],
                    to_key=row["to_key"],
                    props=row["props"],
                ).consume()
            total_edges += len(rows)

    logger.info(
        "Seeded Neo4j PG dataset: %d node collections, %d edges across %d rel types",
        len(collections),
        total_edges,
        len(edge_collections),
    )


def seed_neo4j_northwind(driver: Any) -> None:
    """Seed the Northwind PG fixture into Neo4j using the export-mapping conventions."""
    root = Path(__file__).resolve().parents[2]
    pg_path = root / "tests" / "fixtures" / "datasets" / "northwind" / "pg-data.json"
    mapping_path = root / "tests" / "fixtures" / "mappings" / "northwind_pg.export.json"

    pg_data = json.loads(pg_path.read_text(encoding="utf-8"))
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))

    phys = mapping.get("physicalMapping") or {}
    label_map: dict[str, str] = {}
    for entity_name, info in (phys.get("entities") or {}).items():
        coll = info.get("collectionName")
        if coll:
            label_map[coll] = entity_name

    reltype_map: dict[str, str] = {}
    for rel_name, info in (phys.get("relationships") or {}).items():
        ecoll = info.get("edgeCollectionName")
        if ecoll:
            reltype_map[ecoll] = rel_name

    seed_neo4j_pg(driver, pg_data, label_map=label_map, reltype_map=reltype_map)


# ---------------------------------------------------------------------------
# Running Cypher
# ---------------------------------------------------------------------------


def run_cypher(driver: Any, cypher: str) -> list[dict[str, Any]]:
    """Execute ``cypher`` against Neo4j and return rows as plain dicts.

    ``neo4j.Record`` values are coerced to primitive Python types so the
    result is directly comparable with AQL rows.
    """
    with driver.session() as sess:
        result = sess.run(cypher)
        return [_record_to_dict(r) for r in result]


def _record_to_dict(record: Any) -> dict[str, Any]:
    return {k: _coerce(record[k]) for k in record.keys()}


def _coerce(value: Any) -> Any:
    """Convert Neo4j driver types (Node/Relationship/Path) to plain dicts.

    Scalars pass through unchanged. This intentionally does NOT add an
    ``_id`` / ``_key`` round-trip, because our AQL returns don't include
    that either unless the query asked for it.
    """
    # Lazy-import to avoid hard dep at module import time.
    try:
        from neo4j.graph import Node, Path, Relationship  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        return value

    if isinstance(value, Node):
        return {"_labels": sorted(value.labels), **dict(value)}
    if isinstance(value, Relationship):
        return {"_type": value.type, **dict(value)}
    if isinstance(value, Path):
        return {
            "nodes": [_coerce(n) for n in value.nodes],
            "relationships": [_coerce(r) for r in value.relationships],
        }
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    return value
