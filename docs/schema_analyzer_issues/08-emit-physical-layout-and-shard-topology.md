# Physical layout, shard topology, and multi-tenant characterization

**Labels:** `tracking`, `multi-tenant`, `cluster-awareness`

> **Status (2026-04-21):** Most of what this issue originally asked for is
> already specified upstream in `arango-schema-mapper` PRD §6.2 (commit
> [`b3d4744`][upstream-prd-commit], 2026-04-20) — "Roadmap entries for
> VCI, sharding, and multitenancy detection". This document is now a
> downstream-consumer view of that upstream spec plus one small addition
> (`shardFamilies`) that wasn't covered. **Do not file as a separate
> GitHub issue** — the upstream PRD is the spec of record; this file
> tracks which downstream workarounds each upstream feature retires.

---

## 1) Alignment with upstream PRD §6.2

The upstream spec breaks the work into three deliverables, each emitted
as a `metadata.*` block on the `AnalysisResult`:

| Upstream feature | Emits | Retires locally | Status |
|---|---|---|---|
| **Sharding-pattern detection** (PRD §6.2 bullet 3) | `metadata.shardingProfile` with style ∈ {`OneShard`, `SmartGraph`, `DisjointSmartGraph`, `SatelliteGraph`, `Sharded`} plus per-collection evidence (`shardKeys`, `numberOfShards`, `smartGraphAttribute`, `isDisjoint`, `replicationFactor`). | Multi-tenant PRD §3 MT-0 (would have added `physicalLayout` locally in `schema_acquire.py`). | Spec'd upstream, not implemented. **Phase 1 PR on mapper side.** |
| **Multitenancy detection** (PRD §6.2 bullet 4) | `metadata.multitenancy` with style ∈ {`none`, `disjoint_smartgraph`, `shard_key`, `discriminator_field`, `collection_per_tenant`, `database_per_tenant`}, `tenantKey`, `tenantKeyCollections`, `physicalEnforcement`, `evidence`. | `nl2cypher/tenant_scope.py` local heuristic becomes pure fallback (same pattern as existing `tenantScope` from issue #06). | Spec'd upstream, not implemented. **Phase 2 PR on mapper side.** |
| **VCI detection** (PRD §6.2 bullet 2) | First-class `VCI` mapping style + schema-level duplication detection. | Nothing local today (we already consume per-index `vci=true`). | Orthogonal — not blocking multi-tenant. Follow-up. |

### 1.1 What local code adopts each block

**`metadata.shardingProfile`:**

- `multitenant_prd.md` §3 work-package MT-0 (`physicalLayout.kind` per
  collection) is fully satisfied by
  `shardingProfile.members[*].{kind, smartGraphAttribute, isDisjoint, graphName, shardKeys, numberOfShards, replicationFactor}`.
- `multitenant_prd.md` §7 (EXPLAIN-plan validator, Layer 5) reads
  `shardingProfile.style` once at session start to decide whether to
  enforce per-plan shard-key checks (OneShard ⇒ no-op, Sharded ⇒
  required).
- No new code in `schema_acquire.py`; the interim "compute locally"
  alternative from `multitenant_prd.md` §3 is skipped.

**`metadata.multitenancy`:**

- `arango_cypher/nl2cypher/tenant_scope.py::analyze_tenant_scope`
  consumes `multitenancy.tenantKey` directly instead of re-running its
  denorm-field regex. Local regex heuristic retained as fallback for
  analyzer bundles older than this release, same contract as #06.
- `arango_cypher/nl2cypher/tenant_guardrail.py` reads
  `multitenancy.physicalEnforcement` to decide whether to escalate
  guardrail failures to refusals (enforced ⇒ hard refuse; convention
  ⇒ warn + retry).

---

## 2) New addition: `shardFamilies` (the one piece not covered upstream)

**Not in PRD §6.2.** This is what this tracking doc proposes as a small
addition to upstream — drafted as a PRD bullet in PR form.

### 2.1 Motivation

Hybrid schemas commonly duplicate structurally-identical collections
keyed on a per-repo / per-stream / per-upstream-source dimension:

```
IBEX_Documents     → IBEXDocument
MAROCCHINO_Documents → MAROCCHINODocument
MOR1KX_Documents   → MOR1KXDocument
OR1200_Documents   → OR1200Document
```

All four share the same property set, differ only in an implicit
discriminator (here, `repo`), and collectively form one logical entity
from the LLM / NL→Cypher perspective. The mapper today lists them as
four independent entities; downstream consumers that want to reason
about the family (NL prompt builder; UI mapping panel; future
visualizers) have to re-derive it with their own heuristic.

This is **not multi-tenancy** (it's a per-source data-organization
pattern, not a per-customer isolation boundary). It deserves its own
block so `multitenancy.style == "none"` remains correct while
`shardFamilies` captures the structural duplication.

### 2.2 Proposed emission — `physicalMapping.shardFamilies[]`

```jsonc
"physicalMapping": {
  "entities": { ... },
  "shardFamilies": [
    {
      "name": "Document",
      "suffix": "Document",
      "discriminator": { "source": "collection_prefix", "field": "repo" },
      "sharedProperties": ["doc_version", "label", "path", "source_commit"],
      "members": [
        { "entity": "IBEXDocument",       "collectionName": "IBEX_Documents",       "discriminatorValue": "IBEX" },
        { "entity": "MAROCCHINODocument", "collectionName": "MAROCCHINO_Documents", "discriminatorValue": "MAROCCHINO" },
        { "entity": "MOR1KXDocument",     "collectionName": "MOR1KX_Documents",     "discriminatorValue": "MOR1KX" },
        { "entity": "OR1200Document",     "collectionName": "OR1200_Documents",     "discriminatorValue": "OR1200" }
      ]
    }
  ]
}
```

### 2.3 Detection rules (deterministic, no LLM)

1. Bucket entities by `sha256(sorted(property_names))`. Skip buckets of
   size < 2.
2. Within each bucket, find the longest common suffix of the conceptual
   entity names that is ≥ 4 characters and ends on a capital-letter
   boundary. Skip buckets with no qualifying suffix.
3. Extract the prefix as the discriminator candidate (`IBEX`,
   `MAROCCHINO`, …). Optionally probe for a matching field on the
   collection (default `repo`, configurable via
   `SCHEMA_ANALYZER_SHARD_DISCRIMINATOR_FIELDS`). When found, record
   `discriminator.source = "field"` + `discriminator.field`. When not
   found but prefix is consistent, record
   `discriminator.source = "collection_prefix"`.
4. Emit one family entry per confirmed bucket. Families of 1 are never
   emitted.

### 2.4 Downstream impact

- `nl2cypher/_core.py::_build_schema_summary` renders families as
  grouped sections in the LLM prompt, with an explicit hint that a
  repo-agnostic question must UNION across members. Directly attacks
  the class-of-error that produced the
  "`no entity mapping for AppVersion`" / wrong-shard picks reported
  in `docs/schema_inference_bugfix_prd.md` candidate D7.
- UI mapping panel can collapse a family into a single row with a
  member count badge.

### 2.5 How this gets filed

As a small addition to `arango-schema-mapper/docs/PRD.md` §6.2 — a
fourth bullet alongside VCI / sharding / multitenancy, following the
same style. See Phase 0.2 in the downstream implementation plan.

---

## 3) Cross-reference: what the downstream-PRD trail says

- `multitenant_prd.md` §3 (Schema mapper requirements) — now
  satisfied by upstream `shardingProfile` + `multitenancy`.
  MT-0 work-package is marked **superseded** once Phase 1 ships.
- `schema_inference_bugfix_prd.md` candidate D7 (parallel-shard
  detection) — routed to §2 of this document.
- `schema_inference_bugfix_prd.md` candidate D8 (property existence
  check in resolver) — stays local in
  `arango_cypher/nl2cypher/entity_resolution.py`; not a mapper
  concern.

---

[upstream-prd-commit]: https://github.com/ArthurKeen/arango-schema-mapper/commit/b3d4744
