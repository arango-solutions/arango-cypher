"""Automatic mapping acquisition from a live ArangoDB database.

Provides three tiers of mapping acquisition:
1. Analyzer (primary): delegates to arangodb-schema-analyzer for full ontology
   extraction across PG, LPG, and hybrid schemas
2. Heuristic (fallback): fast classification + simple mapping construction when
   the analyzer is not installed
3. Auto (default): analyzer first, heuristic fallback on ImportError
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

# Upstream fingerprint primitives (arangodb-schema-analyzer >= 0.3.0) are
# imported lazily inside :func:`_shape_fingerprint` / :func:`_full_fingerprint`.
# Lazy so this module keeps working when only the heuristic mapping tier is
# installed (the `analyzer` extra is optional — see module docstring tier 2).
# The wrappers bake in our cache-collection exclusion; see §5 of
# docs/schema_analyzer_issues/WAVE_4M_ARCHITECTURE.md for why excluding the
# cache collection is a correctness invariant, not a perf tweak.
from arango_query_core import CoreError, MappingBundle, MappingSource

from .schema_cache import (
    DEFAULT_CACHE_COLLECTION,
    DEFAULT_CACHE_KEY,
    ArangoSchemaCache,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from arango.database import StandardDatabase

CACHE_TTL_SECONDS = 300

# In-memory fast path: (bundle, ts, shape_fp, full_fp) keyed by db name + cache key.
_mapping_cache: dict[str, tuple[MappingBundle, float, str, str]] = {}

# Operational counter — incremented every time `_build_fresh_bundle` falls
# through to the heuristic path because `schema_analyzer` could not be
# imported. Read-only for the outside world; the current release does not
# yet expose a /metrics endpoint, but operators can inspect it from a Python
# shell or a future metrics surface will aggregate it.
_heuristic_fallback_counter: int = 0


def _attach_warning(
    bundle: MappingBundle,
    *,
    code: str,
    message: str,
    install_hint: str | None = None,
) -> MappingBundle:
    """Return a copy of ``bundle`` with an additional structured warning.

    Warnings live at ``bundle.metadata["warnings"]`` as a list of dicts with
    keys ``code``, ``message`` and (optionally) ``install_hint``. Each call
    appends; existing warnings are preserved. Deliberately copies the
    metadata dict so the original bundle (and any cached reference to it)
    is not mutated under the caller's feet.
    """
    meta = dict(bundle.metadata or {})
    warnings = list(meta.get("warnings") or [])
    warning: dict[str, Any] = {"code": code, "message": message}
    if install_hint:
        warning["install_hint"] = install_hint
    warnings.append(warning)
    meta["warnings"] = warnings
    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=bundle.physical_mapping,
        metadata=meta,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


def _bundle_needs_reacquire(bundle: MappingBundle) -> bool:
    """True when a cached heuristic-fallback bundle should be rebuilt.

    Returns True iff (a) the bundle carries an ``ANALYZER_NOT_INSTALLED``
    warning from an earlier heuristic fallback, AND (b) ``schema_analyzer``
    is now importable in this process. The second check makes the retry
    loop self-healing: when an operator installs the analyzer and the
    next request lands on this worker, the cached degraded bundle is
    treated as a miss and the analyzer path runs.
    """
    warnings = (bundle.metadata or {}).get("warnings") or []
    if not any(
        isinstance(w, dict) and w.get("code") == "ANALYZER_NOT_INSTALLED"
        for w in warnings
    ):
        return False
    try:
        import schema_analyzer  # noqa: F401
    except ImportError:
        return False
    return True


def _cache_key(db: StandardDatabase) -> str:
    """Stable cache key: database name only. Used as the dict key.

    The actual staleness check is done via :func:`_shape_fingerprint` and
    :func:`_full_fingerprint` which inspect the live collection set.
    """
    try:
        return db.name
    except Exception:
        return ""


def _fallback_fingerprint(db: StandardDatabase, *, include_counts: bool) -> str:
    """Coarse local fingerprint used only when ``schema_analyzer`` is unavailable.

    The heuristic mapping tier is advertised as "works without the analyzer
    extra" (see module docstring), so we still need *some* stable digest for
    the cache-freshness check. Upstream hashes far more (types + every index
    signature); this fallback only notices collection set / count changes.
    Acceptable because the degraded path already opts out of analyzer-level
    precision. Re-introduces ~6 LOC versus the ~51 LOC removed in PR-2.
    """
    try:
        cols = db.collections() or []
    except Exception:
        cols = []
    names = sorted(
        c.get("name", "")
        for c in cols
        if isinstance(c, dict)
        and isinstance(c.get("name"), str)
        and not c["name"].startswith("_")
        and c["name"] != DEFAULT_CACHE_COLLECTION
    )
    parts = [db.name, *names]
    if include_counts:
        for name in names:
            try:
                parts.append(f"{name}:{db.collection(name).count()}")
            except Exception:
                parts.append(f"{name}:-1")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _shape_fingerprint(db: StandardDatabase) -> str:
    """Hash of the schema *shape*: collection set, types, and index digests.

    Thin wrapper around ``schema_analyzer.fingerprint_physical_shape`` (v0.3.0+)
    that bakes in our cache-collection exclusion. Kept as a named function (a)
    so existing imports in tests and callers continue to resolve and (b) so
    every caller in this module hits the same exclusion policy without
    rediscovering it.

    Excludes row counts so ordinary writes (INSERT / UPDATE / REMOVE without
    a schema shape change) do not invalidate the fingerprint. This is the
    fingerprint that decides whether a full re-introspection is needed.

    NOTE (cache re-key event, 2026-04-20): when this module was rewired to
    upstream at v0.3.0, the on-disk hash format changed — existing entries
    in ``_arango_schema_cache`` will miss their fingerprint check exactly
    once and be rebuilt. No action required on the operator side; the next
    ``get_mapping()`` call after deployment refills the cache under the new
    fingerprint.
    """
    try:
        from schema_analyzer import fingerprint_physical_shape
    except ImportError:
        return _fallback_fingerprint(db, include_counts=False)

    return fingerprint_physical_shape(db, exclude_collections={DEFAULT_CACHE_COLLECTION})


def _full_fingerprint(db: StandardDatabase) -> str:
    """Shape fingerprint + per-collection row counts.

    Thin wrapper around ``schema_analyzer.fingerprint_physical_counts``
    (v0.3.0+) with our cache-collection exclusion applied. See
    :func:`_shape_fingerprint` for the rationale and the one-time cache
    re-key event.

    Changes whenever either the schema shape or any collection's row count
    changes. When this differs but :func:`_shape_fingerprint` matches, the
    cached mapping remains valid and only cardinality statistics need
    re-computation (the stats-only refresh path).
    """
    try:
        from schema_analyzer import fingerprint_physical_counts
    except ImportError:
        return _fallback_fingerprint(db, include_counts=True)

    return fingerprint_physical_counts(db, exclude_collections={DEFAULT_CACHE_COLLECTION})


@dataclass(frozen=True)
class SchemaChangeReport:
    """Result of a lightweight schema-change probe.

    Returned by :func:`describe_schema_change`. Compared to :func:`get_mapping`
    this probe does not load or rebuild the mapping; it answers only the
    question "would ``get_mapping()`` need to do real work?". Use it to
    short-circuit application-level refresh logic (e.g. skip prompt rebuilds,
    cache-bust downstream views, signal clients) when nothing has changed.

    ``status`` values:

    - ``"unchanged"`` — shape and counts both match cache; the cached mapping
      is fully valid and includes up-to-date statistics.
    - ``"stats_changed"`` — shape matches but counts differ. Calling
      ``get_mapping()`` will reuse the cached conceptual schema + physical
      mapping and refresh only the cardinality statistics in metadata.
    - ``"shape_changed"`` — the collection set, a collection's type, or an
      index set has changed. Calling ``get_mapping()`` triggers a full
      re-introspection (analyzer or heuristic).
    - ``"no_cache"`` — nothing cached yet; first call since service start or
      after an explicit invalidation. ``get_mapping()`` will do a full
      introspection.
    """

    status: Literal["unchanged", "stats_changed", "shape_changed", "no_cache"]
    current_shape_fingerprint: str
    current_full_fingerprint: str
    cached_shape_fingerprint: str | None
    cached_full_fingerprint: str | None

    @property
    def unchanged(self) -> bool:
        """Ergonomic predicate: ``True`` iff ``status == "unchanged"``."""
        return self.status == "unchanged"

    @property
    def needs_full_rebuild(self) -> bool:
        """``True`` when the next ``get_mapping()`` will re-introspect."""
        return self.status in ("shape_changed", "no_cache")


def describe_schema_change(
    db: StandardDatabase,
    *,
    cache_collection: str = DEFAULT_CACHE_COLLECTION,
    cache_key: str = DEFAULT_CACHE_KEY,
) -> SchemaChangeReport:
    """Report whether the schema has changed since the last cached mapping.

    Cheap: runs ``db.collections()`` + per-collection ``count()`` +
    ``indexes()``. No document sampling, no AQL ``COLLECT``, no LLM call.
    Typical cost: ~20 ms for a 50-collection schema.

    Inspects the in-memory cache first, then the persistent ArangoDB
    collection cache. Does not mutate either cache — purely read-only.
    """
    shape_fp = _shape_fingerprint(db)
    full_fp = _full_fingerprint(db)
    key = _cache_key(db)
    cache = ArangoSchemaCache(
        collection_name=cache_collection, cache_key=cache_key
    )

    cached_shape: str | None = None
    cached_full: str | None = None

    mem = _mapping_cache.get(key)
    if mem is not None:
        _bundle, _ts, cached_shape, cached_full = mem
    else:
        persisted = cache.get(db)
        if persisted is not None:
            _bundle, cached_shape, cached_full = persisted

    if cached_shape is None:
        status: Literal[
            "unchanged", "stats_changed", "shape_changed", "no_cache"
        ] = "no_cache"
    elif cached_shape != shape_fp:
        status = "shape_changed"
    elif cached_full != full_fp:
        status = "stats_changed"
    else:
        status = "unchanged"

    return SchemaChangeReport(
        status=status,
        current_shape_fingerprint=shape_fp,
        current_full_fingerprint=full_fp,
        cached_shape_fingerprint=cached_shape,
        cached_full_fingerprint=cached_full,
    )


_IES_TO_Y_WORDS = {
    "companies", "cities", "categories", "stories", "bodies", "parties",
    "entries", "queries", "countries", "activities", "properties",
    "policies", "strategies", "histories", "industries", "libraries",
    "boundaries", "commodities", "entities", "identities", "priorities",
    "securities", "territories", "utilities", "vulnerabilities",
}

def _singularize(name: str) -> str:
    """Naive English singularization for collection-name-to-label conversion."""
    lower = name.lower()
    # "ies" → "y" only for known patterns (not "movies", "series", "species")
    if lower.endswith("ies") and len(name) > 4:
        if lower in _IES_TO_Y_WORDS:
            return name[:-3] + "y"
        # Heuristic: if the char before "ies" is a consonant pair or single consonant
        # and the result would be a short stem, prefer ies→y
        # Otherwise strip just the "s" to preserve the root (movies→movie)
        prefix = name[:-3]
        if len(prefix) >= 2 and prefix[-1].lower() not in "aeiou" and prefix[-2].lower() not in "aeiou":
            return prefix + "y"
        return name[:-1]
    if lower.endswith("ses") or lower.endswith("xes") or lower.endswith("zes") or lower.endswith("ches") or lower.endswith("shes"):
        return name[:-2]
    if lower.endswith("s") and not lower.endswith("ss") and not lower.endswith("us"):
        return name[:-1]
    return name


def _pascal_case(name: str) -> str:
    parts = re.split(r"[_\-\s]+", name)
    return "".join(p.capitalize() for p in parts if p)


def _collection_label(collection_name: str) -> str:
    """Infer a conceptual label from a collection name (e.g., 'users' -> 'User').

    Preserves existing PascalCase/camelCase capitalization when there are
    no word separators (underscores, hyphens, spaces).  Only applies
    capitalize-each-part logic when separators are present.
    """
    singular = _singularize(collection_name)
    if re.search(r"[_\-\s]", singular):
        return _pascal_case(singular)
    # Already a single token — preserve internal caps (e.g. EdrThreat),
    # just ensure the first letter is upper.
    return singular[0].upper() + singular[1:] if singular else singular


def classify_schema(db: StandardDatabase) -> str:
    """Fast heuristic: sample collections and classify as 'pg', 'lpg', 'hybrid', or 'unknown'.

    Strategy:
    - List all document collections and edge collections
    - For document collections: sample N docs, check if they have a common 'type'/'labels' field
      - If all docs have a type field with varying values -> LPG
      - If collection names match conceptual types (no type field) -> PG
    - For edge collections: check if they're dedicated or have a type/relation field
    - If mixed -> hybrid
    - If unclear -> unknown
    """
    try:
        all_cols = db.collections()
    except Exception:
        return "unknown"

    doc_cols = []
    edge_cols = []
    for c in all_cols:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if name.startswith("_"):
            continue
        if c.get("type") in (3, "edge"):
            edge_cols.append(name)
        else:
            doc_cols.append(name)

    if not doc_cols:
        return "unknown"

    type_field_names = {"type", "_type", "label", "labels", "kind", "entityType"}
    sample_size = 20

    doc_signals: list[str] = []
    for col_name in doc_cols:
        try:
            cursor = db.aql.execute(
                "FOR doc IN @@col LIMIT @n RETURN doc",
                bind_vars={"@col": col_name, "n": sample_size},
            )
            docs = list(cursor)
        except Exception:
            doc_signals.append("unknown")
            continue

        if not docs:
            doc_signals.append("unknown")
            continue

        found_type_field = None
        for tf in type_field_names:
            count = sum(1 for d in docs if isinstance(d, dict) and tf in d)
            if count >= len(docs) * 0.8:
                found_type_field = tf
                break

        if found_type_field:
            try:
                distinct_cursor = db.aql.execute(
                    f"FOR doc IN @@col COLLECT v = doc.`{found_type_field}` RETURN v",
                    bind_vars={"@col": col_name},
                )
                values = {str(v) for v in distinct_cursor if v is not None}
            except Exception:
                values = set()
            if len(values) > 1:
                doc_signals.append("lpg")
            else:
                doc_signals.append("pg")
        else:
            doc_signals.append("pg")

    edge_signals: list[str] = []
    edge_type_fields = {"type", "relation", "relType", "_type"}
    for col_name in edge_cols:
        try:
            cursor = db.aql.execute(
                "FOR doc IN @@col LIMIT @n RETURN doc",
                bind_vars={"@col": col_name, "n": sample_size},
            )
            docs = list(cursor)
        except Exception:
            edge_signals.append("unknown")
            continue

        if not docs:
            edge_signals.append("pg")
            continue

        found_type_field = None
        for tf in edge_type_fields:
            count = sum(1 for d in docs if isinstance(d, dict) and tf in d)
            if count >= len(docs) * 0.8:
                found_type_field = tf
                break

        if found_type_field:
            edge_signals.append("lpg")
        else:
            edge_signals.append("pg")

    all_signals = doc_signals + edge_signals
    meaningful = [s for s in all_signals if s != "unknown"]
    if not meaningful:
        return "unknown"

    pg_count = meaningful.count("pg")
    lpg_count = meaningful.count("lpg")

    if lpg_count == 0:
        return "pg"
    if pg_count == 0:
        return "lpg"
    return "hybrid"


# ---------------------------------------------------------------------------
# Data-quality profiling (sentinel detection, numeric-like strings)
# ---------------------------------------------------------------------------

# Case-insensitive string values commonly used as "null" sentinels in dirty data.
_SENTINEL_TOKENS: set[str] = {
    "NULL", "NONE", "NIL", "N/A", "NA", "UNKNOWN",
    "TBD", "TBA", "#N/A", "(NULL)",
}

# A sentinel candidate must occupy at least this share of the sampled values
# to be reported. Prevents isolated "-" or "" values from spuriously flagging
# legitimate columns.
_SENTINEL_MIN_SHARE = 0.02

# Numeric-like detection: share of non-sentinel strings that parse as numbers.
_NUMERIC_LIKE_MIN_SHARE = 0.8

# How many distinct sample values to keep per property for LLM context.
_SAMPLE_VALUES_KEEP = 4
_SAMPLE_VALUE_MAXLEN = 48


def _is_sentinel_token(s: str) -> bool:
    """Return True if ``s`` is a well-known null-sentinel string."""
    return s.strip().upper() in _SENTINEL_TOKENS


def _is_numeric_like(s: str) -> bool:
    """Return True if ``s`` parses as a number (int or float)."""
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _infer_value_type(val: Any) -> str:
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


def _profile_property_values(
    values: list[Any], total_docs: int,
) -> dict[str, Any]:
    """Compute type / sentinel / numeric-like / sample metadata for one field.

    ``values`` is the list of raw values observed for this field across the
    sampled documents (same length as the number of docs where the field
    was present). ``total_docs`` is the total number of sampled docs
    (so ``required`` can be derived).
    """
    if not values:
        return {"field": "", "type": "string"}

    type_counts: dict[str, int] = {}
    for v in values:
        t = _infer_value_type(v)
        type_counts[t] = type_counts.get(t, 0) + 1

    dominant_type = max(type_counts, key=type_counts.get)  # type: ignore[arg-type]

    sentinel_counts: dict[str, int] = {}
    non_sentinel_strings: list[str] = []
    for v in values:
        if not isinstance(v, str):
            continue
        if _is_sentinel_token(v):
            key = v.strip().upper()
            sentinel_counts[key] = sentinel_counts.get(key, 0) + 1
        else:
            non_sentinel_strings.append(v)

    min_count = max(1, int(_SENTINEL_MIN_SHARE * len(values)))
    sentinel_values = sorted(
        [k for k, n in sentinel_counts.items() if n >= min_count],
        key=lambda k: -sentinel_counts[k],
    )

    numeric_like = False
    if non_sentinel_strings:
        numeric_hits = sum(1 for s in non_sentinel_strings if _is_numeric_like(s))
        if numeric_hits / len(non_sentinel_strings) >= _NUMERIC_LIKE_MIN_SHARE:
            numeric_like = True

    sample_values: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        if _is_sentinel_token(v):
            continue
        key = v[:_SAMPLE_VALUE_MAXLEN]
        if key in seen:
            continue
        seen.add(key)
        sample_values.append(key)
        if len(sample_values) >= _SAMPLE_VALUES_KEEP:
            break

    out: dict[str, Any] = {"type": dominant_type}
    if sentinel_values:
        out["sentinelValues"] = sentinel_values
    if numeric_like:
        out["numericLike"] = True
    if sample_values:
        out["sampleValues"] = sample_values
    if total_docs and len(values) == total_docs:
        out["required"] = True
    return out


def _sample_properties(
    db: StandardDatabase, collection_name: str, sample_size: int = 50,
) -> list[dict[str, Any]]:
    """Sample docs and return enriched property profiles.

    Each entry contains the property ``name`` plus data-quality metadata:
    ``type``, ``sentinelValues`` (string sentinels like 'NULL'),
    ``numericLike`` (non-sentinel string values parse as numbers), and
    ``sampleValues`` (a few representative values for LLM context).
    """
    try:
        cursor = db.aql.execute(
            "FOR doc IN @@col LIMIT @n RETURN doc",
            bind_vars={"@col": collection_name, "n": sample_size},
        )
        docs = list(cursor)
    except Exception:
        return []

    if not docs:
        return []

    field_values: dict[str, list[Any]] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if key.startswith("_"):
                continue
            field_values.setdefault(key, []).append(val)

    out: list[dict[str, Any]] = []
    for name in sorted(field_values.keys()):
        prof = _profile_property_values(field_values[name], len(docs))
        entry: dict[str, Any] = {"name": name, "field": name, **prof}
        out.append(entry)
    return out


# Type-field candidate tiers.  Tier-1 names are unambiguous class discriminators
# (accepted on the 80%-coverage rule alone).  Tier-2 names are ambiguous — they
# are frequently used as scalar data fields too — so they additionally require a
# low cardinality ratio and class-like values.  See docs/schema_inference_bugfix_prd.md §4.1.
_TIER1_TYPE_FIELDS = ["type", "_type", "entityType"]
_TIER2_TYPE_FIELDS = ["label", "labels", "kind"]
_DOC_TYPE_FIELDS = _TIER1_TYPE_FIELDS + _TIER2_TYPE_FIELDS
_EDGE_TYPE_FIELDS = ["type", "relation", "relType", "_type", "label"]

_FILE_EXTENSION_SUFFIXES = (
    ".rst", ".md", ".pdf", ".asciidoc", ".txt", ".rtf",
    ".docx", ".html", ".json", ".xml", ".yaml", ".yml",
    ".ttl", ".owl",
)

# Tier-2 cardinality cap: reject if distinct-value count exceeds this.
_TIER2_ABSOLUTE_CARDINALITY_CAP = 50


def _looks_class_like(value: str) -> bool:
    """True when a candidate discriminator value plausibly names a class.

    A class-like name is a short alphanumeric token without dots, slashes, or
    whitespace, and does not end in a common file-extension suffix.
    """
    if not value or not value.strip():
        return False
    if any(c in value for c in (".", "/", " ", "\t")):
        return False
    lv = value.lower()
    if any(lv.endswith(suf) for suf in _FILE_EXTENSION_SUFFIXES):
        return False
    return True


def _detect_type_field(
    db: StandardDatabase,
    collection_name: str,
    candidates: list[str] | None = None,
    *,
    notes_sink: list[dict[str, Any]] | None = None,
) -> str | None:
    """Detect the type/label discriminator field in a collection, if any.

    A candidate must pass the existing 80% coverage rule.  Tier-1 candidates
    (``type``, ``_type``, ``entityType``) are accepted on coverage alone.
    Tier-2 candidates (``label``, ``labels``, ``kind``) must additionally:
      - have a distinct-value count ≤ ``max(50, int(0.5 * row_count))``, and
      - every sampled distinct value must be class-like (no dot, slash,
        whitespace, or file-extension suffix).

    When ``notes_sink`` is provided, each considered-but-rejected candidate is
    appended as ``{"field", "tier", "reason"}`` for observability.  Edge-side
    candidates (``relation``, ``relType``) are treated as tier-1.
    """
    if candidates is None:
        candidates = _DOC_TYPE_FIELDS
    try:
        cursor = db.aql.execute(
            "FOR doc IN @@col LIMIT @n RETURN doc",
            bind_vars={"@col": collection_name, "n": 20},
        )
        docs = list(cursor)
    except Exception:
        return None

    if not docs:
        return None

    def _tier(field: str) -> int:
        return 2 if field in _TIER2_TYPE_FIELDS else 1

    for tf in candidates:
        count = sum(1 for d in docs if isinstance(d, dict) and tf in d)
        if count < len(docs) * 0.8:
            if notes_sink is not None and count > 0:
                notes_sink.append({
                    "field": tf,
                    "tier": _tier(tf),
                    "reason": f"coverage {count}/{len(docs)} below 80% threshold",
                })
            continue

        if _tier(tf) == 1:
            return tf

        try:
            row_count = int(db.collection(collection_name).count() or 0)
        except Exception:
            row_count = len(docs)

        distinct_values = _type_field_values(db, collection_name, tf)
        distinct_count = len(distinct_values)

        cardinality_cap = max(_TIER2_ABSOLUTE_CARDINALITY_CAP, int(0.5 * row_count))
        if distinct_count > cardinality_cap:
            if notes_sink is not None:
                notes_sink.append({
                    "field": tf,
                    "tier": 2,
                    "reason": (
                        f"{distinct_count} distinct values over {row_count} rows "
                        f"exceeds cardinality cap {cardinality_cap}"
                    ),
                })
            continue

        non_class_like = [v for v in distinct_values if not _looks_class_like(v)]
        if non_class_like:
            sample = non_class_like[0]
            if notes_sink is not None:
                notes_sink.append({
                    "field": tf,
                    "tier": 2,
                    "reason": (
                        f"value {sample!r} is not class-like "
                        f"(contains '.', '/', whitespace, or a file extension)"
                    ),
                })
            continue

        return tf
    return None


def _type_field_values(db: StandardDatabase, collection_name: str, type_field: str) -> list[str]:
    """Get distinct values for a type field."""
    try:
        cursor = db.aql.execute(
            f"FOR doc IN @@col COLLECT val = doc.`{type_field}` RETURN val",
            bind_vars={"@col": collection_name},
        )
        vals: list[str] = []
        for v in cursor:
            if v is None:
                continue
            if isinstance(v, list):
                vals.extend(str(x) for x in v)
            else:
                vals.append(str(v))
        return sorted(set(vals))
    except Exception:
        return []


def _sample_properties_filtered(
    db: StandardDatabase,
    collection_name: str,
    type_field: str,
    type_value: str,
    sample_size: int = 50,
) -> list[dict[str, Any]]:
    """Sample documents matching a specific type value and return enriched
    property profiles (same shape as :func:`_sample_properties`).
    """
    skip_fields = {"_key", "_id", "_rev", "_from", "_to", type_field, "labels"}
    try:
        cursor = db.aql.execute(
            f"FOR doc IN @@col FILTER doc.`{type_field}` == @val LIMIT @n RETURN doc",
            bind_vars={"@col": collection_name, "val": type_value, "n": sample_size},
        )
        docs = list(cursor)
    except Exception:
        return []

    if not docs:
        return []

    field_values: dict[str, list[Any]] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if key in skip_fields:
                continue
            field_values.setdefault(key, []).append(val)

    out: list[dict[str, Any]] = []
    for name in sorted(field_values.keys()):
        prof = _profile_property_values(field_values[name], len(docs))
        entry: dict[str, Any] = {"name": name, "field": name, **prof}
        out.append(entry)
    return out


def _infer_lpg_edge_endpoints(
    db: StandardDatabase,
    edge_collection: str,
    type_field: str,
    type_value: str,
    entities_pm: dict[str, Any],
) -> tuple[str, str]:
    """Infer domain and range entity labels for a specific LPG edge type.

    Samples edges matching the type_value, resolves the _from/_to documents,
    and looks up their type to find the correct conceptual entity label.
    """
    col_type_map: dict[str, tuple[str, str]] = {}
    for label, pm in entities_pm.items():
        col = pm.get("collectionName", "")
        tf = pm.get("typeField")
        tv = pm.get("typeValue")
        if tf and tv:
            col_type_map[(col, tv)] = (label, tf)
        elif col:
            col_type_map[(col, "")] = (label, "")

    try:
        cursor = db.aql.execute(
            f"FOR e IN @@col FILTER e.`{type_field}` == @val LIMIT 10 RETURN {{f: e._from, t: e._to}}",
            bind_vars={"@col": edge_collection, "val": type_value},
        )
        samples = list(cursor)
    except Exception:
        return ("Any", "Any")

    if not samples:
        return ("Any", "Any")

    def _resolve_label(doc_id: str) -> str:
        col = doc_id.split("/")[0] if "/" in doc_id else ""
        if (col, "") in col_type_map:
            return col_type_map[(col, "")][0]
        try:
            doc = db.document(doc_id)
        except Exception:
            return "Any"
        if not isinstance(doc, dict):
            return "Any"
        for ent_label, pm in entities_pm.items():
            tf = pm.get("typeField")
            tv = pm.get("typeValue")
            if tf and doc.get(tf) == tv and pm.get("collectionName") == col:
                return ent_label
        return "Any"

    from_labels: set[str] = set()
    to_labels: set[str] = set()
    for s in samples:
        from_labels.add(_resolve_label(s["f"]))
        to_labels.add(_resolve_label(s["t"]))

    domain = sorted(from_labels - {"Any"})[0] if (from_labels - {"Any"}) else "Any"
    range_ = sorted(to_labels - {"Any"})[0] if (to_labels - {"Any"}) else "Any"
    return (domain, range_)


def _infer_dedicated_edge_endpoints(
    db: StandardDatabase,
    edge_collection: str,
    entities_pm: dict[str, Any],
) -> tuple[str, str]:
    """Infer domain/range for a dedicated (PG-style) edge collection.

    Samples ``_from``/``_to`` document IDs, extracts their collection names,
    and maps those to entity labels via the physical mapping.
    """
    col_to_label: dict[str, str] = {}
    for label, pm in entities_pm.items():
        col = pm.get("collectionName", "")
        if col:
            col_to_label[col] = label

    try:
        cursor = db.aql.execute(
            "FOR e IN @@col LIMIT 20 RETURN {f: e._from, t: e._to}",
            bind_vars={"@col": edge_collection},
        )
        samples = list(cursor)
    except Exception:
        return ("Any", "Any")

    if not samples:
        return ("Any", "Any")

    from_labels: set[str] = set()
    to_labels: set[str] = set()
    for s in samples:
        f_id = s.get("f", "")
        t_id = s.get("t", "")
        f_col = f_id.split("/")[0] if "/" in f_id else ""
        t_col = t_id.split("/")[0] if "/" in t_id else ""
        from_labels.add(col_to_label.get(f_col, "Any"))
        to_labels.add(col_to_label.get(t_col, "Any"))

    domain = sorted(from_labels - {"Any"})[0] if (from_labels - {"Any"}) else "Any"
    range_ = sorted(to_labels - {"Any"})[0] if (to_labels - {"Any"}) else "Any"
    return (domain, range_)


def _build_heuristic_mapping(db: StandardDatabase, schema_type: str) -> MappingBundle:
    """Build a MappingBundle from heuristics for PG or LPG schemas."""
    try:
        all_cols = db.collections()
    except Exception as exc:
        raise CoreError("Failed to list collections", code="INVALID_ARGUMENT") from exc

    doc_cols = []
    edge_cols = []
    for c in all_cols:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if name.startswith("_"):
            continue
        if c.get("type") in (3, "edge"):
            edge_cols.append(name)
        else:
            doc_cols.append(name)

    entities_cs: list[dict[str, Any]] = []
    entities_pm: dict[str, Any] = {}
    relationships_cs: list[dict[str, Any]] = []
    relationships_pm: dict[str, Any] = {}
    heuristic_notes: dict[str, dict[str, Any]] = {}

    def _props_to_pm(props: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Convert property list to physical-mapping properties dict.

        Preserves data-quality hints (sentinelValues, numericLike, sampleValues)
        emitted by :func:`_sample_properties` so downstream layers (NL prompts,
        result rendering) can surface them.
        """
        out: dict[str, dict[str, Any]] = {}
        for p in props:
            name = p.get("name", "")
            if not name:
                continue
            entry: dict[str, Any] = {
                "field": p.get("field", name),
                "type": p.get("type", "string"),
            }
            for k in ("sentinelValues", "numericLike", "sampleValues", "required"):
                if k in p:
                    entry[k] = p[k]
            out[name] = entry
        return out

    if schema_type == "pg":
        for col_name in doc_cols:
            label = _collection_label(col_name)
            props = _sample_properties(db, col_name)
            entities_cs.append({"name": label, "labels": [label], "properties": props})
            entities_pm[label] = {
                "style": "COLLECTION",
                "collectionName": col_name,
                "properties": _props_to_pm(props),
            }

        for col_name in edge_cols:
            rel_type = col_name.upper()
            props = _sample_properties(db, col_name)
            domain, range_ = _infer_dedicated_edge_endpoints(db, col_name, entities_pm)
            relationships_cs.append({
                "type": rel_type,
                "fromEntity": domain,
                "toEntity": range_,
                "properties": props,
            })
            relationships_pm[rel_type] = {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": col_name,
                "domain": domain,
                "range": range_,
                "properties": _props_to_pm(props),
            }
    elif schema_type in ("lpg", "hybrid"):
        for col_name in doc_cols:
            rejected: list[dict[str, Any]] = []
            type_field = _detect_type_field(db, col_name, notes_sink=rejected)
            if type_field:
                values = _type_field_values(db, col_name, type_field)
                for val in values:
                    label = _pascal_case(val)
                    props = _sample_properties_filtered(db, col_name, type_field, val)
                    entities_cs.append({"name": label, "labels": [label], "properties": props})
                    entities_pm[label] = {
                        "style": "LABEL",
                        "collectionName": col_name,
                        "typeField": type_field,
                        "typeValue": val,
                        "properties": _props_to_pm(props),
                    }
            else:
                label = _collection_label(col_name)
                props = _sample_properties(db, col_name)
                entities_cs.append({"name": label, "labels": [label], "properties": props})
                entities_pm[label] = {
                    "style": "COLLECTION",
                    "collectionName": col_name,
                    "properties": _props_to_pm(props),
                }
            if rejected or type_field is None:
                heuristic_notes[col_name] = {
                    "rejected_candidates": rejected,
                    "accepted_field": type_field,
                    "resolved_style": "LABEL" if type_field else "COLLECTION",
                }

        for col_name in edge_cols:
            detected_field = _detect_type_field(db, col_name, candidates=_EDGE_TYPE_FIELDS)

            if detected_field:
                values = _type_field_values(db, col_name, detected_field)
                for val in values:
                    domain, range_ = _infer_lpg_edge_endpoints(db, col_name, detected_field, val, entities_pm)
                    props = _sample_properties_filtered(db, col_name, detected_field, val)
                    relationships_cs.append({
                        "type": val,
                        "fromEntity": domain,
                        "toEntity": range_,
                        "properties": props,
                    })
                    relationships_pm[val] = {
                        "style": "GENERIC_WITH_TYPE",
                        "edgeCollectionName": col_name,
                        "typeField": detected_field,
                        "typeValue": val,
                        "properties": _props_to_pm(props),
                    }
            else:
                rel_type = col_name.upper()
                props = _sample_properties(db, col_name)
                domain, range_ = _infer_dedicated_edge_endpoints(db, col_name, entities_pm)
                relationships_cs.append({
                    "type": rel_type,
                    "fromEntity": domain,
                    "toEntity": range_,
                    "properties": props,
                })
                relationships_pm[rel_type] = {
                    "style": "DEDICATED_COLLECTION",
                    "edgeCollectionName": col_name,
                    "domain": domain,
                    "range": range_,
                    "properties": _props_to_pm(props),
                }

    _SKIP_INDEX_TYPES = {"primary", "edge"}
    col_indexes: dict[str, list[dict[str, Any]]] = {}
    for col_name in doc_cols + edge_cols:
        try:
            raw_indexes = db.collection(col_name).indexes()
            filtered = []
            for idx in raw_indexes:
                if not isinstance(idx, dict):
                    continue
                idx_type = idx.get("type", "")
                if idx_type in _SKIP_INDEX_TYPES:
                    continue
                filtered.append({
                    "type": idx_type,
                    "fields": idx.get("fields", []),
                    "unique": idx.get("unique", False),
                    "sparse": idx.get("sparse", False),
                    "name": idx.get("name", ""),
                })
            if filtered:
                col_indexes[col_name] = filtered
        except Exception:
            pass

    for pm_entry in entities_pm.values():
        col = pm_entry.get("collectionName", "")
        if col in col_indexes:
            pm_entry["indexes"] = col_indexes[col]

    for pm_entry in relationships_pm.values():
        col = pm_entry.get("edgeCollectionName", "")
        if col in col_indexes:
            pm_entry["indexes"] = col_indexes[col]

    conceptual_schema = {
        "entities": entities_cs,
        "relationships": relationships_cs,
    }
    physical_mapping = {
        "entities": entities_pm,
        "relationships": relationships_pm,
    }

    metadata: dict[str, Any] = {"source": "heuristic", "schemaType": schema_type}
    if heuristic_notes:
        metadata["heuristic_notes"] = heuristic_notes

    return MappingBundle(
        conceptual_schema=conceptual_schema,
        physical_mapping=physical_mapping,
        metadata=metadata,
        source=MappingSource(kind="heuristic", notes=f"Built from {schema_type} heuristic classification"),
    )


def acquire_mapping_bundle(db: StandardDatabase, *, include_owl: bool = False) -> MappingBundle:
    """Call arangodb-schema-analyzer to produce a MappingBundle from a live database.

    Uses AgenticSchemaAnalyzer with baseline inference (no LLM required).
    If arangodb-schema-analyzer is not installed, raises ImportError.
    """
    try:
        from schema_analyzer import AgenticSchemaAnalyzer, export_mapping
        from schema_analyzer.owl_export import export_conceptual_model_as_owl_turtle
    except ImportError:
        raise ImportError(
            "arangodb-schema-analyzer is not installed. "
            "Install it with: pip install arangodb-schema-analyzer  "
            "or: pip install -e ~/code/arango-schema-mapper"
        ) from None

    analyzer = AgenticSchemaAnalyzer()
    analysis_result = analyzer.analyze_physical_schema(db)

    analysis_dict = {
        "conceptualSchema": analysis_result.conceptual_schema,
        "physicalMapping": analysis_result.physical_mapping,
        "metadata": analysis_result.metadata.model_dump(by_alias=True),
    }

    export = export_mapping(analysis_dict, target="cypher")

    pm = export.get("physicalMapping", {})

    owl_turtle: str | None = None
    if include_owl:
        owl_turtle = export_conceptual_model_as_owl_turtle(analysis_dict)

    bundle = MappingBundle(
        conceptual_schema=export.get("conceptualSchema", {}),
        physical_mapping=pm,
        metadata=export.get("metadata", {}),
        owl_turtle=owl_turtle,
        source=MappingSource(
            kind="schema_analyzer_export",
            notes="Generated by arangodb-schema-analyzer (baseline)",
        ),
    )

    # Surface upstream reconciliation (issue #4 / PR-3): the analyzer's
    # reconcile pass may backfill collections the LLM missed. When the
    # LLM-path is used (not the baseline), emit an observability warning so
    # we retain visibility we used to get from running the backfill here.
    recon = bundle.metadata.get("reconciliation") if bundle.metadata else None
    if isinstance(recon, dict):
        backfilled = recon.get("backfilled_collections") or recon.get("backfilledCollections")
        if backfilled:
            logger.warning(
                "schema_analyzer backfilled %d collection(s) missing from the LLM mapping: %s",
                len(backfilled),
                sorted(backfilled) if isinstance(backfilled, list | tuple | set) else backfilled,
            )

    return bundle


# NOTE (PR-3, 2026-04-20): `_backfill_missing_collections` (~160 LOC) and
# `_fixup_dedicated_edges` (~80 LOC) used to live here. Both closed
# schema-analyzer capability gaps (issues #3 and #4) that shipped upstream
# in arangodb-schema-analyzer v0.2.0 and are now invariants of the
# `AgenticSchemaAnalyzer.analyze_physical_schema` pipeline:
#
#   - Multi-type edge detection → `GENERIC_WITH_TYPE` splits: handled by
#     upstream `analyzer._prepare_analysis` + `export_mapping`.
#   - Collection reconciliation / backfill for LLM omissions: handled by
#     upstream `schema_analyzer.reconcile.reconcile_physical_mapping`; its
#     summary surfaces in `metadata.reconciliation` (consumed above in
#     `acquire_mapping_bundle` to emit a warning when backfilling occurred).
#
# The golden-diff gate (`scripts/pr3_workaround_diff.py`) confirmed that
# upstream output is byte-identical with vs. without these post-processors
# across every fixture DB (movies_pg, movies_lpg, cypher_{pg,lpg,hybrid},
# northwind_test), so deleting them is a safe no-op on the output contract.


def compute_statistics(
    db: StandardDatabase,
    bundle: MappingBundle,
) -> dict[str, Any]:
    """Compute cardinality statistics for the physical model described by *bundle*.

    Returns a dict suitable for storing in ``MappingBundle.metadata["statistics"]``.
    Uses fast AQL ``LENGTH()`` for collection counts and derives per-relationship
    fan-out/fan-in metrics.
    """
    import datetime

    pm = bundle.physical_mapping
    cs = bundle.conceptual_schema
    entities = pm.get("entities", {}) if isinstance(pm.get("entities"), dict) else {}
    rels = pm.get("relationships", {}) if isinstance(pm.get("relationships"), dict) else {}

    cs_rels = cs.get("relationships", []) if isinstance(cs.get("relationships"), list) else []
    cs_rel_lookup: dict[str, tuple[str, str]] = {}
    for cr in cs_rels:
        if isinstance(cr, dict):
            cs_rel_lookup[cr.get("type", "")] = (
                cr.get("fromEntity", ""),
                cr.get("toEntity", ""),
            )

    col_counts: dict[str, dict[str, Any]] = {}
    entity_counts: dict[str, dict[str, Any]] = {}
    rel_stats: dict[str, dict[str, Any]] = {}

    seen_collections: set[str] = set()

    for label, emap in entities.items():
        if not isinstance(emap, dict):
            continue
        col_name = emap.get("collectionName", label)
        if col_name not in seen_collections:
            try:
                cursor = db.aql.execute(f"RETURN LENGTH({col_name})")
                count = next(cursor, 0)
            except Exception:
                count = 0
            col_counts[col_name] = {"count": count, "is_edge": False}
            seen_collections.add(col_name)

        style = emap.get("style", "COLLECTION")
        type_field = emap.get("typeField")
        type_value = emap.get("typeValue")
        if style in ("LABEL", "GENERIC_WITH_TYPE") and type_field and type_value:
            try:
                aql = (
                    f"FOR d IN {col_name} "
                    f"FILTER d.`{type_field}` == @tv "
                    f"COLLECT WITH COUNT INTO c RETURN c"
                )
                cursor = db.aql.execute(aql, bind_vars={"tv": type_value})
                entity_count = next(cursor, 0)
            except Exception:
                entity_count = col_counts.get(col_name, {}).get("count", 0)
        else:
            entity_count = col_counts.get(col_name, {}).get("count", 0)

        entity_counts[label] = {"estimated_count": entity_count}

    for rtype, rmap in rels.items():
        if not isinstance(rmap, dict):
            continue
        edge_col = rmap.get("edgeCollectionName", rtype)
        if not edge_col:
            continue

        if edge_col not in seen_collections:
            try:
                cursor = db.aql.execute(f"RETURN LENGTH({edge_col})")
                edge_count = next(cursor, 0)
            except Exception:
                edge_count = 0
            col_counts[edge_col] = {"count": edge_count, "is_edge": True}
            seen_collections.add(edge_col)

        style = rmap.get("style", "DEDICATED_COLLECTION")
        type_field = rmap.get("typeField")
        type_value = rmap.get("typeValue")

        if style == "GENERIC_WITH_TYPE" and type_field and type_value:
            try:
                aql = (
                    f"FOR e IN {edge_col} "
                    f"FILTER e.`{type_field}` == @tv "
                    f"COLLECT WITH COUNT INTO c RETURN c"
                )
                cursor = db.aql.execute(aql, bind_vars={"tv": type_value})
                edge_count = next(cursor, 0)
            except Exception:
                edge_count = col_counts.get(edge_col, {}).get("count", 0)
        else:
            edge_count = col_counts.get(edge_col, {}).get("count", 0)

        domain_label = rmap.get("domain", "") or ""
        range_label = rmap.get("range", "") or ""
        if (not domain_label or not range_label) and rtype in cs_rel_lookup:
            cs_from, cs_to = cs_rel_lookup[rtype]
            if not domain_label:
                domain_label = cs_from
            if not range_label:
                range_label = cs_to
        source_count = entity_counts.get(domain_label, {}).get("estimated_count", 0) if domain_label else 0
        target_count = entity_counts.get(range_label, {}).get("estimated_count", 0) if range_label else 0

        avg_out = (edge_count / source_count) if source_count > 0 else 0.0
        avg_in = (edge_count / target_count) if target_count > 0 else 0.0

        if source_count > 0 and target_count > 0:
            selectivity = edge_count / (source_count * target_count)
        else:
            selectivity = 1.0

        pattern = _classify_cardinality(avg_out, avg_in)

        rel_stats[rtype] = {
            "edge_count": edge_count,
            "source_count": source_count,
            "target_count": target_count,
            "avg_out_degree": round(avg_out, 2),
            "avg_in_degree": round(avg_in, 2),
            "cardinality_pattern": pattern,
            "selectivity": round(selectivity, 6),
        }

    return {
        "computed_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "collections": col_counts,
        "entities": entity_counts,
        "relationships": rel_stats,
    }


def _classify_cardinality(avg_out: float, avg_in: float) -> str:
    """Classify a relationship as 1:1, 1:N, N:1, or N:M based on average degrees."""
    out_is_one = avg_out <= 1.5
    in_is_one = avg_in <= 1.5
    if out_is_one and in_is_one:
        return "1:1"
    if not out_is_one and in_is_one:
        return "1:N"
    if out_is_one and not in_is_one:
        return "N:1"
    return "N:M"


def enrich_bundle_with_statistics(
    db: StandardDatabase,
    bundle: MappingBundle,
) -> MappingBundle:
    """Return a new MappingBundle with cardinality statistics in metadata."""
    stats = compute_statistics(db, bundle)
    new_meta = {**bundle.metadata, "statistics": stats}
    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=bundle.physical_mapping,
        metadata=new_meta,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


def get_mapping(
    db: StandardDatabase,
    *,
    strategy: str = "auto",
    include_owl: bool = False,
    cache_collection: str | None = DEFAULT_CACHE_COLLECTION,
    cache_key: str = DEFAULT_CACHE_KEY,
    force_refresh: bool = False,
) -> MappingBundle:
    """3-tier mapping acquisition with two-tier caching.

    Strategies
    ----------
    ``strategy="auto"`` (default): analyzer first (all schema types: PG, LPG,
    hybrid); heuristic fallback if the analyzer is not installed.

    ``strategy="analyzer"``: always call ``acquire_mapping_bundle()`` (raises
    if the analyzer is not installed).

    ``strategy="heuristic"``: never call the analyzer; build a best-effort
    mapping from ``classify_schema`` + heuristics.

    Caching
    -------
    Two fingerprints drive the cache decisions:

    - A *shape* fingerprint (collections + types + index digests). When it
      matches, the cached conceptual + physical mapping is reused.
    - A *full* fingerprint (shape + row counts). When it matches, cached
      cardinality statistics are reused too; when only it differs,
      statistics are recomputed on top of the cached mapping (the
      "stats-only refresh" fast path).

    Caches are layered:

    1. Process-local ``dict`` for same-session hits.
    2. Optional persistent ArangoDB collection (``cache_collection``) for
       cross-restart and cross-instance sharing. Pass ``cache_collection=None``
       to disable persistence (e.g. for read-only DB users).

    ``force_refresh=True`` bypasses both caches and rebuilds from scratch.
    """
    if strategy not in ("auto", "analyzer", "heuristic"):
        raise CoreError(
            f"Invalid strategy: {strategy!r}. Must be 'auto', 'analyzer', or 'heuristic'.",
            code="INVALID_ARGUMENT",
        )

    key = _cache_key(db)
    shape_fp = _shape_fingerprint(db)
    full_fp = _full_fingerprint(db)
    persistent = (
        ArangoSchemaCache(collection_name=cache_collection, cache_key=cache_key)
        if cache_collection
        else None
    )

    if not force_refresh and key:
        cached = _lookup_cache(db, key, persistent)
        if cached is not None:
            bundle, cached_shape, cached_full = cached
            if cached_shape == shape_fp:
                if _bundle_needs_reacquire(bundle):
                    # The cached bundle was built by the heuristic fallback
                    # because the analyzer was unavailable. Now that the
                    # analyzer is importable again, the cached entry is
                    # degraded — drop it and rebuild so the next operator
                    # does not inherit a known-bad mapping.
                    logger.info(
                        "Cached mapping for %s carries ANALYZER_NOT_INSTALLED "
                        "and analyzer is now available; rebuilding",
                        key,
                    )
                    _mapping_cache.pop(key, None)
                elif cached_full == full_fp:
                    logger.debug(
                        "Schema unchanged for %s; using cached mapping", key
                    )
                    return bundle
                else:
                    logger.info(
                        "Schema shape stable for %s; refreshing cardinality statistics only",
                        key,
                    )
                    bundle = _safe_refresh_statistics(db, bundle)
                    _save_cache(db, key, bundle, shape_fp, full_fp, persistent)
                    return bundle
            else:
                logger.info(
                    "Schema shape changed for %s; full re-introspection", key
                )

    bundle = _build_fresh_bundle(db, strategy=strategy, include_owl=include_owl)
    bundle = _safe_refresh_statistics(db, bundle)
    if key:
        _save_cache(db, key, bundle, shape_fp, full_fp, persistent)
    return bundle


def _build_fresh_bundle(
    db: StandardDatabase,
    *,
    strategy: str,
    include_owl: bool,
) -> MappingBundle:
    """Run the chosen acquisition strategy and attach OWL Turtle if requested."""
    if strategy == "analyzer":
        bundle = acquire_mapping_bundle(db, include_owl=include_owl)
    elif strategy == "heuristic":
        schema_type = classify_schema(db)
        bundle = _build_heuristic_mapping(
            db,
            schema_type if schema_type in ("pg", "lpg", "hybrid") else "lpg",
        )
    else:
        try:
            bundle = acquire_mapping_bundle(db, include_owl=include_owl)
        except ImportError:
            global _heuristic_fallback_counter
            _heuristic_fallback_counter += 1
            logger.warning(
                "Heuristic schema path used — install arangodb-schema-analyzer "
                "for accurate mappings on hybrid schemas.",
            )
            schema_type = classify_schema(db)
            bundle = _build_heuristic_mapping(
                db,
                schema_type if schema_type in ("pg", "lpg", "hybrid") else "lpg",
            )
            bundle = _attach_warning(
                bundle,
                code="ANALYZER_NOT_INSTALLED",
                message=(
                    "arangodb-schema-analyzer is not installed; the mapping "
                    "was built by the heuristic fallback and may misclassify "
                    "hybrid schemas."
                ),
                install_hint="pip install arangodb-schema-analyzer",
            )

    if include_owl and not bundle.owl_turtle:
        try:
            from arango_query_core.owl_turtle import mapping_to_turtle

            owl_turtle = mapping_to_turtle(bundle)
            bundle = MappingBundle(
                conceptual_schema=bundle.conceptual_schema,
                physical_mapping=bundle.physical_mapping,
                metadata=bundle.metadata,
                owl_turtle=owl_turtle,
                source=bundle.source,
            )
        except Exception:
            logger.warning(
                "Failed to generate OWL Turtle for heuristic mapping",
                exc_info=True,
            )
    return bundle


def _safe_refresh_statistics(
    db: StandardDatabase, bundle: MappingBundle
) -> MappingBundle:
    """Re-compute cardinality statistics without failing the caller.

    Statistics are a best-effort metadata enrichment: a failure here (e.g.
    permission denied on a typed edge COLLECT) must not prevent the caller
    from getting their mapping back.

    PR-3 (2026-04-20) short-circuit: when the analyzer has already populated
    ``metadata.statistics`` with an ``ok`` status (issue #2 / upstream
    ``schema_analyzer.statistics.compute_statistics`` shipped in v0.2.0),
    the upstream block is byte-identical to what the local
    :func:`compute_statistics` would produce and we skip the duplicate pass.
    The local implementation is retained as the fallback for (a) the
    heuristic tier whose bundles do not carry upstream stats, (b) the
    stats-only refresh path on a cached bundle after row counts drift,
    and (c) defensive rebuilds if upstream reports ``partial`` /
    ``skipped_no_db``.
    """
    meta = bundle.metadata or {}
    existing = meta.get("statistics")
    status = meta.get("statisticsStatus") or meta.get("statistics_status")
    if isinstance(existing, dict) and existing.get("relationships") and status == "ok":
        logger.debug(
            "Using analyzer-supplied metadata.statistics; skipping local recompute"
        )
        return bundle

    try:
        return enrich_bundle_with_statistics(db, bundle)
    except Exception:
        logger.warning(
            "Failed to compute cardinality statistics", exc_info=True
        )
        return bundle


def _lookup_cache(
    db: StandardDatabase,
    key: str,
    persistent: ArangoSchemaCache | None,
) -> tuple[MappingBundle, str, str] | None:
    """Check the in-memory cache first, then the persistent cache.

    Hydrates the in-memory cache from the persistent cache on hit so the
    next call in this process skips the DB roundtrip.
    """
    mem = _mapping_cache.get(key)
    if mem is not None:
        bundle, _ts, shape_fp, full_fp = mem
        return bundle, shape_fp, full_fp
    if persistent is None:
        return None
    hit = persistent.get(db)
    if hit is None:
        return None
    bundle, shape_fp, full_fp = hit
    _mapping_cache[key] = (bundle, time.time(), shape_fp, full_fp)
    return bundle, shape_fp, full_fp


def _save_cache(
    db: StandardDatabase,
    key: str,
    bundle: MappingBundle,
    shape_fp: str,
    full_fp: str,
    persistent: ArangoSchemaCache | None,
) -> None:
    """Write to both cache tiers. Persistent failure is non-fatal."""
    _mapping_cache[key] = (bundle, time.time(), shape_fp, full_fp)
    if persistent is not None:
        persistent.set(
            db,
            bundle=bundle,
            shape_fingerprint=shape_fp,
            full_fingerprint=full_fp,
        )


def invalidate_cache(
    db: StandardDatabase,
    *,
    cache_collection: str | None = DEFAULT_CACHE_COLLECTION,
    cache_key: str = DEFAULT_CACHE_KEY,
) -> None:
    """Drop both in-memory and persistent caches for this database.

    Use after a manual schema migration or when you want the next
    ``get_mapping()`` call to re-introspect unconditionally.
    """
    key = _cache_key(db)
    if key:
        _mapping_cache.pop(key, None)
    if cache_collection:
        ArangoSchemaCache(
            collection_name=cache_collection, cache_key=cache_key
        ).invalidate(db)
