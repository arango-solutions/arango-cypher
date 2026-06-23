"""On-demand background schema warming for catalog misses.

The catalog model keeps the schema endpoints read-only and fast: they serve
whatever the sidecar (:mod:`arango_cypher.catalog.sync`) has already analyzed
and never run the analyzer on the request path. That leaves a gap for an
ad-hoc database the operator connects to interactively but never listed in
``configs/catalog.yml`` — its first ``/schema/introspect`` is a hard miss and
stays ``pending`` forever unless someone clicks "Refresh schema".

This module closes that gap. When an endpoint hits a catalog miss it calls
:func:`schedule_warm`, which kicks off **one** background analysis for that
``(database, graph)`` slot using the live, already-authenticated session
handle. The endpoint still returns ``pending`` immediately (no blocking on the
request path); the client's existing retry then finds a populated cache.

Concurrency notes:

* Warms are deduped per ``(database, graph)`` via an in-flight set guarded by a
  lock, so a client retry loop can hammer the endpoint without spawning a pile
  of redundant analyzer passes.
* The warm reuses the session's ``StandardDatabase`` handle. Sync FastAPI
  endpoints already run concurrently against one session handle (the UI fires
  ``/schema/introspect`` and ``/schema/statistics`` together), so this adds no
  new class of concurrency. A worst case (the session is evicted mid-warm and
  its client closed) surfaces as a logged warm failure, not a crash — the next
  connection simply re-triggers the warm.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arango.database import StandardDatabase

logger = logging.getLogger(__name__)

_inflight: set[str] = set()
_lock = threading.Lock()


def _warm_key(db_name: str, graph_name: str | None) -> str:
    """Stable per-target key mirroring the catalog cache's (db, graph) scoping."""
    return f"{db_name}::{graph_name or ''}"


def is_warming(db_name: str, graph_name: str | None = None) -> bool:
    """Return whether a background warm is currently in flight for the target."""
    with _lock:
        return _warm_key(db_name, graph_name) in _inflight


def schedule_warm(
    db: "StandardDatabase",
    graph_name: str | None = None,
) -> bool:
    """Start a one-shot background warm for ``(db, graph_name)`` if not running.

    Returns ``True`` when a new warm thread was started, ``False`` when a warm
    for the same target was already in flight (deduped) or the database name
    could not be resolved. Never raises — warming is best-effort.
    """
    try:
        db_name = db.name
    except Exception:  # noqa: BLE001 - a handle without a resolvable name is unusable
        logger.warning("schedule_warm: could not resolve database name; skipping")
        return False
    if not db_name:
        return False

    key = _warm_key(db_name, graph_name)
    with _lock:
        if key in _inflight:
            return False
        _inflight.add(key)

    def _run() -> None:
        # Imported lazily so importing the catalog package never hard-requires
        # the analyzer/driver stack just to reference the warm scheduler.
        from ..schema_acquire import get_mapping

        try:
            logger.info(
                "Catalog warm starting (background): db=%s graph=%s",
                db_name,
                graph_name or "-",
            )
            get_mapping(db, force_refresh=True, graph_name=graph_name)
            logger.info(
                "Catalog warm complete: db=%s graph=%s", db_name, graph_name or "-"
            )
        except Exception:  # noqa: BLE001 - background best-effort; log and move on
            logger.warning(
                "Catalog warm failed: db=%s graph=%s",
                db_name,
                graph_name or "-",
                exc_info=True,
            )
        finally:
            with _lock:
                _inflight.discard(key)

    thread = threading.Thread(
        target=_run, name=f"catalog-warm-{key}", daemon=True
    )
    thread.start()
    return True
