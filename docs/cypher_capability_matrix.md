# Cypher Capability Matrix

**Authoritative measurement:** `tests/tck/COVERAGE_REPORT.md`  
**Machine-readable declaration:** `arango_cypher/profile.py`  
**Last synchronized:** 2026-08-06

This matrix is the compatibility contract for documentation, API consumers,
and future protocol adapters. A supported item can still be conditional on a
resolvable `MappingBundle` and the physical Arango graph layout.

| Capability | Parse/translate evidence | Runtime/equivalence evidence | Status | Material limits |
| --- | --- | --- | --- | --- |
| `MATCH`, multiple match parts, directed/bounded variable length | TCK `clauses/match`: 371/381 passable | Movies/Northwind cross-validation: 34/34 passed on 2026-08-06 | Supported | Zero-hop and mapping-specific edges need execution verification. |
| `OPTIONAL MATCH` | Included in match coverage | Historical TCK result mismatches | Partial | Null-extension/value semantics have a remaining tail. |
| Labels, relationship types, mapped properties | TCK graph/pattern: 60/61 and 48/50 | Golden fixtures + cross-validation | Supported | Requires an unambiguous mapping. |
| Typeless relationships | Targeted regression tests | No corpus-wide equivalence rate | Constrained | May require expensive edge-collection inference. |
| Multi-type relationship hop `[:A\|B]` | Explicit fail-fast | — | Unsupported | Requires multi-collection traversal/filter lowering. |
| `WHERE`, boolean, comparisons, null, strings | 100% boolean/null/string; 83.3% comparison | Golden fixtures | Supported | Quantifier null three-valued logic differs in edge cases. |
| Lists, maps, literals, precedence | 93.5%, 100%, 91.6%, 100% respectively | Golden fixtures | Supported | Long-tail literal/list edge cases remain. |
| `CASE`, arithmetic, type conversion | 100% in dry run | Golden fixtures | Supported | AQL type coercion may differ for unusual values. |
| `RETURN`, aliases, distinct, order, skip, limit | 80–100% by TCK category | Golden fixtures | Supported | Projection and alias edge cases remain. |
| `WITH`, aggregation, star projection | 63–89% by TCK category | Golden fixtures | Partial | Multi-aggregation/scope and filter-placement shapes remain. |
| `UNWIND`, `UNION` | 85.7%, 83.3% | Golden fixtures | Partial | Multi-part write-pipeline combinations remain. |
| `EXISTS {}` / `COUNT {}` pattern subqueries | 10/10 | Dedicated tests | Supported | Complex multi-`WITH`/non-count aggregation bodies reject clearly. |
| List quantifiers `any`/`all`/`none`/`single` | 544/604 (90.1%) | Dedicated tests | Partial | Null predicate behavior differs from Cypher three-valued logic. |
| `reduce()` | Dedicated tests | — | Constrained | Numeric sum-fold only; general folds reject. |
| `CREATE`, `MERGE`, `SET`, `REMOVE`, `DELETE`, `FOREACH` | 58.7–82.1% by relevant TCK category | Golden fixtures; limited TCK execution | Partial | Complex/multi-hop/pipeline forms and Arango multi-write constraints. |
| `WITH … SET/DELETE/REMOVE` | Dedicated write-tail tests | — | Supported | Requires a MATCH-bound document variable (computed WITH projections reject). |
| `UNWIND … WITH … <write>` | Explicit `NOT_IMPLEMENTED` tests | — | Unsupported | Needs leading-UNWIND multi-part write routing. |
| `CALL` procedures | 2/52 (3.8%) | — | Unsupported | `arango.*` opt-in extensions are not Neo4j procedure compatibility. |
| Temporal values/functions | 147/1,004 (14.6%) | — | Unsupported | Requires a temporal type/coercion/result model. |
| Named dollar parameters | Profile + TCK | Golden fixtures | Supported | Positional parameters are unsupported. |

## Evidence levels

- **Supported**: broad translation coverage plus focused regression or
  execution/cross-validation evidence.
- **Partial**: accepts a useful subset, but known valid Cypher forms remain
  unsupported or have unverified semantic edges.
- **Constrained**: intentionally narrow implementation with clear rejection
  outside the accepted shape.
- **Unsupported**: rejected at validation/translation; do not generate it
  without a fallback.

## Measurement contract

The four outcomes are intentionally separate:

1. **Parses** — accepted by the ANTLR grammar and parser normalizations.
2. **Translates** — generates AQL from the TCK's dynamic LPG mapping.
3. **Executes** — generated AQL runs against a live ArangoDB in the TCK
   harness.
4. **TCK assertion passed** — execution matches the TCK's declared
   result/error assertion. This is a specification-test proxy, not proof of
   semantic equivalence against a reference engine. For direct Neo4j
   comparison, use the separately marked `cross` suites.

Regenerate dry-run data with:

```bash
./.venv/bin/python tests/tck/render_coverage_report.py --write
```

Run execution metrics with:

```bash
RUN_INTEGRATION=1 ./.venv/bin/python tests/tck/analyze_execution.py > tck-execution.json
```
