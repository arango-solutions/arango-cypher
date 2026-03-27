from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArangoConn:
    url: str
    username: str
    password: str


def _ensure_db(sys_db: Any, *, name: str) -> None:
    if not sys_db.has_database(name):
        sys_db.create_database(name)


def _ensure_doc_collection(db: Any, name: str) -> Any:
    if not db.has_collection(name):
        return db.create_collection(name)
    col = db.collection(name)
    if col.properties().get("type") == 3:
        raise ValueError(f"Expected document collection but found edge collection: {name}")
    return col


def _ensure_edge_collection(db: Any, name: str) -> Any:
    if not db.has_collection(name):
        return db.create_collection(name, edge=True)
    col = db.collection(name)
    if col.properties().get("type") != 3:
        raise ValueError(f"Expected edge collection but found document collection: {name}")
    return col


def _reset_collection(col: Any) -> None:
    # Prefer truncate for speed and determinism.
    col.truncate()


def seed_social_dataset(db: Any, *, mode: str) -> None:
    """
    Seed a tiny deterministic dataset to support v0 corpus cases.

    Modes:
    - pg: users + follows (dedicated edge collection)
    - lpg: vertices + edges (generic, with type discriminator)
    - hybrid: users + edges (generic, with type discriminator)
    """
    m = mode.strip().lower()
    if m not in {"pg", "lpg", "hybrid"}:
        raise ValueError(f"Unsupported seed mode: {mode}")

    if m == "pg":
        users = _ensure_doc_collection(db, "users")
        follows = _ensure_edge_collection(db, "follows")
        _reset_collection(users)
        _reset_collection(follows)

        users.insert_many(
            [
                {
                    "_key": "u1",
                    "id": "u1",
                    "name": "Alice",
                    "email": "alice@example.com",
                    "city": "Boston",
                    "state": "MA",
                    "age": 30,
                    "active": True,
                    "address": {"state": "MA", "zip": 1234567, "city": "Boston"},
                },
                {
                    "_key": "u2",
                    "id": "u2",
                    "name": "Bob",
                    "email": None,
                    "city": "SF",
                    "state": "CA",
                    "age": 20,
                    "active": True,
                    "address": {"state": "CA", "zip": 90001, "city": "SF"},
                },
                {
                    "_key": "u3",
                    "id": "u3",
                    "name": "Cara",
                    "email": "cara@example.com",
                    "city": "Boston",
                    "state": "MA",
                    "age": 40,
                    "active": False,
                    "address": {"state": "MA", "zip": 10001, "city": "Boston"},
                },
                {"_key": "u4", "id": "u4", "name": "Dan", "city": "NYC", "state": "NY", "age": 21, "active": True},
                {"_key": "u5", "id": "u5", "name": "Eve", "city": "NYC", "state": "NY", "age": 22, "active": True},
                {"_key": "u6", "id": "u6", "name": "Finn", "city": "Boston", "state": "MA", "age": 23, "active": True},
            ]
        )

        follows.insert_many(
            [
                {"_key": "e1", "_from": "users/u1", "_to": "users/u2"},
                {"_key": "e2", "_from": "users/u1", "_to": "users/u3"},
                {"_key": "e3", "_from": "users/u2", "_to": "users/u3"},
            ]
        )
        return

    if m == "lpg":
        vertices = _ensure_doc_collection(db, "vertices")
        edges = _ensure_edge_collection(db, "edges")
        _reset_collection(vertices)
        _reset_collection(edges)

        vertices.insert_many(
            [
                {"_key": "u1", "type": "User", "id": "u1", "name": "Alice", "city": "Boston", "state": "MA", "age": 30, "active": True},
                {"_key": "u2", "type": "User", "id": "u2", "name": "Bob", "city": "SF", "state": "CA", "age": 20, "active": True},
                {"_key": "u3", "type": "User", "id": "u3", "name": "Cara", "city": "Boston", "state": "MA", "age": 40, "active": False},
                {"_key": "u4", "type": "User", "id": "u4", "name": "Dan", "city": "NYC", "state": "NY", "age": 21, "active": True},
                {"_key": "u5", "type": "User", "id": "u5", "name": "Eve", "city": "NYC", "state": "NY", "age": 22, "active": True},
                {"_key": "u6", "type": "User", "id": "u6", "name": "Finn", "city": "Boston", "state": "MA", "age": 23, "active": True},
                {"_key": "d1", "type": "Doc", "id": "d1", "body": "graph databases are fun"},
                {"_key": "n1", "type": "Note", "id": "n1", "body": "hello world"},
            ]
        )

        edges.insert_many(
            [
                {"_key": "e1", "_from": "vertices/u1", "_to": "vertices/u2", "type": "FOLLOWS"},
                {"_key": "e2", "_from": "vertices/u1", "_to": "vertices/u3", "type": "FOLLOWS"},
                {"_key": "e3", "_from": "vertices/u2", "_to": "vertices/u3", "type": "FOLLOWS"},
                {"_key": "e4", "_from": "vertices/u1", "_to": "vertices/d1", "type": "LIKES"},
            ]
        )
        return

    # hybrid
    users = _ensure_doc_collection(db, "users")
    edges = _ensure_edge_collection(db, "edges")
    _reset_collection(users)
    _reset_collection(edges)

    users.insert_many(
        [
            {"_key": "u1", "id": "u1", "name": "Alice", "city": "Boston", "state": "MA", "age": 30, "active": True, "address": {"state": "MA"}},
            {"_key": "u2", "id": "u2", "name": "Bob", "city": "SF", "state": "CA", "age": 20, "active": True, "address": {"state": "CA"}},
            {"_key": "u3", "id": "u3", "name": "Cara", "city": "Boston", "state": "MA", "age": 40, "active": False, "address": {"state": "MA"}},
            {"_key": "u4", "id": "u4", "name": "Dan", "city": "NYC", "state": "NY", "age": 21, "active": True, "address": {"state": "NY"}},
            {"_key": "u5", "id": "u5", "name": "Eve", "city": "NYC", "state": "NY", "age": 22, "active": True, "address": {"state": "NY"}},
            {"_key": "u6", "id": "u6", "name": "Finn", "city": "Boston", "state": "MA", "age": 23, "active": True, "address": {"state": "MA"}},
        ]
    )

    edges.insert_many(
        [
            {"_key": "e1", "_from": "users/u1", "_to": "users/u2", "type": "FOLLOWS"},
            {"_key": "e2", "_from": "users/u1", "_to": "users/u3", "type": "FOLLOWS"},
            {"_key": "e3", "_from": "users/u2", "_to": "users/u3", "type": "FOLLOWS"},
        ]
    )

