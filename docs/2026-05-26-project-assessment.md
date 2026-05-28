# Project Assessment

Date: 2026-05-26

## Executive Summary

`arango-cypher-py` is a production-oriented Python stack for ArangoDB: a deterministic openCypher-to-AQL transpiler, an NL-to-Cypher-to-AQL pipeline, a FastAPI service, and a React Cypher Workbench UI. The project is past prototype stage for its core paths: it has golden tests, Neo4j cross-validation, NL evaluation gates, packaging smoke tests, service hardening, and a documented multi-tenant safety plan.

The codebase is still early in full openCypher compliance and mid-flight on multi-tenant defense in depth. The strongest parts are the conceptual-schema architecture, test coverage, service hardening, and operational documentation. The main remaining risks are the large transpiler core, documentation drift, incomplete tenant rewrite layers, and a few manual closeout steps.

## Current State

### Built Capabilities

- Core Cypher-to-AQL translation covers broad read/query patterns: `MATCH`, `WHERE`, `RETURN`, `WITH`, `OPTIONAL MATCH`, `UNION`, `UNWIND`, path functions, comprehensions, regex, `EXISTS`, and `arango.*` extensions.
- Write-clause support is tracked as done in the PRD and implementation plan, including `CREATE`, `SET`, `DELETE`, `DETACH DELETE`, and `MERGE`.
- The schema analyzer is the canonical mapping source, with hardened heuristic fallback and visible service warnings when fallback is used.
- The NL-to-Cypher pipeline is implemented with OpenAI, Anthropic, and OpenRouter providers, dynamic few-shot retrieval, fuzzy entity resolution, EXPLAIN-grounded retry, prompt caching, tenant guardrails, and eval baselines.
- The FastAPI service is substantially hardened: request validation, sanitized errors, rate limits, structured observability, connection safeguards, tenant binding, and safe execution paths.
- The Workbench UI includes editors, mapping panels, results views, graph visualization, NL query flow, query history with optional result snapshots (restore without re-run), and export features.
- CI includes lint, format check, unit tests, packaging smoke, and ArangoDB integration tests. Nightly NL eval provides additional regression signal.

### Quality Strengths

- Strong automated test posture across unit, golden, integration, TCK harness, Neo4j cross-validation, NL eval, service hardening, packaging, and UI state tests.
- Recent code-health work split the service and transpiler compatibility layers into focused packages.
- Security and operational concerns are treated as first-class: bind variables, identifier validation, SSRF guardrails, error redaction, rate limiting, tenant-plan validation, and audit-oriented logs.
- Documentation is unusually rich and includes PRDs, implementation plans, audit history, deployment runbooks, TCK coverage, and schema-analyzer handoff notes.

### Quality Risks

- `arango_cypher/_translate_v0/core.py` remains large at roughly 3,783 lines. It is the main maintainability risk and exceeds the repository's stated source-file size guideline.
- The target compiler architecture in `docs/python_prd.md` describes normalized AST and logical-plan stages, but the implementation still largely walks parse trees and emits AQL directly.
- Documentation drift was addressed 2026-05-26 (README status/subset, multitenant layer table, schema-inference PRD status, archived `remaining-tasks-prompt.md`). PRD §implementation-status rows may still lag until the next full PRD edit pass.
- openCypher TCK coverage remains partial. Translation-only coverage is about 32% of the full TCK, 55% of the core subset, and 66% for clause-focused scenarios.
- Multi-tenant security has a strong Layer 5/6 execution boundary, but algorithmic Cypher/AQL tenant-injection layers are still planned.

## Remaining Implementation Plan

### 1. Close Out Shipped Work

- Complete WP-19 staging deployment walkthrough for Arango Platform packaging.
- Run the schema-inference PRD manual E2E closeout on the pilot database and update the closeout log.
- Run `POST /schema/force-reacquire` on deployed services that may contain pre-WP-27 poisoned schema-cache entries.
- ~~Finish, verify, and land the current query-history UI changes.~~ **Done 2026-05-26** (bounded snapshots, restore, connection filter, 27 Vitest cases).
- ~~Update stale docs so README, PRDs, and implementation-plan state match the current code.~~ **Partial 2026-05-26** (README, multitenant exec table, schema-inference PRD, archived remaining-tasks prompt).

### 2. Complete Multi-Tenant Safety Waves

- ~~MT-2: harden the guardrail by rejecting literal tenant predicates in generated Cypher.~~ **Done 2026-05-27 (PR #30).**
- ~~MT-3: add the Cypher tenant-injection pass before transpilation.~~ **Phase 3a done 2026-05-27 (PR #30)** — rewriter core landed; service-route wiring remains as MT-3b follow-up.
- ~~MT-4: add the AQL tenant-injection pass for direct AQL and NL-to-AQL paths.~~ **Done 2026-05-27 (PR #30).**
- MT-3b: wire the Cypher AST rewriter into `/translate` / `/execute` / `/nl2cypher` (currently MT-3a ships only the rewriter module).
- MT-6: add Layer 5 plan-shape caching to reduce EXPLAIN validation overhead.
- MT-7: implement admin bypass and audit log stream.
- MT-8: maintain a standing red-team corpus and security review loop.

### 3. Finish v0.4+ Residual Transpiler Work

- Multi-column `RETURN DISTINCT`.
- Expression support for `LIMIT` and `SKIP`.
- Native `shortestPath()` lowering.
- Index-hint emission and VCI advisory polish.

### 4. Decide the Next Strategic Investment

The two largest next-step options are:

- TCK breadth: relax the leading-`MATCH` constraint, support multi-type and typeless relationships, and address precedence/comparison/map/literal edge cases.
- Compiler architecture: introduce normalized AST and logical-plan stages, then migrate translation incrementally out of the direct parse-tree emission model.

These should not be pursued as one big-bang effort. The safer path is to choose one track, slice it into small verifiable increments, and preserve the existing golden and integration test discipline.

## Recommended Sequence

1. ~~Land the in-progress UI changes and documentation sync.~~ **Done 2026-05-26.**
2. Close WP-19 and the schema-inference manual pilot checks.
3. ~~Execute Wave 8 multi-tenant work: MT-2, MT-3, and MT-4.~~ **Done 2026-05-27 (PR #30; MT-3 service wiring deferred to MT-3b).**
4. In parallel or immediately after, complete Wave 11 transpiler polish.
5. Choose either TCK breadth or compiler architecture as the next major workstream.

## Bottom Line

The codebase is in a solid intermediate state: more mature than a prototype, not yet a complete openCypher implementation or fully finished multi-tenant platform component. The current implementation is well tested and security-conscious, but it needs focused follow-through on documentation truth, tenant rewrite layers, and transpiler modularity before it should be treated as a stable long-term foundation.
