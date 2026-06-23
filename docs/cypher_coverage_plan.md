# Cypher Coverage Completion Plan

**Status:** In progress — transpiler coverage (M1+M2) **complete: 22/22**. Semantics phase (M3+) pending.
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

- **Label predicate on an untyped variable.** `MATCH (risk) … WHERE risk:RISK_FACTOR
  OR risk:EVENT` (q10/q11/q20) compiles to a no-op `(risk OR risk)` because `risk`
  has no label in the pattern, so the discriminator collection/field is unknown at
  compile time. Should resolve to a `risk.type IN [...]`-style filter once the
  resolver can attribute a collection to the bare variable. → folds into **WP-S2**.
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

### WP-S2 — Approximate entity matching · *M, ~2–3 days*
- Resolver: when a string property's `valueShape` is token/short or the exact
  probe misses, emit `CONTAINS`/`=~` (or ArangoSearch when available) instead of
  equality; prefer ticker/`id` match when the mention contains a parenthesized
  symbol like "(CINF)".
- Surface `valueShape` (2.a) in the schema card so the LLM stops inventing legal
  names for token-shaped fields.
- **Improves:** q03/q04/q06/q07 correctness.

### WP-S3 — ArangoSearch index advisory + one-click create (2.c) · *M, ~2–3 days*
- Detect when name-matching falls back to a scan and no analyzer/view exists;
  emit a structured advisory (mirrors the VCI-missing advisory).
- Backend endpoint + UI affordance to create an ArangoSearch View / inverted
  index on the chosen property (consistent with the VCI "offer to create" UX).
- Add `arango.search`/`arango.ngram_match` extension compilers for opt-in fuzzy.

### WP-V1 — Broaden the corpus & guard semantics · *ongoing*
- Add execution-grounded checks (not just transpile-success) for the corpus where
  a live DB is available, asserting result *shape* (path vs scalar) for graph
  questions.
- Expand beyond these 22 with a feature-matrix corpus (every clause/function once).

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
