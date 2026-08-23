# Multi-Sub-Agent Implementation Prompts — arango-cypher-py v0.2

Date: 2026-04-11
Derived from: [`implementation_plan.md`](./implementation_plan.md), [`python_prd.md`](./python_prd.md)

---

## How to use this document

This document contains **ready-to-use prompts** for launching parallel sub-agents to implement the v0.2 work packages. Each prompt is self-contained: it includes all the context a sub-agent needs without reading prior conversation history.

### Orchestration steps

1. **Wave 1 (parallel)**: Launch WP-1, WP-2, WP-3 simultaneously — they have no dependencies on each other
2. **Merge Wave 1**: Review and merge each agent's output. Run `pytest -m "not integration and not tck"` to verify no regressions
3. **Wave 2 (parallel)**: Launch WP-5, WP-4, WP-8 simultaneously — WP-5 depends on WP-1 (CREATE), WP-4 depends on WP-3 (schema analyzer)
4. **Merge Wave 2**: Review and merge
5. **Wave 3 (sequential)**: Launch WP-6 (TCK coverage), then WP-7 (Movies expansion) — both depend on prior waves and are iterative/exploratory

### Launching a sub-agent

Use the Task tool with `subagent_type: "best-of-n-runner"` for isolated branches, or `subagent_type: "generalPurpose"` for direct workspace changes.

---

## Shared context block

> **Include this block at the top of every sub-agent prompt.** It provides the codebase layout, conventions, and quality rules that all agents must follow.

```
SHARED CONTEXT — arango-cypher-py

## Project overview
Python-native Cypher → AQL transpiler for ArangoDB. Translates openCypher queries into
ArangoDB Query Language (AQL) using a conceptual→physical schema mapping. Supports PG
(types-as-collections), LPG (generic collections + type field), and hybrid physical models.

## Repository layout
arango_cypher/          # Main transpiler package
  __init__.py           # Public API re-exports: translate, execute, validate_cypher_profile, get_cypher_profile
  api.py                # Public functions: translate(), execute(), validate_cypher_profile(), TranspiledQuery
  parser.py             # ANTLR4 wrapper: parse_cypher(cypher) -> parse tree
  translate_v0.py       # Core translator: translate_v0(cypher, mapping, params, options) -> AqlQuery
  service.py            # FastAPI app with 12 endpoints
  profile.py            # Cypher profile manifest builder
  extensions/           # arango.* extension compilers (search, vector, geo, document, procedures)
    __init__.py          # register_all_extensions(registry)

arango_query_core/      # Shared core types (no transpiler logic)
  __init__.py           # Exports: MappingBundle, MappingResolver, MappingSource, CoreError, AqlQuery, ExtensionRegistry, ExtensionPolicy
  mapping.py            # MappingBundle dataclass, MappingResolver class
  exec.py               # AqlExecutor (wraps python-arango cursor)
  aql.py                # AqlQuery dataclass
  errors.py             # CoreError exception class
  extensions.py         # ExtensionRegistry, ExtensionPolicy

grammar/Cypher.g4       # openCypher ANTLR4 grammar (source of truth for parser)

tests/
  helpers/
    corpus.py           # CorpusCase dataclass, load_cases_from_file(), iter_cases()
    mapping_fixtures.py # mapping_bundle_for(name) -> MappingBundle (loads tests/fixtures/mappings/{name}.export.json)
  fixtures/
    cases/              # YAML golden test cases (version: 1, cases: [{id, name, mapping_fixture, cypher, expected: {aql, bind_vars}}])
    mappings/           # JSON export mapping fixtures (pg.export.json, lpg.export.json, hybrid.export.json, etc.)
    datasets/           # Seed data for integration tests
  integration/          # Tests requiring ArangoDB (@pytest.mark.integration)
  tck/                  # openCypher TCK harness
  conftest.py           # Shared fixtures (corpus_cases loads all YAML cases)

ui/                     # React + TypeScript frontend (Vite)
  src/
    App.tsx
    api/client.ts       # HTTP client for FastAPI
    api/store.ts        # Zustand-like state (localStorage persistence)
    components/         # CypherEditor, AqlEditor, ResultsPanel, MappingPanel, ConnectionDialog
    lang/               # cypher.ts (syntax), aql.ts (syntax), cypher-completion.ts (autocompletion)

docs/
  python_prd.md         # PRD (§6.4 = Cypher subset, §7.5 = error taxonomy, §8.2 = TCK, §10 = roadmap)
  implementation_plan.md # Work packages with dependencies

## Key types
- MappingBundle(conceptual_schema, physical_mapping, metadata, source, owl_turtle)
- MappingResolver(mapping: MappingBundle) — .resolve_entity(label), .resolve_relationship(type), .resolve_properties(label), .edge_constrains_target(rel, label, dir), .schema_summary()
- AqlQuery(text, bind_vars, debug)
- TranspiledQuery(aql, bind_vars, warnings, debug)
- CoreError(message, code) — codes: UNSUPPORTED, INVALID_ARGUMENT, PARSE_ERROR, NOT_IMPLEMENTED
- ExtensionRegistry — .register_function(name, compiler), .register_procedure(name, compiler), .compile_function(name, args), .compile_procedure(name, args)

## Golden test conventions
- YAML fixture file in tests/fixtures/cases/{feature}.yml
- Format:
    version: 1
    cases:
      - id: C100  # unique across ALL fixture files
        name: descriptive name
        mapping_fixture: pg  # matches tests/fixtures/mappings/{name}.export.json
        extensions_enabled: false
        cypher: |
          MATCH (n:User) RETURN n
        expected:
          aql: |
            FOR n IN @@collection
              RETURN n
          bind_vars:
            "@collection": users

- Test runner file: tests/test_translate_{feature}_goldens.py
- Pattern:
    @pytest.mark.parametrize("case_id", ["C100", "C101", ...])
    def test_translate_{feature}_goldens(corpus_cases, case_id):
        case = next(c for c in corpus_cases if c.id == case_id)
        mapping = mapping_bundle_for(case.mapping_fixture)
        out = translate(case.cypher, mapping=mapping, params=case.params)
        assert out.aql.strip() == case.expected_aql.strip()
        assert out.bind_vars == case.expected_bind_vars

## Quality rules
1. All existing tests must pass after your changes: pytest -m "not integration and not tck"
2. Run ruff check . — no lint errors
3. Never string-interpolate collection names or user values into AQL — always use bind parameters (@@collection for collections, @param for values)
4. Do not add comments that narrate what code does — only explain non-obvious intent
5. Do not create documentation files unless the work package specifies it
6. Update the PRD §6.4 Cypher subset table if you add support for a new construct
7. SCHEMA ANALYZER NO-WORKAROUND POLICY: arangodb-schema-analyzer (~/code/arango-schema-mapper)
   is the canonical source for reverse-engineering ontologies from ArangoDB schemas.
   If the analyzer output is incomplete, incorrect, or missing a capability you need:
   - Do NOT work around it in transpiler code (no shims, no fallback heuristics, no special cases)
   - File a bug or feature report against ~/code/arango-schema-mapper with:
     the database schema, current analyzer output, what you need, and a Cypher example
   - Document the gap in the PRD §5.3 status table referencing the issue
   - Fail gracefully with CoreError(code="ANALYZER_GAP") until the upstream fix lands
```

---

## Wave 1 prompts (parallel, no dependencies)

---

### WP-1: CREATE clause

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-1 — Implement CREATE clause translation

You are implementing the CREATE clause for the Cypher→AQL transpiler. This is the highest-priority
work package because it unblocks TCK testing and dataset expansion.

### What to implement

Translate Cypher CREATE patterns into AQL INSERT statements:

1. Node creation: CREATE (n:Person {name: "Alice", age: 30})
   → INSERT {name: @v1, age: @v2} INTO @@collection LET n = NEW
   For LPG-style entities, inject the typeField/typeValue into the inserted document.

2. Relationship creation: CREATE (a)-[:KNOWS {since: 2020}]->(b)
   → INSERT {_from: a._id, _to: b._id, since: @v3} INTO @@edgeCollection

3. Multi-element CREATE: CREATE (a:Person {name: "Alice"}), (b:Person {name: "Bob"}), (a)-[:KNOWS]->(b)
   → Sequential INSERTs with LET bindings for created documents

4. CREATE after MATCH: MATCH (a:Person {name: "Alice"}) CREATE (a)-[:KNOWS]->(b:Person {name: "Bob"})
   → FOR loop for MATCH, then INSERT for new nodes/relationships

5. CREATE with RETURN: CREATE (n:Person {name: "Alice"}) RETURN n
   → INSERT ... LET n = NEW ... RETURN n

### Where to make changes

FILE: arango_cypher/translate_v0.py
- In _translate_single_query() around lines 112-114, there is a guard:
    if spq.oC_UpdatingClause():
        raise CoreError("Updating clauses are not supported in v0", code="UNSUPPORTED")
  Modify this to allow CREATE but still reject SET/DELETE/MERGE/REMOVE.

- Add a new function _compile_create(updating_clause, resolver, bind_vars, var_env) -> list[str]
  that compiles a single CREATE clause into AQL lines.

- Add a new function _compile_create_pattern(pattern, resolver, bind_vars, var_env) -> list[str]
  that handles a single node or relationship pattern within CREATE.

- Wire _compile_create into the query dispatch so that CREATE-only queries and MATCH+CREATE
  queries both work.

FILE: arango_query_core/aql.py (if needed)
- Add INSERT rendering helpers if the existing AqlQuery structure needs extension.

DO NOT MODIFY these functions (owned by WP-2):
- _compile_function_invocation
- _compile_agg_expr
- _append_return (for aggregation changes)
- _parse_skip_limit

### Mapping resolution for CREATE

Use MappingResolver to determine the physical collection:
- resolver.resolve_entity(label) returns the entity mapping with collectionName and style
- For COLLECTION style: INSERT INTO the named collection
- For LABEL style: INSERT INTO the generic collection AND set the typeField/typeValue on the document
- resolver.resolve_relationship(type) returns the relationship mapping with edgeCollectionName and style
- For DEDICATED_COLLECTION: INSERT INTO the edge collection
- For GENERIC_WITH_TYPE: INSERT INTO the generic edge collection AND set the type field

### Golden tests to create

FILE: tests/fixtures/cases/create.yml
Create at least these cases (use IDs C200-C219 to avoid conflicts):

- C200: CREATE single node with label and properties (PG mapping)
- C201: CREATE single node with label and properties (LPG mapping)
- C202: CREATE two nodes and a relationship between them
- C203: MATCH then CREATE relationship
- C204: CREATE with RETURN
- C205: CREATE node with parameter values: CREATE (n:User {name: $name})
- C206: CREATE relationship with properties
- C207: CREATE multiple disconnected nodes
- C208: MATCH+CREATE with WHERE filter on match side
- C209: CREATE node with multiple labels (LPG mapping, if supported, otherwise skip)

FILE: tests/test_translate_create_goldens.py
Follow the standard pattern from test_translate_basic_goldens.py.

### Integration test

FILE: tests/integration/test_create_smoke.py
- Seed an empty database
- Translate and execute a CREATE query
- Read back the created documents and verify they exist with correct properties
- Mark with @pytest.mark.integration

### Acceptance criteria
- All new golden tests pass
- All existing golden tests still pass (no regressions)
- Integration test passes against ArangoDB
- CREATE (n:Label {props}) RETURN n works end-to-end
- MATCH (a) CREATE (a)-[:REL]->(b:Label {props}) works end-to-end
- ruff check . passes
```

---

### WP-2: Aggregation in RETURN + built-in functions

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-2 — Aggregation in RETURN + built-in functions

You are completing the aggregation and built-in function support in the transpiler.

### Part 1: Aggregation in RETURN

Currently aggregation functions (COUNT, SUM, AVG, MIN, MAX, COLLECT) only work inside WITH
clauses via _compile_agg_expr(). They need to work in RETURN as well.

Examples:
  MATCH (n:Person) RETURN COUNT(n)
  → RETURN LENGTH(FOR n IN @@collection RETURN 1)
  or: FOR n IN @@collection COLLECT WITH COUNT INTO __count RETURN __count

  MATCH (n:Person) RETURN n.city, COUNT(n)
  → FOR n IN @@collection COLLECT city = n.city WITH COUNT INTO __count RETURN {city: city, count: __count}

  MATCH (n:Person) RETURN COUNT(DISTINCT n.city)
  → FOR n IN @@collection COLLECT city = n.city INTO __group RETURN LENGTH(__group) — or similar

Also fix RETURN DISTINCT with multiple projection items (currently only single-item):
  MATCH (n:Person) RETURN DISTINCT n.name, n.city
  → FOR n IN @@collection RETURN DISTINCT {name: n.name, city: n.city}

### Part 2: Built-in functions

Add these to _compile_function_invocation() in translate_v0.py (around line ~2647).
Currently it handles: size→LENGTH, tolower→LOWER, toupper→UPPER, coalesce→NOT_NULL.

Add:
  type(r)      → relationship type from the type field. This is mapping-dependent:
                  For DEDICATED_COLLECTION: return the Cypher relationship type as a string literal
                  For GENERIC_WITH_TYPE: return r[typeField]
                  NOTE: this is complex — you may need to pass the resolver/mapping context
                  into _compile_function_invocation or handle type() as a special case.
                  Simplest v0.2 approach: return r.relation or r.type (the physical type field).
  id(n)        → n._id
  labels(n)    → mapping-dependent. Simplest: [PARSE_IDENTIFIER(n._id).collection] for COLLECTION,
                  or n[typeField] wrapped in array for LABEL style.
                  Simplest v0.2 approach: return ATTRIBUTES(n) filtered, or just n._id-based.
                  NOTE: exact semantics are hard; start with id(n)→n._id and defer labels(n) if complex.
  keys(n)      → ATTRIBUTES(n)
  properties(n)→ UNSET(n, "_id", "_key", "_rev")
  toString(e)  → TO_STRING(e)
  toInteger(e) → TO_NUMBER(e)
  toFloat(e)   → TO_NUMBER(e)
  toBoolean(e) → TO_BOOL(e)

### Part 3: LIMIT/SKIP with expressions

In _parse_skip_limit() (around line ~2126), currently only integer literals are accepted.
Extend to accept:
- Parameter references: LIMIT $count → LIMIT @count
- Simple arithmetic: LIMIT 5 + 5 → LIMIT 10 (compile the expression)

### Where to make changes

FILE: arango_cypher/translate_v0.py
- _append_return: detect aggregation calls in RETURN items, compile using COLLECT
- _compile_function_invocation: add new function mappings
- _parse_skip_limit: accept expressions and parameters
- May need a helper _detect_aggregation(expr) → bool to check if an expression contains aggregation

DO NOT MODIFY these areas (owned by WP-1):
- The updating clause guard (lines 112-114)
- Do not add any CREATE/INSERT/write-related code

### Golden tests to create

FILE: tests/fixtures/cases/aggregation_return.yml (IDs C220-C234)
- C220: RETURN COUNT(n)
- C221: RETURN n.city, COUNT(n)
- C222: RETURN COUNT(DISTINCT n.name)
- C223: RETURN SUM(n.age), AVG(n.age)
- C224: RETURN DISTINCT n.name, n.city (multi-column distinct)
- C225: RETURN MIN(n.age), MAX(n.age)

FILE: tests/fixtures/cases/builtin_functions.yml (IDs C235-C249)
- C235: RETURN id(n)
- C236: RETURN keys(n)
- C237: RETURN properties(n)
- C238: RETURN toString(n.age)
- C239: RETURN toInteger(n.ageStr)
- C240: WHERE toUpper(n.name) = "ALICE" (already works — regression test)
- C241: WHERE size(n.friends) > 3 (already works — regression test)

FILE: tests/test_translate_aggregation_return_goldens.py
FILE: tests/test_translate_builtin_functions_goldens.py

### Acceptance criteria
- Aggregation works in RETURN, not just WITH
- RETURN DISTINCT works with multiple items
- All new built-in functions translate to correct AQL
- LIMIT $param works
- All existing golden tests still pass
- ruff check . passes
```

---

### WP-3: Schema analyzer integration

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-3 — Wire up arangodb-schema-analyzer as an optional dependency

You are integrating the arangodb-schema-analyzer library to enable automatic mapping
acquisition from a live ArangoDB database.

### Background

The arangodb-schema-analyzer library (located at ~/code/arango-schema-mapper) provides:
- AgenticSchemaAnalyzer class with library API
- Tool contract: schema_analyzer.tool.run_tool({"operation": "export", ...}) → JSON export
- Tool contract: schema_analyzer.tool.run_tool({"operation": "owl", ...}) → OWL Turtle string
- The export JSON format matches the MappingBundle structure: conceptualSchema, physicalMapping, metadata

Currently the transpiler requires users to supply a MappingBundle manually. This WP adds
automatic mapping acquisition.

### What to implement

1. NEW FILE: arango_cypher/schema_acquire.py

   def classify_schema(db) -> str:
       """Fast heuristic: sample collections and classify as 'pg', 'lpg', 'hybrid', or 'unknown'.
       
       Strategy:
       - List all document collections and edge collections
       - For document collections: sample N docs, check if they have a common 'type'/'labels' field
         - If all docs have a type field with varying values → LPG
         - If collection names match conceptual types (no type field) → PG
       - For edge collections: check if they're dedicated or have a type/relation field
       - If mixed → hybrid
       - If unclear → unknown
       """

   def acquire_mapping_bundle(db, *, include_owl: bool = False) -> MappingBundle:
       """Call arangodb-schema-analyzer to produce a MappingBundle from a live database.
       
       Steps:
       1. Try to import arangodb_schema_analyzer (optional dependency)
       2. Call run_tool({"operation": "export", "connection": {...}}) or use AgenticSchemaAnalyzer
       3. Transform the export JSON into a MappingBundle
       4. If include_owl: also call operation="owl" and set MappingBundle.owl_turtle
       5. Return the populated MappingBundle
       
       If arangodb-schema-analyzer is not installed, raise ImportError with a helpful message.
       """

   def get_mapping(db, *, strategy: str = "auto", use_analyzer: bool = False) -> MappingBundle:
       """3-tier mapping acquisition.
       
       strategy="auto": classify_schema() first; if 'pg' or 'lpg', build simple mapping;
                        if 'hybrid' or 'unknown', call acquire_mapping_bundle()
       strategy="analyzer": always call acquire_mapping_bundle()
       strategy="heuristic": never call analyzer, build best-effort mapping from heuristics
       
       use_analyzer=True: force analyzer regardless of strategy
       """

   # In-memory cache
   _mapping_cache: dict[str, tuple[MappingBundle, float]] = {}
   CACHE_TTL_SECONDS = 300  # 5 minutes

   def _cache_key(db) -> str:
       """Schema fingerprint: hash of sorted collection names."""

2. FILE: arango_cypher/api.py — add get_mapping() re-export:
   from .schema_acquire import get_mapping  # re-export

3. FILE: arango_cypher/__init__.py — add get_mapping to __all__

4. FILE: pyproject.toml — add optional dependency group:
   [project.optional-dependencies]
   analyzer = ["arangodb-schema-analyzer"]
   
   Also add typer and rich for the CLI (WP-4 will use them):
   cli = ["typer>=0.9.0", "rich>=13.0.0"]

5. FILE: arango_cypher/service.py — update schema_introspect():
   When arangodb-schema-analyzer is available, use acquire_mapping_bundle() instead of
   the basic _sample_properties() approach. Fall back to current behavior if not installed.

### Building simple mappings from heuristics

When classify_schema returns 'pg' or 'lpg' and the user doesn't force the analyzer:

For PG:
- Each document collection → entity with style=COLLECTION, collectionName=collection_name
- Infer conceptual label from collection name (e.g., 'users' → 'User', 'persons' → 'Person')
- Each edge collection → relationship with style=DEDICATED_COLLECTION
- Sample docs for properties

For LPG:
- The document collection → entities with style=LABEL, typeField detected from samples
- The edge collection → relationships with style=GENERIC_WITH_TYPE, typeField detected

### Tests

FILE: tests/test_schema_acquire.py
- Test classify_schema with mocked collection/document data
- Test acquire_mapping_bundle with mocked analyzer (mock the import)
- Test get_mapping with strategy="auto" routing
- Test caching (second call returns cached result within TTL)
- Test ImportError when analyzer is not installed

DO NOT write integration tests that require the actual analyzer library — those are
for a separate integration test file.

### CRITICAL: No-workaround policy

The arangodb-schema-analyzer is the CANONICAL SOURCE for ontology extraction from ArangoDB
schemas. If you encounter ANY situation where the analyzer's output is incomplete, incorrect,
or missing a capability that the transpiler needs:

1. Do NOT work around it. No shims, no fallback logic that reimplements analyzer behavior,
   no special-case handling that papers over a gap.
2. File a bug or feature report against ~/code/arango-schema-mapper. Include:
   - The database schema (collections, sample documents) that triggered the gap
   - What the analyzer currently produces (or fails to produce)
   - What the transpiler needs
   - A Cypher query example that would benefit
3. In the transpiler code, raise CoreError("...", code="ANALYZER_GAP") with a message
   referencing the report.
4. Document the gap in PRD §5.3 implementation status table.

This policy ensures the analyzer improves at the source rather than being papered over
downstream. The heuristic classifier (classify_schema) is for ROUTING (deciding whether to
call the analyzer), not for REPLACING the analyzer's output.

### Acceptance criteria
- get_mapping(db) works with strategy="auto" and strategy="heuristic"
- acquire_mapping_bundle(db) works when arangodb-schema-analyzer is installed
- Helpful ImportError when analyzer is not installed and strategy="analyzer"
- classify_schema correctly identifies PG/LPG for simple databases
- Caching works with TTL
- No workarounds for analyzer gaps — any gap raises CoreError(code="ANALYZER_GAP")
- All existing tests still pass
- ruff check . passes
```

---

## Wave 2 prompts (launch after Wave 1 merges)

---

### WP-5: TCK harness improvements

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-5 — Improve the TCK harness to run real openCypher scenarios

The TCK (Technology Compatibility Kit) harness exists at tests/tck/ but currently only runs
a trivial sample.feature. With CREATE now implemented (WP-1, already merged), the harness
can execute "Given having executed" setup steps. Your job is to make it robust.

### Current state

FILE: tests/tck/gherkin.py — parses .feature files into Feature/Scenario/Step dataclasses
FILE: tests/tck/runner.py — executes scenarios: setup → translate → execute → compare results
FILE: tests/tck/test_tck_harness_smoke.py — runs sample.feature
FILE: tests/tck/features/sample.feature — one trivial scenario
FILE: scripts/download_tck.py — downloads .feature files from GitHub

### What to implement

1. FILE: tests/tck/gherkin.py — Scenario Outline expansion
   Currently only handles Scenario:. Add:
   - Parse "Scenario Outline:" headers
   - Parse "Examples:" tables (pipe-delimited rows with a header row)
   - Expand each Examples row into a concrete Scenario by substituting <placeholder> tokens
     in the step text and doc strings
   Example:
     Scenario Outline: Return literal
       When executing query:
         """
         RETURN <literal>
         """
       Then the result should be:
         | value      |
         | <expected> |
     Examples:
       | literal | expected |
       | 1       | 1        |
       | 'foo'   | 'foo'    |
   → expands to 2 concrete Scenario objects

2. FILE: tests/tck/runner.py — Given having executed
   Currently "Given having executed" steps are SKIPPED. Change to:
   - Extract the Cypher query from the doc string
   - Translate using translate() with a TCK-specific LPG mapping (nodes/edges collections)
   - Execute the resulting AQL against the test database
   - Support multiple sequential "And having executed" steps
   - If translation fails (unsupported construct), mark scenario as SKIPPED (not FAILED)
   - If execution fails, mark as FAILED

3. NEW FILE: tests/tck/normalize.py — Result normalization
   TCK expected results use Neo4j conventions. Implement structural comparison:
   
   def normalize_expected_value(text: str) -> Any:
       """Parse a TCK expected value cell into a Python value.
       Handles: integers, floats, strings ('foo'), booleans (true/false),
       null, lists ([1, 2]), maps ({key: value}),
       node literals (:Label {prop: value}), relationship literals [:TYPE {prop: value}]"""
   
   def normalize_actual_value(value: Any) -> Any:
       """Normalize an ArangoDB result value for comparison.
       Strip _id, _key, _rev from documents. Normalize numeric types."""
   
   def results_match(actual_rows: list[dict], expected_table: list[dict], *, ordered: bool) -> tuple[bool, str]:
       """Compare actual query results against expected TCK table.
       Returns (match, explanation_if_mismatch)."""

4. FILE: tests/tck/runner.py — Error expectation scenarios
   Some TCK scenarios expect errors:
     Then a SyntaxError should be raised
     Then a TypeError should be raised at compile time
   Handle by catching CoreError or ArangoDB errors and comparing against expected category.

5. FILE: tests/tck/runner.py — Given parameters
   Parse "Given parameters are:" data tables into a dict and pass as params= to translate().

### TCK LPG mapping fixture

The TCK uses a simple model: all nodes in a "nodes" collection, all edges in an "edges" collection,
with type/relation fields. Create or verify:
FILE: tests/fixtures/mappings/tck_lpg.export.json
This should be a generic LPG mapping that the TCK runner uses for all scenarios.

### Tests

FILE: tests/tck/test_tck_harness_smoke.py — expand to test:
- Scenario Outline expansion produces correct number of scenarios
- Given having executed creates data that can be queried
- Result normalization handles integers, strings, null, nodes, relationships
- Error expectation scenarios correctly match

### Acceptance criteria
- Scenario Outline / Examples are expanded into concrete scenarios
- "Given having executed" runs CREATE queries to seed the graph
- Result comparison handles type normalization
- Error scenarios are handled (not just skipped)
- All existing tests still pass
- ruff check . passes
```

---

### WP-4: CLI

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-4 — Implement the CLI

Create a CLI entry point using typer + rich. The CLI provides four subcommands:
translate, run, mapping, doctor.

### Prerequisites
- WP-3 (schema analyzer) must be merged first — the mapping command uses get_mapping().
- typer and rich should be in pyproject.toml optional-dependencies (WP-3 adds them).

### What to implement

1. NEW FILE: arango_cypher/cli.py

   import typer
   from rich.console import Console
   from rich.table import Table
   
   app = typer.Typer(name="arango-cypher-py", help="Cypher → AQL transpiler for ArangoDB")
   
   @app.command()
   def translate(cypher: str = typer.Argument(None), ...):
       """Translate Cypher to AQL. Reads from stdin if no argument given."""
       # Read cypher from argument or stdin
       # Load mapping from --mapping-file or --mapping-json or acquire from DB
       # Call arango_cypher.translate()
       # Print AQL and bind vars (JSON)
   
   @app.command()
   def run(cypher: str = typer.Argument(None), ...):
       """Translate and execute Cypher against ArangoDB."""
       # Same as translate, but also connect and execute
       # Print results as rich Table (default) or JSON (--json flag)
   
   @app.command()
   def mapping(...):
       """Print mapping summary for a database."""
       # Connect to DB
       # Call get_mapping(db) (from WP-3)
       # Print summary (rich Table or JSON)
       # Optionally write OWL Turtle to file (--owl-output)
   
   @app.command()
   def doctor(...):
       """Check connectivity, collections, and schema analyzer availability."""
       # Test ArangoDB connection
       # List collections
       # Check if arangodb-schema-analyzer is importable
       # Report status with rich formatting

   Common options (shared across commands):
   --host (default: ARANGO_HOST env or localhost)
   --port (default: ARANGO_PORT env or 8529)
   --db (default: ARANGO_DB env or _system)
   --user (default: ARANGO_USER env or root)
   --password (default: ARANGO_PASSWORD env or empty)
   --mapping-file (path to JSON mapping file)
   --json (output as JSON instead of table)

2. FILE: pyproject.toml — add console_scripts:
   [project.scripts]
   arango-cypher-py = "arango_cypher.cli:app"

3. Stdin support: translate and run should detect piped input:
   if cypher is None:
       cypher = sys.stdin.read()

### Tests

FILE: tests/test_cli.py
Use typer.testing.CliRunner:
- test_translate_prints_aql: invoke translate with a simple MATCH query and mapping file
- test_translate_stdin: pipe cypher via stdin
- test_doctor_no_connection: doctor reports connection failure gracefully
- test_run_requires_connection: run without connection prints helpful error

### Acceptance criteria
- arango-cypher-py translate "MATCH (n:User) RETURN n" --mapping-file mapping.json prints AQL
- echo "MATCH (n:User) RETURN n" | arango-cypher-py translate --mapping-file mapping.json works
- arango-cypher-py doctor reports connectivity status
- All existing tests still pass
- ruff check . passes
```

---

### WP-8: UI parameter binding + query history

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-8 — Add parameter binding panel and query history to the UI

### Part 1: Parameter panel

NEW FILE: ui/src/components/ParameterPanel.tsx
- Scan the current Cypher text for $paramName tokens (regex: /\$([a-zA-Z_]\w*)/g)
- Display a list of detected parameters with JSON input fields
- When the user enters values, include them in the API request as a params object
- Persist parameter values in localStorage keyed by query hash

Integration:
- FILE: ui/src/App.tsx — add ParameterPanel below the CypherEditor
- FILE: ui/src/api/store.ts — add params: Record<string, unknown> to state
- FILE: ui/src/api/client.ts — include params in POST /translate and POST /execute requests
- FILE: arango_cypher/service.py — ensure TranslateRequest and ExecuteRequest models
  accept an optional params field and pass it to translate()

Also add a bind-vars display below the AQL editor showing the bind variables from the
translation result (read-only, formatted JSON).

### Part 2: Query history

NEW FILE: ui/src/components/QueryHistory.tsx
- Store last 50 queries in localStorage (cypher text + timestamp + truncated AQL preview)
- Display as a searchable list in a slide-out drawer
- Click to restore a previous query
- "Clear history" button

Integration:
- FILE: ui/src/api/store.ts — add history: Array<{cypher, timestamp, aqlPreview}> to state
- FILE: ui/src/App.tsx — add history drawer toggle button, save to history on each translate

### Part 3: Keyboard shortcuts

FILE: ui/src/components/CypherEditor.tsx or ui/src/App.tsx
Add CodeMirror keymaps or global event handlers:
- Ctrl/Cmd+Enter → Translate (call makeRequest with mode="translate")
- Shift+Enter → Execute (call makeRequest with mode="execute")

### Acceptance criteria
- Parameters detected from Cypher text are shown in the panel
- Parameter values are sent to the API and used in translation
- Query history persists across browser sessions
- Clicking a history entry restores the query
- Keyboard shortcuts work
- All existing tests still pass
- ruff check . passes (if applicable to Python changes)
```

---

## Wave 3 prompts (sequential, after Waves 1+2)

---

### WP-6: TCK Match coverage

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-6 — Achieve ≥ 40% TCK Match scenario pass rate

This is an iterative task. You will download real TCK Match features, run them, triage failures,
and fix translator bugs until the pass rate reaches 40%.

### Process

1. Download Match features:
   python scripts/download_tck.py --only-match Match

2. Run the TCK:
   RUN_INTEGRATION=1 RUN_TCK=1 pytest tests/tck/ -v 2>&1 | head -200
   
3. For each failing scenario, categorize:
   - TRANSLATOR BUG: the Cypher is in our supported subset but the AQL is wrong → fix translate_v0.py
   - MISSING CONSTRUCT: the Cypher uses a construct we don't support yet → skip, document in SKIP_REASONS.md
   - RUNNER BUG: the harness misparses or miscompares → fix tests/tck/runner.py or normalize.py
   - NORMALIZATION BUG: actual results are correct but comparison fails → fix normalize.py

4. Fix and re-run iteratively.

5. When done, create:
   FILE: tests/tck/SKIP_REASONS.md — document each skipped scenario category and why

### What you can modify
- arango_cypher/translate_v0.py — fix bugs in existing translation logic
- tests/tck/runner.py — fix runner bugs
- tests/tck/normalize.py — fix normalization bugs
- tests/tck/gherkin.py — fix parser bugs

### What you should NOT do
- Do not implement entirely new Cypher clauses (MERGE, DELETE, etc.) — those are future WPs
- Do not modify the golden test fixtures — if a golden test breaks, your translator fix is wrong

### Acceptance criteria
- ≥ 40% of Match*.feature scenarios pass (non-skipped)
- All existing golden tests still pass
- SKIP_REASONS.md documents remaining gaps
- ruff check . passes
```

---

### WP-7: Movies dataset expansion

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-7 — Expand the Movies dataset to the full Neo4j Movies corpus

### What to implement

1. FILE: tests/fixtures/datasets/movies/lpg-data.json — EXPAND
   Convert the full Neo4j Movies dataset (~171 nodes, ~253 relationships) from
   the Neo4j CREATE script to LPG JSON format.
   
   Source: https://github.com/neo4j-graph-examples/movies/blob/main/scripts/import.cypher
   (or the CREATE statement version)
   
   Format (matches existing structure):
   {
     "nodes": [
       {"_key": "TomHanks", "type": "Person", "labels": ["Person", "Actor"], "name": "Tom Hanks", "born": 1956},
       {"_key": "ForrestGump", "type": "Movie", "labels": ["Movie"], "title": "Forrest Gump", "released": 1994, "tagline": "..."},
       ...
     ],
     "edges": [
       {"_from": "nodes/TomHanks", "_to": "nodes/ForrestGump", "relation": "ACTED_IN", "roles": ["Forrest"]},
       ...
     ]
   }

2. NEW FILE: tests/fixtures/datasets/movies/pg-data.json
   Same data in PG format: separate arrays per collection type.
   {
     "collections": {
       "persons": [{"_key": "TomHanks", "name": "Tom Hanks", "born": 1956}, ...],
       "movies": [{"_key": "ForrestGump", "title": "Forrest Gump", ...}, ...]
     },
     "edge_collections": {
       "acted_in": [{"_from": "persons/TomHanks", "_to": "movies/ForrestGump", "roles": ["Forrest"]}, ...],
       "directed": [{"_from": "persons/RobertZemeckis", "_to": "movies/ForrestGump"}, ...]
     }
   }

3. NEW FILE: tests/fixtures/datasets/movies/query-corpus.yml
   Extract ~15-20 example queries from Neo4j Movies documentation:
   - id: movies_001
     description: "Find actor by name"
     cypher: 'MATCH (a:Person {name: "Tom Hanks"}) RETURN a.name, a.born'
     dataset: movies
     mapping_fixture: movies_lpg
     expected_count: 1  # expected number of result rows

4. NEW FILE: tests/fixtures/mappings/movies_pg.export.json
   PG-style mapping for the Movies dataset with separate collections.

5. FILE: tests/integration/datasets.py — extend
   Add seed_movies_pg_dataset(db) that creates separate collections and seeds from pg-data.json.

6. NEW FILE: tests/integration/test_neo4j_movies_dataset.py
   Parametrized test that:
   - Loads query-corpus.yml
   - For each query, seeds the appropriate dataset (LPG or PG)
   - Translates the Cypher
   - Executes the AQL
   - Verifies result count matches expected_count
   - Run against BOTH movies_lpg and movies_pg mapping fixtures

### Acceptance criteria
- Full Movies dataset in both LPG and PG formats
- Query corpus with ≥ 15 queries
- All queries translate and execute correctly against both layouts
- All existing tests still pass
- ruff check . passes
```

---

## Wave 5 prompts — pre-WP-25 cleanup + unblock Wave 4a (parallel, ~½ day total)

**Goal:** knock out three small interlocking items so that (a) the implementation plan and PRD stop contradicting themselves, (b) product scope is unambiguous in the PRD, and (c) the `PromptBuilder` refactor that gates Wave 4a is in place. All three agents work on disjoint files and can run concurrently.

**Orchestration:** Launch S1, S2, S3 in parallel on separate branches off `main`. Merge order (alphabetical is fine — no true dependency): S1 → S2 → S3 → run full unit suite → commit. Wave 4a becomes launchable immediately afterward.

| Sub-agent | Files touched | Estimate |
|-----------|---------------|---------|
| S1 — plan hygiene | `docs/implementation_plan.md` | ~30 min |
| S2 — PRD product-scope edits | `docs/python_prd.md` | ~1 h |
| S3 — PromptBuilder refactor (≡ Wave 4-pre for WP-25) | `arango_cypher/nl2cypher.py`, `tests/test_nl2cypher_prompt_builder.py` | ~0.5 day |

---

### S1 — Implementation plan hygiene

```
{SHARED CONTEXT BLOCK — paste from top of document}

## Your task: S1 — Reconcile WP-19 duplicate and WP-20 stale status in the implementation plan

### Problem
`docs/implementation_plan.md` has two WP-level inconsistencies that pre-date the
WP-25 scoping commit:

1. **WP-19 duplicate.** The body of the plan (around line 584) has a full WP-19
   block titled "Arango Platform deployment enablement" that landed on
   2026-04-17. The status table at the bottom of the plan still has a row
   `| WP-19 | Translation caching | v0.4 | **Done** | 2026-04-13 |` from the
   earlier v0.4 sprint. Two different things share the number.

2. **WP-20 stale status.** The status table has
   `| WP-20 | Filter pushdown into traversals | v0.4 | Not started | |` but
   the 2026-04-15 "WS-F/G sprint" changelog in `docs/python_prd.md` explicitly
   records `Filter pushdown into traversals (PRUNE for variable-length,
   conservative rules)` as shipped, and PRD §7.7 / §7.8 / line ~1779 confirm
   it as **Done**.

### What to do

1. Reconcile WP-19:
   - The body-level WP-19 ("Arango Platform deployment enablement", PRD §15)
     is authoritative and must retain the number **WP-19** (PRD §15
     references it by that number in the 2026-04-17 changelog — grep to
     confirm before editing).
   - The pre-existing "Translation caching" entry needs a new home. Inspect
     `git log -- docs/implementation_plan.md` to see what number the original
     Translation caching WP held before it was displaced. If the git history
     shows it truly was WP-19 originally, renumber it to **WP-26** in the
     status table (next free number after WP-25 which we just added) and
     mark it **Done** with the original 2026-04-13 date and a short note
     referencing the 2026-04-13 changelog entry in the PRD. Do NOT alter
     the shipped code — this is purely a numbering-hygiene fix.

2. Fix WP-20 status:
   - Update the status row to `| WP-20 | Filter pushdown into traversals |
     v0.4 | **Done** | 2026-04-15 | (WS-F/G sprint — PRUNE for variable-length,
     conservative rules) |` — match the date and note style of nearby rows.
   - Double-check by reading PRD §7.7 / §7.8 / the v0.4 status line (~line
     1779) to confirm the feature is in fact shipped. If there's any doubt,
     stop and ask.

3. Grep for any other stale references:
   - `rg -n 'WP-19' docs/ arango_cypher/ arango_query_core/ tests/`
   - `rg -n 'WP-20' docs/ arango_cypher/ arango_query_core/ tests/`
   - Any prose that still says "WP-19 Translation caching" or "WP-20 not
     started" needs the same reconciliation.

4. Add a one-line note at the top of the status table (or in an adjacent
   comment) recording the reconciliation date (today) so future agents don't
   re-open the question.

### Constraints

- No code changes. Documentation only.
- No PRD changes (S2 owns the PRD). If you notice a PRD inconsistency while
  grepping, leave a `TODO(S2):` marker and move on.
- `ruff check .` is not applicable to pure markdown, but ensure no markdown
  linter complaints (`markdownlint-cli` if available; otherwise eyeball the
  table alignment).

### Acceptance

- `rg -c '^\| WP-19 \|' docs/implementation_plan.md` returns 1 (exactly one
  row for WP-19 in the status table, and it's the deployment one).
- `rg '^\| WP-20 \|' docs/implementation_plan.md` returns a row marked **Done**.
- No prose anywhere in the repo still calls WP-19 "Translation caching" or
  WP-20 "Not started".
- Diff touches only `docs/implementation_plan.md`.
```

---

### S2 — PRD product-scope edits

```
{SHARED CONTEXT BLOCK — paste from top of document}

## Your task: S2 — Make the service-is-product / UI-is-debug-and-demo scope explicit in the PRD

### Background
The user has explicitly clarified (see 2026-04-17 conversation history):

> The objective of this project is to provide a Cypher to AQL conversion
> service and a natural language to cypher to aql service. The UI serves
> 2 purposes, debugging and demoing.

The current PRD was written before this scoping clarification and treats the
Cypher Workbench UI on roughly the same footing as the library / CLI / HTTP
service. It needs four targeted edits to make the scope unambiguous.

### What to edit (in `docs/python_prd.md`)

1. **Executive summary (§line 27).** After the existing "Key decisions" bullet
   list, add (or modify the nearest appropriate bullet) a sentence along the
   lines of:

   > **Product scope.** The deliverable is the Cypher→AQL conversion service
   > (§4.3) and the NL→Cypher→AQL / NL→AQL pipelines (§1.2, §1.3) that run
   > inside it. The Cypher Workbench UI (§4.4) exists to **debug** the
   > service (visualize translations, replay activity, inspect schema
   > mappings) and to **demo** it to prospects. It is not a full-featured
   > multi-user workbench and is **not part of the default deployable
   > surface** (see §15).

2. **§2 Goals / non-goals (§line 313).** Under `### Goals`, add a final bullet:

   > - **Primary product**: a deployable conversion service (library, CLI,
   >   HTTP) with a deterministic Cypher→AQL transpiler and an LLM-driven
   >   NL→Cypher pipeline. The UI (§4.4) is a debug/demo surface, not a
   >   separately supported product.

   Under `### Non-goals`, add a bullet:

   > - The UI is not intended as a production multi-user workbench
   >   (authn/authz, collaboration, persistence, multi-tenant isolation are
   >   explicitly out of scope).

3. **§4.4 Cypher Workbench UI (§line 399).** Insert a scope banner immediately
   under the section heading, before any existing subsection:

   > **Scope note.** The Workbench UI is a **debug and demo surface** for
   > the conversion service (§4.3). It is optimized for single-operator use
   > (developer debugging a translation, salesperson demoing to a prospect)
   > and does not target multi-user production use. Authentication is
   > deliberately minimal (§4.4.5), persistence is browser-local
   > (`localStorage` + corrections DB), and the UI is not deployed by
   > default alongside the service (§15).

4. **§15 Packaging and deployment (§line 1966).** Find the scope / "what this
   repo ships" paragraph near the top of §15 and add a short note:

   > The default deployable artifact is the conversion service (library +
   > HTTP surface). The Workbench UI (§4.4) is packaged separately and
   > deployed only when a debug/demo surface is desired. Most service
   > deployments run headless.

   If §15 already has a subsection about deployed artifacts, add the UI
   carve-out there instead of repeating.

5. **Changelog entry** at the top of the PRD:

   > | 2026-04-17 | **Product scope clarified.** The conversion service
   > (§4.3) + NL pipelines (§1.2, §1.3) are the product; the Cypher
   > Workbench UI (§4.4) is a debug/demo surface, not part of the default
   > deployable surface (§15). Edits to Executive summary, §2 Goals /
   > Non-goals, §4.4 scope banner, §15 packaging. |

### Constraints

- Don't rewrite §4.4's existing subsections — only add the scope banner at
  the top. The UI spec remains accurate for when the UI IS deployed.
- Don't remove any existing content. This is an additive clarification, not
  a redesign.
- Preserve existing markdown table alignment and heading numbering.

### Acceptance

- `rg -n 'debug and demo' docs/python_prd.md` returns at least 3 hits
  (executive summary, §4.4 banner, one more).
- `rg -n 'not deployed by default' docs/python_prd.md` returns at least one
  hit in §4.4 and one in §15.
- Changelog entry appears at the top of the date-ordered list.
- Diff touches only `docs/python_prd.md`.
```

---

### S3 — WP-25 Wave 4-pre: PromptBuilder refactor

```
This prompt is **identical to the Wave 4-pre prompt already present in this
document** under "Wave 4 prompts — WP-25: NL→Cypher pipeline hardening".
Paste that prompt verbatim. Summary of what it does:

- Turn the monolithic `_SYSTEM_PROMPT` + ad-hoc retry-prompt construction in
  `arango_cypher/nl2cypher.py` into a composable `PromptBuilder` class with
  sections `schema_summary`, `few_shot_examples`, `resolved_entities`,
  `retry_context`.
- Preserve behaviour BYTE-FOR-BYTE in the zero-shot case (empty few-shot
  and empty resolved-entities).
- Add `tests/test_nl2cypher_prompt_builder.py` with a golden-shape test
  proving the zero-shot prompt is character-identical to pre-refactor.
- Do NOT introduce any new feature behaviour — few-shot and entity
  resolution are Wave 4a's job.

See the Wave 4-pre prompt earlier in this document for the full scope,
acceptance criteria, and constraints.

### Why this is bundled into Wave 5 rather than waiting for Wave 4

Landing it now (a) unblocks Wave 4a at any future moment without needing a
separate pre-step session, (b) is genuinely independent from the other two
Wave 5 items (different files, different concerns), and (c) is the smallest
possible footprint for the WP-25 work — lands cleanly alone if Wave 4a
gets deprioritized.
```

---

## Wave 4 prompts — WP-25: NL→Cypher pipeline hardening

**Source of truth:** `docs/implementation_plan.md` WP-25, `docs/python_prd.md` §1.2.1, research notes in `docs/research/nl2cypher.md` and `docs/research/nl2cypher2aql_analysis.md`.

**Orchestration:**
1. **Wave 4-pre (sequential, 1 agent, ~0.5 d):** PromptBuilder refactor. Lands on `main` before Wave 4a launches.
2. **Wave 4a (parallel, 4 agents):** WP-25.1 (few-shot), WP-25.2 (entity resolution), WP-25.3 (execution-grounded), WP-25.4 (caching). Launch on separate branches from `main` after 4-pre merges.
3. **Wave 4b (sequential):** merge 4a branches, resolve `nl2cypher.py` merge points, run full unit suite.
4. **Wave 4c (sequential, 1 agent):** WP-25.5 (evaluation harness + regression gate).

**Shared context for all Wave 4 sub-agents:**

```
{SHARED CONTEXT BLOCK — paste from top of this document}

## Wave 4 addenda — NL→Cypher module layout

ADDITIONAL FILES RELEVANT TO WAVE 4:
arango_cypher/nl2cypher.py         # Single large module today; being decomposed.
arango_query_core/mapping.py       # MappingBundle / MappingResolver
arango_query_core/exec.py          # AqlExecutor (wraps python-arango cursor)
tests/fixtures/datasets/movies/query-corpus.yml      # ~20 (description, cypher) pairs
tests/fixtures/datasets/northwind/query-corpus.yml   # ~14 (description, cypher) pairs
tests/fixtures/datasets/social/query-corpus.yml      # small, illustrative

## Current NL→Cypher pipeline (what you are hardening)

- nl_to_cypher(question, *, mapping, use_llm=True, llm_provider=None, max_retries=2)
  - Builds a conceptual-only schema summary via _build_schema_summary(bundle).
  - Calls _call_llm_with_retry() which:
    - Sends { _SYSTEM_PROMPT.format(schema=summary), question } to the provider.
    - Extracts a Cypher block from the response (code fences or heuristic).
    - Rewrites hallucinated labels via _fix_labels().
    - Parses the Cypher with the ANTLR parser; on failure feeds error back and retries.
  - Falls back to _rule_based_translate() when no LLM is configured.

## The §1.2 invariant (NON-NEGOTIABLE)

The LLM never sees physical details (collection names, type discriminators, field
names, AQL). It sees only the conceptual schema: entity labels, relationship types,
properties, domain/range, and data-quality hints. Few-shot examples MUST be
conceptual Cypher. Resolved entities are string VALUES (e.g. "Forrest Gump"), not
schema details.

## Quality rules specific to Wave 4

- Offline graceful degradation: every feature must behave sanely when no DB is
  connected and no LLM is configured. Specifically, unit tests MUST run without
  network access.
- Latency budget: each added round-trip must be justified. Cache aggressively within
  a request lifecycle; cache the per-mapping-bundle retriever across requests.
- No silent behaviour change when the feature's flag is off: the zero-shot /
  no-resolution / no-EXPLAIN path must be bit-identical to today's output on a
  given question + mapping + seeded LLM response.
```

---

### Wave 4-pre: PromptBuilder refactor (sequential, must land first)

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: Wave 4-pre — Refactor _SYSTEM_PROMPT into a composable PromptBuilder

### Goal
Replace the monolithic `_SYSTEM_PROMPT` string + ad-hoc retry-prompt construction in
`arango_cypher/nl2cypher.py` with a `PromptBuilder` class whose sections can be
extended independently by the Wave 4a sub-agents. Behaviour is preserved EXACTLY.

### Scope

1. Add a `PromptBuilder` class to `arango_cypher/nl2cypher.py`:

```python
@dataclass
class PromptBuilder:
    schema_summary: str
    few_shot_examples: list[tuple[str, str]] = field(default_factory=list)
    resolved_entities: list[str] = field(default_factory=list)
    retry_context: str = ""

    def render_system(self) -> str:
        # Schema-first so providers can cache this prefix. Existing wording of
        # _SYSTEM_PROMPT MUST be preserved byte-for-byte in the zero-shot case
        # (empty few_shot_examples and empty resolved_entities).
        ...

    def render_user(self, question: str) -> str:
        # retry_context appended if non-empty, as today.
        ...
```

2. Rewrite `_call_llm_with_retry()` to build a `PromptBuilder`, pass it through
   the attempt loop, and mutate only `retry_context` between attempts. Preserve
   the existing `_fix_labels()`, `_extract_cypher_from_response()`, and
   `_validate_cypher()` calls unchanged.

3. Update `OpenAIProvider.generate()` / `OpenRouterProvider.generate()` to
   accept the already-built system string (the builder renders once; the
   provider no longer does `_SYSTEM_PROMPT.format(...)`). This is a small
   provider-interface change — bump the protocol to
   `generate(system: str, user: str) -> (content, usage)`. Update the
   `_AqlChatProvider` wrapper likewise.

4. Do NOT introduce few-shot or entity resolution behaviour. Those are
   Wave 4a's job. Your job is only the shape change.

### Constraints

- Golden-shape test: add `tests/test_nl2cypher_prompt_builder.py` asserting
  that `PromptBuilder(schema_summary="X").render_system()` equals the
  pre-refactor `_SYSTEM_PROMPT.format(schema="X")` character-for-character.
- Mock-provider test: existing unit tests that mock `LLMProvider.generate()`
  must still pass unchanged. If they break because of the signature change,
  update the mocks minimally.

### Acceptance

- `pytest -m "not integration and not tck"` is bit-identical green.
- `ruff check .` passes.
- A grep for `_SYSTEM_PROMPT.format` returns zero hits.
- Diff touches only `arango_cypher/nl2cypher.py` and
  `tests/test_nl2cypher_prompt_builder.py` (plus any mock-fix lines in
  existing tests).
```

---

### WP-25.1: Dynamic few-shot retrieval

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: WP-25.1 — Dynamic few-shot retrieval for NL→Cypher

### Goal
Before the LLM call, retrieve the top-K most similar (NL question → Cypher answer)
examples from a curated seed corpus and inject them into the prompt.

### What to implement

1. Package layout:
   - `arango_cypher/nl2cypher/__init__.py` — re-export the existing public API
     (`nl_to_cypher`, `nl_to_aql`, `NL2CypherResult`, `NL2AqlResult`,
     `LLMProvider`, `OpenAIProvider`, `OpenRouterProvider`,
     `get_llm_provider`, `suggest_nl_queries`). Convert the current
     `arango_cypher/nl2cypher.py` file into a package — preserve every
     import path that exists today.
   - `arango_cypher/nl2cypher/fewshot.py` — new. Implement:
     ```python
     class Retriever(Protocol):
         def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]: ...


     class BM25Retriever(Retriever):
         def __init__(self, examples: list[tuple[str, str]]): ...
         def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]: ...


     class FewShotIndex:
         def __init__(self, retriever: Retriever): ...
         @classmethod
         def from_corpus_files(cls, paths: list[Path]) -> "FewShotIndex": ...
         def format_prompt_section(self, question: str, k: int = 3) -> str: ...
     ```
   - `arango_cypher/nl2cypher/corpora/` — new directory with:
     - `movies.yml`, `northwind.yml`, `social.yml`: mined from the `description`
       and `cypher` fields of `tests/fixtures/datasets/*/query-corpus.yml`.
       Format:
       ```yaml
       version: 1
       examples:
         - question: "Find all movies Tom Hanks acted in"
           cypher: 'MATCH (p:Person {name: "Tom Hanks"})-[:ACTED_IN]->(m:Movie) RETURN m.title'
       ```
     - Use the `description` field as `question` (lightly humanized if it
       reads more like a label than a question — e.g. append a question mark,
       replace "Count foo per bar" with "How many foo per bar").

2. Integration with `PromptBuilder` (from Wave 4-pre):
   - Add `FewShotIndex` invocation in `nl_to_cypher()` when `use_fewshot=True`
     (default). Retrieve K=3 examples; pass them to `PromptBuilder.few_shot_examples`.
   - Render as a `## Examples` block immediately after the schema, in the
     order produced by the retriever:
     ```
     ## Examples

     Q: <question>
     ```cypher
     <cypher>
     ```

     Q: <question>
     ...
     ```

3. Dependency: add `rank_bm25>=0.2.2` to the `[dev]` extra in `pyproject.toml`.
   Do NOT add it to core requirements — the corpora are conceptual-Cypher, so
   the retriever can live behind a lazy import and degrade to no-op if
   `rank_bm25` is unavailable at runtime.

### Tests (`tests/test_nl2cypher_fewshot.py`)

- `test_bm25_retriever_finds_similar_question()`: given a tiny corpus with
  a known best match, `retrieve("find movies for tom hanks", k=1)` returns
  the Tom Hanks example.
- `test_few_shot_index_from_corpus_files()`: loads the three yml corpora,
  checks `len(index.examples) > 30`.
- `test_prompt_section_format()`: asserts the rendered block matches a
  golden string (multi-line, deterministic order).
- `test_empty_corpus_graceful()`: `FewShotIndex(BM25Retriever([]))` yields
  an empty string from `format_prompt_section`, no exceptions.
- `test_nl_to_cypher_use_fewshot_false_is_bit_identical()`: with
  `use_fewshot=False`, the system string exactly matches the Wave 4-pre
  zero-shot baseline.

### Acceptance

- New unit tests pass.
- All existing unit tests pass unchanged.
- `ruff check .` clean.
- With `OPENAI_API_KEY` set, a quick manual sanity check produces a
  plausibly-better Cypher on the Movies fixture for a tricky question
  (document the before/after in the PR description, do not commit it).
```

---

### WP-25.2: Pre-flight entity resolution

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: WP-25.2 — Pre-flight entity resolution for NL→Cypher

### Goal
Extract candidate entity mentions from the user question and rewrite each to its
database-correct form before the LLM call. So "who acted in Forest Gump?" gets
augmented with `User mentioned 'Forest Gump' — matched to Movie.title='Forrest Gump'.`

### What to implement

1. `arango_cypher/nl2cypher/entity_resolution.py` — new. Implement:
   ```python
   @dataclass
   class ResolvedEntity:
       mention: str           # "Forest Gump"
       label: str             # "Movie"
       property: str          # "title"
       value: str             # "Forrest Gump"
       score: float           # 0.0 - 1.0

   class EntityResolver:
       def __init__(
           self,
           *,
           db=None,           # python-arango StandardDatabase or None
           mapping: MappingBundle | None = None,
           max_candidates: int = 5,
       ): ...

       def extract_candidates(self, question: str) -> list[str]: ...
       def resolve(self, question: str) -> list[ResolvedEntity]: ...
       def format_prompt_section(self, resolved: list[ResolvedEntity]) -> str: ...
   ```

2. `extract_candidates()`: return quoted substrings, Title-Case multi-word
   phrases, and capitalized single words that don't match schema keywords.
   Conservative — better to miss a candidate than drown the LLM in garbage.

3. `resolve()`:
   - **DB path (preferred):** for each candidate, try ArangoSearch-backed
     lookup against string properties of entity collections (name, title,
     label — read property names from the `MappingBundle`). Use AQL
     `SEARCH ANALYZER(...)` with BM25 if a view exists; otherwise fall back
     to `FILTER LOWER(d.<prop>) CONTAINS LOWER(@mention)` against each
     relevant collection (cap at 50 rows per collection). Take the highest
     score per candidate above a threshold (e.g. 0.6 for search, strict
     equality/prefix for FILTER fallback).
   - **No-DB path:** return `[]`. Do NOT raise.
   - Cache results per `(mapping_id, question)` for the lifetime of the
     `EntityResolver` instance.

4. Integration with `PromptBuilder`:
   - In `nl_to_cypher()`, if `use_entity_resolution=True` (default) AND an
     `EntityResolver` is available on the call (either passed explicitly or
     derivable from a DB handle on the request), invoke it; pass the
     rendered section to `PromptBuilder.resolved_entities`.
   - Rendered as a `## Resolved entities` block between schema/examples and
     the question:
     ```
     ## Resolved entities

     - "Forest Gump" → Movie.title = "Forrest Gump" (similarity 0.92)
     ```

5. `arango_cypher/service.py`: when a DB connection is configured, wire an
   `EntityResolver` into the `/nl2cypher` handler's call to `nl_to_cypher()`.

### Tests (`tests/test_nl2cypher_entity_resolution.py`)

- `test_extract_candidates_quoted_string()`: extracts `The Matrix` from
  `Find movies similar to "The Matrix"`.
- `test_extract_candidates_title_case()`: extracts `Tom Hanks` from
  `Which movies did Tom Hanks act in?`.
- `test_extract_candidates_skips_schema_keywords()`: does NOT extract `Person`
  from `Find all Person nodes`.
- `test_resolve_with_mocked_db()`: mock the db handle to return a BM25-style
  result for "Forest Gump" → "Forrest Gump"; assert the ResolvedEntity shape.
- `test_resolve_no_db_returns_empty()`: `EntityResolver(db=None).resolve(q)` → `[]`.
- `test_prompt_section_format()`: golden assertion on rendered block.
- `test_nl_to_cypher_use_entity_resolution_false_is_bit_identical()`: with
  flag off, system string matches Wave 4-pre baseline.

### Acceptance

- New unit tests pass (all with mocked or no DB).
- All existing unit tests pass.
- Offline nl_to_cypher() behaviour is unchanged when entity resolution is off.
- `ruff check .` clean.
- Integration test (opt-in, RUN_INTEGRATION=1) against the Movies dataset:
  "who acted in Forest Gump" (deliberate typo) produces a Cypher whose
  property literal is "Forrest Gump".
```

---

### WP-25.3: Execution-grounded validation loop

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: WP-25.3 — Execution-grounded validation for NL→Cypher

### Goal
Extend the LLM retry loop to catch semantic errors (nonexistent collection,
nonexistent property, invalid traversal direction) in addition to the syntactic
errors the ANTLR parser already catches. Use `POST /_api/explain` on the connected
database: no execution, just planning. Errors from `EXPLAIN` feed back into the
next retry prompt the same way ANTLR errors do today.

### What to implement

1. `arango_query_core/exec.py`: add a read-only helper:
   ```python
   def explain_aql(
       db,  # python-arango StandardDatabase
       aql: str,
       bind_vars: dict,
   ) -> tuple[bool, str]:
       """Return (ok, error_message). Empty message on success."""
   ```
   Use `db.aql.explain(aql, bind_vars=bind_vars)` from python-arango; map any
   `AQLQueryExplainError` / `ArangoServerError` into a short human-readable
   error string suitable for LLM feedback. Do NOT surface full HTTP payloads
   or stack traces.

2. `arango_cypher/nl2cypher/__init__.py` (or the split file that replaces it):
   extend `_call_llm_with_retry()`:
   - After ANTLR parse succeeds, call the existing `translate()` to produce AQL.
   - If a `db` is available on the call (passed through the `nl_to_cypher()`
     kwargs), call `explain_aql()`.
   - On `EXPLAIN` failure:
     - Build a retry message: `"Translated AQL failed EXPLAIN: <error>. The
       Cypher was: <cypher>. Please revise your Cypher."`
     - Feed it into `PromptBuilder.retry_context` on the next attempt.
     - Count against `max_retries` (same budget as ANTLR failures).
   - On `EXPLAIN` success, return the result.
   - If no `db` is configured, skip `EXPLAIN` entirely and preserve today's
     behaviour exactly.

3. `arango_cypher/service.py`: pipe the connected DB through the `/nl2cypher`
   handler. Respect existing connection lifecycle.

### Tests (`tests/test_nl2cypher_execution_grounded.py`)

- `test_explain_success_accepted()`: mock `explain_aql` to return `(True, "")`;
  result is identical to today's validation path.
- `test_explain_failure_triggers_retry()`: mock `explain_aql` to return
  `(False, "collection 'Persons' not found")` on attempt 1 and `(True, "")`
  on attempt 2; assert retry prompt contains the error and the final result
  uses the second Cypher.
- `test_no_db_skips_explain()`: with `db=None`, behaviour is bit-identical
  to Wave 4-pre baseline.
- `test_retry_budget_respected()`: `max_retries=2` with three failures total
  produces the best-of result with `retries=2`, not an infinite loop.

### Constraints

- NEVER execute the AQL — EXPLAIN only. Even on a read-only database, we
  respect the budget by not paying for row materialization.
- The feature is opt-in via the presence of a DB handle. No config flag.
- Unit tests MUST run without network access (mock everything).

### Acceptance

- New unit tests pass.
- Offline nl_to_cypher() is bit-identical to Wave 4-pre.
- `ruff check .` clean.
- Integration test (opt-in): a deliberately-bad Cypher like
  `MATCH (n:Persons) RETURN n` (collection is `Person`, not `Persons`) is
  self-healed when called via `/nl2cypher` with a live DB.
```

---

### WP-25.4: Prompt caching

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: WP-25.4 — Schema-prefix prompt caching

### Goal
The schema block is static per mapping. Put it at the top of the system prompt so
OpenAI's automatic prompt caching kicks in above the token threshold, and add the
Anthropic `cache_control` hook so a future Anthropic provider gets caching for free.

### What to implement

1. `arango_cypher/nl2cypher/` (post-4-pre structure): confirm
   `PromptBuilder.render_system()` orders sections as:
   ```
   [role/rules prelude]       <- tiny, static
   [schema summary]           <- large, static per mapping: the cache target
   [few-shot examples]        <- medium, varies per question (WP-25.1)
   [resolved entities]        <- small, varies per question (WP-25.2)
   ```
   If Wave 4-pre shipped the sections in a different order, refactor to this
   order. Existing golden tests may need to be regenerated; keep the zero-shot
   case bit-identical EXCEPT for order, and update the Wave 4-pre golden
   accordingly.

2. OpenAI telemetry: in `OpenAIProvider._chat()`, capture
   `usage.prompt_tokens_details.cached_tokens` when the field is present and
   include it in the returned usage dict. Update `NL2CypherResult` / `NL2AqlResult`
   to expose `cached_tokens: int = 0`. Surface in the `/nl2cypher` JSON response
   so the UI can display cache-hit rate.

3. Anthropic cache_control hook: in the provider dispatch, if the provider is
   an Anthropic-compatible one (detect via base_url or explicit subclass),
   split the system prompt into a cached prefix (everything through the schema
   block) and an uncached suffix, and render to Anthropic's
   `{role: "system", content: [{type: "text", text: "...", cache_control: {...}}]}`
   message shape. Leave a `AnthropicProvider` stub class in
   `arango_cypher/nl2cypher/providers.py` even if it's not wired end-to-end —
   the shape of the hook is what we're committing to here.

4. Document the caching behaviour in `arango_cypher/nl2cypher/README.md`
   (new, short): who caches what, what token thresholds apply, how to read
   the `cached_tokens` field.

### Tests (`tests/test_nl2cypher_caching.py`)

- `test_prompt_ordering_schema_is_first_after_prelude()`: assert the schema
  block appears before examples and resolved-entities sections.
- `test_cached_tokens_propagates_from_usage()`: mock a provider response
  with `prompt_tokens_details.cached_tokens = 512`; assert the final
  `NL2CypherResult.cached_tokens == 512`.
- `test_cached_tokens_default_zero()`: provider returns no details; result
  has `cached_tokens == 0`.
- `test_anthropic_provider_shape_stub()`: assert the Anthropic cache_control
  prefix/suffix split is produced correctly for a sample prompt.

### Acceptance

- New unit tests pass.
- All existing unit tests pass (some may need golden updates for section
  ordering — keep the update minimal).
- No live API calls in the unit suite.
- `ruff check .` clean.
```

---

### WP-25.5: Evaluation harness + regression gate (Wave 4c, sequential)

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: WP-25.5 — NL→Cypher evaluation harness + regression gate

### Goal
A repeatable measurement of the pipeline's accuracy, cost, and reliability — plus
a CI gate that prevents future regressions.

### What to implement

1. `tests/nl2cypher/__init__.py` — new package.
2. `tests/nl2cypher/eval/corpus.yml` — hand-curated evaluation set. ~40-60 cases
   across the three fixture datasets. Categories:
   - **Baseline**: simple NL→Cypher lookups with a known-good answer.
   - **Few-shot bait**: questions whose intent closely mirrors a seed corpus
     example (WP-25.1 should lift these).
   - **Typo cases**: intentional misspellings of property values
     (WP-25.2 should fix these).
   - **Hallucination bait**: questions phrased to tempt label/collection
     invention (WP-25.3 should self-heal these).
   Case format:
   ```yaml
   version: 1
   cases:
     - id: eval_001
       mapping_fixture: movies_lpg
       question: "Which movies did Tom Hanks act in?"
       expected_patterns:
         - "MATCH .*Person.*Tom Hanks.*ACTED_IN.*Movie"
       category: baseline
     - id: eval_042
       mapping_fixture: movies_lpg
       question: "Who acted in Forest Gump?"
       expected_patterns:
         - "Forrest Gump"   # should be resolved by WP-25.2
       category: typo
   ```

3. `tests/nl2cypher/eval/runner.py` — new. For each case in corpus.yml:
   - Run the pipeline with a named config.
   - Collect metrics:
     - `parse_ok`: ANTLR parse success on the returned Cypher.
     - `explain_ok`: AQL EXPLAIN success (requires DB; skip if unavailable).
     - `pattern_match`: regex check against `expected_patterns` — ALL must match.
     - `row_match`: (optional, requires seeded DB) AQL executes and returns
       at least 1 row.
     - `tokens`, `retries`, `latency_ms`, `cached_tokens`.
   - Aggregate into a `Report` dataclass and serialize both markdown and JSON.
   - Output to `tests/nl2cypher/eval/reports/<UTC-date>-<config>.{md,json}`.

4. `tests/nl2cypher/eval/configs.yml` — named configs:
   ```yaml
   - name: zero_shot
     use_fewshot: false
     use_entity_resolution: false
     use_execution_grounded: false
   - name: few_shot
     use_fewshot: true
     use_entity_resolution: false
     use_execution_grounded: false
   - name: few_shot_plus_entity
     use_fewshot: true
     use_entity_resolution: true
     use_execution_grounded: false
   - name: full
     use_fewshot: true
     use_entity_resolution: true
     use_execution_grounded: true
   ```

5. `tests/nl2cypher/eval/baseline.json` — the committed baseline report for
   the `full` config. Check in the initial baseline after the harness lands
   and the pipeline runs green.

6. `tests/test_nl2cypher_eval_gate.py` — gate. Loads baseline.json; asserts
   a fresh run's metrics are not worse by more than:
   - `parse_ok` rate: drop ≤ 5 pp.
   - `pattern_match` rate: drop ≤ 5 pp.
   - `tokens_mean`: increase ≤ 20%.
   - `retries_mean`: increase ≤ 0.3.
   Gated behind `RUN_NL2CYPHER_EVAL=1` because LLM calls cost money.

7. README section: add `tests/nl2cypher/eval/README.md` explaining how to run
   the harness, how to sweep configs, and how to refresh the baseline.

### Tests

- `test_eval_runner_runs_on_fixture()`: mock the provider; assert the runner
  produces a Report with expected fields.
- `test_pattern_match_regex()`: case-level pattern-matching logic is correct.
- `test_gate_fails_on_regression()`: synthetic "worse" report triggers assertion.
- `test_gate_passes_on_no_change()`: identical-to-baseline report passes.

### Acceptance

- Harness runs end-to-end with a mocked provider in the unit suite.
- `RUN_NL2CYPHER_EVAL=1 OPENAI_API_KEY=... pytest tests/test_nl2cypher_eval_gate.py`
  passes on a checkout that has all of WP-25.1 through .4 merged.
- `tests/nl2cypher/eval/baseline.json` committed.
- `ruff check .` clean.
- `docs/python_prd.md` §1.2.1 updated to link to the baseline report.
```

---

## Wave 6 prompts — Schema inference + NL feedback-loop bug-fix (WP-27..WP-30) — **Shipped**

Derived from: [`schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md) and [`implementation_plan.md`](./implementation_plan.md) WP-27..WP-30.

> **Status (2026-05-06): All four work packages are merged to `main`.** Wave 6a shipped 2026-04-23 across commits `0313395` (WP-27 heuristic + transpiler backtick strip) and `ac6eaff` (WP-28 analyzer visibility + force-reacquire + UI banner). Wave 6b shipped 2026-04-24 across commits `d4a871f` (WP-29 NL prompt escaping + fail-closed retry) and `618aace` (WP-30 `editorCypherSource` reducer + regenerate-from-NL action). The prompt bodies below are kept verbatim as historical reference for the eventual sub-agent post-mortem and to seed similar bug-fix waves in future. Do not re-launch any of these sub-agents — running them now produces an empty PR. The only outstanding bug-fix-wave item is the manual §11.4 closeout E2E walkthrough on `ic-knowledge-graph-temporal` documented in [`schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md).

**Summary.** Fixes the six-defect cascade that produced an unrecoverable Translate-time parse error on a hybrid (GraphRAG + PG) database: heuristic mis-classification of a scalar data field named `label` as an LPG type discriminator (D1); silent analyzer-unavailable fallback with indefinite cache poisoning (D2); NL prompt missing backtick-escaping guidance (D3); retry loop returning invalid Cypher on exhaustion instead of failing closed (D4); transpiler not stripping label backticks before resolver lookup (D5); Translate-button parse failures having no route back into NL inference (D6).

### Dependency graph

```
Wave 6a (parallel, same PR, Phase A+B)
  WP-27 (heuristic correctness, incl. D5 transpiler backtick strip)
  WP-28 (analyzer visibility + service hardening)
                 │
                 ▼
Wave 6b (parallel within, Phase C, separate PR)
  WP-29 (NL prompt escaping + fail-closed retry)   ──►  WP-30 (Translate feedback)
```

Wave 6a blocks Wave 6b because (a) WP-29 integration tests against the corrected heuristic mapping depend on WP-27, (b) WP-29's fail-closed branch needs a realistic schema to demonstrate against, and (c) WP-30 consumes the `retry_context` contract that WP-29 introduces on the `/nl2cypher` request.

All four prompts inherit the Shared Context Block above.

---

### Wave 6a — Phase A/B (parallel, single PR)

Launch WP-27 and WP-28 in parallel on sibling branches off `main`. Merge both before running the full suite; the two diffs touch almost-disjoint surfaces (WP-27 is heuristic + transpiler code; WP-28 is service startup, warnings plumbing, and one UI banner). The small overlap is in `arango_cypher/schema_acquire.py` around `_build_fresh_bundle`, where WP-27 changes the heuristic body and WP-28 changes the `except ImportError` branch — these are separate functions/branches, not the same line.

---

#### WP-27 — Heuristic type-field detection hardening + transpiler backtick strip

```
SHARED CONTEXT — arango-cypher-py
<insert the shared context block here>

## Your task: WP-27 — Harden heuristic type-field detection; strip label backticks in the transpiler

### Background

The heuristic schema inference path (used as a fallback when `arangodb-schema-analyzer` is not importable) in `arango_cypher/schema_acquire.py` treats any field in `{type, _type, label, labels, kind, entityType}` present in ≥ 80 % of sampled documents as an LPG type discriminator, then explodes every distinct value into its own conceptual entity. On a real hybrid database, this produced 36 fake entity types from filename values held in a `label` field on a `*_Documents` collection, 43 of them carrying `.` in the name (illegal in `oC_SymbolicName` without backtick escape).

The transpiler's `_pick_primary_entity_label` calls `resolver.resolve_entity` with the raw label identifier string from the AST, which preserves backticks on escaped labels. A correctly-escaped LLM output (``MATCH (d:`Compliance.rst`)``) fails resolution with `No entity mapping for: \`Compliance.rst\`. Available entities: Compliance.rst`.

Full problem analysis: [`docs/schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md) defects **D1** and **D5**.

### What to implement

#### Part 1 — Heuristic hardening (D1)

Tight rewrite of `_detect_type_field` and the fallback behaviour in `_build_heuristic_mapping`. No new public API; the change is in how a candidate is accepted.

1. Split `_DOC_TYPE_FIELDS` into two tiers, keeping edge-candidate list intact:
   ```python
   _TIER1_TYPE_FIELDS = ["type", "_type", "entityType"]
   _TIER2_TYPE_FIELDS = ["label", "labels", "kind"]
   ```
   `_DOC_TYPE_FIELDS` becomes `_TIER1_TYPE_FIELDS + _TIER2_TYPE_FIELDS` for call sites that still pass it explicitly.

2. Add a helper:
   ```python
   _FILE_EXTENSION_SUFFIXES = (
       ".rst",
       ".md",
       ".pdf",
       ".asciidoc",
       ".txt",
       ".rtf",
       ".docx",
       ".html",
       ".json",
       ".xml",
       ".yaml",
       ".yml",
       ".ttl",
       ".owl",
   )


   def _looks_class_like(value: str) -> bool:
       if not value or not value.strip():
           return False
       if any(c in value for c in (".", "/", " ", "\t")):
           return False
       lv = value.lower()
       if any(lv.endswith(suf) for suf in _FILE_EXTENSION_SUFFIXES):
           return False
       return True
   ```

3. Rewrite `_detect_type_field` so it:
   - Still enforces the existing 80 % coverage rule.
   - For tier-1 candidates, accepts as before.
   - For tier-2 candidates, additionally:
     - Rejects when `distinct_count > max(50, int(0.5 * row_count))`.
     - Rejects when any sampled distinct value fails `_looks_class_like`.
   - Returns the first candidate that passes; `None` if none do.
   - Records every candidate considered (accepted or rejected) with a short reason string in a per-collection dict passed in by the caller (or returned alongside the field name — choose whichever is cleaner given the existing signature; do not over-refactor).

4. In `_build_heuristic_mapping`, when `_detect_type_field` returns `None` for a document collection, fall through to the existing COLLECTION-style branch (already implemented at line ~851). The behaviour there is correct; the only change is that more collections will land in this branch than before.

5. Attach rejection reasons to `bundle.metadata.heuristic_notes` keyed by collection name. Structure is a plain dict; no dataclass needed:
   ```json
   {
     "heuristic_notes": {
       "IBEX_Documents": {
         "rejected_candidates": [
           {"field": "label", "tier": 2, "reason": "36 distinct values over 36 rows exceeds cardinality ratio 0.5"}
         ],
         "accepted_field": null,
         "resolved_style": "COLLECTION"
       }
     }
   }
   ```

#### Part 2 — Transpiler backtick strip (D5)

In `arango_cypher/translate_v0.py`, add:

```python
def _strip_label_backticks(name: str) -> str:
    if len(name) >= 2 and name.startswith("`") and name.endswith("`"):
        return name[1:-1]
    return name
```

Apply it at every call site that passes a label identifier to `resolver.resolve_entity` or `resolver.resolve_relationship`. Primary site is `_pick_primary_entity_label` around line 3985 — there may be 3–6 other sites; grep for `resolve_entity(` and `resolve_relationship(`.

Do **not** strip at the parser level. Parse tree preserves the escaping; the strip is a normalisation at the resolution boundary, not a modification of the AST.

### Where to make changes

- `arango_cypher/schema_acquire.py` — `_DOC_TYPE_FIELDS` split, `_looks_class_like`, `_detect_type_field` rewrite, `heuristic_notes` attachment in `_build_heuristic_mapping`.
- `arango_cypher/translate_v0.py` — `_strip_label_backticks` + call-site wiring.
- `tests/test_schema_acquire_heuristic.py` — **new**. Golden cases below.
- `tests/test_translate_v0.py` — extend. Add backtick round-trip cases.

### Tests to add

#### `tests/test_schema_acquire_heuristic.py`

Use a `FakeDb` / mocked `python-arango` cursor; do not require a live DB. Reuse any existing mocking helper in `tests/test_schema_acquire.py` if present.

- `test_tier2_label_rejected_when_high_cardinality` — 36 distinct filenames on 36 rows → `_detect_type_field` returns `None`; `resolved_style` for the collection is `COLLECTION`; `heuristic_notes[col]["rejected_candidates"]` contains a `label` entry with `tier: 2`.
- `test_tier2_label_accepted_when_class_like` — values `{"Movie","Person"}` on 173 rows → `_detect_type_field` returns `"label"`; two entities emitted.
- `test_tier2_label_rejected_when_value_has_dot` — values `{"Compliance.rst","index.rst"}` → rejected; `reason` mentions file extension or `"."`.
- `test_tier1_type_always_wins_over_tier2_label` — collection has both `type` and `label` → `type` is chosen.
- `test_no_candidate_falls_through_to_collection` — collection has no discriminator-like field at all → emits one `COLLECTION` entity using the `_collection_label()` name.
- `test_heuristic_notes_structure` — bundle carries `metadata.heuristic_notes` with expected keys.

#### `tests/test_translate_v0.py` (extend)

- `test_backticked_label_resolves_same_as_bare_label` — build a mapping with entity `Movie`; assert `MATCH (m:Movie) RETURN m` and ``MATCH (m:`Movie`) RETURN m`` both transpile to byte-identical AQL.
- `test_backticked_label_with_dot_resolves` — mapping with entity named `Compliance.rst` (fixture the entity name as string in a test-only `MappingBundle`); assert ``MATCH (d:`Compliance.rst`) RETURN d.doc_version`` resolves and transpiles.

### Out of scope (do NOT change)

- Do **not** modify `acquire_mapping_bundle` or the analyzer call path. Analyzer output is authoritative; this WP only tightens the heuristic fallback.
- Do **not** remove tier-2 candidates entirely. The heuristic must still recognise legitimate LPG schemas that use `label` as a discriminator.
- Do **not** touch the NL prompt, the retry loop, or the UI. Those are WP-29 and WP-30.

### Acceptance criteria

- `pytest -m "not integration and not tck"` — all green (existing + new tests).
- `ruff check .` — clean.
- New tests above all pass.
- Heuristic mapping for a mocked `*_Documents`-shaped collection (36 rows, 36 distinct `label` values containing dots) produces exactly one entity with `style=COLLECTION` and zero entities with a dot in their name.
- Heuristic mapping for a mocked LPG-shaped collection (173 rows, 2 distinct `type` values `{"Person","Movie"}`) produces two entities with `style=LABEL` — unchanged from current behaviour.
- `MATCH (m:Movie)` and ``MATCH (m:`Movie`)`` produce byte-identical transpiled AQL when `Movie` is a mapped entity.

### Hand-off to WP-28 / WP-29 / WP-30

None — WP-27 is self-contained. WP-28 will add warnings plumbing that may touch `_build_heuristic_mapping`'s return shape; coordinate via the shared `metadata` dict. WP-29 will add a test that relies on the post-WP-27 heuristic behaviour being correct on the red-team fixture.
```

---

#### WP-28 — Analyzer-unavailable visibility + service hardening + `/schema/force-reacquire`

```
SHARED CONTEXT — arango-cypher-py
<insert the shared context block here>

## Your task: WP-28 — Surface analyzer-unavailable fallbacks; add a reacquisition endpoint; harden service startup

### Background

When `arangodb-schema-analyzer` is not importable at the deployed service, `_build_fresh_bundle` silently falls back to the heuristic and the result is cached indefinitely because the shape fingerprint does not change. Operators have no visible signal that a degraded mapping is being served, and already-poisoned caches require manual deletion of the cache document.

Full problem analysis: [`docs/schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md) defect **D2**.

### What to implement

1. **Structured warnings on the bundle.**
   Add to `arango_cypher/schema_acquire.py`:
   ```python
   def _attach_warning(
       bundle: MappingBundle, *, code: str, message: str, install_hint: str | None = None
   ) -> MappingBundle:
       meta = dict(bundle.metadata or {})
       warnings = list(meta.get("warnings") or [])
       warnings.append(
           {
               "code": code,
               "message": message,
               **({"install_hint": install_hint} if install_hint else {}),
           }
       )
       meta["warnings"] = warnings
       return MappingBundle(
           conceptual_schema=bundle.conceptual_schema,
           physical_mapping=bundle.physical_mapping,
           metadata=meta,
           owl_turtle=bundle.owl_turtle,
           source=bundle.source,
       )
   ```

2. **Wire the warning into the `ImportError` branch.**
   In `_build_fresh_bundle` (line ~1298):
   - Change the fallback log from `logger.info(...)` to `logger.warning("Heuristic schema path used — install arangodb-schema-analyzer for accurate mappings on hybrid schemas.")`.
   - Wrap the returned bundle with `_attach_warning(bundle, code="ANALYZER_NOT_INSTALLED", message="...", install_hint="pip install arangodb-schema-analyzer")`.

3. **Service startup refusal.**
   In `arango_cypher/service.py`, add at module scope (before `app` is first used in tests):
   ```python
   def _require_analyzer_unless_opted_out() -> None:
       if os.environ.get("ARANGO_CYPHER_ALLOW_HEURISTIC") == "1":
           return
       try:
           import schema_analyzer  # noqa: F401
       except ImportError as exc:
           raise RuntimeError(
               "arango-cypher-py service requires arangodb-schema-analyzer. "
               "Install it (`pip install arangodb-schema-analyzer`) or set "
               "ARANGO_CYPHER_ALLOW_HEURISTIC=1 to accept degraded mappings."
           ) from exc


   _require_analyzer_unless_opted_out()
   ```
   Call it at import time, not in an `on_event("startup")` hook — this ensures the fail is visible at process startup, not deferred until the first request.

4. **Analyzer retry on cache miss.**
   In `get_mapping`, when a persistent-cache lookup returns a bundle whose `metadata.warnings` contains `ANALYZER_NOT_INSTALLED` and `schema_analyzer` is now importable, treat it as a cache miss (do not return it, proceed to rebuild). Thin check, place it between the shape-fingerprint match and the stats-only-refresh branch. Helper:
   ```python
   def _bundle_needs_reacquire(bundle: MappingBundle) -> bool:
       warnings = (bundle.metadata or {}).get("warnings") or []
       if not any(w.get("code") == "ANALYZER_NOT_INSTALLED" for w in warnings):
           return False
       try:
           import schema_analyzer  # noqa: F401

           return True
       except ImportError:
           return False
   ```

5. **`POST /schema/force-reacquire` endpoint.**
   In `arango_cypher/service.py`, alongside the existing `POST /schema/invalidate-cache`:
   ```python
   @app.post("/schema/force-reacquire")
   def schema_force_reacquire(session: _Session = Depends(_get_session)):
       from .schema_acquire import get_mapping as _get_mapping

       bundle = _get_mapping(session.db, force_refresh=True, strategy="analyzer")
       return {
           "source": {"kind": bundle.source.kind, "notes": bundle.source.notes},
           "warnings": (bundle.metadata or {}).get("warnings") or [],
           "entity_count": len(bundle.conceptual_schema.get("entities") or []),
           "relationship_count": len(bundle.conceptual_schema.get("relationships") or []),
       }
   ```
   `strategy="analyzer"` is the hard form; it raises `ImportError` (wrapped into HTTP 503) if the analyzer is missing.

6. **Surface warnings in `/schema/introspect`.**
   In the existing `/schema/introspect` response, attach the bundle's warnings to the returned dict:
   ```python
   result["warnings"] = (bundle.metadata or {}).get("warnings") or []
   ```
   Place it before the return at the end of the endpoint.

7. **UI warning banner.**
   - New component `ui/src/components/SchemaWarningBanner.tsx`: renders an amber strip across the top of the workbench when any warning is present on the current mapping. Reuse the styling already used for the auth-expired banner.
   - In `ui/src/App.tsx`, track `schemaWarnings` in the reducer state; populate from `introspect` / `force-reacquire` responses; render `<SchemaWarningBanner warnings={schemaWarnings} />` above the main layout.
   - Click-to-dismiss persists in `localStorage` keyed by `(url, database, warning.code)` so a dismissed warning stays dismissed for the same database, but returns on a different connection.

8. **Operational metric.**
   No new telemetry dependency. Add a simple counter module-global `_heuristic_fallback_counter: int = 0` in `schema_acquire.py` and increment in the `ImportError` branch. Expose via `get_metrics()` or a new `GET /schema/metrics` endpoint if none exists yet — check what is there and match the pattern. If no metrics infra exists, skip this item and note it in the PR description for follow-up.

### Where to make changes

- `arango_cypher/schema_acquire.py` — `_attach_warning`, `_bundle_needs_reacquire`, `_build_fresh_bundle` update, `get_mapping` retry-on-miss branch.
- `arango_cypher/service.py` — startup hook, `/schema/force-reacquire` endpoint, `/schema/introspect` warnings passthrough.
- `ui/src/components/SchemaWarningBanner.tsx` — new.
- `ui/src/App.tsx` + `ui/src/api/client.ts` — state wiring + request type for the new fields.
- `tests/test_schema_acquire_warnings.py` — new.
- `tests/test_service_startup.py` — new.
- `tests/test_service_schema_status.py` — extend.

### Tests to add

- `tests/test_schema_acquire_warnings.py`
  - `test_attach_warning_roundtrip` — warning survives bundle reconstruction and dict serialization.
  - `test_importerror_branch_attaches_warning` — patch `schema_analyzer` import to raise `ImportError`; call `_build_fresh_bundle(strategy="auto")`; assert bundle carries `ANALYZER_NOT_INSTALLED`.
  - `test_bundle_needs_reacquire_when_analyzer_available` — bundle carries the warning + analyzer is importable → returns `True`.
  - `test_bundle_needs_reacquire_false_when_analyzer_missing` — bundle carries the warning + analyzer is NOT importable → returns `False` (no re-tryable improvement).
  - `test_get_mapping_busts_cache_when_needs_reacquire` — seed the persistent cache with a `ANALYZER_NOT_INSTALLED` bundle, then call `get_mapping` with analyzer importable → result is a fresh analyzer-built bundle, not the cached one.

- `tests/test_service_startup.py`
  - `test_startup_fails_without_analyzer` — monkeypatch `schema_analyzer` to be unimportable; assert importing `arango_cypher.service` raises `RuntimeError` with the install hint in the message.
  - `test_startup_succeeds_with_opt_out` — same but with `ARANGO_CYPHER_ALLOW_HEURISTIC=1` in env; import succeeds.
  - `test_startup_succeeds_with_analyzer` — analyzer present; import succeeds.

- `tests/test_service_schema_status.py` (extend)
  - `test_introspect_surfaces_warnings` — mocked session returns a bundle with warnings; response carries `result["warnings"]`.
  - `test_force_reacquire_invokes_analyzer_strategy` — mocked `get_mapping` recorded with `strategy="analyzer"` and `force_refresh=True`.
  - `test_force_reacquire_503_when_analyzer_missing` — `get_mapping` raises `ImportError`; endpoint returns 503.

### Out of scope (do NOT change)

- Do **not** modify the heuristic detection logic (WP-27's surface).
- Do **not** change `ArangoSchemaCache` schema or fingerprint semantics. Cache structure is stable.
- Do **not** add a distributed-invalidation mechanism. Per-worker reacquisition is sufficient for v1 (see bug-fix PRD §9 Open Question 2).

### Acceptance criteria

- `pytest -m "not integration and not tck"` — all green.
- `ruff check .` — clean.
- `ui/` typechecks + builds clean: `cd ui && npx tsc --noEmit -p tsconfig.app.json && npm run build`.
- Service fails to start with a clear `RuntimeError` when analyzer is absent and opt-out is unset.
- `POST /schema/force-reacquire` on a session backed by an analyzer-present DB returns a fresh bundle with `source.kind == "schema_analyzer_export"` and `warnings == []`.
- `/schema/introspect` response shape gains a `warnings` field (empty list when none, list of warning objects otherwise).

### Hand-off to WP-29

Neither WP-29 nor WP-30 consume WP-28's endpoints or warning surface. Overlap is confined to `schema_acquire.py`; conflicts expected only at the top of `_build_fresh_bundle` where both WP-27 and WP-28 touch adjacent lines.
```

---

### Wave 6b — Phase C (parallel within, separate PR after Wave 6a)

Launch WP-29 and WP-30 on separate branches off the merged Wave 6a tip. WP-30 depends on WP-29 for the `retry_context` field on `/nl2cypher`, so WP-29 should land first within the PR; if the PR is split, WP-29 ships before WP-30. For sub-agent purposes, both can be drafted in parallel and rebased after WP-29 merges.

---

#### WP-29 — NL prompt label-escaping + fail-closed retry

```
SHARED CONTEXT — arango-cypher-py
<insert the shared context block here>

## Your task: WP-29 — Teach the NL prompt to escape non-identifier labels; fail closed on retry exhaustion

### Background

When a conceptual entity name contains characters outside `[A-Za-z_][A-Za-z0-9_]*`, the LLM must emit it backtick-quoted in Cypher. The current system prompt does not mention this rule, and the schema card rendered by `_build_schema_summary` emits the label raw (`Node :Compliance.rst (…)`), so the LLM faithfully copies the illegal form. Separately, `_call_llm_with_retry` returns `best_cypher` to the UI on retry-budget exhaustion with a WARNING prefix buried in `.explanation` — the UI then writes the invalid Cypher into the editor. The tenant-guardrail code path already demonstrates the correct shape: return an empty-`cypher` result with a structured `method="…_blocked"` that the UI handles as a red banner.

Full problem analysis: [`docs/schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md) defects **D3** and **D4**.

### What to implement

#### Part 1 — Label escaping in the schema card and system prompt

1. Helper in `arango_cypher/nl2cypher/_core.py`:
   ```python
   _SYMBOLIC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


   def _escape_label(name: str) -> str:
       return name if _SYMBOLIC_NAME_RE.match(name or "") else f"`{name}`"
   ```

2. In `_build_schema_summary`'s `_format_entity`:
   - Replace `return f"  Node :{label} ({prop_str})"` with `return f"  Node :{_escape_label(label)} ({prop_str})"`.
3. In the relationship-rendering paths in `_build_schema_summary`:
   - Replace `f"  (:{from_e})-[:{rtype}{prop_str}]->(:{to_e})"` with `f"  (:{_escape_label(from_e)})-[:{_escape_label(rtype)}{prop_str}]->(:{_escape_label(to_e)})"`.
   - Same for the two fallback branches that use `cs_rel_types` / `pm["relationships"]`.

4. Apply the same `_escape_label` treatment in `arango_cypher/nl2cypher/_aql.py`'s `_build_physical_schema_summary` (the NL→AQL direct path). Keep the helper private to `_core.py` and re-import into `_aql.py`, or duplicate as a small utility — prefer the first.

5. Append to `_SYSTEM_PROMPT` (immediately after the existing "Rules:" block, before `"{schema}"`):
   ```
   - Labels and relationship types containing characters other than ASCII
     letters, digits, and underscore must be wrapped in backticks, e.g.
     MATCH (d:`Compliance.rst`) RETURN d.doc_version.
     The schema below has already pre-escaped such names; copy them verbatim.
   ```

#### Part 2 — Fail closed on retry exhaustion (D4)

Replace the fall-through at `arango_cypher/nl2cypher/_core.py:604–617`:

```python
if best_cypher:
    return NL2CypherResult(
        cypher="",
        explanation=(
            f"NL → Cypher validation failed after {1 + max_retries} attempts. "
            f"Last error: {builder.retry_context}.\n\n"
            f"Last attempted Cypher was:\n\n{best_cypher}"
        ),
        confidence=0.0,
        method="validation_failed",
        schema_context=schema_summary,
        prompt_tokens=total_usage["prompt_tokens"],
        completion_tokens=total_usage["completion_tokens"],
        total_tokens=total_usage["total_tokens"],
        retries=max_retries,
        cached_tokens=total_usage["cached_tokens"],
    )
```

Add a WARN log immediately before the return:
```python
logger.warning(
    "NL2Cypher validation_failed after %d attempts; last error: %s",
    1 + max_retries,
    builder.retry_context,
)
```

The tenant-guardrail fail-closed branch above it is unchanged.

#### Part 3 — UI handling for `method="validation_failed"`

In `ui/src/App.tsx` / the NL dispatch handler:

- When the `/nl2cypher` response has `method === "validation_failed"`, do **not** write `resp.cypher` into the Cypher editor. Dispatch a `NL_VALIDATION_FAILED` action instead that sets an error banner state and leaves the editor untouched.
- Render the red banner with the response's `explanation` text. Reuse the existing error-banner styling; if a dedicated `<ErrorBanner variant="validation-failed" />` is cleaner, ship that.
- The existing `tenant_guardrail_blocked` handling is the template; mirror its branch structure.

#### Part 4 — Add `retry_context` forwarding field (WP-30 dependency)

Prepare for WP-30 by extending the plumbing. No UI or new behaviour yet — just the contract:

- `arango_cypher/service.py`: add an optional `retry_context: str | None = None` field to the NL translate request payload (Pydantic model). Forward it into `nl_to_cypher(..., retry_context=retry_context)`.
- `arango_cypher/nl2cypher/_core.py`: `nl_to_cypher` gains an optional `retry_context: str | None = None` kwarg; when provided, set `builder.retry_context = retry_context` before the first attempt.
- Update `arango_cypher/nl2cypher/__init__.py` exports if needed.

### Where to make changes

- `arango_cypher/nl2cypher/_core.py` — `_escape_label`, `_build_schema_summary`, `_SYSTEM_PROMPT`, `_call_llm_with_retry` failure branch, `nl_to_cypher` signature.
- `arango_cypher/nl2cypher/_aql.py` — same escaping for `_build_physical_schema_summary`.
- `arango_cypher/service.py` — `retry_context` field on the NL request model; forward-wiring.
- `ui/src/App.tsx` + `ui/src/components/ErrorBanner.tsx` — validation-failed rendering branch.
- `ui/src/api/client.ts` — `retry_context` field on the request type (no UI trigger yet; WP-30 adds the trigger).
- `tests/test_nl2cypher_prompt_builder.py` — extend.
- `tests/test_nl2cypher_core.py` — extend.

### Tests to add

- `tests/test_nl2cypher_prompt_builder.py`
  - `test_escape_label_bare_identifier_unchanged` — `_escape_label("Person") == "Person"`.
  - `test_escape_label_wraps_non_identifier` — `_escape_label("Compliance.rst") == "\`Compliance.rst\`"`.
  - `test_schema_summary_escapes_dotted_entity` — bundle with entity `Compliance.rst` → schema summary contains `` Node :`Compliance.rst` ``.
  - `test_schema_summary_escapes_relationship_type` — bundle with a relationship type `HAS-CONTROL` (hyphen → non-identifier) is escaped.
  - `test_zero_shot_byte_identical_for_bare_names` — **critical regression test**. Build a bundle whose entities and relationships are all bare identifiers, render the system prompt with `tenant_context=None` and empty few-shot. Compare byte-for-byte against a fixed expected string (lift from existing `test_no_tenant_context_leaves_prompt_byte_identical` pattern). This pins that entities without special chars do not see any change.
  - `test_system_prompt_contains_backtick_rule` — the rule is present in `_SYSTEM_PROMPT` (string contains check).

- `tests/test_nl2cypher_core.py`
  - `test_call_llm_with_retry_fails_closed_on_exhaustion` — stub provider returns `"INVALID"` every time; after `max_retries=2`, the result has `cypher=""`, `method="validation_failed"`, and `explanation` contains the last error and the invalid cypher string.
  - `test_call_llm_with_retry_does_not_write_invalid_cypher` — `best_cypher` on the returned object is empty; a caller cannot accidentally populate the editor.
  - `test_retry_context_seeded_on_first_attempt_when_provided` — call `nl_to_cypher(..., retry_context="explain hint")`; first-attempt user message ends with `"Your previous Cypher was invalid: explain hint. Please fix it."`.
  - `test_validation_failed_logs_warning` — `caplog.records` contains a WARN record with `validation_failed` text.

### Out of scope (do NOT change)

- Do **not** add client-triggered regeneration on translate failure. That is WP-30.
- Do **not** change the schema acquisition path or the heuristic. Those are WP-27 / WP-28.
- Do **not** change the tenant-guardrail path; it already behaves correctly and is the template this WP copies.

### Acceptance criteria

- `pytest -m "not integration and not tck"` — all green.
- `ruff check .` — clean.
- `ui/` typechecks + builds clean.
- WP-25.5 eval corpus (`RUN_NL2CYPHER_EVAL=1 pytest -m eval`): no regression in `parse_ok` or `pattern_match` rates against the current baseline.
- Manual smoke: in the workbench, issue an NL question that the stub LLM cannot satisfy (configurable via a dev-only provider) and confirm the UI renders the red banner with the failure explanation and does not write anything into the Cypher editor.

### Hand-off to WP-30

WP-30 consumes the `retry_context` field added in Part 4. No other hand-off.
```

---

#### WP-30 — Translate-on-NL-output feedback in the UI

```
SHARED CONTEXT — arango-cypher-py
<insert the shared context block here>

## Your task: WP-30 — When Translate fails on NL-generated Cypher, offer a one-click regenerate-with-hint action

### Background

The Translate button is a pure Cypher → AQL call with no edge back into the NL pipeline. When bad Cypher sits in the editor (e.g. from a prior NL generation that squeaked past validation on an earlier build, or from a model error on a difficult schema), the user sees a parse error and has no path forward except deleting and re-typing. Expected UX is: if the Cypher came from NL in this session, offer a regenerate-with-hint action that re-invokes `/nl2cypher` with the transpile error as retry context. Hand-written Cypher must **not** trigger this (the user wrote what they wanted).

Full problem analysis: [`docs/schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md) defect **D6**. Dependency: WP-29 must be merged first — it introduces the `retry_context` field on the `/nl2cypher` request.

### What to implement

1. **`editorCypherSource` state machine in the UI reducer** (`ui/src/App.tsx` or its reducer module):
   - Add `editorCypherSource: "nl_pipeline" | "user" | null` to the reducer state. Initial value `null`.
   - On `NL_SUCCESS` action (writing NL-generated Cypher into the editor): set to `"nl_pipeline"`.
   - On `CYPHER_EDITED` action (any user edit, paste, or sample-load into the Cypher editor): set to `"user"`.
   - On `DISCONNECT` / connection change: reset to `null`.

2. **Regenerate action on translate failure**:
   - On `TRANSLATE_ERROR` when `editorCypherSource === "nl_pipeline"`, render a "Regenerate from NL with error hint" button inside the existing translate-error banner. Style matches the existing primary action buttons.
   - Clicking the button dispatches `NL_REGENERATE_REQUESTED` with the parse error string as payload.
   - The NL client invokes `/nl2cypher` with `retry_context` set to the parse error string and the original question text.
   - On success, the new Cypher replaces the editor contents (this correctly sets `editorCypherSource="nl_pipeline"` again). On failure (e.g. `validation_failed`), WP-29's banner handling kicks in.

3. **Retry-context plumbing**:
   - `ui/src/api/client.ts`: the `/nl2cypher` request body type already gains `retry_context?: string` in WP-29. This WP adds the call-site that supplies it. Nothing new in the client shape — just use it.
   - The question text used on regenerate is the last NL question stored in reducer state / history. If that is not available (e.g. the user cleared history), grey out the regenerate button and tooltip "Regenerate unavailable — original question not available in this session".

4. **Tests**:
   - UI reducer unit tests for the state machine (`editorCypherSource` transitions).
   - Extend `tests/test_service_nl.py` (or the equivalent NL service test) to assert that when `retry_context` is supplied on the request, the prompt's first-attempt user message reflects it (contract test — backs up WP-29's test with a service-layer verification).

### Where to make changes

- `ui/src/App.tsx` + its reducer file — new state field, new actions, new branch on `TRANSLATE_ERROR`.
- `ui/src/components/CypherEditor.tsx` — wire the "edit" event to dispatch `CYPHER_EDITED`. Verify no existing handler already does this.
- `ui/src/components/ErrorBanner.tsx` — regenerate-action variant when `editorCypherSource === "nl_pipeline"`.
- `ui/src/api/client.ts` — no shape change (done in WP-29); only the call-site.
- `ui/src/App.tsx` or `ui/src/api/store.ts` — state-management wiring for the last-NL-question.
- `tests/test_service_nl.py` (or the NL service test module) — extend.
- No changes to `arango_cypher/`.

### Out of scope (do NOT change)

- Do **not** auto-regenerate without a click. The regenerate action must be user-initiated.
- Do **not** trigger NL regeneration for hand-written Cypher (`editorCypherSource !== "nl_pipeline"`). Typing in the editor flips the source to `"user"` and the button must not appear.
- Do **not** implement EXPLAIN-time feedback (the bug-fix PRD §9 defers that to a future wave).
- Do **not** change the backend. All backend contract changes belong to WP-29.

### Acceptance criteria

- `ui/` typechecks + builds clean.
- UI unit tests for the `editorCypherSource` state machine pass (pure reducer test; no DOM needed).
- Extended service NL test passes — the `retry_context` field is correctly forwarded into the prompt.
- Manual smoke:
  - NL question "show me person names" → NL writes `MATCH (p:Person) RETURN p.name` → click Translate → success → no regenerate button anywhere (no error).
  - NL question crafted to produce invalid Cypher (use a dev-only stub provider) → NL writes invalid Cypher → click Translate → parse error + regenerate button visible. Click regenerate → `/nl2cypher` is called with `retry_context` set to the parse error. New Cypher replaces the editor.
  - Edit the NL-generated Cypher by typing one character → click Translate → parse error + **no** regenerate button (source is now `"user"`).

### Hand-off

None — WP-30 closes the bug-fix wave.
```

---

## Orchestrator checklist

Use this checklist when running the waves:

### Pre-flight
- [ ] All existing tests pass: `pytest -m "not integration and not tck"`
- [ ] Working tree is clean: `git status`
- [ ] Branch created for v0.2 work: `git checkout -b v0.2-dev`

### Wave 1
- [ ] Launch WP-1 (CREATE), WP-2 (aggregation), WP-3 (schema analyzer)
- [ ] Review WP-1 output: golden tests pass, integration test passes
- [ ] Review WP-2 output: golden tests pass, no regressions
- [ ] Review WP-3 output: unit tests pass, schema acquisition works
- [ ] Merge all three, resolve any conflicts in translate_v0.py
- [ ] Full test run: `pytest -m "not integration and not tck"` — all green

### Wave 2
- [ ] Launch WP-5 (TCK harness), WP-4 (CLI), WP-8 (UI)
- [ ] Review WP-5: Scenario Outline works, normalization works
- [ ] Review WP-4: CLI smoke tests pass
- [ ] Review WP-8: UI changes work (manual verification)
- [ ] Merge all three
- [ ] Full test run — all green

### Wave 3
- [ ] Launch WP-6 (TCK coverage) — iterative, may take multiple sessions
- [ ] Track pass rate: aim for ≥ 40% of Match scenarios
- [ ] Launch WP-7 (Movies expansion)
- [ ] Full test run including integration: `RUN_INTEGRATION=1 pytest`
- [ ] TCK run: `RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck`

### Post-flight
- [ ] Update PRD implementation status table
- [ ] Update PRD §6.4 Cypher subset table
- [ ] Update implementation_plan.md tracking table
- [ ] Update pyproject.toml version to 0.2.0
- [ ] Tag release: `git tag v0.2.0`

### Wave 5 (pre-WP-25 cleanup + unblock Wave 4a)
- [ ] Launch S1 (plan hygiene), S2 (PRD product-scope edits), S3 (PromptBuilder refactor ≡ Wave 4-pre) in parallel on separate branches.
- [ ] Review S1: WP-19 and WP-20 status rows consistent; no stale prose anywhere.
- [ ] Review S2: scope banner visible in §4.4; "not deployed by default" in §15; changelog entry present.
- [ ] Review S3: zero-shot prompt is bit-identical to pre-refactor; `pytest -m "not integration and not tck"` all green.
- [ ] Merge all three; commit as a single logical split ("Wave 5 cleanup: plan hygiene, PRD scope, PromptBuilder refactor") or three sequential commits if cleaner diffs help review.
- [ ] Dual-push to `ArthurKeen/arango-cypher-py` and `arango-solutions/arango-cypher`.
- [ ] Wave 4a is now launchable at any time. Wave 4-pre step is satisfied by S3.

### Wave 4 (WP-25 — NL→Cypher hardening, separate release band)
- [ ] **Wave 4-pre**: land PromptBuilder refactor on `main` (1 agent, sequential). Verify `pytest -m "not integration and not tck"` is bit-identical green.
- [ ] **Wave 4a**: launch WP-25.1, WP-25.2, WP-25.3, WP-25.4 in parallel (4 agents, separate branches from `main`).
- [ ] Review each branch: unit tests pass, offline behaviour preserved, `ruff check .` clean.
- [ ] **Wave 4b**: merge 4a branches sequentially into an integration branch; resolve `nl2cypher/__init__.py` / `nl2cypher.py` conflicts (expect small overlaps in `PromptBuilder` section wiring and in `_call_llm_with_retry`).
- [ ] Full test run: `pytest -m "not integration and not tck"` — all green.
- [ ] **Wave 4c**: launch WP-25.5 (1 agent, sequential). Produce initial baseline report, commit `tests/nl2cypher/eval/baseline.json`.
- [ ] Manual smoke with a live LLM + DB on the Movies fixture: verify each of few-shot / entity resolution / execution-grounded actually fires on representative questions.
- [ ] Update PRD §1.2.1: mark implemented techniques with `*(implemented)*` and link the baseline report.
- [ ] Update `docs/implementation_plan.md` status table WP-25 row to **Done** with the merge date.

### Wave 6 (schema-inference + NL feedback-loop bug fix) — **Shipped**
- [x] **Wave 6a** (Phase A/B, single PR): WP-27 (heuristic + transpiler backtick-strip) and WP-28 (analyzer visibility + force-reacquire + service startup) merged 2026-04-23 (commits `0313395`, `ac6eaff`). The two waves were ultimately consolidated into a single agent because both modify `arango_cypher/schema_acquire.py` and the textual overlap, while small, is non-zero — coordinating one agent across both eliminated merge-resolution overhead.
- [x] Reviewed WP-27: heuristic tests pass; no existing tests regress; transpiler round-trip tests for backticked labels pass.
- [x] Reviewed WP-28: service refuses to start without the analyzer unless `ARANGO_CYPHER_ALLOW_HEURISTIC=1`; `/schema/force-reacquire` returns a fresh analyzer-sourced bundle; `/schema/introspect` surfaces `warnings`; UI banner renders.
- [x] Full test run on the merge tip: `pytest -m "not integration and not tck"` green; `ruff check .` clean; UI typecheck + build clean.
- [ ] **Operational step (deferred to deployment):** on each deployed service, run `POST /schema/force-reacquire` once to evict any cached bundles that were poisoned by the pre-WP-27 heuristic on hybrid databases. (Tracked in the §11 closeout runbook of [`schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md) as P5.)
- [x] **Wave 6b** (Phase C, separate PR off the merged Wave 6a tip): WP-29 (prompt escaping + fail-closed retry + `retry_context` plumbing) merged 2026-04-24 (commit `d4a871f`); WP-30 (Translate-feedback UI) merged 2026-04-24 (commit `618aace`).
- [x] Reviewed WP-29: zero-shot prompt byte-identical for bare-identifier schemas (regression pin held); `validation_failed` branch returns empty cypher; UI renders red banner; WP-25.5 eval showed no regression.
- [x] Reviewed WP-30: state-machine unit tests pass; regenerate button appears only for `editorCypherSource === "nl_pipeline"`; `retry_context` forwarded correctly on click.
- [x] Updated `docs/implementation_plan.md` tracking table: WP-27..WP-30 marked **Done** with merge commit refs (this PR).
- [x] Updated `docs/python_prd.md` implementation-status rows for "Heuristic fallback correctness (hybrid schemas)" and "NL → Translate feedback loop" from *Known defect — scheduled* to **Done** (this PR).
- [ ] **Manual closeout (pending):** run the §11.4 E2E walkthrough on `ic-knowledge-graph-temporal` per [`schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md) §11 and fill in the AC-1..AC-6 closeout log. Until that runs, the bug-fix PRD itself remains marked OPEN even though all four implementation WPs are merged.

### Waves 7–11 (multi-tenant safety + v0.4+ residual tail)

Continued in [`agent_prompts_multitenant.md`](./agent_prompts_multitenant.md). Covers:

- **Wave 7** — MT-Phase 1: Layer 1 session binding + Layer 5 EXPLAIN-plan validator + `safe_execute` (1 agent, security-critical).
- **Wave 8** — MT-Phase 2: 1 pre-step (`tenant_ast_common`) + 3 parallel agents (MT-2 guardrail hardening, MT-3 Cypher AST, MT-4 AQL AST).
- **Wave 9** — MT-Phase 3: plan-shape LRU + admin cross-tenant bypass + audit stream (1 agent).
- **Wave 10** — MT-8 standing red-team corpus (1 agent, ongoing).
- **Wave 11** — v0.4+ residual tail: `RETURN DISTINCT` multi-column, `LIMIT`/`SKIP` exprs, native `shortestPath()`, index hint emission, VCI advisory polish (1 agent, independent of Waves 7–10).
