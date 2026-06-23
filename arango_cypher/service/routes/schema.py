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
from ..models import CreateIndexRequest, TranslateRequest
from ..observability import log_endpoint_timing
from ..security import (
    _check_compute_rate_limit,
    _COLLECTION_NAME_RE,
    _get_session,
    _Session,
    _translate_errors,
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


def _summarize_bundle(db: StandardDatabase, bundle: Any) -> dict[str, Any]:
    """Build the introspect summary (entities/relationships) from a bundle.

    For PG schemas the analyzer may leave a relationship's domain/range blank;
    we backfill those by sampling ``_from``/``_to`` on the edge collection.
    """
    resolver = MappingResolver(bundle)
    result = resolver.schema_summary()

    col_to_label: dict[str, str] = {}
    for ent in result.get("entities", []):
        col_to_label[ent.get("collection", "")] = ent.get("label", "")

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
    return result


@app.get("/schema/introspect")
def schema_introspect(
    sample: int = 50,
    force: bool = False,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
):
    """Serve the analyzed schema for the connected database from the catalog.

    Catalog model (PRD §"Schema catalog sidecar"): schema analysis is performed
    out of band by the sidecar (:mod:`arango_cypher.catalog`) and persisted to
    the shared cache. This endpoint is **read-only by default** — it returns
    whatever the catalog holds without ever running the analyzer or a fingerprint
    walk on the request path, so it responds in milliseconds instead of blocking
    for tens of seconds.

    Responses carry a ``status``:

    * ``"ready"`` — a mapping was served (``entities`` / ``relationships``
      populated).
    * ``"pending"`` — the database has not been analyzed yet; an out-of-band
      warm has been kicked off and the client should retry shortly. Returned
      with empty ``entities`` / ``relationships`` so the UI can show a
      "preparing schema" state instead of an error.

    Pass ``force=true`` ("Refresh schema" / "Analyze now") to rebuild
    synchronously, bypassing the catalog. This is the opt-in slow path for power
    users who need an immediate refresh.
    """
    t0 = time.perf_counter()
    db = session.db
    graph_name = getattr(session, "graph_name", None)
    from ...schema_acquire import get_mapping as _get_mapping
    from ...schema_acquire import read_cached_mapping as _read_cached

    if force:
        bundle = _get_mapping(db, force_refresh=True, graph_name=graph_name)
    else:
        bundle = _read_cached(db, graph_name=graph_name)

    if bundle is None:
        # True catalog miss: the sidecar has not analyzed this database yet.
        # Kick off a one-shot background warm (deduped per database+graph) using
        # the live session handle so an interactively-connected database that
        # was never registered in the sidecar self-heals — the client's retry
        # then finds a populated cache. We still return immediately; the
        # expensive analyzer never runs on the request path.
        from ...catalog.warm import schedule_warm

        warming = schedule_warm(db, graph_name)
        log_endpoint_timing(
            "/schema/introspect",
            round((time.perf_counter() - t0) * 1000, 1),
            force=force,
            status="pending",
            warming=warming,
        )
        return {
            "status": "pending",
            "warming": warming,
            "entities": [],
            "relationships": [],
            "warnings": [
                {
                    "code": "SCHEMA_PENDING",
                    "message": (
                        "Schema for this database is being analyzed in the "
                        "background — retry in a moment, or use "
                        '"Refresh schema" to analyze it now.'
                        if warming
                        else (
                            "Schema for this database has not been analyzed "
                            "yet. The catalog sidecar will populate it shortly "
                            '— retry in a moment, or use "Refresh schema" to '
                            "analyze it now."
                        )
                    ),
                }
            ],
        }

    result = _summarize_bundle(db, bundle)
    result["status"] = "ready"
    log_endpoint_timing(
        "/schema/introspect",
        round((time.perf_counter() - t0) * 1000, 1),
        force=force,
        status="ready",
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
    from ...schema_acquire import read_cached_mapping as _read_cached

    t0 = time.perf_counter()
    graph_name = getattr(session, "graph_name", None)
    # Catalog model: read the analyzed mapping from the cache rather than build
    # it inline. A miss returns pending so this never blocks on the analyzer;
    # the sidecar (or "Refresh schema") populates the catalog.
    bundle = _read_cached(session.db, graph_name=graph_name)
    if bundle is None:
        # Mirror /schema/introspect: a catalog miss self-heals via a one-shot
        # background warm (deduped, so introspect + statistics firing together
        # share a single analysis pass) rather than staying pending forever.
        from ...catalog.warm import schedule_warm

        warming = schedule_warm(session.db, graph_name)
        log_endpoint_timing(
            "/schema/statistics",
            round((time.perf_counter() - t0) * 1000, 1),
            status="pending",
            warming=warming,
        )
        return {
            "status": "pending",
            "warming": warming,
            "statistics": {},
            "elapsed_seconds": 0.0,
        }

    stats = _compute_stats(session.db, bundle)
    elapsed = round(time.perf_counter() - t0, 3)
    log_endpoint_timing(
        "/schema/statistics",
        round(elapsed * 1000, 1),
        status="ready",
        elapsed_seconds=elapsed,
    )
    return {"status": "ready", "statistics": stats, "elapsed_seconds": elapsed}


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
        graph_name=getattr(session, "graph_name", None),
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
        bundle = _get_mapping(
            session.db,
            force_refresh=True,
            strategy="analyzer",
            graph_name=getattr(session, "graph_name", None),
        )
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


@app.post("/schema/index/create")
def create_index(
    req: CreateIndexRequest,
    session: _Session = Depends(_get_session),
):
    """WP-S3c: create an inverted index to accelerate fuzzy name matching.

    Consumes an ``IndexAdvisory`` (collection + field) surfaced by the NL
    entity resolver and creates an inverted index with a text analyzer on that
    field. The index spec is reconstructed here from the validated fields — the
    client never passes a free-form spec — so this endpoint can only create the
    one shape the advisory recommends.

    Idempotent: if an inverted index already covers the field it returns
    ``created=False`` instead of erroring, so the UI's one-click button is safe
    to press more than once (and safe after a background sidecar already added
    the index).
    """
    coll = req.collection
    field = req.field
    if not _COLLECTION_NAME_RE.fullmatch(coll):
        raise HTTPException(status_code=400, detail="Invalid collection name")
    if not field.strip():
        raise HTTPException(status_code=400, detail="Field name is required")
    t0 = time.perf_counter()
    db = session.db
    with _translate_errors("Index creation failed"):
        if not db.has_collection(coll):
            raise HTTPException(status_code=404, detail=f"Collection '{coll}' not found")
        collection = db.collection(coll)
        # Idempotency guard: don't add a second inverted index over the same
        # field. Mirrors the resolver's own index-coverage probe.
        for idx in collection.indexes():
            if str(idx.get("type", "")).lower() != "inverted":
                continue
            names = {
                (f.get("name") if isinstance(f, dict) else f)
                for f in (idx.get("fields") or [])
            }
            if field in names:
                log_endpoint_timing(
                    "/schema/index/create",
                    round((time.perf_counter() - t0) * 1000, 1),
                    collection=coll,
                    field=field,
                    created=False,
                )
                return {
                    "created": False,
                    "collection": coll,
                    "field": field,
                    "index": {"name": idx.get("name"), "id": idx.get("id")},
                    "message": "An inverted index already covers this field.",
                }
        spec = {
            "type": "inverted",
            "name": req.name or f"idx_fuzzy_{field}",
            "fields": [{"name": field, "analyzer": req.analyzer}],
        }
        created = collection.add_index(spec)
    log_endpoint_timing(
        "/schema/index/create",
        round((time.perf_counter() - t0) * 1000, 1),
        collection=coll,
        field=field,
        created=True,
    )
    return {"created": True, "collection": coll, "field": field, "index": created}


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
