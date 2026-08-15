# Cypher Specification Coverage Assessment

**Assessment date:** 2026-08-06  
**Project version:** 0.2.0  
**Scope:** `arango_cypher.translate_v0` translating Cypher to AQL

For the current, generated category-level measurement, see
[`tests/tck/COVERAGE_REPORT.md`](../tests/tck/COVERAGE_REPORT.md). For the
feature-level compatibility contract, see
[`docs/cypher_capability_matrix.md`](cypher_capability_matrix.md).

## Executive assessment

The transpiler has strong coverage of the read-oriented, expression-oriented
openCypher surface that is exercised by the bundled TCK corpus: **2,676 of
3,861 scenarios (69.3%)** translate successfully or are correctly rejected
when the scenario expects an error. Removing the deliberately excluded
temporal and procedure categories yields **2,527 of 2,805 scenarios
(90.1%)**.

Those figures are useful progress indicators, but they are **not a claim of
openCypher conformance**.  The measurement is a translation-only dry run:
it does not run the generated AQL, compare results or side effects with a
reference Cypher implementation, or cover every Cypher dialect feature.
The project should therefore be described as supporting a broad,
mapping-aware Cypher-to-AQL subset, rather than as an openCypher-compliant
implementation.

The largest remaining product gaps are temporal values/functions, `CALL`
procedures, the harder write/transaction shapes, and certain graph-pattern
forms that cannot be faithfully or efficiently lowered to AQL without
additional mapping/runtime work.

## Measurement method and interpretation

The figures below were generated on 2026-08-06 with:

```bash
./.venv/bin/python tests/tck/analyze_coverage.py
```

The analyzer:

1. expands all bundled Gherkin scenarios;
2. builds an LPG mapping inferred from each scenario;
3. translates the scenario's main Cypher query;
4. records parseable, translatable, and correctly-rejected scenarios
   separately.

It evaluates **translation feasibility**, not runtime equivalence.  In
particular, it does not verify AQL syntax against a server, graph setup,
result rows/types/order, mutations, null behavior, or transaction semantics.
Fifty scenarios using procedure setup steps are additionally skipped by the
harness.  Consequently, the rates are an upper bound on end-to-end
compatibility, not a certification result.

The Core subset excludes only `expressions/temporal` and `clauses/call`.
Quantifiers are included because 544/604 scenarios translate, making the
denominator representative of the current product surface.

## Overall results

| Population | Passable | Rate | Interpretation |
| --- | ---: | ---: | --- |
| Full bundled TCK corpus | 2,676 / 3,861 | **69.3%** | Broad translation coverage, reduced chiefly by temporal and procedure gaps. |
| Core subset | 2,527 / 2,805 | **90.1%** | High coverage excluding only temporal and procedure categories. |
| Temporal expressions | 147 / 1,004 | 14.6% | Major intentional gap. |
| List quantifiers | 544 / 604 | 90.1% | Common forms work; null three-valued-logic edges remain. |
| `CALL` clauses | 2 / 52 | 3.8% | Procedure syntax/semantics are not implemented. |

## Secondary validation evidence

The TCK is the broadest specification-oriented corpus, but several smaller
corpora exercise more realistic mappings and, in some cases, execution:

| Corpus | Current result | What it demonstrates |
| --- | --- | --- |
| Golden translation fixtures | 214 cases across 35 files | Expected AQL generation for focused syntax and mapping cases. |
| FinReflectKG | 22 / 22 translate | Domain-specific financial graph queries against an LPG-style mapping. |
| Movies cross-validation | 20 / 20 | Query results compared with Neo4j for the checked-in corpus. |
| Northwind cross-validation | 14 / 14 | Query results compared with Neo4j for the checked-in corpus. |
| External Movies Text2Cypher corpus | 1,756 / 1,942 translate (90.4%); 97.8% of translated queries ran without error | Large, real LLM-emitted query sample; the source dataset is not committed. |

These results increase confidence in common query paths, but they are not
substitutes for TCK end-to-end equivalence.  The full TCK execution suite is
opt-in (`RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck`), and the default CI unit
job explicitly excludes both integration and TCK markers.

## Coverage by specification area

### Strongly covered (at least 90%)

| Area | Passable | Assessment |
| --- | ---: | --- |
| Boolean expressions | 150 / 150 | Full corpus coverage. |
| Conditional expressions | 13 / 13 | Full corpus coverage. |
| Map expressions | 44 / 44 | Full corpus coverage. |
| Mathematical expressions | 6 / 6 | Full corpus coverage. |
| Null expressions | 44 / 44 | Full corpus coverage in dry run. |
| Operator precedence | 104 / 104 | Full corpus coverage. |
| String expressions | 32 / 32 | Full corpus coverage. |
| Type conversion | 47 / 47 | Full corpus coverage. |
| `MATCH` | 371 / 381 | High, but not complete. |
| `MATCH ... WHERE` | 32 / 34 | High, but not complete. |
| Graph expressions | 60 / 61 | High, but not complete. |
| Pattern expressions | 48 / 50 | High, but not complete. |
| List expressions | 173 / 185 | Strong, with a remaining long tail. |
| Literal expressions | 120 / 131 | Strong, with edge cases remaining. |
| `RETURN` with skip/limit | 31 / 31 | Full corpus coverage. |
| Counting subgraph matches | 11 / 11 | Full corpus coverage. |

### Substantially covered (70–89%)

| Area | Passable | Assessment |
| --- | ---: | --- |
| `WITH` + skip/limit | 8 / 9 | One remaining scenario. |
| `WITH` + order by | 254 / 292 | Strong pipeline coverage; aggregation/scope corners remain. |
| `UNWIND` | 12 / 14 | Most standard forms translate. |
| `UNION` / `UNION ALL` | 10 / 12 | Small residual gap. |
| `RETURN` | 52 / 63 | Projection edge cases remain. |
| `RETURN` + order by | 28 / 35 | Alias/projection corners remain. |
| Aggregation | 29 / 35 | `collect` and nested aggregation shapes remain. |
| `CREATE` | 64 / 78 | Common creation forms work; complex/multi-step writes do not. |
| `REMOVE` | 27 / 33 | Partial mutation support. |
| Path expressions | 5 / 7 | Small but material gap. |
| Comparison expressions | 60 / 72 | Chained comparisons work; type/coercion corners remain. |

### Partial coverage (below 70%)

| Area | Passable | Primary limitation |
| --- | ---: | --- |
| `WITH ... WHERE` | 12 / 19 | Filter placement and scoped pipeline edges. |
| `DELETE` | 27 / 41 | Advanced mutation forms and AQL write constraints. |
| `SET` | 34 / 53 | Append-mode and multi-part write gaps. |
| `MERGE` | 44 / 75 | Complex/multi-hop merge and mutation combinations. |
| `CALL` | 2 / 52 | Neo4j procedure model is not implemented. |
| Temporal expressions | 147 / 1,004 | Temporal type/function model is not implemented. |

## Supported surface with important qualifications

The translator has demonstrated support for:

- `MATCH`, `OPTIONAL MATCH`, multiple match parts, directed and bounded
  variable-length traversals, mapped labels/properties, and relationship
  property constraints;
- `WHERE` boolean logic, comparisons (including chained comparisons),
  arithmetic, null tests, string predicates, regex, label predicates, and
  list predicates;
- `WITH`/`RETURN`, aliases, distinct, order/skip/limit, star projection,
  grouping, and common aggregations including `collect`;
- `UNWIND`, `UNION`, `EXISTS { ... }`, `COUNT { ... }`, and list
  quantifiers (`any`, `all`, `none`, `single`) for common non-null inputs;
- common write patterns for `CREATE`, `MERGE`, `SET`, `REMOVE`, `DELETE`,
  and `FOREACH`, including several `MATCH`/`WITH`/`UNWIND` write tails;
- mapping-aware physical layouts (PG, LPG, and hybrid) and Arango-specific
  extensions where explicitly enabled.

Support is conditional on a suitable `MappingBundle`.  A Cypher construct
that translates against the generated TCK LPG mapping may still be rejected
for a real schema when its labels, relationship types, or collections cannot
be resolved unambiguously.

## Material gaps and semantic risks

### 1. Temporal types and functions

Temporal support is the dominant numeric gap.  Unsupported functions include
`datetime`, `time`, `localtime`, `duration`, and truncation variants.  This
is not a small parser omission: faithful support requires decisions about
Cypher temporal values, time zones, duration arithmetic, parameter coercion,
and AQL result conversion.

### 2. Procedures and `CALL`

The project does not implement the Neo4j procedure model.  It exposes
opt-in `arango.*` extensions rather than attempting to emulate `db.*` or
APOC procedures.  Generic `CALL`, `CALL { ... }` beyond supported query
forms, procedure setup, and procedure result contracts should be considered
unsupported.

### 3. Write completeness and transaction semantics

The current write support is valuable but incomplete.  Remaining failures
cluster around multi-part pipelines such as `WITH ... SET`/`DELETE`,
`UNWIND ... WITH ... <write>`, complex/multi-hop `MERGE`, and nested write
forms.  Even when a shape translates, ArangoDB's constraints on repeated
data modification within an AQL query differ from Neo4j's transaction model.
Translation success must not be interpreted as equivalent transactional or
side-effect behavior.

### 4. Graph-pattern portability

Known structural gaps include multiple relationship types per hop
(`[:A|B]`) and multi-label nodes without an appropriate label mapping.
Typeless relationships have constrained inference support, but can still
require a potentially expensive search across edge collections.  These are
not merely grammar features: their correct AQL lowering depends on the
physical graph layout and may have performance consequences.

### 5. Three-valued logic and result semantics

Quantifier predicates cover the common case but do not reproduce every
Cypher null behavior: AQL treats a null predicate as false in ways that
differ from Cypher's three-valued semantics.  Other areas that require
execution-grounded validation include `OPTIONAL MATCH`, path values,
ordering, numeric/type coercion, and graph value serialization.

### 6. General `reduce`

The parser accepts `reduce`, but implementation is intentionally limited to
numeric sum-fold forms.  Product/multi-accumulator/string folds are rejected
because AQL has no direct general fold equivalent.

## Evidence quality and documentation drift

The repository contains two older coverage documents that should not be used
as the current baseline without qualification:

- `tests/tck/COVERAGE_REPORT.md` correctly states that its category tables
  were stale; its tables predate subsequent translator improvements.
- `tests/tck/SKIP_REASONS.md` describes an older, narrow live `MATCH` run
  (426 scenarios) and lists several features that are now covered.

The runtime profile in `arango_cypher/profile.py` also understates current
write support by listing all write clauses as not yet supported while the
translator and TCK dry run demonstrate partial support.  The profile should
be updated before it is used as a machine-readable capability contract.

## Recommended next measurements

1. **Regenerate the checked-in TCK report from the current analyzer output.**
   This report establishes the current baseline; the existing category table
   does not.
2. **Promote the metric from translation-only to execution-grounded.**
   Run supported scenarios against ArangoDB and a reference Cypher engine,
   compare rows, types, ordering, errors, and side effects.
3. **Split reporting into four outcomes:** parses, translates, executes, and
   is semantically equivalent.  A single "passable" value hides too much.
4. **Revise the Core denominator.**  Quantifiers should either join Core or
   be reported separately as a mostly-supported capability; procedure and
   temporal exclusions should remain explicit product decisions.
5. **Create a feature-level capability matrix** from the profile, targeted
   tests, TCK outcomes, and known semantic caveats.  Treat it as the source
   for documentation, APIs, and future Bolt/driver compatibility claims.
6. **Make TCK execution visible in automation.** Keep `REMOVE` covered by
   the `SET` TCK runner, explicitly document `CALL` as excluded, and publish
   an opt-in end-to-end result as a CI artifact or scheduled job.

## Bottom line

The project has moved well beyond a prototype parser: the current dry-run
evidence supports a strong claim for a broad, mapping-aware Cypher-to-AQL
translator, especially for read queries, expressions, projection/pipeline
work, existential subqueries, and common graph operations.

It is not yet appropriate to claim openCypher specification conformance or
general Neo4j compatibility.  The next quality step is not simply raising
the 69.3% translation rate; it is validating that the already-translated
majority executes with Cypher-equivalent semantics and documenting the
unsupported behavior as a precise compatibility contract.
