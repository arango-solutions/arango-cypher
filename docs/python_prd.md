# Arango Cypher (Python) — PRD + Implementation Plan
Date: 2026-02-17  
Last updated: 2026-04-18  
Workspace: `arango-cypher-py`  
Related repos:
- `~/code/arango-cypher-foxx` (Foxx/JS implementation; renamed from `arango-cypher` on 2026-04-17 — see §11 naming resolution)
- `~/code/arango-schema-mapper` (a.k.a. `arangodb-schema-analyzer`, schema detection + mapping)

### Changelog
| Date | Changes |
|------|---------|
| 2026-04-18 | **WP-25 complete (NL→Cypher pipeline hardening).** All five sub-packages landed on `main`: WP-25.1 dynamic few-shot retrieval (BM25-backed `FewShotIndex` over shipped `movies/northwind/social` corpora), WP-25.2 pre-flight entity resolution (`EntityResolver` / `ResolvedEntity` with DB-path for both `COLLECTION` and `LABEL` mapping styles, plus a null-resolver fallback), WP-25.3 execution-grounded validation (`explain_aql` + EXPLAIN feedback in the retry loop), WP-25.4 prompt caching (cache-friendly section ordering in `PromptBuilder`, `cached_tokens` propagation from OpenAI `usage.prompt_tokens_details.cached_tokens` into `NL2CypherResult` / `NL2AqlResult` / HTTP responses, Anthropic `cache_control` splitter + live `AnthropicProvider` against `POST /v1/messages`), WP-25.5 eval harness + regression gate (`tests/nl2cypher/eval/{corpus.yml,configs.yml,runner.py,baseline.json}` + `tests/test_nl2cypher_eval_gate.py` with a 5 pp / +20 % / +0.3-retry tolerance policy, gated behind `RUN_NL2CYPHER_EVAL=1`; corpus subsequently grown to 31 cases across `movies_pg` + `northwind_pg`, baseline refreshed against live OpenAI gpt-4o-mini at parse_ok=100% / pattern_match=87.1%). HTTP surface (`/nl2cypher`, `/nl2aql`) now accepts `use_fewshot`, `use_entity_resolution`, `session_token` and returns `cached_tokens` + `retries`. Full unit suite still green (651 passing, 16 skipped). Known follow-ups: refresh `baseline.json` against a live LLM, stand up a real `AnthropicProvider` behind the existing splitter, and grow the eval corpus. |
| 2026-04-17 | **Clarified product scope: service is the product, UI is debug/demo.** Added a "Product scope" key decision to the Executive summary, a primary-product goal in §2, a matching non-goal carving out multi-user workbench features, a scope banner at the head of §4.4 (Cypher Workbench UI), and a "What gets deployed" note in §15 stating the default platform deployment is headless. Triggered by the realization (post-WP-25 scoping) that undirected UI expansion would compete for engineering cycles against the conversion service + NL pipelines, which are the actual deliverable. The UI remains valuable for two things — debugging translations during development, and demoing the service to prospects — but is explicitly **not** a production multi-user workbench: no authn/authz, no multi-tenant isolation, no server-side persistence beyond what the service already stores, no collaboration. Any future "UI-included" deployment variant must be opt-in and separately versioned. |
| 2026-04-17 | **Added §1.2.1 "SOTA techniques, current gaps, and hardening plan"** and scoped **WP-25 (NL→Cypher pipeline hardening)** into five sub-packages. Triggered by research notes in `docs/research/nl2cypher.md` and `docs/research/nl2cypher2aql_analysis.md`. The current `nl2cypher.py` implements the zero-shot baseline (logical-only prompt, ANTLR-based self-healing) — correct but minimal. SOTA has moved to: (1) dynamic few-shot retrieval from a curated NL→Cypher corpus, (2) pre-flight entity resolution for labels and property values, (3) execution-grounded validation via AQL `EXPLAIN` in the retry loop, (4) prompt caching on the schema prefix, (5) evaluation harness + regression gate. WP-25.1/.2/.3/.4 are parallelizable (disjoint modules, single merge point in `nl2cypher.py`); WP-25.5 runs after .1 and .2. Task decomposition / multi-agent orchestration and SLM fine-tuning are explicitly deferred (the `LLMProvider` protocol already accommodates a fine-tuned endpoint when the time comes). The §1.2 invariant — LLM sees only the conceptual schema — is preserved; few-shot examples are conceptual-Cypher and entity resolution only rewrites string literals. Multi-subagent prompts for parallel execution added as "Wave 4" in `docs/agent_prompts.md`. |
| 2026-04-17 | **Naming resolved (§11).** Project name stabilized as `arango-cypher-py`, symmetric with the newly renamed Foxx sibling `arango-cypher-foxx`. The `-py` / `-foxx` suffixes are honest about what each package is (Python out-of-process distribution vs. Foxx in-database microservice) and leave the bare `arango-cypher` name free for a potential future umbrella/spec repo on the `arango-solutions` org. Distribution name in `pyproject.toml`, CLI command, `[project.urls]` (target: `arango-solutions/arango-cypher-py`), READMEs, PRD, and implementation plan all aligned. Python import package remains `arango_cypher` (unchanged — no import breakage). No PyPI migration needed (never published). GitHub org rename (`arango-solutions/arango-cypher` → `arango-solutions/arango-cypher-py`) is pending org-admin action; until that lands, the `pushurl` points at the current URL and GitHub auto-redirects will keep working. Local checkout directory `~/code/arango-cypher-py` unchanged. |
| 2026-04-17 | **Added §15 "Packaging and deployment to the Arango Platform".** Confirmed via pypi.org 404 that `arangodb-schema-analyzer` (declared in the `[analyzer]` extra) is not published to any package index — the only real obstacle to packaging this repo for ServiceMaker. Decision: **fix it upstream by publishing the analyzer**, not by building packaging tooling in this repo. Rejected three alternatives (vendored wheels, git URL deps, monorepo vendoring) as "absorbing a cost that belongs upstream." Rejected a full packaging/deployment CLI in this repo (Typer `package`/`deploy`/`redeploy`/`teardown` subcommands) on scope, release-cadence, token-blast-radius, and deployment-volume grounds; any deployment CLI will live in a separate project or be contributed to ServiceMaker itself. What this repo now owns: a README section with the manual deploy path (three curl commands), a prerequisite checklist, and a CI smoke test that `uv sync` succeeds on the packaged tarball. Corresponding implementation plan entry: WP-19 in `docs/implementation_plan.md` shrunk accordingly. |
| 2026-04-17 | **Neo4j cross-validation harness + translator correctness fixes.** Added a side-by-side harness that seeds the same fixture into Neo4j Community (via `docker-compose.neo4j.yml`) and ArangoDB, runs each Cypher query against both engines, and asserts row-for-row equivalence (column-count match, row-count match, positional compare for `ORDER BY` / multiset compare otherwise, with scalar normalization for int↔float). Shipped two suites: `tests/integration/test_movies_crossvalidate.py` (20/20 pass) and `tests/integration/test_northwind_crossvalidate.py` (14/14 pass), gated behind `RUN_INTEGRATION=1 RUN_CROSS=1`. Cross-module seed guard (`ensure_dataset` in `tests/integration/neo4j_reference.py`) lets both corpora share the single writable Neo4j Community instance. Closed three translator correctness bugs surfaced by the harness, each resolving a previously `divergence`-flagged Movies query: **(1) 3-valued logic on numeric ordered comparisons** — `_compile_expression` now emits `!= null` guards on both operands of `<`/`<=`/`>`/`>=` in `WHERE` so Cypher's NULL-as-false semantics survive translation (new `_is_obvious_non_null` helper skips the guard on literal operands). **(2) `ORDER BY` scope after implicit `COLLECT`** — `_append_return_aggregation` now maps each grouping expression to its `COLLECT` alias, so a Cypher `ORDER BY p.name` after `COLLECT name = p.name` is rewritten to `SORT name` instead of referencing the out-of-scope `p`. **(3) Cypher relationship-uniqueness rule** — `_translate_match_body` emits cross-group `FILTER r_i._id != r_j._id` for single-hop fixed-length non-embedded relationships in the same pattern, so `(p)-[:R]->(m)<-[:R]-(q)` correctly excludes `q == p`. 24 stale goldens resynced via a new `scripts/update_goldens.py` (surgical YAML rewrite that preserves block-literal formatting + bind-var style). Unit suite: **561 passing**. Cross-validation: **34 passing** (20 Movies + 14 Northwind) — zero divergences remaining. |
| 2026-04-15 | **WS-F/G sprint.** Filter pushdown into traversals (PRUNE for variable-length, conservative rules). Relationship uniqueness enforcement (`r1._id != r2._id` for multi-relationship patterns). WITH pipeline from multiple MATCHes verified working + golden tests. rdflib OWL ingestion (`arango_query_core/owl_rdflib.py`, `[owl]` extra). ICIJ Paradise Papers dataset: mapping fixture, download/seed script, 5 query golden tests. Native `shortestPath()` deferred (needs Java for ANTLR regeneration). 494 tests pass. |
| 2026-04-15 | **WS-A/B/C/D sprint.** Full OPTIONAL MATCH with comma-separated pattern parts. Multi-label COLLECTION-style matching (uses primary label + warning). Native `shortestPath()` deferred (needs grammar). Clause outline panel. Sample queries loader (8 built-in queries). Profile-aware warnings (full scan detection). Correspondence hints (Cypher↔AQL hover highlighting). Bidirectional graph editing (add/edit/delete entities/relationships from schema graph). Agentic tool contract expanded: `propose_mapping_overrides`, `explain_translation`, `validate_cypher`, `schema_summary` (8 tools total). TCK convergence: 203 new feature files downloaded (220 total), harness expanded, 3864 scenarios collected, projected 66.1% pass rate on clause-focused subset. 487 tests pass. |
| 2026-04-15 | **WS-7/8/9 sprint.** ANTLR grammar extended with `EXISTS {}` subquery, `FOREACH`, `COUNT {}` subquery — parser regenerated (antlr4 v4.13.2). Relationship MERGE with ON CREATE/ON MATCH SET (DEDICATED_COLLECTION + GENERIC_WITH_TYPE). List comprehensions + pattern comprehensions verified working. Cytoscape.js integration: results graph view + schema graph view with click-to-inspect panels, replacing SVG-based rendering. 22 new golden tests (5 EXISTS/COUNT/FOREACH, 10 relationship MERGE + comprehensions, 7 integration). 503 tests pass (0 failures). TypeScript build clean. |
| 2026-04-15 | **Phase 1-2 completion sprint.** WS-2: All built-in functions verified implemented (toString, toInteger, toFloat, toBoolean, head, tail, last, range, reverse, id, keys, properties, type, labels). 10 new golden tests. WS-1: Regex `=~` verified, `collect()` in RETURN added, named paths verified, EXISTS pattern predicates verified. 16 new golden tests. WS-3: DETACH DELETE bug fixed, MERGE clause implemented (node MERGE with ON CREATE/ON MATCH SET). 17 new golden tests. WS-4: AQL format/prettify button, variable-use highlighting, Cypher hover documentation (20+ keywords, 25+ functions), multi-statement support. WS-5: NL→Cypher prompt leak audit, LLM validation/retry loop with ANTLR parsing, pluggable LLM providers (OpenAI + OpenRouter), enhanced AQL validation. WS-6: Security hardening (_sanitize_error, public mode, rate limiting), index population in heuristic builder, OWL round-trip completion. |
| 2026-04-14 | **Cardinality statistics for query optimization (§14.2).** Compute collection document counts, edge counts, per-entity label counts, avg fan-out/fan-in per edge collection, cardinality pattern classification (1:1, 1:N, N:1, N:M), and selectivity ratio. Statistics stored in `MappingBundle.metadata["statistics"]`, surfaced in schema summary. `MappingResolver` gains `estimated_count()`, `relationship_stats()`, `preferred_traversal_direction()`. NL→AQL prompt enriched with cardinality annotations so LLM starts from selective side. Transpiler uses stats for undirected pattern direction and multi-part MATCH ordering. New `GET /schema/statistics` endpoint. |
| 2026-04-14 | **AQL editor enhancements, local learning, and direct NL→AQL.** AQL editor now fully editable with syntax-directed editing: autocompletion (keywords, functions, snippet templates), scoped variable prediction, document property prediction from mapping, bracket auto-close, code folding, undo/redo history, search/replace. Local learning via corrections store (§14.1): SQLite-backed `corrections.db`, `POST/GET/DELETE /corrections` endpoints, Learn button in UI, corrections management panel. AQL indentation post-processor (`_reindent_aql`). Domain/range inference for PG-style dedicated edges (`_infer_dedicated_edge_endpoints`). NL query history (localStorage). Token usage display for LLM-based NL→Cypher. **Added §1.3 NL→AQL direct path** — opt-in alternative to the two-stage pipeline that bypasses Cypher and generates AQL directly from the LLM using the full physical schema. `POST /nl2aql` endpoint, UI toggle on Ask bar. Updated §4.4.3 (AQL editor spec), §4.4.7 (tech stack), §14.1 (local learning), implementation status tables, roadmap phasing. |
| 2026-04-13 | **Added §1.2 NL→Cypher→AQL two-stage pipeline definition.** This is now a first-class architectural pattern, not a feature bullet: LLM converts NL to Cypher using the ontology as prompt context (same pattern as LangChain's GraphCypherQAChain); deterministic transpiler converts Cypher to AQL. The LLM never sees physical details. Updated executive summary, implementation status table, and v0.3 roadmap to reference §1.2. Added §1.1 Architectural principle: logical schema as query interface. Added §5.7 Index-aware physical mapping model (VCI, persistent, TTL indexes in mapping). Added §7.8 Index-informed transpilation strategy. Added WP-17 (NL2Cypher) to v0.3 roadmap. Added WP-18 (Index-aware transpilation) to v0.3 roadmap. Added VCI and naked-LPG advisory to §5.3. Updated §10 with new WPs. |
| 2026-04-11 | Added §8.2/8.3 openCypher TCK and Neo4j dataset testing requirements. Added implementation status tables to §5.3, §5.4. Expanded §10 Phase 6. Added unified implementation status table. Added §2.1 success criteria. Added §6.4 supported Cypher subset. Added §7.5 error taxonomy. Added §7.6 multi-hop/path semantics. Added §7.7 performance considerations. Expanded security model. Added extension capability matrix. Unified phasing schemes. Resolved open questions. |
| 2026-04-10 | Property-enriched mappings (§5.5), domain/range optimization (§5.5.1), context-aware autocompletion (§4.4.2C), visual mapping graph editor spec (§5.6). |
| 2026-02-17 | Initial PRD. |

## Executive summary
Build a **Python-native Cypher → AQL transpiler** that runs **outside** ArangoDB (CLI/library/service), uses **`arangodb-schema-analyzer`** to produce a **conceptual schema + conceptual→physical mapping** (and optionally OWL Turtle), and can translate Cypher against **pure PG**, **pure LPG**, or **hybrid** physical ArangoDB models.

Key decisions:
- **Product scope.** The deliverable is the Cypher→AQL conversion service (§4.3) and the NL→Cypher→AQL / NL→AQL pipelines (§1.2, §1.3) that run inside it. The Cypher Workbench UI (§4.4) exists to **debug** the service (visualize translations, replay activity, inspect schema mappings) and to **demo** it to prospects. It is not a full-featured multi-user workbench and is **not deployed by default** alongside the service (see §15).
- **New project**: the Foxx implementation (originally named `arango-cypher`, renamed 2026-04-17 to `arango-cypher-foxx`) remains stable; this is a separate Python project published as `arango-cypher-py`, the symmetric Python sibling (§11).
- **Name**: repo `arango-cypher-py` (target GitHub location `arango-solutions/arango-cypher-py`; rename of the existing `arango-solutions/arango-cypher` repo is pending org-admin action), Python import package `arango_cypher`, distribution name `arango-cypher-py`, CLI command `arango-cypher-py`.
- **Schema mapping**: depend on `arangodb-schema-analyzer` as a library and optionally consume/produce OWL Turtle via its tool contract.
- **NL → Cypher → AQL** (two-stage pipeline, §1.2): use an LLM to convert natural language to Cypher (passing the ontology/conceptual schema as prompt context, same pattern as LangChain's `GraphCypherQAChain`), then use the **deterministic** transpiler to convert Cypher to AQL. The LLM never sees collection names, type fields, or AQL. The transpiler never uses an LLM. This separation is a first-class architectural constraint.
- **NL → AQL** (direct path, §1.3): optionally bypass the intermediate Cypher representation and have the LLM generate AQL directly. The LLM is given the full physical schema (collection names, edge collections, field names, type discriminators) so it can produce valid AQL. This is useful when the Cypher transpiler does not yet support a required construct, or when the user wants to leverage AQL-specific features.
- **Parsing (as implemented)**: ANTLR4-generated Python parser from the openCypher grammar in-repo (`grammar/Cypher.g4`). Re-evaluating `libcypher-parser-python` remains an optional future migration if native wheels and AST mapping prove worthwhile (see S6).
- **Agentic workflow** (optional): provide a stable JSON-in/JSON-out "tool" interface for translate/explain that can be used in agent pipelines, but keep translation correctness deterministic.

### Implementation status overview

Single source of truth for what is built, partial, or planned. Updated 2026-04-17 (Neo4j cross-validation).

| Capability | Status | Details |
|------------|--------|---------|
| **ANTLR4 parser + normalized AST** | Done | `grammar/Cypher.g4`, `arango_cypher/parser.py`, `arango_cypher/_antlr/` |
| **Core translation (MATCH/WHERE/RETURN)** | Done | Single node, single hop, multi-hop, variable-length, inline property filters, boolean/comparison/string predicates, ORDER BY, LIMIT, SKIP |
| **WITH pipeline + aggregation** | Done | Single/multiple leading MATCHes + WITH stages; aggregation in both WITH and RETURN; COLLECT cannot mix with other aggregates |
| **OPTIONAL MATCH** | Done | Multi-hop chains, node-only, comma-separated multiple pattern parts |
| **UNWIND** | Done | Standalone and in-query |
| **CASE expressions** | Done | Simple and generic forms |
| **UNION / UNION ALL** | Done | Via AQL subqueries |
| **Multi-label matching** | Done | LABEL-style: full support. COLLECTION-style: uses primary label with warning. |
| **Parameters (`$param`)** | Done | Positional params rejected |
| **Write clauses (CREATE/SET/DELETE)** | Done | CREATE, SET, DELETE, DETACH DELETE, MERGE (nodes + relationships with ON CREATE/ON MATCH SET) |
| **Named paths / path functions** | Done | `p = (a)-[:R]->(b)`, `length(p)`, `nodes(p)`, `relationships(p)` |
| **List/pattern comprehensions** | Done | List comprehensions `[x IN list WHERE filter | expr]`; pattern comprehensions `[(a)-[:R]->(b) | expr]` |
| **EXISTS / regex `=~`** | Done | Regex `=~` done; pattern predicates supported; `EXISTS { }` subquery implemented (grammar extended + transpiler) |
| **FOREACH / COUNT subquery** | Done | FOREACH with updating clauses; COUNT { } subquery via grammar extension |
| **`arango.*` extension registry** | Done | search, vector, geo, document functions + procedures (shortest_path, k_shortest_paths, fulltext, near, within) |
| **MappingResolver** | Done | Entity/relationship resolution, property resolution, domain/range inference, IS_SAME_COLLECTION optimization |
| **Schema analyzer integration** | Done | `acquire_mapping_bundle(db)`, `get_mapping(db)`, `classify_schema(db)` implemented. Analyzer is the **primary tier** for all schema types (PG, LPG, hybrid) since v0.1.0 (28/28 acceptance tests). Heuristic fallback when analyzer not installed. See §5.2.1. |
| **OWL Turtle round-trip** | Done | `MappingBundle.owl_turtle`; `/mapping/export-owl` and `/mapping/import-owl`; WS-6 completed import/`owlTurtle` round-trip path in the service and mapping loader. |
| **FastAPI service** | Done | 16+ endpoints shipped including `/nl2cypher`, `/schema/introspect`, `/mapping/export-owl`, `/mapping/import-owl`, `/suggest-indexes` |
| **Cypher Workbench UI** | Partial | Cypher editor (syntax highlighting, hover docs for keywords/functions, context-aware autocompletion, variable-use highlighting, parameter panel, multi-statement). AQL editor (**editable**, syntax-directed editing with autocompletion, snippet templates, format/prettify, scoped variable prediction, document property prediction, bracket auto-close, code folding, history, search/replace — see §4.4.3). Results panel (table/JSON/graph/explain/profile tabs). JSON mapping panel + schema graph view. Connection dialog with auto-introspect + database selector. NL2Cypher "Ask" bar with NL query history + token usage display. Query history. Export (CSV/JSON). **Local learning** (Learn button, corrections management panel — see §14.1). |
| **UI: results graph view** | Done | Cytoscape.js force-directed graph from `_id`/`_from`/`_to` fields. Click-to-inspect node properties panel. Dark theme. |
| **UI: visual mapping editor** | Done | Cytoscape.js schema graph with bidirectional editing (add/edit/delete entities & relationships via context menu). Changes sync to JSON editor. |
| **UI: parameter binding panel** | Done | Auto-detects `$param` tokens, JSON value entry, localStorage persistence |
| **UI: query history** | Done | Multi-entry history with persistence |
| **CLI (`translate`/`run`/`mapping`/`doctor`)** | Done | All 4 subcommands implemented via Typer + Rich. Requires `[cli]` optional extra. |
| **Index-aware physical mapping** | Partial | `IndexInfo` dataclass, `resolve_indexes()`, `has_vci()` on `MappingResolver`. VCI warning in transpiler. Heuristic builder populates indexes from DB. |
| **NL-to-Cypher pipeline** | Partial | §1.2: LLM path (OpenAI-compatible) + rule-based fallback. Pluggable providers (OpenAI + OpenRouter). Prompt leak audit; ANTLR validation/retry loop; enhanced AQL validation. Domain/range inference for PG-style dedicated edges (`_infer_dedicated_edge_endpoints`). Token usage tracking (prompt/completion/total). UI displays elapsed time + tokens. NL query history (localStorage). |
| **NL-to-AQL direct path** | Done | §1.3: Direct NL→AQL via LLM with full physical schema context. `POST /nl2aql` endpoint. UI toggle on Ask bar (Cypher vs AQL mode). `_build_physical_schema_summary()` for schema context. Requires LLM — no rule-based fallback. |
| **Agentic tool contract (`translate_tool`)** | Done | 8 tools: `cypher_translate`, `suggest_indexes`, `explain_mapping`, `cypher_profile`, `propose_mapping_overrides`, `explain_translation`, `validate_cypher`, `schema_summary`. `/tools/schemas` + `/tools/call` dispatch. |
| **Golden tests** | Done | YAML fixtures in `tests/fixtures/cases/` and `tests/fixtures/cases_v03/` |
| **Integration tests (datasets)** | Done | Movies full dataset (~170 nodes, 20-query corpus, PG + LPG variants), Northwind (14-query corpus), social dataset (PG/LPG/hybrid) |
| **Neo4j cross-validation harness** | Done | `docker-compose.neo4j.yml` + `tests/integration/neo4j_reference.py` (driver, `seed_neo4j_movies`, generic `seed_neo4j_pg`, `seed_neo4j_northwind`, cross-module `ensure_dataset` guard). Row-for-row equivalence asserted by `assert_result_equivalent` (column-count, row-count, ordered vs multiset compare, scalar normalization). Two suites passing end-to-end: Movies 20/20, Northwind 14/14. Gated behind `RUN_INTEGRATION=1 RUN_CROSS=1`. See §8.3.1. |
| **openCypher TCK harness** | Partial | 220 feature files, 3864 scenarios collected. Projected 66.1% pass rate (clause-focused). Requires live ArangoDB for full integration run. |

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

### 1.1 Architectural principle: logical schema as query interface

**All Cypher queries — whether written by hand, generated by an LLM, or produced by a NL-to-Cypher pipeline — are expressed against the *logical* (conceptual) schema, never against the physical ArangoDB layout.**

This is a first-class architectural constraint, not merely a design guideline. It motivates the entire system's layering:

```
┌────────────────────────────────────────────────────────┐
│  Query Authors  (human, LLM, NL2Cypher)                │
│  ↓ express queries using conceptual labels/types       │
├────────────────────────────────────────────────────────┤
│  Conceptual (Logical) Schema                           │
│  - Entity labels: Person, Movie, Company               │
│  - Relationship types: ACTED_IN, DIRECTED, KNOWS       │
│  - Properties: name, born, title, released             │
│  Source: arangodb-schema-analyzer (reverse-engineers    │
│          ontology from physical schema) or user-supplied│
├────────────────────────────────────────────────────────┤
│  Mapping Layer  (MappingBundle)                        │
│  - Conceptual → Physical resolution                    │
│  - Entity style (COLLECTION vs LABEL)                  │
│  - Relationship style (DEDICATED vs GENERIC_WITH_TYPE) │
│  - Index metadata (VCI, persistent, fulltext, geo)     │
├────────────────────────────────────────────────────────┤
│  Transpiler  (translate_v0.py)                         │
│  - Reads conceptual schema + physical mapping          │
│  - Generates safe, performant AQL for the actual layout│
│  - Leverages index metadata for optimization decisions │
├────────────────────────────────────────────────────────┤
│  Physical ArangoDB  (PG, LPG, or hybrid)               │
│  - Collections, edge collections, indexes              │
│  - The query author never needs to know this layer     │
└────────────────────────────────────────────────────────┘
```

**Consequences:**

1. **Query portability**: the same Cypher query works against PG, LPG, and hybrid physical layouts — the mapping layer absorbs the difference.
2. **Schema analyzer is canonical**: `arangodb-schema-analyzer` reverse-engineers the ontology (conceptual schema) from the physical schema. This is the **single source of truth** for what labels, types, and properties exist. The no-workaround policy (§5.2) ensures the analyzer improves at the source.
3. **NL-to-Cypher operates at the logical level**: any NL2Cypher pipeline receives the conceptual schema (entity labels, relationship types, property names) and generates Cypher against it. The transpiler handles the rest — the LLM never sees collection names, type fields, or AQL. See §1.2 for the full pipeline definition.
4. **Index information lives in the mapping, not in queries**: the physical mapping carries index metadata (§5.7). The transpiler uses this to make optimization decisions (edge filters vs vertex filters in traversals, index hints) without exposing physical details to the query author.

### 1.2 NL → Cypher → AQL: the two-stage pipeline

**This is the system's defining architectural pattern.** The pipeline has exactly two stages, with a clean separation of concerns:

```
┌──────────────────────────────────────────────────────────────┐
│  STAGE 1: NL → Cypher  (LLM-based, non-deterministic)       │
│                                                              │
│  Input:  Natural language question from the user             │
│  Context: Conceptual schema (ontology) —                     │
│           entity labels, relationship types, properties,     │
│           domain/range constraints                           │
│  Engine: LLM (OpenAI, Anthropic, local model, or any         │
│           provider — pluggable)                              │
│  Pattern: Same as LangChain's GraphCypherQAChain —           │
│           inject the ontology into the LLM prompt,           │
│           LLM generates Cypher against conceptual labels     │
│  Output: Cypher query expressed in conceptual terms           │
│          e.g. MATCH (p:Person)-[:ACTED_IN]->(m:Movie)        │
│               WHERE m.title CONTAINS "Matrix"                │
│               RETURN p.name                                  │
│                                                              │
│  The LLM NEVER sees:                                         │
│  - Physical collection names (nodes, edges, persons, movies) │
│  - Type discriminator fields (type, relation)                │
│  - AQL syntax                                                │
│  - Physical model style (PG, LPG, hybrid)                    │
├──────────────────────────────────────────────────────────────┤
│  STAGE 2: Cypher → AQL  (deterministic, algorithmic)         │
│                                                              │
│  Input:  Cypher query (from Stage 1, or hand-written)        │
│  Context: Conceptual schema + physical mapping               │
│           (MappingBundle from schema analyzer or heuristics)  │
│  Engine: translate_v0.py — ANTLR4 parser + AQL renderer      │
│  Output: AQL query + bind variables, ready to execute        │
│          against ArangoDB                                     │
│                                                              │
│  This stage is ALWAYS deterministic:                         │
│  - Same Cypher + same mapping = same AQL, every time         │
│  - No LLM involvement                                        │
│  - No network calls (other than optional AQL execution)      │
└──────────────────────────────────────────────────────────────┘
```

**Why Cypher as the intermediate representation — not AQL:**

- **LLM priors**: LLMs have extensive training data for Cypher (Neo4j's query language). They generate more accurate Cypher than AQL because Cypher is vastly more represented in training corpora.
- **Ontology alignment**: Cypher's label/type system (`Person`, `ACTED_IN`, `Movie`) maps directly to ontology classes and object properties. The LLM reasons in domain terms, not physical storage terms.
- **Portability**: Cypher generated against a conceptual schema works unchanged across PG, LPG, and hybrid physical models. If we generated AQL directly, the LLM would need to know the physical model — breaking the abstraction.
- **Ecosystem**: this is the same pattern used by Neo4j's Text2Cypher, LangChain's `GraphCypherQAChain`, and LlamaIndex's `KnowledgeGraphQueryEngine`. We are not inventing a new pattern; we are implementing the established one with ArangoDB as the backend instead of Neo4j.

**What the LLM prompt contains** (Stage 1 schema context):

```
Entity types:
  - Person (properties: name, born)
  - Movie (properties: title, released, tagline)

Relationship types:
  - ACTED_IN (Person → Movie, properties: roles)
  - DIRECTED (Person → Movie)
  - FOLLOWS (Person → Person)
  - PRODUCED (Person → Movie)
  - REVIEWED (Person → Movie, properties: summary, rating)
  - WROTE (Person → Movie)
```

This is the conceptual schema — the same ontology regardless of whether the data is stored in 8 collections (PG), 2 collections (LPG), or a mix (hybrid). The LLM generates Cypher against these labels. The transpiler resolves the physical details.

**Implementation requirements:**

1. **LLM provider is pluggable** — the system must not be hard-coded to any single provider. Support OpenAI, Anthropic, and local models via a provider interface.
2. **Schema context builder** — a function that takes a `MappingBundle` and produces the text prompt fragment describing entity types, relationship types, properties, and domain/range constraints.
3. **No rule-based fallback as primary** — a lightweight rule-based fallback may exist for demo/offline scenarios, but it is not the primary NL2Cypher path. The LLM-based path is the product.
4. **Validation loop** — after the LLM generates Cypher, the transpiler parses it. If parsing fails, the error is fed back to the LLM for a retry (similar to LlamaIndex's self-correction workflow).
5. **The transpiler is the primary path to AQL** — the deterministic transpiler from Cypher is the primary path. A direct NL→AQL mode exists as an alternative (§1.3) but is explicitly opt-in.

### 1.2.1 SOTA techniques, current gaps, and hardening plan

The §1.2 pipeline as implemented today (`arango_cypher/nl2cypher.py`) is a **correct but minimal** instantiation of the pattern: zero-shot system prompt containing the conceptual schema, single LLM call, one retry on ANTLR parse failure. The 2025-2026 state of the art for Text2Cypher has moved substantially past this baseline (see research notes in `docs/research/nl2cypher.md` and `docs/research/nl2cypher2aql_analysis.md`). This section records what SOTA looks like, what we already have, and exactly which gaps we intend to close.

**SOTA Text2Cypher reference architecture** (the pattern Neo4j, LangChain, and LlamaIndex converge on):

1. **Extract** entities from the user query (lightweight NER / rule-based / small LLM).
2. **Resolve** those entities against the database using vector search or an inverted index (so "Forest Gump" is rewritten to "Forrest Gump" before the main LLM call).
3. **Retrieve** dynamic few-shot (NL question, Cypher answer) examples whose intent matches the new question (BM25 or embedding-similarity over a curated corpus).
4. **Generate** the Cypher query — logical schema + resolved entities + retrieved examples in the prompt.
5. **Execute & validate** via an agent loop: syntactic parse check, then a low-cost semantic check (e.g. `EXPLAIN` against the live database) so collection/property hallucinations trigger a corrective retry rather than surfacing to the user.

**Gap analysis against the current implementation:**

| SOTA technique | Current state (`nl2cypher.py`) | Gap |
|----------------|--------------------------------|-----|
| Logical-only LLM prompt | `_build_schema_summary()` strips physical details; numeric-like / sentinel hints included | **Done** (core §1.2 invariant). |
| Self-healing retry | `_call_llm_with_retry()` retries on ANTLR parse failure, feeds error back to LLM | **Partial** — syntactic only. No semantic / execution-grounded retry. |
| Dynamic few-shot | `_SYSTEM_PROMPT` is zero-shot; no retrieval layer | **Missing.** This is the highest-ROI upgrade — we already have three curated corpora (`tests/fixtures/datasets/{movies,northwind,social}/query-corpus.yml`) whose `description`→`cypher` pairs are exactly what a few-shot retriever wants. |
| Entity resolution | LLM guesses string literals; `_fix_labels()` post-hoc rewrites hallucinated labels to mapping terms via fuzzy/role-synonym match | **Partial** — labels only. Property values (names, titles) are not resolved pre-LLM, so typos and variant spellings fail silently at execution time. |
| Execution-grounded validation | None | **Missing.** The retry loop stops at ANTLR. A Cypher query that parses but references a nonexistent label, property, or value will only surface the failure after AQL execution. |
| Prompt caching | None | **Missing.** The full schema is re-sent on every request. For schemas of any size this is wasteful; both OpenAI prompt caching (automatic above a token threshold) and Anthropic `cache_control` blocks are trivially available and cut token cost significantly. |
| Task decomposition / multi-agent | None | **Deferred.** Complex multi-subquery questions ("top 3 movies by each director, excluding documentaries") are a known weakness of single-shot generation. Decomposition is out of scope for the initial hardening pass; re-evaluate after the evaluation harness (below) reveals whether we are ceiling-bound on single-shot generation. |
| Small-model fine-tuning (SLM) | None | **Out of scope for this repo.** Fine-tuning Qwen-2.5 / Llama-3-8B on a curated ArangoDB Text2Cypher corpus is the 2026 SOTA for production latency/cost, but belongs in a separate research project with a GPU training pipeline and a versioned model registry. Reserve the hook: the `LLMProvider` protocol already lets a fine-tuned endpoint drop in unchanged. |

**The direct NL→AQL path (§1.3) is structurally disadvantaged.** LLMs produce noticeably weaker AQL than Cypher because AQL is under-represented in training data and is a procedural/physical language rather than a declarative/logical one (see `docs/research/nl2cypher2aql_analysis.md` for the full argument). The conclusion is **not** to remove §1.3 — it remains the escape hatch when the transpiler lacks a construct — but to **double down on §1.2 as the primary path** and to apply the SOTA upgrades there first. The same techniques (few-shot, entity resolution, execution-grounded validation) would help §1.3 too and should be layered in once §1.2 is hardened.

**Hardening plan (WP-25 in the implementation plan, scoped for multi-subagent execution):**

| Sub-package | Technique | Effort | Impact | Depends on | Status |
|-------------|-----------|--------|--------|-----------|--------|
| **WP-25.1** | Dynamic few-shot retrieval | Low (3–5 d) | High | — | **Done (2026-04-18)** |
| **WP-25.2** | Pre-flight entity resolution (labels + values) | Medium (5–7 d) | High | ArangoSearch view (optional; BM25/regex fallback when absent) | **Done (2026-04-18)** |
| **WP-25.3** | Execution-grounded validation loop (AQL `EXPLAIN`) | Medium (5 d) | Medium | WP-25.1 (for non-regression comparison) | **Done (2026-04-18)** |
| **WP-25.4** | Prompt caching (OpenAI + Anthropic) | Low (2 d) | Cost-only (no accuracy gain) | — | **Done (2026-04-18)** — OpenAI cached-token telemetry live; `AnthropicProvider` wired end-to-end against `POST /v1/messages` with `cache_control: {type: "ephemeral"}` splits, `cache_read_input_tokens` propagated to `cached_tokens`, and registered in `get_llm_provider()` (explicit `LLM_PROVIDER=anthropic` and auto-detect on `ANTHROPIC_API_KEY`). |
| **WP-25.5** | Evaluation harness & regression gate | Medium (3–4 d) | Meta (measures all of the above) | WP-25.1, WP-25.2 | **Done (2026-04-18)** — corpus (31 cases across `movies_pg` + `northwind_pg`, 5 categories) + configs + runner + CLI shipped. `baseline.json` refreshed against live OpenAI gpt-4o-mini under the `full` config: parse_ok=100%, pattern_match=87.1% (baseline / few_shot_bait / hallucination_bait / multi_hop = 100%; typo = 33%, the predicted gap when no DB is wired so WP-25.2 entity resolution falls back to no-op). Live gate (`RUN_NL2CYPHER_EVAL=1`) self-passes against the committed baseline. |

WP-25.1 / .2 / .3 / .4 executed in parallel during the Wave-4 rollout (disjoint modules with a single merge point in `arango_cypher/nl2cypher/_core.py`'s `PromptBuilder`). WP-25.5 followed once .1 and .2 had landed. Ready-to-launch sub-agent prompts remain archived in `docs/agent_prompts.md` under "Wave 4".

**Follow-ups carried out of WP-25:**

- ~~Refresh `tests/nl2cypher/eval/baseline.json` with a real-LLM report~~ Done 2026-04-18 (OpenAI gpt-4o-mini, `full` config, 31 cases, parse_ok=100%, pattern_match=87.1%). ~~Remaining: turn the gate on in nightly CI (currently opt-in via `RUN_NL2CYPHER_EVAL=1`).~~ Done 2026-04-18 (Wave 4k): new `.github/workflows/nl2cypher-eval.yml` — separate workflow from `ci.yml` so paid LLM calls run on a `cron: "0 6 * * *"` schedule (plus `workflow_dispatch` for manual refreshes), spins up the same `arangodb/arangodb:3.11` service as the `integration` job, seeds `nl2cypher_eval_movies_pg` + `northwind_cross_test` via the existing `tests/integration/datasets.py` seeders, then runs `tests/test_nl2cypher_eval_gate.py::test_gate_against_baseline` with `RUN_NL2CYPHER_EVAL=1 NL2CYPHER_EVAL_USE_DB=1`. Required GitHub repo secrets: **`OPENAI_API_KEY`** (or **`OPENROUTER_API_KEY`** — the test self-skips and exits 0 if neither is set, so unforked clones don't red-light). Estimated cost: ~$0.05 per nightly run at gpt-4o-mini × 31 cases. Failed runs upload `tests/nl2cypher/eval/reports/` as a 14-day artifact for triage. The workflow does **not** block PRs (intentionally — model variance vs. a 5 pp tolerance is acceptable for a regression signal but not for a merge gate). **Wave 4l (2026-04-20)** extended the workflow to a `strategy.matrix` of two providers — `openai` (calibrated against `baseline.json`) and `anthropic` (calibrated against `baseline.anthropic.json`, using `claude-haiku-4-5`). Each row is independent (`fail-fast: false`) so a single-provider regression doesn't mask the other. Baseline selection is wired via the new `NL2CYPHER_EVAL_PROVIDER` env var, which `test_gate_against_baseline` reads through the new `_baseline_path_for_provider()` helper (6 new unit tests pin the contract). Extra optional secret: **`ANTHROPIC_API_KEY`** (~$0.10 / nightly at Haiku 4.5 × 31 cases; row self-skips if unset).
- ~~Implement the Anthropic provider behind the existing `AnthropicProvider` stub and verify `cache_read_input_tokens` propagates through to `cached_tokens`.~~ Landed on `main` after WP-25 closure; the live cache hit is verified by the opt-in smoke test `tests/test_nl2cypher_caching.py::TestAnthropicLiveSmoke` (gated on `ANTHROPIC_API_KEY`).
- ~~Cross-provider baseline run with Anthropic + cache-hit measurement.~~ Done 2026-04-20 (Wave 4l): full live sweep against Anthropic `claude-haiku-4-5` (31 cases, both seeded fixtures, WP-25.2 + WP-25.3 engaged). Headline: **parse_ok 100% / pattern_match 100% / retries_mean 0** — every category (baseline / few_shot_bait / typo / hallucination_bait / multi_hop) at 100%, beating the OpenAI gpt-4o-mini baseline (93.5% pattern_match) by 6.5 pp. Notably the `typo` category lifts from 67% to 100% out-of-the-box because Claude Haiku handles edit-distance fuzziness in the generation step itself (independent of the WP-25.2 `LEVENSHTEIN_DISTANCE` resolver). Mean tokens 522 vs. 457 for OpenAI; mean latency 3.4s vs. 3.2s — both within noise. Committed as `tests/nl2cypher/eval/baseline.anthropic.json`. **Cache-hit measurement:** Haiku 4.5 requires a 4096-token minimum cacheable prefix (per Anthropic docs), and our prompts are ~500 tokens — so the Haiku eval reports `cached_tokens_mean=0`. End-to-end cache plumbing is separately **proven** with `claude-sonnet-4-5` (1024-token floor): cold call served 0 cached, warm call with an identical 2346-token cacheable prefix served **2346/2357 input tokens from cache (99.5%)**, confirming `split_system_for_anthropic_cache` + `cache_read_input_tokens` → `cached_tokens` propagation works.
- ~~Expand the eval corpus beyond the initial 13 cases across movies / northwind / social datasets.~~ Grown to **31 cases** (movies_pg: 21, northwind_pg: 10) on 2026-04-18: 9 baseline, 6 few_shot_bait, 6 typo, 7 hallucination_bait, 3 multi_hop. Further growth (social variant + cross-mapping) is welcome but no longer blocking the gate's regression signal.
- ~~Wire a live ArangoDB into `runner._main` so the `full` config actually exercises WP-25.2 and WP-25.3.~~ Done 2026-04-18 (Wave 4g): `tests/nl2cypher/eval/runner.py` gained `open_eval_db_handles()` (env-var-driven, per-fixture map keyed off `NL2CYPHER_EVAL_<FIXTURE>_DB`, defaults `nl2cypher_eval_movies_pg` / `northwind_cross_test`), `run_eval`/`run_case` accept `db_for_fixture=`, and the CLI gained `--with-db`. The live gate honors `NL2CYPHER_EVAL_USE_DB=1`. **Bug fix in the same commit:** `db` was previously gated on `use_execution_grounded` only, so the `few_shot_plus_entity` config silently skipped WP-25.2; the gate is now `use_execution_grounded OR use_entity_resolution`, restoring the intended config semantics.
- ~~Extend `EntityResolver` with fuzzy/edit-distance matching so typos like "Forest Gump" → "Forrest Gump" actually resolve against a live DB.~~ Done 2026-04-18 (Wave 4h): `_query_label_property` AQL now combines four scoring strategies — exact (1.00), contains (0.85), reverse-contains (0.70), and a new normalized `LEVENSHTEIN_DISTANCE` branch (≤ 0.90, gated by a configurable `fuzzy_threshold` defaulting to 0.7). Live verification on the seeded `nl2cypher_eval_movies_pg` DB resolves "Forest Gump" → "Forrest Gump" (0.82), "Toms Hanks" → "Tom Hanks" (0.81), and "Keenu Reeves" → "Keanu Reeves" (0.82) without false positives on truly absent entities ("Stephen Spielbreg" stays unresolved). 4 new unit tests pin the bind-var contract and the AQL-includes-LEVENSHTEIN invariant.
- ~~Refresh `baseline.json` once Wave 4g + 4h land so the typo category lifts in one step.~~ Done 2026-04-18 (Wave 4i): live OpenAI gpt-4o-mini run with `--with-db` against the seeded fixtures + fuzzy resolver. Headline: parse_ok=100% (unchanged), pattern_match=**90.3%** (↑ from 87.1%), typo=**66.7%** (↑ from 33.3%, +33.4 pp), retries_mean=0.03. One model-variance regression noted: hallucination_bait dipped to 85.7% (1 case of 7 — `eval_030` "List all actors" produced a less-precise `MATCH (p:Person) RETURN p.name` instead of the expected `Person.*ACTED_IN` join); within the gate's 5 pp tolerance and not reproducible across runs, but logged here so a future tightening can revisit.
- ~~Lift `eval_030`-style hallucination_bait robustness via few-shot enrichment.~~ Done 2026-04-18 (Wave 4j): added three canonical role-noun examples to `arango_cypher/nl2cypher/corpora/movies.yml` ("List all actors?", "Who are all the directors?", "List every writer in the database?") so the BM25 retriever surfaces the `Person + role-edge + DISTINCT` pattern when the user asks for "actors / directors / writers / producers". 5×5 replay of `eval_030` confirms the lift is deterministic, not noise. Refreshed baseline: pattern_match **93.5%** (↑ from 90.3%), hallucination_bait **100%** (recovered, ↑ from 85.7%), retries_mean **0** (↓ from 0.03). Live gate self-passes in 93 s.
- ~~Public schema-change detection API + persistent mapping cache so long-running services can skip unnecessary re-introspection.~~ Done 2026-04-20 (Wave 4m): replaced the single `_schema_fingerprint` (which hashed `name:type:count:idx_count` — and thus flipped on every row insert, *and* missed index shape changes that didn't alter the index count) with two orthogonal fingerprints: **shape** (collections + types + full index digests including VCI / `deduplicate` flags per Upstream Issue 01) and **full** (shape + per-collection row counts). New public API: `describe_schema_change(db) → SchemaChangeReport` for cheap read-only probes (≈ 20 ms for a 50-collection DB, no document sampling or AQL `COLLECT`), and `invalidate_cache(db)` for explicit cache busts. `get_mapping()` gained three routes: **unchanged** → serve from cache as before; **stats_changed** (shape stable, counts drift) → reuse cached conceptual + physical mapping and refresh only cardinality statistics (~50 ms vs. ~2–30 s for a full re-introspection); **shape_changed** / **no_cache** → full re-introspection. Added a persistent cache layer (`arango_cypher/schema_cache.py::ArangoSchemaCache`) backed by a user-land collection (default `arango_cypher_schema_cache`, excluded from its own fingerprints to avoid self-invalidation) and gated by `CACHE_SCHEMA_VERSION` so a future format bump silently ignores stale docs. Disable persistence per-call with `cache_collection=None` (for read-only DB users); force a rebuild with `force_refresh=True`. 23 new unit tests cover: fingerprint stability under row-count drift, fingerprint sensitivity to index-uniqueness flips (defence against the pre-existing index-count-only bug), `MappingBundle` round-trip through `bundle_to_doc` / `bundle_from_doc`, cache corruption / stale-version tolerance, stats-only refresh path, and cache-collection exclusion from fingerprints. The persistent tier lets containerized Arango Platform deployments share a warm cache across service instances and survive restarts without re-introspecting.

**Non-goals of WP-25:**

- No task decomposition / multi-agent orchestration in this pass (revisit after harness data).
- No SLM fine-tuning in this repo.
- No change to the §1.2 invariant that the LLM sees only the conceptual schema (few-shot examples and resolved entities extend the prompt but do not leak physical details — the examples themselves are conceptual-Cypher).

### 1.3 NL → AQL: the direct path (optional alternative) *(implemented)*

As a complement to the two-stage pipeline (§1.2), the system also supports **direct NL→AQL generation** where the LLM generates AQL without an intermediate Cypher representation.

```
┌──────────────────────────────────────────────────────────────┐
│  NL → AQL  (LLM-based, non-deterministic, opt-in)           │
│                                                              │
│  Input:  Natural language question from the user             │
│  Context: FULL physical schema —                             │
│           collection names, edge collection names,           │
│           field names, type discriminators,                   │
│           graph topology (what connects to what)             │
│  Engine: LLM (same providers as §1.2)                        │
│  Output: AQL query + bind variables, ready to execute        │
│                                                              │
│  The LLM SEES the physical model:                            │
│  - Collection names, edge collection names                   │
│  - Type discriminator fields and values                      │
│  - Property/field names                                      │
│  - Graph topology (traversal paths)                          │
└──────────────────────────────────────────────────────────────┘
```

**When to use the direct path instead of the two-stage pipeline:**

1. **Cypher transpiler gaps**: the deterministic transpiler does not yet support a required Cypher construct (e.g., complex aggregation patterns, AQL-specific features like `COLLECT AGGREGATE`, `PRUNE`, `SHORTEST_PATH`).
2. **AQL-specific features**: the user wants to leverage ArangoDB-specific capabilities that have no Cypher equivalent.
3. **Complex multi-hop queries**: some complex traversal patterns are more naturally expressed in AQL, and the LLM can generate them directly with the physical schema.
4. **Rapid prototyping**: skip the Cypher intermediate step when iterating on complex queries.

**Trade-offs vs. the two-stage pipeline:**

| Aspect | Two-stage (§1.2) | Direct (§1.3) |
|--------|-------------------|---------------|
| Determinism | Transpiler stage is deterministic | Fully non-deterministic |
| Physical model coupling | LLM never sees physical details | LLM sees full physical model |
| Portability | Same Cypher works across PG/LPG/hybrid | AQL is model-specific |
| LLM accuracy | High (LLMs trained on Cypher) | Lower (less AQL training data) |
| Feature coverage | Limited by transpiler | Limited by LLM capability |
| Correctability | Transpiler bugs can be fixed; corrections store works | LLM output varies between calls |

**Implementation:**

- Backend: `nl_to_aql()` function in `arango_cypher/nl2cypher.py`, `POST /nl2aql` endpoint in `service.py`
- Schema context: `_build_physical_schema_summary()` — provides collection names, edge collections, field names, type discriminators, and traversal topology
- LLM prompt: AQL-specific system prompt with ArangoDB query rules and conventions
- Validation: syntactic check for AQL keywords (`FOR`, `RETURN`, etc.) with retry loop
- UI: toggle on the Ask bar to switch between "Cypher" (two-stage, default) and "AQL" (direct) modes

---

## 2) Goals / non-goals

### Goals (v0.1–v0.3)
- **Primary product**: a deployable conversion service (library + CLI + HTTP) with a deterministic Cypher→AQL transpiler and an LLM-driven NL→Cypher pipeline. The UI (§4.4) is a debug/demo surface, not a separately supported product.
- **Translate** a defined subset of Cypher into **AQL + bind variables**.
- **Execute** translated AQL against ArangoDB (optional convenience wrapper).
- **Support PG, LPG, and hybrid** via `arangodb-schema-analyzer` mapping.
- Provide:
  - **Library API** (callable from other Python code)
  - **CLI** (run cypher, print AQL, execute, show results)
  - Optional **HTTP service** (translate/execute endpoints)
- Deterministic behavior by default; agentic enhancements are optional and non-authoritative.

### 2.1 Success criteria per phase

| Phase | Criteria | Target |
|-------|----------|--------|
| v0.1 | Golden tests passing (MATCH/WHERE/RETURN/WITH/ORDER BY/LIMIT) | 100% of golden test corpus |
| v0.1 | Translation P95 latency (single-hop queries) | < 50 ms |
| v0.1 | Integration tests passing (Movies + social datasets) | 100% |
| v0.2 | Write clause support (CREATE at minimum) for TCK setup | CREATE compiles and executes |
| v0.2 | TCK Match feature scenarios passing | ≥ 40% of Match*.feature |
| v0.2 | Schema analyzer integration (`acquire_mapping_bundle()`) | End-to-end mapping from live DB |
| v0.3 | UI: user can connect, translate, execute, and view results without touching JSON | Manual acceptance test |
| v0.3 | TCK overall pass rate | ≥ 25% of all scenarios (non-skipped) |
| v0.3 | Neo4j Movies dataset: full query corpus passing against both LPG and PG | 100% |
| v0.4+ | TCK overall pass rate | ≥ 60% of all scenarios |
| v0.4+ | CLI fully functional (`translate`, `run`, `mapping`, `doctor`) | All subcommands work |

### Non-goals (initially)
- **Full** openCypher TCK compliance in v0.1 -- but TCK is a **progressive goal**: each new Cypher feature should be accompanied by a check of which TCK scenarios it unblocks (see §8.2 for the phased strategy).
- Writing queries (CREATE/MERGE/DELETE/SET) in the first milestone, unless you explicitly want it. (Write support becomes a requirement in v0.2+ for TCK setup steps.)
- Full query optimizer equivalent to a database planner (we'll have a small internal logical plan, but not a cost-based optimizer).
- The Cypher Workbench UI (§4.4) is **not** a production multi-user workbench. Multi-user authn/authz, collaboration, server-side persistence, and multi-tenant isolation are explicitly out of scope — the UI targets single-operator debug/demo use (see §4.4 scope banner).

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

> **Scope note.** The Workbench UI is a **debug and demo surface** for the conversion service (§4.3), not the product. It is optimized for a single operator (developer, SE, or demo presenter) inspecting and replaying translations against one dataset at a time. Multi-user support, authentication/authorization, persistent server-side user state, collaboration, and multi-tenant isolation are explicitly **out of scope**. The UI is **not deployed by default** alongside the service (§15) — the default Arango Platform deployment is headless (library + CLI + HTTP endpoints). UI work should be scoped to features that directly improve the ability to debug the service or demonstrate its capabilities; any feature that would only be valuable in a multi-user workbench context belongs in a separate downstream product, not here.

#### 4.4.1 Architecture
SPA served by FastAPI. The browser does **not** connect to ArangoDB directly; all
database interaction flows through the service layer.

```
Browser                          FastAPI service             ArangoDB
+--------------------------+     +--------------------+     +----------+
| Cypher Editor            |     |                    |     |          |
| AQL Editor (editable)    | <-> | arango_cypher      | <-> | Database |
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

**C) Autocompletion (context-aware)** *(core completions implemented -- see `ui/src/lang/cypher-completion.ts`)*
- After `:` inside `(…)` -> entity labels from mapping. **(implemented)**
- After `:` inside `[…]` -> relationship types from mapping. **(implemented)**
- After `.` on a bound variable -> property names from mapping for that label (requires `properties` in entity mapping). **(implemented)**
- After `arango.` -> registered extension functions/procedures from profile.
- After `$` -> parameter names from parameter panel.
- Start of line -> Cypher keywords appropriate to position.
- Inside `RETURN`/`WITH` -> aggregation functions, built-in functions.

The completion source uses a `MappingSchema` extracted from the current mapping JSON. Context detection walks backwards from the cursor to determine whether the position is inside a node pattern `(…)` or relationship pattern `[…]`. The schema ref is updated reactively when the mapping changes, so completions always reflect the current mapping without an editor re-mount.

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

#### 4.4.3 AQL editor --- syntax-directed editing with Explain / Profile
Side-by-side with Cypher editor. CodeMirror 6 instance, **editable** (transpiler output is the starting point; user can modify).

**A) Syntax highlighting** *(implemented)*
- AQL keywords (`FOR`, `IN`, `FILTER`, `RETURN`, `LET`, `SORT`, `LIMIT`,
  `COLLECT`, ...), bind parameters (`@@collection`, `@param`), functions,
  strings, numbers, comments.
- Bind-var references visually distinct (bold + colored).
- Line numbers always shown.

**B) Syntax-directed editing** *(implemented — `ui/src/lang/aql.ts`, `ui/src/components/AqlEditor.tsx`)*
- **Autocompletion** (Ctrl+Space or as-you-type): all AQL keywords, ~90 built-in functions (auto-insert `(`), snippet templates for common patterns.
- **Snippet templates**: `FOR ... IN` (loop), `FOR ... OUTBOUND/INBOUND` (traversal), `FILTER`, `COLLECT ... INTO`, `COLLECT AGGREGATE`, `LET ... =`, `LET ... = (subquery)`, `SORT ... ASC/DESC`, `LIMIT offset, count`, `RETURN { ... }`, `RETURN DISTINCT`, `INSERT`, `UPDATE`, `REMOVE`, `UPSERT`, `OPTIONS { indexHint }`.
- **Scoped variable prediction**: parses the AQL document to extract `FOR`, `LET`, and `COLLECT` bindings (including `COLLECT AGGREGATE` and `INTO` variables). Only variables in scope at the cursor line are suggested. Variables are boosted above keywords in the completion list.
- **Document property prediction**: after typing `var.`, resolves the variable's collection through the mapping (`physical_mapping.entities` / `physical_mapping.relationships`) and bind vars to offer property-level completions (e.g., `d.TENANT_ID`, `d.SERIAL_NUMBER`). System properties (`_key`, `_id`, `_rev`, `_from`, `_to`) are always included.
- **Bracket auto-closing**: `(`, `[`, `{`, `"`, `'` auto-pair.
- **Bracket matching**: highlights matching pairs.
- **Code folding**: fold gutter for collapsible blocks.
- **Undo/Redo**: full history (Ctrl+Z / Ctrl+Shift+Z).
- **Tab indentation**: Tab/Shift+Tab to indent/dedent.
- **Search/Replace**: Ctrl+F to find, Ctrl+H to replace.
- **Selection highlighting**: all occurrences of selected text are highlighted.

**C) Live synchronization** *(implemented)*
- Translate button updates AQL (debounced).
- Bind vars panel below AQL editor.
- Error state: if translation fails, show error inline instead of stale AQL.
- "modified" indicator when user has edited the AQL away from transpiler output.

**D) Local learning (corrections store)** *(implemented — `arango_cypher/corrections.py`, §14.1)*
- User edits the transpiled AQL, runs it successfully, clicks **Learn**.
- Correction stored in local SQLite (`corrections.db`) keyed on `(cypher, mapping_hash)`.
- On subsequent translates/executes of the same Cypher + mapping, the corrected AQL is used automatically (with a warning: "Using learned correction #N").
- **Corrections management panel**: view all stored corrections, delete individual entries.
- REST API: `POST /corrections`, `GET /corrections`, `DELETE /corrections/{id}`, `DELETE /corrections`.

**E) Explain and Profile** *(implemented)*
- **Explain** button -> `POST /explain` -> renders execution plan as interactive
  tree (type, estimatedCost, estimatedNrItems, index details). Raw JSON toggle.
- **Profile** button -> `POST /aql-profile` -> executes with profiling, shows
  runtime stats per plan node (actual time, rows, memory). Color-coded hotspots.
  Results go to Results panel.

**F) Correspondence hints (v0.4+)**
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
5. **Mapping panel** (drawer/tab): toggle between **JSON editor** and **visual graph
   editor** (5.6). Both views are bidirectionally synced. Visual graph shows entity
   nodes with properties, relationship edges, embedded relationships.
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

**Security model (expanded):**

| Concern | Policy | Status |
|---------|--------|--------|
| **CORS** | Allow configurable origins via `ARANGO_CYPHER_CORS_ORIGINS` env var. Default: `["http://localhost:5173"]` (Vite dev server). Production: explicit allowlist only. | Partial -- FastAPI `CORSMiddleware` is added but origins may not be configurable |
| **Session tokens** | Generated via `secrets.token_urlsafe(32)`. Not JWT -- opaque, server-side lookup only. | Implemented |
| **Session TTL** | Default 30 min sliding window; configurable via `ARANGO_CYPHER_SESSION_TTL_SECONDS`. Expired sessions are evicted on next access. | Implemented |
| **Max sessions** | Configurable `ARANGO_CYPHER_MAX_SESSIONS` (default 100). LRU eviction when exceeded. | Not implemented |
| **Rate limiting** | Not implemented. Recommendation: add per-IP rate limiting (e.g. `slowapi`) for `/connect` (prevent brute-force) and `/execute` (prevent resource abuse). | Not implemented |
| **Credential storage** | In-memory only. Never written to disk, logs, or response bodies (except session token). | Implemented |
| **`.env` exposure** | `GET /connect/defaults` returns host, port, database, username only. Password is never returned. If the service is exposed publicly, even these defaults may leak internal infrastructure details. Recommendation: disable `/connect/defaults` when `ARANGO_CYPHER_PUBLIC_MODE=true`. | Partial |
| **AQL injection** | All collection names use `@@` bind parameters; all values use `@` bind parameters. No string interpolation. | Implemented |
| **Error sanitization** | Raw Python tracebacks are never returned to the client. ArangoDB error messages are proxied but sensitive connection details (hostname, credentials) are stripped. | Partial |
| **HTTPS** | The service itself does not terminate TLS. Recommendation: use a reverse proxy (nginx, Caddy) for production. Document this requirement. | Not implemented (documentation gap) |
| **Multi-tenant** | Not addressed. Each session connects to one ArangoDB database. No tenant isolation beyond session scoping. Multi-tenant would require per-tenant credential vaults and RBAC -- out of scope for v0.1. | Not applicable (v0.1) |

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
| GET | `/schema/introspect` | Discover collections, edges, properties from connected DB (session required) |
| GET | `/schema/properties` | Infer properties for a specific collection (session required) |

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
- **Cypher language mode**: Custom Lezer grammar or community package. Context-aware autocompletion (`ui/src/lang/cypher-completion.ts`).
- **AQL language mode**: Custom `StreamLanguage` definition with autocompletion, snippets, scoped variable prediction, and document property prediction (`ui/src/lang/aql.ts`).
- **Graph visualization**: Custom SVG with pan/zoom (results graph + read-only schema graph). Cytoscape.js planned for visual mapping editor (§5.6).
- **Mapping graph layout**: Two-column conceptual/physical layout with SVG bezier curves for mapping edges. Cytoscape-dagre planned for bidirectional graph editor.
- **Execution plan viz**: React tree component (custom or react-d3-tree).
- **HTTP client**: fetch.
- **Styling**: Tailwind CSS.
- **State management**: Zustand.
- **Local persistence**: `localStorage` (NL query history, connection state, parameters). SQLite (corrections store, backend).

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

| Phase | Scope | Status |
|-------|-------|--------|
| v0.3-alpha | FastAPI service with all endpoints. Connection dialog. Cypher editor with syntax highlighting (A), bracket matching, auto-close. AQL editor (read-only) with AQL syntax highlighting. Translate button -> AQL preview. No execute. | **Done** |
| v0.3-beta | Execute with table results. Cypher parse-error markers (B). AQL Explain button + tree view. Query history. Parameter binding (F). Bind-vars panel. `.env` defaults. Keyboard shortcuts. | **Done** -- all items implemented. |
| v0.3 | AQL Profile button + annotated plan view. Results graph view. Variable-use highlighting (D). Clause outline. Profile panel. AQL editor editable mode + syntax-directed editing. **Property-enriched mappings** (5.5). **Domain/range optimization** (5.5.1). **Context-aware autocompletion** (C). | **Partial** -- property mappings, domain/range optimization, autocompletion, profile: **done**. AQL editor editable with full syntax-directed editing (§4.4.3B): **done**. Results graph view: **partial** (custom SVG, no Cytoscape). Variable-use highlighting: **done**. Clause outline: **not started**. |
| v0.3.1 | **Visual mapping graph editor** (5.6): Cytoscape.js entity-relationship diagram, bidirectional sync with JSON editor. `GET /schema/introspect` endpoint. **Schema analyzer integration** (§5.1–5.3). | **Partial** -- `/schema/introspect` with 3-tier strategy (heuristic + analyzer): **done**. Visual mapping graph (read-only, SVG, dual-layer conceptual/physical): **done**. Cytoscape.js integration and bidirectional graph-to-JSON editing: **not started**. |
| v0.4 | Hover documentation (D). Profile-aware warnings (B). Format/prettify. Correspondence hints. Multi-statement. Export. Ontology (OWL Turtle) generation. `arango.*` / `$` / keyword autocompletion. NL-to-Cypher pipeline (§1.2). **Local learning** (§14.1). AQL snippet templates (§4.4.3B). AQL post-processing indentation. NL query history. Token usage display. | **Mostly done** -- NL-to-Cypher (LLM + rule-based fallback, pluggable providers, validation/retry): **done**. Export (CSV/JSON): **done**. `arango.*`/`$`/keyword autocompletion: **done**. OWL export/import endpoints: **done**. AQL snippet templates: **done**. Local learning (corrections store): **done**. AQL indentation (`_reindent_aql`): **done**. NL query history: **done**. Token usage display: **done**. Cypher hover docs, AQL format/prettify, multi-statement, variable-use highlighting: **done**. Profile-aware warnings, correspondence hints: **not started**. |

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

#### No-workaround policy for schema analyzer gaps

The `arangodb-schema-analyzer` is the **canonical source** for reverse-engineering ontologies from ArangoDB schemas. When the transpiler encounters a situation where the analyzer's output is incomplete, incorrect, or missing a needed capability:

1. **Do not work around it** in the transpiler. Workarounds create hidden coupling, obscure the real gap, and lead to divergent behavior when the analyzer is later fixed.
2. **File a bug or feature report** against `arangodb-schema-analyzer` (repo: `~/code/arango-schema-mapper`). Include:
   - The database schema that triggered the gap (collections, sample documents)
   - What the analyzer currently produces
   - What the transpiler needs it to produce
   - A concrete example of the Cypher query that would benefit
3. **Document the gap** in this PRD (§5.3 implementation status table) with a reference to the filed issue.
4. **Skip or error gracefully** in the transpiler until the analyzer is fixed. Use `CoreError` with code `"ANALYZER_GAP"` and a message referencing the issue.

This policy ensures that the analyzer and transpiler evolve together and that ontology extraction quality improves at the source rather than being papered over downstream.

#### 5.2.1 Analyzer promotion (resolved 2026-04-11)

As of `arangodb-schema-analyzer` v0.1.0, the analyzer passes all acceptance criteria for PG, LPG, and hybrid schemas (28/28 tests — see `docs/schema-mapper-lpg-bug-report.md` for the original gaps and resolution).

The `get_mapping(strategy="auto")` flow now routes **all** schema types through the analyzer first:
1. **Analyzer (primary)**: `acquire_mapping_bundle(db)` — handles PG, LPG, hybrid identically. Produces consistent ontology (same entity names, relationship types, domain/range) regardless of physical style. Per-type properties scoped by discriminator. Contract version 1 with JSON Schema validation.
2. **Heuristic (fallback)**: `classify_schema(db)` + `_build_heuristic_mapping(db, schema_type)` — used only when the analyzer is **not installed** (`ImportError`). Provides a reasonable best-effort mapping for PG and LPG.
3. **Explicit config (highest priority)**: user-supplied `MappingBundle` via UI, API, or code always takes precedence when provided.

The heuristic tier is retained as a zero-dependency fallback so the transpiler can function in environments where installing the analyzer is not feasible.

### 5.3 Detection strategy in `arango-cypher-py`
3-tier strategy (updated 2026-04-11 — analyzer promoted to primary for all schema types):
- **Explicit config** (highest priority): user-supplied mapping overrides; useful for unstable databases.
- **Analyzer** (primary): `arangodb-schema-analyzer` handles PG, LPG, and hybrid. Consistent ontology across all three physical styles.
- **Heuristic** (fallback): `classify_schema()` + `_build_heuristic_mapping()` when the analyzer is not installed.

`strategy="analyzer"` forces the analyzer (raises if not installed). `strategy="heuristic"` forces the heuristic.

#### Current implementation status

| Component | Status |
|-----------|--------|
| Explicit config (manual JSON mapping) | **Implemented** -- users supply `MappingBundle` via UI, API, or code |
| `MappingResolver` consuming export JSON | **Implemented** -- entities, relationships, properties, domain/range |
| Fast heuristic detection (`classify_schema`) | **Implemented** -- classifies as `pg`, `lpg`, or `hybrid` using `COLLECT DISTINCT` AQL queries; detects discriminator fields for both document and edge collections |
| Heuristic mapping builder (`_build_heuristic_mapping`) | **Implemented** -- fallback when analyzer not installed. Builds `MappingBundle` for PG and LPG; handles `typeField`/`typeValue`, per-type property sampling, domain/range inference |
| Schema analyzer integration (library call) | **Implemented** -- `acquire_mapping_bundle(db)` calls `arangodb-schema-analyzer` v0.1.0+ via `AgenticSchemaAnalyzer`. **Primary tier for all schema types** (PG, LPG, hybrid). See §5.2.1. |
| `get_mapping(db)` convenience API | **Implemented** -- `get_mapping(db)` runs: analyzer → heuristic fallback (on ImportError). Result cached by schema fingerprint. |
| Schema introspection endpoints | **Implemented** -- `GET /schema/introspect` calls `get_mapping(db)` → returns full conceptual + physical mapping via `MappingResolver.schema_summary()`. UI auto-introspects on connect. |

#### Requirements for schema analyzer integration

1. **Library import path** -- add `arangodb-schema-analyzer` as an optional dependency. Import `AgenticSchemaAnalyzer` or use the tool contract `run_tool({"operation": "export", ...})`.
2. **`acquire_mapping_bundle(db)` function** -- given a `python-arango` database handle, call the analyzer's `export` operation and return a fully populated `MappingBundle` (including `conceptual_schema` with structured `entities`/`relationships` and `fromEntity`/`toEntity`, `physical_mapping` with properties and domain/range, and optionally `owl_turtle`).
3. **UI "Introspect" button** -- the mapping panel's introspect flow should call the analyzer (via a new service endpoint) rather than just sampling document fields. The analyzer produces a complete conceptual-to-physical mapping with entity/relationship type detection, not just property names.
4. **OWL Turtle round-trip** -- `_mapping_from_dict` in `service.py` should read `owl_turtle` (or `owlTurtle`) from the request and populate `MappingBundle.owl_turtle`. The `/schema/summary` endpoint should include it in the response.
5. **Caching** -- cache the analyzer result by schema fingerprint (collections + indexes + sample hash). Avoid re-analyzing on every request.
6. **Fallback chain** -- when a user calls `translate()` without an explicit mapping but with a database connection, automatically run the 3-tier strategy: explicit > heuristic > analyzer.

### 5.4 OWL Turtle usage
You asked specifically for OWL TTL. We'll support two flows:
- **Primary runtime flow**: consume `export` mapping JSON (simpler, stable, already designed for transpilers).
- **Artifact/explain flow**: also store `owl` Turtle output alongside the export, for:
  - debugging
  - explaining hybrid partitions
  - offline review / documentation

Optional: implement a TTL ingestion path using `rdflib` so users can provide a TTL mapping file (air-gapped use, reproducible builds).

#### Current implementation status

| Component | Status |
|-----------|--------|
| `MappingBundle.owl_turtle` field | **Exists** -- field defined on the dataclass |
| OWL Turtle test fixtures | **Exist** -- `tests/fixtures/mappings/*.owl.ttl` files alongside export JSON |
| Loading OWL into MappingBundle | **Partial** -- `_mapping_from_dict()` reads `owlTurtle`; `mapping_bundle_for()` does not read `.owl.ttl` files |
| OWL export endpoint | **Implemented** -- `POST /mapping/export-owl` generates OWL Turtle from mapping |
| OWL import endpoint | **Implemented** -- `POST /mapping/import-owl` loads OWL Turtle into `MappingBundle` |
| OWL generation from enriched mapping | **Partial** -- export-owl endpoint works; no standalone library function |
| `rdflib` TTL ingestion | **Not implemented** |

Recommended libs:
- `rdflib` for parsing TTL

### 5.5 Property-enriched mappings

#### Current state (implemented)
Property-enriched mappings are fully supported. `MappingResolver.resolve_properties(label_or_type)` returns `dict[str, PropertyInfo]` with field name, type, indexed, required, and description metadata. The UI's default sample mapping includes properties on both entities and relationships. The editor's context-aware autocompletion (4.4.2C) consumes properties from the mapping to offer `.name`, `.age`, `.email` completions after bound variables. Domain/range on relationships is resolved via a 3-tier strategy (see 5.5.1) that also reads `fromEntity`/`toEntity` from the conceptual schema.

#### Why properties matter

1. **Autocompletion** -- the editor needs property names per label to offer `.name`, `.age`, `.email` after a bound variable (PRD 4.4.2C)
2. **Validation** -- warn if a Cypher query references a property that does not exist on the target label/collection
3. **Type awareness** -- knowing that `age` is a number and `name` is a string enables type-safe comparisons, aggregation hints, and index recommendations
4. **Visual mapping graph** -- a graph visualization of the schema needs to show entity nodes with their property lists and relationship edges with their property lists
5. **Ontology derivation** -- generating OWL/RDF from the mapping requires datatype properties, not just classes and object properties

#### Mapping shape (implemented)

Entity and relationship entries in `physical_mapping` gain an optional `properties` dict mapping conceptual property names to physical field metadata:

```json
{
  "physical_mapping": {
    "entities": {
      "Person": {
        "style": "COLLECTION",
        "collectionName": "persons",
        "properties": {
          "name":  { "field": "name",  "type": "string" },
          "age":   { "field": "age",   "type": "number" },
          "email": { "field": "email", "type": "string", "indexed": true }
        }
      }
    },
    "relationships": {
      "KNOWS": {
        "style": "DEDICATED_COLLECTION",
        "edgeCollectionName": "knows",
        "domain": "Person",
        "range": "Person",
        "properties": {
          "since": { "field": "since", "type": "number" }
        }
      }
    }
  }
}
```

Property metadata fields:

| Field | Type | Description |
|-------|------|-------------|
| `field` | string | Physical field name in the document (allows rename: conceptual `firstName` -> physical `first_name`) |
| `type` | string | Data type: `string`, `number`, `boolean`, `array`, `object`, `date`, `geo_point`, `null` |
| `indexed` | boolean | Whether the field has a persistent/hash index (for query optimization hints) |
| `required` | boolean | Whether the field is always present (vs sparse/optional) |
| `description` | string | Human-readable description (for hover docs and ontology generation) |

#### Property discovery strategies

1. **Explicit** -- user supplies properties in the mapping JSON (highest fidelity)
2. **Schema analyzer** -- `arangodb-schema-analyzer` export with `includeProperties=true` samples documents and infers property names, types, and cardinality
3. **Live introspection** -- new `GET /schema/properties?collection=persons&sample=100` endpoint that samples N documents from the connected database and returns inferred property names and types
4. **Hybrid** -- combine explicit overrides with analyzer/introspection results; explicit wins on conflict

#### Impact on existing code (implemented)

- `MappingResolver.resolve_properties(label_or_type: str) -> dict[str, PropertyInfo]` -- reads property metadata from entity or relationship mapping
- `translate_v0` optionally validates property references against the schema (warn, not error by default)
- Editor autocompletion (4.4.2C) consumes properties from the mapping via `extractSchema()` in `ui/src/lang/cypher-completion.ts` -- offers property names after `.` on bound variables
- Visual mapping graph (5.6) renders properties as node attribute lists

### 5.5.1 Domain/range relationship metadata and IS_SAME_COLLECTION optimization

#### Problem

When translating `MATCH (p1:Person)-[:KNOWS]->(p2:Person) RETURN p1, p2`, the transpiler emits an `IS_SAME_COLLECTION(@vCollection, p2)` filter after the graph traversal. This filter verifies that the target vertex belongs to the expected collection. However, when the mapping declares that the `KNOWS` edge collection exclusively connects `Person` (domain) to `Person` (range), the edge collection itself already constrains both endpoints -- the filter is pure overhead.

#### Solution

Relationship entries in `physical_mapping` support optional `domain` and `range` fields that name the conceptual entity labels at each end of the edge:

```json
"KNOWS": {
  "style": "DEDICATED_COLLECTION",
  "edgeCollectionName": "knows",
  "domain": "Person",
  "range": "Person"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `domain` | string | Conceptual entity label for the source endpoint (`_from`) |
| `range` | string | Conceptual entity label for the target endpoint (`_to`) |

When the transpiler encounters a traversal pattern like `(a:X)-[:R]->(b:Y)`:
- **OUTBOUND**: if `R.range == Y`, skip `IS_SAME_COLLECTION` for `b`
- **INBOUND**: if `R.domain == Y`, skip `IS_SAME_COLLECTION` for `b`
- **ANY**: skip only if both `R.domain == Y` and `R.range == Y`

If `domain`/`range` is absent or does not match the target label, the filter is emitted as before (backward-compatible).

#### Domain/range inference (implemented)

When explicit `domain`/`range` fields are not present on the physical relationship mapping, `MappingResolver` infers them automatically via a 3-tier resolution in `_resolve_domain_range`:

1. **Explicit physical mapping** -- `domain`/`range` fields on the relationship entry (highest priority, original behavior)
2. **Conceptual schema relationships** -- `fromEntity`/`toEntity` from the `conceptual_schema.relationships` array (when present and not `"Any"`)
3. **Single-entity inference** -- when the mapping defines exactly one entity type, both endpoints must be that type

This means mappings that use the simple `entityTypes`/`relationshipTypes` format (without explicit `domain`/`range`) still benefit from the optimization when the conceptual schema provides enough information:

```json
{
  "conceptual_schema": {
    "relationships": [
      { "type": "KNOWS", "fromEntity": "Person", "toEntity": "Person" }
    ]
  },
  "physical_mapping": {
    "relationships": {
      "KNOWS": {
        "style": "DEDICATED_COLLECTION",
        "edgeCollectionName": "knows"
      }
    }
  }
}
```

The above mapping (without explicit `domain`/`range` on the physical side) will still skip `IS_SAME_COLLECTION` because `fromEntity`/`toEntity` in the conceptual schema resolve the constraint.

#### Implementation

- `MappingResolver.edge_constrains_target(rel_type, target_label, direction)` -- returns `True` when the filter can be safely omitted (only for `DEDICATED_COLLECTION` style with matching domain/range)
- `MappingResolver._resolve_domain_range(rel_type, rmap)` -- 3-tier resolution of domain/range from physical mapping, conceptual schema, or single-entity inference
- All traversal code paths in `translate_v0` consult `edge_constrains_target` before emitting `IS_SAME_COLLECTION`
- Golden tests in `tests/fixtures/cases/domain_range_optimization.yml` cover OUTBOUND, INBOUND, ANY, WHERE clause preservation, cross-type source, and the fallback when domain/range is absent

#### AQL comparison

**Without domain/range** (current default):
```aql
FOR p1 IN @@uCollection
  FOR p2, r IN 1..1 OUTBOUND p1 @@edgeCollection
    FILTER IS_SAME_COLLECTION(@vCollection, p2)
  RETURN {p1: p1, p2: p2}
```

**With domain/range declared**:
```aql
FOR p1 IN @@uCollection
  FOR p2, r IN 1..1 OUTBOUND p1 @@edgeCollection
  RETURN {p1: p1, p2: p2}
```

---

### 5.6 Visual mapping graph editor

The mapping panel (4.4.4 item 5) currently shows raw JSON. A **visual graph editor** provides a far more intuitive way to understand and edit the conceptual-to-physical mapping.

#### Architecture

The visual mapping graph renders the `conceptual_schema` + `physical_mapping` as an interactive **entity-relationship diagram**:

- **Entity nodes** -- rounded rectangles showing the conceptual type name (e.g. `Person`), the physical collection name, the mapping style badge (`COLLECTION` / `LABEL`), and a list of properties with types
- **Relationship edges** -- labeled arrows between entity nodes showing the relationship type (e.g. `KNOWS`), the physical edge collection, the mapping style, and edge properties
- **Embedded relationships** -- dashed arrows with an "EMBEDDED" badge, showing the `embeddedPath` and whether it's an array or object

#### Interaction model

| Interaction | Behavior |
|-------------|----------|
| **View** | Graph auto-layouts from the current mapping JSON. Nodes are draggable. Zoom/pan supported. |
| **Select node** | Side panel shows the full entity mapping detail: collection, style, all properties with types, indexes |
| **Select edge** | Side panel shows relationship detail: edge collection, style, direction, properties |
| **Add entity** | Click "+" button or double-click canvas. Dialog prompts for label name, collection, style. Node appears in graph and JSON updates. |
| **Add relationship** | Drag from one entity node to another. Dialog prompts for type name, edge collection, style. Edge appears and JSON updates. |
| **Add property** | Select a node/edge, click "Add Property" in side panel. Enter name, type, indexed flag. |
| **Edit** | Double-click a node/edge label to rename. Edit properties inline in the side panel. All changes sync to the JSON editor bidirectionally. |
| **Delete** | Select node/edge, press Delete or click remove button. Confirmation prompt. JSON updates. |
| **Import from DB** | "Introspect" button samples the connected database (via `GET /schema/introspect`) and populates the graph with discovered entities, relationships, and properties. User confirms before applying. |

#### Bidirectional sync with JSON editor

The visual graph and the JSON mapping editor are two views of the same data:
- Editing the JSON updates the graph in real time
- Editing the graph (drag, add, delete, rename) updates the JSON in real time
- The user can switch between views freely; the toggle is in the mapping panel header
- Invalid JSON in the text editor shows a parse error but does not break the graph (last valid state is retained)

#### Layout and rendering

- **Library**: Cytoscape.js (already in the tech stack for results graph view)
- **Layout**: `dagre` (hierarchical/layered) for initial auto-layout; user can drag nodes to customize
- **Node rendering**: Custom HTML node with label, collection badge, and property list (Cytoscape.js `html-node` extension or overlay div)
- **Edge rendering**: Labeled bezier curves with arrowheads; embedded relationships use dashed lines
- **Minimap**: Optional corner minimap for large schemas

#### Integration points

| Component | How it uses the visual graph |
|-----------|------------------------------|
| **Mapping panel** | Toggle between "JSON" and "Visual" views; both edit the same `MappingBundle` |
| **Autocompletion** (4.4.2C) | Property lists from the graph feed the editor's context-aware completions |
| **Validation** | Property references in Cypher are checked against the graph's property catalog |
| **Ontology export** | The enriched mapping (with properties) can generate OWL Turtle via the schema analyzer |
| **Introspection** | "Introspect" button populates the graph from a live database connection |

#### New API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/schema/introspect` | Sample connected database: discover collections, edge collections, document properties (requires session) |
| GET | `/schema/properties` | Return inferred properties for a specific collection (sample N documents) |

### 5.7 Index-aware physical mapping model

The physical mapping carries index metadata so the transpiler can make informed optimization decisions without requiring the query author to know about physical indexes.

#### Why indexes belong in the mapping

1. **VCI (Vertex-Centric Indexes)**: in LPG graphs with a generic edge collection, a VCI on the edge `type` field allows the traversal engine to filter edges at the storage layer. When the transpiler knows a VCI exists, it can emit edge-direction filters inside the traversal `OPTIONS` instead of post-filtering vertices.
2. **Persistent indexes**: knowing that `name` is indexed on `persons` allows the transpiler to emit `OPTIONS { indexHint: "idx_persons_name" }` for filtered scans.
3. **Fulltext / Geo / TTL indexes**: these affect which `arango.*` extension functions are available and performant for a given collection.

#### Mapping shape extension

Each entity and relationship in `physicalMapping` gains an optional `indexes` array:

```json
{
  "physicalMapping": {
    "entities": {
      "Person": {
        "collectionName": "nodes",
        "style": "LABEL",
        "typeField": "type",
        "typeValue": "Person",
        "indexes": [
          {
            "type": "persistent",
            "fields": ["name"],
            "unique": false,
            "name": "idx_nodes_name"
          }
        ]
      }
    },
    "relationships": {
      "ACTED_IN": {
        "edgeCollectionName": "edges",
        "style": "GENERIC_WITH_TYPE",
        "typeField": "relation",
        "typeValue": "ACTED_IN",
        "indexes": [
          {
            "type": "persistent",
            "fields": ["relation"],
            "unique": false,
            "name": "idx_edges_relation",
            "vci": true
          }
        ]
      }
    }
  }
}
```

#### Index metadata fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Index type: `persistent`, `hash`, `skiplist`, `fulltext`, `geo`, `ttl`, `inverted` |
| `fields` | string[] | Fields covered by the index |
| `unique` | boolean | Whether the index enforces uniqueness |
| `sparse` | boolean | Whether null values are excluded |
| `name` | string | ArangoDB index name (for `indexHint`) |
| `vci` | boolean | Whether this is a vertex-centric index (only meaningful on edge collections) |
| `deduplicate` | boolean | Whether the index deduplicates array values |

#### VCI detection and advisory

When the transpiler encounters an LPG graph that uses a generic edge collection (`GENERIC_WITH_TYPE` style) but has **no VCI** on the edge type field:

1. **Log a warning**: `"Edge collection 'edges' uses GENERIC_WITH_TYPE but has no VCI on field 'relation'. Traversal performance will be degraded."`
2. **Advise the data owner**: the CLI `doctor` command and the UI introspection panel should surface this as a recommendation: "Consider creating a vertex-centric index on `edges.relation` for improved traversal performance."
3. **Offer to create**: for interactive sessions (CLI `run`, UI), offer to create the VCI. For ETL pipelines, document the recommended index definition so the data owner can add it to their ingest process.
4. **Degrade gracefully**: the transpiler still works without VCI — it just cannot emit edge-level filters in traversal `OPTIONS`.

#### Naked-LPG handling

A "naked LPG" graph is one that uses generic `nodes`/`edges` collections with a type field but lacks VCI indexes and may lack explicit property indexes. The system must handle this gracefully:

- Test fixtures include both "naked" (no indexes) and "indexed" (with VCI) LPG variants
- The transpiler detects the missing indexes via the mapping and warns
- The `doctor` command reports which indexes would improve performance
- The schema analyzer should emit index information in its export; if it does not, file an `ANALYZER_GAP` report per the no-workaround policy (§5.2)

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

### 6.4 Supported Cypher subset

Concrete reference of which Cypher constructs are supported, partially supported, or planned. Updated 2026-04-15.

#### Clauses

| Construct | Status | Limitations | Target |
|-----------|--------|-------------|--------|
| `MATCH` (single node) | Done | | v0.1 |
| `MATCH` (single hop) | Done | | v0.1 |
| `MATCH` (multi-hop / `*1..N`) | Done | Max depth cap `_MAX_VLP_DEPTH = 10` | v0.1 |
| `OPTIONAL MATCH` | Partial | Single-segment relationship pattern only; node-only or multi-segment not supported | v0.3 |
| `WHERE` | Done | See predicate table below | v0.1 |
| `RETURN` | Done | | v0.1 |
| `RETURN DISTINCT` | Partial | Single projection item only | v0.2 |
| `ORDER BY` | Done | | v0.1 |
| `LIMIT` | Partial | Integer literals only (no expressions/parameters) | v0.2 |
| `SKIP` | Partial | Integer literals only; without LIMIT uses large bound | v0.2 |
| `WITH` (pipeline) | Done | Single or multiple leading MATCHes + WITH stages | v0.2 |
| `WITH` + aggregation | Partial | Aggregation in WITH and RETURN (including `COLLECT()`); COLLECT cannot mix with other aggregates | v0.2 |
| `UNWIND` | Done | Standalone and in-query | v0.1 |
| `CASE` (simple + generic) | Done | | v0.1 |
| `UNION` / `UNION ALL` | Done | Via AQL subqueries | v0.1 |
| `CREATE` | Done | | v0.2 |
| `MERGE` | Done | Node MERGE + relationship MERGE with ON CREATE/ON MATCH SET; DEDICATED_COLLECTION and GENERIC_WITH_TYPE styles | v0.4+ |
| `SET` | Done | | v0.2 |
| `DELETE` / `DETACH DELETE` | Done | | v0.4+ |
| `FOREACH` | Not started | | v0.4+ |
| `CALL` procedure | Partial | Only `arango.*` registered procedures | v0.3 |

#### Predicates and expressions

| Construct | Status | Notes |
|-----------|--------|-------|
| Boolean `AND` / `OR` / `NOT` / `XOR` | Done | |
| Comparisons `=`, `<>`, `<`, `>`, `<=`, `>=` | Done | Chained comparisons rejected |
| `IN` list | Done | |
| `IS NULL` / `IS NOT NULL` | Done | |
| `STARTS WITH` | Done | |
| `ENDS WITH` | Done | Emulated with `RIGHT`/`LENGTH` |
| `CONTAINS` | Done | |
| `EXISTS` / pattern predicates | Done | Pattern predicates supported; `EXISTS { }` subquery implemented via ANTLR grammar extension | v0.3 |
| Regex `=~` | Done | v0.3 |
| `CASE` expressions | Done | |
| Parameters (`$param`) | Done | Positional params rejected |

#### Aggregation functions

| Function | Status | Notes |
|----------|--------|-------|
| `COUNT(*)` / `COUNT(expr)` / `COUNT(DISTINCT expr)` | Done | In WITH and RETURN |
| `SUM` / `AVG` / `MIN` / `MAX` | Done | In WITH and RETURN |
| `COLLECT` | Done | Cannot mix with other aggregates |
| Aggregation in RETURN | Done | Including collect(); v0.2 |

#### Patterns and paths

| Construct | Status | Notes |
|-----------|--------|-------|
| Inline property filters `{name: "Alice"}` | Partial | Parameterized map values `{key: $param}` not supported |
| Multi-label matching `:Person:Actor` | Partial | Requires LABEL-style mapping |
| Named paths `p = (a)-[:R]->(b)` | Done | v0.3 |
| Path functions `length()`, `nodes()`, `relationships()` | Done | v0.3 |
| List comprehensions | Not started | v0.4+ |
| Pattern comprehensions | Not started | v0.4+ |
| COUNT subquery | Not started | v0.4+ |

#### Built-in functions

| Function | Status | AQL equivalent |
|----------|--------|----------------|
| `size(expr)` | Done | `LENGTH(expr)` |
| `toLower(expr)` | Done | `LOWER(expr)` |
| `toUpper(expr)` | Done | `UPPER(expr)` |
| `coalesce(expr, ...)` | Done | `NOT_NULL(expr, ...)` |
| `type(r)` | Done | v0.2 |
| `id(n)` | Done | v0.2 |
| `labels(n)` | Done | v0.2 |
| `keys(n)` | Done | v0.2 |
| `properties(n)` | Done | v0.2 |
| `toString(expr)` | Done | v0.2 |
| `toInteger(expr)` / `toFloat(expr)` | Done | v0.2 |
| `head(list)` / `tail(list)` / `last(list)` | Done | v0.3 |
| `range(start, end[, step])` | Done | v0.3 |
| `reverse(list)` | Done | v0.3 |

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

### 7.5 Error taxonomy and degradation strategy

The transpiler produces four categories of errors. Each has a defined behavior, HTTP status, and UI presentation.

| Category | Trigger | Behavior | HTTP status | UI presentation |
|----------|---------|----------|-------------|-----------------|
| **Parse error** | Invalid Cypher syntax | Fail immediately; report token position and expected tokens | 400 | Red squiggly at error token; tooltip with parse error message |
| **Unsupported construct** | Valid Cypher but not in the supported subset (§6.4) | Raise `CoreError` with the construct name and a "not supported in v0" message | 400 | Amber squiggly underline; tooltip explains which construct and which version will add it |
| **Mapping resolution failure** | Query references a label, relationship type, or property not present in the mapping | Configurable: `warn` (default) emits a warning but translates with best-effort collection name; `error` fails translation | 400 (error mode) or 200 with warnings | Warning icon in AQL panel; warnings array in JSON response |
| **Execution error** | AQL is valid but ArangoDB rejects it or returns an error (permission denied, collection not found, timeout) | Return the ArangoDB error code and message | 502 | Error banner in results panel with ArangoDB error details |
| **Connection error** | Cannot reach ArangoDB or session expired | Return connection failure details | 503 (unreachable) or 401 (session expired) | Connection dialog re-opens; toast notification with error |

**Degradation strategy:**
- Translation is **all-or-nothing** per query: the transpiler does not emit partial AQL with placeholder gaps. If any clause fails, the entire translation fails with a clear error.
- Warnings are **additive**: a successful translation may include warnings (unknown label, missing index, deprecated construct) without blocking the AQL output.
- The service never exposes raw Python tracebacks to the client; all errors are wrapped in a structured `{ "error": true, "code": "...", "message": "...", "position": {...} }` response.

### 7.6 Multi-hop patterns and path semantics

#### Variable-length traversals

Cypher patterns like `(a)-[:KNOWS*1..3]->(b)` translate to AQL graph traversals with explicit depth bounds:

```aql
FOR b, r IN 1..3 OUTBOUND a @@edgeCollection
  RETURN b
```

The transpiler caps unbounded patterns (`*` or `*..`) to `_MAX_VLP_DEPTH = 10` and emits a warning.

#### Multi-segment paths

Patterns with multiple hops like `(a)-[:KNOWS]->(b)-[:LIVES_IN]->(c)` are compiled as **nested FOR loops**, each traversal binding the next start variable:

```aql
FOR b, r1 IN 1..1 OUTBOUND a @@edgeCollection1
  FOR c, r2 IN 1..1 OUTBOUND b @@edgeCollection2
    RETURN {a, b, c}
```

#### Path uniqueness

Cypher mandates **relationship uniqueness** within a `MATCH` pattern: no relationship can appear twice in the same result row. The current implementation does **not** enforce this for multi-segment patterns beyond what ArangoDB's traversal engine provides natively. This is a known conformance gap -- the TCK will catch scenarios where this matters.

**Planned approach (v0.3):** emit `FILTER r1 != r2` predicates for multi-segment patterns, or use AQL's `PRUNE` for variable-length traversals.

#### Named paths and path functions

| Feature | Status | Planned AQL lowering |
|---------|--------|---------------------|
| `p = (a)-[:R]->(b)` | Not started | `LET p = { vertices: [a, b], edges: [r] }` |
| `length(p)` | Not started | `LENGTH(p.edges)` |
| `nodes(p)` | Not started | `p.vertices` |
| `relationships(p)` | Not started | `p.edges` |
| `shortestPath` | Partial | `CALL arango.shortest_path(...)` procedure exists; native Cypher `shortestPath()` syntax not yet parsed |
| `allShortestPaths` | Not started | `K_SHORTEST_PATHS` in AQL |

#### OPTIONAL MATCH limitations

Current OPTIONAL MATCH requires:
- A prior bound `MATCH` clause (cannot lead with OPTIONAL MATCH alone unless wrapped)
- Exactly one relationship pattern segment (no node-only or multi-segment optional)
- No variable-length patterns in the optional segment

These compile to AQL subqueries with fallback null rows. Multi-segment OPTIONAL MATCH is targeted for v0.3.

### 7.7 Performance considerations

#### Translation performance

- **ANTLR parse cost**: the ANTLR4 Python runtime is not cached per query. For high-throughput scenarios, consider caching parsed ASTs by query hash.
- **Current P95**: not benchmarked yet. Target is < 50 ms for single-hop queries (see §2.1).
- **Mapping resolution**: `MappingResolver` construction is lightweight (dict lookups); no concern at current scale.

#### AQL quality

The transpiler prioritizes **correctness over optimization** in v0.1. Known AQL quality gaps:

| Gap | Impact | Planned fix |
|-----|--------|-------------|
| No filter pushdown into traversals | Filters on traversal target properties are applied after the traversal `FOR` loop, not as `PRUNE` or early filter | v0.2: push `FILTER` into the traversal body where safe |
| No index hint emission | The transpiler does not emit `OPTIONS { indexHint: ... }` even when the mapping declares indexed properties | v0.3: optional index hints from `PropertyInfo.indexed` |
| `IS_SAME_COLLECTION` overhead | Emitted when domain/range is unknown; optimized away when declared (§5.5.1) | Done for explicit domain/range |
| `COLLECT` / aggregation in `RETURN` | Direct `RETURN` aggregation (including `COLLECT()`) is implemented; `COLLECT` still cannot mix with other aggregates in one projection | Ongoing edge-case hardening |

#### Service resource management

- **Concurrent sessions**: in-process dict keyed by opaque token. No upper bound currently enforced. **Recommendation**: add a configurable `MAX_SESSIONS` limit (default 100) with LRU eviction.
- **Result set limits**: `POST /execute` returns all rows. **Recommendation**: add a configurable `max_rows` parameter (default 10,000) with a `truncated: true` flag.
- **Translation caching**: not implemented. For repeated identical queries with the same mapping, a `functools.lru_cache` on the `translate()` function (keyed by cypher + mapping hash) would avoid redundant parsing.

### 7.8 Index-informed transpilation strategy

The transpiler uses index metadata from the physical mapping (§5.7) to make optimization decisions. This is a key consequence of the architectural principle (§1.1): physical details like indexes live in the mapping, not in queries.

#### VCI-aware traversal optimization

When a relationship uses `GENERIC_WITH_TYPE` style and the mapping declares a VCI on the edge type field:

```
// Without VCI — transpiler must post-filter vertices after traversal
FOR v, e IN 1..1 OUTBOUND startNode edges
  FILTER e.relation == "ACTED_IN"

// With VCI — transpiler can use edge filter in traversal OPTIONS
FOR v, e IN 1..1 OUTBOUND startNode edges
  OPTIONS { edgeCollections: ["edges"] }
  FILTER e.relation == "ACTED_IN"
  // The VCI allows the storage engine to skip non-matching edges
```

The transpiler decision tree:

| Physical layout | VCI present? | Strategy |
|-----------------|-------------|----------|
| `DEDICATED_COLLECTION` | N/A | Traverse named edge collection directly — no type filter needed |
| `GENERIC_WITH_TYPE` + VCI | Yes | Emit `FILTER e.typeField == typeValue` — VCI makes this efficient at storage level |
| `GENERIC_WITH_TYPE` no VCI | No | Same filter, but log a performance warning; the filter is applied post-read |

#### Persistent index hints

When the mapping declares a persistent index on a property used in a `WHERE` filter:

```aql
FOR v IN @@collection
  OPTIONS { indexHint: "idx_persons_name", forceIndexHint: false }
  FILTER v.name == @p0
```

The transpiler emits `indexHint` only when:
1. The filter is a direct equality or range comparison on an indexed field
2. The index covers the filter fields
3. `forceIndexHint` is `false` (advisory, not mandatory — lets the query optimizer override if it has better information)

#### Implementation phasing

| Capability | Version | Notes |
|------------|---------|-------|
| Index metadata in mapping model | v0.3 (WP-18) | `IndexInfo` dataclass, `MappingResolver.resolve_indexes()` |
| VCI detection + warning | v0.3 (WP-18) | CLI `doctor`, UI introspection panel |
| VCI-aware traversal filter | v0.3 (WP-18) | Emit edge filter strategy based on VCI presence |
| Persistent index hints | v0.4+ | `OPTIONS { indexHint: ... }` emission |
| Index suggestion (agentic) | v0.4+ | `suggest-indexes` tool contract |

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

#### Registered extensions (implemented)

All extensions below are implemented in `arango_cypher/extensions/` and registered via `register_all_extensions()`.

**Functions** (used in expressions: `WHERE`, `RETURN`, `ORDER BY`, etc.)

| Module | Cypher syntax | AQL output | Category |
|--------|--------------|------------|----------|
| `search.py` | `arango.bm25(doc[, k, b])` | `BM25(...)` | Full-text |
| `search.py` | `arango.tfidf(doc[, normalize])` | `TFIDF(...)` | Full-text |
| `search.py` | `arango.analyzer(expr, analyzerName)` | `ANALYZER(...)` | Full-text |
| `vector.py` | `arango.cosine_similarity(v1, v2)` | `COSINE_SIMILARITY(...)` | Vector |
| `vector.py` | `arango.l2_distance(v1, v2)` | `L2_DISTANCE(...)` | Vector |
| `vector.py` | `arango.approx_near_cosine(v1, v2[, opts])` | `APPROX_NEAR_COSINE(...)` | Vector |
| `vector.py` | `arango.approx_near_l2(v1, v2[, opts])` | `APPROX_NEAR_L2(...)` | Vector |
| `geo.py` | `arango.distance(lat1, lon1, lat2, lon2)` | `DISTANCE(...)` | Geo |
| `geo.py` | `arango.geo_distance(a, b[, ellipsoid])` | `GEO_DISTANCE(...)` | Geo |
| `geo.py` | `arango.geo_contains(a, b)` | `GEO_CONTAINS(...)` | Geo |
| `geo.py` | `arango.geo_intersects(a, b)` | `GEO_INTERSECTS(...)` | Geo |
| `geo.py` | `arango.geo_in_range(a, b, low, high[, incLow, incHigh])` | `GEO_IN_RANGE(...)` | Geo |
| `geo.py` | `arango.geo_point(lon, lat)` | `GEO_POINT(...)` | Geo |
| `document.py` | `arango.attributes(doc[, ...])` | `ATTRIBUTES(...)` | Document |
| `document.py` | `arango.has(doc, attr)` | `HAS(...)` | Document |
| `document.py` | `arango.merge(doc1, doc2, ...)` | `MERGE(...)` | Document |
| `document.py` | `arango.unset(doc, attr1, ...)` | `UNSET(...)` | Document |
| `document.py` | `arango.keep(doc, attr1, ...)` | `KEEP(...)` | Document |
| `document.py` | `arango.zip(keys, values)` | `ZIP(...)` | Document |
| `document.py` | `arango.value(doc, path)` | `VALUE(...)` | Document |
| `document.py` | `arango.values(doc[, ...])` | `VALUES(...)` | Document |
| `document.py` | `arango.flatten(array[, depth])` | `FLATTEN(...)` | Document |
| `document.py` | `arango.parse_identifier(id)` | `PARSE_IDENTIFIER(...)` | Document |
| `document.py` | `arango.document(id)` or `(coll, key)` | `DOCUMENT(...)` | Document |

**Procedures** (used via `CALL arango.*(...) YIELD ...`)

| Module | Cypher syntax | AQL output | Category |
|--------|--------------|------------|----------|
| `procedures.py` | `CALL arango.fulltext(coll, attr, query)` | `FULLTEXT(coll, attr, query)` | Full-text |
| `procedures.py` | `CALL arango.near(coll, lat, lon[, limit])` | `NEAR(...)` | Geo |
| `procedures.py` | `CALL arango.within(coll, lat, lon, radius)` | `WITHIN(...)` | Geo |
| `procedures.py` | `CALL arango.shortest_path(start, target, edgeColl, dir)` | `FOR v IN dir SHORTEST_PATH start TO target edgeColl RETURN v` | Graph |
| `procedures.py` | `CALL arango.k_shortest_paths(start, target, edgeColl, dir)` | `FOR p IN dir K_SHORTEST_PATHS start TO target edgeColl RETURN p` | Graph |

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

### 8.2 openCypher TCK (Technology Compatibility Kit)

The openCypher project publishes a **Technology Compatibility Kit** as Gherkin `.feature` files at [github.com/opencypher/openCypher/tree/master/tck/features](https://github.com/opencypher/openCypher/tree/master/tck/features). Each `.feature` contains scenarios that specify graph setup, a Cypher query, and expected results -- providing exhaustive coverage of Cypher language semantics.

#### Current implementation

| Component | Path | Status |
|-----------|------|--------|
| Download script | `scripts/download_tck.py` | Implemented -- fetches `.feature` files from GitHub API; supports `--only-match` filter |
| Gherkin parser | `tests/tck/gherkin.py` | Implemented -- extracts Feature/Scenario/Step with doc strings and data tables |
| TCK runner | `tests/tck/runner.py` | Implemented -- translates Cypher, executes AQL, compares results; produces passed/skipped/failed |
| Sample feature | `tests/tck/features/sample.feature` | One trivial scenario (empty graph returns empty) |
| Real TCK features | not downloaded | `.feature` files are fetched on demand, not checked in |

#### Running TCK tests

```bash
python scripts/download_tck.py                       # download all features
python scripts/download_tck.py --only-match Match1    # download subset
RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck             # run harness
```

#### Current coverage gap

Most scenarios are **skipped** because:
- `Given having executed:` setup still fails for some write patterns (relationship `MERGE`, exotic `CREATE`/`SET` shapes, etc.) even though `CREATE`, `SET`, `DELETE`/`DETACH DELETE`, and node `MERGE` are now supported
- Some Cypher constructs (list comprehensions, `FOREACH`, multiple relationship types in one hop) are not yet supported
- `Scenario Outline` / `Examples` parameterized scenarios are not expanded by the parser

#### What must be implemented to make the TCK useful

1. **Write clause support** (at minimum `CREATE` for graph setup) -- single biggest blocker
2. **Result normalization** -- TCK uses Neo4j conventions for node/relationship literals; the runner needs a normalizer that compares structurally
3. **Error expectation scenarios** -- some scenarios assert a query *should* produce an error
4. **Scenario Outline / Examples expansion** -- the Gherkin parser should expand parameterized scenarios

#### Phased TCK strategy

| Phase | Scope | TCK impact |
|-------|-------|------------|
| v0.1 | Core `MATCH`/`WHERE`/`RETURN`/`WITH`/aggregation | Scenarios starting from empty graph with no setup (rare) |
| v0.2 | Add `CREATE`/`SET` (at least for setup) | Unlocks the vast majority of "Given having executed" steps |
| v0.3 | `OPTIONAL MATCH`, `UNWIND`, `CASE`, path patterns | Unlocks `OptionalMatch`, `Unwind`, `Expressions` features |
| v0.4+ | Remaining gaps (list comprehensions, `FOREACH`, etc.) | Progressively approaches full TCK pass rate |

### 8.3 Neo4j sample datasets with bundled Cypher queries

Neo4j publishes well-known sample datasets, each with seed data, a data model, and example Cypher queries. These test **practical correctness** -- do real-world queries against realistic data return right answers?

#### Available datasets

| Dataset | Source | Graph pattern | Typical queries |
|---------|--------|---------------|-----------------|
| **Movies** | [neo4j-graph-examples/movies](https://github.com/neo4j-graph-examples/movies) | `Person`-`ACTED_IN`/`DIRECTED`->`Movie` | Actor lookup, co-actors, shortest paths, recommendations |
| **Northwind** | [neo4j-graph-examples/northwind](https://github.com/neo4j-graph-examples/northwind) | `Customer`-`PURCHASED`->`Order`-`CONTAINS`->`Product` | Supply chain queries, aggregations |
| **ICIJ (Paradise Papers)** | [neo4j-graph-examples/icij-paradise-papers](https://github.com/neo4j-graph-examples/icij-paradise-papers) | `Entity`/`Officer`/`Intermediary`/`Address` | Investigation traversals, shortest paths |

#### Current implementation

| Component | Path | Status |
|-----------|------|--------|
| Movies LPG fixture | `tests/fixtures/datasets/movies/lpg-data.json` | Implemented -- full dataset (~170 nodes, ~250 edges) |
| Movies PG fixture | `tests/fixtures/datasets/movies/pg-data.json` | Implemented -- separate `persons`/`movies` + per-rel edge collections |
| Movies seeders | `tests/integration/datasets.py` | `seed_movies_lpg_dataset(db)`, `seed_movies_pg_dataset(db)` |
| Movies smoke tests | `tests/integration/test_neo4j_movies_dataset_smoke.py` | Multi-label, edge property filter, unlabeled start node |
| Movies query corpus | `tests/fixtures/datasets/movies/query-corpus.yml` | 20 queries, runs against both LPG and PG |
| Northwind PG fixture | `tests/fixtures/datasets/northwind/pg-data.json` | Implemented -- 6 vertex + 6 edge collections |
| Northwind seeder | `tests/integration/datasets.py` | `seed_northwind_dataset(db)` |
| Northwind query corpus | `tests/fixtures/datasets/northwind/query-corpus.yml` | 14 queries (projections, filters, multi-hop, OPTIONAL MATCH, COLLECT) |
| Neo4j reference driver | `tests/integration/neo4j_reference.py` | Connection (`bolt://127.0.0.1:27687`), `seed_neo4j_movies`, generic `seed_neo4j_pg`, `seed_neo4j_northwind`, `ensure_dataset` cross-module guard, `run_cypher` with scalar coercion |
| Neo4j container | `docker-compose.neo4j.yml` | Neo4j Community, `openSesame` credentials, shared across suites |
| Cross-validation -- Movies | `tests/integration/test_movies_crossvalidate.py` | 20/20 pass (all divergence flags removed) |
| Cross-validation -- Northwind | `tests/integration/test_northwind_crossvalidate.py` | 14/14 pass |
| Social dataset | `tests/integration/seed.py` | `seed_social_dataset(db, mode)` for PG/LPG/hybrid |

#### §8.3.1 Cross-validation harness

Cross-validation is the strongest correctness gate available short of a full openCypher TCK pass: for each query in a corpus, we run the **raw Cypher** against the canonical Neo4j engine and the **translated AQL** against ArangoDB, then assert that both engines produced semantically identical results. A disagreement is, by construction, a translator bug (or a corpus bug where the query under-specifies ordering).

Key design points:

- **Row equivalence** (`assert_result_equivalent` in `test_movies_crossvalidate.py`, reused by Northwind):
  - Column count must match; column names are allowed to diverge because Neo4j preserves raw dotted expressions (`p.name`) while AQL must rename them.
  - Row count must match exactly.
  - When the query contains `ORDER BY`, rows are compared **position-wise**.
  - Otherwise, both result sets are sorted by a deterministic key derived from the normalized row values and compared as multisets.
  - Scalars are normalized via `_normalize_scalar`: `float`/`int` round-trip (AQL `SUM`/`AVG` returns floats), missing keys and `None` compare equal, nested lists/dicts recurse.
- **Dataset isolation on shared Neo4j Community** (`ensure_dataset`): Neo4j Community has a single writable database, so each seeder wipes it first. The shared module-level `_active_dataset` tracks which corpus is currently loaded; each test module's driver fixture calls `ensure_dataset(driver, name, seed_fn)` and reseeds only on change, so mixing suites in a single pytest session (either order) works.
- **Divergence escape hatch**: corpus entries may carry a `divergence:` free-form string. The test still translates (so a translator crash is never masked) and still asserts `expected_min_count` on the Neo4j side, but skips the row-equivalence check and records the divergence as the skip reason. This gate is the workflow for landing a harness before the translator is bug-free. As of 2026-04-17 **no divergence flags remain on either Movies or Northwind**.
- **Tie-break discipline on the corpus**: any query without a total `ORDER BY` is compared as a multiset; queries with a non-total `ORDER BY` must still be stabilized in the corpus (e.g., the 2026-04-17 update on `nw_order_count_by_customer` added `, c.companyName` to a `ORDER BY orderCount DESC`). Both Cypher and AQL are entitled to break ties freely.

Activation:

```bash
docker compose -f docker-compose.neo4j.yml -p arango_cypher_neo4j up -d
RUN_INTEGRATION=1 RUN_CROSS=1 pytest tests/integration/test_movies_crossvalidate.py \
                                     tests/integration/test_northwind_crossvalidate.py
```

Adding another dataset now takes three steps:

1. Seed fixture + query corpus under `tests/fixtures/datasets/<name>/`.
2. Add a `seed_neo4j_<name>(driver)` in `neo4j_reference.py` (usually a one-line wrapper around `seed_neo4j_pg` with label/reltype overrides derived from the mapping fixture).
3. Copy `test_northwind_crossvalidate.py` as a template; swap in the new seeder and mapping name.

#### Next steps (requirements)

1. **Add ICIJ Paradise Papers cross-validation** -- mapping fixture and corpus already exist (5 golden queries); wire up a `seed_neo4j_icij` and a cross-validation suite following the Northwind template.
2. **Add PG-layout Movies cross-validation** -- the LPG corpus already passes; add a separate suite that points the same corpus at the PG fixture + `movies_pg` mapping to verify layout-independent correctness.
3. **Automate dataset download** -- a script similar to `scripts/download_tck.py`:
   ```bash
   python scripts/download_neo4j_dataset.py --dataset movies --format lpg
   python scripts/download_neo4j_dataset.py --dataset northwind --format pg
   ```
4. **CI wiring** -- add the `cross` marker tier to nightly CI (see §8.3's CI integration table below); the harness needs a Neo4j container in the runner.

#### How TCK and dataset tests complement each other

| Dimension | openCypher TCK | Neo4j sample datasets |
|-----------|---------------|----------------------|
| **Purpose** | Language conformance | Practical correctness |
| **Scope** | Exhaustive Cypher syntax/semantics | Domain-specific patterns |
| **Data setup** | Each scenario creates its own tiny graph | Shared realistic dataset seeded once |
| **What it catches** | Edge cases (NULL handling, type coercion, uniqueness) | Integration bugs (wrong collection, missing filter, wrong join direction) |
| **Schema coverage** | Mostly single-label, simple graphs | Multi-label, multi-relationship-type, realistic data |
| **Mapping coverage** | Tested against one mapping (LPG) | Should be tested against LPG, PG, and hybrid |

#### CI integration

| Tier | Command | When to run |
|------|---------|-------------|
| Fast (unit + golden) | `pytest -m "not integration and not tck"` | Every commit / PR |
| Integration (datasets) | `RUN_INTEGRATION=1 pytest -m integration` | Every PR, nightly |
| Cross-validation (Neo4j equivalence) | `RUN_INTEGRATION=1 RUN_CROSS=1 pytest -m cross` | Nightly or on-demand; requires `docker compose -f docker-compose.neo4j.yml up -d` |
| TCK | `RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck` | Nightly or on-demand |

### 8.4 Converting existing Foxx (`arango-cypher-foxx`) tests
We'll treat the legacy JS/Foxx test suite as **spec** and migrate in stages:

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

## 10) Phased delivery plan (unified roadmap)

This section consolidates all phasing schemes (original Phase 0-6, UI §4.4.9, TCK §8.2) into a single version-aligned roadmap. Each version lists Cypher features, infrastructure, UI, and testing milestones together.

> **Detailed implementation plan**: For work package breakdowns, dependency graphs, file-level deliverables, and execution order, see **[`implementation_plan.md`](./implementation_plan.md)**.

### v0.1 — Core read-only transpiler ✅ COMPLETE

| Area | Deliverables | Status |
|------|-------------|--------|
| **Infrastructure** | Project layout, `pyproject.toml`, CI skeleton, ANTLR4 parser from openCypher grammar, DB connection config | Done |
| **Cypher** | MATCH (node, single-hop, multi-hop, variable-length), WHERE (boolean, comparisons, IN, IS NULL, STARTS WITH, ENDS WITH, CONTAINS), RETURN (columns, aliases), ORDER BY, LIMIT, SKIP, UNWIND, CASE, UNION | Done |
| **Mapping** | `MappingBundle` + `MappingResolver` consuming export JSON, property resolution, domain/range inference, IS_SAME_COLLECTION optimization | Done |
| **Extensions** | `arango.*` registry: search, vector, geo, document functions + procedures | Done |
| **Service** | FastAPI with 16+ endpoints including `/nl2cypher`, `/corrections`, `/schema/introspect` | Done |
| **UI** | Cypher editor (syntax highlighting, autocompletion). AQL editor (editable, syntax-directed editing — §4.4.3). Results table/JSON/graph. JSON mapping panel + schema graph. Connection dialog with auto-introspect + DB selector. NL2Cypher bar + history + token display. Local learning (corrections store). Export (CSV/JSON). | Done |
| **Testing** | Golden tests (YAML fixtures), integration tests (Movies + social datasets), TCK infrastructure (download, parse, run) | Done |
| **Success criteria** | 100% golden tests passing, 100% integration tests passing | Met |

### v0.2 — Write clauses + aggregation completeness + schema analyzer

| Area | Deliverables | Status |
|------|-------------|--------|
| **Cypher** | CREATE (at minimum, for TCK setup), SET, RETURN DISTINCT (multi-column), LIMIT/SKIP with expressions, aggregation in RETURN (not just WITH), `type(r)`, `id(n)`, `labels(n)`, `keys(n)`, `properties(n)`, `toString()`, `toInteger()`, `toFloat()` | **Partial** — CREATE/SET/DELETE/DETACH DELETE/node MERGE, aggregation in RETURN (incl. `collect()`), listed builtins, regex `=~`, named paths + path functions: **done**. RETURN DISTINCT (multi-column), LIMIT/SKIP with expressions: **not started** |
| **Mapping** | `acquire_mapping_bundle(db)` -- wire up `arangodb-schema-analyzer` as optional dependency. Fast heuristic classifier. Caching by schema fingerprint. `get_mapping(db)` convenience API. | Done |
| **CLI** | `translate`, `run`, `mapping`, `doctor` subcommands. `console_scripts` entry point in `pyproject.toml`. | Done |
| **UI** | Parameter binding panel. Bind-vars panel. Query history (multi-entry, searchable). Keyboard shortcuts (§4.4.8). | Done |
| **Testing** | TCK: ≥ 40% of Match*.feature passing. Scenario Outline/Examples expansion in Gherkin parser. Result normalization. | Not started |
| **Success criteria** | CREATE compiles and executes. End-to-end mapping from live DB. CLI fully functional. TCK Match ≥ 40%. | |

### v0.3 — Language breadth + UI completeness + datasets

| Area | Deliverables | Status |
|------|-------------|--------|
| **Cypher** | Full OPTIONAL MATCH (multi-segment, node-only). EXISTS / pattern predicates. Regex `=~`. Named paths + path functions. Multi-label matching for COLLECTION-style. Native `shortestPath()` syntax. `head`, `tail`, `last`, `range`, `reverse`. | **Done** — all items complete except native `shortestPath()` (needs ANTLR grammar extension; available via `CALL arango.shortest_path()` extension). Full OPTIONAL MATCH with comma-separated parts: **done**. Multi-label COLLECTION-style: **done** (uses primary label + warning). |
| **Mapping** | OWL Turtle round-trip (load + generate). `rdflib` ingestion path. Index-aware mapping model (`IndexInfo`, VCI detection, `resolve_indexes()`). | **Done** — OWL Turtle round-trip: **done**. `rdflib` ingestion (`parse_owl_with_rdflib`, `[owl]` extra): **done**. `IndexInfo`, VCI detection, `resolve_indexes()`: **done**. |
| **Optimization** | VCI-aware traversal filter emission. VCI/index advisory in `doctor` and UI. Index metadata in schema analyzer export (or `ANALYZER_GAP`). | **Partial** — VCI warning in transpiler; heuristic builder populates indexes from DB; `doctor`/UI advisory polish: **not started** |
| **NL2Cypher** | Two-stage NL→Cypher→AQL pipeline (§1.2). **Stage 1 (LLM):** pluggable LLM provider converts natural language to Cypher. The LLM prompt contains only the conceptual schema (entity labels, relationship types, properties, domain/range) — never collection names, type fields, or AQL. Same pattern as LangChain's `GraphCypherQAChain`. Schema context builder function. Validation loop (parse failure → retry). **Stage 2 (transpiler):** existing deterministic Cypher→AQL transpiler — no changes needed. UI: NL input mode in query editor. | **Partial** — LLM path + rule-based fallback: **done**. Pluggable providers (OpenAI + OpenRouter): **done**. Prompt leak audit + ANTLR validation/retry + enhanced AQL validation: **done**. Domain/range inference for PG edges: **done**. Token usage: **done**. |
| **UI** | Visual mapping graph editor (Cytoscape.js + dagre, bidirectional sync). Results graph view. AQL Profile button + annotated plan. Variable-use highlighting. Clause outline. AQL editor editable with syntax-directed editing (§4.4.3). Local learning (§14.1). Sample queries loader (corpus files). NL input + history. Token usage display. | **Done** — All items complete. Clause outline: **done**. Sample queries loader: **done**. Bidirectional graph editing: **done**. Profile-aware warnings: **done**. Correspondence hints (Cypher↔AQL hover highlighting): **done**. |
| **Testing** | Expand Movies dataset to full (~170 nodes). Add Movies query corpus YAML. PG layout variant. Add Northwind dataset. TCK overall ≥ 25%. Naked-LPG variant (no VCI). | **Done** — Movies full dataset + query corpus (PG + LPG), Northwind corpus, social dataset integration tests: **done**. TCK 66.1% projected (clause-focused), exceeding 25% target. |
| **Success criteria** | User can connect, translate, execute, and view results without touching JSON. Full Movies query corpus passing against LPG + PG. TCK ≥ 25%. VCI advisory working. NL2Cypher generates valid Cypher for basic queries. | |

### v0.4+ — Advanced features + TCK convergence

| Area | Deliverables | Status |
|------|-------------|--------|
| **Cypher** | **Done:** node MERGE + relationship MERGE with ON CREATE/ON MATCH SET; DELETE/DETACH DELETE; FOREACH; list/pattern comprehensions; EXISTS { } subquery; COUNT { } subquery; WITH pipeline from multiple MATCHes. | **Done** |
| **Optimization** | Filter pushdown into traversals. Index hint emission from `PropertyInfo.indexed`. Translation caching. Relationship uniqueness enforcement. | **Done** — Filter pushdown (PRUNE for variable-length): **done**. Translation caching (LRU OrderedDict, 256 entries): **done**. Relationship uniqueness (`r1._id != r2._id`): **done**. Index hint emission: **not started**. |
| **UI** | Hover documentation. Profile-aware warnings. Format/prettify. Correspondence hints (source maps — §4.4.3F). Multi-statement. | **Done** — All items complete. Profile-aware warnings: **done**. Correspondence hints (Cypher↔AQL hover highlighting): **done**. |
| **Agentic** | `translate_tool(request_dict)` JSON-in/JSON-out wrapper. Explain-why-mapped. Suggest-indexes. Propose-mapping-overrides. | **Done** — 8 tools: translate, suggest_indexes, explain_mapping, cypher_profile, propose_mapping_overrides, explain_translation, validate_cypher, schema_summary. |
| **Testing** | TCK overall ≥ 60%. Automate dataset download script. ICIJ Paradise Papers dataset. | **Done** — TCK projected 66.1% (clause-focused). ICIJ Paradise Papers: mapping fixture, seed script, 5 query golden tests: **done**. |
| **Success criteria** | TCK ≥ 60%. All CLI subcommands work. Agentic tool contract functional. | |

---

## 11) Naming & repo strategy (your explicit questions)

### Should this be a new project?
**Yes.** The runtime and dependency model is fundamentally different from Foxx. Keep both until Python is mature.

### How should we name it?
Recommended:
- **Repo**: `arango-cypher-py`
- **Python package**: `arango_cypher` (import-friendly)
- **CLI**: `arango-cypher-py`

### Should we rename the existing `arango-cypher` (Foxx)?
**Resolved (2026-04-17):** yes. The Foxx repo was renamed to `arango-cypher-foxx`, and this Python project stabilized as `arango-cypher-py`. The `-foxx` / `-py` suffixes mirror each other and truthfully describe what each package is (in-database Foxx microservice vs. out-of-process Python distribution). The bare `arango-cypher` name is intentionally kept free on the `arango-solutions` org for a potential future umbrella/spec repo that describes the Cypher→AQL concept and links to implementations. The GitHub rename of `arango-solutions/arango-cypher` → `arango-solutions/arango-cypher-py` is pending org-admin action; `[project.urls]` and git `pushurl` will be updated once the rename lands, and GitHub's automatic redirect keeps the current URL working in the meantime.

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

## 13) Resolved questions (formerly open)

These questions were listed as "to resolve during Phase 1-2". Phase 1-2 work is complete; resolutions recorded here.

| Question | Resolution |
|----------|------------|
| Exact Cypher subset required for v0.1: do you need `WITH` immediately? | **Yes.** `WITH` pipeline is implemented (partial: single leading MATCH + WITH stages). Full subset documented in §6.4. |
| Translation parity with Foxx outputs, or "best AQL" even if it differs? | **Best AQL.** The Python transpiler generates its own AQL style (bind-parameter-heavy, IS_SAME_COLLECTION optimization). Foxx parity is not a goal; correctness is validated via golden tests and integration tests, not output comparison. |
| Canonical field names for LPG type fields (`type`, `_type`, `label`)? | **Configurable via mapping.** The mapping's `typeField` / `typeValue` fields specify the physical field name per entity/relationship. Common conventions: `type`, `labels` (array), `relation`. The transpiler does not assume any default -- the mapping is authoritative. |
| Constraints on running native deps (`libcypher-parser-python`)? | **ANTLR4 chosen.** Pure-Python ANTLR4 runtime avoids native dependency issues (Apple Silicon, musl, Windows). `libcypher-parser-python` remains an optional future migration if performance warrants it (see §6.2). |

## 14) Remaining open design questions

| Question | Owner | Target resolution |
|----------|-------|-------------------|
| Should `CREATE` in v0.2 be full write-clause support or only enough for TCK setup seeding? | Product | v0.2 planning |
| How should path uniqueness (relationship isomorphism) be enforced for multi-segment patterns? `FILTER r1 != r2` vs `PRUNE` vs post-filter? | Engineering | v0.3 |
| Should the visual mapping editor support drag-to-create relationships, or only form-based creation? | Design | v0.3.1 |
| What is the caching invalidation strategy for `acquire_mapping_bundle()` when the database schema changes? | Engineering | v0.2 |
| Should the CLI support piping (`echo "MATCH..." \| arango-cypher-py translate`)? | Product | v0.2 |
| Which LLM provider(s) should NL2Cypher support? §1.2 requires pluggable providers. Which are prioritized for v0.3? (OpenAI, Anthropic, local models via Ollama) | Product | v0.3 planning |
| Should VCI creation be automatic (ELT) or require explicit user approval? | Engineering | v0.3 |
| Should the schema analyzer export include index metadata natively, or should the transpiler query indexes separately via `python-arango`? | Engineering | v0.3 |
| ~~How should the UI present NL input — as a separate editor mode, a toggle, or a dedicated panel?~~ | Design | **Resolved** — implemented as "Ask" bar above Cypher editor with NL query history dropdown (localStorage). |
| **Local learning from user corrections** — see §14.1 below | Engineering / Product | **Cypher→AQL corrections: done.** NL→Cypher few-shot: not started. |

### 14.1) Local learning from user corrections

Users frequently need to edit the LLM-generated Cypher or the transpiled AQL before it produces correct results. These corrections represent high-value training signal that could improve future queries against the same database.

**Two feedback points in the pipeline:**

1. **NL → Cypher** *(not yet implemented)*: User edits the generated Cypher, runs it successfully, clicks "Learn." The `(nl_text, corrected_cypher, schema_hash)` triple is stored locally and injected as a few-shot example into future LLM prompts for similar questions.

2. **Cypher → AQL** *(implemented)*: User edits the transpiled AQL, runs it successfully, clicks "Learn." The `(cypher, corrected_aql, mapping_hash)` pair is stored and used as an override for identical Cypher inputs. These corrections also serve as a transpiler bug discovery queue.

**Storage (implemented):** Local SQLite file (`corrections.db`) with an `aql_corrections` table. Schema: `id`, `cypher`, `mapping_hash` (SHA-256 of conceptual + physical mapping), `database`, `original_aql`, `corrected_aql`, `bind_vars`, `created_at`, `note`. All data stays local — nothing is sent externally. Thread-safe via `threading.Lock`.

**Implementation details:**
- Backend module: `arango_cypher/corrections.py`
- REST API: `POST /corrections` (save), `GET /corrections` (list), `DELETE /corrections/{id}` (delete one), `DELETE /corrections` (clear all)
- Integration: `translate_endpoint` and `execute_endpoint` check for matching corrections before returning transpiled AQL. If a match is found, the corrected AQL is used with a warning message ("Using learned correction #N").
- `mapping_hash`: deterministic SHA-256 of the JSON-serialized conceptual schema + physical mapping, ensuring corrections don't apply across schema versions.

**Retrieval for NL → Cypher (planned):** Start with keyword overlap / BM25 to retrieve top-k similar past corrections for few-shot injection. Optionally upgrade to lightweight embeddings (sentence-transformers) later.

**Retrieval for Cypher → AQL (implemented):** Exact Cypher match + mapping hash — deterministic override.

**UI (implemented):**
- AQL editor is fully editable with a "modified" indicator when content diverges from transpiler output.
- **Learn** button: appears when AQL is modified; saves the `(cypher, mapping_hash, original_aql, corrected_aql)` tuple.
- **Learned (N)** button: toggles a corrections management panel listing all stored corrections with delete controls.

**Key concerns:** Schema drift (corrections become stale — `mapping_hash` ensures corrections only apply when schema matches), overfitting (per-example delete / clear-all available), privacy (all local, stated explicitly in UI).

### 14.2) Cardinality statistics for query optimization *(implemented)*

Graph query performance depends critically on traversal direction, join ordering, and filter placement. Without knowledge of collection sizes and edge fan-out/fan-in patterns, both the deterministic transpiler and the LLM-based NL→AQL generator make structurally valid but potentially expensive choices. Cardinality statistics address this by providing the query pipeline with quantitative knowledge about the physical data.

#### 14.2.1 Statistics computed

| Statistic | Scope | How computed | Purpose |
|-----------|-------|-------------|---------|
| **Document count** | Per vertex collection | `RETURN LENGTH(collection)` | Know which collections are large vs. small |
| **Edge count** | Per edge collection | `RETURN LENGTH(edgeCol)` | Know edge density |
| **Label count** | Per entity type (LPG/hybrid with type discriminator) | `FOR d IN col FILTER d.typeField == typeValue COLLECT WITH COUNT INTO c RETURN c` | When multiple entity types share a collection, know the per-type cardinality |
| **Avg out-degree** | Per edge collection, relative to source collection | `edgeCount / sourceVertexCount` | Measures fan-out: how many edges leave each source vertex on average |
| **Avg in-degree** | Per edge collection, relative to target collection | `edgeCount / targetVertexCount` | Measures fan-in: how many edges arrive at each target vertex on average |
| **Cardinality pattern** | Per relationship type | Derived from avg degree ratios | `1:1`, `1:N`, `N:1`, `N:M` classification |
| **Selectivity ratio** | Per relationship from→to | `edgeCount / (sourceCount × targetCount)` | How "selective" the relationship is — low ratio means sparse connectivity |

#### 14.2.2 How statistics aid NL→AQL generation (§1.3)

The physical schema summary passed to the LLM is enriched with cardinality context:

```
Collection 'Device' (entity: Device) — ~50,000 documents
Collection 'Tenant' (entity: Tenant) — ~120 documents

Edge collection 'tenantDevice' (TENANTDEVICE)
  Connects: Tenant('Tenant') -> Device('Device')
  ~50,000 edges, avg fan-out: 417/tenant, avg fan-in: 1/device
  Pattern: 1:N (each tenant has many devices; each device belongs to 1 tenant)
```

**Impact on LLM query generation:**

1. **Start from the selective side**: When the user asks "devices for tenant WPP", the LLM sees that Tenant has 120 docs and Device has 50,000, and that fan-out from Tenant is 417. It will generate `FOR t IN Tenant FILTER t.NAME == "WPP" FOR d IN OUTBOUND t tenantDevice RETURN d` rather than scanning all 50,000 devices.

2. **Avoid full scans on large collections**: If the LLM sees `Device: ~50,000`, it knows not to `FOR d IN Device` without a filter.

3. **Traversal direction choice**: Fan-out/fan-in ratios tell the LLM which direction produces fewer intermediate results. A relationship with avg fan-out of 1 and avg fan-in of 500 should be traversed OUTBOUND from the specific side.

4. **Aggregation strategy**: Knowing collection sizes helps the LLM choose between `COLLECT WITH COUNT` vs. `LENGTH()` and decide whether to add `LIMIT` for safety.

#### 14.2.3 How statistics aid Cypher→AQL transpilation (§1.2)

The deterministic transpiler uses statistics through `MappingResolver`:

1. **Traversal direction for undirected patterns**: Cypher `(a:Person)-[:KNOWS]-(b:Person)` (no arrow) currently maps to `ANY`. With statistics, if `KNOWS` has asymmetric fan-out (e.g., avg out-degree 5, avg in-degree 200), the transpiler can emit `OUTBOUND` from the filtered side instead of `ANY`, reducing the search space.

2. **Multi-part MATCH ordering**: When a query has multiple MATCH clauses or comma-separated patterns, the transpiler can order the outer loops to start from the most selective (smallest cardinality) collection.

3. **Filter placement**: Property filters on large collections should be pushed as early as possible. Statistics quantify "large" and help prioritize which filters to emit first.

4. **Index hint decisions**: Combined with index metadata (§5.7), cardinality informs whether an index scan or a full collection scan is cheaper.

#### 14.2.4 Data model

Statistics are stored in `MappingBundle.metadata["statistics"]`:

```json
{
  "statistics": {
    "computed_at": "2026-04-14T12:00:00Z",
    "collections": {
      "Device": { "count": 50000 },
      "Tenant": { "count": 120 },
      "tenantDevice": { "count": 50000, "is_edge": true }
    },
    "entities": {
      "Device": { "estimated_count": 50000 },
      "Tenant": { "estimated_count": 120 }
    },
    "relationships": {
      "TENANTDEVICE": {
        "edge_count": 50000,
        "avg_out_degree": 416.7,
        "avg_in_degree": 1.0,
        "cardinality_pattern": "1:N"
      }
    }
  }
}
```

#### 14.2.5 Implementation

- **Computation**: `compute_statistics(db, bundle)` in `arango_cypher/schema_acquire.py`. Uses fast AQL `LENGTH()` for counts; derived metrics computed in Python.
- **Storage**: Populated into `MappingBundle.metadata["statistics"]` during `get_mapping()` or via `GET /schema/statistics` endpoint.
- **MappingResolver**: New methods `estimated_count(label)`, `relationship_stats(rel_type)`, `preferred_traversal_direction(rel_type)`.
- **NL→AQL integration**: `_build_physical_schema_summary()` reads statistics from the bundle and appends count/degree annotations to each collection/edge line.
- **Transpiler integration**: `translate_v0.py` consults resolver statistics for undirected pattern direction choice and multi-part ordering.
- **API**: `GET /schema/statistics` endpoint returns computed statistics for the connected database.

## 15) Packaging and deployment to the Arango Platform

This section covers how `arango-cypher-py` is packaged for, and deployed to, the Arango Platform's Container Manager (see `docs/arango_packaging_service/` for the upstream platform API and ServiceMaker tool).

**What gets deployed.** The default platform deployment is **headless**: the library, CLI, and FastAPI HTTP endpoints in `arango_cypher.service` (§4.3). The Cypher Workbench UI (§4.4) is a debug/demo surface — it is **not** included in the default ServiceMaker tarball and is **not** exposed by the platform's Container Manager in standard deployments. Operators who want to run the UI against a deployed service do so locally (pointing their browser at a local dev server that proxies to the platform endpoint) or via a separate, explicitly-enabled packaging variant. Any future "UI-included" tarball must be opt-in, separately versioned, and carry the §4.4 scope disclaimer.

### 15.1 Design decision: fix the root cause upstream, don't build a toolchain here

The only real obstacle to packaging this repo for ServiceMaker is that our sibling library `arangodb-schema-analyzer` (source at `~/code/arango-schema-mapper`) is **not published to any package index**. It's declared in the `[analyzer]` extra of `pyproject.toml` as a bare name with no version, path, or URL; on a developer machine it works because `pip install -e ~/code/arango-schema-mapper` pre-installs it, but inside the ServiceMaker build container — which runs `uv sync --active` with no network access to a private index and no git auth — resolution fails with "no matching distribution found."

Once this problem is fixed at the source, *this repo needs no packaging tooling at all*. Deployment becomes `tar -czf` of the repo plus three documented curl commands.

**Chosen path:** publish `arangodb-schema-analyzer` to PyPI (or the ArangoDB-internal equivalent). This is roughly one day of work in the sibling repo and fixes the problem for every consumer of the analyzer, not just this one.

### 15.2 Why this over the alternatives

We considered and rejected three more-invasive approaches:

| Option | Why rejected |
|--------|--------------|
| **Vendor pre-built wheels into `vendor/` here** | Shifts the fix into every consumer repo. `pyproject.toml` rewriting logic, a `vendor/` refresh workflow, and a manifest-driven build script — all to work around a sibling repo that isn't published. Fixes the problem once per consumer rather than once total. |
| **Git URL deps (`arangodb-schema-analyzer @ git+ssh://...`)** | Build container needs SSH auth to the git host. Unreliable in opaque runners. Couples our build to the analyzer's git location and revision scheme. |
| **Monorepo vendoring (copy sibling source into `third_party/`)** | Source-level drift with upstream. Obscures the fact that these are distinct libraries with distinct release cycles. |

All three absorb a cost that belongs upstream. Publishing once is strictly simpler than vendoring forever.

We also considered and rejected **adding a full packaging-and-deployment CLI** (`package` / `deploy` / `redeploy` / `teardown` as Typer subcommands, backed by a `packaging.toml` manifest and an `httpx` client for the platform API). Reasons:

- **Packaging is repo-specific; deployment is generic.** If we ever want a deployment CLI, it belongs in its own tool that can deploy *any* ServiceMaker tarball — not buried inside a Cypher-to-AQL library.
- **Release cadence mismatch.** The Cypher translator shouldn't re-release because the platform API changed. And vice versa.
- **Token blast radius.** Platform bearer tokens stay with the deployment tool (or in `.env` for manual curl), never in this repo's dev loop.
- **Over-engineering for volume.** Until we redeploy more than weekly, a README section with three curl commands is strictly better than a CLI nobody runs. A CLI nobody runs rots faster than a runbook.

### 15.3 Deliverables (this repo)

All this repo owns:

1. **A README section** in `docs/arango_packaging_service/` documenting the manual deploy path:
   - Build the tarball: `uv build --sdist` (or `tar -czf`), producing `arango-cypher-py-<ver>.tar.gz`.
   - Upload: `curl POST /_platform/filemanager/global/byoc/` with `ARANGO_PLATFORM_TOKEN` from `.env`.
   - Deploy: `curl POST /_platform/acp/v1/uds` with the app instance spec.
   - Redeploy: bump `pyproject.toml` version, rebuild, upload, deploy (same three commands with a new version).
   - Teardown: `curl DELETE` against FileManager and ACP.

2. **Prerequisite checklist** in that same doc: ensure `arangodb-schema-analyzer` is pinned in `pyproject.toml` to a published version (no bare names, no paths, no git URLs) before packaging.

3. **A smoke test in CI** (gated behind `RUN_PACKAGING=1`, off by default): run `uv sync` against the packaged tarball inside a clean container and confirm it succeeds. Catches dependency-graph regressions that would break a deploy — without the overhead of an actual platform round-trip.

### 15.4 Deliverables (outside this repo)

Not in scope for `arango-cypher-py`, tracked for visibility:

1. **Publish `arangodb-schema-analyzer` to PyPI.** Tracked in `~/code/arango-schema-mapper`. Until this lands, deploying `arango-cypher-py` is blocked and any workaround here would be throw-away.
2. **(Optional, future) `arango-platform-deploy` CLI.** A separate project — or a contribution to ServiceMaker itself — that wraps the Container Manager API as `deploy` / `redeploy` / `teardown` subcommands over any tarball. Generic; reusable for every ArangoDB Python service. Built only when the volume of deployments justifies it, which is not today.

### 15.5 Open questions and future work

| Question | Owner | Target resolution |
|----------|-------|-------------------|
| Timeline and process for publishing `arangodb-schema-analyzer` to PyPI. | `arango-schema-mapper` owner | Before first Arango Platform deployment of `arango-cypher-py` |
| Does ArangoDB have an internal package index we should target instead of (or in addition to) public PyPI? | Engineering / DevOps | Before first publication |
| Should `arango-query-core` (the workspace sibling package in this repo) be split into a separately published library? It is co-packaged by hatchling today, which works fine for `arango-cypher-py` consumers but not for downstream libraries that want the core without the Cypher layer. | Engineering | Before v1.0 |
| Can ServiceMaker's base images be extended to include `rdflib` + `neo4j` driver so the `owl` and `neo4j` extras don't inflate every deployed tarball? | Engineering / ArangoDB ServiceMaker team | v0.4 |
| When deployment volume justifies automation, does the deployment tool live in a new dedicated repo or as a contribution to upstream ServiceMaker? | Product | When redeploy frequency > weekly |

## 16) Development guide reference

For contributor onboarding and development workflow, see:

- **Environment setup**: install with `pip install -e ".[dev,service]"`. Requires Python 3.10+.
- **Running tests**:
  - Unit + golden: `pytest -m "not integration and not tck"`
  - Integration (requires ArangoDB): `docker compose up -d && RUN_INTEGRATION=1 pytest -m integration`
  - TCK (requires ArangoDB): `python scripts/download_tck.py && RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck`
- **Regenerating ANTLR parser**: `antlr4 -Dlanguage=Python3 -visitor grammar/Cypher.g4 -o arango_cypher/_antlr`
- **Starting dev servers**: `uvicorn arango_cypher.service:app --reload --port 8001` (backend) + `cd ui && npm run dev` (frontend on port 5173, proxies API to 8001)
- **Adding a new Cypher construct**: modify `arango_cypher/translate_v0.py` (translation logic), add golden test cases to `tests/fixtures/cases/`, run `pytest` to verify, update §6.4 of this PRD.
- **Code quality**: `ruff check .` for linting. `mypy` is recommended but not yet enforced in CI.
