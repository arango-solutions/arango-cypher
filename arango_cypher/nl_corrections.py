"""Local corrections store for the NL → Cypher pipeline.

Stores user-approved ``(question, cypher)`` pairs alongside an optional
mapping fingerprint and note. Each saved pair is fed back into the
:class:`FewShotIndex` so the BM25 retriever can surface it as a
few-shot example on future similar questions.

This is the NL-layer counterpart to :mod:`arango_cypher.corrections`,
which sits at the Cypher → AQL layer. The two stores are intentionally
separate — they operate on different artifacts, have different lookup
keys, and have different invalidation triggers. Collocating them in a
single table would cross concerns without shared behavior.

Storage: SQLite file (default: ``nl_corrections.db`` next to
``corrections.db``). All data stays local — nothing is sent externally.

Cache-invalidation contract
---------------------------
Every mutating function (``save``, ``delete``, ``delete_all``) fires
:func:`_notify_cache_invalidation` at end of the write lock. The
nl2cypher core registers a listener via
:func:`register_invalidation_listener` so the process-wide
``FewShotIndex`` is rebuilt lazily on the next ``nl_to_cypher`` call.
Listeners that raise are logged and ignored so a misbehaving subscriber
never blocks a write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = os.getenv("NL_CORRECTIONS_DB", "nl_corrections.db")
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_invalidation_listeners: list[Callable[[], None]] = []


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nl_corrections (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                question     TEXT    NOT NULL,
                cypher       TEXT    NOT NULL,
                mapping_hash TEXT    NOT NULL DEFAULT '',
                database     TEXT    NOT NULL DEFAULT '',
                created_at   TEXT    NOT NULL,
                note         TEXT    NOT NULL DEFAULT ''
            )
            """
        )
        _conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_nl_corrections_question
            ON nl_corrections (question)
            """
        )
        _conn.commit()
    return _conn


def _mapping_hash(mapping: dict[str, Any] | Any) -> str:
    """Deterministic hash of the mapping for fingerprinting.

    Mirrors :func:`arango_cypher.corrections._mapping_hash` so the two
    stores share a single canonical fingerprint — a correction on the
    same mapping is keyed identically regardless of which layer saved it
    or which key spelling (snake_case vs. camelCase) the caller used.
    """
    cs: Any
    pm: Any
    if hasattr(mapping, "conceptual_schema"):
        cs = mapping.conceptual_schema
        pm = mapping.physical_mapping
    elif isinstance(mapping, dict):
        cs = mapping.get("conceptual_schema")
        if cs is None:
            cs = mapping.get("conceptualSchema", {})
        pm = mapping.get("physical_mapping")
        if pm is None:
            pm = mapping.get("physicalMapping", {})
    else:
        cs, pm = {}, {}
    raw = {"cs": cs, "pm": pm}
    blob = json.dumps(raw, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


@dataclass
class NLCorrection:
    id: int
    question: str
    cypher: str
    mapping_hash: str
    database: str
    created_at: str
    note: str


def register_invalidation_listener(listener: Callable[[], None]) -> None:
    """Register a callback fired after every mutating operation.

    The nl2cypher core uses this to invalidate its cached FewShotIndex
    so the next translation picks up the new corpus.
    """
    if listener not in _invalidation_listeners:
        _invalidation_listeners.append(listener)


def unregister_invalidation_listener(listener: Callable[[], None]) -> None:
    """Remove a previously-registered listener; no-op if not present."""
    if listener in _invalidation_listeners:
        _invalidation_listeners.remove(listener)


def _notify_cache_invalidation() -> None:
    for listener in list(_invalidation_listeners):
        try:
            listener()
        except Exception as exc:
            logger.info("nl_corrections invalidation listener failed: %s", exc)


def save(
    *,
    question: str,
    cypher: str,
    mapping: dict[str, Any] | Any | None = None,
    database: str = "",
    note: str = "",
) -> int:
    """Save an ``(nl_question, approved_cypher)`` pair. Returns the row id.

    ``mapping`` is optional. When supplied, the fingerprint is stored so
    future work can narrow few-shot retrieval to mapping-compatible
    examples (not yet used — BM25 retrieves from the full corpus today).
    """
    q = question.strip()
    c = cypher.strip()
    if not q or not c:
        raise ValueError("question and cypher must both be non-empty")
    mh = _mapping_hash(mapping) if mapping is not None else ""
    now = datetime.now(UTC).isoformat()
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO nl_corrections
               (question, cypher, mapping_hash, database, created_at, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (q, c, mh, database, now, note),
        )
        conn.commit()
        row_id = cur.lastrowid
    _notify_cache_invalidation()
    return row_id  # type: ignore[return-value]


def list_all(limit: int = 100) -> list[NLCorrection]:
    """List corrections, most recent first."""
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT id, question, cypher, mapping_hash, database, created_at, note
               FROM nl_corrections
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        NLCorrection(
            id=r[0],
            question=r[1],
            cypher=r[2],
            mapping_hash=r[3],
            database=r[4],
            created_at=r[5],
            note=r[6],
        )
        for r in rows
    ]


def all_examples() -> list[tuple[str, str]]:
    """Return every ``(question, cypher)`` pair in insertion order.

    Called by the nl2cypher core when (re)building the default
    :class:`FewShotIndex`. The pairs are appended *after* the shipped
    corpora so, all else equal, a user's approved correction wins ties
    against a seed example with the same BM25 score.
    """
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT question, cypher
               FROM nl_corrections
               ORDER BY id ASC"""
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def delete(correction_id: int) -> bool:
    """Delete a correction by id. Returns True if it existed."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM nl_corrections WHERE id = ?", (correction_id,))
        conn.commit()
        existed = cur.rowcount > 0
    if existed:
        _notify_cache_invalidation()
    return existed


def delete_all() -> int:
    """Delete all corrections. Returns the count deleted."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM nl_corrections")
        conn.commit()
        count = cur.rowcount
    if count > 0:
        _notify_cache_invalidation()
    return count
