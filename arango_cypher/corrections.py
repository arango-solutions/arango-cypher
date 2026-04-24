"""Local corrections store for Cypher→AQL transpiler learning.

Stores user-corrected AQL alongside the original Cypher query and mapping
fingerprint. When a matching correction exists for a given Cypher + mapping
pair, the corrected AQL is returned instead of the transpiled output.

Storage: SQLite file (default: ``corrections.db`` in the working directory).
All data stays local — nothing is sent externally.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from arango_query_core import mapping_hash as _canonical_mapping_hash

_DB_PATH = os.getenv("CORRECTIONS_DB", "corrections.db")
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS aql_corrections (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                cypher       TEXT    NOT NULL,
                mapping_hash TEXT    NOT NULL,
                database     TEXT    NOT NULL DEFAULT '',
                original_aql TEXT    NOT NULL,
                corrected_aql TEXT   NOT NULL,
                bind_vars    TEXT    NOT NULL DEFAULT '{}',
                created_at   TEXT    NOT NULL,
                note         TEXT    NOT NULL DEFAULT ''
            )
        """)
        _conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_corrections_lookup
            ON aql_corrections (cypher, mapping_hash)
        """)
        _conn.commit()
    return _conn


def _mapping_hash(mapping: dict[str, Any] | Any) -> str:
    """Module-private alias for :func:`arango_query_core.mapping_hash`.

    Kept private (leading underscore) so external callers reach for the
    canonical ``arango_query_core.mapping_hash`` instead. The alias
    exists because :mod:`tests.test_service_hardening` asserts on this
    module attribute directly (pin for the ``corrections``/``nl_corrections``
    symmetry introduced in the 2026-04-26 hardening PR) and moving the
    assertion would be an unnecessary churn.
    """
    return _canonical_mapping_hash(mapping)


@dataclass
class Correction:
    id: int
    cypher: str
    mapping_hash: str
    database: str
    original_aql: str
    corrected_aql: str
    bind_vars: dict[str, Any]
    created_at: str
    note: str


def lookup(cypher: str, mapping: dict[str, Any] | Any) -> Correction | None:
    """Find the most recent correction for an exact Cypher + mapping match."""
    mh = _mapping_hash(mapping)
    normalized = cypher.strip()
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            """SELECT id, cypher, mapping_hash, database, original_aql,
                      corrected_aql, bind_vars, created_at, note
               FROM aql_corrections
               WHERE cypher = ? AND mapping_hash = ?
               ORDER BY id DESC LIMIT 1""",
            (normalized, mh),
        ).fetchone()
    if row is None:
        return None
    return Correction(
        id=row[0],
        cypher=row[1],
        mapping_hash=row[2],
        database=row[3],
        original_aql=row[4],
        corrected_aql=row[5],
        bind_vars=json.loads(row[6]),
        created_at=row[7],
        note=row[8],
    )


def save(
    *,
    cypher: str,
    mapping: dict[str, Any] | Any,
    database: str = "",
    original_aql: str,
    corrected_aql: str,
    bind_vars: dict[str, Any] | None = None,
    note: str = "",
) -> int:
    """Save a correction. Returns the row id."""
    mh = _mapping_hash(mapping)
    normalized = cypher.strip()
    now = datetime.now(UTC).isoformat()
    bv = json.dumps(bind_vars or {}, default=str)
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO aql_corrections
               (cypher, mapping_hash, database, original_aql, corrected_aql,
                bind_vars, created_at, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (normalized, mh, database, original_aql, corrected_aql, bv, now, note),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def list_all(limit: int = 100) -> list[Correction]:
    """List corrections, most recent first."""
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT id, cypher, mapping_hash, database, original_aql,
                      corrected_aql, bind_vars, created_at, note
               FROM aql_corrections
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        Correction(
            id=r[0],
            cypher=r[1],
            mapping_hash=r[2],
            database=r[3],
            original_aql=r[4],
            corrected_aql=r[5],
            bind_vars=json.loads(r[6]),
            created_at=r[7],
            note=r[8],
        )
        for r in rows
    ]


def delete(correction_id: int) -> bool:
    """Delete a correction by id. Returns True if it existed."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM aql_corrections WHERE id = ?", (correction_id,))
        conn.commit()
        return cur.rowcount > 0


def delete_all() -> int:
    """Delete all corrections. Returns count deleted."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM aql_corrections")
        conn.commit()
        return cur.rowcount
