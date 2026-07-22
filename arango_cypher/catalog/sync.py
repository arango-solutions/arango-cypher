"""Sidecar refresher: rebuild registered mappings and write them to the catalog.

This is the out-of-band worker. For each registered database (and each of its
optional named-graph scopes) it calls :func:`get_mapping(force_refresh=True)`,
which runs the full analyzer + statistics pass and persists the result to that
database's shared ``arango_cypher_schema_cache`` collection. The FastAPI service
then reads that cache read-only, so analysis never lands on the request path.

Run modes (see ``scripts/catalog_sync.py``):

* ``sync_once`` — one full pass over the registry, then return. Ideal for cron.
* ``sync_forever`` — pass, sleep ``interval_seconds``, repeat. Ideal for a
  long-running sidecar process / container.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .registry import CatalogRegistry, DatabaseEntry, load_registry

if TYPE_CHECKING:
    from arango.database import StandardDatabase

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    """Outcome of analyzing one (database, graph-scope) target."""

    name: str
    database: str
    graph: str | None
    ok: bool
    elapsed_seconds: float
    entities: int = 0
    relationships: int = 0
    source: str | None = None
    error: str | None = None

    def log_fields(self) -> dict[str, object]:
        return {
            "name": self.name,
            "database": self.database,
            "graph": self.graph or "",
            "ok": self.ok,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "entities": self.entities,
            "relationships": self.relationships,
            "source": self.source or "",
            "error": self.error or "",
        }


def _connect(entry: DatabaseEntry) -> StandardDatabase:
    """Open a database handle for a registry entry.

    Imported lazily so importing the catalog package does not hard-require the
    ``arango`` driver in environments that only consume the registry types.
    """
    from arango import ArangoClient

    client = ArangoClient(hosts=entry.url)
    return client.db(
        entry.database,
        username=entry.username,
        password=entry.password,
        verify=entry.verify_ssl,
    )


def _sync_target(db: StandardDatabase, entry: DatabaseEntry, graph: str | None) -> SyncResult:
    """Force-rebuild and persist one (database, graph-scope) mapping."""
    from ..schema_acquire import get_mapping

    t0 = time.perf_counter()
    try:
        bundle = get_mapping(db, force_refresh=True, graph_name=graph)
    except Exception as exc:  # noqa: BLE001 - one target's failure must not abort the pass
        elapsed = time.perf_counter() - t0
        logger.warning(
            "Catalog sync failed for %s (graph=%s) after %.1fs: %s",
            entry.name,
            graph or "-",
            elapsed,
            exc,
            exc_info=True,
        )
        return SyncResult(
            name=entry.name,
            database=entry.database,
            graph=graph,
            ok=False,
            elapsed_seconds=elapsed,
            error=f"{type(exc).__name__}: {exc}",
        )

    elapsed = time.perf_counter() - t0
    conceptual = bundle.conceptual_schema or {}
    result = SyncResult(
        name=entry.name,
        database=entry.database,
        graph=graph,
        ok=True,
        elapsed_seconds=elapsed,
        entities=len(conceptual.get("entities") or []),
        relationships=len(conceptual.get("relationships") or []),
        source=bundle.source.kind if bundle.source is not None else None,
    )
    logger.info("Catalog sync ok: %s", result.log_fields())
    return result


def sync_entry(entry: DatabaseEntry) -> list[SyncResult]:
    """Sync a single database and all of its configured named-graph scopes.

    Always syncs the whole-database (unscoped) mapping; additionally syncs each
    named-graph scope so scoped sessions are served from a warm cache too. A
    connection failure is reported once and short-circuits that entry's targets.
    """
    try:
        db = _connect(entry)
    except Exception as exc:  # noqa: BLE001 - surface as a failed result, never raise
        logger.warning("Catalog sync could not connect to %s: %s", entry.name, exc)
        return [
            SyncResult(
                name=entry.name,
                database=entry.database,
                graph=None,
                ok=False,
                elapsed_seconds=0.0,
                error=f"connect failed: {type(exc).__name__}: {exc}",
            )
        ]

    targets: list[str | None] = [None, *entry.graphs]
    return [_sync_target(db, entry, graph) for graph in targets]


def sync_once(registry: CatalogRegistry | None = None) -> list[SyncResult]:
    """Run one full pass over the registry and return every target's result."""
    # Distinguish "not supplied" (None -> load from config/env) from an
    # explicitly-empty registry (falsy via __bool__, but must NOT trigger the
    # env fallback — that would silently analyze the .env database).
    if registry is None:
        registry = load_registry()
    if not registry:
        logger.warning("Catalog registry is empty; nothing to sync")
        return []

    logger.info(
        "Catalog sync pass starting: %d database(s) from %s",
        len(registry.databases),
        registry.source,
    )
    results: list[SyncResult] = []
    for entry in registry.databases:
        results.extend(sync_entry(entry))

    ok = sum(1 for r in results if r.ok)
    logger.info(
        "Catalog sync pass complete: %d/%d targets ok",
        ok,
        len(results),
    )
    return results


def sync_forever(
    registry: CatalogRegistry | None = None,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Sync on a loop, sleeping ``interval_seconds`` between passes.

    ``stop_event`` (when supplied) makes the loop cleanly interruptible — the
    sleep is event-driven so a shutdown signal does not have to wait out the
    full interval. Without it, the loop runs until the process is killed.
    """
    if registry is None:
        registry = load_registry()
    if not registry:
        logger.warning("Catalog registry is empty; sidecar has nothing to do")
        return

    interval = registry.interval_seconds
    stop = stop_event or threading.Event()
    logger.info(
        "Catalog sidecar started: %d database(s), interval=%ds",
        len(registry.databases),
        interval,
    )
    while not stop.is_set():
        sync_once(registry)
        if stop.wait(timeout=interval):
            break
    logger.info("Catalog sidecar stopped")
