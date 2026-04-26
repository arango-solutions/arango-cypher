"""Schema introspection / mapping cache endpoints + ``/sample-queries``."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from arango.database import StandardDatabase
from fastapi import Depends, HTTPException

from arango_query_core import MappingResolver

from ..app import app
from ..mapping import _mapping_from_dict
from ..models import TranslateRequest
from ..observability import log_endpoint_timing
from ..security import (
    _check_compute_rate_limit,
    _get_session,
    _Session,
)


def _sample_properties(
    db: StandardDatabase, collection_name: str, sample_size: int = 100
) -> dict[str, dict[str, Any]]:
    """Sample documents from a collection and infer property names and types."""
    try:
        cursor = db.aql.execute(
            "FOR doc IN @@col LIMIT @n RETURN doc",
            bind_vars={"@col": collection_name, "n": sample_size},
        )
        docs = list(cursor)
    except Exception:
        return {}

    if not docs:
        return {}

    field_types: dict[str, dict[str, int]] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if key.startswith("_"):
                continue
            if key not in field_types:
                field_types[key] = {}
            t = _infer_type(val)
            field_types[key][t] = field_types[key].get(t, 0) + 1

    result: dict[str, dict[str, Any]] = {}
    for name, types in field_types.items():
        dominant = max(types, key=types.get)  # type: ignore[arg-type]
        result[name] = {
            "field": name,
            "type": dominant,
            "required": len([d for d in docs if isinstance(d, dict) and name in d]) == len(docs),
        }
    return result


def _infer_type(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, int | float):
        return "number"
    if isinstance(val, str):
        return "string"
    if isinstance(val, list):
        return "array"
    if isinstance(val, dict):
        return "object"
    return "string"


def _infer_edge_endpoints(
    db: StandardDatabase,
    edge_collection: str,
    limit: int = 20,
) -> tuple[str | None, str | None]:
    """Sample _from/_to in an edge collection to determine which document collections it connects."""
    try:
        cursor = db.aql.execute(
            "FOR e IN @@col LIMIT @n RETURN { f: e._from, t: e._to }",
            bind_vars={"@col": edge_collection, "n": limit},
        )
        from_cols: set[str] = set()
        to_cols: set[str] = set()
        for doc in cursor:
            f, t = doc.get("f", ""), doc.get("t", "")
            if "/" in f:
                from_cols.add(f.split("/", 1)[0])
            if "/" in t:
                to_cols.add(t.split("/", 1)[0])
        domain = sorted(from_cols)[0] if len(from_cols) == 1 else None
        range_ = sorted(to_cols)[0] if len(to_cols) == 1 else None
        return domain, range_
    except Exception:
        return None, None


@app.get("/schema/introspect")
def schema_introspect(
    sample: int = 50,
    force: bool = False,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
):
    """Discover collections, edge collections, and their properties from the connected database.

    Delegates to ``get_mapping(db)`` which uses the 3-tier strategy:
    analyzer first (all schema types), heuristic fallback if the analyzer
    is not installed.

    Pass ``force=true`` to bypass *both* mapping cache tiers (in-process
    and persistent ``_schemas`` collection). Previously this only
    popped the in-process tier, which meant a stale entry in the
    persistent cache would keep getting served forever — the user's
    only recourse was to manually call ``/schema/invalidate-cache``,
    which is non-discoverable. Forwarding ``force_refresh=True`` to
    ``get_mapping`` makes the flag do what its name says.
    """
    t0 = time.perf_counter()
    db = session.db
    from ...schema_acquire import get_mapping as _get_mapping

    bundle = _get_mapping(db, force_refresh=force)

    resolver = MappingResolver(bundle)
    result = resolver.schema_summary()

    # Build collection→label lookup from entities
    col_to_label: dict[str, str] = {}
    for ent in result.get("entities", []):
        col_to_label[ent.get("collection", "")] = ent.get("label", "")

    # For PG schemas, fill in missing domain/range by sampling _from/_to
    for rel in result.get("relationships", []):
        if rel.get("domain") and rel.get("range"):
            continue
        edge_col = rel.get("edgeCollection", "")
        if not edge_col:
            continue
        from_col, to_col = _infer_edge_endpoints(db, edge_col)
        if from_col and not rel.get("domain"):
            rel["domain"] = col_to_label.get(from_col, from_col)
        if to_col and not rel.get("range"):
            rel["range"] = col_to_label.get(to_col, to_col)

    result["warnings"] = (bundle.metadata or {}).get("warnings") or []
    log_endpoint_timing(
        "/schema/introspect",
        round((time.perf_counter() - t0) * 1000, 1),
        force=force,
        entities=len(result.get("entities") or []),
        relationships=len(result.get("relationships") or []),
        warnings=len(result["warnings"]),
        source=(bundle.source.kind if bundle.source is not None else "unknown"),
    )
    return result


@app.get("/schema/properties")
def schema_properties(
    collection: str,
    sample: int = 100,
    session: _Session = Depends(_get_session),
):
    """Infer properties for a specific collection by sampling documents."""
    t0 = time.perf_counter()
    props = _sample_properties(session.db, collection, sample)
    log_endpoint_timing(
        "/schema/properties",
        round((time.perf_counter() - t0) * 1000, 1),
        collection=collection,
        sample_size=sample,
        properties=len(props),
    )
    return {"collection": collection, "sample_size": sample, "properties": props}


@app.get("/schema/summary")
def schema_summary(
    req: TranslateRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Return a structured summary of the mapping for the visual graph editor."""
    t0 = time.perf_counter()
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")
    resolver = MappingResolver(mapping)
    summary = resolver.schema_summary()
    log_endpoint_timing(
        "/schema/summary",
        round((time.perf_counter() - t0) * 1000, 1),
        entities=len(summary.get("entities") or []),
        relationships=len(summary.get("relationships") or []),
    )
    return summary


@app.get("/schema/statistics")
def schema_statistics(
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
):
    """Compute and return cardinality statistics for the connected database.

    Returns collection counts, per-entity estimated counts, per-relationship
    fan-out/fan-in metrics, cardinality patterns, and selectivity ratios.
    """
    from ...schema_acquire import compute_statistics as _compute_stats
    from ...schema_acquire import get_mapping as _get_mapping

    t0 = time.perf_counter()
    bundle = _get_mapping(session.db)
    stats = _compute_stats(session.db, bundle)
    elapsed = round(time.perf_counter() - t0, 3)
    log_endpoint_timing(
        "/schema/statistics",
        round(elapsed * 1000, 1),
        elapsed_seconds=elapsed,
    )
    return {"statistics": stats, "elapsed_seconds": elapsed}


@app.get("/schema/status")
def schema_status(
    cache_collection: str | None = None,
    cache_key: str | None = None,
    session: _Session = Depends(_get_session),
):
    """Report whether the schema has changed since the cached mapping was built.

    Cheap probe: runs ``db.collections()`` + per-collection ``count()`` +
    ``indexes()``. No document sampling, no AQL, no LLM call. Typical cost
    ~20 ms for a 50-collection schema.

    ``status`` values:

    * ``"unchanged"`` — the cached mapping is fully valid.
    * ``"stats_changed"`` — shape matches but counts differ; calling a
      mapping-consuming endpoint (e.g. ``/schema/introspect``) will refresh
      only the statistics block.
    * ``"shape_changed"`` — collection set, collection type, or an index
      set has changed; a mapping-consuming endpoint will re-introspect.
    * ``"no_cache"`` — no prior fingerprint recorded (e.g. first call
      after service start or after ``POST /schema/invalidate-cache``).

    Response also includes ``unchanged`` and ``needs_full_rebuild``
    convenience booleans and the four fingerprints (current + cached,
    shape + full) so callers can build their own diff UIs.

    Use this to skip expensive prompt rebuilds / view cache busts /
    downstream notifications when nothing has actually changed.
    """
    from ...schema_acquire import (
        DEFAULT_CACHE_COLLECTION,
        DEFAULT_CACHE_KEY,
    )
    from ...schema_acquire import (
        describe_schema_change as _describe,
    )

    t0 = time.perf_counter()
    report = _describe(
        session.db,
        cache_collection=cache_collection or DEFAULT_CACHE_COLLECTION,
        cache_key=cache_key or DEFAULT_CACHE_KEY,
    )
    log_endpoint_timing(
        "/schema/status",
        round((time.perf_counter() - t0) * 1000, 1),
        report_status=report.status,
        unchanged=bool(report.unchanged),
    )
    return {
        "status": report.status,
        "unchanged": report.unchanged,
        "needs_full_rebuild": report.needs_full_rebuild,
        "current_shape_fingerprint": report.current_shape_fingerprint,
        "current_full_fingerprint": report.current_full_fingerprint,
        "cached_shape_fingerprint": report.cached_shape_fingerprint,
        "cached_full_fingerprint": report.cached_full_fingerprint,
    }


@app.post("/schema/invalidate-cache")
def schema_invalidate_cache(
    cache_collection: str | None = None,
    cache_key: str | None = None,
    persistent: bool = True,
    session: _Session = Depends(_get_session),
):
    """Drop the in-memory and (optionally) persistent mapping cache.

    The next call to ``/schema/introspect`` — or any other mapping-consuming
    endpoint — will re-introspect the schema unconditionally.

    Query parameters:

    * ``cache_collection`` — name of the persistent cache collection
      (default: ``arango_cypher_schema_cache``). Used only when
      ``persistent=true``.
    * ``cache_key`` — key inside the cache collection (default:
      ``mapping``). Used only when ``persistent=true``.
    * ``persistent`` — when ``true`` (default), both the in-memory and the
      persistent cache are dropped. When ``false``, only the in-memory
      (process-local) cache is dropped; the persistent cache survives
      and will be re-read on the next call from a cold process.

    Use ``persistent=false`` for targeted in-process invalidation (e.g.
    after an administrative action that you know only affects the current
    replica's view, not the shared database state).
    """
    from ...schema_acquire import (
        DEFAULT_CACHE_COLLECTION,
        DEFAULT_CACHE_KEY,
    )
    from ...schema_acquire import (
        invalidate_cache as _invalidate,
    )

    t0 = time.perf_counter()
    _invalidate(
        session.db,
        cache_collection=(cache_collection or DEFAULT_CACHE_COLLECTION) if persistent else None,
        cache_key=cache_key or DEFAULT_CACHE_KEY,
    )
    log_endpoint_timing(
        "/schema/invalidate-cache",
        round((time.perf_counter() - t0) * 1000, 1),
        persistent=persistent,
    )
    return {"invalidated": True, "persistent": persistent}


@app.post("/schema/force-reacquire")
def schema_force_reacquire(
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
):
    """Drop any cached mapping and rebuild from scratch via the analyzer.

    Operational tool for recovering from a poisoned cache: the previous
    ``get_mapping`` call fell back to the heuristic because the analyzer
    was not installed at that moment, the degraded bundle got persisted,
    and subsequent ``force=true`` introspects re-served the same cached
    bundle because the shape fingerprint did not change. This endpoint
    calls ``get_mapping(..., strategy="analyzer", force_refresh=True)`` —
    the hard form — which raises ``ImportError`` (surfaced as HTTP 503) if
    the analyzer is still unavailable instead of silently falling back.
    """
    from ...schema_acquire import get_mapping as _get_mapping

    t0 = time.perf_counter()
    try:
        bundle = _get_mapping(session.db, force_refresh=True, strategy="analyzer")
    except ImportError as exc:
        log_endpoint_timing(
            "/schema/force-reacquire",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            error_type="ImportError",
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "arangodb-schema-analyzer is not installed; cannot force a "
                "fresh analyzer mapping. Install it and retry, or call "
                "/schema/invalidate-cache to drop the cached entry and let "
                "the heuristic fallback run again. Underlying error: "
                f"{exc}"
            ),
        ) from exc

    source_kind = bundle.source.kind if bundle.source is not None else None
    source_notes = bundle.source.notes if bundle.source is not None else None
    warnings = (bundle.metadata or {}).get("warnings") or []
    payload = {
        "source": {"kind": source_kind, "notes": source_notes},
        "warnings": warnings,
        "entity_count": len(bundle.conceptual_schema.get("entities") or []),
        "relationship_count": len(bundle.conceptual_schema.get("relationships") or []),
    }
    log_endpoint_timing(
        "/schema/force-reacquire",
        round((time.perf_counter() - t0) * 1000, 1),
        source=source_kind or "unknown",
        entities=payload["entity_count"],
        relationships=payload["relationship_count"],
        warnings=len(warnings),
    )
    return payload


# ---------------------------------------------------------------------------
# Sample queries (query corpus files)
# ---------------------------------------------------------------------------
#
# The fixtures dir lives at <repo>/tests/fixtures, two levels up from
# this module after the audit-v2 #8 split (was one level under the
# pre-split flat-file layout).
_FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "tests" / "fixtures"


@app.get("/sample-queries")
def sample_queries(dataset: str | None = None):
    """Return sample Cypher queries from the query corpus files.

    Optionally filter by dataset name (e.g., 'movies', 'social').
    """
    import yaml

    t0 = time.perf_counter()
    corpora: list[dict[str, Any]] = []
    datasets_dir = _FIXTURES_DIR / "datasets"
    if not datasets_dir.is_dir():
        log_endpoint_timing(
            "/sample-queries",
            round((time.perf_counter() - t0) * 1000, 1),
            queries=0,
            datasets_dir_missing=True,
        )
        return {"queries": []}

    for corpus_file in sorted(datasets_dir.rglob("query-corpus.yml")):
        ds_name = corpus_file.parent.name
        if dataset and ds_name != dataset:
            continue
        try:
            entries = yaml.safe_load(corpus_file.read_text(encoding="utf-8")) or []
        except Exception:
            continue
        for entry in entries:
            if isinstance(entry, dict):
                entry["dataset"] = ds_name
                corpora.append(entry)

    log_endpoint_timing(
        "/sample-queries",
        round((time.perf_counter() - t0) * 1000, 1),
        queries=len(corpora),
        dataset_filter=dataset or "",
    )
    return {"queries": corpora}
