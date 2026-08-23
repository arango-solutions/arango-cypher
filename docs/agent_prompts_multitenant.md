# Multi-Sub-Agent Implementation Prompts — Multi-tenant safety + v0.4+ tail

Date: 2026-05-06
Derived from: [`multitenant_prd.md`](./multitenant_prd.md) (MT-0..MT-8), [`implementation_plan.md`](./implementation_plan.md) (v0.4+ residual tail)
Companion to: [`agent_prompts.md`](./agent_prompts.md) (Waves 1–6 for v0.1..v0.4 work)

---

## How to use this document

This document picks up where `agent_prompts.md` leaves off. It contains ready-to-use prompts for the multi-tenant safety architecture (Waves 7–10) and the v0.4+ language/optimization residual tail (Wave 11). The shared context block, project overview, repository layout, key types, golden-test conventions, and quality rules are defined once at the top of `agent_prompts.md` (lines 26–134) — every prompt below begins with the marker `{SHARED CONTEXT BLOCK — paste from agent_prompts.md}` and assumes those rules apply.

### Wave numbering continues from `agent_prompts.md`

| Wave | Scope | Source-of-truth doc | Status |
|---|---|---|---|
| 1–3 | v0.2 (CREATE/aggregation/CLI/TCK) | `agent_prompts.md` | Done |
| 4   | WP-25 NL→Cypher hardening | `agent_prompts.md` | Done |
| 5   | Pre-WP-25 cleanup + plan hygiene | `agent_prompts.md` | Done |
| 6   | WP-27..WP-30 schema-inference + NL feedback bug-fix | `agent_prompts.md` | Drafted |
| **7** | **MT-Phase 1 — session binding + Layer 5 validator (security boundary)** | **this doc** | **Drafted** |
| **8** | **MT-Phase 2 — guardrail hardening + AST rewrite passes (parallel)** | **this doc** | **Drafted** |
| **9** | **MT-Phase 3 — operability (plan LRU + admin bypass + audit log)** | **this doc** | **Drafted** |
| **10** | **MT-8 — standing red-team corpus** | **this doc** | **Drafted** |
| **11** | **v0.4+ residual tail (DISTINCT multi-col, LIMIT/SKIP exprs, native shortestPath, index hints, VCI advisory)** | **this doc** | **Drafted** |

### Orchestration overview

```
Wave 7  (1 agent, ~2 weeks)        ──►  demoable MVP closes T1, T4, T5, T7
   │
   ▼
Wave 8-pre (1 agent, ~0.5 d)       ──►  shared tenant_ast_common module
   │
   ▼
Wave 8a (3 parallel agents, ~3 weeks) ──►  closes T2, T6 structurally
   │
   ▼
Wave 9   (1 agent, ~1 week)        ──►  production-ready operability
   │
   ▼
Wave 10  (standing, 1 agent)       ──►  every red-team escape becomes a test
```

Wave 11 is **independent** of Waves 7–10 and can run any time as filler against an idle agent — the tail items have no shared files with the multi-tenant work.

### Quality rules specific to multi-tenant work

These extend the rules in `agent_prompts.md` §Quality rules; do not relax them.

1. **Fail-closed everywhere.** Any internal error, ambiguity, missing manifest entry, or unparseable plan → refuse, never permit. The unit-test name template is `test_<feature>_fails_closed_when_<condition>`.
2. **No string-template tenant predicates.** The tenant identifier is always a bind variable (`@tenantId` / `@tenantKey`). Layer 5 actively rejects plans whose tenant constraint is encoded as a string literal.
3. **The LLM is never trusted.** Layer 2 (guardrail) is a *quality* measure, not a safety boundary. Layer 5 is the boundary. Comments and docstrings must reflect this taxonomy — do not call Layer 2 a "defense" without qualifying it as soft.
4. **Bind-var spread order is load-bearing.** `safe_execute` must spread session values *after* client-supplied bind vars: `bind_vars = {**client_bind_vars, "tenantId": session.tenant_id, "tenantKey": session.tenant_key}`. The session value silently wins. Tests must pin this with a deliberate-conflict case.
5. **Audit logs are the deliverable.** Every Layer 5 pass logs a plan digest; every Layer 5 refusal logs a structured violation. Tests assert log shape, not just behaviour.
6. **No new dependency without justification.** AQL plan walking is plain dict traversal — do not pull in a graph library.
7. **Offline graceful degradation.** Unit tests for Layers 3, 4, 5 must run without a live ArangoDB. Use the existing `tests/integration/` gate (`RUN_INTEGRATION=1`) for live-DB cases; never let unit tests touch the network.

---

## Wave 7 — Multi-tenant Phase 1 (1 agent, sequential, ~2 weeks)

**Goal.** Demoable MVP that closes threats T1 (underconstraint), T4 (body-supplied tenant), T5 (direct-AQL bypass), and T7 (bind-var override) for the common case. Produces auditable proof via the Layer 5 validator's structured log.

**Why one agent, not three.** MT-5 is the only check that matters for security audit (`multitenant_prd.md` §2.1 + §8.3). It is ~300 LOC of safety-critical logic with non-local invariants (subquery recursion, traversal kinds, plan-shape coverage). Splitting it across sub-agents trades ~3 days of wall-clock for a worse threat-model fit and harder review. MT-1 (~80 LOC, session binding) and the residual half of MT-0 (`scopingPathFromTenant` BFS, ~20 LOC) are dependencies of MT-5 and small enough that bundling them into the same agent removes inter-WP coordination overhead entirely.

**PRD source-of-truth.** [`multitenant_prd.md`](./multitenant_prd.md) §1.2, §3, §4 (Layer 1), §8 (Layer 5), §9 (Layer 6).

---

### Wave 7 — MT-Phase-1: session binding + scoping-path BFS + Layer 5 EXPLAIN validator + safe_execute

```
{SHARED CONTEXT BLOCK — paste from agent_prompts.md lines 26–134}

## Your task: Wave 7 — Implement Layer 1 session binding, the residual `scopingPathFromTenant` BFS, the Layer 5 EXPLAIN-plan validator, and the `safe_execute` boundary helper

### Background

`docs/multitenant_prd.md` §1.2 defines a query as **safe** if every collection it reads is satellite, OR is a smartgraph collection with `doc.<smartGraphAttribute> == @tenantId` (bind-var) on the access node, OR is `Tenant` keyed by `@tenantKey`, OR runs inside a constrained traversal/subquery. Layer 5 verifies this against the ArangoDB EXPLAIN plan **independently of every upstream layer**: it trusts no LLM, no guardrail, no AST pass, no transpiler. If Layer 5 passes, the query is safe by definition; if it refuses, the query does not run.

Layer 1 (this WP, Part 1) ensures the `@tenantId` Layer 5 verifies against actually came from the authenticated session and was never sourced from the request body. Without Layer 1, Layer 5 can only verify "the bind-var matches itself"; with Layer 1, Layer 5 verifies "the bind-var matches the session-bound tenant the user authenticated as."

The MT-0 residual (`scopingPathFromTenant`) is the BFS-derived edge path from the `Tenant` collection to a tenant-scoped entity that lacks a denormalised tenant column. Layer 3 will need it later (Wave 8) to rewrite traversal-only patterns; Layer 5 needs it now to recognise a constrained traversal as safe.

### What to implement

This WP has four parts. They can be implemented in any order, but the recommended sequence is Part 1 → Part 2 → Part 3 → Part 4 because each builds on the prior.

---

#### Part 1 — Layer 1: session-bound `@tenantId`

`multitenant_prd.md` §4. The `_Session` class in `arango_cypher/service/security.py` does not yet carry a tenant identifier; add it.

1. **Extend `_Session`** (in `arango_cypher/service/security.py`):
   ```python
   class _Session:
       __slots__ = (
           "token", "db", "client", "created_at", "last_used",
           "tenant_id", "tenant_key", "is_admin",
       )

       def __init__(
           self,
           token: str,
           db: StandardDatabase,
           client: ArangoClient,
           *,
           tenant_id: str | None = None,
           tenant_key: str | None = None,
           is_admin: bool = False,
       ):
           ...
   ```
   Default values are `None` / `False` so that existing callers and workbench-mode flows keep working. A `None` tenant_id means "not in tenant-user mode" — Layer 5 will refuse to validate any tenant-scoped access in that state.

2. **`/connect` accepts an optional `tenantId`** (in `arango_cypher/service/routes/connect.py`):
   - Add `tenantId: str | None = None` and `tenantKey: str | None = None` to the connect request model. (`tenantKey` is usually equal to `tenantId` for `_key`-keyed Tenant collections; allow override for deployments that key by another field.)
   - Validate at connect time: query the `Tenant` collection (read manifest from the bundle) for a document with `_key == tenantKey`; if missing, return HTTP 403 with body `{"error": "unknown_tenant", "tenantId": tenantId}`.
   - Construct the session with the validated values.

3. **Tenant-user mode vs. workbench mode.** Add an env-var gate `ARANGO_CYPHER_WORKBENCH=1`:
   - **Workbench (default for local dev):** `tenant_context` field on `/nl2cypher` and `/nl2aql` requests is honoured. Existing behaviour preserved.
   - **Tenant-user mode (`ARANGO_CYPHER_WORKBENCH` unset or `0`):** the body's `tenant_context` value is silently ignored if it differs from `session.tenant_id`. Emit `logger.warning("body-supplied tenant_context=%r ignored; session-bound tenant=%r", body_tenant, session.tenant_id)`. The downstream pipeline always uses `session.tenant_id`.

4. **Admin sessions.** The `is_admin` flag is set at `/connect` if the auth token / claim says so (deployment-dependent — pass through the existing connect input). Admin sessions go through Layers 2–5 as normal; only the `cross_tenant=True` request flag (added in Wave 9) relaxes the boundary, and that's not your concern here.

#### Part 2 — Residual MT-0: `scopingPathFromTenant` BFS

`multitenant_prd.md` §3 (last paragraph) and §6.2. The schema mapper supplies `tenantScope.role` and `tenantScope.denormField` (analyzer ≥ 0.5); it does **not** yet supply the BFS-derived edge path from `Tenant` to a `TENANT_SCOPED` entity that lacks a denorm field. Compute it locally.

1. **New helper in `arango_cypher/nl2cypher/tenant_scope.py`:**
   ```python
   def compute_scoping_path(
       manifest: TenantScopeManifest,
       *,
       from_label: str = "Tenant",
       to_label: str,
   ) -> list[str] | None:
       """Return the ordered list of relationship-type names connecting `from_label`
       to `to_label`, or None if no path exists.

       BFS over the `physicalMapping.relationships` graph. Prefers shortest path;
       on ties, lexicographic on the relationship-type names.
       """
   ```

2. **Wire it into `analyze_tenant_scope` once.** When a `TENANT_SCOPED` entity has no `tenantField`, populate `EntityScope.scoping_path: list[str] | None` by calling `compute_scoping_path`. Existing `EntityScope` carries `role` and `tenant_field`; extend the dataclass with a third field. Default `None`.

3. **Caching.** The BFS is computed at most once per `TenantScopeManifest` instance. Memoize on the manifest's `__hash__` (or wrap into a `functools.cache` keyed by `(id(manifest), to_label)` — manifests are short-lived per request).

4. **Acceptance is observability, not just function-call return.** Log at INFO once per session: `"scoping paths: " + json.dumps({label: path for label, path in manifest.scoping_paths.items() if path})`.

#### Part 3 — Layer 5: EXPLAIN-plan validator (the security boundary)

`multitenant_prd.md` §8. New module `arango_cypher/tenant_plan_validator.py`.

1. **Public entry point:**
   ```python
   def validate_plan(
       *,
       db: StandardDatabase,
       aql: str,
       bind_vars: dict[str, Any],
       manifest: TenantScopeManifest,
       sharding_profile: dict[str, Any] | None,
       session: _Session,
   ) -> None:
       """Raise TenantScopeViolation if the plan is unsafe under §1.2."""
   ```

2. **Algorithm.** Implement exactly `multitenant_prd.md` §8.2's pseudocode. The pre-flight bind-var sanity check is first:
   ```python
   if not session.tenant_id:
       raise TenantScopeViolation(
           code="NO_SESSION_TENANT",
           message="session has no tenant_id; cannot validate scoped query",
       )
   if bind_vars.get("tenantId") != session.tenant_id:
       raise TenantScopeViolation(
           code="TENANT_BIND_MISMATCH",
           message=f"bind_vars['tenantId']={bind_vars.get('tenantId')!r} "
           f"does not match session={session.tenant_id!r}",
       )
   ```

3. **Plan walk.** Call `db.aql.explain(aql, bind_vars=bind_vars)` with `all_plans=False`; iterate `plan["nodes"]`. Per node type:
   - `EnumerateCollectionNode`: read `node["collection"]`, look up its physical-layout kind (`satellite` / `smartgraph` / `regular` / `system`) from `sharding_profile.members[coll].kind`. Satellite → continue. Smartgraph and regular: look for a child `FilterNode` / `IndexNode` / `CalculationNode` whose condition references this node's `outVariable.name` and binds `@tenantId` against the smartGraphAttribute or `manifest.entity_scope(coll).tenant_field`. Bind-var form **only**; reject literal predicates.
   - `IndexNode`: inspect `node["condition"]` for an equality on the smartGraphAttribute keyed against `@tenantId`. Same bind-var rule.
   - `TraversalNode`: pass if (a) every vertex collection in `node["graph"]["vertexCollections"]` is satellite, OR (b) `node["options"]["prune"]` references `@tenantId`, OR (c) `node["graphName"]` resolves via `sharding_profile.graphs[*]` to a graph with `isDisjoint=True` and `style="DisjointSmartGraph"`. Otherwise refuse.
   - `SubqueryNode`: recurse `validate_subplan(node["subquery"]["nodes"], ...)`. Bind-var scope is shared.
   - `EnumerateListNode` / `CalculationNode` / `ReturnNode` / `LimitNode` / `SortNode` / `CollectNode`: not collection accesses; pass through. `CollectNode` over a tenant-scoped enumeration is implicitly safe iff its parent enumeration was constrained (the recursion guarantees this).

4. **Helpers.** Three private helpers:
   ```python
   def _node_has_tenant_predicate(plan: dict, node: dict, bind_vars: dict) -> bool: ...
   def _index_covers_tenant(node: dict, manifest: TenantScopeManifest, bind_vars: dict) -> bool: ...
   def _traversal_constrained_to_tenant(node: dict, manifest, sharding_profile, bind_vars: dict) -> bool: ...
   ```
   Each is pure (no DB access, no side effects); each is independently unit-testable against hand-crafted plan fragments.

5. **`TenantScopeViolation` exception.** New class in the same module. Carries `code`, `message`, `aql_digest` (sha256 of `aql + sorted bind_vars`), `plan_digest` (sha256 of the plan JSON). The digests go into the audit log.

6. **Audit log.** Every refusal:
   ```python
   logger.warning(
       "TENANT_SCOPE_VIOLATION code=%s session=%s tenant=%s aql_digest=%s plan_digest=%s message=%s",
       violation.code,
       session.token[:8],
       session.tenant_id,
       violation.aql_digest[:16],
       violation.plan_digest[:16],
       violation.message,
   )
   ```
   Every pass: `logger.info("TENANT_SCOPE_OK ...same fields...")`. Both lines are required for audit replay; do not skip the OK line for performance reasons.

#### Part 4 — Layer 6: `safe_execute` wrapper

`multitenant_prd.md` §9. New helper in `arango_query_core/exec.py` (or `arango_cypher/tenant_plan_validator.py` if cohesion is tighter — judge based on dependency direction, but prefer `exec.py` so the executor pattern stays canonical).

```python
def safe_execute(
    *,
    db: StandardDatabase,
    aql: str,
    client_bind_vars: dict[str, Any],
    manifest: TenantScopeManifest,
    sharding_profile: dict[str, Any] | None,
    session: _Session,
):
    bind_vars = {
        **client_bind_vars,
        "tenantId": session.tenant_id,
        "tenantKey": session.tenant_key,
    }
    validate_plan(
        db=db,
        aql=aql,
        bind_vars=bind_vars,
        manifest=manifest,
        sharding_profile=sharding_profile,
        session=session,
    )
    return db.aql.execute(aql, bind_vars=bind_vars)
```

The spread order is **load-bearing**; pin it with a unit test that supplies a wrong `tenantId` in `client_bind_vars` and asserts the session value wins.

**Wire `safe_execute` into every execute call site in `arango_cypher/service/routes/`:**
- `cypher.py::translate_endpoint` and the `/execute` handler.
- `nl.py::nl2cypher_endpoint` and `nl2aql_endpoint`.
- Any `/execute-aql` handler (if it exists; otherwise note in the PR description that the raw-AQL path is not yet exposed).

The ad-hoc `db.aql.execute(...)` call sites must all migrate. Audit grep: `rg "db\.aql\.execute" arango_cypher/service/`.

### Where to make changes

- `arango_cypher/service/security.py` — `_Session` slots + tenant fields + admin flag.
- `arango_cypher/service/routes/connect.py` — `tenantId` / `tenantKey` validation on `/connect`.
- `arango_cypher/service/routes/{cypher,nl}.py` — replace direct `db.aql.execute` with `safe_execute`.
- `arango_cypher/nl2cypher/tenant_scope.py` — `compute_scoping_path` + `EntityScope.scoping_path` field.
- `arango_cypher/tenant_plan_validator.py` — new; `validate_plan`, helpers, `TenantScopeViolation`.
- `arango_query_core/exec.py` — `safe_execute` helper.
- `tests/test_tenant_plan_validator.py` — new; the §8.6 test list (most important suite).
- `tests/test_tenant_scoping_path.py` — new; BFS correctness on hand-crafted manifests.
- `tests/test_session_tenant_binding.py` — new; Layer 1 session-binding cases.

### Tests to add

#### `tests/test_tenant_plan_validator.py` — `multitenant_prd.md` §8.6 in full

Each test takes a hand-crafted plan dict (no live DB) and asserts validate or refuse:

- `test_collection_scan_without_tenant_filter_rejected` — `EnumerateCollectionNode` over `Employee` with no child filter → rejected, `code="UNCONSTRAINED_COLLECTION_SCAN"`.
- `test_collection_scan_with_literal_tenant_predicate_rejected` — filter is `e.TENANT_HEX_ID == 'tenant-B-uuid'` (literal) → rejected, `code="LITERAL_TENANT_PREDICATE"`.
- `test_collection_scan_with_bindvar_tenant_predicate_accepted` — filter binds `@tenantId` → accepted.
- `test_bindvar_mismatch_rejected` — bind_vars[`tenantId`] != session.tenant_id → rejected, `code="TENANT_BIND_MISMATCH"`.
- `test_index_node_with_tenant_equality_accepted` — IndexNode whose condition is `e.TENANT_HEX_ID == @tenantId` → accepted.
- `test_index_node_without_tenant_equality_rejected` — IndexNode keyed only by `_key` on a tenant-scoped collection → rejected.
- `test_traversal_any_with_satellite_in_play_no_prune_rejected` — TraversalNode over `ANY` direction, vertex collections include a satellite, no `prune` → rejected, `code="UNCONSTRAINED_TRAVERSAL"`.
- `test_traversal_outbound_disjoint_smartgraph_accepted` — TraversalNode over `OUTBOUND` on a graph whose `sharding_profile.graphs[*]` says `isDisjoint=True` → accepted without explicit prune.
- `test_traversal_with_prune_on_tenant_id_accepted` — TraversalNode whose `options.prune` references `@tenantId` → accepted.
- `test_subquery_unconstrained_rejected` — SubqueryNode whose body scans `Employee` without a filter → rejected (recursion).
- `test_count_subquery_unconstrained_rejected` — `COUNT { FOR e IN Employee RETURN 1 }` plan → rejected.
- `test_satellite_only_query_accepted` — `FOR c IN Country RETURN c` (Country is satellite) → accepted.
- `test_no_session_tenant_in_tenant_user_mode_rejected` — session.tenant_id is None and the plan touches a TENANT_SCOPED collection → rejected, `code="NO_SESSION_TENANT"`.
- `test_pass_logs_OK_record` — accepted plan emits a `TENANT_SCOPE_OK` log line with both digests.
- `test_violation_logs_warning_with_digests` — refused plan emits a `TENANT_SCOPE_VIOLATION` log line with both digests.

Every accept case is also exercised with a deliberately-wrong session tenant to confirm the bind-var sanity check dominates.

#### `tests/test_tenant_scoping_path.py`

- `test_bfs_finds_shortest_path` — manifest has two paths from `Tenant` to `Asset` (length 2 and length 3); BFS returns the length-2 path.
- `test_bfs_lex_tiebreak` — two paths of equal length; alphabetic on relationship-type names.
- `test_bfs_no_path_returns_none` — disconnected entity → `None`.
- `test_compute_scoping_path_memoized` — second call with same args is O(1) (no graph rebuild).

#### `tests/test_session_tenant_binding.py`

- `test_connect_validates_tenant_exists` — `/connect` with unknown `tenantId` → 403.
- `test_connect_persists_tenant_in_session` — `/connect` with valid tenant → subsequent requests see `session.tenant_id`.
- `test_workbench_mode_honors_body_tenant_context` — `ARANGO_CYPHER_WORKBENCH=1`; body value used; no warning.
- `test_tenant_user_mode_ignores_body_tenant_context` — env unset; body value with mismatched tenant is ignored; WARN log emitted.
- `test_safe_execute_session_tenant_wins` — `client_bind_vars["tenantId"] = "tenant-B"` while `session.tenant_id == "tenant-A"`; final `bind_vars["tenantId"] == "tenant-A"`.

### Out of scope (do NOT change)

- Layer 2 guardrail (`tenant_guardrail.py`) — Wave 8a (MT-2) hardens it. This wave only **reads** the manifest the guardrail produces.
- Layer 3 (Cypher AST rewrite) and Layer 4 (AQL AST rewrite) — Wave 8a. Those passes are not yet present; Layer 5 must validate plans **without** assuming Layers 3 or 4 ran.
- Plan-shape LRU — Wave 9 (MT-6). Every execute pays for one EXPLAIN today; that is acceptable for the MVP.
- Admin `cross_tenant=True` flag — Wave 9 (MT-7). Admin sessions in this WP behave exactly like tenant-user sessions.
- The `/execute-aql` endpoint — if it does not exist yet, do not invent it. Note in the PR description that hand-AQL submissions are not currently accepted, so Wave 8a's Layer 4 has no entry point until that endpoint is added (separate WP).

### Acceptance criteria

- `pytest -m "not integration and not tck"` — all green (existing + new ~25 tests).
- `ruff check .` — clean.
- `cd ui && npx tsc --noEmit -p tsconfig.app.json && npm run build` — clean (UI is unchanged but the build must not break).
- A `safe_execute` grep shows it wraps every `db.aql.execute` call in `arango_cypher/service/routes/`.
- Integration smoke (`RUN_INTEGRATION=1 RUN_MULTITENANT=1 pytest tests/integration/test_multitenant_phase1_smoke.py`):
  - Seed a two-tenant disjoint-SmartGraph fixture (`tests/integration/datasets.py` extension).
  - Connect as tenant A. Issue `MATCH (e:Employee) RETURN e` via `/translate` then `/execute`.
  - Assert: zero rows from tenant B in the result set; one `TENANT_SCOPE_OK` log line with the expected plan digest.
  - Hand-craft an AQL submission attempting `FOR e IN Employee RETURN e` (no filter) — assert `TENANT_SCOPE_VIOLATION` and HTTP 4xx.

### Hand-off to Wave 8

- Layer 5 must remain functional after Wave 8 lands Layers 3 and 4. Specifically: a plan that has been pre-constrained by Layer 3/4 still passes Layer 5 (the predicates Layer 5 looks for are exactly those Layers 3/4 inject). Pin this with a Wave-7 test that hand-crafts a Layer-3-style pattern (`{TENANT_HEX_ID: @tenantId}` inline property) and confirms acceptance — so when Layer 3 lands, no Layer 5 changes are needed.
- The `EntityScope.scoping_path` field added in Part 2 is the contract Wave 8a's MT-3 will consume to rewrite traversal-only Cypher patterns.
```

---

## Wave 8 — Multi-tenant Phase 2 (1 pre-step + 3 parallel agents, ~3 weeks)

**Goal.** Close T2 (injection) and T6 (aggregation leakage) **structurally** by mechanically rewriting Cypher and AQL ASTs to inject tenant predicates the validator already knows how to verify. After Wave 8a, the demo for a security review is "open the network tab, see every query rewritten, see Layer 5 refuse every hand-tampered query."

**Why parallel.** MT-2 (~30 LOC), MT-3 (~400 LOC, Cypher AST), and MT-4 (~500 LOC, AQL AST) touch disjoint files: `tenant_guardrail.py`, `tenant_ast_cypher.py` (new), `tenant_ast_aql.py` (new). Each has its own ~25-test surface. Three sub-agents finish in ~3 weeks of wall-clock vs. ~5 weeks sequential.

**Pre-step.** All three agents will need the same vocabulary: a "what does a tenant predicate look like" definition (property-map injection, WHERE-clause injection, prune-options injection); a manifest reader that returns `(role, tenant_field, scoping_path)` for a label or collection; a bind-var-name-vs-literal classifier. Without a shared module, each agent will reinvent it differently and the merge will be miserable. Land the pre-step on `main` first; the three agents import from it.

**PRD source-of-truth.** [`multitenant_prd.md`](./multitenant_prd.md) §5.2 (MT-2), §6 (MT-3 Layer 3), §7 (MT-4 Layer 4).

---

### Wave 8-pre — Shared `tenant_ast_common` module (1 agent, sequential, ~0.5 day)

```
{SHARED CONTEXT BLOCK — paste from agent_prompts.md lines 26–134}

## Your task: Wave 8-pre — Extract shared tenant-rewrite vocabulary into `tenant_ast_common`

### Goal

Three sub-agents (Wave 8a) will simultaneously implement Layer 2 hardening (MT-2), Cypher AST rewriting (MT-3), and AQL AST rewriting (MT-4). They must agree on what a "tenant predicate" is, how to read the manifest, and how to distinguish bind vars from literals. Land this shared module **first** so the three agents extend a common vocabulary instead of inventing one each.

### What to implement

New module `arango_cypher/nl2cypher/tenant_ast_common.py`. Pure Python, no AST library, no DB access:

```python
from dataclasses import dataclass
from .tenant_scope import TenantScopeManifest, EntityTenantRole

# Bind-var names — single source of truth so the three rewriters can't drift.
TENANT_ID_BIND = "tenantId"
TENANT_KEY_BIND = "tenantKey"


@dataclass(frozen=True)
class TenantPredicateShape:
    """How a tenant predicate is rendered, regardless of layer."""

    style: str  # "property_map" | "where_eq" | "traversal_path" | "prune"
    field: str | None  # smartGraphAttribute or denormField; None for traversal_path
    bind_name: str  # "tenantId" or "tenantKey" — never a literal
    scoping_path: list[str] | None  # for style == "traversal_path"


def predicate_for_entity(
    label: str,
    manifest: TenantScopeManifest,
) -> TenantPredicateShape | None:
    """Return the canonical predicate shape for `label`, or None for GLOBAL.

    Decision rule:
      role == GLOBAL          → None
      role == TENANT_ROOT     → property_map / _key / tenantKey
      role == TENANT_SCOPED:
          tenant_field set    → property_map / <tenant_field> / tenantId
          else                → traversal_path / None / tenantKey / scoping_path
      role missing            → raise UnknownEntityScope (caller decides: refuse vs. fall-through)
    """


def predicate_for_collection(
    collection: str,
    manifest: TenantScopeManifest,
    sharding_profile: dict | None,
) -> TenantPredicateShape | None:
    """Same as predicate_for_entity but keyed off the physical collection name.
    Layer 4 (AQL) consumes this; Layer 3 (Cypher) consumes predicate_for_entity.
    """


def is_bindvar_reference(node: object, *, name: str) -> bool:
    """Plan-walk helper: True iff `node` is a `Reference` to bind variable `name`.
    Used by Layer 5 today and by Layer 4's preflight check for already-injected predicates.
    """


def is_literal_tenant_value(node: object, manifest: TenantScopeManifest) -> bool:
    """True iff `node` is a literal whose value matches a known tenant key
    (i.e. it appears in the `Tenant` collection's `_key` set, sampled at manifest
    build time). Used by Layer 2 / 3 / 4 to reject literal tenant predicates (T2).

    Manifest must carry a `_known_tenant_keys: frozenset[str]` populated at acquire
    time. If unset, default to refusing all literal tenant comparisons (fail-closed).
    """


class UnknownEntityScope(Exception):
    """Raised when a label/collection has no manifest entry. Caller decides
    refuse-vs-pass; the convention is REFUSE for Layer 3, REFUSE for Layer 4
    on a tenant-scoped collection, PASS for satellite/system."""
```

### Constraints

- Pure functions; no DB access, no logging side effects, no global state.
- All five public symbols carry full docstrings. The downstream sub-agents will read these docstrings as their contract.
- `TenantPredicateShape` is the **only** way a predicate is described in the codebase after this lands. The rewriters render the shape into Cypher/AQL fragments using their own emitters.

### Tests (`tests/test_tenant_ast_common.py`)

- `test_predicate_for_entity_global_returns_none` — entity with `role == GLOBAL` → `None`.
- `test_predicate_for_entity_tenant_root` — entity `Tenant` → `style == "property_map"`, `field == "_key"`, `bind_name == "tenantKey"`.
- `test_predicate_for_entity_tenant_scoped_with_denorm` — `field == "TENANT_HEX_ID"`, `bind_name == "tenantId"`.
- `test_predicate_for_entity_tenant_scoped_traversal_only` — entity has no `tenant_field` but has `scoping_path` → `style == "traversal_path"`, `scoping_path` populated.
- `test_predicate_for_entity_unknown_label_raises` — unmapped label → `UnknownEntityScope`.
- `test_is_literal_tenant_value_with_known_key` — literal `'tenant-A-uuid'` matches a known key → `True`.
- `test_is_literal_tenant_value_with_unknown_value` — literal `'banana'` → `False`.
- `test_is_literal_tenant_value_fails_closed_when_keyset_missing` — manifest lacks `_known_tenant_keys` → returns `True` (refusing all literals is the safe default; the test pins this explicitly).

### Acceptance

- New tests pass; `pytest -m "not integration and not tck"` all green.
- `ruff check .` clean.
- `rg "tenantId|TENANT_HEX_ID|tenantKey" arango_cypher/nl2cypher/` shows the bind names appear only via `TENANT_ID_BIND` / `TENANT_KEY_BIND` constants in any file landing in Wave 8a (this is enforced by review, not a script).
- Module's public surface is exactly the six symbols above. Add/remove only with explicit Wave-8a coordination.

### Hand-off to Wave 8a

- MT-2 imports `is_literal_tenant_value` to reject literal-tenant Cypher in the guardrail postcheck.
- MT-3 imports `predicate_for_entity` and `TENANT_ID_BIND` / `TENANT_KEY_BIND` to render Cypher predicates.
- MT-4 imports `predicate_for_collection`, `is_bindvar_reference`, and the bind-name constants to render and detect AQL predicates.
```

---

### Wave 8a — Three parallel sub-agents on sibling branches off `main` (after 8-pre merges)

#### MT-2 — Layer 2 guardrail hardening (~30 LOC + 4 tests)

```
{SHARED CONTEXT BLOCK — paste from agent_prompts.md lines 26–134}

## Your task: MT-2 — Reject literal tenant predicates in LLM-generated Cypher

### Background

Today the Layer 2 guardrail in `arango_cypher/nl2cypher/tenant_guardrail.py::check_tenant_scope` accepts a `TENANT_HEX_ID = '<value>'` inline filter as proof of scope **when the value matches the active tenant**. Once Layer 3 (Wave 8a, MT-3) lands, every literal tenant predicate is rewritten to a bind-var. The guardrail must therefore reject literal tenant predicates **always**, not only when they reference another tenant — this forces the LLM into the bind-var form Layer 3 can rewrite (T2 defense at the LLM layer; PRD §5.2 enhancement #1).

### What to implement

1. In `arango_cypher/nl2cypher/tenant_guardrail.py::_denorm_filter_satisfied` (around line 218), invert the condition:
   - Today: a literal value is **accepted** if it equals the active tenant.
   - After: a literal value is **always rejected** (regardless of its content). Only a bind-var reference to `@tenantId` or `@tenantKey` counts as proof of scope.
   - Use `tenant_ast_common.is_literal_tenant_value` for the literal check; `is_bindvar_reference` for the bind-var check.

2. The `TenantScopeViolation` returned in this case carries `code="LITERAL_TENANT_PREDICATE"` (new code). Existing codes stay.

3. Update `tenant_guardrail.prompt_section` to extend the rules block with one more line:
   ```
   - Tenant identifiers are bind variables (@tenantId, @tenantKey).
     Never compare a tenant column to a literal string.
   ```

### Where to make changes

- `arango_cypher/nl2cypher/tenant_guardrail.py` — `_denorm_filter_satisfied`, `_build_violation`, `prompt_section`.
- `tests/test_tenant_guardrail.py` (extend; if missing, create new).

### Tests to add

- `test_literal_tenant_predicate_rejected_even_when_value_matches` — Cypher with `e.TENANT_HEX_ID = 'tenant-A-uuid'` and active tenant is `tenant-A-uuid` → still rejected with `code="LITERAL_TENANT_PREDICATE"`.
- `test_bindvar_tenant_predicate_accepted` — Cypher with `e.TENANT_HEX_ID = $tenantId` → accepted.
- `test_no_predicate_rejected_unchanged` — pre-existing case; still rejected with the prior code.
- `test_prompt_section_contains_no_literal_rule` — `prompt_section(...)` rendered output contains the new rule line.

### Out of scope

- Do NOT change the manifest, the `analyze_tenant_scope` function, or any of the `prompt_section` formatting beyond the one-line rule addition.
- Do NOT touch any AST rewrite paths — those are MT-3 and MT-4.

### Acceptance criteria

- All new + existing tests green.
- `ruff check .` clean.
- WP-25.5 eval baseline regenerated locally if its `pattern_match` rate moves; otherwise no change to the eval CI.

### Hand-off

None. MT-3 and MT-4 do not consume MT-2's behaviour.
```

---

#### MT-3 — Layer 3 Cypher AST tenant injection (~400 LOC + 20 tests)

```
{SHARED CONTEXT BLOCK — paste from agent_prompts.md lines 26–134}

## Your task: MT-3 — Implement the Cypher AST tenant-injection pass

### Background

`multitenant_prd.md` §6. Every `MATCH (var:Label)` over a TENANT_SCOPED entity must be augmented with `{tenant_field: $tenantId}` (cheap path) or rewritten as a path from `Tenant` (traversal-only path). The rewritten Cypher is what the user sees in the UI — it is the audit-friendly representation.

### What to implement

1. **New module `arango_cypher/nl2cypher/tenant_ast_cypher.py`:**

```python
def inject_tenant_scope(
    *,
    cypher: str,
    parse_tree: Any,  # ANTLR4 ParseTree from arango_cypher.parser.parse_cypher
    manifest: TenantScopeManifest,
    tenant_id: str,
    tenant_key: str,
) -> tuple[str, list[str]]:
    """Return (rewritten_cypher_text, list_of_human_readable_changes).

    The second element is rendered into the UI 'rewrites' panel so the user sees
    exactly what was added. Empty list ⇒ no rewrite needed (all entities GLOBAL).
    """
```

2. **Visitor pattern.** Subclass the ANTLR4 `CypherVisitor` (already in `arango_cypher/_antlr/`). Implement these visit overrides:
   - `visitOC_NodePattern`: read `(var:Label {props})`. Look up `predicate_for_entity(Label, manifest)`. Apply per `multitenant_prd.md` §6.2:
     - GLOBAL → no change.
     - TENANT_ROOT → ensure property map carries `_key: $tenantKey`. If a literal `_key:` is present and differs, **REJECT** the query.
     - TENANT_SCOPED with denorm → merge `{<denormField>: $tenantId}` into the property map.
     - TENANT_SCOPED traversal-only → mark for path-promotion (post-process, see step 3).
     - Unknown label → REJECT (`UnknownEntityScope`).
   - `visitOC_Where`: walk predicates; for any `x.<field> OP literal` where `<field>` matches a tenant field on `x`'s entity, REJECT (`LITERAL_TENANT_PREDICATE`).
   - `visitOC_Unwind`: if the unwound list contains literals matching `_known_tenant_keys`, REJECT.

3. **Path promotion** (the hard part). For every traversal-only TENANT_SCOPED node pattern, replace the pattern in-place with the scoping-path expansion:
   ```
   (e:Employee)  →  (t:Tenant {_key: $tenantKey})-[:TENANTUSERTENANT]->(:TenantUser)<-[:GSUITEUSERTENANTUSER]-(e:Employee)
   ```
   The `scoping_path` comes from `EntityScope.scoping_path` (Wave 7, Part 2). If multiple node patterns in the same MATCH share the same `Tenant` anchor, deduplicate to one `(t:Tenant {_key: $tenantKey})` and reuse it as the start vertex.

4. **Output rendering.** Re-emit the Cypher from the modified parse tree. ANTLR's `getText()` is byte-faithful for non-modified subtrees; for modified ones, render the new property map / inserted path manually. Pin the byte-output of canonical examples in golden tests (cases below).

5. **Integration.** Wire `inject_tenant_scope` into `arango_cypher/service/routes/cypher.py::translate_endpoint` between `parse_cypher` and `translate`. Output:
   - The rewritten Cypher text is what the UI displays in the Cypher pane.
   - The "changes" list is exposed as a new top-level response field `tenantRewrites: list[str]` so the UI can show them in a "Tenant scoping applied" subsection.
   - Layer 1 must be active for this to fire; in workbench mode, only fire when `tenantId` is supplied on the request.

### Where to make changes

- `arango_cypher/nl2cypher/tenant_ast_cypher.py` — new.
- `arango_cypher/service/routes/cypher.py` — wire `inject_tenant_scope`.
- `arango_cypher/service/models.py` — `tenantRewrites: list[str]` field on the translate-response model.
- `ui/src/components/CypherEditor.tsx` (or the appropriate panel) — render the `tenantRewrites` list as a small annotation strip below the editor (read-only). Match existing annotation styling.
- `tests/test_tenant_ast_cypher.py` — new (20 cases below).

### Tests to add (`tests/test_tenant_ast_cypher.py`)

Cover the §6.3 examples plus edge cases. Each test asserts both the rewritten Cypher text and the `changes` list:

- `test_global_entity_no_change` — `MATCH (c:Country) RETURN c` → unchanged; empty changes list.
- `test_tenant_scoped_with_denorm_inline_property` — `MATCH (e:Employee)` → `MATCH (e:Employee {TENANT_HEX_ID: $tenantId})`.
- `test_tenant_root_inline_key` — `MATCH (t:Tenant)` → `MATCH (t:Tenant {_key: $tenantKey})`.
- `test_existing_property_map_merged` — `MATCH (e:Employee {name: 'Alice'})` → `MATCH (e:Employee {name: 'Alice', TENANT_HEX_ID: $tenantId})`.
- `test_existing_tenant_property_with_bindvar_kept` — `MATCH (e:Employee {TENANT_HEX_ID: $tenantId})` → unchanged (idempotent).
- `test_existing_tenant_property_with_literal_rejected` — `MATCH (e:Employee {TENANT_HEX_ID: 'tenant-B-uuid'})` → REJECT.
- `test_traversal_only_path_promotion` — Employee has no denorm field; rewrite as the scoping-path pattern.
- `test_traversal_only_dedupes_tenant_anchor` — two patterns over `(e:Employee)` and `(d:Device)` both lacking denorm; one shared `(t:Tenant {_key: $tenantKey})` anchor in the rewrite.
- `test_relationship_pattern_unchanged` — Layer 3 does not inject on relationships; only on node patterns.
- `test_optional_match_predicate_injected` — `OPTIONAL MATCH (e:Employee)` → predicate injected; OPTIONAL semantics preserved.
- `test_merge_predicate_injected` — `MERGE (e:Employee {name: 'Alice'})` → predicate injected on the merge pattern (and on ON CREATE / ON MATCH SET if those touch tenant fields).
- `test_create_predicate_injected_on_node` — `CREATE (e:Employee {name: 'Alice'})` → `CREATE (e:Employee {name: 'Alice', TENANT_HEX_ID: $tenantId})`.
- `test_unknown_label_rejected` — `MATCH (x:UnknownLabel)` → REJECT, `UnknownEntityScope`.
- `test_where_literal_tenant_predicate_rejected` — `WHERE e.TENANT_HEX_ID = 'tenant-B-uuid'` → REJECT.
- `test_where_bindvar_tenant_predicate_kept` — `WHERE e.TENANT_HEX_ID = $tenantId` → unchanged (idempotent).
- `test_unwind_known_tenant_literal_rejected` — `UNWIND ['tenant-A-uuid', 'tenant-B-uuid'] AS t` where both are known → REJECT.
- `test_changes_list_human_readable` — assert each entry in `changes` reads like `"Added TENANT_HEX_ID = $tenantId to (e:Employee)"`.
- `test_zero_shot_byte_identical_for_global_only_query` — query referencing only GLOBAL entities → `inject_tenant_scope` returns the original Cypher byte-for-byte.
- `test_admin_session_in_workbench_mode_no_rewrite` — when `session.is_admin and ARANGO_CYPHER_WORKBENCH=1`, the pass is a no-op (the admin path in Wave 9 will still run Layer 5).
- `test_round_trip_through_layer_5_validator` — rewritten Cypher → transpile → validate_plan (Wave 7's Layer 5) → accepted. **This is the critical contract test that pins Layer 3 / Layer 5 cohesion.**

### Out of scope

- Do NOT modify Layer 5 (`tenant_plan_validator.py`) — Layer 3 must produce predicates Layer 5 already accepts.
- Do NOT modify the Cypher grammar in `grammar/Cypher.g4`.
- Do NOT touch `tenant_ast_aql.py` (MT-4) or `tenant_guardrail.py` (MT-2).

### Acceptance criteria

- All new + existing tests green.
- `ruff check .` clean.
- `cd ui && npx tsc --noEmit -p tsconfig.app.json && npm run build` clean.
- The `test_round_trip_through_layer_5_validator` case in particular passes — this proves Layers 3 and 5 are coherent.
- Manual smoke (workbench): connect with `tenantId="tenant-A-uuid"`; type `MATCH (e:Employee) RETURN e`; click Translate; the Cypher editor (or annotation strip) shows the rewritten form and the changes list "Added TENANT_HEX_ID = $tenantId to (e:Employee)".

### Hand-off

MT-4 (AQL) operates on the *output* of the existing Cypher→AQL transpiler, which already consumes the rewritten Cypher from MT-3 transparently — so the Cypher path's tenant predicates flow into AQL via the transpiler. MT-4 must still inject independently for the NL→AQL direct path and the raw-AQL path.
```

---

#### MT-4 — Layer 4 AQL AST tenant injection (~500 LOC + 25 tests)

```
{SHARED CONTEXT BLOCK — paste from agent_prompts.md lines 26–134}

## Your task: MT-4 — Implement the AQL AST tenant-injection pass

### Background

`multitenant_prd.md` §7. Layer 4 is the only defense for the NL→AQL direct path (`/nl2aql`) and for hand-submitted AQL via `/execute-aql` (when that endpoint exists). It is also a defense-in-depth pass on the Cypher→AQL output: if MT-3 misses a case or the transpiler drops a predicate, Layer 4 catches it.

### What to implement

1. **New module `arango_cypher/tenant_ast_aql.py`.**

ArangoDB's AQL has no published Python AST library. Two viable approaches:

   a. **Round-trip parse via the server.** `db.aql.explain(aql, ...)` returns a structured plan. The plan contains every collection access, every traversal, every filter. We rewrite by mutating the *AQL text* using a regex-and-position approach informed by the plan — i.e., locate `FOR x IN <coll>` statements via the plan's nodes (which carry source positions), then splice in `FILTER x.<tenant_field> == @tenantId` at the right textual position.

   b. **Local mini-parser.** A focused recursive-descent parser that handles only the subset our transpiler emits + the subset NL→AQL emits. Faster but ~800 LOC.

   **Pick approach (a).** It piggybacks on the EXPLAIN call Layer 5 already pays for, and the rewriter only needs to handle textual splicing. Document the choice in the module docstring with a one-line note about the trade-off.

2. **Public entry point:**

```python
def inject_tenant_scope(
    *,
    db: StandardDatabase,
    aql: str,
    bind_vars: dict[str, Any],
    manifest: TenantScopeManifest,
    sharding_profile: dict | None,
    tenant_id: str,
    tenant_key: str,
) -> tuple[str, dict[str, Any], list[str]]:
    """Return (rewritten_aql, augmented_bind_vars, list_of_human_readable_changes).

    The augmented bind_vars include any new bind values introduced by the rewrite
    (for collection-name binds @@coll, etc.) — never literal tenant values.
    """
```

3. **Algorithm.** Implement `multitenant_prd.md` §7.2 step-by-step. Per plan node:
   - `EnumerateCollectionNode` over a TENANT_SCOPED collection: if the next FILTER is already `<var>.<tenant_field> == @tenantId` (use `is_bindvar_reference` from `tenant_ast_common`), no-op. Otherwise splice `FILTER <var>.<tenant_field> == @tenantId` immediately after the `FOR <var> IN <coll>` line.
   - `EnumerateCollectionNode` over a TENANT_SCOPED collection lacking both `smartGraphAttribute` and `denormField` → REJECT (`UNCONSTRAINED_COLLECTION_ACCESS`).
   - `TraversalNode` whose graph is not disjoint-smartgraph and whose `options.prune` does not reference `@tenantId`: rewrite to add `OPTIONS { prune: v.<denormField> != @tenantId }` (or the smartGraphAttribute equivalent).
   - `SubqueryNode`: recurse on the subquery's plan.
   - `FunctionCallNode` referencing a collection name (`LENGTH(Employee)` style): rewrite to `LENGTH(FOR e IN Employee FILTER e.<tenant_field> == @tenantId RETURN 1)`.
   - `COLLECT` / `AGGREGATE` over a tenant-scoped enumeration: rely on the enclosing FOR's filter (recursive structure handles this); REJECT only if the enclosing FOR is itself unconstrained.

4. **Bind-var hygiene.** This pass never adds literal tenant values to `bind_vars` — only references to the existing `@tenantId` / `@tenantKey`. New collection-name binds (`@@<coll>`) for rewritten function calls are added with deterministic names (`@@_tenant_subq_<n>`).

5. **Idempotence.** Running `inject_tenant_scope` twice on the same input must produce byte-identical output the second time. Pin with a unit test.

6. **Integration:**
   - `arango_cypher/service/routes/cypher.py::translate_endpoint`: after `translate(cypher, ...)` produces AQL, run `inject_tenant_scope` on the AQL before passing to `safe_execute`. (Layer 5 will then validate.)
   - `arango_cypher/service/routes/nl.py::nl2aql_endpoint`: same; this is the NL→AQL direct path.
   - If a `/execute-aql` endpoint exists, wire it there too. If not, leave a TODO and surface a follow-up issue.

### Where to make changes

- `arango_cypher/tenant_ast_aql.py` — new.
- `arango_cypher/service/routes/{cypher,nl}.py` — wire `inject_tenant_scope` between transpile and execute.
- `arango_cypher/service/models.py` — extend the response model with `tenantRewritesAql: list[str]`.
- `ui/src/components/AqlEditor.tsx` (or the appropriate panel) — render the `tenantRewritesAql` annotations alongside MT-3's `tenantRewrites`.
- `tests/test_tenant_ast_aql.py` — new (25 cases).

### Tests to add (`tests/test_tenant_ast_aql.py`)

Each test mocks `db.aql.explain` to return a hand-crafted plan dict; the rewriter consumes the plan + AQL text and produces the rewrite. This keeps tests offline.

- §7.3 example case (Asset/Product) → assert exact rewritten AQL and that Layer 5 accepts the result.
- `test_satellite_only_no_change` — `FOR c IN Country RETURN c` (Country is satellite) → unchanged.
- `test_tenant_scoped_collection_filter_injected` — `FOR e IN Employee RETURN e` → `FOR e IN Employee FILTER e.TENANT_HEX_ID == @tenantId RETURN e`.
- `test_existing_filter_kept_no_dup` — `FOR e IN Employee FILTER e.TENANT_HEX_ID == @tenantId RETURN e` → unchanged (idempotent).
- `test_collection_lacking_both_attrs_rejected` — manifest entry has neither smartGraphAttribute nor denormField → REJECT.
- `test_traversal_outbound_disjoint_no_prune_no_rewrite_needed` — traversal on a disjoint-smartgraph; storage enforces; pass through.
- `test_traversal_any_with_satellite_prune_added` — traversal mixes satellite and tenant-scoped; `OPTIONS { prune: v.TENANT_HEX_ID != @tenantId }` added.
- `test_subquery_recursion` — `LET x = (FOR e IN Employee RETURN e)` → subquery rewritten with FILTER injected.
- `test_count_subquery_rewritten` — `RETURN COUNT(FOR e IN Employee RETURN 1)` → inner FOR gets the filter.
- `test_function_call_collection_arg` — `LENGTH(Employee)` → `LENGTH(FOR e IN Employee FILTER e.TENANT_HEX_ID == @tenantId RETURN 1)`.
- `test_idempotent_double_pass` — `inject(inject(aql)) == inject(aql)` byte-for-byte.
- `test_no_literal_tenant_in_bind_vars` — augmented `bind_vars` contains no value matching any `_known_tenant_keys` entry; only references to `@tenantId` / `@tenantKey`.
- `test_changes_list_human_readable` — entries read like `"Added FILTER e.TENANT_HEX_ID == @tenantId after FOR e IN Employee"`.
- `test_round_trip_through_layer_5_validator` — rewritten AQL passes Layer 5. **Critical contract test.**
- `test_admin_in_workbench_mode_no_op` — admin session + workbench mode → no rewrite.
- ...plus the remaining §8.6 traversal / aggregation / subquery cases mirrored at the AQL layer.

### Out of scope

- Do NOT modify the Cypher transpiler (`arango_cypher/_translate_v0/`).
- Do NOT modify Layer 5 (`tenant_plan_validator.py`).
- Do NOT touch the Cypher AST pass (MT-3) or the guardrail (MT-2).
- Do NOT add a local AQL parser. Round-trip via `EXPLAIN` per the chosen approach (a).

### Acceptance criteria

- All new + existing tests green.
- `ruff check .` clean.
- UI typechecks + builds clean.
- `test_round_trip_through_layer_5_validator` passes — Layers 4 and 5 are coherent.
- Integration smoke: NL→AQL direct path on a Movies-style fixture produces AQL that Layer 5 accepts; raw-AQL submission of `FOR e IN Employee RETURN e` is rewritten + executes scoped to the session tenant.

### Hand-off

None. Wave 9 (MT-6 plan-shape LRU) keys off `(rewritten_aql_hash, manifest_hash)` so Layer 4 must produce deterministic output for identical inputs — pinned by `test_idempotent_double_pass`.
```

---

## Wave 9 — Multi-tenant Phase 3 (1 agent, sequential, ~1 week)

**Goal.** Production-readiness. Plan-shape LRU to amortise Layer 5's EXPLAIN cost; admin cross-tenant bypass with audit log.

**Why one agent.** MT-6 (~80 LOC, LRU keying) and MT-7 (~150 LOC, admin flag + audit stream) share the same surface (`tenant_plan_validator.py` + `safe_execute` + audit logger) and have a load-bearing interaction (admin bypass must still log via the audit stream the LRU bypasses on cache hit). Single agent eliminates that coordination.

**PRD source-of-truth.** `multitenant_prd.md` §8.4 (cost / LRU), §10 (admin bypass), §12.4 (performance budget).

---

### Wave 9 — MT-6 + MT-7: plan-shape LRU + admin cross-tenant bypass + audit stream

```
{SHARED CONTEXT BLOCK — paste from agent_prompts.md lines 26–134}

## Your task: Wave 9 — Add a plan-shape LRU around Layer 5; add admin cross-tenant bypass with a separate audit stream

### Background

`multitenant_prd.md` §8.4: every execute pays for one EXPLAIN round-trip (5–20 ms). Same query shape with different bind-var values = same plan = same safety verdict. Cache the verdict by `(aql_hash, mapping_fingerprint)` and skip the EXPLAIN on hit.

`multitenant_prd.md` §10: operator workflows (health reports, billing rollups) legitimately span tenants. Admin sessions issue queries with explicit `cross_tenant=True`. The structural Layer 5 checks still run; only the "must filter by `@tenantId`" rule is relaxed. Every bypass logs to a separate audit stream with a required reason.

### What to implement

#### Part 1 — Plan-shape LRU (MT-6)

1. New helper in `arango_cypher/tenant_plan_validator.py`:

```python
from collections import OrderedDict
from hashlib import sha256


@dataclass(frozen=True)
class PlanCertification:
    aql_hash: str
    mapping_fingerprint: str
    verdict: str  # "ok" or "violation"
    violation_code: str | None
    plan_digest: str


class PlanLRU:
    def __init__(self, capacity: int = 1024):
        self._cache: OrderedDict[tuple[str, str], PlanCertification] = OrderedDict()
        self._capacity = capacity
        self._hits = 0
        self._misses = 0

    def get(self, key: tuple[str, str]) -> PlanCertification | None: ...
    def put(self, key: tuple[str, str], cert: PlanCertification) -> None: ...
    def metrics(self) -> dict: ...
```

2. `validate_plan` opens with a cache check:
   ```python
   key = (sha256(aql.encode() + json.dumps(sorted(bind_vars.items()), default=str).encode()).hexdigest(),
          manifest.fingerprint)
   cert = _plan_lru.get(key)
   if cert and cert.verdict == "ok":
       _audit_ok(session, cert, source="lru_hit")
       return
   if cert and cert.verdict == "violation":
       raise TenantScopeViolation(code=cert.violation_code, ...)
   # cache miss — call db.aql.explain, walk plan, store cert
   ```

3. Cache invalidation: `manifest.fingerprint` already changes on schema-shape changes (Wave 4m landed this). Reuse it. Add a `tenant_plan_validator.invalidate_cache()` and `GET /schema/plan-cache/metrics` endpoint exposing `{capacity, size, hits, misses, hit_rate}`.

4. Capacity. Default 1024. Override via `ARANGO_CYPHER_PLAN_LRU_CAPACITY` env var.

5. **The cache stores violations too**, so a query shape that was unsafe last time is rejected fast. This is intentional. The cache is invalidated only on manifest-fingerprint change, not on session change — a session-level violation is independent of cache state.

#### Part 2 — Admin cross-tenant bypass (MT-7)

1. **`cross_tenant: bool = False` flag** on every endpoint that runs `safe_execute`. Plumb as a query param (`?cross_tenant=1`) or a request-body field, consistent with existing patterns. The flag is silently ignored when `not session.is_admin`.

2. **Bypass reason is required.** When `cross_tenant=True`, the request must also carry a `bypass_reason: str` (non-empty). Reject with HTTP 400 if missing. The reason is logged verbatim into the audit stream.

3. **Layer 5 relaxation.** When `session.is_admin and cross_tenant=True`:
   - The bind-var sanity check (`bind_vars["tenantId"] != session.tenant_id`) is skipped.
   - The "TENANT_SCOPED collection scanned without `@tenantId`" rule is skipped.
   - All **other** structural checks still run: literal tenant predicates are still rejected (T2), unparseable plans are still refused, satellite-traversal escapes are still caught.

4. **Separate audit stream.** New module `arango_cypher/tenant_audit.py`:
   ```python
   def audit_tenant_scope_pass(session, plan_digest, source: str) -> None: ...
   def audit_tenant_scope_violation(session, violation, plan_digest, aql) -> None: ...
   def audit_admin_bypass(session, plan_digest, reason: str, aql) -> None: ...
   ```
   All three write to a `tenant_audit` named logger configured in `arango_cypher/service/observability.py` to use a separate handler (file or stdout, deployment-dependent). Format: structured JSON, one record per line.

5. **Rate limiting.** Admin bypass requests are rate-limited to 60/minute per session. Reuse existing rate-limiter infra in `arango_cypher/service/security.py` (the project has one — confirm by grepping; if not, lift the pattern from the existing per-IP limiter and key by session token).

6. **UI.** Admin sessions render a small "Cross-tenant" toggle in the connection panel. Toggling on opens a modal that requires a bypass-reason text entry before any query can execute. The toggle and the reason persist per session, not across sessions. Match the existing tenant-context input styling.

### Where to make changes

- `arango_cypher/tenant_plan_validator.py` — `PlanLRU`, `PlanCertification`, cache wiring in `validate_plan`.
- `arango_cypher/tenant_audit.py` — new.
- `arango_cypher/service/observability.py` — register `tenant_audit` logger with separate handler.
- `arango_cypher/service/routes/{cypher,nl,schema}.py` — add `cross_tenant` + `bypass_reason` plumbing; new `GET /schema/plan-cache/metrics` endpoint.
- `arango_cypher/service/security.py` — rate-limit admin bypass.
- `arango_query_core/exec.py::safe_execute` — pass `cross_tenant` through to `validate_plan`.
- `ui/src/components/ConnectionDialog.tsx` (or equivalent) — admin cross-tenant toggle + reason modal.
- `tests/test_plan_lru.py` — new (~6 tests).
- `tests/test_admin_cross_tenant.py` — new (~10 tests).

### Tests to add

#### `tests/test_plan_lru.py`

- `test_lru_get_miss_returns_none` — empty cache → `None`.
- `test_lru_put_then_get_returns_cert` — round-trip a `PlanCertification`.
- `test_lru_capacity_evicts_oldest` — capacity 2, put 3 entries, oldest evicted.
- `test_validate_plan_uses_cache_on_second_call_with_same_aql` — mock `db.aql.explain`; assert called exactly once across two `validate_plan` calls with identical aql + bind_vars.
- `test_validate_plan_caches_violations_too` — first call raises; second call raises faster without an explain mock fire.
- `test_metrics_endpoint_returns_hit_rate` — after 1 miss + 2 hits, `/schema/plan-cache/metrics` returns `{hits: 2, misses: 1, hit_rate: 0.667}`.
- `test_cache_invalidates_on_manifest_fingerprint_change` — same aql, same bind_vars, but a new manifest fingerprint → cache miss.

#### `tests/test_admin_cross_tenant.py`

- `test_non_admin_session_with_flag_is_ignored` — non-admin sets `cross_tenant=1`; flag is silently dropped; the standard refusal applies.
- `test_admin_session_without_flag_still_refused` — admin session with no flag is treated like any tenant user; `MATCH (e:Employee) RETURN e` still rewritten + filtered.
- `test_admin_with_flag_and_reason_bypasses_tenant_filter` — admin + `cross_tenant=1` + `bypass_reason="quarterly billing"`; query executes across tenants; result has rows from multiple tenants.
- `test_admin_with_flag_without_reason_400` — missing or empty `bypass_reason` → HTTP 400.
- `test_admin_bypass_still_blocks_literal_tenant_predicate` — even with `cross_tenant=1`, literal `TENANT_HEX_ID = 'tenant-B-uuid'` is rejected (Layer 3/MT-2 still fires).
- `test_admin_bypass_audit_log_written` — assert `tenant_audit.json` contains exactly one record matching the request, with `reason` and `session.token[:8]`.
- `test_admin_bypass_rate_limited` — 61 requests in 60 s → the 61st returns HTTP 429.
- `test_admin_bypass_logs_reason_verbatim` — bypass_reason with multibyte chars or quotes round-trips through the audit log unchanged.
- `test_admin_bypass_preserves_layer_5_structural_checks` — admin + `cross_tenant=1` against an unparseable plan → still refused.
- `test_audit_log_separate_from_main_log` — main app logger does not receive the bypass record; `tenant_audit` logger does.

### Out of scope

- Do NOT change Layer 3 / Layer 4 / `safe_execute`'s spread-order semantics.
- Do NOT introduce a distributed cache. Per-process LRU is sufficient.
- Do NOT change the existing rate-limiter infra; reuse it.

### Acceptance criteria

- All new + existing tests green.
- `ruff check .` clean.
- UI typechecks + builds clean.
- `RUN_INTEGRATION=1 RUN_MULTITENANT=1 pytest tests/integration/test_multitenant_phase3.py` — admin can issue a cross-tenant rollup; non-admin cannot; LRU shows hit-rate ≥ 80% on a steady-state workload of 100 repeated queries.
- `multitenant_prd.md` §8.4 budget check: P95 of `validate_plan` itself (cache miss case) ≤ 30 ms on the dev box.

### Hand-off

None. Wave 9 closes Phase 3.
```

---

## Wave 10 — MT-8 standing red-team corpus (1 agent, ongoing)

**Goal.** Every attempted escape — discovered, hand-crafted, or hypothesized — becomes a permanent test case the validator must refuse. The corpus is a test oracle: adding an attempted escape is how a new defense makes it into the product.

**Why standing.** MT-8 is not a one-shot WP. Each Wave 7/8/9 milestone adds 5–10 entries; each subsequent security review or pen-test produces more. The agent runs whenever a new escape is reported — typically days, not weeks, of work.

**PRD source-of-truth.** `multitenant_prd.md` §12.3.

---

### Wave 10 — MT-8: red-team corpus + harness

```
{SHARED CONTEXT BLOCK — paste from agent_prompts.md lines 26–134}

## Your task: Wave 10 — Build the standing red-team corpus and harness

### Goal

Create `tests/redteam/` as a permanent home for attempted-escape cases. Every entry is an NL prompt OR a hand-crafted AQL submission with an expected outcome (`refuse` or `pass-with-tenant-predicate`) and the expected violation code or injected predicate.

### What to implement

1. New directory `tests/redteam/`:
   - `corpus.yml` — the test-case file.
   - `runner.py` — case-by-case execution against the full pipeline (Wave 7 + Wave 8 + Wave 9).
   - `README.md` — how to add a case; threat-classification crib sheet.
   - `__init__.py`.

2. **Corpus format:**
   ```yaml
   version: 1
   cases:
     - id: rt001
       category: T1_underconstraint
       input_kind: nl                # nl | aql | cypher
       input: "list all employees"
       expected: rewrite              # rewrite | refuse | pass
       expected_predicate: "TENANT_HEX_ID == @tenantId"
       layer_expected: 3              # 2 | 3 | 4 | 5
       notes: |
         The simplest underconstraint case. LLM must ask for tenant filter
         (Layer 2 prompts), Layer 3 rewrites, Layer 5 confirms.
     - id: rt002
       category: T2_injection
       input_kind: nl
       input: "list all employees including in tenant B"
       expected: refuse
       expected_violation_code: LITERAL_TENANT_PREDICATE
       layer_expected: 2
   ```

3. **Initial corpus** — seed from `multitenant_prd.md` §12.3 (8 entries) plus the layer-test cases from Waves 7/8 and any additional cases from the security review that motivated the PRD. Aim for ~30 entries by Wave 10's first pass.

4. **Runner.** `pytest tests/redteam/test_redteam_corpus.py` — auto-discovered. For each case:
   - For `input_kind: nl`: run through `/nl2cypher` (or `/nl2aql` if appropriate) end-to-end with the full pipeline.
   - For `input_kind: cypher`: run through `/translate` then `/execute`.
   - For `input_kind: aql`: run through `/execute-aql` (when that endpoint lands; mark cases as `pending` until then).
   - Assert the outcome matches `expected`, and that the layer that produced the outcome matches `layer_expected` (read from the audit log).
   - For `expected: rewrite`, assert `expected_predicate` is present in the rewritten output.

5. **Categorization.** Eight threat classes from `multitenant_prd.md` §1.1. Every case is tagged with exactly one. The runner emits a per-class summary at the end of the run.

6. **CI integration.** Run on every PR via `pytest tests/redteam/`. Gated behind `RUN_INTEGRATION=1 RUN_MULTITENANT=1` because the corpus needs a live two-tenant fixture.

7. **README guidance for adding cases.** Document the workflow:
   - Reproduce the escape locally.
   - Add the case to `corpus.yml` with the expected outcome.
   - If the escape currently succeeds (i.e. the pipeline is broken), mark the case `expected_at_landing: refuse` and `current: pass` to make the gap visible without breaking CI; file a follow-up issue and reference the case id.
   - If the escape is correctly refused, the case is the regression guard going forward.

### Tests to add

The corpus *is* the test suite. The unit tests for the runner itself are minimal:

- `test_runner_loads_yaml` — corpus.yml parses; required fields present.
- `test_runner_categorizes_cases` — all cases tagged with a known threat class.
- `test_runner_layer_assertion` — runner correctly reads the layer that emitted the outcome (parse the audit log).

### Out of scope

- Adding to the corpus is the standing work; the runner is one-shot. Do not mix new defenses into this WP — the runner is purely the test harness.

### Acceptance criteria

- `tests/redteam/corpus.yml` has ≥ 30 entries spanning all eight threat classes.
- `RUN_INTEGRATION=1 RUN_MULTITENANT=1 pytest tests/redteam/` is green.
- Per-class summary at the end of a run shows ≥ 1 entry per class.
- The README walks a new contributor through adding a case in under 5 minutes.

### Hand-off

Standing — every subsequent multi-tenant change adds cases here.
```

---

## Wave 11 — v0.4+ residual tail (1 agent, sequential, ~1–2 weeks)

**Goal.** Close the small disparate items in `python_prd.md` §10 v0.4+ that were not absorbed into earlier waves. None individually justifies multi-agent overhead; together they fit one focused pass.

**PRD source-of-truth.** [`python_prd.md`](./python_prd.md) §10 v0.2 row (RETURN DISTINCT, LIMIT/SKIP exprs), §10 v0.3 (native shortestPath, VCI advisory), §10 v0.4+ (index hint emission).

---

### Wave 11 — v0.4+ residual tail: language polish + optimization advisory

```
{SHARED CONTEXT BLOCK — paste from agent_prompts.md lines 26–134}

## Your task: Wave 11 — Close the v0.4+ residual tail

### Goal

Five small features that remain `Partial` or `Not started` in `python_prd.md` §10. Each is < 1 day; together ~1 week. Sequential single agent.

### Features

#### Tail-1 — `RETURN DISTINCT` multi-column

Today `RETURN DISTINCT a.name` works; `RETURN DISTINCT a.name, b.name` falls through to single-item handling. Extend `_compile_return` in `arango_cypher/_translate_v0/core.py` to lower multi-item DISTINCT to `COLLECT a_name = a.name, b_name = b.name` followed by `RETURN {a_name, b_name}` (matching the existing `WITH DISTINCT` pattern).

Tests: extend `tests/fixtures/cases/return_distinct.yml` (or create) with C400–C403:
- C400: `RETURN DISTINCT a.name, b.name` against PG.
- C401: `RETURN DISTINCT a.name, b.name, c.age` against LPG.
- C402: `RETURN DISTINCT n` (whole-node) — already works; pin as regression.
- C403: `RETURN DISTINCT n.name AS who, n.age AS age_at_test` — alias preservation.

#### Tail-2 — `LIMIT` / `SKIP` with parameter and expression

Today `LIMIT 5` works; `LIMIT $count` and `LIMIT 2 + 3` fall through. Extend `_parse_skip_limit` in `arango_cypher/_translate_v0/core.py` to accept any expression — bind it directly into the AQL `LIMIT` clause. The grammar already produces an expression node; the issue is the integer-only check. Replace the check with a "must be reducible to a non-negative integer at runtime" assertion (delegated to AQL — emit the expression and let ArangoDB validate).

Tests: extend `tests/fixtures/cases/limit_skip.yml`:
- C410: `LIMIT $count` with a bind var.
- C411: `LIMIT 2 + 3` with arithmetic.
- C412: `SKIP $offset LIMIT $count`.
- C413: Negative-literal LIMIT is still rejected at parse time (or at AQL time; pin whichever the implementation chooses).

#### Tail-3 — Native `shortestPath()` syntax

`CALL arango.shortest_path()` works today; openCypher's native `shortestPath((a)-[*..6]-(b))` does not parse because the grammar is outdated. Two-part fix:

- (a) Update `grammar/Cypher.g4` to admit the `shortestPath` and `allShortestPaths` keyword forms. Regenerate the ANTLR Python files into `arango_cypher/_antlr/` (use the existing build script — confirm name; commonly `scripts/build_antlr.sh`).
- (b) Lower `shortestPath((a)-[*..6]-(b))` to a `K_SHORTEST_PATHS` traversal in `_translate_v0/core.py`, reusing the existing `arango.shortest_path` extension's lowering. `allShortestPaths` lowers to `K_SHORTEST_PATHS` with `OPTIONS { allDifferent: false }`.

Tests: `tests/fixtures/cases/shortest_path_native.yml`:
- C420: `MATCH p = shortestPath((a:Person {name: 'Alice'})-[*..6]-(b:Person {name: 'Bob'})) RETURN p`.
- C421: `MATCH p = allShortestPaths((a)-[*..3]-(b)) RETURN p`.
- C422: `length(shortestPath(...))` aggregation.
- Both should produce AQL byte-identical to the corresponding `CALL arango.shortest_path(...)` form for the same inputs.

#### Tail-4 — Index hint emission from `PropertyInfo.indexed`

The `MappingResolver` carries `PropertyInfo.indexed: bool` and `PropertyInfo.index_kind: str | None` (from Wave-3 schema-acquire). The transpiler does not currently emit `OPTIONS { indexHint: "<idx_name>" }` even when an indexed property is filtered. Add it: in `_compile_node_pattern_filter` (or wherever node-property filters are emitted), look up the property's `IndexInfo` via `MappingResolver.resolve_indexes(label, prop)`; if a non-empty hint is available, render `OPTIONS { indexHint: "<idx_name>", forceIndexHint: false }`.

`forceIndexHint: false` is intentional — we hint, we don't force. The query optimizer can still ignore the hint if it has a better plan.

Tests: extend `tests/fixtures/cases/index_hint.yml`:
- C430: filter on a hash-indexed property emits the hint.
- C431: filter on a non-indexed property emits no hint (regression pin).
- C432: VCI-indexed property emits the hint with the VCI's name.
- C433: composite index (multi-property) — emit the hint when all filtered fields are in the index; otherwise omit.

#### Tail-5 — VCI / index advisory polish in `doctor` and UI

Today the transpiler emits a warning when a generic-with-type query lacks a VCI; `doctor` and the UI do not surface it. Two parts:

- (a) `arango_cypher/cli.py::doctor` reads `MappingResolver.resolve_indexes()` for every entity in the bundle; entities of `style=LABEL` (LPG-naked) without a VCI on `typeField` produce an advisory line `"⚠ Performance: VCI on typeField recommended for <collection>."`. Output goes to the existing `doctor` rich console output.
- (b) UI: render the same advisories in a new "Schema advisories" panel (collapsible, default-closed) below the existing schema graph. Read from a new `GET /schema/advisories` endpoint that calls `resolve_indexes()` and returns the list.

Tests:
- `tests/test_cli_doctor.py` (extend or create): mock a bundle with one missing-VCI entity; assert the advisory line appears in `doctor` output.
- `tests/test_service_schema_advisories.py` (new): the new endpoint returns the advisories list with the expected shape.
- UI unit test for the panel render.

### Where to make changes

- `arango_cypher/_translate_v0/core.py` — Tail-1, Tail-2, Tail-3 lowering, Tail-4 hint emission.
- `grammar/Cypher.g4` + `arango_cypher/_antlr/` — Tail-3 grammar update + regen.
- `arango_cypher/cli.py` — Tail-5 doctor advisory.
- `arango_cypher/service/routes/schema.py` — Tail-5 `/schema/advisories` endpoint.
- `ui/src/components/SchemaAdvisoriesPanel.tsx` — Tail-5 new panel.
- `tests/fixtures/cases/{return_distinct,limit_skip,shortest_path_native,index_hint}.yml` — extend or new.
- `tests/test_cli_doctor.py`, `tests/test_service_schema_advisories.py` — extend or new.

### Out of scope

- Do NOT touch any multi-tenant code (that's Waves 7–10).
- Do NOT regenerate ANTLR files for any reason other than Tail-3.
- Do NOT change the VCI warning emission in `_translate_v0` itself; only surface it.

### Acceptance criteria

- `pytest -m "not integration and not tck"` — all green.
- `ruff check .` clean.
- UI typechecks + builds clean.
- `python_prd.md` §10 v0.2 row updated: "RETURN DISTINCT (multi-column), LIMIT/SKIP with expressions" → **Done**.
- `python_prd.md` §10 v0.3 row updated: "VCI/index advisory polish" → **Done**.
- `python_prd.md` §10 v0.4+ row updated: "Index hint emission" → **Done**.

### Hand-off

None. Wave 11 closes the v0.4+ residual tail.
```

---

## Orchestrator checklist

Use this checklist when running Waves 7–11.

### Pre-flight

- [ ] All existing tests pass: `pytest -m "not integration and not tck"`.
- [ ] `ruff check .` clean.
- [ ] UI builds clean: `cd ui && npx tsc --noEmit -p tsconfig.app.json && npm run build`.
- [ ] Wave 6 (WP-27..WP-30) has merged (multi-tenant work depends on the analyzer-warning surface from WP-28 and the `retry_context` field from WP-29).

### Wave 7 — MT-Phase 1 (security boundary, single agent)

- [ ] Launch as **one** sub-agent with the Wave-7 prompt above. Branch off `main`.
- [ ] Review: Layer 5's ~15 hand-crafted plan tests all pass; the `test_round_trip_through_layer_5_validator` placeholder asserts the predicate shapes Layers 3/4 will emit later are accepted now.
- [ ] Review: integration smoke seeds a two-tenant disjoint-SmartGraph fixture; tenant A cannot see tenant B's data; hand-AQL bypass attempt is refused.
- [ ] Review: `safe_execute` wraps every `db.aql.execute` site; grep confirms no direct executes left in `arango_cypher/service/routes/`.
- [ ] Merge; full test run green; dual-push.
- [ ] Update `multitenant_prd.md` §11 status table: MT-0 (residual), MT-1, MT-5 → **Done**.

### Wave 8 — MT-Phase 2 (parallel, 1 pre-step + 3 agents)

- [ ] **Wave 8-pre**: launch 1 sub-agent with the `tenant_ast_common` prompt. Land on `main` before the parallel agents start.
- [ ] Verify the six public symbols are exactly as specified; full unit tests green.
- [ ] **Wave 8a**: launch MT-2, MT-3, MT-4 in parallel on sibling branches off `main` (post-8-pre).
- [ ] Review MT-2: `test_literal_tenant_predicate_rejected_even_when_value_matches` passes; WP-25.5 eval re-baselined if `pattern_match` rate moves.
- [ ] Review MT-3: `test_round_trip_through_layer_5_validator` passes — Layers 3 and 5 are coherent.
- [ ] Review MT-4: `test_round_trip_through_layer_5_validator` passes — Layers 4 and 5 are coherent.
- [ ] Merge MT-2, MT-3, MT-4 (in any order — files disjoint). If conflicts in `tenant_ast_common.py`, that's a pre-step bug; revert and re-do 8-pre.
- [ ] Full test run green; dual-push.
- [ ] Update `multitenant_prd.md` §11 status table: MT-2, MT-3, MT-4 → **Done**.

### Wave 9 — MT-Phase 3 (operability, single agent)

- [ ] Launch as **one** sub-agent. Branch off the merged Wave 8 tip.
- [ ] Review: LRU hit-rate ≥ 80% on a steady-state 100-query workload (integration test).
- [ ] Review: admin bypass is gated by `is_admin + cross_tenant=1 + bypass_reason`; audit log records all three; rate limit fires at 61 req/min.
- [ ] Review: literal tenant predicate rejection still fires for admin (structural checks preserved).
- [ ] Merge; full test run green; dual-push.
- [ ] Update `multitenant_prd.md` §11 status table: MT-6, MT-7 → **Done**.

### Wave 10 — MT-8 (standing, single agent)

- [ ] Launch on day MT-Phase-1 ships (i.e. Wave 7 merged). Initial corpus seeded from `multitenant_prd.md` §12.3 + Wave 7/8/9 test cases.
- [ ] Re-launch any time a new escape is reported. Each invocation adds cases; the runner is unchanged.
- [ ] Update `multitenant_prd.md` §11 status table: MT-8 → **Ongoing**.

### Wave 11 — v0.4+ residual tail (single agent)

- [ ] Launch any time after Wave 7 ships (not coupled to multi-tenant). Filler against an idle agent.
- [ ] Review per-feature acceptance criteria above.
- [ ] Merge; full test run green; dual-push.
- [ ] Update `python_prd.md` §10 status rows for Tail-1..Tail-5 → **Done**.

### Post-flight (after Waves 7–10 merge)

- [ ] Update `multitenant_prd.md` Status field at the top: `Draft` → `Implemented`. Move this document to a "Living spec" status note.
- [ ] Merge `multitenant_prd.md` into `python_prd.md` per its `## Merge notes` section. The standalone PRD becomes a historical reference.
- [ ] Update `docs/implementation_plan.md` tracking table: every MT-* row → **Done** with merge dates.
- [ ] Refresh the §11 status row in `python_prd.md` from "Multi-tenant safety: Not started" → "Done".
- [ ] Tag release: `git tag v0.5.0` (multi-tenant safety is the v0.5 headline).
- [ ] Run a security-review session against the red-team corpus + the three integration suites; document findings in `docs/audits/multi-tenant-launch-review.md`.
