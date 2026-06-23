"""Schema-catalog sidecar: keep analyzed mappings warm out of the request path.

Schema analysis (LLM-grade conceptual extraction + statistics) is expensive —
seconds to minutes on a remote cluster. Running it inline on a user click makes
the interactive tool unusable. This package moves that work out of band:

* :mod:`arango_cypher.catalog.registry` — a small, declarative list of the
  databases (and optional named-graph scopes) to keep analyzed.
* :mod:`arango_cypher.catalog.sync` — a sidecar refresher that, on a schedule,
  rebuilds each registered mapping and writes it to that database's shared
  persistent cache (``arango_cypher_schema_cache``).

The FastAPI service then reads the catalog (the persistent cache) *read-only*
and never triggers analysis on the request path. See ``docs/python_prd.md``
§"Schema catalog sidecar".
"""

from __future__ import annotations

from .registry import (
    CatalogRegistry,
    DatabaseEntry,
    load_registry,
)
from .sync import (
    SyncResult,
    sync_entry,
    sync_forever,
    sync_once,
)

__all__ = [
    "CatalogRegistry",
    "DatabaseEntry",
    "SyncResult",
    "load_registry",
    "sync_entry",
    "sync_forever",
    "sync_once",
]
