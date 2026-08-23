# Add a cheap `fingerprint_physical_shape(db)` probe (no snapshot required)

**Labels:** `enhancement`, `performance`, `export-contract`

## Background

`snapshot.py::fingerprint_physical_schema(snapshot)` computes a stable hash over a snapshot that has *already* been produced by `snapshot_physical_schema(db)`. That's the right fingerprint when you already have a snapshot in hand — but it forces every consumer that wants to answer "has the schema changed?" to first run the expensive full snapshot, defeating the purpose of the cache layer (`cache.py::FilesystemCache`) that is keyed on the fingerprint.

Downstream consumers (including long-running services, MCP servers, and `arango-cypher-py`'s `get_mapping()` layer) need a cheaper probe that answers "would running `snapshot_physical_schema` + `build_baseline_mapping` return materially different output?" *without* having to run either one.

`arango-cypher-py` has already implemented this probe locally in `arango_cypher/schema_acquire.py` (`_shape_fingerprint`, `_full_fingerprint`, `_index_digest`, `_iter_user_collections`) because the functionality is missing upstream. That ~60 LOC should move into `schema_analyzer` so every consumer benefits and there is one canonical definition of "shape."

## Current behavior

- `fingerprint_physical_schema(snapshot)` — exists, but requires a precomputed snapshot.
- No `db`-keyed variant exists. To detect a schema-shape change, a caller must:
  1. Call `snapshot_physical_schema(db)` (full walk over every collection, every index, type-discriminator detection via AQL `COLLECT`).
  2. Call `fingerprint_physical_schema(snapshot)`.
  3. Compare to the previously stored hash.

For a DB with 50 collections, step (1) is tens to hundreds of milliseconds and up to several seconds when index metadata is large, which makes it unusable as an "is it worth refreshing?" probe on a hot path.

## Desired behavior

Introduce two new top-level functions in `snapshot.py`:

```python
def fingerprint_physical_shape(db: StandardDatabase) -> str:
    """Cheap shape-only fingerprint.

    Hashes collection names, collection types (doc vs edge), and index digests
    only. Does NOT read row counts and does NOT run AQL. Stable under ordinary
    writes (INSERT / UPDATE / REMOVE). Changes whenever a collection is added
    or dropped, a collection type changes, or an index is added, dropped, or
    has its (type, fields, unique, sparse, vci, deduplicate) changed.
    """


def fingerprint_physical_counts(db: StandardDatabase) -> str:
    """Shape fingerprint + per-collection row counts.

    Hash of `fingerprint_physical_shape(db)` concatenated with each
    collection's `count()`. Changes whenever either the shape or any
    collection's row count changes. Used by callers that want to refresh
    *statistics* (but not the mapping itself) when only counts drift.
    """
```

Both functions read *only* python-arango primitives — `db.collections()`, `col.indexes()`, `col.count()`. No AQL, no samples, no analyzer logic.

### Required behavior

1. `fingerprint_physical_shape(db)` MUST be stable when the caller writes to a collection (adds / updates / removes documents) without changing any index.
2. `fingerprint_physical_shape(db)` MUST change when:
   - a collection is created or dropped (user collection only — `_`-prefixed system collections are excluded);
   - a collection changes type between `document` and `edge`;
   - an index is added, dropped, or any of its identity-carrying fields changes (`type`, `fields`, `unique`, `sparse`, and — per issue #2 once it lands — `vci`, `deduplicate`).
3. `fingerprint_physical_counts(db)` MUST change when `fingerprint_physical_shape(db)` changes OR any collection's `count()` changes.
4. Both functions MUST exclude system collections (`name.startswith("_")`). They SHOULD accept an optional `exclude_collections: Iterable[str] | None = None` so callers can exclude their own cache / bookkeeping collections (e.g. `arango_cypher_schema_cache`) and prevent fingerprint self-perturbation.
5. Index digests MUST be insensitive to auto-generated `name` and `id` fields, because ArangoDB can assign different names to semantically-equivalent indexes across reboots / rebuilds.
6. The functions MUST be tolerant to transient failures on individual collections: an unreadable index list or a failed `count()` SHOULD degrade gracefully (sentinel value in the hash) rather than raise.

## Acceptance criteria

1. Given a clean snapshot, `fingerprint_physical_schema(snapshot_physical_schema(db))` and `fingerprint_physical_shape(db)` produce outputs that are both stable across repeated calls (different hex values are fine — they hash different inputs).
2. `fingerprint_physical_shape(db)` remains constant when only document writes occur between calls.
3. `fingerprint_physical_shape(db)` changes when a persistent index is added to any collection.
4. `fingerprint_physical_counts(db)` changes when documents are inserted into any collection even if `fingerprint_physical_shape(db)` does not.
5. With `exclude_collections={"my_cache"}`, both fingerprints ignore drops / inserts on `my_cache`.
6. Unit tests in `tests/test_snapshot_fingerprints.py` (new file) covering each of the above against a mocked `StandardDatabase`.
7. Integration test against live ArangoDB (gated behind the existing `ARANGO_LIVE_TESTS=1` or equivalent env var) verifying stability under writes.

## Non-goals

- No change to the existing `fingerprint_physical_schema(snapshot)` signature or semantics.
- No change to `FilesystemCache` keying.
- No statistics (covered separately by issue #3).
- No schema diff (the caller can compare two `fingerprint_physical_shape` outputs as opaque strings; a *structured* diff is a separate future enhancement).

## Impact on downstream consumers

- **`arango-cypher-py`**: deletes `_shape_fingerprint`, `_full_fingerprint`, `_index_digest`, `_iter_user_collections` from `schema_acquire.py` (~60 LOC) and replaces the call sites with `fingerprint_physical_shape(db, exclude_collections={DEFAULT_CACHE_COLLECTION})` and `fingerprint_physical_counts(...)`. `SchemaChangeReport` logic is unchanged.
- **`schema_analyzer.mcp_server`**: gains a cheap "has it changed?" endpoint without reimplementing the probe.
- **Any other consumer** that wants to cache mapping output across process restarts gets a stable, reusable cache key.

## Implementation sketch

```python
# snapshot.py


def _stable_index_digest(idx: dict[str, Any]) -> str:
    if idx.get("type") == "primary":
        return ""
    fields = idx.get("fields")
    fields_part = ",".join(str(f) for f in fields) if isinstance(fields, list) else ""
    return "|".join(
        [
            str(idx.get("type") or ""),
            fields_part,
            "u" if idx.get("unique") else "",
            "s" if idx.get("sparse") else "",
            "v" if idx.get("vci") else "",
            "d" if idx.get("deduplicate") is False else "",
        ]
    )


def _iter_user_collections(
    db: StandardDatabase,
    *,
    exclude_collections: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    exclude = set(exclude_collections or ())
    try:
        cols = db.collections()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for c in cols:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if not isinstance(name, str) or name.startswith("_"):
            continue
        if name in exclude:
            continue
        out.append(c)
    out.sort(key=lambda x: x.get("name", ""))
    return out


def fingerprint_physical_shape(
    db: StandardDatabase,
    *,
    exclude_collections: Iterable[str] | None = None,
) -> str:
    parts: list[str] = []
    for c in _iter_user_collections(db, exclude_collections=exclude_collections):
        name = c.get("name", "")
        col_type = "edge" if c.get("type") in (3, "edge") else "doc"
        try:
            idxs = db.collection(name).indexes() or []
        except Exception:
            idxs = []
        digests = sorted(d for d in (_stable_index_digest(i) for i in idxs) if d)
        parts.append(f"{name}:{col_type}:" + ";".join(digests))
    raw = f"{db.name}|" + "|".join(parts)
    return sha256_hex(raw)


def fingerprint_physical_counts(
    db: StandardDatabase,
    *,
    exclude_collections: Iterable[str] | None = None,
) -> str:
    shape = fingerprint_physical_shape(db, exclude_collections=exclude_collections)
    parts: list[str] = []
    for c in _iter_user_collections(db, exclude_collections=exclude_collections):
        name = c.get("name", "")
        try:
            count = db.collection(name).count()
        except Exception:
            count = -1
        parts.append(f"{name}:{count}")
    raw = f"{shape}|" + "|".join(parts)
    return sha256_hex(raw)
```

Both functions live alongside the existing `fingerprint_physical_schema` in `snapshot.py` and should be re-exported from `schema_analyzer/__init__.py`. The implementation is ported verbatim from `arango-cypher-py`'s `schema_acquire.py` where it has been running in production since Wave 4m (tests in `tests/test_schema_acquire.py::TestSchemaFingerprints` and `tests/test_schema_change_detection.py` cover the invariants listed above).

## Relationship to other issues

- Depends on #2 (emit `vci` / `deduplicate`) only in that the shape fingerprint SHOULD incorporate those flags once available. If #6 lands first, the digest is still correct for today's flag set and becomes automatically tighter once #2 adds the flags.
- Independent of #3, #4, #5.
