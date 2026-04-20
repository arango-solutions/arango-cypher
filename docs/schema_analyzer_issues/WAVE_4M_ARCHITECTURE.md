# Wave 4m — schema-change detection architecture reference

Companion document for upstream issues [#7 (cheap shape fingerprint)](./06-cheap-shape-fingerprint-probe.md) and [#8 (PRD §3.13 revision)](./07-prd-3-13-schema-change-detection.md) against [`ArthurKeen/arango-schema-mapper`](https://github.com/ArthurKeen/arango-schema-mapper).

This document captures the full design of what `arango-cypher-py` built in Wave 4m so the mapper maintainer can see the complete pattern — not just the fingerprint functions that issue #7 proposes to port upstream. It is intentionally more than either issue so it can inform future upstream work without re-deriving the rationale each time.

## 1. What Wave 4m delivered

A four-part change-detection + fast-refresh system on top of `get_mapping(db)`:

| Layer | Component | Owned by |
|---|---|---|
| Fingerprint | `_shape_fingerprint(db)` — stable under writes | **Upstream candidate** (issue #7) |
| Fingerprint | `_full_fingerprint(db)` — shape + row counts | **Upstream candidate** (issue #7) |
| Probe API | `describe_schema_change(db) -> SchemaChangeReport` | Caller-side (or upstream, per issue #8) |
| Cache tier 1 | `_mapping_cache: dict[db_name, (bundle, ts, shape_fp, full_fp)]` — process-local | Caller-side |
| Cache tier 2 | `ArangoSchemaCache` — persists the bundle in a dedicated collection | Caller-side (substitutable) |
| Fast path | Stats-only refresh — when `_shape_fingerprint` matches but `_full_fingerprint` differs, preserve `ConceptualSchema` + `PhysicalMapping` and recompute only statistics | Caller-side |

Everything else (OWL emission, bundle construction, analyzer/heuristic fallback selection) was untouched.

## 2. The two-fingerprint model

### Why one fingerprint is insufficient

The existing `fingerprint_physical_schema(snapshot)` hashes the full snapshot and is used today as a `FilesystemCache` key. It has three problems when used as a change-detection probe:

1. **Requires the expensive input to compute.** You must run `snapshot_physical_schema(db)` first, which is what you were trying to avoid.
2. **Conflates shape with content.** Every document insert flips the hash via sample-doc drift (when samples are on) or via count-sensitive downstream statistics, invalidating cached mappings that are in fact still structurally valid.
3. **Auto-generated index identifiers destabilize it.** ArangoDB sometimes assigns different `name` / `id` values to semantically-equivalent indexes across restarts or rebuilds, causing false-positive change reports.

### Shape fingerprint (stable under writes)

Hashes the subset of physical state that, if changed, would invalidate the conceptual or physical mapping:

- Collection set (name, system-collection filtered, cache-collection filtered).
- Per-collection type (`doc` vs `edge`).
- Per-collection sorted list of *index digests*, where each digest is `(type, fields, unique, sparse, vci, deduplicate)` — notably **excluding** `name` and `id`.

Does **not** read row counts, samples, or any AQL result. Stable under ordinary writes.

### Counts fingerprint (shape + per-collection counts)

`sha256(shape_fingerprint || concat(collection:count for collection in user_collections))`.

Changes whenever either the shape or any collection's `count()` changes. This is what the stats-only refresh path keys off.

## 3. The four-status change report

```python
@dataclass(frozen=True)
class SchemaChangeReport:
    status: Literal["unchanged", "stats_changed", "shape_changed", "no_cache"]
    cached_shape_fingerprint: str | None
    cached_full_fingerprint: str | None
    current_shape_fingerprint: str
    current_full_fingerprint: str
    cache_age_seconds: float | None
```

Why four values and not two (`changed` / `unchanged`):

| Status | Meaning | Caller action |
|---|---|---|
| `unchanged` | shape fp matches, full fp matches | Skip refresh entirely. Prompt builders, downstream views, and client-facing schema exports can reuse their cached artifacts. |
| `stats_changed` | shape fp matches, full fp differs | Refresh statistics only. Preserve conceptual + physical mapping; recompute cardinality + selectivity. Cost scales with `len(user_collections)` AQL calls, not with full introspection. |
| `shape_changed` | shape fp differs | Full re-introspection (analyzer or heuristic). Invalidate every downstream artifact. |
| `no_cache` | nothing cached for this DB name | Full introspection and populate cache. Distinguished from `shape_changed` so callers can distinguish "service just started" from "schema actually drifted". |

The three-way distinction between `unchanged` / `stats_changed` / `shape_changed` is the architecturally interesting part — without it, every cardinality drift forces a full mapping rebuild, and the whole cache stops paying for itself on write-heavy databases.

## 4. Stats-only refresh path

When `describe_schema_change(db).status == "stats_changed"`:

1. Keep the cached `ConceptualSchema`.
2. Keep the cached `PhysicalMapping`.
3. Recompute `statistics` by calling `compute_statistics(db, mapping)` (today local to `arango-cypher-py`; proposed for upstream in issue #3).
4. Replace `bundle.metadata["statistics"]` with the new block.
5. Write the refreshed bundle back to both cache tiers under the new `_full_fingerprint` (same `_shape_fingerprint`).

This path is ~5–50× cheaper than full introspection on a moderately-sized database (tens of collections, a few million documents) because it skips analyzer invocation, OWL regeneration, type-discriminator detection, and sample-doc extraction.

## 5. Two-tier caching

### Tier 1 — in-memory (`_mapping_cache`)

Process-local `dict[db_name, (bundle, ts, shape_fp, full_fp)]` with a TTL. Handles the hot path within a single process. This tier is straightforward and not a candidate for upstream motion.

### Tier 2 — persistent (`ArangoSchemaCache`)

Round-trips a `MappingBundle` through JSON into a dedicated ArangoDB collection (`arango_cypher_schema_cache` by default). The document stores:

- The serialized bundle.
- `shape_fingerprint`, `full_fingerprint`.
- `cache_schema_version` (for forward-compatible schema evolution on the cache document itself).

Why collection-backed rather than the mapper's existing `FilesystemCache`:

- **Containers.** `FilesystemCache` requires a shared persistent volume across replicas. Collection-backed survives restarts and is naturally shared across replicas that point at the same ArangoDB.
- **Data locality.** The cache lives in the database the mapping describes. No separate storage config.
- **Bootstrap simplicity.** No mount point, no path permissions, no eviction daemon.

### The self-exclusion requirement

The cache collection is itself a user collection. Without special handling, inserting the first cache entry would perturb `_shape_fingerprint` on the next round, invalidating itself. The solution: `_iter_user_collections(db)` unconditionally filters out the configured cache-collection name. Any upstream adoption must replicate this — it is a correctness invariant, not an optimization.

### Substitutability

`ArangoSchemaCache` conforms to a minimal interface (`get(db) -> (bundle, shape_fp, full_fp) | None`, `set(...)`, `invalidate(db)`). Any cache implementation — filesystem, Redis, memcached, S3 — can substitute. The upstream port (issue #8) should preserve this substitutability in the PRD and not bake in a specific backend.

## 6. Empirical lessons worth preserving upstream

1. **Exclude `name` and `id` from index digests.** Different ArangoDB builds assign different auto-generated names to semantically-equivalent indexes, causing false-positive `shape_changed` reports.
2. **Filter the cache collection from `_iter_user_collections`.** See §5.
3. **`fingerprint_physical_schema(snapshot)` is a sink, not a probe.** It answers "is this snapshot the same as the one I already have?" — useful for deduping a stored snapshot against a new one, useless for deciding whether to take the new snapshot in the first place.
4. **Sample-doc inclusion destabilizes fingerprints.** The mapper already knows this (`include_samples=False` is the default for `fingerprint_physical_schema`). The shape-only fingerprint should not even have the option — it has no basis for sampling.
5. **Degrade gracefully on transient failures.** A collection whose `indexes()` call raises should contribute a sentinel to the hash rather than propagating the exception. Same for `count()`. This matters for services that call `describe_schema_change` on request handlers where a transient Arango hiccup must not take down the endpoint.
6. **Version the persisted cache document.** `cache_schema_version` lets the cache-loading code refuse-and-discard documents whose shape it no longer understands, without corrupting them or forcing a manual purge.

## 7. Upstream porting plan

| Issue | Scope | Status |
|---|---|---|
| **#7** | Port `fingerprint_physical_shape(db)` and `fingerprint_physical_counts(db)` into `schema_analyzer/snapshot.py`. Minimal, additive. | **Filed** (issue #7, 2026-04-18) |
| **#8** | Revise mapper PRD §3.13 to recognize the shape / counts distinction, the four-state change report, stats-only refresh, and storage-agnostic caching. | **Filed** (issue #8) |
| (future) | Optional: add a `SchemaChangeReport` dataclass + `describe_schema_change(db, cache)` top-level function to the mapper, once the PRD sanctions it. | Deferred — caller-side is fine today. |
| (future) | Optional: introduce a generic `SchemaCache` protocol in the mapper so both `FilesystemCache` and a future collection-backed cache share a single interface. | Deferred — no second consumer yet. |

### What stays caller-side

- `ArangoSchemaCache` itself (the concrete collection-backed cache). This is a `MappingBundle`-shaped cache, and `MappingBundle` is an `arango_query_core` type, not a mapper type. Pushing it upstream would require inverting the dependency.
- The stats-only-refresh *implementation* (needs `compute_statistics`, which is local to `arango-cypher-py` until issue #3 lands).
- The specific TTL and replacement policy for the in-memory tier. Those are deployment policies, not library concerns.

## 8. References

- `arango_cypher/schema_acquire.py` — `_shape_fingerprint`, `_full_fingerprint`, `_index_digest`, `_iter_user_collections`, `SchemaChangeReport`, `describe_schema_change`, `get_mapping`, `invalidate_cache`.
- `arango_cypher/schema_cache.py` — `ArangoSchemaCache`, bundle serialization.
- `tests/test_schema_change_detection.py` — 23 tests covering fingerprint invariants, cache round-trip, stats-only refresh, graceful degradation.
- `tests/test_schema_acquire.py::TestSchemaFingerprints` — shape vs. counts separation tests.
- `docs/python_prd.md` (Wave 4m entry) — product-level requirements and acceptance criteria.
- `README.md` (Schema change detection section) — user-facing API documentation.
