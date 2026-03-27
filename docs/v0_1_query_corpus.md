# v0.1 Cypher query corpus (golden fixtures)

This corpus is the **test-first driver** for `arango-cypher-py` v0.1. It is intentionally small but representative, and it is organized as YAML fixtures under:

- `tests/fixtures/cases/*.yml`

Each YAML file contains a list of cases. Each case has:
- **`id`**: stable identifier (`C001`..)
- **`mapping_fixture`**: which schema-mapping fixture the case must run against (`pg`, `lpg`, `hybrid`)
- **`extensions_enabled`**: whether Arango extensions (`arango.*`, `CALL arango.*`) are allowed
- **`cypher`**: the Cypher query text
- **`params`** (optional): example bind parameters for execution/integration tests
- **`expected`**: placeholders for golden AQL and bind vars

Notes:
- For now, `expected.aql` is set to `null` in most cases. As we implement features, we will fill in golden AQL and bind vars for deterministic snapshot testing.
- The same corpus is designed to support:
  - **golden translation tests** (Cypher → AQL + bind vars)
  - **integration tests** (execute translated AQL against seeded DB fixtures)

---

## Fixture files

- `tests/fixtures/cases/basic_match_return.yml`
  - core `MATCH/WHERE/RETURN`, DISTINCT, simple functions, parameters
- `tests/fixtures/cases/relationships_one_hop.yml`
  - 1-hop relationship patterns (directed/undirected), relationship variable, `type(r)`
- `tests/fixtures/cases/with_and_aggregation.yml`
  - `WITH` pipeline semantics + aggregation + ordering + pagination
- `tests/fixtures/cases/nested_hybrid_extensions.yml`
  - nested-document dot-paths, hybrid mapping coverage, and one extension-function example

---

## Mapping fixtures (to be added next)

The corpus references three mapping modes:
- **`pg`**: types-as-collections (vertex collections per label, dedicated edge collections)
- **`lpg`**: generic collections with type discriminator fields (`LABEL` / `GENERIC_WITH_TYPE`)
- **`hybrid`**: mixed mapping styles per entity/relationship

Planned fixtures location:
- `tests/fixtures/mappings/pg.export.json`
- `tests/fixtures/mappings/lpg.export.json`
- `tests/fixtures/mappings/hybrid.export.json`

These should be generated via `arangodb-schema-analyzer` using `operation="export"` (and optionally `operation="owl"` for artifacts).

---

## Case index (IDs and intent)

### Basic MATCH/WHERE/RETURN (C001–C010)
These are minimal translation “smoke tests” and should be the first to go green.

### One-hop relationships (C011–C018)
These validate hybrid-safe relationship expansion and edge mapping resolution.

### WITH + aggregation (C019–C026)
These validate pipeline semantics and grouping.

### Nested docs + hybrid + extension example (C027–C030)
These validate dot-path behavior, a hybrid scenario, and the extension registry surface.

