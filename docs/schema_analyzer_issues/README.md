# Schema analyzer feature requests (handoff to `arango-schema-mapper`)

Seven drafts, in priority order, to be filed as GitHub issues against [`ArthurKeen/arango-schema-mapper`](https://github.com/ArthurKeen/arango-schema-mapper). The first five close workarounds currently living in `arango_cypher/schema_acquire.py` that violate the "no workaround" policy in [`python_prd.md §5.2`](../python_prd.md#52-mapping-contract-we-will-consume). The sixth standardizes the cheap shape-fingerprint probe introduced by Wave 4m. The seventh revises the mapper PRD §3.13 so the full schema-change-detection pattern is sanctioned upstream.

Companion reference: [`WAVE_4M_ARCHITECTURE.md`](./WAVE_4M_ARCHITECTURE.md) — full design document for the Wave 4m pattern; referenced from issues #7 and #8.

| # | Title | Removes from arango-cypher-py | Blocker for |
|---|---|---|---|
| [01](./01-emit-vci-and-deduplicate-flags.md) | Emit `vci` and `deduplicate` flags on physical-mapping indexes | (none — fills a capability gap) | Index-aware traversal `OPTIONS.indexHint` emission for ArangoDB 3.12+ VCIs |
| [02](./02-emit-statistics-block.md) | Emit a `statistics` block with per-relationship cardinality and selectivity | `compute_statistics`, `_classify_cardinality`, `enrich_bundle_with_statistics` (~170 LOC) | NL-to-Cypher prompt enrichment, cost-based query planning |
| [03](./03-split-multi-type-edge-collections.md) | Detect multi-type edge collections and emit per-type `GENERIC_WITH_TYPE` entries | `_fixup_dedicated_edges` (~80 LOC) | Correct AQL style selection for LPG-style edge collections |
| [04](./04-discover-collections-outside-named-graphs.md) | Discover all non-system collections, not just those in a named graph | `_backfill_missing_collections` (~160 LOC) | Any deployment whose schema has collections outside named-graph definitions |
| [05](./05-align-property-key-naming.md) | Emit `field` (not `physicalFieldName`) and `edgeCollectionName` (not `collectionName`) | `_normalize_analyzer_pm`, `_normalize_props` (~30 LOC) | Any downstream consumer of the mapping export |
| [06](./06-cheap-shape-fingerprint-probe.md) | Add cheap `fingerprint_physical_shape(db)` / `fingerprint_physical_counts(db)` (no snapshot required) | `_shape_fingerprint`, `_full_fingerprint`, `_index_digest`, `_iter_user_collections` (~60 LOC) | Any long-running consumer that wants "has schema changed?" without a full snapshot |
| [07](./07-prd-3-13-schema-change-detection.md) | PRD §3.13 revision: two-fingerprint model + four-state change report + storage-agnostic cache | (docs only; sanctions the pattern) | Long-term alignment; informs optional follow-up issues for a generic `SchemaCache` protocol and `describe_schema_change` top-level API |

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
