# arango-cypher-py — Product Requirements Document (Consolidated)

**Status:** Active development · **v0.1.0**
**Last updated:** 2026-06-29
**Owner:** arango-cypher-py maintainers

> **This is the single, all-encompassing PRD for the project.** It consolidates the
> product vision, architecture, requirements, current state, and roadmap that were
> previously spread across several documents. Those documents remain as the
> *detailed records of their respective tracks* and are the place to go for
> work-package breakdowns, dependency graphs, and historical changelogs:
>
> | Track | Detailed source of record |
> | --- | --- |
> | Full product PRD + per-feature changelog | [`python_prd.md`](./python_prd.md) |
> | Cypher→AQL coverage completion | [`cypher_coverage_plan.md`](./cypher_coverage_plan.md) |
> | Multi-tenant defense-in-depth | [`multitenant_prd.md`](./multitenant_prd.md) |
> | Work-package execution plan | [`implementation_plan.md`](./implementation_plan.md) |
> | Schema-inference bug fixes | [`schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md) |
> | Project assessment (outside-in critique) | [`2026-05-26-project-assessment.md`](./2026-05-26-project-assessment.md) |
> | Platform packaging / deployment | [`arango_packaging_service/`](./arango_packaging_service/) |
>
> Where this PRD and a source document disagree, **this PRD wins** for current
> intent; the source document wins for historical detail.

---

## 1. Executive summary

`arango-cypher-py` is a **Python-native NL → Cypher → AQL stack for ArangoDB**. It
runs **outside** the database (as a library, CLI, or HTTP service) and lets users
query an ArangoDB graph using [openCypher](https://opencypher.org/) — or plain
natural language — regardless of how the data is physically modeled.

Two paths share one engine:

1. **Cypher → AQL transpiler** — a *deterministic* translator from openCypher to
   ArangoDB Query Language across **property-graph (PG)**, **labeled-property-graph
   (LPG)**, and **hybrid** physical models.
2. **NL → Cypher pipeline** — an LLM generates *conceptual* Cypher from a
   natural-language question; the transpiler converts it to AQL. **The LLM never
   sees physical mapping details; the transpiler never uses an LLM.** This
   separation is a first-class architectural constraint, not a guideline.

The product also ships: a `arangodb-schema-analyzer`-driven mapping layer that
reverse-engineers a conceptual schema from a live database; a six-layer
multi-tenant safety architecture; a registry of `arango.*` extension functions
(search / vector / geo / document); schema-change detection with a two-tier cache;
and a browser-based **Cypher Workbench** UI for debugging and demos.

### 1.1 Defining decisions

- **Product scope.** The deliverable is the conversion *service* (library + CLI +
  HTTP) and the NL pipelines that run inside it. The Workbench UI exists to
  **debug** and **demo** the service; it is not a production multi-user workbench
  and is not deployed by default (see §11).
- **Conceptual schema is the query interface.** All Cypher — hand-written,
  LLM-generated, or NL-derived — is expressed against the *logical* schema, never
  the physical ArangoDB layout. The mapping layer absorbs the difference, which is
  what makes a single query portable across PG / LPG / hybrid.
- **Cypher is the intermediate representation, not AQL.** LLMs produce far better
  Cypher than AQL (training-data abundance + declarative/logical fit). A direct
  NL → AQL escape hatch exists (§5.3) but is explicitly secondary.
- **Schema analyzer is canonical.** `arangodb-schema-analyzer` is the single
  source of truth for what labels, types, and properties exist. A no-workaround
  policy fixes inference problems at the source rather than patching downstream.
- **Determinism by default.** Same Cypher + same mapping = same AQL, every time.
  Agentic and LLM enhancements are optional and never authoritative.
- **Safe AQL output.** Bind parameters (`@@collection`, `@param`) throughout;
  user input is never string-interpolated into queries.

---

## 2. Problem statement

ArangoDB supports multiple physical graph-modeling styles:

- **PG** — types-as-collections: one vertex collection per label, one edge
  collection per relationship type.
- **LPG** — generic collections + type discriminator: a single vertex collection
  with a `type` field, a single edge collection with a `type` field.
- **Hybrid** — a mixture of both, sometimes within a single query path.
- **GraphRAG-style** — a generic `Node` collection plus a generic, type-
  discriminated `relations` edge collection carrying thousands of relationship
  types (the open-vocabulary case).

Cypher is a conceptual, label/type-centric language. To execute it on ArangoDB we
need a conceptual schema (labels, relationship types, properties), a mapping to
physical collections and type fields, and a transpiler that emits safe, performant
AQL for the actual layout — including hybrid paths.

A Python implementation (vs. the in-database [Foxx sibling](https://github.com/ArthurKeen/arango-cypher-foxx))
enables richer parsing toolchains, strong typing and testing ergonomics, and easy
integration into notebooks, CLIs, services, and agentic workflows.

---

## 3. Goals / non-goals

### Goals (v0.1–v0.4)

- A deployable conversion service: **library + CLI + HTTP**, with a deterministic
  Cypher→AQL transpiler and an LLM-driven NL→Cypher pipeline.
- Translate a broad, documented subset of Cypher into **AQL + bind variables**.
- Optionally execute translated AQL against ArangoDB.
- Support **PG, LPG, and hybrid** physical models via `arangodb-schema-analyzer`.
- Make cross-tenant data leakage **structurally impossible** when deployed against
  a multi-tenant graph (§7).
- Deterministic behavior by default; agentic/LLM enhancements are optional.

### Non-goals

- Full openCypher TCK compliance in early versions (TCK is a *progressive* goal).
- A cost-based query optimizer equivalent to a database planner (a small internal
  logical plan is in scope; a full optimizer is not).
- The Workbench UI as a production multi-user product (no multi-user authn/authz,
  collaboration, server-side persistence, or UI-level tenant isolation).
- SLM fine-tuning inside this repo (the `LLMProvider` protocol reserves the hook).

### Success criteria (per version)

| Version | Representative criteria |
| --- | --- |
| v0.1 | 100% golden + integration tests; single-hop translation P95 < 50 ms |
| v0.2 | Write clauses compile and execute; live-DB mapping; CLI complete |
| v0.3 | UI connect→translate→execute→view without touching JSON; Movies corpus passes PG+LPG; TCK ≥ 25% |
| v0.4+ | TCK ≥ 60% (clause-focused subset already ~66%); agentic tool contract functional |

---

## 4. Users & use cases

**Personas:** data engineer (Neo4j-ish migration / exploration), application
developer (Cypher compatibility layer over ArangoDB), analyst / notebook user
(Cypher in Jupyter), agent workflow (stable translate/explain tool contract),
and **tenant user** of a deployed multi-tenant service (NL questions, must never
see another tenant's data).

**Core user stories:**

- *Translate-only* — "Given Cypher, show me AQL and bind vars."
- *Translate + execute* — "Run Cypher against database X, return JSON."
- *Ask in natural language* — "Who acted in 'Forest Gump'?" (typo tolerated) →
  conceptual Cypher → AQL → results.
- *Explain mapping* — "Show how labels/types mapped to collections/fields."
- *Validate* — "Warn if a query references unknown labels/types."
- *Stay in my tenant* — every NL/AQL query is provably scoped to the caller's
  tenant, or it is refused before execution.

---

## 5. Architecture

### 5.1 System overview

Two consumption modes share one engine. The FastAPI service hosts both the JSON
HTTP API and the built UI bundle; the same Python package can be imported directly
from application code. The transpiler is pure (no I/O); the NL pipeline reaches out
to LLM providers; the schema cache writes its persistent tier into the connected
ArangoDB itself so it survives restarts and is shared across replicas.

```
Clients (Workbench UI · application code)
        │
        ▼
FastAPI service (arango_cypher.service)
  ├── Cypher → AQL transpiler        (pure, deterministic)
  ├── NL → Cypher pipeline + tenant guardrail
  └── Schema cache + change detection
        │                    │
        ▼                    ▼
   ArangoDB             LLM providers
 (user data +        (OpenAI · Anthropic
  schema cache)        · OpenRouter)
```

### 5.2 Layered query interface (the core invariant)

```
Query authors (human · LLM · NL2Cypher)
  ↓ express queries in conceptual labels/types
Conceptual (logical) schema     ← from arangodb-schema-analyzer (canonical)
  ↓
Mapping layer (MappingBundle)   ← conceptual→physical resolution, styles, indexes
  ↓
Transpiler (translate_v0)       ← emits safe AQL + bind vars for the real layout
  ↓
Physical ArangoDB (PG / LPG / hybrid)
```

Consequences: **query portability** (one Cypher query runs against any layout),
**analyzer is canonical**, **NL operates at the logical level**, and **index
metadata lives in the mapping**, never in queries.

### 5.3 The two pipelines

**Stage 1 — NL → Cypher (LLM, non-deterministic).** Input: an NL question.
Context: the *conceptual* schema only (entity labels, relationship types,
properties, domain/range). Output: Cypher in conceptual terms. The LLM never sees
collection names, type discriminator fields, AQL, or the physical model style.

**Stage 2 — Cypher → AQL (deterministic, algorithmic).** Input: Cypher (from
Stage 1 or hand-written). Context: conceptual schema + physical mapping. Output:
AQL + bind variables. No LLM, no network calls (other than optional execution).

**Why Cypher as the IR, not AQL:** LLM priors (Cypher is far better represented in
training data), ontology alignment (labels map to ontology classes), portability
(conceptual Cypher is layout-independent), and ecosystem fit (the same pattern as
Neo4j Text2Cypher, LangChain `GraphCypherQAChain`, LlamaIndex).

**Direct NL → AQL (§5.3 escape hatch).** An opt-in path where the LLM sees the
*full physical schema* and emits AQL directly — used only when the transpiler
lacks a construct or an AQL-specific feature is required. It is structurally weaker
(non-deterministic, layout-coupled, lower LLM accuracy) and deliberately separate
from the primary path. Crucially, it is **no less safe**: the multi-tenant
security boundary (§7, Layer 5) applies to *every* execute path.

---

## 6. Product surfaces

### 6.1 Library API (primary)

```python
from arango_cypher import translate
from arango_query_core import MappingBundle

mapping = MappingBundle(
    conceptual_schema={"entityTypes": ["Person"], "relationshipTypes": ["KNOWS"]},
    physical_mapping={
        "entities": {"Person": {"style": "COLLECTION", "collectionName": "persons"}},
        "relationships": {"KNOWS": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "knows"}},
    },
)
result = translate("MATCH (n:Person) RETURN n.name", mapping=mapping)
result.aql, result.bind_vars
```

Also exposed: `nl_to_cypher` / `nl_to_aql`, `get_mapping` / `describe_schema_change`
/ `invalidate_cache`, `get_cypher_profile` / `validate_cypher_profile`, and the
`register_all_extensions` registry helpers.

### 6.2 CLI

`translate`, `run`, `mapping`, `doctor` subcommands (Typer + Rich, via the `[cli]`
extra).

### 6.3 HTTP service (FastAPI)

Endpoint families (all under `arango_cypher.service`):

- **Connection & session** — `POST /connect`, `POST /disconnect`,
  `GET /connections`, `GET /connect/defaults`.
- **Cypher → AQL** — `POST /translate`, `POST /execute`, `POST /validate`,
  `POST /explain`, `POST /aql-profile`, `GET /cypher-profile`.
- **NL → Cypher / AQL** — `POST /nl2cypher`, `POST /nl2aql` (responses carry
  `cached_tokens` for cost telemetry).
- **Schema** — `GET /schema/introspect`, `GET /schema/status`,
  `POST /schema/invalidate-cache`, `POST /schema/force-reacquire`,
  `GET /schema/statistics`, `POST /schema/index/create`.
- **Mapping** — `/mapping/export-owl`, `/mapping/import-owl`, `/suggest-indexes`.
- **Corrections (local learning)** — `POST|GET|DELETE /corrections` (Cypher→AQL),
  `POST|GET|DELETE /nl-corrections` (NL→Cypher).
- **Multi-tenant** — `GET /tenants[?collection=…]`; `tenant_context` field on the
  NL routes; `safe_execute`/EXPLAIN validation on every execute path.
- **Agentic tools** — `GET /tools/schemas`, `POST /tools/call`.

> **Operational note.** Agent/IDE shells inject `HTTP_PROXY`/`HTTPS_PROXY`/
> `ALL_PROXY` pointing at a loopback proxy that blocks DB traffic. Start the
> backend from a plain terminal or strip those vars first
> (`scripts/run_service.sh` does this).

### 6.4 Cypher Workbench UI (debug/demo)

React + TypeScript + Vite + CodeMirror 6. Chat-first, progressive-disclosure
workbench: an NL "Ask" composer, side-by-side Cypher/AQL editors, a query
inspector, results panel (table / JSON / graph via Cytoscape.js / explain /
profile), a schema-graph mapping view, a connection dialog with auto-introspect,
query history with bounded result snapshots, and local-learning ("Learn")
controls. UI architecture rules (object-centric canvas, left-click selects /
right-click acts, overlays over routes) are enforced project-wide.

> `ui/dist/` is gitignored; rerun `cd ui && npm run build` after pulling UI
> changes. The service logs a `UI bundle is stale` warning on drift.

---

## 7. Schema detection, mapping & change detection

### 7.1 Mapping model

`arangodb-schema-analyzer` (>=0.6.1,<0.7, from PyPI) is the **primary tier** for
all schema types. It produces a `MappingBundle` (conceptual schema + physical
mapping + metadata). When the analyzer is unavailable, a heuristic fallback runs
and emits an `ANALYZER_NOT_INSTALLED` warning; the service refuses to start on a
heuristic bundle unless `ARANGO_CYPHER_ALLOW_HEURISTIC=1`.

Physical styles the mapping layer resolves:

| Style | Entities | Relationships |
| --- | --- | --- |
| **PG** | one collection per type | `DEDICATED_COLLECTION` per type |
| **LPG** | generic collection + `typeField` (`LABEL` style) | `GENERIC_WITH_TYPE` |
| **Hybrid** | mix per type | mix per relationship type |
| **Embedded** | — | `EMBEDDED` (object/array on the parent doc) |

Property roles are classified (`identifier`, `name`, `categorical`, `temporal`,
`numeric`, `free_text`) and surfaced to both the NL prompt and the entity resolver
so name-vs-token matching strategies are chosen correctly. Open-vocabulary edge
collections are capped and normalized (per-type domain/range derived from
`_fromType`/`_toType`) so GraphRAG schemas render and resolve sanely.

### 7.2 Cardinality statistics

`compute_statistics(db, bundle)` stores per-collection counts, per-type label
counts, avg fan-out/fan-in, cardinality pattern (`1:1`/`1:N`/`N:1`/`N:M`), and
selectivity in `MappingBundle.metadata["statistics"]`. These feed traversal
direction, multi-part MATCH ordering, and filter placement in the transpiler, and
enrich the physical schema summary for the NL→AQL path.

### 7.3 Change detection & two-tier cache

Two fingerprints drive a cheap read-only probe: a **shape fingerprint** (collection
set + types + full index digests, stable under ordinary writes) and a **full
fingerprint** (shape + row counts). `describe_schema_change(db)` returns one of
`unchanged` / `stats_changed` / `shape_changed` / `no_cache`, letting long-running
services skip unnecessary re-introspection:

- `unchanged` → serve cached bundle (~1 ms)
- `stats_changed` → reuse mapping, refresh cardinality only (~50 ms)
- `shape_changed` / `no_cache` → full re-introspection

Caching is two-tier: a process-local dict in front of an ArangoDB-collection cache
(`arango_cypher_schema_cache` by default, excluded from its own fingerprints,
gated by `CACHE_SCHEMA_VERSION`). The persistent tier survives restarts and is
shared across replicas. Exposed via `GET /schema/status` and
`POST /schema/invalidate-cache`.

---

## 8. Cypher → AQL transpiler

### 8.1 Pipeline

1. **Parse** — Cypher → ANTLR4 parse tree (in-repo `grammar/Cypher.g4`,
   `arango_cypher/_antlr/`). No visitor; the renderer walks the tree directly.
2. **Resolve** — labels, relationship types, properties against the mapping
   (`MappingResolver`), with domain/range inference and `IS_SAME_COLLECTION`
   optimization.
3. **Emit** — parse tree + resolved mapping → AQL string + bind vars, with a
   post-pass that re-indents by `FOR` depth and prepends a `WITH` collection
   declaration for traversals.

The translator core lives in `arango_cypher/_translate_v0/` (`core.py`,
`writes.py`, `formatting.py`, shared `state.py` contextvars). Translation results
are LRU-cached (256 entries).

### 8.2 Supported subset (read)

`MATCH` (single/multi pattern parts, multiple clauses, multi-hop, bounded
variable-length `*1..n`); `WHERE` (boolean `AND`/`OR`/`NOT`/`XOR`, comparisons,
`IN`, `IS [NOT] NULL`, `STARTS WITH`/`ENDS WITH`/`CONTAINS`, regex `=~`);
`RETURN` (projections, aliases, `DISTINCT`, `ORDER BY`, `SKIP`, `LIMIT`); `WITH`
pipelines + aggregation (`count`, `avg`, `sum`, `min`, `max`, `collect`);
`WITH … MATCH`; `OPTIONAL MATCH` (companion and sole-reading-clause forms);
`UNWIND`; `CASE` (simple + generic); `UNION` / `UNION ALL`; list & pattern
comprehensions; named paths + `length`/`nodes`/`relationships`; `EXISTS { }` and
`COUNT { }` subqueries; label predicates on untyped vars
(`WHERE risk:RISK_FACTOR`); scalar/builtin functions and `arango.*` extensions
(§9); inline pattern properties; named parameters `$param`; dot-path property
access (`n.address.zip`).

### 8.3 Supported subset (write)

`CREATE` (nodes, relationships, whole-map params `CREATE (n $props)`,
`CREATE → SET/REMOVE`); `SET` (`=`, `+=`, property and whole-document forms);
`DELETE` / `DETACH DELETE`; `REMOVE` (property unset); `MERGE` (node and
single-hop relationship, with `ON CREATE` / `ON MATCH SET`); `FOREACH` (with
`SET`, and — newly — `CREATE` / `DELETE`).

**Recently closed write-clause gaps (2026-06):** unlabeled `SET`/`DELETE`/`REMOVE`
on `MATCH (n)`; **multiple `MERGE` clauses** in one statement; **multi-hop
relationship `MERGE`**; **`CREATE`/`DELETE` inside `FOREACH`**.

**The governing AQL constraint (ERR 1579).** AQL cannot read a collection after
modifying it, nor write the same collection twice, within one query. Every
multi-write form therefore *works when each write targets a distinct physical
collection* and otherwise **fails closed with an actionable error** (e.g.
same-collection multi-`MERGE`, repeated edge collections in a multi-hop `MERGE`,
`MATCH`-prefixed multi-`MERGE`, unbound relationship endpoints, multi-collection
unlabeled mutation, `DETACH DELETE` inside `FOREACH`). No query ever silently
writes the wrong collection. Same-collection multi-write needs a
multi-statement/transaction execution model the single-`AqlQuery` output cannot
represent — tracked as future work.

### 8.4 Not yet supported

Multiple relationship types in one hop (`[:A|B]`); typeless relationships
`-[r]-`; leading clauses other than `MATCH` at query start (this single
restriction blocks ~1,560 TCK scenarios — the largest available coverage lever);
native `shortestPath()` syntax (available via `CALL arango.shortest_path()`);
`CREATE`+`DELETE` in one query.

### 8.5 AI fallback for recoverable transpile failures

When a hand-written or NL-generated Cypher query fails to transpile on an
*unsupported but recognizable* construct, an optional LLM-backed fallback can
propose an equivalent. This never overrides the deterministic path; it is a
recovery affordance, surfaced with provenance.

---

## 9. `arango.*` extensions

A registry-gated namespace (`ExtensionRegistry` + `ExtensionPolicy`) exposes
ArangoDB-native capabilities to Cypher, arity-checked and allow/deny-list aware:

- **Search/text** — `bm25`, `tfidf`, `analyzer`, `like`, `levenshtein_distance`,
  `levenshtein_match`, `ngram_match`, `ngram_similarity`, `phrase`, `boost`,
  `min_match`, `tokens`, `soundex`, `regex_test`/`matches`/`replace`.
- **Vector** — `cosine_similarity`, `l2_distance`, `approx_near_cosine`,
  `approx_near_l2`.
- **Geo** — `distance`, `geo_distance`, `geo_contains`, `geo_intersects`,
  `geo_in_range`, `geo_point`.
- **Document** — `attributes`, `has`, `merge`, `unset`, `keep`, `zip`, `value`,
  `values`, `flatten`, `parse_identifier`, `document`.
- **Procedures** (`CALL arango.*`) — `fulltext`, `near`, `within`,
  `shortest_path`, `k_shortest_paths`.

The entity resolver emits a structured `IndexAdvisory` whenever a fuzzy name probe
runs against a field with no inverted/ArangoSearch coverage; the advisory carries a
ready `add_index` spec and is actionable from the UI via `POST /schema/index/create`.

---

## 10. NL → Cypher pipeline (Text2Cypher, hardened)

The pipeline implements the SOTA Text2Cypher reference architecture
(extract → resolve → retrieve → generate → execute/validate), packaged as WP-25:

- **Multi-provider LLM support** — `OpenAIProvider`, `AnthropicProvider`,
  `OpenRouterProvider`; auto-detected from API-key presence or pinned via
  `LLM_PROVIDER`. Without a key, a rule-based fallback runs (demo/offline only).
- **Dynamic few-shot retrieval (WP-25.1)** — `FewShotIndex` with BM25 over shipped
  `movies`/`northwind`/`social` corpora plus user-approved corrections.
- **Pre-flight entity resolution (WP-25.2)** — `EntityResolver` rewrites user
  string literals against the live DB before generation, combining exact /
  contains / reverse-contains / `LEVENSHTEIN_DISTANCE` scoring with a configurable
  `fuzzy_threshold`. Role-aware (identifier/name first), exact-first two-pass
  collection scans for performance on large collections, and `alt_labels` guidance
  for ambiguous types.
- **Execution-grounded validation (WP-25.3)** — translated AQL is run through
  `_api/explain` in a self-healing retry loop; semantic errors (missing
  collections, unbound vars) feed back into the next prompt.
- **Prompt caching (WP-25.4)** — cache-friendly section ordering; OpenAI automatic
  prefix caching; Anthropic explicit `cache_control` splits; `cached_tokens`
  propagated uniformly and surfaced on results and HTTP responses.
- **Graph-intent + value-shape awareness** — "show me … as a graph" yields a
  path-returning Cypher; value-shape hints steer the LLM toward fuzzy/`CONTAINS`
  matching of token-shaped fields instead of inventing legal names.
- **Evaluation harness + regression gate (WP-25.5)** — 31-case corpus
  (`movies_pg` + `northwind_pg`, 5 categories), reproducible runner, tolerance
  policy (5 pp / +20% / +0.3 retry), nightly CI matrix over OpenAI + Anthropic.

**Quality baseline (live, 31 cases, both fixtures seeded):**

| Metric | OpenAI `gpt-4o-mini` | Anthropic `claude-haiku-4-5` |
| --- | --- | --- |
| parse_ok | 100.0% | 100.0% |
| pattern_match | 93.5% | **100.0%** |
| typo category | 66.7% | **100%** |
| retries_mean | 0.000 | 0.000 |

### 10.1 Local learning (feedback loops)

Two independent correction stores. **Cypher→AQL** (`corrections.py`, SQLite, keyed
by `(cypher, mapping_hash)`) is a deterministic override and a transpiler-bug
discovery queue. **NL→Cypher** (`nl_corrections.py`) captures approved
`(question, cypher)` pairs and re-enters them into the BM25 few-shot retriever on
the very next request (appended after shipped corpora, so a user correction wins
BM25 ties). All data stays local.

### 10.2 Agentic tool contract

Eight JSON-in/JSON-out tools: `cypher_translate`, `suggest_indexes`,
`explain_mapping`, `cypher_profile`, `propose_mapping_overrides`,
`explain_translation`, `validate_cypher`, `schema_summary` — dispatched via
`/tools/schemas` + `/tools/call`. The `get_cypher_profile()` manifest
(`profile_schema_version`-versioned) lets NL/agent gateways discover the supported
subset programmatically.

---

## 11. Multi-tenant safety (defense-in-depth)

When deployed against a multi-tenant graph, two failure modes leak cross-tenant
data: **underconstraint** (LLM omits the tenant filter) and **injection** (user
names another tenant). These are data-leak-class defects. The architecture makes
leakage **structurally impossible, independent of LLM behavior**, via six layers:

| # | Layer | Mechanism | Status |
| --- | --- | --- | --- |
| 0 | Storage | Disjoint SmartGraphs (per-tenant shard key) + satellite collections | Partial (analyzer metadata adopted) |
| 1 | Session | Server-bound `@tenantId` from the authenticated session; body tenant ignored in tenant-user mode | **Done** |
| 2 | LLM | Manifest-aware prompt + few-shot + regex postcheck + retry | **Done** |
| 3 | Cypher AST | Algorithmic tenant-predicate injection on parsed Cypher | Phase 3a done (core); route wiring pending |
| 4 | AQL AST | Tenant-predicate injection on transpiled AQL; covers NL→AQL + `/execute-aql` | **Done** |
| 5 | Pre-execute | EXPLAIN-plan validator refusing any plan that scans a tenant-scoped collection without a bind-var tenant predicate | **Done** |
| 6 | Execute | `safe_execute` overrides client bind vars with the session tenant (session value wins) | **Done** |

**Key principles.** Layer 5 is the *security boundary* — if it passes, the query
is safe by definition; if it fails, the query does not run. **Fail-closed
everywhere** (unknown labels, missing manifest entries, unparseable plans →
refuse). **The LLM is never trusted** (Layer 2 reduces retry burden but is not
counted as a defense for audit). **Bind-variables only** — the tenant identifier
is never inlined as a literal; Layer 5 refuses literal tenant predicates and
bind-value mismatches against the session.

Detailed threat model (T1–T8), the formal definition of "safe", per-layer
specs, the red-team corpus, and remaining work (MT-6 plan-shape LRU, MT-7 admin
bypass + audit log, MT-8 security review) live in
[`multitenant_prd.md`](./multitenant_prd.md).

---

## 12. Testing & quality

- **Golden tests** — YAML fixtures in `tests/fixtures/cases/` and `cases_v03/`,
  exact AQL + bind-var matches.
- **Integration tests** — Movies (~170 nodes, 20-query corpus, PG + LPG),
  Northwind (14-query corpus), social (PG/LPG/hybrid), ICIJ Paradise Papers.
  Gated by `RUN_INTEGRATION=1` (Arango on host port 28529 / 28530).
- **Neo4j cross-validation** — every corpus query runs against Neo4j (the
  reference Cypher engine) *and* the translated AQL; result sets are diffed
  row-by-row (`assert_result_equivalent`). Two suites pass end-to-end: Movies
  20/20, Northwind 14/14. Gated by `RUN_INTEGRATION=1 RUN_CROSS=1`
  (`docker-compose.neo4j.yml`, Bolt 27687).
- **openCypher TCK harness** — 220 feature files / ~3,861 scenarios. Translation-
  only coverage: full TCK ~32%, clause-focused subset ~66% (full breakdown in
  `tests/tck/COVERAGE_REPORT.md`).
- **NL eval gate** — opt-in (`RUN_NL2CYPHER_EVAL=1`), nightly CI matrix over
  OpenAI + Anthropic against committed baselines.
- **CI** — `ci.yml` (ruff + unit + integration on Py 3.11/3.12 against Arango
  3.11) on every push/PR; `nl2cypher-eval.yml` nightly (non-blocking).

Engineering rules enforced repo-wide (see `.cursor/rules/`): read-before-write,
test-what-you-touch, verify-before-done, incremental-over-atomic,
comprehensiveness-over-simplification, wiring-over-deletion, modularity (source
files ≤ 1500 lines), and checkpoint-regularly.

---

## 13. Packaging & deployment

The default Arango Platform deployment (via ServiceMaker → Container Manager) is
**headless**: library, CLI, and the FastAPI HTTP endpoints. The Workbench UI is
*not* in the default tarball and *not* exposed by the Container Manager; any
UI-included variant must be opt-in, separately versioned, and carry the debug/demo
scope disclaimer. The persistent schema cache (a user-land collection in the
connected DB) lets containerized replicas share a warm cache and survive restarts.
See [`arango_packaging_service/`](./arango_packaging_service/) for the platform API
and deployment runbook.

**Naming / repos.** Repo `arango-cypher-py`, import package `arango_cypher`,
distribution `arango-cypher-py`, CLI `arango-cypher-py`. The in-database sibling is
[`arango-cypher-foxx`](https://github.com/ArthurKeen/arango-cypher-foxx). The bare
`arango-cypher` name is reserved for a potential umbrella/spec repo.

**Tech stack.** `python-arango`; `arangodb-schema-analyzer`; ANTLR4 openCypher
grammar; `typer`+`rich` (CLI); `fastapi`+`uvicorn` (service); `rdflib` (optional
OWL ingestion); `pytest`/`httpx`/`syrupy` (tests); `ruff`/`mypy` (quality);
React + TypeScript + Vite + CodeMirror 6 + Cytoscape.js + Tailwind (UI).

---

## 14. Current status

| Capability | Status |
| --- | --- |
| ANTLR4 parser + translation core (MATCH/WHERE/RETURN/WITH/OPTIONAL/UNWIND/CASE/UNION) | **Done** |
| Read subset incl. comprehensions, named paths, EXISTS/COUNT subqueries, label predicates | **Done** |
| Write clauses (CREATE/SET/DELETE/REMOVE/MERGE/FOREACH) incl. 2026-06 gap closures | **Done** (within ERR-1579 limits) |
| `arango.*` extension registry (search/vector/geo/document/procedures) | **Done** |
| MappingResolver + schema-analyzer integration (PG/LPG/hybrid) + heuristic fallback | **Done** |
| Property-role classification + open-vocab edge normalization | **Done** |
| Schema change detection + two-tier persistent cache | **Done** |
| Cardinality statistics | **Done** |
| NL → Cypher pipeline (WP-25.1–.5) + feedback loops + agentic tools | **Done** |
| NL → AQL direct path | **Done** |
| FastAPI service (connection/translate/NL/schema/mapping/corrections/tools) | **Done** |
| Cypher Workbench UI (chat-first redesign, schema graph, results, history, learning) | **Done** (debug/demo scope) |
| Neo4j cross-validation (Movies 20/20, Northwind 14/14) | **Done** |
| Multi-tenant Layers 1, 2, 4, 5, 6 | **Done** |
| Multi-tenant Layer 3 (Cypher AST injection) route wiring | **Partial** (core done) |
| Multi-tenant Layer 0 (storage) | **Partial** |
| openCypher TCK (clause-focused) | ~66% translation-only |

---

## 15. Roadmap

| Version | Focus |
| --- | --- |
| **v0.1** ✅ | Core read-only transpiler; mapping; extensions; service; UI; golden + integration tests |
| **v0.2** | Write-clause + aggregation completeness; live-DB mapping; CLI; TCK Match ≥ 40% |
| **v0.3** | Language breadth (full OPTIONAL MATCH, EXISTS, named paths); OWL round-trip; index-aware mapping; NL pipeline; UI completeness; TCK ≥ 25% |
| **v0.4+** | MERGE/FOREACH/comprehensions; optimization (filter pushdown, translation caching, relationship uniqueness); agentic tools; TCK ≥ 60% |
| **Multi-tenant** | MT-3b route wiring, MT-6 plan-shape LRU, MT-7 admin bypass + audit, MT-8 red-team |
| **Compiler architecture** | Normalized AST / logical plan; relax the leading-`MATCH` parser constraint (unblocks ~1,560 TCK scenarios) |

---

## 16. Open questions

**Transpiler / coverage.** Same-collection multi-write needs a
multi-statement/transaction execution model (the single-`AqlQuery` output can't
represent it) — adopt one, or keep failing closed? Should the leading-`MATCH`
constraint be relaxed (large TCK lever, non-trivial grammar/renderer work)?
Index-hint emission from `PropertyInfo.indexed` — when?

**NL pipeline.** Default fuzzy matching to portable `CONTAINS`/`=~` vs. invest
further in the ArangoSearch `arango.*` path as primary? Task decomposition /
multi-agent for complex multi-subquery questions — revisit after harness data.
SLM fine-tuning — separate research project; the `LLMProvider` hook is reserved.

**Multi-tenant.** Tenant identifier format (`_key` canonical) and hierarchical
tenants (`== @tenantId` vs `IN @allowedTenants`)? Write-operation policy against
satellite collections? Should Layer 4 rewrite anonymous traversals into named-graph
form? Does the Platform inject a tenant identifier via ingress header (so Layer 1
reads it instead of requiring it at `/connect`)?

**Product / UX.** Should the "create index" advisory become a dedicated Indexing
panel vs. the current inline strip? `App.tsx` exceeds the modularity cap —
extract a `useQueryActions` hook (tracked tech debt).

---

*End of consolidated PRD. For historical detail and per-feature changelogs, follow
the source-document links in the header.*
