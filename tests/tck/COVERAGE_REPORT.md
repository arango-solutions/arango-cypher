# openCypher TCK coverage — measured

> Headline re-measured 2026-07-05 after single-node `EXISTS`/`COUNT`
> subquery support (WP-V1c). The per-category tables below were last
> refreshed 2026-07-01 and are stale for several categories (a full
> `analyze_coverage.py` re-run now reports materially higher numbers in
> `expressions/{list,map,null,precedence,…}` and `useCases/countingSubgraphMatches`);
> treat the live analyzer output as source of truth until the tables are
> regenerated. The `expressions/existentialSubqueries` line is updated below.

> Measurement date: 2026-07-01 (was 2026-04-20)
> Methodology: translation-only dry run (`python tests/tck/analyze_coverage.py`). Each scenario's main Cypher query is parsed and translated; scenarios that translate successfully (or correctly reject an error-expected input) count as passable. No DB execution — this is an upper bound on what the runner could achieve with a live ArangoDB.

## Headline numbers

| Subset | Passable | Pass rate | (was 2026-04-20) |
|--------|----------|-----------|------------------|
| **Full TCK** (all 3,861 scenarios) | 2,676 / 3,861 | **69.3 %** | 32.2 % |
| **Core TCK** (excludes out-of-scope: `expressions/temporal`, `expressions/quantifier`, `clauses/call` — 2,201 scenarios) | 1,983 / 2,201 | **90.1 %** | 54.8 % |

> 2026-07-07: WP-V1i (chained comparisons, `a < b < c`) added +10
> (Full 2,666→2,676, Core 1,973→1,983); `expressions/comparison` 69→83%.

> 2026-07-07: WP-V1h (`any`/`all`/`none`/`single` list quantifiers) added
> **+574 Full** (2,092→2,666) — `expressions/quantifier` 12→544 (90%), no
> longer meaningfully out-of-scope — and **+42 Core** (1,931→1,973), since
> quantifiers also appear in boolean / match-where scenarios.

> 2026-07-05: WP-V1e/V1f (write-tail combos — `UNWIND … CREATE`/`MERGE`,
> `MATCH … WITH … CREATE`, `MATCH … WITH … MERGE`) added +8 (Full 2,068→2,076,
> Core 1,907→1,915); `clauses/create` 74→82%, `clauses/unwind` 50→71%,
> `clauses/merge` 55→59%. WP-V1g (`RETURN *` / `WITH *` star projection) added
> +16 (Full 2,076→2,092, Core 1,915→1,931).

> 2026-07-05: WP-V1c (single-node `EXISTS`/`COUNT` subquery bodies) took
> `expressions/existentialSubqueries` from 6/10 → 9/10; WP-V1d
> (`WITH`+aggregation counting existentials, via an ANTLR grammar change) took
> it to **10/10**. Combined headline +4 (Full 2,064→2,068, Core 1,903→1,907).

The jump (Core 54.8 % → 86.4 %) came from relaxing the leading-clause
constraint: no-MATCH computational pipelines (leading `WITH`-constants / `UNWIND`
over literals) now translate instead of failing "MATCH is required before WITH"
(that bucket fell 475 → 22). Earlier Wave-8 fixes (unlabeled-endpoint inference,
`EXISTS`/`COUNT` pattern shorthand, COLLECT/ORDER-BY hygiene) contributed the
rest. The remaining full-TCK gap is dominated by the two out-of-scope expression
categories (`temporal`: 1,004 scenarios; `quantifier`: 604) and the
`Unsupported atom` bucket (largely temporal/quantifier expression atoms).

## Top translation-failure reasons (actionable)

Measured 2026-07-01:

| Count | Reason | Implication |
|------:|--------|-------------|
| 539 | `Unsupported atom in v0` | Largely temporal/quantifier expression atoms (out of scope); the rest are `any()`/`all()`/`none()` list predicates and other complex atoms. |
| 372 / 112 / 110 / 66 / 64 / 46 / 34 | `Unsupported function in v0: datetime / time / localtime / *.truncate / duration` | `expressions/temporal` — out of scope. |
| 46 | `Updating clauses are not supported in v0` | Largely closed 2026-07-05 (WP-V1e/V1f): `UNWIND … CREATE/MERGE`, `MATCH … WITH … CREATE`, and `MATCH … WITH … MERGE` now translate. Remaining: `WITH … SET`/`DELETE` (need append-mode mutating builders) + `UNWIND … WITH … <write>`. |
| 36 | `Only one collect(...) is supported in v0` | Multiple `collect()` in one projection. |
| 22 | `MATCH is required before WITH in v0 subset` | Remaining are write-tail combos (need read+write pipeline integration). |
| 20 | `Cypher syntax error … no viable alternative` | ANTLR grammar gaps. |
| 15 | `Unsupported function in v0: count` | Standalone `count()` in a not-yet-accepted context. |
| 13 | `RETURN items required` / `Unsupported expression node: NoneType` | Misc projection edge cases. |

## Category breakdown

### High-coverage (≥ 70 % passable)
| Category | Passable | Pass rate |
|----------|----------|-----------|
| clauses/return-skip-limit | 30 / 31 | 96.8 % |
| expressions/boolean | 130 / 150 | 86.7 % |
| expressions/string | 27 / 32 | 84.4 % |
| clauses/match | 302 / 381 | 79.3 % |
| expressions/graph | 47 / 61 | 77.0 % |
| expressions/pattern | 38 / 50 | 76.0 % |
| clauses/create | 58 / 78 | 74.4 % |
| expressions/aggregation | 26 / 35 | 74.3 % |
| clauses/with-orderBy | 211 / 292 | 72.3 % |

### Medium-coverage (40 – 70 %)
| Category | Passable | Pass rate |
|----------|----------|-----------|
| clauses/return-orderby | 24 / 35 | 68.6 % |
| expressions/typeConversion | 28 / 47 | 59.6 % |
| expressions/path | 4 / 7 | 57.1 % |
| clauses/with-skip-limit | 5 / 9 | 55.6 % |
| clauses/merge | 41 / 75 | 54.7 % |
| clauses/return | 34 / 63 | 54.0 % |
| clauses/union | 6 / 12 | 50.0 % |
| clauses/unwind | 7 / 14 | 50.0 % |
| clauses/set | 24 / 53 | 45.3 % |
| clauses/with | 12 / 29 | 41.4 % |
| clauses/match-where | 14 / 34 | 41.2 % |

### Low-coverage (< 40 %) — biggest room for improvement
| Category | Passable | Pass rate | Notes |
|----------|----------|-----------|-------|
| clauses/remove | 12 / 33 | 36.4 % | `REMOVE` is partial; additional patterns needed. |
| expressions/list | 62 / 185 | 33.5 % | List operators; list comprehension edge cases. |
| expressions/mathematical | 2 / 6 | 33.3 % | Small category, check what's missing. |
| clauses/delete | 11 / 41 | 26.8 % | Advanced `DELETE` patterns. |
| expressions/map | 9 / 44 | 20.5 % | Map constructors in various positions. |
| expressions/literals | 25 / 131 | 19.1 % | Numeric/string literal edge cases. |
| expressions/null | 8 / 44 | 18.2 % | `null`-in-context handling. |
| expressions/existentialSubqueries | 10 / 10 | 100.0 % | Relationship, multi-hop, trailing-`RETURN`, single-node (WP-V1c), and `WITH`+aggregation counting existentials (WP-V1d, ANTLR grammar change) all supported. |
| expressions/comparison | 6 / 72 | 8.3 % | Chained comparisons + type-coercion corners. |
| expressions/conditional | 1 / 13 | 7.7 % | `CASE` expressions in edge-case contexts. |
| clauses/with-where | 1 / 19 | 5.3 % | `WITH` + `WHERE` filter placement edge cases. |
| useCases/countingSubgraphMatches | 0 / 11 | 0.0 % | Specialized subgraph-counting queries. |
| expressions/precedence | 0 / 104 | 0.0 % | Operator-precedence torture tests — systematic gap. |

### Out of scope (excluded from Core TCK)
| Category | Passable | Pass rate | Reason |
|----------|----------|-----------|--------|
| expressions/temporal | 25 / 1,004 | 2.5 % | TCK temporal types not implemented. |
| expressions/quantifier | 544 / 604 | 90.1 % | **Implemented 2026-07-07 (WP-V1h)** — `any`/`all`/`none`/`single(x IN list WHERE …)` lower to count-subquery tests; no longer out-of-scope. Remaining ~60 are `null`-list three-valued-logic edge cases (AQL treats a null predicate as false). |
| clauses/call | 2 / 52 | 3.8 % | `CALL` procedure syntax not implemented (handled via `arango.*` extensions, not TCK `CALL`). |

## How to reproduce

```bash
python tests/tck/analyze_coverage.py
```

No DB needed; takes ~10 seconds over 220 feature files.

For an end-to-end measurement (requires live ArangoDB):

```bash
docker compose up -d
RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck
```

End-to-end numbers will be lower than translation-only numbers because the runner still has to seed the graph from the `Given` steps and normalize results against Neo4j conventions (see `tests/tck/normalize.py`). Translation-only is the primary metric tracked here because it isolates the transpiler from the surrounding harness.

## Prioritized follow-ups (if/when TCK uplift is prioritized again)

1. **Accept non-MATCH leading clauses** (≈ +1,560 scenarios now blocked at the leading-clause guard). Standalone `CREATE`, `WITH`, `UNWIND` at top of query. Single largest unlock.
2. **Multi-type relationships** `-[:A|B]->` (≈ +84 scenarios). Minor translator change — emit a multi-collection traversal or filtered `ANY`.
3. **Typeless relationships** `-[r]-` (≈ +102 scenarios). Requires iterating all edge collections or using a union subquery — non-trivial but tractable.
4. **Operator-precedence corpus** (104 scenarios at 0 %). Likely a targeted cluster of grammar rules; investigate whether ANTLR grammar fidelity is the issue.
5. **Map / literal / null / comparison expression edges** (≈ 230 scenarios combined under 20 %). Long tail; each is a small translator fix.

None of these are currently on the v0.4 plan. They are listed here purely as a triage-ready backlog for a future TCK-uplift sprint.
