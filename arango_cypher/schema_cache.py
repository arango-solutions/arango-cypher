"""Persistent schema-mapping cache backed by an ArangoDB collection.

Design intent
-------------
Two-tier caching around :func:`arango_cypher.schema_acquire.get_mapping`:

1. A fast process-local ``dict`` cache (kept in ``schema_acquire``) gates
   on a shape fingerprint.
2. A persistent ArangoDB-collection cache (this module) survives process
   restarts and is shared across service instances connecting to the
   same database.

The cache document stores:

- ``shape_fingerprint`` — hashed collection set, types, and index digests
  (no row counts). Used to decide if a full re-introspection is needed.
- ``full_fingerprint`` — shape + per-collection counts. When it differs
  but the shape fingerprint matches, the cached mapping is reused and
  only cardinality statistics are re-computed.
- ``bundle`` — serialized :class:`~arango_query_core.MappingBundle`.
- ``schema_version`` — cache format version. On mismatch the cached
  document is ignored (and eventually overwritten).

The collection is created on first write; reads against a missing
collection return ``None`` and are silent (no error). A corrupt / partial
document is treated as a miss.

The cache document ``_key`` defaults to ``"mapping"``; callers who run
multiple logical mappings against the same database (e.g. restricted to
a subset of collections) can pass a distinguishing ``cache_key``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from arango_query_core import MappingBundle, MappingSource

if TYPE_CHECKING:
    from arango.database import StandardDatabase

logger = logging.getLogger(__name__)

DEFAULT_CACHE_COLLECTION = "arango_cypher_schema_cache"
DEFAULT_CACHE_KEY = "mapping"
CACHE_SCHEMA_VERSION = 1


def bundle_to_doc(bundle: MappingBundle) -> dict[str, Any]:
    """Serialize a :class:`MappingBundle` to a JSON-safe dict.

    ``conceptual_schema``, ``physical_mapping``, and ``metadata`` are already
    JSON-typed. ``source`` is a frozen dataclass and is round-tripped via
    :func:`dataclasses.asdict`. ``owl_turtle`` is a string or ``None``.
    """
    return {
        "conceptual_schema": bundle.conceptual_schema,
        "physical_mapping": bundle.physical_mapping,
        "metadata": bundle.metadata,
        "owl_turtle": bundle.owl_turtle,
        "source": asdict(bundle.source) if is_dataclass(bundle.source) else None,
    }


def bundle_from_doc(doc: dict[str, Any]) -> MappingBundle:
    """Reverse of :func:`bundle_to_doc`.

    Raises :class:`KeyError` or :class:`TypeError` on a malformed document;
    callers should catch broadly and treat failures as a cache miss rather
    than propagate (the cache is a performance hint, not a source of truth).
    """
    src_raw = doc.get("source")
    source = MappingSource(**src_raw) if isinstance(src_raw, dict) else None
    return MappingBundle(
        conceptual_schema=doc["conceptual_schema"],
        physical_mapping=doc["physical_mapping"],
        metadata=doc["metadata"],
        owl_turtle=doc.get("owl_turtle"),
        source=source,
    )


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class ArangoSchemaCache:
    """Collection-backed mapping cache.

    One document per ``cache_key`` (default: ``"mapping"``). The class is
    stateless — pass the ``db`` handle on every call so the cache can be
    reused across connections / threads without worrying about stale
    references.
    """

    def __init__(
        self,
        *,
        collection_name: str = DEFAULT_CACHE_COLLECTION,
        cache_key: str = DEFAULT_CACHE_KEY,
    ) -> None:
        self.collection_name = collection_name
        self.cache_key = cache_key

    def _ensure_collection(self, db: StandardDatabase) -> Any:
        """Return a live collection handle, creating the collection if missing.

        Returns ``None`` if the collection cannot be created (e.g. read-only
        user, unreachable coordinator). The cache is best-effort and must
        never fail the caller — a failed persistence attempt degrades to
        an in-memory-only cache for this session.
        """
        try:
            if db.has_collection(self.collection_name):
                return db.collection(self.collection_name)
        except Exception:
            logger.debug(
                "Cache-collection existence check failed for %r; treating as miss",
                self.collection_name,
                exc_info=True,
            )
            return None
        try:
            return db.create_collection(self.collection_name)
        except Exception:
            logger.warning(
                "Could not create schema cache collection %r; persistent cache disabled for this session",
                self.collection_name,
                exc_info=True,
            )
            return None

    def get(self, db: StandardDatabase) -> tuple[MappingBundle, str, str] | None:
        """Fetch cached ``(bundle, shape_fp, full_fp)`` or ``None`` on miss."""
        try:
            if not db.has_collection(self.collection_name):
                return None
            col = db.collection(self.collection_name)
            doc = col.get(self.cache_key)
        except Exception:
            logger.debug(
                "Schema cache read failed (%r); treating as miss",
                self.collection_name,
                exc_info=True,
            )
            return None
        if not isinstance(doc, dict):
            return None
        if doc.get("schema_version") != CACHE_SCHEMA_VERSION:
            logger.info(
                "Schema cache %r is at version %r; expected %r. Ignoring.",
                self.collection_name,
                doc.get("schema_version"),
                CACHE_SCHEMA_VERSION,
            )
            return None
        shape_fp = doc.get("shape_fingerprint")
        full_fp = doc.get("full_fingerprint")
        bundle_raw = doc.get("bundle")
        if not isinstance(shape_fp, str) or not isinstance(full_fp, str):
            return None
        if not isinstance(bundle_raw, dict):
            return None
        try:
            bundle = bundle_from_doc(bundle_raw)
        except (KeyError, TypeError):
            logger.warning(
                "Corrupt cache document at %s/%s; ignoring",
                self.collection_name,
                self.cache_key,
            )
            return None
        return bundle, shape_fp, full_fp

    def set(
        self,
        db: StandardDatabase,
        *,
        bundle: MappingBundle,
        shape_fingerprint: str,
        full_fingerprint: str,
    ) -> bool:
        """Persist a bundle + fingerprints. Returns ``True`` on success."""
        col = self._ensure_collection(db)
        if col is None:
            return False
        now = _utcnow_iso()
        doc = {
            "_key": self.cache_key,
            "schema_version": CACHE_SCHEMA_VERSION,
            "shape_fingerprint": shape_fingerprint,
            "full_fingerprint": full_fingerprint,
            "bundle": bundle_to_doc(bundle),
            "updated_at": now,
        }
        try:
            existing = col.get(self.cache_key)
        except Exception:
            existing = None
        try:
            if isinstance(existing, dict):
                doc["created_at"] = existing.get("created_at") or now
                col.update(doc, merge=False, keep_none=False)
            else:
                doc["created_at"] = now
                col.insert(doc, overwrite=True, silent=True)
            return True
        except Exception:
            logger.warning(
                "Failed to persist schema cache to %s/%s",
                self.collection_name,
                self.cache_key,
                exc_info=True,
            )
            return False

    def invalidate(self, db: StandardDatabase) -> bool:
        """Remove the cached document. Returns ``True`` if anything was deleted."""
        try:
            if not db.has_collection(self.collection_name):
                return False
            col = db.collection(self.collection_name)
            col.delete(self.cache_key, ignore_missing=True)
            return True
        except Exception:
            logger.debug(
                "Schema cache invalidation failed for %s/%s",
                self.collection_name,
                self.cache_key,
                exc_info=True,
            )
            return False
