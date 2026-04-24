# Emit `tenantScope` annotations on physical-mapping entities

**Labels:** `enhancement`, `export-contract`, `multi-tenant`

## Background

Multi-tenant ArangoDB graphs root every business object at a `Tenant`
document, but the *way* a given collection is bound to a tenant varies
across the schema:

* **Denormalised reference** — most fact-style collections carry the
  Tenant `_key` directly on every document under a column whose name
  follows a deterministic convention (`TENANT_ID`, `tenant_id`,
  `tenantId`, `tenant_key`, …). A query against such a collection can
  scope itself with a single indexed equality and avoid a graph
  traversal: `FOR d IN Device FILTER d.TENANT_ID == @tenantKey`.
* **Traversal-only scoping** — some collections have no inline tenant
  column but are reachable from `Tenant` via the conceptual
  relationship graph. The only way to scope queries against them is
  to bind `:Tenant` and traverse to the target.
* **Global / metadata** — reference tables (`Cve`, `AppVersion`,
  `Library` in some schemas, …) are intentionally cross-tenant: every
  tenant sees the same canonical rows. A query against such a
  collection MUST NOT carry a tenant filter — adding one would
  return zero rows.

Today the analyzer emits no signal that distinguishes these three
roles. Every downstream consumer (transpilers, NL→Cypher pipelines,
agentic clients, dashboards) re-derives the classification from the
mapping, with each consumer landing on subtly different heuristics.
The first such re-derivation in `arango-cypher-py` (see
[`arango_cypher/nl2cypher/tenant_scope.py`][cypher-py-impl]) makes the
case for hoisting it upstream as a first-class part of the export
contract:

* the discovery rules are deterministic and depend only on the
  conceptual schema + physical mapping,
* the alternative is N copies of the same heuristic that drift
  out of sync,
* downstream consumers benefit immediately — the
  NL→Cypher tenant guardrail can give the LLM correct per-entity
  guidance instead of the v1 hardcoded "every collection has a
  TENANT_ID" anti-guidance.

## Current behavior

`schema_analyzer/mapping.py::PhysicalMapping` carries `style`,
`collectionName`, `typeField`, `typeValue`, `indexes`, and
`properties` on each entity. None of these expose tenant role.

`schema_analyzer/tool_contract/v1/response.schema.json` (the
authoritative export contract) has the same gap.

## Desired behavior

Add an optional `tenantScope` block to each entry under
`physicalMapping.entities`:

```jsonc
"physicalMapping": {
  "entities": {
    "Tenant": {
      "style": "COLLECTION",
      "collectionName": "Tenant",
      "tenantScope": { "role": "tenant_root" }
    },
    "Device": {
      "style": "COLLECTION",
      "collectionName": "Device",
      "tenantScope": {
        "role": "tenant_scoped",
        "tenantField": "TENANT_ID",
        "tenantEntity": "Tenant"
      }
    },
    "Library": {
      "style": "COLLECTION",
      "collectionName": "Library",
      "tenantScope": {
        "role": "tenant_scoped",
        "tenantEntity": "Tenant"
        // no tenantField — scope only via traversal
      }
    },
    "Cve": {
      "style": "COLLECTION",
      "collectionName": "Cve",
      "tenantScope": { "role": "global" }
    }
  }
}
```

### Schema additions

| Field | Type | Conditional | Notes |
|---|---|---|---|
| `tenantScope.role` | enum: `tenant_root` / `tenant_scoped` / `global` | required when `tenantScope` present | Exactly one entity may carry `tenant_root`. |
| `tenantScope.tenantField` | string | optional, only meaningful when `role == "tenant_scoped"` | Exact field name as it appears on documents (e.g. `"TENANT_ID"`). |
| `tenantScope.tenantEntity` | string | required when `role == "tenant_scoped"` | Name of the `tenant_root` entity for cross-checks. |

`tenantScope` itself is OPTIONAL on every entity — single-tenant
graphs and pre-tenant exports remain valid against the v1 contract.

### Detection rules (deterministic, no LLM)

1. **`tenant_root`** — the conceptual entity whose name matches one
   of `("Tenant",)` (extensible via env var or analyzer config). At
   most one per mapping.
2. **`tenant_scoped` with `tenantField`** — the entity has a
   conceptual property whose name matches the configured regex
   (default: `^tenant[_-]?(id|key)$`, case-insensitive). Field
   captured as-discovered (preserve original casing).
3. **`tenant_scoped` without `tenantField`** — the entity has no
   matching property, but is reachable from the tenant root within N
   hops (default 5) over the conceptual relationship graph (BFS,
   undirected). The only way to scope queries to it is via
   traversal.
4. **`global`** — neither of the above. Treated as
   tenant-independent reference data.

### Configuration

* `SCHEMA_ANALYZER_TENANT_FIELD_REGEX` — env var; overrides the
  default regex for installations that follow a non-standard naming
  convention (e.g. `customer_id` for an ISV that calls tenants
  "customers"). Bad pattern → silent fallback to default.
* `SCHEMA_ANALYZER_TENANT_ROOT_NAMES` — env var; comma-separated
  list of entity names that count as tenant roots. Default
  `"Tenant"`.
* `SCHEMA_ANALYZER_TENANT_SCOPE_MAX_HOPS` — env var; BFS depth cap.
  Default 5.

## Where to wire it in

`schema_analyzer/analyzer.py` runs reconciliation as the last step
before validation. Tenant-scope annotation should run *after*
reconciliation (so backfilled entities also get classified) and
*before* validation (so the response schema check covers them).

A new module `schema_analyzer/tenant_scope.py` should expose:

```python
def annotate_tenant_scope(
    data: dict[str, Any],
    *,
    tenant_root_names: tuple[str, ...] = ("Tenant",),
    tenant_field_regex: re.Pattern[str] | None = None,
    max_hops: int = 5,
) -> dict[str, Any] | None:
    """Annotate physicalMapping.entities[*].tenantScope in place.

    Returns a summary dict suitable for `metadata.tenantScopeReport`,
    or None when no entity was annotated (no Tenant root detected).
    """
```

The summary block in `metadata.tenantScopeReport` is symmetric to
`metadata.reconciliation` (issue #5) and gives operators a
single-glance view of what got classified:

```jsonc
"tenantScopeReport": {
  "tenantEntity": "Tenant",
  "denormScopedCount": 12,
  "traversalScopedCount": 4,
  "globalCount": 7,
  "tenantFieldRegex": "^tenant[_-]?(id|key)$",
  "discovery": {
    "fromExplicitAnnotation": 0,
    "fromDenormFieldHeuristic": 12,
    "fromTraversalReachability": 4
  }
}
```

When the LLM (or a future operator-supplied input) supplies an
explicit `tenantScope` annotation on an entity, it MUST win — the
annotator only fills in missing entries. This is the same precedence
contract the reconciliation step uses for entities and is what lets
operators override edge cases (e.g. a vestigial `TENANT_ID` column
on a collection that is in fact intentionally global).

## Acceptance criteria

* New module + tests covering each of the four role-derivation paths,
  including the GLOBAL-only case (no edge to Tenant, no denorm field)
  and the override-precedence case (explicit annotation wins).
* `response.schema.json` updated with the optional `tenantScope`
  property. Existing tests against the schema continue to pass
  (annotation is optional).
* `metadata.tenantScopeReport` is emitted only when at least one
  entity was classified — symmetric to the `reconciliation` block.
* No behavioural change for single-tenant exports: when no tenant
  root is detected, no `tenantScope` field is added to any entity
  and no `tenantScopeReport` is emitted.
* `CHANGELOG.md` notes the new export field under "Unreleased".

## Downstream impact

* `arango-cypher-py` removes its local `tenant_scope.py` shim and
  consumes `tenantScope.role` / `tenantScope.tenantField` directly.
  The local shim becomes a back-compat fallback for mappings
  produced by analyzers older than this PR.
* Documentation snippets that show example mapping JSON in
  consumers' READMEs no longer need the disclaimer about deriving
  tenant roles.

## Downstream adoption (2026-04-23)

Shipped upstream in `arangodb-schema-analyzer v0.6.0` (mapper PR #17);
floor in `arango-cypher-py` is now `>=0.6.1,<0.7` across the
`[analyzer]`, `[service]`, and `[dev]` extras.

Adoption in `arango-cypher-py`:

* `arango_cypher/nl2cypher/tenant_scope.py::analyze_tenant_scope` reads
  `tenantScope.role` (`TENANT_ROOT` / `TENANT_SCOPED` / `GLOBAL`) and
  `tenantScope.tenantField` directly from each `entity` in the
  physical mapping, and extends its discovery regex with the
  `metadata.multitenancy.tenantKey[]` list from the upstream
  classification. The manifest built from these annotations is the
  primary authority for the per-label classification that
  `check_tenant_scope` uses when deciding whether a denorm-field
  filter satisfies the scope.
* The local classification heuristic is retained as a back-compat
  fallback for mappings produced by analyzers older than `0.6.x`
  and for the heuristic mapping tier (which has no upstream
  signals at all), in line with the
  [`python_prd.md §5.2`](../python_prd.md#52-mapping-contract-we-will-consume)
  "no workaround" policy — the workaround is the heuristic *tier*,
  not a heuristic living on top of the analyzer output.
* The adjacent signal `metadata.multitenancy.physicalEnforcement`
  (PRD §6.2 bullet 4) is consumed by
  `tenant_guardrail.multitenancy_physical_enforcement` to label
  `TenantScopeViolation.physical_enforcement`, so log sinks can
  distinguish data-leak-class violations (`False`, application
  convention only) from translation-quality nits (`True`, physical
  storage guarantees isolation regardless of the generated query).

The adoption closes the drift risk flagged in `docs/python_prd.md`
§5.2 where every downstream consumer was re-deriving the
classification from the mapping.

[cypher-py-impl]: https://github.com/ArthurKeen/arango-cypher-py/blob/main/arango_cypher/nl2cypher/tenant_scope.py
