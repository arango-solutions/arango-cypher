# Schema analyzer feature requests (handoff to `arango-schema-mapper`)

**Status (2026-04-20):** all seven issues are resolved upstream. See the [downstream migration note](https://github.com/ArthurKeen/arango-schema-mapper/pull/11) on the mapper. Local rollout is tracked in the PRD Changelog under Wave 4q.

Seven drafts, in priority order, to be filed as GitHub issues against [`ArthurKeen/arango-schema-mapper`](https://github.com/ArthurKeen/arango-schema-mapper). The first five close workarounds currently living in `arango_cypher/schema_acquire.py` that violate the "no workaround" policy in [`python_prd.md §5.2`](../python_prd.md#52-mapping-contract-we-will-consume). The sixth standardizes the cheap shape-fingerprint probe introduced by Wave 4m. The seventh revises the mapper PRD §3.13 so the full schema-change-detection pattern is sanctioned upstream.

Companion reference: [`WAVE_4M_ARCHITECTURE.md`](./WAVE_4M_ARCHITECTURE.md) — full design document for the Wave 4m pattern; referenced from issues #7 and #8.

| # | Title | Removes from arango-cypher-py | Upstream | Downstream |
|---|---|---|---|---|
| [01](./01-emit-vci-and-deduplicate-flags.md) | Emit `vci` and `deduplicate` flags on physical-mapping indexes | (none — fills a capability gap) | shipped in v0.2.0 | already consumed |
| [02](./02-emit-statistics-block.md) | Emit a `statistics` block with per-relationship cardinality and selectivity | `compute_statistics`, `_classify_cardinality`, `enrich_bundle_with_statistics` (~170 LOC) | shipped in v0.2.0 | **PR-3 (adopted; local retained as fallback)** |
| [03](./03-split-multi-type-edge-collections.md) | Detect multi-type edge collections and emit per-type `GENERIC_WITH_TYPE` entries | `_fixup_dedicated_edges` (~80 LOC) | shipped in v0.2.0 | **PR-3 (this wave — deleted)** |
| [04](./04-discover-collections-outside-named-graphs.md) | Discover all non-system collections, not just those in a named graph | `_backfill_missing_collections` (~160 LOC) | shipped in v0.2.0 | **PR-3 (this wave — deleted)** |
| [05](./05-align-property-key-naming.md) | Emit `field` (not `physicalFieldName`) and `edgeCollectionName` (not `collectionName`) | `_normalize_analyzer_pm`, `_normalize_props` (~30 LOC) | shipped in v0.2.0 | **PR-1 (this wave)** |
| [06](./06-cheap-shape-fingerprint-probe.md) | Add cheap `fingerprint_physical_shape(db)` / `fingerprint_physical_counts(db)` (no snapshot required) | `_shape_fingerprint`, `_full_fingerprint`, `_index_digest`, `_iter_user_collections` (~60 LOC) | shipped in v0.3.0 | **PR-2 (this wave)** |
| [07](./07-prd-3-13-schema-change-detection.md) | PRD §3.13 revision: two-fingerprint model + four-state change report + storage-agnostic cache | (docs only; sanctions the pattern) | shipped in v0.3.0 | n/a (docs-only upstream) |

**Total workaround + duplicated code removable once issues #01–#06 land:** ~500 LOC from `arango_cypher/schema_acquire.py`. The file shrinks from ~1350 lines to ~850 lines (heuristic tier stays as zero-dependency fallback, but `acquire_mapping_bundle` becomes a ~20-line pass-through). Issue #07 is a pure PRD amendment with no associated code delta.

## Filing checklist

After review:

```bash
gh issue create \
  --repo ArthurKeen/arango-schema-mapper \
  --title "$(head -1 docs/schema_analyzer_issues/01-emit-vci-and-deduplicate-flags.md | sed 's/^# //')" \
  --body-file docs/schema_analyzer_issues/01-emit-vci-and-deduplicate-flags.md \
  --label enhancement
# ...repeat for 02-07 (issue 07 uses labels: documentation, enhancement, prd)
```

These are all `enhancement` issues (additive changes to an existing stable contract); none are `bug` unless the analyzer owner considers collection-discovery-via-named-graph-only a bug against the documented contract.
