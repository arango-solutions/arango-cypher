# Implementation Handoff

**Branch:** `docs/wave8-phase2-closeout` (pushed to both remotes)
**Date:** 2026-06-23
**Purpose:** Context handoff for the next agent. Canonical roadmap lives in
`docs/cypher_coverage_plan.md`; this file captures the *remaining* work and the
operational context a fresh session needs.

---

## State as of this handoff

Cypher coverage is at **22/22 transpile**. WP-S1, WP-S2 (incl. S2c), and WP-S3
(incl. S3c) are all complete.

**Recently shipped (commits `51be876` → `7491bb7`):**

- `fix(ui)`: DB-switch 401 storm fix (client token lifecycle race).
- `feat(translate)`: WP-S2c — label predicates on untyped vars
  (`WHERE risk:RISK_FACTOR` over `MATCH (risk)`).
- `feat(nl2cypher,service,ui)`: WP-S3c — one-click inverted-index creation from
  NL entity-resolver advisories (full stack: result field → API → endpoint → UI).
- `docs`: plan + changelog updates.

---

## Remaining implementation plan

### A. Loose ends from the last session (small, high-value)

1. ~~**Dedupe the unlabeled-MATCH warning.**~~ ✅ **Done** (commit `4e48943`).
   Translate/execute warnings render in a single dismissible strip above the AQL
   editor (`ui/src/App.tsx` ~line 1789), backed by `ui/src/utils/warnings.ts`
   (`warningsKey` resets dismissals on a new warning set; `filterVisibleWarnings`).
   The duplicate `ResultsPanel` banner was removed. (`ProfileWarningsBanner` and
   `SchemaWarningBanner` are distinct concerns and intentionally remain.)

2. ~~**Catalog sidecar auto-warm for newly-connected DBs.**~~ ✅ **Done** (commit
   `4e48943`). `arango_cypher/catalog/warm.py` + the `pending` path in
   `service/routes/schema.py` auto-warm a connected-but-unregistered DB.

3. ~~**Verify live schema classification**~~ ✅ **Done** (verified 2026-06-29
   against the live `FinReflectKG` pilot DB). Note: that host rejects HTTP Basic
   auth — connect with `auth_method="jwt"` (python-arango). Findings:
   - `chunks` → entity `Chunk`, **`style=COLLECTION`** (a side store), correctly
     *not* a `LABEL`-style entity sharing `Node`. The 19 domain types
     (COMP/ORG/PERSON/RISK_FACTOR/…) are `style=LABEL coll=Node`; `relations` is
     the single `GENERIC_WITH_TYPE` edge collection (200 types). `aga_*`,
     `benchmark_*`, and `arango_cypher_schema_cache` are all COLLECTION side
     stores.
   - `MATCH (n)` resolves to `Node` and emits the (correct) warning listing the
     29 excluded side collections; `MATCH (c:Chunk)` targets `chunks`. The
     unlabeled-MATCH warning is therefore *correct* behavior — confirmed, not a
     misclassification.

### B. WP-V1 — Broaden the corpus & guard semantics (ongoing)

- Add **execution-grounded** checks (not just transpile-success) where a live DB
  is available — assert result *shape* (path vs scalar) for "as a graph"
  questions.
- Expand beyond the 22-query benchmark into a **feature-matrix corpus** (every
  clause/function exercised once).
- `tests/test_translate_finreflectkg_corpus.py` (+ new fixtures under
  `tests/fixtures/`).

### C. Out-of-scope so far — next big tracks (confirm priority with user)

- **Write clauses**: `CREATE` / `MERGE` / `SET` / `DELETE` / `REMOVE` /
  `FOREACH` are now broadly supported in `arango_cypher/_translate_v0/writes.py`
  (see `tests/test_translate_write_clause_gaps.py`). Recently closed:
  whole-map `CREATE` params, `CREATE→SET/REMOVE`, unlabeled `SET/DELETE/REMOVE`,
  multiple `MERGE` clauses, multi-hop relationship `MERGE`, and `CREATE`/`DELETE`
  inside `FOREACH`. Hard AQL constraint honored throughout: a collection cannot
  be written twice (or read after write) in one query (ERR 1579), so multi-write
  forms require distinct collections and otherwise fail closed with actionable
  errors. Remaining write work is narrower: same-collection multi-MERGE (needs a
  multi-statement/transaction execution model the single-`AqlQuery` output can't
  represent), `MERGE` actions on multi-hop, and `CREATE`+`DELETE` in one query.
- **`OPTIONAL MATCH`** semantics beyond current support.
- **APOC-equivalent procedures** (Neo4j `apoc.*`, `db.index.fulltext.*`). Only
  the `arango.*` extension family exists today
  (`arango_cypher/extensions/search.py`).

### D. Open design questions (need user decisions)

1. Default fuzzy matching to **portable `CONTAINS`/`=~`** vs. invest further in
   the **ArangoSearch `arango.*`** path?
2. Should "create index" become a dedicated **"Indexing" panel** vs. the current
   inline NL advisory strip?

---

## Operational context the next agent needs

- **Two remotes** on `origin` (`ArthurKeen/arango-cypher-py` +
  `arango-solutions/arango-cypher`). A single `git push origin HEAD` pushes to
  both.
- **Translator internals:** contextvars (`_active_resolver`, `_active_warnings`,
  `_active_path_vars`, `_active_registry`) in
  `arango_cypher/_translate_v0/state.py`. The recursive expression compiler is
  `_compile_expression` in `arango_cypher/_translate_v0/core.py`; WP-S2c added
  `_compile_label_predicate` there.
- **UI build:** after editing `ui/src`, run `cd ui && npm run build` or the
  `test_ui_dist_freshness` pytest fails. UI unit tests: `cd ui && npx vitest run`.
  (The `working_directory` tool param has not been reliable for `ui/`; prefer
  `cd /abs/path/ui && ...`.)
- **Pre-existing flaky pair:**
  `tests/test_session_tenant_binding.py::TestExecuteTenantViolationStatusCode`
  (2 tests) fail under full-suite ordering but pass in isolation — not yours to
  fix unless asked.
- **Live dev server:** one uvicorn on `127.0.0.1:8001` (was pid 34219) runs the
  service. Restart it to pick up new backend code when testing via the UI:
  `lsof -ti tcp:8001 | xargs kill -9; ./scripts/run_service.sh --host 127.0.0.1 --port 8001`
- **Backend tests (skip live/LLM):**
  `.venv/bin/python -m pytest tests/ -q -k "not live and not anthropic and not openai"`
- **Do not commit** the untracked `.cursor/` dir or the root `package-lock.json`
  (unrelated to this work).

---

## Key file map

| Concern | File |
| --- | --- |
| Cypher→AQL transpiler core | `arango_cypher/_translate_v0/core.py` |
| Translator shared state (contextvars) | `arango_cypher/_translate_v0/state.py` |
| NL→Cypher pipeline + `NL2CypherResult` | `arango_cypher/nl2cypher/_core.py` |
| Entity resolver + `IndexAdvisory` | `arango_cypher/nl2cypher/entity_resolution.py` |
| `arango.*` fuzzy/text extensions | `arango_cypher/extensions/search.py` |
| NL service route | `arango_cypher/service/routes/nl.py` |
| Schema + create-index route | `arango_cypher/service/routes/schema.py` |
| Request/response models | `arango_cypher/service/models.py` |
| Catalog sidecar | `arango_cypher/catalog/{registry,sync}.py` |
| UI app shell + NL/advisory UI | `ui/src/App.tsx` |
| UI API client | `ui/src/api/client.ts` |
| UI reducer/store | `ui/src/api/store.ts` |
| Connection dialog (db switch) | `ui/src/components/ConnectionDialog.tsx` |
| Coverage roadmap | `docs/cypher_coverage_plan.md` |
| Changelog | `docs/python_prd.md` |
