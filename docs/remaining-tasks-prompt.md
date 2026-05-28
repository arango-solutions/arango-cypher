# Multi-Subagent Task Decomposition: Remaining Work

> **Archived (2026-05-26).** This document predates the v0.3/v0.4 waves. Most workstreams here (named paths, EXISTS, regex, write clauses, NL hardening, visual mapping editor, etc.) are **shipped**. For current status use [`implementation_plan.md`](./implementation_plan.md), [`python_prd.md`](./python_prd.md), and [`2026-05-26-project-assessment.md`](./2026-05-26-project-assessment.md). Active follow-ups: Wave 8+ multi-tenant (MT-2..MT-8), Wave 11 transpiler polish, WP-19 staging deploy.

## Dependency Analysis

The remaining work decomposes into **6 independent workstreams** that can execute in parallel, plus a final integration pass. Each workstream touches distinct files with minimal overlap.

### File Ownership Map

| Workstream | Primary Files | Shared (read-only) |
|------------|--------------|---------------------|
| WS-1: Cypher Language (Transpiler) | `arango_cypher/translate_v0.py`, `tests/fixtures/cases/` | `arango_query_core/mapping.py` |
| WS-2: Built-in Functions | `arango_cypher/translate_v0.py` (functions section only) | `arango_query_core/mapping.py` |
| WS-3: Write Clauses (CREATE/SET/DELETE/MERGE) | `arango_cypher/translate_v0.py` (write section only) | `arango_query_core/mapping.py` |
| WS-4: UI Enhancements | `ui/src/components/`, `ui/src/lang/` | `ui/src/api/` |
| WS-5: NL Pipeline Quality | `arango_cypher/nl2cypher.py` | `arango_query_core/mapping.py` |
| WS-6: Infrastructure & Testing | `tests/`, `arango_cypher/service.py`, docs | — |

> **Conflict risk:** WS-1, WS-2, and WS-3 all modify `translate_v0.py`. To avoid merge conflicts, they should target non-overlapping sections of the file, or be run sequentially within a single agent.

---

## Workstream 1: Cypher Language Gaps (Transpiler Core)

**Goal:** Expand the supported Cypher subset in `translate_v0.py`.

**Priority order** (each builds on the previous):

### Task 1.1: Named Paths + Path Functions
- Parse `p = (a)-[:R]->(b)` assignment syntax
- Store path variable → list of vertex/edge variables
- Implement `length(p)` → `LENGTH(p.edges)`
- Implement `nodes(p)` → `p.vertices`  
- Implement `relationships(p)` → `p.edges`
- Add golden tests in `tests/fixtures/cases/named_paths.yml`

### Task 1.2: EXISTS / Pattern Predicates
- `WHERE EXISTS((n)-[:R]->())` → AQL subquery `LENGTH(FOR v IN 1..1 OUTBOUND n @@edge RETURN 1) > 0`
- `WHERE (n)-[:R]->()` shorthand (pattern predicate) → same translation
- Add golden tests

### Task 1.3: Regex `=~`
- `WHERE n.name =~ 'pattern'` → `REGEX_TEST(n.name, 'pattern', true)`
- Handle Cypher regex flavor differences vs. AQL regex
- Add golden tests

### Task 1.4: Aggregation in RETURN
- Currently requires `WITH` pipeline; enable `RETURN count(n), collect(n.name)` directly
- Should emit `COLLECT` + `RETURN` without intermediate `WITH`
- Add golden tests

### Task 1.5: OPTIONAL MATCH Multi-Segment
- Extend to support `OPTIONAL MATCH (a)-[:R1]->(b)-[:R2]->(c)`
- Currently only single-segment `(a)-[:R]->(b)` works
- Add golden tests

### Task 1.6: RETURN DISTINCT Multi-Item
- `RETURN DISTINCT a.name, b.name` → should work with multiple projection items
- Currently limited to single item
- Add golden tests

### Task 1.7: List Comprehensions
- `[x IN list WHERE pred | expr]` → `FOR x IN list FILTER pred RETURN expr`
- Add golden tests

### Task 1.8: Pattern Comprehensions
- `[(a)-[:R]->(b) | b.name]` → subquery
- Add golden tests

**Test command:** `pytest tests/ -x -q --ignore=tests/test_service.py --ignore=tests/test_tck.py -m "not integration and not tck"`

---

## Workstream 2: Built-in Functions

**Goal:** Add missing Cypher built-in function translations to the function dispatch table in `translate_v0.py`.

### Task 2.1: Type/identity functions
- `type(r)` → read `r.typeField` or `r._type` depending on mapping
- `id(n)` → `n._key` (or `n._id` depending on convention)
- `labels(n)` → `[n.typeField]` for LPG or `["Label"]` for PG
- `keys(n)` → `ATTRIBUTES(n, true)`
- `properties(n)` → `UNSET(n, "_id", "_key", "_rev")`

### Task 2.2: Conversion functions
- `toString(expr)` → `TO_STRING(expr)`
- `toInteger(expr)` → `TO_NUMBER(expr)` 
- `toFloat(expr)` → `TO_NUMBER(expr)`
- `toBoolean(expr)` → `TO_BOOL(expr)`

### Task 2.3: List functions
- `head(list)` → `FIRST(list)`
- `tail(list)` → `SLICE(list, 1)`
- `last(list)` → `LAST(list)`
- `range(start, end)` → `RANGE(start, end)`
- `range(start, end, step)` → `RANGE(start, end, step)`
- `reverse(list)` → `REVERSE(list)`

### Task 2.4: Add golden tests for all new functions

**Test command:** same as WS-1

---

## Workstream 3: Write Clauses

**Goal:** Implement CREATE, SET, DELETE, MERGE in the transpiler.

### Task 3.1: CREATE (nodes)
- `CREATE (n:Person {name: "Alice"})` → `INSERT {name: "Alice", typeField: "Person"} INTO @@collection`
- Handle property mapping (conceptual → physical field names)
- Return the created document

### Task 3.2: CREATE (relationships)
- `CREATE (a)-[:KNOWS {since: 2020}]->(b)` → `INSERT {_from: a._id, _to: b._id, since: 2020} INTO @@edgeCollection`
- Handle DEDICATED_COLLECTION vs GENERIC_WITH_TYPE (add typeField/typeValue)

### Task 3.3: SET
- `SET n.name = "Bob"` → `UPDATE n WITH {name: "Bob"} IN @@collection`
- `SET n += {name: "Bob"}` → merge semantics
- `SET n:Label` → update type discriminator

### Task 3.4: DELETE / DETACH DELETE
- `DELETE n` → `REMOVE n IN @@collection`
- `DETACH DELETE n` → remove all connected edges first, then remove node

### Task 3.5: MERGE
- `MERGE (n:Person {name: "Alice"})` → upsert pattern
- `ON CREATE SET ...` / `ON MATCH SET ...` → `UPSERT ... INSERT ... UPDATE ...`

### Task 3.6: Golden tests for all write clauses

**Test command:** same as WS-1

---

## Workstream 4: UI Enhancements

**Goal:** Complete the remaining UI features from the v0.3/v0.4 roadmap.

### Task 4.1: Cytoscape.js Results Graph View
- Replace custom SVG graph with Cytoscape.js
- Extract `_id`, `_from`, `_to` from results to build graph
- Node labels from entity type, edge labels from relationship type
- Pan/zoom, click-to-inspect

### Task 4.2: Visual Mapping Graph Editor (Cytoscape.js)
- Replace read-only SVG schema graph with Cytoscape.js
- Bidirectional sync: edits in graph → update JSON, edits in JSON → update graph
- Add/edit/delete entities and relationships via graph UI

### Task 4.3: Variable-Use Highlighting
- When cursor is on a variable in Cypher editor, highlight all occurrences
- Show corresponding AQL variable highlighted in AQL editor

### Task 4.4: Hover Documentation
- Hover over Cypher keywords/functions → show documentation tooltip
- Hover over `arango.*` functions → show ArangoDB docs link

### Task 4.5: AQL Format/Prettify
- "Format" button in AQL editor toolbar
- Apply `_reindent_aql` + additional formatting rules

### Task 4.6: Profile-Aware Warnings
- After running Profile, highlight slow operations in AQL editor
- Show performance annotations (rows scanned, time) inline

### Task 4.7: Multi-Statement Support
- Allow multiple Cypher statements separated by `;`
- Execute sequentially, show results per statement

**Install:** `cd ui && npm install cytoscape @types/cytoscape`

---

## Workstream 5: NL Pipeline Quality

**Goal:** Improve NL→Cypher and NL→AQL generation quality and robustness.

### Task 5.1: Fix NL→Cypher Prompt Leaking Physical Properties
- `_build_schema_summary()` should use ONLY conceptual schema per §1.2
- Remove any physical field names, collection names, type fields from the Cypher prompt
- The transpiler handles physical mapping

### Task 5.2: LLM Validation/Retry Loop for NL→Cypher
- After LLM generates Cypher, parse it with the ANTLR parser
- If parse fails, retry with error context (up to 2 retries)
- If retry fails, return the best attempt with a warning

### Task 5.3: Pluggable LLM Provider Interface
- Define abstract `LLMProvider` protocol properly
- Implement `OpenAIProvider`, `OpenRouterProvider`, `AnthropicProvider`
- Provider selection from `.env` (`LLM_PROVIDER=openai|openrouter|anthropic`)

### Task 5.4: NL→AQL Validation with Dry-Run
- After LLM generates AQL, validate via `db.aql.explain()` (dry-run parse)
- If validation fails, retry with the ArangoDB error message as context
- Improves first-attempt success rate significantly

**Test approach:** Integration tests against live ArangoDB

---

## Workstream 6: Infrastructure & Testing

**Goal:** Improve test coverage, security, and documentation.

### Task 6.1: openCypher TCK Harness
- Write clauses (WS-3) are the main blocker
- After CREATE/SET land, re-run TCK and report pass rate
- Target: ≥ 25% pass rate

### Task 6.2: Security Hardening
- Make CORS origins configurable via `ARANGO_CYPHER_CORS_ORIGINS` env var
- Error sanitization: strip hostnames/credentials from error messages
- Add `ARANGO_CYPHER_PUBLIC_MODE` to disable `/connect/defaults`
- Rate limiting on LLM endpoints

### Task 6.3: Index Population in Heuristic Builder
- `_build_heuristic_mapping()` should query `db.collection(name).indexes()`
- Populate `IndexInfo` entries in the mapping for each collection
- Currently only the analyzer path populates indexes

### Task 6.4: OWL Round-Trip Completion
- `mapping_bundle_for()` should read `.owl.ttl` files
- `schema_summary()` should optionally include OWL data
- Standalone library function for OWL generation (not just endpoint)

### Task 6.5: PRD Status Update
- After all workstreams complete, update all status tables in `docs/python_prd.md`
- Update changelog with new entries

---

## Execution Strategy

### Phase 1 — Parallel (no conflicts)
Launch simultaneously:
- **WS-4** (UI — completely independent files)
- **WS-5** (NL pipeline — `nl2cypher.py` only)
- **WS-6** (Infrastructure — tests, service, docs)

### Phase 2 — Sequential within one agent (shared `translate_v0.py`)
Run in order:
1. **WS-2** (Built-in functions — smallest diff, isolated function table)
2. **WS-1** (Cypher language gaps — larger changes, clause-level)
3. **WS-3** (Write clauses — new clause handlers)

### Phase 3 — Integration
- Run full test suite
- Update PRD status tables
- Fix any cross-workstream issues

### Estimated Effort

| Workstream | Complexity | Files Changed | New Test Cases |
|------------|-----------|---------------|----------------|
| WS-1: Cypher Language | High | 1-2 | ~40 golden |
| WS-2: Built-in Functions | Medium | 1 | ~15 golden |
| WS-3: Write Clauses | High | 1-2 | ~25 golden |
| WS-4: UI Enhancements | High | 5-8 | — |
| WS-5: NL Quality | Medium | 1-2 | ~5 integration |
| WS-6: Infrastructure | Medium | 3-5 | ~10 |
