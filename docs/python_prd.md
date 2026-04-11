# Arango Cypher (Python) — PRD + Implementation Plan
Date: 2026-02-17  
Last updated: 2026-04-10  
Workspace: `arango-cypher-py`  
Related repos:
- `~/code/arango-cypher` (Foxx/JS implementation)
- `~/code/arango-schema-mapper` (a.k.a. `arangodb-schema-analyzer`, schema detection + mapping)

## Executive summary
Build a **Python-native Cypher → AQL transpiler** that runs **outside** ArangoDB (CLI/library/service), uses **`arangodb-schema-analyzer`** to produce a **conceptual schema + conceptual→physical mapping** (and optionally OWL Turtle), and can translate Cypher against **pure PG**, **pure LPG**, or **hybrid** physical ArangoDB models.

Key decisions:
- **New project**: keep Foxx `arango-cypher` stable; create a separate Python project.
- **Name**: repo `arango-cypher-py`, Python import package `arango_cypher`, distributable name `arango-cypher-py` (or `arangodb-cypher-transpiler` if you want to avoid ambiguity).
- **Schema mapping**: depend on `arangodb-schema-analyzer` as a library and optionally consume/produce OWL Turtle via its tool contract.
- **NL -> Cypher -> AQL** (prospect-driven): natural-language stacks target **Cypher** (richer model priors than AQL); Arango provides a **deterministic** Cypher->AQL transpiler plus **namespaced `arango.*` extensions** for capabilities Cypher does not standardize (see S2A.0, S7A).
- **Parsing (as implemented)**: ANTLR4-generated Python parser from the openCypher grammar in-repo (`grammar/Cypher.g4`). Re-evaluating `libcypher-parser-python` remains an optional future migration if native wheels and AST mapping prove worthwhile (see S6).
- **Agentic workflow** (optional): provide a stable JSON-in/JSON-out "tool" interface for translate/explain that can be used in agent pipelines, but keep translation correctness deterministic.

---

## 1) Problem statement
ArangoDB supports multiple physical graph modeling styles:
- **PG-ish**: "types-as-collections" (one vertex collection per label/type; one edge collection per relationship type, etc.)
- **LPG-ish**: "generic collections + type field" (single vertex collection with `type`, single edge collection with `type`, etc.)
- **Hybrid**: a mixture of both across entities/relationships, sometimes within the same query path.

Cypher is a conceptual (label/type-centric) query language. To execute on ArangoDB, we need:
- a **conceptual schema** (labels, relationship types, properties)
- a **mapping** to physical collections and type fields
- a **transpiler** that generates safe, performant AQL for the physical layout (including hybrid paths)

The Foxx version runs inside ArangoDB coordinators, which constrains dependencies and runtime. A Python implementation enables:
- richer parsing toolchains
- strong typing and better testing ergonomics
- easy integration into notebooks, CLIs, services, and "agentic workflows"

---

## 2) Goals / non-goals

### Goals (v0.1–v0.3)
- **Translate** a defined subset of Cypher into **AQL + bind variables**.
- **Execute** translated AQL against ArangoDB (optional convenience wrapper).
- **Support PG, LPG, and hybrid** via `arangodb-schema-analyzer` mapping.
- Provide:
  - **Library API** (callable from other Python code)
  - **CLI** (run cypher, print AQL, execute, show results)
  - Optional **HTTP service** (translate/execute endpoints)
- Deterministic behavior by default; agentic enhancements are optional and non-authoritative.

### Non-goals (initially)
- Full openCypher TCK compliance.
- Writing queries (CREATE/MERGE/DELETE/SET) in the first milestone, unless you explicitly want it.
- Full query optimizer equivalent to a database planner (we'll have a small internal logical plan, but not a cost-based optimizer).

---

## 3) Users & primary use cases

### Personas
- **Data engineer**: wants to run Cypher for exploration and migrate Neo4j-ish workloads.
- **Application developer**: wants a Cypher compatibility layer for an app backed by ArangoDB.
- **Analyst / notebook user**: wants Cypher in Jupyter, quick iteration, shareable queries.
- **Agent workflow**: tools that need a stable "translate/execute/explain" contract.

### Core user stories
- **Translate-only**: "Given Cypher, show me AQL and bind vars."
- **Translate + execute**: "Run Cypher against database X and return JSON results."
- **Explain mapping**: "Show how labels/types were mapped to collections/fields (hybrid-aware)."
- **Validate**: "Warn if query references unknown labels/types based on inferred conceptual schema."

---

## 4) Product surfaces

### 4.1 Library API (primary)
Python package: `arango_cypher`

Suggested API:
- `translate(cypher: str, *, db=None, mapping=None, options=None) -> TranspiledQuery`
  - returns `{ aql: str, bind_vars: dict, warnings: list, metadata: dict }`
- `execute(cypher: str, *, db, mapping=None, options=None) -> list[dict] | Cursor`
- `get_mapping(db, *, strategy="auto", analyzer_options=...) -> MappingBundle`

### 4.2 CLI (v0.1)
Command: `arango-cypher-py` (or `arangodb-cypher`)

Commands:
- `translate`: prints AQL (+ bind vars JSON)
- `run`: executes and prints results (pretty table or JSON)
- `mapping`: prints mapping summary; optionally writes OWL Turtle
- `doctor`: checks connectivity, required collections/indexes, and config

### 4.3 HTTP service (shipped)
FastAPI service (`arango_cypher.service:app`) with endpoints:
- `POST /connect` --- authenticate to ArangoDB, returns session token
- `POST /disconnect` --- tear down session
- `GET /connections` --- list active sessions (admin/debug)
- `GET /connect/defaults` --- `.env` defaults for pre-filling the connection dialog (never returns password)
- `POST /translate` --- Cypher -> AQL + bind vars
- `POST /execute` --- translate and execute (requires session)
- `POST /validate` --- syntax-only or parse+translate validation
- `POST /explain` --- translate Cypher, run AQL EXPLAIN, return execution plan (requires session)
- `POST /aql-profile` --- translate Cypher, execute with profiling, return runtime stats (requires session)
- `GET /cypher-profile` --- JSON manifest for agent/NL gateways (S2A.0)

Run with: `uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8000`

### 4.4 Cypher Workbench UI

#### 4.4.1 Architecture
SPA served by FastAPI. The browser does **not** connect to ArangoDB directly; all
database interaction flows through the service layer.

```
Browser                          FastAPI service             ArangoDB
+--------------------------+     +--------------------+     +----------+
| Cypher Editor            |     |                    |     |          |
| AQL Editor (read-only)   | <-> | arango_cypher      | <-> | Database |
| Results / Explain / Prof |     | .service:app       |     |          |
+--------------------------+     +--------------------+     +----------+
```

#### 4.4.2 Cypher editor --- syntax-directed capabilities
The Cypher editor is a **full syntax-directed editing experience**.

**A) Syntax highlighting and structural awareness**
- Token-level highlighting: keywords (`MATCH`, `WHERE`, `RETURN`, `WITH`, `CALL`,
  `YIELD`, `UNION`, `OPTIONAL`, `UNWIND`, `CASE`, ...), labels (`:User`),
  relationship types (`[:KNOWS]`), properties, parameters (`$name`), strings,
  numbers, comments, `arango.*` namespace.
- Clause-level colorization: distinct background tint or gutter icon per clause block.
- Bracket/paren matching: highlight matching pairs, flash on close.
- Auto-close: `(`, `[`, `{`, `'` auto-close.
- Indentation: auto-indent continuation lines; smart indent after `WHERE`, `AND`, `OR`.
- Code folding: collapse multi-line clause bodies.

**B) Real-time error detection**
- Parse-error markers: red squiggly underline at exact error token; tooltip shows
  parse error. Triggered on keystroke debounce (300 ms) via `POST /translate`.
- Profile-aware warnings: amber squiggly for constructs not yet supported (from
  `/cypher-profile` `not_yet_supported` list).
- Bind-var warnings: warn if `$paramName` appears but is not defined in parameter panel.

**C) Autocompletion (context-aware)**
- After `MATCH (` or `:` -> entity labels from mapping.
- After `[` or `[:` -> relationship types from mapping.
- After `.` on a bound variable -> property names from conceptual schema for that label.
- After `arango.` -> registered extension functions/procedures from profile.
- After `$` -> parameter names from parameter panel.
- Start of line -> Cypher keywords appropriate to position.
- Inside `RETURN`/`WITH` -> aggregation functions, built-in functions.

**D) Navigation and reference**
- Variable-use highlighting: cursor on variable highlights all occurrences.
- Go-to-definition: Ctrl/Cmd+click on variable jumps to where it is first bound.
- Clause outline: minimap/sidebar showing clause structure (`MATCH` -> `WHERE` -> `RETURN`).
- Hover documentation: keyword descriptions, `arango.*` function signatures and AQL equivalents.

**E) Editing assistance**
- Snippet templates: `match`+Tab expands to template; customizable.
- Comment toggle: Ctrl/Cmd+`/`.
- Multi-cursor support.
- Query history: up/down in empty editor; history panel with search.
- Format/prettify: Ctrl/Cmd+Shift+F.

**F) Parameter binding**
- Auto-detection of `$paramName` tokens.
- JSON value entry per parameter.
- Persistence in localStorage per query hash.

Editor library: **CodeMirror 6** with custom Lezer grammar or community Cypher package.

#### 4.4.3 AQL editor --- syntax-directed display with Explain / Profile
Side-by-side with Cypher editor. CodeMirror 6 instance, read-only by default.

**A) Syntax highlighting**
- AQL keywords (`FOR`, `IN`, `FILTER`, `RETURN`, `LET`, `SORT`, `LIMIT`,
  `COLLECT`, ...), bind parameters (`@@collection`, `@param`), functions,
  strings, numbers, comments.
- Bind-var references visually distinct (bold + colored).
- Line numbers always shown.

**B) Live synchronization**
- Auto-translate on Cypher changes (debounced 500 ms).
- Bind vars panel below AQL editor.
- Error state: if translation fails, show error inline instead of stale AQL.

**C) Explain and Profile**
- **Explain** button -> `POST /explain` -> renders execution plan as interactive
  tree (type, estimatedCost, estimatedNrItems, index details). Raw JSON toggle.
- **Profile** button -> `POST /aql-profile` -> executes with profiling, shows
  runtime stats per plan node (actual time, rows, memory). Color-coded hotspots.
  Results go to Results panel.

**D) Read-only vs editable mode**
- Read-only (default): reflects transpiler output exactly.
- Editable (toggle): user can modify AQL for experimentation. "Modified" indicator.
  Reset button. Re-translating from Cypher overwrites (with confirmation if modified).

**E) Correspondence hints (v0.4+)**
- Hovering over Cypher clause highlights corresponding AQL lines (via source-map
  metadata).

#### 4.4.4 Panels and layout
Split-pane layout with resizable dividers:

1. **Connection bar** (top).
2. **Cypher editor** (left) with Translate and Run buttons, parameter panel.
3. **AQL editor** (right, side-by-side) with Explain and Profile buttons, bind-vars
   panel, read-only/edit toggle.
4. **Results panel** (bottom, full width) with tabs: Table, Graph, JSON, Explain,
   Profile.
5. **Mapping panel** (drawer/tab).
6. **Profile panel** (drawer/tab).

#### 4.4.5 Connection and credential model
- **Browser-supplied** (primary): user enters host, port, database, username,
  password in the connection dialog. Credentials travel to FastAPI only; the browser
  never contacts ArangoDB directly.
- **`.env` defaults** (convenience): `GET /connect/defaults` returns non-secret
  defaults (host, port, database, username) to pre-fill the dialog. Password is
  **never** returned.
- **Security constraints**: credentials are held in server-side session storage
  (in-process dict keyed by opaque token). No credentials are persisted to disk.
  Session tokens are short-lived (configurable TTL, default 30 min, sliding).

Service endpoints used by the connection model:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/connect` | Authenticate, return session token |
| POST | `/disconnect` | Tear down session |
| GET | `/connections` | List active sessions (admin/debug) |
| GET | `/connect/defaults` | `.env` defaults for pre-fill |
| POST | `/translate` | Cypher -> AQL (no session required) |
| POST | `/execute` | Translate + execute (session required) |
| POST | `/validate` | Syntax / translation validation |
| POST | `/explain` | AQL EXPLAIN (session required) |
| POST | `/aql-profile` | Execute with profiling (session required) |
| GET | `/cypher-profile` | JSON manifest for agent/NL gateways |

#### 4.4.6 Results display
- **Table view** (default).
- **Graph view** (Cytoscape.js).
- **JSON view**.
- **Explain view**: interactive tree of AQL execution plan.
- **Profile view**: annotated plan with runtime metrics, color-coded hotspots.
- **Export**: CSV or JSON.

#### 4.4.7 Tech stack
- **Framework**: React (Vite).
- **Editor**: CodeMirror 6.
- **Cypher language mode**: Custom Lezer grammar or community package.
- **AQL language mode**: Custom Lezer grammar.
- **Graph visualization**: Cytoscape.js.
- **Execution plan viz**: React tree component (custom or react-d3-tree).
- **HTTP client**: fetch / axios.
- **Styling**: Tailwind CSS or CSS modules.

#### 4.4.8 Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl/Cmd+Enter | Translate |
| Shift+Enter | Execute |
| Ctrl/Cmd+Shift+E | Explain |
| Ctrl/Cmd+Shift+P | Profile |
| Ctrl+Space | Autocomplete |
| Ctrl/Cmd+Shift+F | Format |
| Ctrl/Cmd+/ | Toggle comment |
| Ctrl/Cmd+D | Select next occurrence |
| Ctrl/Cmd+Z / Ctrl/Cmd+Y | Undo / Redo |
| Up/Down (empty editor) | Query history |

#### 4.4.9 Phasing

| Phase | Scope |
|-------|-------|
| v0.3-alpha | FastAPI service with all endpoints. Connection dialog. Cypher editor with syntax highlighting (A), bracket matching, auto-close. AQL editor (read-only) with AQL syntax highlighting. Translate button -> AQL preview. No execute. |
| v0.3-beta | Execute with table results. Cypher parse-error markers (B). AQL Explain button + tree view. Query history. Parameter binding (F). Bind-vars panel. `.env` defaults. Keyboard shortcuts. |
| v0.3 | AQL Profile button + annotated plan view. Graph view. Variable-use highlighting (D). Clause outline. Mapping panel. Profile panel. AQL editor editable mode toggle. |
| v0.4 | Context-aware autocompletion (C). Hover documentation (D). Profile-aware warnings (B). Snippet templates (E). Format/prettify. Correspondence hints. Multi-statement. Export. |

---

## 5) Schema detection & mapping (hard requirement)

### 5.1 Required dependency: `arangodb-schema-analyzer`
`~/code/arango-schema-mapper` is a Python library named `arangodb-schema-analyzer` with:
- `AgenticSchemaAnalyzer` library API
- A stable **tool contract v1** (`schema_analyzer.tool.run_tool(request_dict)` or CLI `arangodb-schema-analyzer`)
- Export formats:
  - stable JSON export for transpilers (`operation="export"`)
  - OWL Turtle (`operation="owl"`)

### 5.2 Mapping contract we will consume
We will treat `export` output as the authoritative "transpiler mapping" contract.

Important: the analyzer already defines mapping styles and even provides injection-safe AQL fragments:
- Entity mapping styles: `COLLECTION` vs `LABEL` (generic + `typeField/typeValue`)
- Relationship mapping styles: `DEDICATED_COLLECTION` vs `GENERIC_WITH_TYPE`

This aligns exactly with your hybrid requirement: mapping is per entity type and per relationship type, so any mixture is representable.

### 5.3 Detection strategy in `arango-cypher-py`
We will implement a 3-tier strategy:
- **Explicit config** (highest priority): user-supplied mapping overrides; useful for unstable databases.
- **Fast heuristic** (cheap): quickly classify as "pure PG", "pure LPG", or "uncertain/hybrid".
- **Fallback**: call `arangodb-schema-analyzer` when uncertain/hybrid or when user forces it.

Even if heuristics say "pure PG/LPG", we should allow `--use-analyzer` to force analyzer mapping (for correctness).

### 5.4 OWL Turtle usage
You asked specifically for OWL TTL. We'll support two flows:
- **Primary runtime flow**: consume `export` mapping JSON (simpler, stable, already designed for transpilers).
- **Artifact/explain flow**: also store `owl` Turtle output alongside the export, for:
  - debugging
  - explaining hybrid partitions
  - offline review / documentation

Optional: implement a TTL ingestion path using `rdflib` so users can provide a TTL mapping file (air-gapped use, reproducible builds).

Recommended libs:
- `rdflib` for parsing TTL

---

## 6) Cypher parsing strategy (critical choice)

### 6.1 Requirements for a parser
We need:
- correct tokenization and precedence for expressions
- a parse tree rich enough to build an internal AST
- maintainability (avoid "hand-rolled" parser for full Cypher)

### 6.2 Candidate approaches (ranked)

#### Option A (preferred): `libcypher-parser-python`
Use `libcypher-parser-python` (Python bindings over `libcypher-parser`) to parse Cypher into a C-backed AST/parse tree, then convert into our normalized AST.

Pros:
- purpose-built Cypher parser
- avoids maintaining grammar ourselves

Cons:
- native dependency (platform wheels availability must be verified)
- AST shape may be low-level; still need a normalizer

#### Option B (fallback): ANTLR4-generated parser from openCypher grammar
Use openCypher grammar sources and generate a Python parser.

Pros:
- controllable; pure-python runtime (ANTLR runtime dependency only)
- grammar is public and versionable in the repo

Cons:
- you own grammar drift / compatibility
- need to write visitors and handle ambiguities

#### Option C (not recommended as primary): builder-only libraries
Packages like `opencypher` are helpful for *constructing* Cypher, not parsing arbitrary Cypher inputs. Not enough for a transpiler.

### 6.3 Parser evaluation plan (1-week spike)
Deliverables:
- parse a representative query corpus (MATCH/WHERE/RETURN/WITH/aggregations/patterns)
- emit a normalized internal AST
- confirm error reporting quality and performance

Decision criteria:
- feature coverage for our target subset
- reproducibility and install story (esp. Apple Silicon)
- license compatibility

---

## 7) Translation architecture (deterministic core)

### 7.1 High-level pipeline
1) **Parse** Cypher → parse tree  
2) **Normalize** parse tree → internal AST (stable shape)  
3) **Resolve** labels/types/properties using conceptual schema + physical mapping  
4) **Lower** AST → logical plan (a small set of operations: node scan, expand, filter, project, aggregate, sort, paginate)  
5) **Render** logical plan → AQL string + bind vars  
6) (Optional) **Execute** via `python-arango`

### 7.2 Internal models
- `NormalizedAst` (pydantic models or dataclasses)
- `ConceptualSchema` + `PhysicalMapping` imported from `schema_analyzer` (or mirrored types if you want to decouple)
- `LogicalPlan` nodes (dataclasses)
- `AqlQuery` object: `{ text, bind_vars, debug }`

### 7.3 Hybrid physical model handling (core requirement)
At translation time, every conceptual node label and relationship type must be resolved to a **physical implementation**:
- entity:
  - `COLLECTION` → `FOR v IN @@collection`
  - `LABEL` → `FOR v IN @@collection FILTER v[@typeField] == @typeValue`
- relationship:
  - `DEDICATED_COLLECTION` → scan/traverse the edge collection
  - `GENERIC_WITH_TYPE` → scan generic edge collection + filter by type

For a path pattern like `(a:User)-[:FOLLOWS]->(b:User)`:
- `User` might be `COLLECTION(users)` in PG but `LABEL(vertices, type="User")` in LPG
- `FOLLOWS` might be dedicated edge collection `follows` or generic `edges` with `type="FOLLOWS"`

The renderer must produce correct AQL in all combinations:
- PG vertex + PG edge
- PG vertex + LPG edge
- LPG vertex + PG edge
- LPG vertex + LPG edge

### 7.4 AQL safety
Never string-interpolate collection names or user expressions directly:
- use Arango bind parameters:
  - `@@collection` for collection names
  - `@param` for values

We should use (or replicate) the analyzer's `PhysicalMapping.aql_entity_match()` and `aql_relationship_traversal()` patterns where possible, then extend them for:
- multi-hop expansions
- OPTIONAL MATCH patterns
- multiple relationship types
- predicate pushdown

---

## 7A) Arango extensions and nested-document policy

### 7A.1 Design goals for extensions
- **Keep core Cypher portable**: standard Cypher should translate without needing Arango-specific constructs.
- **Namespaced and explicit**: Arango-only features must be clearly marked and easy to lint/deny in "portable mode".
- **Deterministic translation**: extensions compile to AQL predictably; no hidden runtime prompts.
- **Security**: no raw AQL injection; enforce bind variables and allowlist capabilities.

### 7A.2 Extension registry (compile-time plugin surface)
Implement an internal registry that maps Cypher-level extension calls to AQL fragments.

Conceptual interfaces:
- `FunctionRegistry`:
  - key: `arango.<name>` (e.g. `arango.bm25`, `arango.vector_near`, `arango.geo_distance`)
  - signature: argument kinds + return kind
  - compiler: `compile(call_ast, ctx) -> (aql_expr: str, bind_vars: dict, warnings: list)`
- `ProcedureRegistry`:
  - key: `arango.<name>` invoked via `CALL arango.<name>(...) YIELD ...`
  - yield schema: list of columns produced (names + conceptual types)
  - compiler: `compile(call_ast, ctx) -> (aql_subquery: str, bind_vars: dict, yielded_columns: list, warnings: list)`

Policy knobs:
- `extensions.enabled: bool` (default `false` in "portable mode"; `true` in Arango mode)
- `extensions.allowlist: set[str]` (e.g. allow `arango.search` but disallow `arango.aql`)
- `extensions.denylist: set[str]`

### 7A.3 Cypher surface syntax for extensions

#### A) Namespaced functions (`arango.*`) in expressions (preferred for v0.1)
These are used inside `WHERE`, `RETURN`, `ORDER BY`, etc., and compile to AQL expressions.

Examples (illustrative; exact names are part of the registry spec):
- Full text scoring/ranking: `arango.bm25(n)` → `BM25(n)` (when `n` comes from a view doc)
- Geo distance: `arango.geo_distance(n.location, point({longitude: 32, latitude: 45}))`
- Vector similarity: `arango.cosine_similarity(n.embedding, $queryEmbedding)`

Constraints:
- Must compile to **pure AQL expressions** (no new FROM sources).
- If an extension requires a different FROM (e.g. ArangoSearch view), it must be a procedure (or a clause extension in later versions).

#### B) Procedures (`CALL arango.*`) for source-changing operations
Some Arango features are fundamentally "source changing" (e.g. querying an ArangoSearch view, vector topK retrieval). Those are better expressed as procedures that yield rows.

Shape:
- `CALL arango.search("viewName", {query: "...", ...}) YIELD doc, score`
- `CALL arango.vector_near("collectionOrView", $vector, {k: 20}) YIELD doc, distance`

Compilation model:
- compiles into an AQL subquery that returns an array of rows, then unwinds into the Cypher pipeline.

Initial v0.1 stance:
- We can **design** the procedure interface in v0.1, but implement only a minimal subset once we have `WITH` pipeline semantics working (v0.2+).

#### C) Escape hatch (optional, disabled by default): `CALL arango.aql(text, bindVars)`
This is powerful but risky. If included at all:
- **off by default**
- requires explicit allowlist enablement
- requires bind vars provided separately
- must run in a "least privilege" execution mode

### 7A.4 Mapping Arango capabilities (full text, vector, geo)
High-level mapping targets for the registry:
- **Full-text search**:
  - procedure form that changes source to an ArangoSearch view
  - AQL: `FOR d IN view SEARCH ... SORT BM25(d) DESC RETURN ...`
- **Vector search**:
  - procedure form that returns topK candidates
  - AQL pattern depends on index type/version, but always: (a) query vector (b) topK results (c) return docs + distance/score
- **Geospatial search**:
  - function form (distance computations)
  - procedure form if we want geo "near" as a source

Implementation note:
- The registry is how we keep these additions from contaminating the core transpiler logic.

### 7A.5 Nested-document policy (hierarchical JSON)

#### Default: nested documents are properties (dot-path access)
For a document like:
`{"location":{"long":32,"lat":45},"address":{"zip":1234567,"city":"Springfield"}}`

Default semantics:
- `n.address.zip` is a property access on `n`
- in AQL, this lowers to `n.address.zip` (with safe handling for missing fields where needed)

This is the recommended default because it matches ArangoDB's document model and keeps translation predictable.

#### Optional (mapping-driven): embedded conceptual entities + embedded relationships
Some nested objects should behave like conceptual nodes/relationships (without necessarily being physically separate documents).

We will support this only when the **mapping** (from `arangodb-schema-analyzer` or explicit override) declares an embedded relationship, e.g.:
- conceptual relationship: `(:User)-[:HAS_ADDRESS]->(:Address)`
- physical: `User.address` is an embedded object

Lowering strategy:
- treat the "virtual node" value as a computed value (`LET addr = u.address`)
- allow property predicates/projections over that value (`addr.zip`, `addr.city`)

Critical constraint:
- Unless we define a stable identity rule, "virtual nodes" do not have real `_id` and cannot participate in general graph traversal semantics.

### 7A.6 "Virtual edges" support: v0.1 vs later

#### Definition
A "virtual edge" is a conceptual relationship where the physical representation is:
- embedded object/array inside a document, or
- a foreign-key-like reference field (e.g. `user.companyId`) without an edge collection.

#### v0.1 (supported)
- **Dot-path property access** (nested documents) in expressions and projections.
- **Embedded relationship as computed value** (mapping-driven):
  - one-hop only
  - no path expansion
  - no uniqueness semantics beyond what the document already provides
  - no `MERGE`/writes

Example of what v0.1 can do:
- `(u:User)` with mapping declaring `HAS_ADDRESS` embedded:
  - allow `MATCH (u:User) WHERE u.address.zip = 1234567 RETURN u.address.city`
  - allow `MATCH (u:User)-[:HAS_ADDRESS]->(a:Address) WHERE a.zip = 1234567 RETURN a`
    - implemented as `LET a = u.address` (no separate scan)

#### v0.1 (not supported)
- Variable-length traversal over virtual edges: `-[:HAS_ADDRESS*1..3]->`
- Joining virtual nodes across rows by identity (no `_id`)
- Using virtual edges with graph algorithms or path uniqueness semantics
- OPTIONAL MATCH semantics involving virtual edges unless explicitly designed (defer)

#### Later (v0.3+) possible extensions
If you want richer semantics, we can add one (or more) explicit identity strategies:
- **Synthetic identity** for embedded objects (e.g. hash of parent `_id` + JSON pointer)
- **Materialized view** strategy: treat embedded objects as a derived "virtual collection" via AQL views/subqueries
- **FK expansion** strategy: treat `*_id` fields as joinable references with `DOCUMENT()`

These should be **explicitly configured** because they affect correctness/performance expectations.

---

## 8) Testing strategy & migration plan

### 8.1 Test layers
- **Unit tests** (pure Python):
  - parser normalization tests
  - mapping resolution tests
  - AQL rendering tests
- **Golden tests** (snapshot):
  - Cypher input → expected AQL + bind vars
  - stable formatting enforced
- **Integration tests** (requires ArangoDB):
  - run AQL and validate results on a seeded dataset
  - validate hybrid mappings end-to-end

Recommended libs:
- `pytest`, `pytest-cov`
- `syrupy` (or `pytest-regressions`) for snapshots
- `hypothesis` for property-based tests (optional but great for expression rendering and bind var stability)
- `docker` / `docker compose` for integration environment

### 8.2 Converting existing `arango-cypher` tests
We'll treat the JS/Foxx test suite as **spec** and migrate in stages:

#### Step 1: Extract a corpus
Create a `tests/fixtures/cypher_cases/` directory with files like:
- `case_001.yml`:
  - `cypher`
  - `expected_aql`
  - `expected_bind_vars`
  - optional `mapping_override`
  - optional `notes`

#### Step 2: Recreate "golden AQL" expectations
For translation parity, snapshot the AQL output of the Foxx version (where applicable) and store as expected outputs for the Python version.

#### Step 3: Add integration semantics incrementally
Once translation matches, add integration assertions:
- seed database with a minimal dataset
- execute AQL produced by Python
- compare result sets (order-insensitive unless ORDER BY is part of query)

#### Step 4: Optional: compatibility harness
If you want automation, we can build a small script that:
- runs the JS translator to capture AQL outputs
- emits Python fixture YAML files
This reduces manual porting effort dramatically.

---

## 9) Agentic workflow support (optional, but easy to add)
Keep deterministic translation as the source of truth.

Add an optional "tool contract" layer similar to the schema analyzer:
- `translate_tool(request_dict) -> response_dict`
  - request fields: cypher, connection (optional), mapping (optional), options
  - response: ok/error, aql, bind_vars, mapping_summary, warnings

Where agentic adds value:
- "Explain why this label mapped to that collection"
- "Suggest missing indexes for performance"
- "Propose a mapping override for ambiguous hybrid areas"

Recommended libs (optional):
- `pydantic` for tool IO models
- whichever LLM provider you already use; do not make it a hard dependency

---

## 10) Phased delivery plan (with milestones)

### Phase 0 — Bootstrap (2–3 days)
- Project layout, packaging (`pyproject.toml`)
- CI skeleton (lint + unit tests)
- CLI skeleton
- DB connection config (env vars + config file)

### Phase 1 — Parser spike + normalized AST (1 week)
- Evaluate `libcypher-parser-python` vs ANTLR
- Implement normalizer for target subset:
  - MATCH patterns (single hop)
  - WHERE predicates (basic boolean + comparisons)
  - RETURN projections
  - LIMIT/SKIP
- Error reporting: "unsupported clause" and syntax errors

### Phase 2 — Mapping integration (1 week)
- Implement mapping acquisition:
  - connect via `python-arango`
  - run analyzer `export` (library call or tool contract)
  - store mapping bundle + optional TTL
- Implement "fast heuristic" classifier to avoid analyzer when clearly pure and user wants speed
- Cache mapping by schema fingerprint

### Phase 3 — Core translation for MATCH/WHERE/RETURN (2–3 weeks)
- Resolve labels/types via mapping
- Render hybrid-safe AQL for:
  - node scans
  - single-hop expansions (edge scan + DOCUMENT())
  - property filters
  - projection shaping
- Golden tests for dozens of queries across PG/LPG/hybrid mappings

### Phase 4 — Query language coverage (2–4 weeks)
- OPTIONAL MATCH
- WITH (pipeline semantics)
- ORDER BY
- Aggregations and grouping
- Path length `1..N` (bounded) expansions

### Phase 5 — Service + Cypher Workbench UI (2-3 weeks)
- FastAPI service with all endpoints (shipped): `/translate`, `/execute`, `/validate`, `/connect`, `/explain`, `/aql-profile`, `/cypher-profile` (see 4.3)
- Cypher Workbench UI (4.4): syntax-directed Cypher editor + AQL editor side-by-side, Explain/Profile, connection dialog
- Auth story: session-based token management, `.env` defaults, HTTPS for production
- Phased UI delivery per 4.4.9: v0.3-alpha through v0.4

### Phase 6 — Writes + advanced features (future)
- CREATE/MERGE/SET/DELETE translation
- Parameter handling parity with Neo4j drivers
- Better planner for performance, index hints, pattern reordering

---

## 11) Naming & repo strategy (your explicit questions)

### Should this be a new project?
**Yes.** The runtime and dependency model is fundamentally different from Foxx. Keep both until Python is mature.

### How should we name it?
Recommended:
- **Repo**: `arango-cypher-py`
- **Python package**: `arango_cypher` (import-friendly)
- **CLI**: `arango-cypher-py`

### Should we rename the existing `arango-cypher`?
Recommendation: **not yet**.
- If Python becomes primary later:
  - rename Foxx repo to `arango-cypher-foxx`
  - optionally move Python to `arango-cypher`

---

## 12) Proposed tech stack (summary)
- **DB**: `python-arango`
- **Schema mapping**: `arangodb-schema-analyzer` (library + tool contract)
- **Cypher parsing**: ANTLR4 openCypher grammar (in-repo `grammar/Cypher.g4`)
- **CLI**: `typer` + `rich`
- **Service**: `fastapi` + `uvicorn`
- **TTL parsing** (optional ingestion): `rdflib`
- **Testing**: `pytest`, `pytest-cov`, `httpx` (service tests), snapshot (`syrupy`)
- **Quality**: `ruff` + `mypy` (optional early; recommended by v0.2)
- **Frontend (Cypher Workbench UI)**:
  - **Framework**: React + TypeScript (Vite)
  - **Editor**: CodeMirror 6 (Cypher + AQL language modes via Lezer grammars)
  - **Graph viz**: Cytoscape.js
  - **Execution plan viz**: react-d3-tree or custom tree component
  - **Styling**: Tailwind CSS

---

## 13) Open questions (to resolve during Phase 1–2)
- Exact Cypher subset required for v0.1: do you need `WITH` immediately?
- Do you want translation parity with Foxx outputs, or "best AQL" even if it differs?
- Are there established canonical field names for LPG type fields in your environments (`type`, `_type`, `label`, etc.) or do we always rely on analyzer mapping?
- Any constraints on running native deps (if we choose `libcypher-parser-python`)?
