# Schema analyzer feature requests (handoff to `arango-schema-mapper`)

**Status (2026-04-27):** all eight issues are resolved upstream and adopted downstream. Issues #01–#07 shipped in mapper v0.2.0 / v0.3.0 (Wave 4q, see the [downstream migration note](https://github.com/ArthurKeen/arango-schema-mapper/pull/11)); issue #06 adoption for the NL multi-tenant guardrail landed 2026-04-23 (see the downstream note on `06-emit-tenant-scope-annotations.md`); issue #08 shipped in mapper v0.5.0 / v0.6.0 (PRs #15 / #16 / #17) and was adopted locally via `arango-cypher-py` PR #6 (`feat/adopt-sharding-profile`) on 2026-04-23.

Eight drafts, in priority order, filed as GitHub issues against [`ArthurKeen/arango-schema-mapper`](https://github.com/ArthurKeen/arango-schema-mapper). Issues #01–#05 close workarounds in `arango_cypher/schema_acquire.py` that violated the "no workaround" policy in [`python_prd.md §5.2`](../python_prd.md#52-mapping-contract-we-will-consume). #06 standardizes the cheap shape-fingerprint probe introduced by Wave 4m. #07 revises the mapper PRD §3.13 so the full schema-change-detection pattern is sanctioned upstream. #08 tracks the sharding-profile / multitenancy / shard-family metadata surfaced by upstream PRD §6.2 and adopted locally by the NL prompt builder and the tenant guardrail.

Companion reference: [`WAVE_4M_ARCHITECTURE.md`](./WAVE_4M_ARCHITECTURE.md) — full design document for the Wave 4m pattern; referenced from issues #7 and #8.

| # | Title | Removes from arango-cypher-py | Upstream | Downstream |
|---|---|---|---|---|
| [01](./01-emit-vci-and-deduplicate-flags.md) | Emit `vci` and `deduplicate` flags on physical-mapping indexes | (none — fills a capability gap) | shipped in v0.2.0 | already consumed |
| [02](./02-emit-statistics-block.md) | Emit a `statistics` block with per-relationship cardinality and selectivity | `compute_statistics`, `_classify_cardinality`, `enrich_bundle_with_statistics` (~170 LOC) | shipped in v0.2.0 | **PR-3 (adopted; local retained as fallback)** |
| [03](./03-split-multi-type-edge-collections.md) | Detect multi-type edge collections and emit per-type `GENERIC_WITH_TYPE` entries | `_fixup_dedicated_edges` (~80 LOC) | shipped in v0.2.0 | **PR-3 (Wave 4q — deleted)** |
| [04](./04-discover-collections-outside-named-graphs.md) | Discover all non-system collections, not just those in a named graph | `_backfill_missing_collections` (~160 LOC) | shipped in v0.2.0 | **PR-3 (Wave 4q — deleted)** |
| [05](./05-align-property-key-naming.md) | Emit `field` (not `physicalFieldName`) and `edgeCollectionName` (not `collectionName`) | `_normalize_analyzer_pm`, `_normalize_props` (~30 LOC) | shipped in v0.2.0 | **PR-1 (Wave 4q)** |
| [06](./06-emit-tenant-scope-annotations.md) | Emit `tenantScope.role` / `tenantScope.tenantField` annotations on entities | `nl2cypher/tenant_scope.py` becomes a back-compat fallback (~120 LOC of classification heuristic) | shipped in v0.6.0 | **adopted 2026-04-23** (downstream note added to the issue file) |
| [07](./07-prd-3-13-schema-change-detection.md) | PRD §3.13 revision: two-fingerprint model + four-state change report + storage-agnostic cache | (docs only; sanctions the pattern) | shipped in v0.3.0 | n/a (docs-only upstream) |
| [08](./08-emit-physical-layout-and-shard-topology.md) | Emit `metadata.shardingProfile`, `metadata.multitenancy`, `physicalMapping.shardFamilies` | Retires the D7 parallel-shard tracking and the local `_deployment_style_hint`/`_shard_families_block` heuristics | shardingProfile shipped in v0.5.0 (mapper PR #15); multitenancy + shardFamilies in v0.6.0 (mapper PRs #16/#17) | **adopted 2026-04-23** via arango-cypher-py PR #6 (`feat/adopt-sharding-profile`); issue file tracks per-block adoption |

**Total workaround + duplicated code retired by issues #01–#08 landing and being adopted:** ~500 LOC from `arango_cypher/schema_acquire.py` (Wave 4q) plus ~250 LOC of shard-family / multi-tenant heuristics retired in PR #6. The remaining local code in this space is intentional: fallbacks for the heuristic-tier mapping path, and the NL-prompt rendering of the now-first-class metadata blocks (`nl2cypher._core._deployment_style_hint`, `_shard_families_block`, `tenant_guardrail.multitenancy_physical_enforcement`).

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
