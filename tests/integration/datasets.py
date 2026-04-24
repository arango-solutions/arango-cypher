from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from tests.integration.seed import _ensure_doc_collection, _ensure_edge_collection, _reset_collection

logger = logging.getLogger(__name__)


def _ensure_persistent_index(
    col: Any, fields: list[str], *, name: str | None = None, unique: bool = False
) -> None:
    """Create a persistent index if one covering the same fields does not exist."""
    existing = col.indexes()
    for idx in existing:
        if idx.get("type") == "persistent" and idx.get("fields") == fields:
            return
    kwargs: dict[str, Any] = {"fields": fields, "unique": unique}
    if name:
        kwargs["name"] = name
    col.add_persistent_index(**kwargs)


def seed_movies_lpg_dataset(db: Any, *, with_vci: bool = True) -> None:
    """Seed the full Neo4j Movies dataset (LPG format) into ArangoDB.

    Args:
        with_vci: If True, create VCI indexes on the edge collection's
            ``relation`` field and persistent indexes on the nodes collection's
            ``type``, ``name``, and ``title`` fields.  Set to False to simulate
            a "naked" LPG graph without performance indexes.
    """
    root = Path(__file__).resolve().parents[2]
    p = root / "tests" / "fixtures" / "datasets" / "movies" / "lpg-data.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError(f"Invalid movies dataset fixture: {p}")

    nodes_col = _ensure_doc_collection(db, "nodes")
    edges_col = _ensure_edge_collection(db, "edges")
    _reset_collection(nodes_col)
    _reset_collection(edges_col)

    if nodes:
        nodes_col.insert_many(nodes)
    if edges:
        edges_col.insert_many(edges)

    if with_vci:
        _ensure_persistent_index(nodes_col, ["type"], name="idx_nodes_type")
        _ensure_persistent_index(nodes_col, ["name"], name="idx_nodes_name")
        _ensure_persistent_index(nodes_col, ["title"], name="idx_nodes_title")
        _ensure_persistent_index(edges_col, ["relation"], name="idx_edges_relation")
    else:
        logger.warning(
            "Seeded movies LPG dataset without VCI indexes. "
            "Traversal performance will be degraded. "
            "Consider creating a persistent index on edges.relation for vertex-centric indexing."
        )


def seed_movies_pg_dataset(db: Any) -> None:
    """Seed the full Neo4j Movies dataset in PG format (separate collections)."""
    root = Path(__file__).resolve().parents[2]
    p = root / "tests" / "fixtures" / "datasets" / "movies" / "pg-data.json"
    data = json.loads(p.read_text(encoding="utf-8"))

    for coll_name, docs in data.get("collections", {}).items():
        col = _ensure_doc_collection(db, coll_name)
        _reset_collection(col)
        if docs:
            col.insert_many(docs)

    for coll_name, docs in data.get("edge_collections", {}).items():
        col = _ensure_edge_collection(db, coll_name)
        _reset_collection(col)
        if docs:
            col.insert_many(docs)

    _ensure_persistent_index(db.collection("persons"), ["name"], name="idx_persons_name")
    _ensure_persistent_index(db.collection("movies"), ["title"], name="idx_movies_title")


def seed_northwind_dataset(db: Any) -> None:
    """Seed the Northwind e-commerce dataset (PG format) into ArangoDB."""
    root = Path(__file__).resolve().parents[2]
    p = root / "tests" / "fixtures" / "datasets" / "northwind" / "pg-data.json"
    data = json.loads(p.read_text(encoding="utf-8"))

    for coll_name, docs in data.get("collections", {}).items():
        col = _ensure_doc_collection(db, coll_name)
        _reset_collection(col)
        if docs:
            col.insert_many(docs)

    for coll_name, docs in data.get("edge_collections", {}).items():
        col = _ensure_edge_collection(db, coll_name)
        _reset_collection(col)
        if docs:
            col.insert_many(docs)

    _ensure_persistent_index(db.collection("customers"), ["companyName"], name="idx_customers_name")
    _ensure_persistent_index(db.collection("products"), ["productName"], name="idx_products_name")
    _ensure_persistent_index(db.collection("orders"), ["orderDate"], name="idx_orders_date")
    _ensure_persistent_index(db.collection("categories"), ["categoryName"], name="idx_categories_name")
