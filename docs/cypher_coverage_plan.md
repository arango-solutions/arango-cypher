# Cypher Coverage Completion Plan

**Status:** Transpiler coverage (M1+M2) **complete: 22/22**. Semantics (M3): WP-S1
done; WP-S2 done (incl. S2c); WP-S3 done (incl. S3c).
**Owner:** transpiler / NL→Cypher
**Date:** 2026-06-23

This plan defines the work to close openCypher coverage in the v0 transpiler and
to fix the NL→Cypher semantic gaps surfaced by the "as a graph" / approximate-match
issue. It is grounded in a measured baseline against a real-world financial
knowledge-graph benchmark (22 NL/Cypher/AQL queries), now living in the repo as
`tests/test_translate_finreflectkg_corpus.py` (fixture `finreflectkg.export.json`).

---

## 1. Measured baseline

Transpiler result on the 22-query benchmark (LPG: `Node` + `relations`, both
`type`-discriminated):

| Result | Original baseline | After WP-C1…C3 |
| --- | --- | --- |
| ✅ Transpiles | 15 / 22 (68%) | **22 / 22 (100%)** |
| ❌ Coverage gap | 7 / 22 | 0 / 22 |

The 7 original failures collapsed into **three transpiler gaps** plus a
**cross-cutting NL→Cypher semantic issue**; the transpiler gaps are now closed:

| Gap | Error | Queries | Status |
| --- | --- | --- | --- |
| **A. `collect()` aggregation** | `Unsupported function in v0: COLLECT` | q05, q09, q10 | ✅ WP-C3 (DISTINCT, slice, mixed-with-aggregate) |
| **B. List subscript / slice `x[i]`, `x[i..j]`** | `Only IN operator is supported in v0` | q01, q05, q09, q10 | ✅ WP-C2 |
| **C. Scalar fn aliases `upper`/`lower`** | `Unsupported function in v0: upper` | q03, q04, q06 | ✅ WP-C1 |
| **S. NL semantics: "as a graph" + fuzzy match** | (transpiles, wrong intent) | q03, q06 | ⏳ WP-S1/S2 |

### Remaining *semantic* gaps (transpile ✅, correctness ⏳)

100% transpile-success is **not** 100% semantic correctness. Tracked for the
WP-S phase:

- ~~**Label predicate on an untyped variable.**~~ ✅ **WP-S2c (done).**
  `MATCH (risk) … WHERE risk:RISK_FACTOR OR risk:EVENT` (q10/q11/q20) now compiles
  the label suffix to the mapping's discriminator filter
  (`risk[@typeField] == @typeValue` for LABEL/GENERIC_WITH_TYPE,
  `IS_SAME_COLLECTION(...)` for COLLECTION) instead of dropping it to a no-op.
- **"as a graph" intent + approximate entity match** (the CINF query). → **WP-S1/S2**.

What already works and must stay working (regression guard): anonymous edge
scans (`MATCH ()-[r]->()`), `type(r)`/`labels(n)` discriminator mapping,
multi-hop fixed paths, variable-length `*2..4`, multi-`MATCH` pipelines,
`WITH … count()` aggregation, `=~` regex, `CONTAINS`, `IN`, `IS [NOT] NULL`,
path variables `MATCH p = … RETURN p`, list comprehension `[n IN nodes(path) | n.name]`.

---

## 2. The CINF query — root-cause of the "flawed" result (answers to #2)

NL: *"Show me, **as a graph**, the publicly-traded companies that Cincinnati
Financial (CINF) has a stake in."*

Generated: `MATCH (c:COMP {name: "cincinnati financial corporation"})-[:has_stake_in]->(p:COMP) RETURN p.name`

Three independent defects:

1. **Returns a scalar, not a graph.** The question explicitly asks for a graph,
   so the Cypher should bind and return a path (`MATCH p = (c)-[:Has_Stake_In]->(x) RETURN p`)
   — i.e. nodes + relationships the UI can render — not `p.name`.
2. **Brittle exact match.** `{name: "cincinnati financial corporation"}` (and the
   AQL `LOWER(name) == LOWER(...)`) is an equality test. The entity resolver guessed
   a *full legal name*; in this dataset the node's `name` is the ticker-style token
   `"cinf"`. An exact match — even case-folded — misses.
3. **Wrong label.** It used `:COMP` for the anchor; CINF is an `:ORG` in the source.

### 2.a — Does the analyzer tell us the shape of `name` values?

Partly. The consumer (`schema_acquire._profile_property_values`) already records,
per property: `type`, `sampleValues` (≤4 distinct, ≤48 chars), `sentinelValues`,
`numericLike`, `required`. So for `name` it *does* capture representative values
(e.g. `["cinf","aapl","msft"]`) that reveal casing and token-vs-phrase shape —
**but we don't currently feed a "value shape" signal to the LLM or the resolver
in a way that says "match approximately; names are short lowercased tokens".**

Planned additions (cheap, sampled): a `valueShape` descriptor per string property —
`{casing: "lower|upper|mixed", avgLen, maxLen, looksLikeToken: bool, distinctRatio}` —
derived from the same sample, surfaced in the prompt's schema card and consumed by
the resolver to choose exact vs. fuzzy strategy.

### 2.b — Does Cypher lack fuzzy text functions?

Core openCypher has only `CONTAINS`, `STARTS WITH`, `ENDS WITH`, and `=~` (regex).
There is **no built-in similarity/fuzzy/full-text** in the language itself — Neo4j
exposes those via procedures (`db.index.fulltext.queryNodes`, `apoc.text.*`). So:
- For NL→Cypher we should generate `CONTAINS`/`=~` for approximate name matches
  (portable), and
- Optionally expose ArangoSearch-backed fuzzy matching through the existing
  `arango.*` extension namespace (e.g. `arango.search(...)`, `arango.ngram_match(...)`)
  for callers who opt in.

### 2.c — Should ArangoDB use an inverted (ArangoSearch) index on `name`?

Yes, for production. `LOWER(name) == …` and `name LIKE "%…%"` are full collection
scans on `Node`. An **ArangoSearch View** (or inverted index) on `name` with a
`text`/`ngram` analyzer enables `SEARCH ANALYZER(...)`, `PHRASE`, `NGRAM_MATCH`,
and `LEVENSHTEIN_MATCH` — fast fuzzy lookups. This pairs with the workbench's
"offer to create the index" pattern (same UX we use for VCI): detect the missing
analyzer/view when the resolver falls back to a scan, and offer one-click creation.

---

## 3. Work packages

Each WP is independently shippable, test-first, and flips one or more corpus
entries from GAP → SUPPORTED (the `test_known_gap_*` cases fail when a gap closes,
which is the prompt to promote the entry).

### WP-C1 — Scalar function aliases (Gap C) · *S, ~0.5 day*
- Map `upper`→`UPPER`, `lower`→`LOWER` (alias of canonical `toUpper`/`toLower`)
  in the function compiler.
- Audit other common non-canonical aliases the LLM emits (`len`→`size`, etc.).
- **Closes:** q03, q04, q06 (transpile-level). *Caveat:* q03/q06 also need WP-S
  for correct intent.
- **Tests:** unit cases in `test_translate_functions` + corpus promotion.

### WP-C2 — List subscript & slice operators (Gap B) · *M, ~2 days*
- Compile `expr[i]` → AQL `NTH(expr, i)` / `expr[i]`; `expr[i..j]` → `SLICE(...)`;
  open-ended `[i..]`, `[..j]`. Negative indices per Cypher semantics.
- Wire into `oC_ListOperatorExpression` (currently only `IN`).
- **Closes:** q01 (with WP-C4), and the `[0..5]`/`[0..3]` slices in q05/q09/q10.
- **Tests:** golden tests for index/slice + corpus promotion.

### WP-C3 — `collect()` aggregation (Gap A) · *L, ~3–4 days*
- Recognize `collect(x)` / `collect(DISTINCT x)` in `WITH`/`RETURN` aggregation
  position → AQL `COLLECT … AGGREGATE g = UNIQUE(x) | PUSH(x)` (DISTINCT→`UNIQUE`).
- Compose with grouping keys, post-aggregation `WHERE` (→ `FILTER`), `ORDER BY`,
  `LIMIT`, and slice-of-collect (`collect(...)[0..5]`, needs WP-C2).
- **Closes:** q05, q09, q10.
- **Tests:** dedicated aggregation goldens (grouping, DISTINCT, slice, filter).

### WP-C4 — Group-by computed key (q01) · *SUBSUMED by WP-C2*
- The aggregation pipeline already supported a computed grouping key
  (`COLLECT k = <expr> AGGREGATE …`); q01's only blocker was the list subscript
  in `labels(n)[0]`. Once WP-C2 landed, q01 transpiles. No separate work needed.

### WP-S1 — NL "return a graph" intent · ✅ DONE
- `_detect_graph_intent` (conservative regex: "as a graph/network/subgraph",
  "visualize/visualise", "show/draw/render/display/plot … graph/network/
  relationships/connections", "graph of …") in `nl2cypher/_core.py`.
- On a hit, `PromptBuilder.graph_intent=True` appends an "Output shape: return a
  graph" section instructing a **path-returning** Cypher (`MATCH p = … RETURN p`)
  with an inline example — appended (not baked into the cacheable prefix) so it
  costs tokens only when relevant. Threaded through `_call_llm_with_retry`.
- Tests: `TestGraphIntentDetection` in `tests/test_nl2cypher.py` (detector
  positives/negatives, builder rendering, and recording-provider integration
  asserting the section reaches/omits from the live system prompt).
- **Improves:** q03, q06 correctness; general "show me … graph" questions.

### WP-S2 — Approximate entity matching · ✅ DONE (incl. S2c)
- **S2a — valueShape surfaced (done).** `_property_quality_hint` now renders
  `shape: <…>` and `e.g. "…", "…"` from analyzer property metadata
  (`valueShape`/`value_shape`, `examples`/`exampleValues`/`sampleValues`), and
  `_build_schema_summary` appends a "Value-shape hints" block (only when present)
  telling the LLM to match the real value form and prefer fuzzy/`CONTAINS` over
  brittle exact equality — so it stops inventing legal names for token fields.
- **S2b — symbol/ticker matching (done).** `extract_candidates` captures a
  parenthesized symbol ("Cincinnati Financial **(CINF)**") *first*, and the
  resolver's property set now includes `ticker`/`symbol`/`code`/`id`, so an exact
  symbol match resolves even when the long name fuzzes. (The resolver already
  emitted exact/contains/reverse/Levenshtein scoring; S2b feeds it better
  candidates + fields.)
- Tests: `TestValueShapeHints` (`tests/test_nl2cypher.py`),
  `TestExtractCandidates`/`TestIdentifierPropertyCandidates`
  (`tests/test_nl2cypher_entity_resolution.py`).
- **S2c — label predicate on untyped var (done).** `MATCH (risk) …
  WHERE risk:RISK_FACTOR` no longer compiles to a no-op. `_compile_expression`'s
  `OC_PropertyOrLabelsExpression` branch (`core.py`) now calls a new
  `_compile_label_predicate(var, labels_ctx, bind_vars)` helper for the
  pure-variable + labels case. It reads the active mapping via the existing
  `_active_resolver` ContextVar (no parameter-threading refactor needed) and
  emits, per label: `var[@typeField] == @typeValue` for LABEL/GENERIC_WITH_TYPE,
  or `IS_SAME_COLLECTION(@coll, var)` for COLLECTION. Multiple labels (`n:A:B`)
  AND together (Cypher "has all labels"); `OR` of label predicates composes
  through the normal boolean compiler. Unknown labels raise `MAPPING_NOT_FOUND`
  rather than silently passing. Tests: `tests/test_translate_label_predicate.py`
  + corpus guard `test_label_predicate_on_untyped_var_emits_filter` (q10/q11/q20).
- **Improves:** q10/q11/q20 semantic correctness (plus q03/q04/q06/q07).

### WP-S3 — ArangoSearch index advisory + fuzzy compilers · ✅ DONE (incl. S3c)
- **S3a — `arango.*` fuzzy compilers (done).** Extended the *existing*
  registry-gated extension framework (`arango_cypher/extensions/search.py`,
  alongside `arango.bm25`/`tfidf`/`analyzer`) with a fuzzy/text family:
  `arango.like`, `starts_with`, `in_range`, `levenshtein_distance`,
  `levenshtein_match`, `ngram_match`, `ngram_similarity`, `phrase`, `boost`,
  `min_match`, `tokens`, `soundex`, `regex_test`/`matches`/`replace` → the
  identically-signatured AQL functions, arity-checked. Honors the existing
  `ExtensionPolicy` gating (no registry → `EXTENSIONS_DISABLED`; allowlist/
  denylist respected). Tests: `TestFuzzyTextFunctions` in `tests/test_extensions.py`.
- **S3b — ArangoSearch advisory (done).** `EntityResolver` now records a
  structured `IndexAdvisory(collection, field, …)` (deduped) whenever a fuzzy
  name probe runs against a field with no inverted/ArangoSearch coverage — i.e.
  a full Levenshtein scan. `IndexAdvisory.suggested_inverted_index()` returns a
  ready `add_index` spec (`inverted`, `text_en` analyzer) and `.as_dict()` is
  UI-ready. Exposed via `EntityResolver.advisories`. Mirrors the missing-VCI
  advisory. Tests: `TestArangoSearchAdvisory` in
  `tests/test_nl2cypher_entity_resolution.py`.
- **S3c — create endpoint + UI affordance (done).** End-to-end wiring of the
  advisory: (1) `NL2CypherResult` gained an `advisories: list[dict]` field;
  `nl_to_cypher` reads `resolver.advisories` after resolution and attaches them
  (even when `resolve()` raised — a partial probe may already have flagged a
  missing index). (2) `POST /nl2cypher` forwards `advisories`. (3) New
  `POST /schema/index/create` (session-authenticated) reconstructs the inverted-
  index spec **server-side** from validated `collection`/`field`/`analyzer`
  (the client never sends a free-form spec), is idempotent (`created:false` if
  an inverted index already covers the field), and 404s on a missing collection
  / 400s on a bad name. (4) UI: `client.ts` gains `IndexAdvisory` +
  `createIndex`; `App.tsx` renders an amber "Slow fuzzy match detected" strip
  below the NL input with a per-advisory "Create index" button (creating/
  created/error states), mirroring the `SchemaWarningBanner` action pattern.
  Tests: `tests/test_service_index_create.py`, advisory pass-through in
  `tests/test_service_nl.py`, threading in `tests/test_nl2cypher.py`.

### WP-V1 — Broaden the corpus & guard semantics · *ongoing*
- Add execution-grounded checks (not just transpile-success) for the corpus where
  a live DB is available, asserting result *shape* (path vs scalar) for graph
  questions.
- Expand beyond these 22 with a feature-matrix corpus (every clause/function once).

#### WP-V1a — Text2Cypher corpus harness (large-scale, real LLM Cypher)

A far bigger, execution-grounded benchmark than the 22-query FinReflectKG set:
the neo4j-derived **movies Text2Cypher** corpus (~1,942 real LLM-emitted Cypher
queries against the movies demo graph, which our `movies_pg` fixture matches
exactly). Harness: **`scripts/eval_text2cypher_corpus.py`** — opt-in, reads an
external `--dataset` path (the dataset's redistribution license is pending, so it
is **not** committed), runs the transpiler over the `cypher` column, reports
transpile coverage + ranked failure buckets, and (`--with-db`) executes the
generated AQL against a seeded movies PG DB. Treat the dataset's `cypher` +
Neo4j `results` as the reference; its own machine-translated `aql_query` is not
used.

**Measured baseline (2026-07-01):**

| Metric | Start | After WP-V1a fixes |
| --- | --- | --- |
| Transpile coverage | 85.8% (1666/1942) | **90.4% (1756/1942)** |
| Ran without error (of transpiled) | 95.1% | **97.8%** |

Fixes landed from this corpus (each with regression tests):
- Unlabeled traversal endpoints inferred from relationship domain/range
  (`(m:Movie)<-[:REVIEWED]-()` → Person), COLLECTION-style only.
- Two variable-collision (ERR 1511) classes: back-reference self-cycle and
  unnamed-edge collision across MATCH clauses.
- COLLECT group-var shadowing (`COLLECT director = director.name`).
- ORDER BY on a RETURN alias emitted an unbound `SORT` (collection-not-found).
- Cypher scalar functions inside aggregate arguments (`max(size(x))` → LENGTH).

**Ranked remaining backlog (execution errors, ~39; diminishing returns):**
- **Transpile gaps (higher leverage):** `count{}` / `exists{}` subquery
  expression syntax (**58** — the single biggest bucket; needs grammar+compiler),
  multi-type hops `[:A|B]`, `collect()` nested in `size()`.
- **Execution-correctness tail:** ~14 remaining ERR-1511 (auto edge/node
  namespace pollution + multi-`WITH` scope), `FOR IN <alias>` (iterating a
  `collect()` result), var-lost-after-COLLECT (double-aggregation scope),
  UNWIND-var-lost, a few `unexpected INTO` syntax cases, `date()`.

The largest coverage lever overall (not corpus-specific) remains relaxing the
leading-`MATCH` parser constraint (~1,560 TCK scenarios) — tracked separately as
a deliberate refactor.

---

## 4. Sequencing & milestones

| Milestone | WPs | Corpus result |
| --- | --- | --- |
| **M1 — quick wins** | WP-C1, WP-C2 | 15 → ~18/22 (q01 partial, q03/q04/q06 transpile) |
| **M2 — aggregation** | WP-C3, WP-C4 | ~18 → 22/22 transpile |
| **M3 — semantics** | WP-S1, WP-S2 | "as a graph" + fuzzy correct; q03/q06 intent fixed |
| **M4 — performance/UX** | WP-S3, WP-V1 | ArangoSearch advisory + execution-grounded guards |

Recommended order: **C1 → C2 → C3 → C4** (transpile to 22/22 first, fully
test-guarded), then **S1 → S2 → S3** (semantic correctness + performance).
Each WP is one PR, checkpointed, with its corpus entries promoted in the same PR.

---

## 5. Risks & non-goals

- **Risk:** `collect()` + slice + post-filter interaction is the hardest piece;
  keep WP-C3 narrow (single-level grouping) before nested aggregation.
- **Risk:** ArangoSearch requires analyzers/views that may not exist; WP-S3 must
  degrade gracefully to `CONTAINS`/`=~` when absent.
- **Non-goal (this plan):** write clauses (`CREATE`/`MERGE`/`SET`), `OPTIONAL MATCH`
  semantics beyond current support, and APOC-equivalent procedure coverage —
  track separately.

---

## 6. Open questions for review

1. Priority order — do you want **transpile-completeness (M1+M2) first**, or the
   **"as a graph"/fuzzy semantic fixes (M3) first** since that's the visible demo defect?
2. For fuzzy matching, default to **portable `CONTAINS`/`=~`**, or invest in the
   **ArangoSearch extension path** (WP-S3) as the primary?
3. Should WP-S3's "create index" follow the exact VCI advisory UX, or be a
   separate "Indexing" panel?
