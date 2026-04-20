# Implementation Plan — arango-cypher-py v0.2 → v0.4+

Date: 2026-04-13
Derived from: [`python_prd.md`](./python_prd.md) §10 (unified roadmap)
Supersedes: [`requirements_plan.md`](./requirements_plan.md) (historical context only)
Sub-agent prompts: [`agent_prompts.md`](./agent_prompts.md) (ready-to-use prompts for parallel implementation)

---

## How to read this document

- **Work packages (WP)** are the unit of planning. Each is 1-2 weeks of work.
- **Dependencies** are explicit: a WP cannot start until its prerequisites are complete.
- **Critical path** items are marked with ⚡ — these gate downstream work and should be prioritized.
- **Files** lists the primary source files affected (not exhaustive).
- Each WP references the PRD section it implements.

---

## Dependency graph (v0.2)

```
WP-1 CREATE clause ⚡
  ├── WP-5 TCK setup seeding ⚡
  │     └── WP-6 TCK Match coverage
  └── WP-7 Movies dataset expansion

WP-2 Aggregation + built-in functions
  └── WP-6 TCK Match coverage

WP-3 Schema analyzer integration ⚡
  └── WP-4 CLI

WP-4 CLI
  (standalone, depends on WP-3 for `mapping` subcommand)

WP-5 TCK harness improvements ⚡
  └── WP-6 TCK Match coverage

WP-8 UI: parameter binding + query history
  (standalone)
```

---

## v0.2 — Write clauses + aggregation completeness + schema analyzer

### WP-1: CREATE clause (translator) ⚡

**PRD**: §6.4 clauses, §8.2 (TCK blocker), §10 v0.2
**Priority**: Critical path — blocks TCK and dataset expansion
**Estimate**: 1-2 weeks

#### Scope

Implement `CREATE` translation for:
- Node creation: `CREATE (n:Person {name: "Alice", age: 30})`
- Relationship creation: `CREATE (a)-[:KNOWS {since: 2020}]->(b)`
- Multi-element creation: `CREATE (a:Person {name: "Alice"}), (b:Person {name: "Bob"}), (a)-[:KNOWS]->(b)`
- `CREATE` after `MATCH` (using bound variables)

AQL lowering strategy:
- Node → `INSERT {_key: ..., ...props} INTO @@collection`
- Relationship → `INSERT {_from: a._id, _to: b._id, ...props} INTO @@edgeCollection`
- For LPG-style entities, inject `typeField`/`typeValue` into the inserted document
- Bind variables for all values (no string interpolation)

#### Deliverables

1. `_compile_create()` method in `translate_v0.py`
2. Remove or gate the "updating clauses rejected" guard (line ~112-114) to allow `CREATE`
3. Golden tests in `tests/fixtures/cases/create.yml`:
   - Node creation (PG + LPG)
   - Relationship creation (dedicated + generic edge collection)
   - CREATE after MATCH
   - Multi-element CREATE
   - Inline property map with parameters
4. Integration test: CREATE + read-back in `tests/integration/`

#### Files

- `arango_cypher/translate_v0.py` — new `_compile_create()`, modify clause dispatch
- `arango_query_core/aql.py` — may need `INSERT` rendering helpers
- `tests/fixtures/cases/create.yml` — new golden fixture
- `tests/test_translate_create_goldens.py` — new golden test runner
- `tests/integration/test_create_smoke.py` — new integration test

#### Design decisions needed

- **PRD §14**: Should `CREATE` be full write-clause support or only enough for TCK setup? **Recommendation**: implement `CREATE` fully (nodes + relationships + properties + MATCH-then-CREATE). Skip `SET`, `MERGE`, `DELETE` for now — they have different AQL patterns and are not needed for TCK setup.
- **Return semantics**: `CREATE (n:Person {name: "Alice"}) RETURN n` — should the transpiler return the inserted document? **Recommendation**: yes, use `INSERT ... INTO ... LET created = NEW RETURN created`.

---

### WP-2: Aggregation in RETURN + built-in functions

**PRD**: §6.4 aggregation, §6.4 built-in functions, §10 v0.2
**Priority**: High — needed for TCK and general usability
**Estimate**: 1 week
**Dependencies**: None (parallel with WP-1)

#### Scope

**Aggregation in RETURN** (currently only works in `WITH`):
- `MATCH (n:Person) RETURN COUNT(n)` → `COLLECT WITH COUNT INTO __count RETURN __count`
- `MATCH (n:Person) RETURN n.city, COUNT(n)` → `COLLECT city = n.city WITH COUNT INTO __count RETURN {city, count: __count}`
- `RETURN DISTINCT` with multiple projection items (currently single-item only)

**Built-in functions** (add to `_compile_function_invocation`):
- `type(r)` → edge's relationship type field (mapping-dependent)
- `id(n)` → `n._id`
- `labels(n)` → mapping-dependent: for COLLECTION-style return `[collectionName]`; for LABEL-style return `n[typeField]` (may be array or wrap in array)
- `keys(n)` → `ATTRIBUTES(n)`
- `properties(n)` → `UNSET(n, "_id", "_key", "_rev")`
- `toString(expr)` → `TO_STRING(expr)`
- `toInteger(expr)` → `TO_NUMBER(expr)` (AQL has no separate integer type)
- `toFloat(expr)` → `TO_NUMBER(expr)`

**LIMIT/SKIP with expressions**:
- Allow `$param` and arithmetic in LIMIT/SKIP (currently integer literals only)

#### Deliverables

1. Refactor `_compile_agg_expr` to work in RETURN context, not just WITH
2. RETURN DISTINCT with multiple items → `COLLECT` + projection
3. Add function cases to `_compile_function_invocation` in `translate_v0.py`
4. Golden tests for each new function and aggregation pattern
5. Update §6.4 status to "Done" for each function

#### Files

- `arango_cypher/translate_v0.py` — `_compile_function_invocation`, `_append_return`, LIMIT/SKIP
- `tests/fixtures/cases/aggregation_return.yml` — new
- `tests/fixtures/cases/builtin_functions.yml` — new
- `tests/test_translate_aggregation_return_goldens.py` — new
- `tests/test_translate_builtin_functions_goldens.py` — new

---

### WP-3: Schema analyzer integration ⚡

**PRD**: §5.1, §5.2, §5.3, §10 v0.2
**Priority**: Critical path — blocks CLI `mapping` command and automated mapping
**Estimate**: 1-2 weeks
**Dependencies**: None (parallel with WP-1, WP-2)

#### Scope

Wire up `arangodb-schema-analyzer` as an optional dependency and implement the full mapping acquisition pipeline.

#### Deliverables

1. **Optional dependency** — add `arangodb-schema-analyzer` to `pyproject.toml` under a new `[project.optional-dependencies] analyzer = [...]` group
2. **`acquire_mapping_bundle(db)` function** — new module `arango_cypher/schema_acquire.py`:
   - Import `AgenticSchemaAnalyzer` (or use tool contract `run_tool`)
   - Call `operation="export"` against the connected database
   - Transform analyzer output into `MappingBundle` (with `conceptual_schema`, `physical_mapping`, properties, domain/range)
   - Optionally call `operation="owl"` and populate `MappingBundle.owl_turtle`
3. **`get_mapping(db)` convenience API** — add to `arango_cypher/api.py`:
   - `get_mapping(db, *, strategy="auto", use_analyzer=False) -> MappingBundle`
   - Implements the 3-tier fallback: explicit > heuristic > analyzer
4. **Fast heuristic classifier** — new function in `arango_cypher/schema_acquire.py`:
   - Check if all document collections have a `type` field (LPG) or all edge collections are dedicated (PG)
   - Return `"pg"`, `"lpg"`, `"hybrid"`, or `"unknown"`
   - Fast: sample first N docs from each collection
5. **Caching** — cache analyzer results by schema fingerprint:
   - Key: hash of (collection names + edge collection names + sample document keys)
   - Store: in-memory `dict` with TTL
6. **Service endpoint** — update `POST /translate` and the introspect flow to optionally call `acquire_mapping_bundle` when no mapping is provided but a session exists
7. **Tests** — unit tests with mocked analyzer, integration test with real DB

#### Files

- `pyproject.toml` — add `analyzer` optional dependency
- `arango_cypher/schema_acquire.py` — new module
- `arango_cypher/api.py` — add `get_mapping()`
- `arango_cypher/service.py` — update introspect to use analyzer when available
- `tests/test_schema_acquire.py` — new (unit tests with mocks)
- `tests/integration/test_schema_acquire_integration.py` — new

---

### WP-4: CLI

**PRD**: §4.2, §10 v0.2
**Priority**: Medium — useful but not on the critical path for TCK
**Estimate**: 1 week
**Dependencies**: WP-3 (for `mapping` subcommand)

#### Scope

Implement CLI entry point using `typer` + `rich`.

#### Deliverables

1. **`arango_cypher/cli.py`** — new module with four subcommands:
   - `translate` — read Cypher from argument or stdin, print AQL + bind vars (JSON)
   - `run` — translate + execute against connected DB, print results (table or JSON)
   - `mapping` — print mapping summary; optionally write OWL Turtle. Uses `get_mapping(db)`.
   - `doctor` — check connectivity, required collections, schema analyzer availability
2. **`pyproject.toml`** — add `[project.scripts]`:
   ```toml
   [project.scripts]
   arango-cypher-py = "arango_cypher.cli:app"
   ```
3. **Dependencies** — add `typer` and `rich` to a new `cli` optional group
4. **Stdin support** — `translate` and `run` accept piped input: `echo "MATCH (n) RETURN n" | arango-cypher-py translate`
5. **Connection config** — read from `ARANGO_*` environment variables or `--host`/`--port`/`--db`/`--user`/`--password` flags
6. **Tests** — CLI smoke tests using `typer.testing.CliRunner`

#### Files

- `arango_cypher/cli.py` — new
- `pyproject.toml` — `[project.scripts]`, `[project.optional-dependencies]`
- `tests/test_cli.py` — new

---

### WP-5: TCK harness improvements ⚡

**PRD**: §8.2, §10 v0.2
**Priority**: Critical path — must be done before WP-6
**Estimate**: 1 week
**Dependencies**: WP-1 (CREATE for setup steps)

#### Scope

Make the TCK runner capable of executing the majority of TCK scenarios end-to-end.

#### Deliverables

1. **Scenario Outline / Examples expansion** — extend `tests/tck/gherkin.py`:
   - Parse `Scenario Outline:` headers
   - Parse `Examples:` tables
   - Expand each row into a concrete `Scenario` with substituted `<placeholder>` values
2. **Given having executed** — in `tests/tck/runner.py`:
   - Extract Cypher from the doc string
   - Translate using `translate()` with CREATE support (WP-1)
   - Execute the resulting AQL to seed the graph
   - Support multiple "And having executed" steps (sequential seeding)
3. **Result normalization** — new `tests/tck/normalize.py`:
   - Parse Neo4j result conventions: `(:Label {prop: value})` for nodes, `[:TYPE {prop: value}]` for relationships
   - Compare actual ArangoDB documents against expected TCK values structurally
   - Handle type coercion: TCK uses `1` (integer), ArangoDB may return `1.0`
   - Handle null, boolean, string, list, map comparisons
4. **Error expectation scenarios** — in `runner.py`:
   - Handle `Then a SyntaxError should be raised` / `Then a TypeError should be raised`
   - Catch `CoreError` or ArangoDB errors and compare against expected error category
5. **Given parameters** — parse parameter tables and pass as `params` to `translate()`

#### Files

- `tests/tck/gherkin.py` — Scenario Outline expansion
- `tests/tck/runner.py` — setup execution, error expectations, parameter passing
- `tests/tck/normalize.py` — new result normalization module
- `tests/tck/test_tck_harness_smoke.py` — expand to test new capabilities

---

### WP-6: TCK Match coverage target

**PRD**: §8.2 phased strategy, §2.1 success criteria
**Priority**: High — validates correctness
**Estimate**: 1-2 weeks (iterative: run, fix, re-run)
**Dependencies**: WP-1, WP-2, WP-5

#### Scope

Download real TCK Match features, run them, and fix translator bugs until ≥ 40% of Match scenarios pass.

#### Deliverables

1. Download `Match*.feature` files: `python scripts/download_tck.py --only-match Match`
2. Run and triage: categorize failures as:
   - **Translator bug** — fix in `translate_v0.py`
   - **Missing construct** — document in §6.4, skip for now
   - **Runner bug** — fix in `tests/tck/runner.py`
   - **Normalization bug** — fix in `tests/tck/normalize.py`
3. Fix translator bugs iteratively until ≥ 40% pass rate
4. Document remaining skip reasons in a `tests/tck/SKIP_REASONS.md`
5. Add CI marker: `pytest -m tck` runs against downloaded features

#### Files

- `arango_cypher/translate_v0.py` — bug fixes discovered during TCK runs
- `tests/tck/runner.py` — runner improvements
- `tests/tck/normalize.py` — normalization improvements
- `tests/tck/SKIP_REASONS.md` — new

---

### WP-7: Movies dataset expansion

**PRD**: §8.3, §10 v0.2
**Priority**: Medium — validates practical correctness
**Estimate**: 1 week
**Dependencies**: WP-1 (CREATE for seed data conversion)

#### Scope

Expand the Movies dataset from 4 nodes / 3 edges to the full Neo4j Movies dataset.

#### Deliverables

1. **Full dataset fixture** — convert the Neo4j Movies `CREATE` script (~170 nodes, ~250 relationships) to `tests/fixtures/datasets/movies/lpg-data.json`
2. **Query corpus YAML** — extract example queries from the Neo4j Movies repo documentation:
   ```yaml
   - id: movies_001
     description: "Find actor by name"
     cypher: 'MATCH (a:Person {name: "Tom Hanks"}) RETURN a'
     dataset: movies
     mapping_fixture: movies_lpg
   ```
   Store as `tests/fixtures/datasets/movies/query-corpus.yml`
3. **PG layout fixture** — `tests/fixtures/datasets/movies/pg-data.json` with separate `Person`, `Movie` collections and `ACTED_IN`, `DIRECTED` edge collections
4. **PG mapping fixture** — `tests/fixtures/mappings/movies_pg.export.json`
5. **Integration tests** — parametrized test that runs each query corpus entry against both LPG and PG mappings
6. **Update seeder** — extend `tests/integration/datasets.py` to handle PG layout

#### Files

- `tests/fixtures/datasets/movies/lpg-data.json` — expand
- `tests/fixtures/datasets/movies/pg-data.json` — new
- `tests/fixtures/datasets/movies/query-corpus.yml` — new
- `tests/fixtures/mappings/movies_pg.export.json` — new
- `tests/integration/datasets.py` — extend
- `tests/integration/test_neo4j_movies_dataset.py` — new (replaces smoke test)

---

### WP-8: UI parameter binding + query history

**PRD**: §4.4.2F, §4.4.9 v0.3-beta
**Priority**: Medium — improves usability but not on critical path
**Estimate**: 1 week
**Dependencies**: None (parallel with everything)

#### Deliverables

1. **Parameter panel** — new `ui/src/components/ParameterPanel.tsx`:
   - Auto-detect `$paramName` tokens from Cypher text (regex scan)
   - JSON value input per parameter
   - Send `params` alongside `cypher` and `mapping` in API requests
   - Persist in localStorage per query hash
2. **Bind-vars panel** — show bind variables from the translation result below the AQL editor
3. **Query history** — new `ui/src/components/QueryHistory.tsx`:
   - Store last N queries (Cypher text + timestamp) in localStorage
   - Searchable list panel (drawer)
   - Click to restore a previous query
   - Up/down arrow in empty editor cycles through history
4. **Keyboard shortcuts** — implement §4.4.8:
   - Ctrl/Cmd+Enter → Translate
   - Shift+Enter → Execute
   - Ctrl+Space → Autocomplete (already works via CodeMirror)
5. **Update store** — extend `ui/src/api/store.ts` with `params`, `history` state

#### Files

- `ui/src/components/ParameterPanel.tsx` — new
- `ui/src/components/QueryHistory.tsx` — new
- `ui/src/api/store.ts` — extend
- `ui/src/api/client.ts` — send params in requests
- `ui/src/App.tsx` — integrate new panels
- `arango_cypher/service.py` — accept `params` in `/translate` and `/execute` requests

---

## v0.2 execution order (recommended)

```
Week 1-2:  WP-1 (CREATE) ⚡ + WP-2 (aggregation/functions) + WP-3 (schema analyzer) ⚡
           [all three in parallel]

Week 3:    WP-5 (TCK harness) ⚡ [depends on WP-1]
           WP-4 (CLI) [depends on WP-3]
           WP-8 (UI params/history) [parallel]

Week 4-5:  WP-6 (TCK Match coverage) [depends on WP-1, WP-2, WP-5]
           WP-7 (Movies expansion) [depends on WP-1]

Week 6:    Buffer / bug fixes from TCK runs
```

**Estimated total: 5-6 weeks** for full v0.2 with ≥ 40% TCK Match coverage.

---

## v0.3 — Language breadth + UI completeness + datasets

Work packages at higher granularity. Detailed breakdowns should be created when v0.2 nears completion.

### WP-9: OPTIONAL MATCH full support

**PRD**: §6.4, §7.6
**Estimate**: 1-2 weeks

- Multi-segment OPTIONAL MATCH: `OPTIONAL MATCH (a)-[:KNOWS]->(b)-[:LIVES_IN]->(c)`
- Node-only OPTIONAL MATCH: `OPTIONAL MATCH (n:Person {name: "Nobody"})`
- Leading OPTIONAL MATCH (no prior MATCH)
- AQL: subquery with `LEFT JOIN`-style fallback to null rows

### WP-10: EXISTS, regex, pattern predicates

**PRD**: §6.4 predicates
**Estimate**: 1 week

- `WHERE EXISTS(n.email)` → `FILTER n.email != null` (property existence)
- `WHERE (n)-[:KNOWS]->()` → pattern predicate (subquery existence check)
- `WHERE n.name =~ "^A.*"` → `FILTER REGEX_TEST(n.name, "^A.*")`

### WP-11: Named paths + path functions

**PRD**: §6.4 patterns, §7.6
**Estimate**: 1-2 weeks

- `p = (a)-[:KNOWS]->(b)` → `LET p = {vertices: [a, b], edges: [r]}`
- `length(p)` → `LENGTH(p.edges)`
- `nodes(p)` / `relationships(p)` → `p.vertices` / `p.edges`
- Native `shortestPath()` syntax → delegate to existing `arango.shortest_path` procedure
- Relationship uniqueness enforcement for multi-segment paths

### WP-12: Remaining built-in functions

**PRD**: §6.4 built-in functions
**Estimate**: 3-5 days

- `head(list)`, `tail(list)`, `last(list)`, `range(start, end, step)`, `reverse(list)`
- Multi-label matching for COLLECTION-style (currently only LABEL-style)

### WP-13: OWL Turtle round-trip

**PRD**: §5.4
**Estimate**: 1 week
**Dependencies**: WP-3

- Add `rdflib` as optional dependency
- `load_owl_turtle(ttl_string) -> MappingBundle` — parse TTL, extract entity/relationship mappings
- `generate_owl_turtle(mapping: MappingBundle) -> str` — produce OWL Turtle from enriched mapping
- Wire into `_mapping_from_dict` in `service.py` to read `owl_turtle` / `owlTurtle`
- Update `/schema/summary` to include OWL in response

### WP-14: Visual mapping graph editor

**PRD**: §5.6
**Estimate**: 2 weeks

- Add `cytoscape` + `cytoscape-dagre` to `ui/package.json`
- New `ui/src/components/MappingGraph.tsx` — Cytoscape.js entity-relationship diagram
- Entity nodes: rounded rectangles with label, collection badge, property list
- Relationship edges: labeled arrows with edge collection, style badge
- Bidirectional sync with JSON mapping editor
- Toggle between "JSON" and "Visual" views in mapping panel
- Add/edit/delete entities and relationships via side panel

### WP-15: Results graph view + profile

**PRD**: §4.4.6, §4.4.3C
**Estimate**: 1-2 weeks

- Add Cytoscape.js for results graph view (nodes + edges from query results)
- AQL Profile button → `POST /aql-profile` → annotated execution plan tree
- Color-coded hotspots (nodes with high execution time)
- Variable-use highlighting in Cypher editor

### WP-16: Datasets expansion

**PRD**: §8.3
**Estimate**: 1-2 weeks
**Dependencies**: WP-7

- Add Northwind dataset (convert from Neo4j, extract queries)
- Run Movies and Northwind query corpus against both LPG and PG mappings
- Automate dataset download: `scripts/download_neo4j_dataset.py`
- TCK overall ≥ 25% pass rate

### WP-17: NL-to-Cypher pipeline

**PRD**: §1.1 (architectural principle), §10 v0.3
**Priority**: Medium — high-value feature but not on critical path for transpiler correctness
**Estimate**: 2-3 weeks
**Dependencies**: WP-3 (schema analyzer — provides the conceptual schema the LLM consumes)

#### Architectural context

Per §1.1, the NL2Cypher pipeline operates exclusively against the **logical (conceptual) schema**. The LLM receives entity labels, relationship types, and property names — never collection names, type fields, or AQL. The transpiler handles all physical mapping concerns.

#### Scope

1. **Schema prompt builder** — given a `MappingBundle`, extract a compact textual representation of the conceptual schema suitable for LLM context:
   - Entity labels with properties and types
   - Relationship types with domain/range and properties
   - Example Cypher patterns for each relationship
2. **LLM adapter interface** — pluggable provider abstraction:
   - `NL2CypherProvider` protocol: `async def generate(nl_query: str, schema_context: str) -> str`
   - OpenAI implementation (GPT-4o / GPT-4.1)
   - Anthropic implementation (Claude)
   - Local/custom endpoint implementation
3. **Validation loop** — generated Cypher is parsed by the ANTLR parser before returning; if parsing fails, retry with error feedback (up to 2 retries)
4. **API endpoint** — `POST /nl2cypher` accepting `{ query: string, mapping?: MappingBundle }` and returning `{ cypher: string, confidence: number }`
5. **UI integration** — NL input toggle in the query editor: user types natural language, clicks "Generate Cypher", result populates the Cypher editor for review/edit before translation

#### Deliverables

1. `arango_cypher/nl2cypher.py` — schema prompt builder, provider protocol, OpenAI/Anthropic implementations
2. `arango_cypher/service.py` — `POST /nl2cypher` endpoint
3. `ui/src/components/NLInput.tsx` — NL input panel with generate button
4. `tests/test_nl2cypher.py` — unit tests with mocked LLM responses
5. Golden test corpus: natural language → expected Cypher pairs for regression testing

#### Files

- `arango_cypher/nl2cypher.py` — new
- `arango_cypher/service.py` — new endpoint
- `ui/src/components/NLInput.tsx` — new
- `ui/src/App.tsx` — integrate NL panel
- `tests/test_nl2cypher.py` — new

---

### WP-18: Index-aware transpilation

**PRD**: §5.7, §7.8
**Priority**: High — directly improves generated AQL quality for LPG graphs
**Estimate**: 1-2 weeks
**Dependencies**: WP-3 (schema analyzer for index metadata export)

#### Scope

Add index metadata to the physical mapping model and use it in the transpiler for optimization decisions.

#### Deliverables

1. **`IndexInfo` dataclass** — add to `arango_query_core/mapping.py`:
   ```python
   @dataclass(frozen=True)
   class IndexInfo:
       type: str  # persistent, hash, fulltext, geo, ttl, inverted
       fields: tuple[str, ...]
       unique: bool = False
       sparse: bool = False
       name: str = ""
       vci: bool = False
   ```
2. **`MappingResolver.resolve_indexes(label_or_type)`** — returns `list[IndexInfo]` from the entity/relationship mapping
3. **`MappingResolver.has_vci(rel_type)`** — convenience: checks if any index on the relationship's edge collection has `vci=True`
4. **VCI-aware traversal** — in `translate_v0.py`, when emitting traversal for `GENERIC_WITH_TYPE`:
   - If VCI exists on edge type field: emit edge filter (efficient at storage layer)
   - If no VCI: emit edge filter anyway (still correct) but log a performance warning
5. **VCI advisory** — in CLI `doctor` and service `/schema/introspect`:
   - Detect `GENERIC_WITH_TYPE` relationships without VCI
   - Report as recommendation: "Consider creating a VCI on `edges.relation`"
   - CLI `doctor --fix` offers to create missing VCI indexes
6. **Naked-LPG test variant** — add `tests/fixtures/datasets/movies/lpg-naked-data.json` (same data, no `vciIndexes` key) and `tests/fixtures/mappings/movies_lpg_naked.export.json` (no indexes array)
7. **Index metadata in mapping fixtures** — update `movies_lpg.export.json` and `movies_pg.export.json` to include index arrays

#### Files

- `arango_query_core/mapping.py` — `IndexInfo`, `resolve_indexes()`, `has_vci()`
- `arango_cypher/translate_v0.py` — VCI-aware traversal logic
- `arango_cypher/cli.py` — `doctor` VCI advisory
- `arango_cypher/service.py` — introspect VCI advisory
- `tests/fixtures/mappings/movies_lpg.export.json` — add indexes
- `tests/fixtures/mappings/movies_lpg_naked.export.json` — new (no indexes)
- `tests/fixtures/datasets/movies/lpg-naked-data.json` — new
- `tests/test_index_aware.py` — new golden + unit tests

---

### v0.3 execution order

```
Weeks 1-2:  WP-9 (OPTIONAL MATCH) + WP-10 (EXISTS/regex) [parallel]
Week 3:     WP-11 (named paths) + WP-12 (built-in functions) [parallel]
Weeks 4-5:  WP-14 (visual mapping editor) + WP-13 (OWL round-trip) + WP-18 (index-aware) [parallel]
Week 6:     WP-15 (graph view + profile) + WP-16 (datasets)
Weeks 7-8:  WP-17 (NL2Cypher)
Week 9:     TCK re-run + bug fixes to hit ≥ 25%
```

**Estimated total: 8-9 weeks.** WP-19 below is tracked in the same version band but is mostly unblocking/documentation work (≤ 3 days) and runs in parallel.

---

### WP-19: Arango Platform deployment enablement

**Goal:** Make this repo deployable to the Arango Platform via ServiceMaker + the Container Manager API, with the smallest possible change inside this repo.

**Motivation:** See `docs/python_prd.md` §15. The only blocker is that our `[analyzer]` extra depends on `arangodb-schema-analyzer`, which is not published to any package index today — so `uv sync` fails inside the ServiceMaker build container. The decision in §15.1 is to fix this at the source (publish the analyzer) rather than build packaging tooling in this repo. That makes WP-19 a documentation + smoke-test effort here, with the actual packaging fix tracked upstream in `~/code/arango-schema-mapper`.

**Scope (must have):**

1. **Deployment runbook** — add a README-style markdown file in `docs/arango_packaging_service/` (e.g. `deploying_arango_cypher_py.md`) documenting the manual happy path end-to-end:
   - Prerequisite: confirm `arangodb-schema-analyzer` is pinned in `pyproject.toml` to a published version (no bare names, no paths, no git URLs).
   - Build: `uv build --sdist` (or equivalent `tar -czf`) → `dist/arango-cypher-py-<ver>.tar.gz`.
   - Upload: `curl POST /_platform/filemanager/global/byoc/` with `$ARANGO_PLATFORM_TOKEN` from `.env`.
   - Deploy: `curl POST /_platform/acp/v1/uds` with the app instance spec (sample JSON body included).
   - Redeploy: bump `pyproject.toml` version, rebuild, upload, deploy (same three commands, new version).
   - Teardown: `curl DELETE` for both FileManager versions and the ACP instance.
   - Troubleshooting section for the common failure modes (build fails → deps not pinned; deploy fails → token scope; container won't start → port or env mismatch).

2. **Packaging smoke test** — new test file `tests/integration/test_packaging_smoke.py`, gated behind `RUN_PACKAGING=1`, that:
   - Builds the sdist via `uv build --sdist`.
   - Creates a fresh virtualenv in a temp directory.
   - Runs `uv sync` against the built sdist with the `[service,analyzer]` extras enabled.
   - Asserts `python -c "import arango_cypher.service"` succeeds inside that venv.
   This catches dependency-graph regressions that would break deployment without requiring a live platform.

3. **`pyproject.toml` cleanup** — once `arangodb-schema-analyzer` is published upstream, pin it with a version specifier (`"arangodb-schema-analyzer>=X.Y.Z"`) in the `[analyzer]` extra. This WP lands after the publication.

**Scope (explicit non-goals):**

- **No packaging/deployment CLI in this repo** (no `arango-cypher-py package` / `deploy` / `redeploy` / `teardown` subcommands, no `arango_cypher/packaging.py` module, no `packaging.toml` manifest). Rejected in PRD §15.2 on scope, release-cadence, and blast-radius grounds.
- **No vendored wheels, no `vendor/` directory, no pyproject rewriting logic.** Rejected in PRD §15.2 as absorbing a cost that belongs upstream.
- **No HTTP endpoints for packaging/deployment on `arango_cypher.service`.**
- **No repo-local implementation of the platform API client.** If/when a general deployment CLI becomes worthwhile, it lives in its own project or is contributed to ServiceMaker.

**Tests:**

- `tests/integration/test_packaging_smoke.py` (per above) — one test, gated behind `RUN_PACKAGING=1`. Runs in CI on a cron schedule, not on every PR (build time is tens of seconds).

**Documentation:**

- PRD §15 (landed).
- `docs/arango_packaging_service/deploying_arango_cypher_py.md` (new, per scope item 1).
- PRD implementation status table updated to reflect WP-19 status.

**Acceptance criteria:**

1. The runbook document exists and a human following it end-to-end can deploy this repo to a staging Arango Platform without consulting external sources.
2. `RUN_PACKAGING=1 pytest tests/integration/test_packaging_smoke.py` passes on a clean checkout (once `arangodb-schema-analyzer` publication lands).
3. `pyproject.toml`'s `[analyzer]` extra pins a published version with no local-path or git references.

**Estimate:** 2-3 days in this repo, after the upstream analyzer publication. Runbook ~1 day, smoke test ~1 day, pyproject cleanup and verification ~0.5 day.

**Dependencies:**

- **Upstream blocker:** `arangodb-schema-analyzer` must be published to PyPI (or the ArangoDB-internal package index, if one exists). Tracked in `~/code/arango-schema-mapper`, not in this repo. No useful work on WP-19 in this repo until that lands.
- Staging Arango Platform endpoint for runbook verification.

**Related future work (out of scope for WP-19):**

- An `arango-platform-deploy` CLI (or a contribution to upstream ServiceMaker) that wraps the Container Manager API generically for any ServiceMaker tarball. Tracked separately; built only when deployment volume justifies the investment.

---

### WP-25: NL→Cypher pipeline hardening (SOTA upgrades)

**PRD**: §1.2.1
**Priority**: High — the existing `nl2cypher.py` is a correct but minimal zero-shot baseline; each SOTA upgrade below directly improves accuracy, cost, or reliability on real workloads.
**Estimate**: 3-4 weeks wall-clock with 4 parallel sub-agents on .1-.4, then 1 week sequential on .5. ~18-23 dev-days total.
**Dependencies**: WP-17 (existing NL→Cypher pipeline — the thing being hardened). Optional: an ArangoSearch view on the target database improves .2; otherwise a BM25/regex fallback is used.

#### Motivation and research

Two research notes (`docs/research/nl2cypher.md`, `docs/research/nl2cypher2aql_analysis.md`) document the 2025-2026 state of the art for Text2Cypher and analyze this repo's implementation against it. The summary: we have the right architecture (two-stage pipeline, logical-only prompt, self-healing retry) but we are missing every non-structural technique the literature calls essential — dynamic few-shot retrieval, pre-flight entity resolution, execution-grounded validation, and prompt caching. PRD §1.2.1 gives the full gap analysis.

#### Scope: five sub-packages, four parallelizable

The first four sub-packages touch disjoint modules and can run in parallel under separate sub-agents. Their single merge point in `arango_cypher/nl2cypher.py` is the shape of the prompt builder — handled by a pre-step refactor (below) so each sub-agent extends a composable builder rather than editing the monolithic `_SYSTEM_PROMPT` constant.

**Pre-step (must land first, ~0.5 d, 1 agent):** refactor `_SYSTEM_PROMPT` / `_call_llm_with_retry` in `arango_cypher/nl2cypher.py` into a `PromptBuilder` class with composable sections (`schema`, `few_shot`, `resolved_entities`, `question`, `retry_context`). The existing behaviour is preserved exactly — zero-shot with schema only — but the shape lets sub-agents add their section without conflicting diffs. Ship the refactor on `main` before launching parallel agents.

##### WP-25.1 — Dynamic few-shot retrieval

**Goal:** inject the top-K most similar (NL, Cypher) example pairs into the prompt.

**Files:**
- `arango_cypher/nl2cypher/fewshot.py` — new. `FewShotIndex` class: build an in-memory index over a seed corpus of `(question, cypher)` pairs, retrieve top-K by similarity given a new question. Start with **BM25** (rank_bm25 library, already light on dependencies) as the baseline so the feature ships without requiring embeddings or network calls. Leave a pluggable `Retriever` protocol so an embedding-based retriever can replace it later without API churn.
- `arango_cypher/nl2cypher/corpora/` — new. Mine `tests/fixtures/datasets/{movies,northwind,social}/query-corpus.yml` into `(description, cypher)` seed files (`movies.yml`, `northwind.yml`, `social.yml`). These are conceptual-Cypher, so they respect the §1.2 invariant. ~60 pairs total, expandable.
- `arango_cypher/nl2cypher.py` — wire the retriever into `PromptBuilder.few_shot`. Behaviour: when `use_fewshot=True` (default), retrieve K=3 similar examples and render them as `Q: <question>\nA:\n```cypher\n<cypher>\n```\n\n` before the user question.
- `tests/test_nl2cypher_fewshot.py` — unit tests: BM25 retrieval correctness, end-to-end prompt shape, empty-corpus fallback (no crash), retriever protocol shape.

**Corpus format (in `corpora/*.yml`):**
```yaml
version: 1
mapping_fixture: movies_lpg
examples:
  - question: "Find all movies Tom Hanks acted in"
    cypher: 'MATCH (p:Person {name: "Tom Hanks"})-[:ACTED_IN]->(m:Movie) RETURN m.title'
  - ...
```

**Acceptance:**
- Unit tests pass.
- A/B against the held-out set from WP-25.5: first-shot parse-success rate on the evaluation harness improves by ≥ 5 pp over zero-shot (measurement is WP-25.5's responsibility; this WP just has to make the knob turn on).
- No regressions in existing unit tests (`pytest -m "not integration and not tck"`).

##### WP-25.2 — Pre-flight entity resolution

**Goal:** rewrite user-supplied string literals ("Forest Gump") to their database-correct form ("Forrest Gump") before the LLM call, so the generated Cypher matches actual data.

**Files:**
- `arango_cypher/nl2cypher/entity_resolution.py` — new. Extract entity candidates from the question (proper nouns via simple POS heuristics, or quoted strings), then resolve each against the live database:
  - **Preferred path:** ArangoSearch lookup — if the connected database has a view spanning the property/field candidates (`name`, `title`, etc.), issue an AQL `SEARCH ANALYZER(...)` query and take the top BM25 hit per candidate.
  - **Fallback path (no view):** AQL `FILTER LOWER(d.name) == LOWER(@value) OR CONTAINS(LOWER(d.name), LOWER(@value))` against the relevant collections. Cap candidates and rows.
  - **Offline/no-DB path:** skip entity resolution cleanly and log at INFO (the feature degrades gracefully when NL2Cypher is used without a live connection).
- `arango_cypher/nl2cypher.py` — wire resolver into `PromptBuilder.resolved_entities`. Rendered as: `User mentioned 'Forest Gump' — matched to Movie.title='Forrest Gump'.` The section appears before the question.
- `tests/test_nl2cypher_entity_resolution.py` — unit tests with mocked DB responses: typo correction, no-match handling, multiple candidates in one question, offline fallback.

**Notes:**
- Does NOT leak physical details to the LLM. The resolved string is a property *value*, not a collection name or schema detail. The conceptual label (`Movie`, `Person`) already appears in the schema summary.
- Respect latency budget: one extra round-trip per request, cache per-session by question hash.
- `_fix_labels()` (existing post-hoc label rewriter) stays — it catches cases the resolver misses.

**Acceptance:**
- Unit tests pass.
- Intentional-typo corpus (added to WP-25.5 harness) now produces correct Cypher where previously it produced queries returning zero rows.
- Graceful no-op when no provider/DB is configured.

##### WP-25.3 — Execution-grounded validation loop

**Goal:** extend the retry loop in `_call_llm_with_retry` to also run the translated AQL through `_api/explain` on the connected database. Collection-not-exists, property-not-exists, and syntax errors surfaced by `EXPLAIN` feed back into the LLM retry — same mechanism as the existing ANTLR parse-error feedback.

**Files:**
- `arango_cypher/nl2cypher.py` — extend `_call_llm_with_retry`:
  - After ANTLR parse succeeds, invoke `translate()` to produce AQL.
  - If a DB client is available on the request, call `POST /_api/explain` with the AQL (no execution, just planning).
  - On `EXPLAIN` failure, capture the error message, include it in the next retry prompt, and try again.
  - On `EXPLAIN` success, return the result as before.
- `arango_query_core/exec.py` — add `explain_aql(aql, bind_vars) -> (ok, plan_or_error)` helper if one doesn't exist. Read-only; no data risk.
- `tests/test_nl2cypher_execution_grounded.py` — unit tests with a mocked `explain` function: label-typo self-heal, property-typo self-heal, syntax-only failure still works without DB.

**Bounded retry:** honour the existing `max_retries` knob (default 2). Total LLM calls is still capped.

**Acceptance:**
- Unit tests pass.
- Offline mode (no DB configured) behaves identically to today — ANTLR-only validation.
- Online mode self-heals at least two of the intentional-failure cases added in WP-25.5.

##### WP-25.4 — Prompt caching

**Goal:** the schema block is the same on every request for a given mapping. Stop paying tokens for it.

**Files:**
- `arango_cypher/nl2cypher.py` — `PromptBuilder.render()` returns the prompt *structured* (system prefix including schema, then the per-request tail) so the provider layer can apply provider-specific caching.
- `arango_cypher/nl2cypher/providers.py` (or extend inline) — provider-specific:
  - **OpenAI:** prompt caching is automatic above a token threshold; ensure schema-prefix ordering (largest static block first). Log `usage.prompt_tokens_details.cached_tokens` when present.
  - **Anthropic:** wrap the schema block in a `cache_control: {type: "ephemeral"}` message segment. ✅ **Shipped** via `split_system_for_anthropic_cache()` + `AnthropicProvider.generate()` (Wave 4d / 2026-04-18). End-to-end cache-hit measured live against `claude-sonnet-4-5` in Wave 4l (2026-04-20): 2346/2357 = 99.5 % of input tokens served from cache on the warm call.
  - **OpenRouter:** varies by upstream model; best-effort, no assertion required.
- `tests/test_nl2cypher_caching.py` — provider plumbing tests only (no live API calls). Assert that the rendered prompt places schema first and marks the cache boundary.

**Acceptance:**
- Unit tests pass.
- At least one real-API smoke test (opt-in, requires `OPENAI_API_KEY`) shows `cached_tokens > 0` on the second of two identical requests.
- No prompt-shape changes visible to the end user.

##### WP-25.5 — Evaluation harness + regression gate

**Goal:** a repeatable measurement that tells us whether each of WP-25.1 through .4 actually improves the pipeline, plus a CI gate that blocks regressions.

**Files:**
- `tests/nl2cypher/eval/corpus.yml` — new. Hand-curated NL→expected-Cypher pairs per dataset (movies, northwind, social), with:
  - Straightforward cases (baseline coverage).
  - Typo cases (for WP-25.2).
  - Intent-similar-to-corpus cases (for WP-25.1).
  - Hallucination-bait cases — questions phrased to tempt the LLM into inventing labels (for WP-25.3).
  - ~40-60 cases total.
- `tests/nl2cypher/eval/runner.py` — new. For each case: run the pipeline, collect metrics (parse-success, `EXPLAIN`-success, exact-match against expected, row-match against the live DB if `RUN_NL2CYPHER_EVAL_LIVE=1`), token usage, retries, latency. Emit a markdown + JSON report to `tests/nl2cypher/eval/reports/<date>-<config>.{md,json}`.
- `tests/nl2cypher/eval/configs.yml` — named configs (`zero_shot`, `few_shot`, `few_shot+entity`, `few_shot+entity+grounded`, `full`) that set the flags on the pipeline. The runner can sweep all configs to produce a comparison.
- `tests/test_nl2cypher_eval_gate.py` — a tiny regression test that loads `tests/nl2cypher/eval/baseline.json` (committed) and fails if first-shot parse-success drops by more than 5 pp or mean tokens per query increases by more than 20%. Gated behind `RUN_NL2CYPHER_EVAL=1` so it only runs when opted in (LLM calls cost money).

**Acceptance:**
- `RUN_NL2CYPHER_EVAL=1 pytest tests/test_nl2cypher_eval_gate.py` passes on a checkout that includes all of WP-25.1 through .4.
- The committed baseline report shows measurable uplift over zero-shot across the corpus.
- Report format is readable (markdown tables) and diff-friendly (JSON).

#### Test strategy summary

- **Unit tests per sub-package** (no LLM calls, no DB calls): run on every PR.
- **Eval harness** (WP-25.5): opt-in via `RUN_NL2CYPHER_EVAL=1`, runs on a nightly/cron CI or manually.
- **Live smoke test for caching** (WP-25.4): opt-in via `OPENAI_API_KEY`, manual.

#### Multi-subagent orchestration

Agent prompts for WP-25 are documented as "Wave 4" in `docs/agent_prompts.md`. Orchestration:

```
Pre-step:  refactor PromptBuilder (1 agent, sequential; ~0.5 d)
Wave 4a:   WP-25.1, WP-25.2, WP-25.3, WP-25.4 in parallel (4 agents)
Wave 4b:   merge 4a, run unit suite, resolve any small merge conflicts
Wave 4c:   WP-25.5 (1 agent, after 4b)
```

#### Status (2026-04-20)

All five sub-packages landed on `main`:

- **WP-25.1 (Dynamic few-shot retrieval)** — `arango_cypher/nl2cypher/fewshot.py` ships a `Retriever` protocol, a BM25 implementation (backed by the optional `rank_bm25` dependency, with a token-overlap fallback), and a `FewShotIndex` loaded from `arango_cypher/nl2cypher/corpora/{movies,northwind,social}.yml`. `PromptBuilder.few_shot` renders the top-K examples ahead of the user question. Toggled via `use_fewshot` on `nl_to_cypher` and on the `/nl2cypher` HTTP endpoint.
- **WP-25.2 (Pre-flight entity resolution)** — `arango_cypher/nl2cypher/entity_resolution.py` exposes `EntityResolver` + `ResolvedEntity`. Candidates are extracted with conservative regex heuristics (quoted strings, Title-Case phrases, stopword-filtered tokens) and resolved against string-valued properties via `MappingResolver.resolve_entity`. Respects both `COLLECTION` and `LABEL` mapping styles. Degrades to a null resolver when no DB handle is supplied.
- **WP-25.3 (Execution-grounded validation)** — `arango_query_core.exec.explain_aql` plans the translated AQL with `db.aql.explain`; `_call_llm_with_retry` now feeds EXPLAIN errors back into the retry prompt alongside parse errors. Skips cleanly when no DB is wired.
- **WP-25.4 (Prompt caching)** — `PromptBuilder` orders sections `prelude → schema → few-shot → resolved entities → question → retry-context`, maximising prefix stability. `_BaseChatProvider._chat` surfaces `usage.prompt_tokens_details.cached_tokens`; it is summed across retries and propagated on `NL2CypherResult.cached_tokens` / `NL2AqlResult.cached_tokens` and the HTTP responses. The `AnthropicProvider` is now wired end-to-end against `POST /v1/messages` with the `cache_control: {type: "ephemeral"}` split (`split_system_for_anthropic_cache`); `cache_read_input_tokens` is propagated to `cached_tokens` so telemetry is provider-agnostic. `get_llm_provider()` auto-detects Anthropic on `ANTHROPIC_API_KEY`, after OpenAI/OpenRouter, or explicitly via `LLM_PROVIDER=anthropic`. `_llm_suggest_nl_queries` now goes through the public `LLMProvider.generate` protocol so any provider works.
- **WP-25.5 (Eval harness + regression gate)** — `tests/nl2cypher/eval/{corpus.yml,configs.yml,runner.py,baseline.json}` drive a reproducible evaluation. The corpus is a 31-case curation across `movies_pg` (21) + `northwind_pg` (10), spanning 5 categories: baseline, few_shot_bait, typo, hallucination_bait, multi_hop. The runner is importable by unit tests with a scripted provider, and exposes a `python -m tests.nl2cypher.eval.runner --config full [--baseline] [--with-db]` CLI for refreshing reports. `tests/test_nl2cypher_eval_gate.py::test_gate_against_baseline` enforces the regression policy (parse_ok / pattern_match within 5 pp, mean tokens within +20 %, mean retries within +0.3) when `RUN_NL2CYPHER_EVAL=1` is set; pass `NL2CYPHER_EVAL_USE_DB=1` to also engage the live ArangoDB so WP-25.2 entity resolution and WP-25.3 EXPLAIN-grounded retry actually run. **Wave 4g (2026-04-18)** added `open_eval_db_handles()` (env-var-driven, per-fixture map keyed off `NL2CYPHER_EVAL_<FIXTURE>_DB`, defaults `nl2cypher_eval_movies_pg` / `northwind_cross_test`) plus `db_for_fixture=` arguments on `run_case` and `run_eval`, and fixed a latent bug where `db` was gated on `use_execution_grounded` only — meaning the `few_shot_plus_entity` config silently skipped WP-25.2; the gate is now `use_execution_grounded OR use_entity_resolution`. **Wave 4h (2026-04-18)** extended `EntityResolver._query_label_property` with a `LEVENSHTEIN_DISTANCE`-based fuzzy-scoring branch (configurable `fuzzy_threshold`, default 0.7, contribution down-weighted to 0.9 so exact / substring still win when both fire), so typos like "Forest Gump" → "Forrest Gump" resolve correctly against a live DB. **Wave 4i (2026-04-18)** refreshed `baseline.json` against live OpenAI gpt-4o-mini with `--with-db` + fuzzy resolver — pattern_match jumped from 87.1 % to **90.3 %** and the typo category from 33 % to **67 %** in a single step (live gate self-passes against the new floor in 130 s). **Wave 4j (2026-04-18)** added three canonical role-noun few-shot examples ("List all actors?", "Who are all the directors?", "List every writer in the database?") to `arango_cypher/nl2cypher/corpora/movies.yml` so the BM25 retriever teaches the LLM that role nouns map to `Person + role-edge + DISTINCT`, not a separate label. Headline: pattern_match **93.5 %**, hallucination_bait recovered to **100 %**, retries_mean **0**. **Wave 4k (2026-04-18)** wired the regression gate into nightly CI via `.github/workflows/nl2cypher-eval.yml` (cron `0 6 * * *` + `workflow_dispatch`), reusing the `arangodb/arangodb:3.11` service from the existing `integration` job, seeding `nl2cypher_eval_movies_pg` + `northwind_cross_test` from `tests/integration/datasets.py`, and running the live gate against `OPENAI_API_KEY`. Costs ~$0.05 per nightly; self-skips cleanly when no LLM secret is configured; failures upload `tests/nl2cypher/eval/reports/` as a 14-day artifact. **Wave 4l (2026-04-20)** extended the nightly CI with a two-row `strategy.matrix` — adding an `anthropic` row (claude-haiku-4-5) alongside `openai` (gpt-4o-mini), each with its own calibrated baseline (`baseline.json` / `baseline.anthropic.json`) selected at test time via `NL2CYPHER_EVAL_PROVIDER` and `_baseline_path_for_provider()`. First Anthropic baseline headline: **parse_ok=100% / pattern_match=100% / retries=0** (every category including typo at 100%, beating OpenAI by 6.5 pp). Cache-hit plumbing separately proven against `claude-sonnet-4-5` (2346/2357 tokens cache-read on the warm call); Haiku 4.5's 4096-token cache-minimum is above our ~500-token prompts, which is why the eval run shows `cached_tokens_mean=0`. Run artifacts under `tests/nl2cypher/eval/reports/` are gitignored; refresh the baseline by re-running the CLI with `--baseline`. **Wave 4m (2026-04-20)** is cross-cutting (not in WP-25 proper, but shipped in the same window as the Emmet snapshot prep): a public schema-change-detection API + two-tier persistent cache around `get_mapping()`. Replaced the single opaque `_schema_fingerprint` with two orthogonal fingerprints — `_shape_fingerprint` (collections + types + full index digests) and `_full_fingerprint` (shape + counts) — and added `describe_schema_change(db) → SchemaChangeReport`, `invalidate_cache(db)`, plus new `cache_collection` / `cache_key` / `force_refresh` kwargs on `get_mapping`. When the shape is stable but row counts have drifted, `get_mapping` reuses the cached conceptual + physical mapping and recomputes only the cardinality statistics block — replacing a full re-introspection (≈ 2–30 s with the LLM path) with a ~50 ms stats refresh. The persistent cache (`arango_cypher.schema_cache.ArangoSchemaCache`, defaults to an `arango_cypher_schema_cache` user-land collection, gated by `CACHE_SCHEMA_VERSION`, excluded from its own fingerprints) survives service restarts and is shared across instances pointed at the same DB — directly benefiting the Arango Platform container deployment path. Delivered with 23 new unit tests pinning: fingerprint stability under row-count drift, fingerprint sensitivity to index-uniqueness flips (covering the pre-existing index-count-only bug), `bundle_to_doc` / `bundle_from_doc` round-trip completeness, cache corruption / stale-version tolerance, the stats-only-refresh path, and self-exclusion of the cache collection from its own fingerprints. **Wave 4n (2026-04-20)** exposed Wave 4m on the HTTP surface: `GET /schema/status` (calls `describe_schema_change(session.db)` and returns the four-valued status + ergonomic booleans + current/cached fingerprint pairs) and `POST /schema/invalidate-cache` (drops both cache tiers; `?persistent=false` preserves tier 2). Both accept optional `cache_collection` / `cache_key` query params mirroring the Python API. 10 new tests in `tests/test_service_schema_status.py` use FastAPI `dependency_overrides` + a `_MutableFakeDb` wrapper to simulate schema drift between HTTP calls without a live DB (covers all four status transitions, the persistence toggle, and 401 on missing session). Closes the gap flagged during the mapper-issue audit: Wave 4m was Python-only, now UI clients, platform orchestrators, and monitoring probes can act on change detection without embedding Python.

#### Out of scope for WP-25 (tracked as future work)

- **Task decomposition** — multi-agent splitting of complex questions into sub-queries. Revisit after the eval harness tells us whether single-shot is ceiling-bound.
- **SLM fine-tuning** — belongs in a separate research project with a GPU training pipeline. The `LLMProvider` protocol already accommodates a fine-tuned endpoint.
- **NL → AQL direct-path (§1.3) parity** — the same SOTA techniques would help the direct path. Layer in only after §1.2 (primary) is hardened.

---

## v0.4+ — Advanced features + TCK convergence

Feature-level outline. Detailed WP breakdown created when v0.3 nears completion.

### Cypher language

| Feature | Estimate | Dependencies |
|---------|----------|-------------|
| MERGE | 1-2 weeks | WP-1 (CREATE patterns) |
| DELETE / DETACH DELETE | 1 week | WP-1 |
| SET (property update) | 1 week | WP-1 |
| FOREACH | 1 week | |
| List comprehensions | 1 week | |
| Pattern comprehensions | 1 week | WP-10 (pattern predicates) |
| COUNT subquery | 1 week | |
| WITH from multiple MATCHes | 1 week | |

### Optimization

| Feature | Estimate | Dependencies |
|---------|----------|-------------|
| Filter pushdown into traversals | 1 week | |
| Index hint emission | 3-5 days | |
| Translation caching (`lru_cache`) | 2-3 days | |
| Relationship uniqueness enforcement | 1 week | WP-11 (named paths) |

### UI

| Feature | Estimate | Dependencies |
|---------|----------|-------------|
| Hover documentation | 1 week | |
| Profile-aware warnings | 3-5 days | WP-15 (profile) |
| Snippet templates | 3-5 days | |
| Format/prettify | 1 week | |
| Correspondence hints (source maps) | 1-2 weeks | |
| Multi-statement support | 1 week | |
| Export (CSV/JSON) | 3-5 days | |
| `arango.*` / `$` / keyword autocompletion | 1 week | |

### Agentic

| Feature | Estimate | Dependencies |
|---------|----------|-------------|
| `translate_tool(request_dict)` wrapper | 3-5 days | |
| Explain-why-mapped | 3-5 days | WP-3 (schema analyzer) |
| Suggest-indexes | 3-5 days | |
| Propose-mapping-overrides | 1 week | WP-3 |

### Testing

| Feature | Estimate | Dependencies |
|---------|----------|-------------|
| TCK ≥ 60% overall | 2-3 weeks (iterative) | All Cypher WPs |
| Automate dataset download script | 3-5 days | |
| ICIJ Paradise Papers dataset | 1 week | |

---

## Cross-cutting concerns (applicable to all versions)

### Test discipline

Every work package must include:
- Golden tests (YAML fixture + `test_translate_*_goldens.py`) for new translation features
- Integration tests for features that touch AQL execution
- Update to PRD §6.4 (Cypher subset table) marking new constructs as "Done"

### Documentation updates

Every work package must update:
- PRD implementation status table (top of `python_prd.md`)
- PRD §6.4 supported Cypher subset
- PRD §4.4.9 UI phasing status column
- This implementation plan (mark WP as complete)

### Regression prevention

- All existing golden tests must pass after every WP merge
- All existing integration tests must pass
- `ruff check .` must pass (enforced in CI)

### Schema analyzer no-workaround policy

When the transpiler encounters a gap in `arangodb-schema-analyzer` output (missing data, incorrect mapping, lacking a capability):

1. **Do not work around it** in transpiler code. No shims, no fallback heuristics that duplicate analyzer logic, no special-case handling.
2. **File a bug or feature report** against `~/code/arango-schema-mapper` with the database schema, current analyzer output, expected output, and a Cypher example.
3. **Document the gap** in the PRD §5.3 status table with a reference to the filed issue.
4. **Fail gracefully** with `CoreError(code="ANALYZER_GAP")` until the upstream fix lands.

This applies to all work packages that touch schema acquisition (WP-3, WP-13, WP-17, WP-18, and any future WP that consumes analyzer output). The analyzer is the canonical source for ontology extraction; improving it at the source benefits all consumers.

### Logical schema as query interface (§1.1)

All Cypher queries — whether hand-written, LLM-generated, or from NL2Cypher — are expressed against the **conceptual (logical) schema**, never against the physical ArangoDB layout. The transpiler and mapping layer absorb all physical details (collection names, type fields, indexes). This principle:

- Ensures query portability across PG, LPG, and hybrid layouts
- Motivates the schema analyzer as the canonical ontology source
- Constrains NL2Cypher (WP-17) to operate solely at the conceptual level
- Constrains index-aware transpilation (WP-18) to read physical details from the mapping, not from queries

---

## Risk register

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| `arangodb-schema-analyzer` API changes | WP-3 blocked | Low | Pin version; use tool contract as stable interface |
| CREATE semantics differ from Neo4j (no auto-IDs, no schema constraints) | TCK scenarios fail on edge cases | Medium | Document ArangoDB-specific behavior; skip scenarios that depend on Neo4j-specific ID generation |
| ANTLR4 Python runtime performance | Translation latency exceeds 50ms target for complex queries | Low | Benchmark during WP-6; if needed, add AST caching by query hash |
| TCK 40% target too ambitious for v0.2 | Delays release | Medium | TCK target is aspirational; release v0.2 when CREATE + aggregation + CLI are solid, even if TCK is at 30% |
| Visual mapping editor scope creep | WP-14 exceeds 2 weeks | Medium | Ship read-only graph view first; defer edit/create interactions to follow-up |
| Schema analyzer does not export index metadata | WP-18 blocked on index data | Medium | Fall back to querying indexes directly via `python-arango` `db.collection(c).indexes()`; file ANALYZER_GAP per no-workaround policy |
| NL2Cypher LLM quality varies across models/providers | Generated Cypher may be invalid or use wrong labels | Medium | Validation loop (ANTLR parse check + retry); golden test corpus for regression; schema prompt engineering |
| VCI creation on production data | User may not want automatic index creation | Low | `doctor --fix` requires explicit confirmation; advisory-only by default |

---

## Tracking

Update this table as work packages are completed:

| WP | Name | Version | Status | Completed |
|----|------|---------|--------|-----------|
| WP-1 | CREATE clause | v0.2 | **Done** | 2026-04-11 |
| WP-2 | Aggregation + built-in functions | v0.2 | **Done** | 2026-04-11 |
| WP-3 | Schema analyzer integration | v0.2 | **Done** | 2026-04-11 |
| WP-4 | CLI | v0.2 | **Done** | 2026-04-11 |
| WP-5 | TCK harness improvements | v0.2 | **Done** | 2026-04-11 |
| WP-6 | TCK Match coverage | v0.2 | **Done** | 2026-04-12 |
| WP-7 | Movies dataset expansion | v0.2 | **Done** | 2026-04-12 |
| WP-8 | UI parameter binding + history | v0.2 | **Done** | 2026-04-11 |
| WP-9 | OPTIONAL MATCH full support | v0.3 | **Done** | 2026-04-13 |
| WP-10 | EXISTS, regex, pattern predicates | v0.3 | **Done** | 2026-04-13 |
| WP-11 | Named paths + path functions | v0.3 | **Done** | 2026-04-13 |
| WP-12 | Remaining built-in functions | v0.3 | **Done** | 2026-04-13 |
| WP-13 | OWL Turtle round-trip | v0.3 | **Done** | 2026-04-13 |
| WP-14 | Visual mapping graph editor | v0.3 | **Done** | 2026-04-13 |
| WP-15 | Results graph view + profile | v0.3 | **Done** | 2026-04-13 |
| WP-16 | Datasets expansion | v0.3 | **Done** | 2026-04-13 |
| WP-17 | NL-to-Cypher pipeline | v0.3 | **Done** | 2026-04-13 |
| WP-18 | Index-aware transpilation | v0.3 | **Done** | 2026-04-13 |
| WP-19 | Arango Platform deployment enablement | v0.4 | Blocked (upstream) | WP body above. Awaiting `arangodb-schema-analyzer` PyPI publication. |
| WP-20 | Filter pushdown into traversals | v0.4 | **Done** | 2026-04-15 (WS-F/G sprint — PRUNE for variable-length, conservative rules). |
| WP-26 | Translation caching (LRU, 256 entries) | v0.4 | **Done** | 2026-04-13. Originally tracked as WP-19; renumbered 2026-04-17 when WP-19 was reassigned to Arango Platform deployment enablement (PRD §15). |
| WP-21 | List + pattern comprehensions | v0.4 | **Done** | 2026-04-13 |
| WP-22 | Results export (CSV/JSON) | v0.4 | **Done** | 2026-04-13 |
| WP-23 | Agentic tools | v0.4 | **Done** | 2026-04-13 |
| WP-24 | WITH from multiple MATCHes | v0.4 | **Done** | 2026-04-13 |
| WP-25 | NL→Cypher pipeline hardening (SOTA upgrades) | v0.4 | **Done** | 2026-04-20. All five sub-packages landed 2026-04-18 (WP-25.1 dynamic few-shot, WP-25.2 pre-flight entity resolution, WP-25.3 execution-grounded validation via `_api/explain`, WP-25.4 cache-friendly section ordering + `cached_tokens` + live `AnthropicProvider`, WP-25.5 eval harness + regression gate); Waves 4g–4l (2026-04-18..20) closed all post-WP-25 follow-ups: live-DB plumbing in the runner + latent bug fix (4g), `LEVENSHTEIN_DISTANCE` fuzzy scoring in `EntityResolver` (4h), baseline refreshes landing pattern_match at 93.5 % (OpenAI gpt-4o-mini) and 100 % (Anthropic claude-haiku-4-5) (4i / 4j / 4l), role-noun few-shot enrichment for hallucination_bait (4j), nightly CI workflow (4k) with two-row provider matrix (4l). End-to-end cache-hit plumbing proven against Sonnet 4.5 (99.5 % cache-read on warm call). |
