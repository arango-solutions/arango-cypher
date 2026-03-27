from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.integration.seed import _ensure_doc_collection, _ensure_edge_collection, _reset_collection


def seed_movies_lpg_dataset(db: Any) -> None:
    """
    Seed a tiny Neo4j movies dataset (converted LPG format) into ArangoDB.

    Source format matches `cypher2aql/datasets/movies/lpg-data.json`.
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

