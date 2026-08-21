# openCypher TCK coverage

> Generated: 2026-08-06
> Command: `./.venv/bin/python tests/tck/render_coverage_report.py --write`

## Scope and limitations

Dry-run results measure parse and translation feasibility for the bundled TCK corpus.
When execution data is supplied, the report also records AQL execution and whether it
satisfies the TCK's declared result/error assertion. That assertion is not a direct
per-scenario Neo4j comparison; use the Neo4j cross-validation suites for that evidence.

A scenario that is expected by the TCK to fail counts as a correct rejection when the parser
or translator rejects it. The Core subset excludes only `clauses/call`, `expressions/temporal`; list quantifiers are included because they are substantially implemented.

## Four-outcome dashboard

| Population | Parses | Translates | Correctly rejects | Executes | TCK assertion passed (executed) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full (3861 scenarios) | 3709 (96.1%) | 2409 (62.4%) | 267 (6.9%) | 2408 (62.4%) | 1296 (33.6%) |
| Core (2805 scenarios) | 2703 (96.4%) | 2262 (80.6%) | 265 (9.4%) | 2261 (80.6%) | 1288 (45.9%) |

The headline dry-run passability (translation or correct rejection) is **2676 / 3861 (69.3%)** for Full and **2527 / 2805 (90.1%)** for Core.

## Category breakdown

| Category | Parseable | Translates | Correct rejections | Passable | Rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| `clauses/call` (out of Core) | 2 / 52 | 0 / 52 | 2 / 52 | 2 / 52 | 3.8% |
| `clauses/create` | 78 / 78 | 61 / 78 | 3 / 78 | 64 / 78 | 82.1% |
| `clauses/delete` | 41 / 41 | 25 / 41 | 2 / 41 | 27 / 41 | 65.9% |
| `clauses/match` | 379 / 381 | 271 / 381 | 100 / 381 | 371 / 381 | 97.4% |
| `clauses/match-where` | 34 / 34 | 31 / 34 | 1 / 34 | 32 / 34 | 94.1% |
| `clauses/merge` | 75 / 75 | 33 / 75 | 11 / 75 | 44 / 75 | 58.7% |
| `clauses/remove` | 33 / 33 | 27 / 33 | 0 / 33 | 27 / 33 | 81.8% |
| `clauses/return` | 63 / 63 | 46 / 63 | 6 / 63 | 52 / 63 | 82.5% |
| `clauses/return-orderby` | 35 / 35 | 25 / 35 | 3 / 35 | 28 / 35 | 80.0% |
| `clauses/return-skip-limit` | 31 / 31 | 31 / 31 | 0 / 31 | 31 / 31 | 100.0% |
| `clauses/set` | 53 / 53 | 34 / 53 | 0 / 53 | 34 / 53 | 64.2% |
| `clauses/union` | 12 / 12 | 10 / 12 | 0 / 12 | 10 / 12 | 83.3% |
| `clauses/unwind` | 14 / 14 | 12 / 14 | 0 / 14 | 12 / 14 | 85.7% |
| `clauses/with` | 29 / 29 | 19 / 29 | 2 / 29 | 21 / 29 | 72.4% |
| `clauses/with-orderBy` | 292 / 292 | 215 / 292 | 39 / 292 | 254 / 292 | 87.0% |
| `clauses/with-skip-limit` | 9 / 9 | 8 / 9 | 0 / 9 | 8 / 9 | 88.9% |
| `clauses/with-where` | 19 / 19 | 12 / 19 | 0 / 19 | 12 / 19 | 63.2% |
| `expressions/aggregation` | 35 / 35 | 22 / 35 | 7 / 35 | 29 / 35 | 82.9% |
| `expressions/boolean` | 150 / 150 | 81 / 150 | 69 / 150 | 150 / 150 | 100.0% |
| `expressions/comparison` | 64 / 72 | 60 / 72 | 0 / 72 | 60 / 72 | 83.3% |
| `expressions/conditional` | 13 / 13 | 13 / 13 | 0 / 13 | 13 / 13 | 100.0% |
| `expressions/existentialSubqueries` | 9 / 10 | 9 / 10 | 1 / 10 | 10 / 10 | 100.0% |
| `expressions/graph` | 61 / 61 | 60 / 61 | 0 / 61 | 60 / 61 | 98.4% |
| `expressions/list` | 185 / 185 | 172 / 185 | 1 / 185 | 173 / 185 | 93.5% |
| `expressions/literals` | 101 / 131 | 101 / 131 | 19 / 131 | 120 / 131 | 91.6% |
| `expressions/map` | 44 / 44 | 44 / 44 | 0 / 44 | 44 / 44 | 100.0% |
| `expressions/mathematical` | 5 / 6 | 5 / 6 | 1 / 6 | 6 / 6 | 100.0% |
| `expressions/null` | 44 / 44 | 44 / 44 | 0 / 44 | 44 / 44 | 100.0% |
| `expressions/path` | 7 / 7 | 5 / 7 | 0 / 7 | 5 / 7 | 71.4% |
| `expressions/pattern` | 50 / 50 | 48 / 50 | 0 / 50 | 48 / 50 | 96.0% |
| `expressions/precedence` | 104 / 104 | 104 / 104 | 0 / 104 | 104 / 104 | 100.0% |
| `expressions/quantifier` | 544 / 604 | 544 / 604 | 0 / 604 | 544 / 604 | 90.1% |
| `expressions/string` | 32 / 32 | 32 / 32 | 0 / 32 | 32 / 32 | 100.0% |
| `expressions/temporal` (out of Core) | 1004 / 1004 | 147 / 1004 | 0 / 1004 | 147 / 1004 | 14.6% |
| `expressions/typeConversion` | 47 / 47 | 47 / 47 | 0 / 47 | 47 / 47 | 100.0% |
| `useCases/countingSubgraphMatches` | 11 / 11 | 11 / 11 | 0 / 11 | 11 / 11 | 100.0% |

## Top dry-run blockers

| Count | Failure reason |
| ---: | --- |
| 372 | `Unsupported function in v0: datetime` |
| 112 | `Unsupported function in v0: time` |
| 110 | `Unsupported function in v0: localtime` |
| 66 | `Unsupported function in v0: datetime.truncate` |
| 64 | `Unsupported function in v0: duration` |
| 46 | `Unsupported function in v0: localdatetime.truncate` |
| 34 | `Unsupported function in v0: date.truncate` |
| 33 | `Updating clauses are not supported in v0` |
| 22 | `MATCH is required before WITH in v0 subset` |
| 20 | `Cypher syntax error at 11:31: no viable alternative at input` |
| 15 | `Unsupported function in v0: count` |
| 13 | `Unsupported expression node: NoneType` |
| 13 | `Unsupported function in v0: time.truncate` |
| 12 | `Unsupported function in v0: localtime.truncate` |
| 11 | `Unsupported function in v0: collect` |

## Harness exclusions

| Count | Reason |
| ---: | --- |
| 50 | procedure step |

## Reproduce

```bash
# Translation-only metrics and checked-in report
./.venv/bin/python tests/tck/analyze_coverage.py
./.venv/bin/python tests/tck/render_coverage_report.py --write

# Execution + TCK assertion outcomes (requires ArangoDB)
RUN_INTEGRATION=1 ./.venv/bin/python tests/tck/analyze_execution.py > tck-execution.json
./.venv/bin/python tests/tck/render_coverage_report.py --execution-json tck-execution.json --write

# Reference-engine comparison for fixture corpora (requires ArangoDB + Neo4j)
RUN_INTEGRATION=1 RUN_CROSS=1 pytest -m cross
```
