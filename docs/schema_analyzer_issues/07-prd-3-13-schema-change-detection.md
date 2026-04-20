# PRD §3.13 revision: two-fingerprint model + four-state change report + storage-agnostic cache

**Labels:** `documentation`, `enhancement`, `prd`

## Background

PRD §3.13 ("Temporal provenance, runs, and schema-change lineage") and §4.1 ("Caching") currently describe schema-change detection in terms of a **single** fingerprint — `fingerprint_physical_schema(snapshot)` — used both as a cache key and as a change-detection signal. §3.13.3 describes the product behavior as "when physical fingerprint changes, flag prior analysis as stale or auto-queue re-run."

Production experience from [`arango-cypher-py` Wave 4m](./WAVE_4M_ARCHITECTURE.md) shows this model is insufficient for long-running consumers in three ways:

1. **The existing fingerprint requires the expensive input to compute.** It hashes a snapshot, so answering "has the schema changed?" requires running `snapshot_physical_schema(db)` — exactly the work the cache is supposed to avoid. Issue #7 proposes a cheap `fingerprint_physical_shape(db)` to fill this gap.
2. **The existing fingerprint conflates shape with content.** Any document insert flips it via downstream count-sensitive statistics or optional sample-doc inclusion, invalidating mappings that are structurally still valid. Callers end up doing full re-introspection on every data-volume change.
3. **The existing fingerprint is auto-gen-sensitive.** ArangoDB can assign different auto-generated index `name` / `id` values to semantically-equivalent indexes across restarts, producing false-positive change reports.

The PRD should be updated to sanction the two-fingerprint model, the four-state change report, the stats-only refresh product behavior, and storage-agnostic caching — independent of whether issue #7 or any caller-side code ships.

## Proposed PRD change

### §3.13.3 — Change detection and diff (replace section)

**Current text:**

> | **Trigger re-analysis** | When physical fingerprint changes, flag prior analysis as **stale** or auto-queue re-run (product policy). |
> | **Diff** | Compare two `AnalysisResult` payloads (or OWL exports): added/removed/changed entities, relationships, and mapping styles — analogous to AOE `get_ontology_diff` but scoped to **schema-derived conceptual models**. |

**Proposed text:**

> #### **3.13.3. Change detection and diff (target)**
>
> Schema-analyzer distinguishes between **physical shape** changes (which invalidate the conceptual schema and physical mapping) and **data-volume** changes (which invalidate only derived statistics). Consumers MUST be able to determine which, if either, has occurred **without** running a full snapshot.
>
> | Capability | Requirement |
> |---|---|
> | **Shape fingerprint** | Cheap `fingerprint_physical_shape(db)` probe (cf. issue #7) — hashes only the collection set, per-collection type, and per-collection sorted index digests `(type, fields, unique, sparse, vci, deduplicate)` with `name` and `id` excluded. Stable under ordinary writes. |
> | **Counts fingerprint** | Cheap `fingerprint_physical_counts(db)` probe — shape fingerprint concatenated with per-collection `count()`. Changes whenever either the shape or any collection's row count changes. |
> | **Change-state contract** | Callers comparing current fingerprints against cached fingerprints MUST be able to derive a four-valued status: `unchanged` (both match), `stats_changed` (shape matches, counts differ), `shape_changed` (shape differs), `no_cache` (no prior fingerprint recorded). |
> | **Stats-only refresh** | When status is `stats_changed`, the library MUST preserve the cached `conceptual_schema` and `physical_mapping` and recompute only derived statistics (cf. issue #3). Analyzer invocation, OWL regeneration, type-discriminator detection, and sample extraction MUST be skipped on this path. |
> | **Trigger re-analysis** | When status is `shape_changed`, flag prior analysis as **stale** or auto-queue re-run (product policy). |
> | **Diff** | Compare two `AnalysisResult` payloads (or OWL exports): added/removed/changed entities, relationships, and mapping styles — analogous to AOE `get_ontology_diff` but scoped to schema-derived conceptual models. |
>
> **Implementation notes (non-normative):**
>
> - The existing `fingerprint_physical_schema(snapshot)` (§4.1) remains the key for a full-snapshot cache. The new shape and counts fingerprints in §3.13.3 are probes, not replacements.
> - Auto-generated index identifiers (`name`, `id`) MUST NOT contribute to the shape fingerprint; ArangoDB may assign different values to semantically-equivalent indexes across restarts.
> - Transient failures on individual collections (e.g. `indexes()` or `count()` raises) MUST degrade gracefully — the fingerprint function contributes a sentinel rather than propagating the exception.

### §4.1 — Caching (revise bullets)

**Current text:**

> - Filesystem-based cache keyed by physical schema fingerprint (SHA-256 of normalized snapshot)
> - Configurable TTL (default 86400s / 24h)
> - `generated_at` timestamp excluded from fingerprint for stability
>
> **Implementation**: `cache.py` (`AnalysisCache` / `FilesystemCache`).

**Proposed text:**

> - Default cache is filesystem-based, keyed by physical schema fingerprint (SHA-256 of normalized snapshot).
> - The cache interface (`get` / `set` / `invalidate`) is storage-agnostic; deployments MAY substitute alternate backends (collection-backed, Redis, object store, etc.). Any substitute MUST be tolerant to missing entries, corrupt documents, and stale cache-document schema versions.
> - When using a database-resident cache (e.g. an ArangoDB collection in the same database being analyzed), the cache collection MUST be excluded from shape-fingerprint computation to prevent self-invalidation on its own writes.
> - Cache documents SHOULD carry a `cache_schema_version` field so loading code can refuse-and-discard documents whose shape it no longer understands.
> - Configurable TTL (default 86400s / 24h).
> - `generated_at` timestamp excluded from fingerprint for stability.
>
> **Implementation**: `cache.py` (`AnalysisCache` / `FilesystemCache`). Alternate backends, if any, live alongside.

## Acceptance criteria

1. PRD §3.13.3 is revised to include the two-fingerprint model, the four-state change-status contract, and the stats-only refresh product behavior.
2. PRD §4.1 acknowledges storage-agnostic caching, the self-exclusion requirement for database-resident caches, and the `cache_schema_version` field.
3. The PRD change log (if any) records the revision.
4. No code change is required by this issue; it is purely a PRD amendment. (Issue #7 covers the fingerprint-function code; optional follow-up issues may cover a generic `SchemaCache` protocol and a `SchemaChangeReport` top-level API.)

## Non-goals

- No change to `fingerprint_physical_schema(snapshot)` semantics (remains the correct choice when a snapshot is already in hand, e.g. for deduping stored snapshots).
- No mandatory implementation of `describe_schema_change(db, cache)` as a top-level library function. Callers may continue to compose the two fingerprint probes with their own cache. The PRD sanctions the pattern; a generic implementation is a future issue.
- No mandatory collection-backed cache implementation in `schema_analyzer`. The PRD permits alternate backends; whether the mapper ships one is a separate decision.

## References

- **Architecture reference:** [`WAVE_4M_ARCHITECTURE.md`](./WAVE_4M_ARCHITECTURE.md) — full design doc for the caller-side pattern, including empirical lessons worth preserving.
- **Related issue:** #7 — the upstream-portable function pair (`fingerprint_physical_shape(db)`, `fingerprint_physical_counts(db)`).
- **Downstream consumer:** `arango-cypher-py` Wave 4m (`schema_acquire.py`, `schema_cache.py`, `tests/test_schema_change_detection.py`).
