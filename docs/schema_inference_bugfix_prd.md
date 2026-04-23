# Schema Inference & NL Feedback-Loop Bug-Fix PRD

Date: 2026-04-22
Workspace: `arango-cypher-py`
Related: [`python_prd.md`](./python_prd.md) §5 (schema detection & mapping), §1.2 (NL→Cypher pipeline)
Status: Proposed — not yet implemented

---

## Changelog

| Date | Changes |
|------|---------|
| 2026-04-22 | Initial draft. Consolidates six discrete defects surfaced by a translate-time failure on a hybrid (GraphRAG + PG) database in the pilot environment. Written as a self-contained PRD so it can be scheduled against a single wave without re-opening the whole of §5 in `python_prd.md`. |

---

## 1) Executive summary

A user-authored query against a hybrid ArangoDB database failed at the **Translate** step with a Cypher parse error (`no viable alternative at input 'MATCH (d:Compliance.'`). Investigation traced the failure to a cascade of six distinct defects spanning schema inference, NL prompt construction, the NL→Cypher retry loop, the transpiler's label resolver, and the UI's error-recovery path. Each defect is individually small, but together they produced an unrecoverable failure on a realistic query (`"What are the different versions of the Compliance.rst document?"`) against a live database.

The **primary** defect is in the heuristic schema inference path: any field named `label` / `labels` / `type` / `_type` / `kind` / `entityType` present in ≥80 % of sampled documents is treated as an LPG type discriminator, regardless of its cardinality or value shape. In the failing database, the `*_Documents` collections carry `label` as a scalar **data** field (a filename). The heuristic exploded 36 distinct filenames into 36 fake conceptual entities (`Compliance.rst`, `index.rst`, `requirements.txt`, …), 43 of them across the database containing characters (`.`) that are illegal in bare Cypher labels per the openCypher `oC_SymbolicName` grammar.

The fact that the heuristic was used at all in an environment where the analyzer was available is a **secondary** defect: `_build_fresh_bundle` silently falls back to the heuristic on `ImportError`, caches the result, and never re-tries the analyzer once the cache shape fingerprint stabilizes. Operators have no visible signal that a degraded mapping is being served.

The remaining four defects are the "defense-in-depth" layers that should have recovered from the primary defect and did not: the NL prompt does not teach the model to backtick-escape non-identifier labels; the retry loop returns the last invalid Cypher to the UI on budget exhaustion instead of failing closed; the transpiler does not strip backticks before resolving labels; and the Translate button surfaces parse errors without ever re-invoking the NL pipeline.

This PRD consolidates all six into a single scheduled fix, because each layer assumes the others behave correctly and fixing only the primary defect leaves the others as latent failures for the next schema the heuristic mis-infers.

---

## 2) Problem statement

### 2.1 Reproduction

**Database:** mixed PG + LPG (GraphRAG-derived) schema with `*_Entities` collections using `type` (scalar) + `labels` (list) as LPG discriminators, and `*_Documents` collections using `label` (scalar) as a filename data field. Roughly 75 collections, 45 k documents.

**Question:** `"What are the different versions of the Compliance.rst document?"`

**LLM-generated Cypher (written to editor):**

```cypher
MATCH (d:Compliance.rst)
RETURN d.doc_version
```

**Observed behaviour:** user clicks **Translate**. UI displays:

```
Cypher syntax error at 1:19: no viable alternative at input 'MATCH (d:Compliance.'
```

### 2.2 What the user was entitled to expect

Given the question and the schema, the pipeline should have produced either:

1. A valid Cypher query against a sensibly-named entity — e.g. ``MATCH (d:IBEXDocument {label: 'Compliance.rst'}) RETURN d.doc_version`` — and a successful transpile + execute; **or**
2. A closed, explained failure — e.g. "We could not generate a valid query for this schema. The LLM's last attempt failed Cypher parsing: `<error>`. This usually means the schema contains labels that need backtick escaping or the query is outside the supported subset" — with **no** invalid Cypher written to the editor.

### 2.3 Root cause analysis

Six distinct defects, in pipeline order:

| # | Layer | Defect | Severity |
|---|-------|--------|----------|
| **D1** | Schema inference (heuristic) | `_DOC_TYPE_FIELDS` treats `label` as a type-discriminator candidate with the same weight as `type`. In `*_Documents` collections, `label` holds a filename (data), not a class (metadata). The heuristic explodes one real entity into 36 fake entities, 43 of them with `.` in the name. | **High** — primary root cause |
| **D2** | Acquisition strategy | `_build_fresh_bundle` silently falls back to the heuristic when `schema_analyzer` raises `ImportError` at the deployed service. The (broken) heuristic bundle is cached indefinitely; stats-only refreshes never reconsider the strategy. | **High** — prevents D1 from being self-healing |
| **D3** | NL prompt | `_SYSTEM_PROMPT` + `_build_schema_summary` emit `Node :Compliance.rst (…)` to the LLM with no guidance to backtick-escape labels containing non-identifier characters. The LLM faithfully copies the illegal label into `MATCH`. | **Medium** — prompt cannot rescue D1 output |
| **D4** | NL retry loop | `_call_llm_with_retry` returns `best_cypher` even when every attempt failed ANTLR parsing, with `confidence=0.3` and a WARNING prefix buried inside `.explanation`. The UI renders the invalid Cypher as a first-class result. | **Medium** — retry loop "succeeds" with invalid output |
| **D5** | Transpiler | `_pick_primary_entity_label` looks up the literal string including backticks (`` `Compliance.rst` ``) in the mapping; never strips them. Even a correctly-escaped LLM output would not translate. | **Medium** — silent correctness trap |
| **D6** | Transpile feedback | The Translate button is a pure Cypher→AQL call with no edge back into inference. Bad Cypher in the editor produces a fresh parse error on every click, never a re-generation attempt. | **Low** — expected UX implies feedback exists |

Evidence from the pilot cache row in `arango_cypher_schema_cache`:

- `source.kind: "heuristic"` — analyzer path not taken
- `metadata.source: "heuristic"` — confirmed a second way
- 410 conceptual entities, **43** with `.` in name (`Compliance.rst`, `Mor1kx.asciidoc`, `Mstatus.mprv`, `Fetch.ctrl.lcGated`, `Openrisc1200Spec.pdf`, …)
- Physical mapping for `Compliance.rst`: `style=LABEL`, `collectionName=IBEX_Documents`, `typeField=label`, `typeValue=compliance.rst`

Evidence from running `acquire_mapping_bundle` (analyzer path) against the same live database:

- 183 conceptual entities (vs. 410)
- **0** with `.` in name
- `IBEX_Documents` collapses into one entity `IBEXDocument` with `style=COLLECTION` and `label` retained as a scalar *property*

The analyzer gets this right. The heuristic gets it wrong. The secondary defects then prevent the broken heuristic output from being caught anywhere downstream.

---

## 3) Goals

| # | Goal | Related defect |
|---|------|----------------|
| G1 | The heuristic inference path must not treat a data field as a type discriminator. | D1 |
| G2 | When the analyzer is unavailable at schema acquisition time, operators and end-users must be able to tell. The degraded bundle must not be silently re-served forever. | D2 |
| G3 | The NL prompt must teach the LLM how to emit any label, including labels containing characters that require escaping. | D3 |
| G4 | When the NL pipeline cannot produce a parse-valid Cypher within its retry budget, it must fail closed with a structured error and must **not** write invalid Cypher into the editor. | D4 |
| G5 | The transpiler must resolve backtick-escaped labels to the same mapping entry as their unescaped form. | D5 |
| G6 | Translate failures on NL-generated Cypher should be offered, or automatically routed, back into the NL pipeline with the parse error as retry context. | D6 |

### Non-goals

- Rewriting the analyzer path, adding new analyzer capabilities, or changing the analyzer contract. These remain upstream concerns governed by the no-workaround policy in PRD §5.2.
- Redesigning the schema cache. Cache structure and fingerprinting are untouched; only the producer strategy and a small visibility signal change.
- Changing the UI into a multi-user workbench. All UI changes are within the existing debug/demo surface per PRD §4.4.
- Removing the heuristic path entirely. The heuristic remains as a zero-dependency fallback per PRD §5.2.1; it just needs to be correct within its advertised scope.

---

## 4) Requirements

### 4.1 D1 — Heuristic type-field detection (primary fix)

**Requirement R1.1** — `_detect_type_field` must reject a candidate field as a discriminator when its distinct-value count is close to the row count. A field where nearly every row has its own unique value is data, not a class.

  - Threshold proposal: reject when `distinct_count / row_count > 0.5` OR `distinct_count > 50` for collections with fewer than 1 000 rows. Tunable via a module-level constant.
  - Implementation: `_type_field_values` already performs `COLLECT val`. The caller must receive the distinct count and row count and reject on the ratio before returning the field name.

**Requirement R1.2** — `_detect_type_field` must reject a candidate field whose distinct values do not look class-like.

  - A value is **not** class-like if it contains any of: `.` (dot), `/` (path separator), whitespace, or a common file extension suffix (`.rst`, `.md`, `.pdf`, `.asciidoc`, `.txt`, `.rtf`, `.docx`, `.html`, `.json`, `.xml`, `.yaml`, `.yml`, `.ttl`, `.owl`).
  - When any value in the sampled distinct set trips this rule, the candidate is rejected.

**Requirement R1.3** — Candidate priority must prefer fields whose name is unambiguously class-like. `type` and `_type` and `entityType` are unambiguous. `label` and `labels` are ambiguous. `kind` is ambiguous.

  - Implementation: split `_DOC_TYPE_FIELDS` into a tier-1 list (`type`, `_type`, `entityType`) that is accepted on the existing 80 % coverage rule alone, and a tier-2 list (`label`, `labels`, `kind`) that is accepted only when (R1.1 AND R1.2) pass in addition to coverage.

**Requirement R1.4** — When no discriminator field passes the checks, the collection must be emitted as a single entity with `style=COLLECTION`, mirroring the analyzer's behaviour for the same input.

  - The field originally considered (e.g. `label`) is retained as a scalar property on that entity, not discarded.

**Requirement R1.5** — The `_build_heuristic_mapping` output must carry a `metadata.heuristic_notes` block listing any candidate fields that were **rejected** and why, per collection. This is observability, not behaviour, but it turns future "why did X happen?" sessions from an investigation into a read.

### 4.2 D2 — Analyzer-unavailable visibility

**Requirement R2.1** — When `_build_fresh_bundle` falls through to the heuristic branch due to `ImportError`, the event must be recorded in the `MappingBundle.metadata.warnings` list as a structured record (code `ANALYZER_NOT_INSTALLED`, human message, install hint). This record rides with the bundle into the cache.

**Requirement R2.2** — The FastAPI service must refuse to start (fail fast with a clear error message) when `schema_analyzer` cannot be imported, unless `ARANGO_CYPHER_ALLOW_HEURISTIC=1` is explicitly set in the environment. The library and CLI surfaces keep their current lenient behaviour so embedders are unaffected.

**Requirement R2.3** — The `/schema/introspect` response must surface `metadata.warnings` so the UI can display a banner ("Schema inferred via heuristic fallback — labels on hybrid schemas may be imprecise. Install `arangodb-schema-analyzer` for accurate labels.").

**Requirement R2.4** — `get_mapping` must retry the analyzer path on every persistent-cache miss, not only on first acquisition. If a persistent-cache entry carries the `ANALYZER_NOT_INSTALLED` warning and the analyzer is now importable, the cache entry must be invalidated and the analyzer re-run.

**Requirement R2.5** — A one-shot admin endpoint `POST /schema/force-reacquire` must be added that clears the cache entry for the current session's database and rebuilds from scratch using `strategy="analyzer"` (hard, not `auto`). This is an operational tool for remediating already-poisoned caches without requiring DB-side manual deletion.

### 4.3 D3 — NL prompt guidance for label escaping

**Requirement R3.1** — `_SYSTEM_PROMPT` must include a rule: "Labels and relationship types that contain characters other than ASCII letters, digits, and underscore must be wrapped in backticks. For example: `` MATCH (d:`Compliance.rst`) ``, not `MATCH (d:Compliance.rst)`."

**Requirement R3.2** — `_build_schema_summary` must pre-escape any entity or relationship name that is not a valid `UnescapedSymbolicName`. A schema entry for `Compliance.rst` must be rendered in the card as `` Node :`Compliance.rst` (…) ``. An entry for `Person` stays as `Node :Person (…)`. This gives the LLM a concrete, correct template to copy.

**Requirement R3.3** — The same escaping must be applied to relationship-type rendering (the `(:X)-[:R]->(:Y)` lines).

**Requirement R3.4** — No change is required to the retry-user-suffix or to the schema-acquisition code; R3.1–R3.3 are confined to `_build_schema_summary` + the prompt template.

### 4.4 D4 — Fail-closed on retry exhaustion

**Requirement R4.1** — `_call_llm_with_retry` must return a structured failure result (empty `cypher`, `method="validation_failed"`, `explanation` = last parse or EXPLAIN error with the last attempted Cypher embedded for debugging) when every attempt fails validation. This mirrors the tenant-guardrail fail-closed branch and uses the same return shape.

**Requirement R4.2** — The UI must recognise `method="validation_failed"` and render a red error banner with the explanation, instead of writing `cypher` into the editor. This mirrors the existing handling for `method="tenant_guardrail_blocked"`.

**Requirement R4.3** — Telemetry: a counter / log at WARN level must be emitted on every validation-failed return, including the schema fingerprint and the last parse error. This is the signal operators need to tell whether a newly-deployed prompt change is regressing.

### 4.5 D5 — Transpiler strips label backticks

**Requirement R5.1** — `_pick_primary_entity_label` (and any other site that calls `resolver.resolve_entity`) must strip surrounding backticks from a label before resolution. A label identifier from the AST of the form `` `Foo.Bar` `` must be looked up as `Foo.Bar`.

**Requirement R5.2** — The same rule applies to relationship-type identifiers resolved via `resolver.resolve_relationship`.

**Requirement R5.3** — A round-trip unit test must pin this: the same mapping bundle resolves both `MATCH (d:Compliance.rst)` (when the label is a legal bare identifier) and ``MATCH (d:`Compliance.rst`)`` (when it is not) to the same `EntityMapping`.

### 4.6 D6 — Translate-on-NL-output feedback

**Requirement R6.1** — When the Translate button fails parsing on Cypher that originated from the NL pipeline in the current session (tracked via a session flag on the translate request), the UI must offer a one-click "Regenerate with error hint" action rather than only displaying the parse error.

**Requirement R6.2** — The regenerate action must invoke `/nl2cypher` with an additional `retry_context` field containing the transpile parse error, matching the shape `_RETRY_USER_SUFFIX` already consumes internally. This reuses the existing retry plumbing instead of adding a new code path.

**Requirement R6.3** — `R6.1`–`R6.2` must be gated on the same session-origin flag; hand-written Cypher that fails translation must not trigger NL re-generation (the user wrote what they wanted; silently rewriting it is worse than the parse error).

---

## 5) Design

### 5.1 D1 design sketch

```python
# arango_cypher/schema_acquire.py

_TIER1_TYPE_FIELDS = ["type", "_type", "entityType"]
_TIER2_TYPE_FIELDS = ["label", "labels", "kind"]

_FILE_EXTENSION_SUFFIXES = (
    ".rst", ".md", ".pdf", ".asciidoc", ".txt", ".rtf",
    ".docx", ".html", ".json", ".xml", ".yaml", ".yml",
    ".ttl", ".owl",
)

def _looks_class_like(value: str) -> bool:
    """True when a candidate discriminator value plausibly names a class."""
    if not value or not value.strip():
        return False
    if any(c in value for c in (".", "/", " ", "\t")):
        return False
    lv = value.lower()
    if any(lv.endswith(suf) for suf in _FILE_EXTENSION_SUFFIXES):
        return False
    return True

def _detect_type_field(db, collection_name, *, candidates=None) -> str | None:
    # ... existing 80%-coverage sampling ...
    #
    # When accepted via coverage, additionally verify per-tier rules:
    #   Tier 1: accept.
    #   Tier 2: reject unless (distinct_count ≤ max(50, 0.5*row_count))
    #           AND every sampled distinct value is class-like.
    ...
```

Rejection reasons are collected into a per-collection dict and attached to the bundle at `metadata.heuristic_notes` by `_build_heuristic_mapping`.

### 5.2 D2 design sketch

```python
# arango_cypher/schema_acquire.py (acquire_mapping_bundle / _build_fresh_bundle)

except ImportError:
    bundle = _build_heuristic_mapping(db, schema_type)
    bundle = _attach_warning(bundle, code="ANALYZER_NOT_INSTALLED",
                              message="arangodb-schema-analyzer not installed; "
                                      "using heuristic fallback.",
                              install_hint="pip install arangodb-schema-analyzer")
    logger.warning(  # escalated from info
        "Heuristic schema path used — install arangodb-schema-analyzer for accurate mappings on hybrid schemas.",
    )
    return bundle
```

```python
# arango_cypher/service.py (app startup)

@app.on_event("startup")
def _require_analyzer_unless_opted_out():
    if os.environ.get("ARANGO_CYPHER_ALLOW_HEURISTIC") == "1":
        return
    try:
        import schema_analyzer  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI service requires arangodb-schema-analyzer. "
            "Install it (`pip install arangodb-schema-analyzer`) or set "
            "ARANGO_CYPHER_ALLOW_HEURISTIC=1 to accept degraded mappings."
        ) from exc
```

### 5.3 D3 design sketch

```python
# arango_cypher/nl2cypher/_core.py

_SYMBOLIC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def _escape_label(name: str) -> str:
    """Return `name` wrapped in backticks if it's not a bare symbolic name."""
    return name if _SYMBOLIC_NAME_RE.match(name) else f"`{name}`"

# ... in _format_entity and relationship rendering in _build_schema_summary:
#   Node :{name}        → Node :{_escape_label(name)}
#   (:{from_e})-[:{rtype}]->(:{to_e})
#                       → (:{_escape_label(from_e)})-[:{_escape_label(rtype)}]->(:{_escape_label(to_e)})
```

```text
# _SYSTEM_PROMPT (appended after the existing "Rules:" block)

- Labels and relationship types containing characters other than ASCII
  letters, digits, and underscore must be wrapped in backticks, e.g.
  MATCH (d:`Compliance.rst`) RETURN d.doc_version.
  The schema below has already escaped such names; copy them verbatim.
```

### 5.4 D4 design sketch

Replace the fall-through at `_core.py:604–617` with a new closed-failure branch that mirrors the tenant-guardrail shape. The UI-side handling follows the precedent set by `method="tenant_guardrail_blocked"` (empty `cypher` + explanation banner).

### 5.5 D5 design sketch

```python
# arango_cypher/translate_v0.py (_pick_primary_entity_label and siblings)

def _strip_label_backticks(name: str) -> str:
    if len(name) >= 2 and name.startswith("`") and name.endswith("`"):
        return name[1:-1]
    return name

# at each resolver call site:
primary = resolver.resolve_entity(_strip_label_backticks(lab))
```

### 5.6 D6 design sketch

Add a `source: "nl_pipeline" | "user" | null` field to the UI's translate-request builder, set to `"nl_pipeline"` when the editor Cypher was last populated by an `/nl2cypher` response and untouched since, and `"user"` otherwise (typing / paste / sample-load clears the flag). The UI reads this flag on translate failure and offers the regenerate action only when it is `"nl_pipeline"`. Backend handler unchanged.

---

## 6) Out-of-scope / deferred

- **Teaching the analyzer about this schema family.** The analyzer already handles it correctly (as confirmed against the live DB); no upstream change is needed.
- **A full "NL pipeline ↔ Translate" bidirectional channel** beyond the single-step feedback in R6.1–R6.2. A richer edit-then-regenerate flow (e.g. "the user edited the Cypher, then clicked Translate, and the translate failed — should we prefer their edit or regenerate from NL?") is a larger design and is out of scope.
- **Auto-regeneration** on translate failure (i.e. without a user click). R6.1 deliberately requires a click; auto-regenerating invisibly is a footgun on token budget and on user expectations.
- **Schema-mapper improvements in this repo.** Anything that belongs upstream goes upstream per §5.2 no-workaround policy.

---

## 7) Testing strategy

### 7.1 Unit tests

| Defect | New test module | Cases |
|--------|-----------------|-------|
| D1 | `tests/test_schema_acquire_heuristic.py` (new) | `label` with one filename per row on a 36-row collection → rejected; `type` with `{"Person","Movie"}` on a 173-row collection → accepted; `label` with class-like values (`"Movie"`, `"Person"`) on a small collection → accepted; `kind` with values containing spaces → rejected. |
| D1 | same | `metadata.heuristic_notes` present and correctly scoped when rejections occur. |
| D2 | `tests/test_schema_acquire_warnings.py` (new) | `ImportError` path attaches `ANALYZER_NOT_INSTALLED` warning; warning survives round-trip through cache; `force-reacquire` endpoint invalidates and refetches. |
| D2 | `tests/test_service_startup.py` (new) | Service fails to start without analyzer and without `ARANGO_CYPHER_ALLOW_HEURISTIC=1`; starts with either. |
| D3 | `tests/test_nl2cypher_prompt_builder.py` (extend) | Entity with `.` in name is rendered pre-escaped; relationship types with special chars are pre-escaped; bare-identifier entities are not touched; zero-shot byte-identical invariant with `tenant_context=None` (must still hold; escaping is purely a data-driven change). |
| D4 | `tests/test_nl2cypher_core.py` (extend) | Exhausted-retry path returns empty `cypher` + `method="validation_failed"`; warning logged; existing tenant-guardrail path unchanged. |
| D5 | `tests/test_translate_v0.py` (extend) | `MATCH (d:\`Compliance.rst\`)` and `MATCH (d:Foo)` (mapped to an entity with a non-identifier alias) both resolve to the same `EntityMapping`. |
| D6 | `ui/src/api/__tests__/client.test.ts` (extend) | `source` flag plumbed through; Regenerate action only offered when `source === "nl_pipeline"`. |

### 7.2 Integration tests

- Re-run the failing production query against a fixture derived from `ic-knowledge-graph-temporal` (anonymised). After fixes: heuristic path produces an `IBEXDocument`-shaped entity, NL→Cypher produces a query that transpiles and runs, Translate button does not fail.
- Red-team case: a synthetic DB where one collection has a legitimate `label` discriminator with class-like values (`"Customer"`, `"Supplier"`, `"Employee"`) and another collection has `label` with filename values. The heuristic must produce an LPG mapping on the first and a COLLECTION mapping on the second, in the same bundle.

### 7.3 Regression

Run the existing translation-golden suite and the NL2Cypher eval corpus. No expected deltas — these fixes target schemas and prompt paths that the existing corpora do not currently exercise.

---

## 8) Rollout plan

### 8.1 Phasing

1. **Phase A (local / library — same PR):** D1, D5. These are pure code changes with no deployment impact and no feature gate.
2. **Phase B (service / service-only — same PR as A):** D2, D4. These require a service restart and a one-line env var if the deployment environment currently runs without the analyzer installed.
3. **Phase C (service + UI — separate PR):** D3, D6. D3 is backend-only but is paired with D6 which requires a UI ship.

Phases A and B ship together as the bug-fix wave; Phase C ships as a follow-up once A/B is validated in the pilot environment.

### 8.2 Cache invalidation

When Phase A/B ships, existing pilot caches will still contain the broken heuristic bundle. The fingerprint has not changed, so stats-only refresh will re-serve the old bundle. Operators must:

1. Deploy the new image.
2. Call `POST /schema/force-reacquire` for each affected database, or delete the row in `arango_cypher_schema_cache` and hit `/schema/introspect?force=true`.
3. Verify the new bundle's `source.kind == "schema_analyzer_export"` (or, if the analyzer is still missing, that the UI shows the heuristic-fallback banner).

The `force-reacquire` endpoint is introduced in R2.5 specifically to make step 2 one curl.

### 8.3 Backwards compatibility

- Heuristic-path output shape is strictly narrower after fixes: some entity labels disappear from the conceptual schema (because they were spurious). No renames. No changes to entities the heuristic got right.
- Analyzer-path output is unchanged.
- Existing persistent-cache entries without the new `metadata.heuristic_notes` field are valid (field is optional). Entries with the new `metadata.warnings` shape are a pure addition.
- NL prompt changes are additive to `_SYSTEM_PROMPT`. Zero-shot byte-identical invariant for the no-tenant-context path is preserved by making the pre-escape a data-level change (the schema summary is already allowed to vary).

### 8.4 Operational runbook additions

- New metric: `schema_acquire_heuristic_fallbacks_total` (counter). Alert threshold: any non-zero value in production.
- New log pattern: `Heuristic schema path used — install arangodb-schema-analyzer ...` (WARN). Should be zero in production.
- New log pattern: `NL2Cypher validation_failed ...` (WARN). Baseline rate expected to be low; spike indicates either a prompt regression or a schema family the pipeline cannot handle.

---

## 9) Open questions

1. Should the heuristic's tier-2 acceptance rules (R1.1, R1.2) be per-candidate configurable, or are the defaults sufficient for all anticipated schemas? **Provisional answer:** defaults for v1; promote to config only if a real schema needs it.
2. Should `POST /schema/force-reacquire` also bust the in-process cache tier across all workers (distributed invalidation), or is per-worker sufficient? **Provisional answer:** per-worker is sufficient; the persistent cache is the shared truth, and invalidation races resolve within one request cycle.
3. Should D6 be extended to also trigger on EXPLAIN-time failures at execute time, not just parse-time failures at translate time? **Provisional answer:** out of scope for this wave; revisit after the translate-time feedback is shipped and measured.

---

## 10) Merge notes

- Fold this document's requirements into `python_prd.md` §5.3 (detection strategy) once the fix ships. Keep the document as the historical record of the incident and analysis.
- Cross-reference this PRD from the implementation plan's new work-package entries (WP-27 through WP-30 proposed — see `implementation_plan.md`).
- No change required to `multitenant_prd.md`; this bug is orthogonal to tenant isolation.

---

## 11) Closeout runbook — `ic-knowledge-graph-temporal` end-to-end verification

This section is the executable acceptance check for the PRD. It operationalises §2.2 (binary pass/fail), §3 (goals G1-G6), and §7.2 (integration tests). Run it once, record the pass/fail per AC in §11.4, and the PRD is closed.

### 11.1 Preconditions

| # | Item | Command / check |
|---|------|-----------------|
| P1 | Backend service running locally | `uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8001` |
| P2 | UI dev server running | `cd ui && npm run dev` (default: http://localhost:5173) |
| P3 | `arangodb-schema-analyzer` installed in the backend venv | `python -c "import arangodb_schema_analyzer; print(arangodb_schema_analyzer.__version__)"` — if this raises `ImportError`, AC-2 becomes active; otherwise AC-2 is a no-op and should be recorded as such. |
| P4 | Connect dialog pointed at the temporal DB | In the UI Connect dialog, enter: endpoint `https://a2lc79ac.rnd.pilot.arango.ai`, user `root`, password `YzW?<N!q:OT(f8*)`, database `ic-knowledge-graph-temporal`. **Do not edit `.env`** — the default points elsewhere; this check is session-scoped. |
| P5 | Cold cache | After connecting, run `curl -X POST http://localhost:8001/schema/force-reacquire` (or use the UI warning banner's "Force reacquire" action if it is offered). This guarantees the DB is reconciled against the newly-shipped code, not against a pre-fix cached bundle. |

### 11.2 Acceptance checks

Each check has exact inputs, exact expected behavior, and an exact verification command. Record one of `PASS` / `FAIL` / `N/A (reason)` per check in §11.4.

---

**AC-1 — Heuristic does not explode `label` into 36 fake entities (G1 / D1)**

*Input:* After P4+P5, inspect the schema bundle produced for the temporal DB.

*Verification:*
```bash
curl -s http://localhost:8001/schema/introspect \
  | jq '[.entities[] | select(.label | test("\\."))] | length'
```

*Expected:* `0` — zero conceptual entities have `.` in their label. (Pre-fix: 43.)

*Secondary check:* at least one entity with `physicalMapping.collectionName` matching `*_Documents` exists with a sensible label (e.g. `IBEXDocument`, or the COLLECTION-style name the heuristic falls back to when no valid discriminator is found):

```bash
curl -s http://localhost:8001/schema/introspect \
  | jq '[.entities[] | select(.physicalMapping.collectionName | test("_Documents$"))] | length'
```

*Expected:* a small number (ideally 1 per `*_Documents` collection, not 36 per collection).

*Evidence to capture:* paste the full `metadata.heuristic_notes` block for any `*_Documents` collection into §11.4. Per R1.4, it should carry `rejected_candidates: { label: <reason> }` and `resolved_style: "COLLECTION"` (or `"LABEL"` with a *different* discriminator if one was accepted).

---

**AC-2 — Analyzer-unavailable visibility (G2 / D2)**

*Condition:* Only applies if P3 raised `ImportError`. If the analyzer is installed (expected on local dev), mark `N/A (analyzer installed)`.

*If active:*

```bash
curl -s http://localhost:8001/schema/status | jq '.warnings'
```

*Expected:* contains `{"code": "ANALYZER_NOT_INSTALLED", ...}`.

*UI check:* the yellow schema-warning banner renders at the top of the app with a "Force reacquire" action wired to `POST /schema/force-reacquire`.

---

**AC-3 — NL pipeline produces either a valid query or a fail-closed banner (G3 / G4 / D3 / D4)**

*Input:* In the NL input box, type verbatim:

> What are the different versions of the Compliance.rst document?

Click **Ask**.

*Expected — one of two outcomes, both are PASS:*

1. **Success path:** The Cypher editor fills with a parse-valid query (e.g. `` MATCH (d:IBEXDocument {label: 'Compliance.rst'}) RETURN d.doc_version `` or equivalent). The Translate button produces AQL. Execute returns ≥1 row. No red banner.
2. **Fail-closed path:** The red "NL → Cypher failed:" banner renders under the NL input with an explanatory message that includes the last LLM parse error and the last attempted Cypher. The Cypher editor stays **empty**. No invalid Cypher is written anywhere.

*Anti-expected (FAIL):* the editor is populated with a query containing `MATCH (d:Compliance.rst ...)` (no backticks) or `` MATCH (d:`Compliance.rst` ...) `` that then fails Translate with no recovery affordance. Either of these is a regression of D3/D4.

*Evidence to capture:* screenshot of the final UI state; the `method` field from the response (`"llm"` for success, `"validation_failed"` for fail-closed).

---

**AC-4 — Transpiler resolves backtick-escaped labels (G5 / D5)**

*Input:* Paste directly into the Cypher editor (bypassing NL):

```cypher
MATCH (d:`Compliance.rst`) RETURN d.doc_version LIMIT 5
```

Click **Translate**.

*Expected:* Translate succeeds. The resulting AQL targets the same collection as if the user had typed `MATCH (d:IBEXDocument {label: 'Compliance.rst'}) ...` (or whichever sensible entity AC-1 produced). No "unknown entity" / "no mapping" error.

*Anti-expected (FAIL):* translate error mentioning ``` `Compliance.rst` ``` with backticks in the error message (means the backticks weren't stripped at the resolver boundary).

---

**AC-5 — Regenerate-from-NL UX when NL output fails Translate (G6 / D6)**

*Condition:* This is only exercisable if AC-3 landed on success-path AND the produced Cypher then fails Translate for some schema-related reason. If AC-3 was fail-closed, skip and mark `N/A (fail-closed branch)`.

*If AC-3 succeeded and Translate fails:*

*Expected:* the red translate-error banner shows the **"Regenerate from NL with error hint"** button. The button is **enabled**. Clicking it reinvokes the NL pipeline with the translate error as `retry_context` and updates the editor.

*Anti-expected (FAIL):* button is missing, disabled when `lastNlQuestion` is populated, or does not forward `retry_context` (check browser devtools: the `POST /nl2cypher` body should include `"retry_context": "<error message>"`).

---

**AC-6 — Red-team: mixed `label` semantics coexist in one bundle (§7.2, optional)**

*Condition:* Optional per the PRD; run only if you want to close this as well. Requires a synthetic DB. If skipping, mark `N/A (optional, skipped for v1 closeout)`.

*Input:* A DB with two collections:
- `customers`: 200 rows, each with `label ∈ {"Customer", "Supplier", "Employee"}` (uniform distribution).
- `docs`: 50 rows, each with `label` holding a unique filename.

*Expected:* After `POST /schema/force-reacquire`, `/schema/introspect` returns one LPG-style entity family for `customers` (three labels) and one COLLECTION-style entity for `docs` (no filename explosion).

### 11.3 Exit criteria

The PRD is closed when:
- AC-1 PASS
- AC-2 PASS or `N/A (analyzer installed)`
- AC-3 PASS on either branch
- AC-4 PASS
- AC-5 PASS or `N/A (fail-closed branch)`
- AC-6 PASS or `N/A (optional)`

### 11.4 Closeout log (fill in during verification)

| AC | Result | Evidence / notes | Verified by | Date |
|----|--------|------------------|-------------|------|
| AC-1 | ___ | | | |
| AC-2 | ___ | | | |
| AC-3 | ___ | | | |
| AC-4 | ___ | | | |
| AC-5 | ___ | | | |
| AC-6 | ___ | | | |

**Overall PRD status:** `OPEN` — change to `CLOSED` once all rows satisfy §11.3 exit criteria.
