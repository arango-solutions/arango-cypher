"""Micro-benchmark for ``arango_cypher.translate()``.

Closes the PRD §7.7 "not benchmarked yet" gap with a reproducible measurement of
parse + translate latency across a representative query corpus.

The benchmark deliberately disables the translation LRU cache between iterations
(each iteration uses a fresh ``Cypher`` string by appending an iteration-unique
no-op literal) so we measure cold-path latency — the worst case the transpiler
presents to callers that do not repeat queries. A separate warm-cache row
measures the LRU-hit path, which is what long-running services experience in
production after the cache has filled.

Usage:

    python scripts/benchmark_translate.py              # text report (stdout)
    python scripts/benchmark_translate.py --json       # JSON report (stdout)
    python scripts/benchmark_translate.py --iter 2000  # more iterations

The test counterpart ``tests/test_translate_perf.py`` runs a shorter version of
the same corpus under ``RUN_PERF=1`` and asserts the PRD §2.1 single-hop target
of P95 < 50 ms.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arango_cypher import translate
from arango_cypher.service import _mapping_from_dict

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "mappings"


def _load_mapping(name: str):
    payload = json.loads((_FIXTURE_ROOT / f"{name}.export.json").read_text(encoding="utf-8"))
    bundle = _mapping_from_dict(payload)
    if bundle is None:
        raise RuntimeError(f"fixture {name} did not produce a MappingBundle")
    return bundle


_CORPUS: dict[str, tuple[str, str]] = {
    "single_hop_simple_match": (
        "movies_pg",
        'MATCH (p:Person {name: "Tom Hanks"}) RETURN p.name, p.born',
    ),
    "single_hop_with_relationship": (
        "movies_pg",
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name, m.title LIMIT 10",
    ),
    "two_hop": (
        "movies_pg",
        "MATCH (p:Person)-[:DIRECTED]->(m:Movie)<-[:ACTED_IN]-(a:Person) "
        'WHERE p.name = "Clint Eastwood" RETURN a.name',
    ),
    "variable_length": (
        "movies_pg",
        'MATCH (p:Person {name: "Tom Hanks"})-[:ACTED_IN*1..3]-(m:Movie) RETURN m.title LIMIT 20',
    ),
    "aggregation": (
        "movies_pg",
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) "
        "RETURN p.name, COUNT(m) AS movies ORDER BY movies DESC LIMIT 10",
    ),
    "with_pipeline": (
        "movies_pg",
        "MATCH (m:Movie) WITH m WHERE m.released > 2000 "
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name, m.title LIMIT 25",
    ),
    "optional_match": (
        "movies_pg",
        'MATCH (p:Person {name: "Tom Hanks"}) '
        "OPTIONAL MATCH (p)-[:DIRECTED]->(m:Movie) RETURN p.name, m.title",
    ),
    "write_create": (
        "movies_pg",
        'CREATE (p:Person {name: "Benchmark Actor", born: 1990}) RETURN p',
    ),
    "single_hop_lpg": (
        "movies_lpg",
        'MATCH (p:Person {name: "Tom Hanks"})-[:ACTED_IN]->(m:Movie) RETURN m.title',
    ),
    "single_hop_lpg_naked": (
        "movies_lpg_naked",
        'MATCH (p:Person {name: "Tom Hanks"})-[:ACTED_IN]->(m:Movie) RETURN m.title',
    ),
}


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (inclusive). Sorts a copy of `values`."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return ordered[k]


def _measure(
    cypher: str,
    mapping,
    *,
    iterations: int,
    warm_cache: bool,
) -> list[float]:
    samples_ms: list[float] = []
    if warm_cache:
        translate(cypher, mapping=mapping)
        for _ in range(iterations):
            t0 = time.perf_counter()
            translate(cypher, mapping=mapping)
            samples_ms.append((time.perf_counter() - t0) * 1000.0)
    else:
        for i in range(iterations):
            unique = f"{cypher} /*#{i}*/"
            t0 = time.perf_counter()
            translate(unique, mapping=mapping)
            samples_ms.append((time.perf_counter() - t0) * 1000.0)
    return samples_ms


def _summarize(name: str, samples: list[float]) -> dict[str, Any]:
    return {
        "name": name,
        "n": len(samples),
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": _percentile(samples, 95),
        "p99_ms": _percentile(samples, 99),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "stdev_ms": statistics.pstdev(samples),
    }


def run(iterations: int) -> dict[str, Any]:
    mappings: dict[str, Any] = {}
    reports_cold: list[dict[str, Any]] = []
    reports_warm: list[dict[str, Any]] = []
    for case_name, (fixture, cypher) in _CORPUS.items():
        if fixture not in mappings:
            mappings[fixture] = _load_mapping(fixture)
        mapping = mappings[fixture]
        cold = _measure(cypher, mapping, iterations=iterations, warm_cache=False)
        warm = _measure(cypher, mapping, iterations=iterations, warm_cache=True)
        reports_cold.append(_summarize(case_name, cold))
        reports_warm.append(_summarize(case_name, warm))
    return {
        "iterations_per_case": iterations,
        "cold_cache": reports_cold,
        "warm_cache": reports_warm,
    }


def _format_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(
        f"arango_cypher.translate() micro-benchmark  ({report['iterations_per_case']} iterations per case)"
    )
    lines.append("")
    header = f"{'case':<32} {'mean':>8} {'p50':>8} {'p95':>8} {'p99':>8} {'min':>8} {'max':>8}  (ms)"

    def _fmt_block(title: str, rows: list[dict[str, Any]]) -> None:
        lines.append(title)
        lines.append("-" * len(header))
        lines.append(header)
        lines.append("-" * len(header))
        for r in rows:
            lines.append(
                f"{r['name']:<32} "
                f"{r['mean_ms']:>8.2f} {r['median_ms']:>8.2f} "
                f"{r['p95_ms']:>8.2f} {r['p99_ms']:>8.2f} "
                f"{r['min_ms']:>8.2f} {r['max_ms']:>8.2f}"
            )
        lines.append("")

    _fmt_block("COLD CACHE (unique cypher per iteration — worst case)", report["cold_cache"])
    _fmt_block("WARM CACHE (LRU hit — long-running service steady state)", report["warm_cache"])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--iter", type=int, default=500, help="iterations per case (default 500)")
    ap.add_argument("--json", action="store_true", help="emit JSON rather than a text table")
    args = ap.parse_args()

    report = run(args.iter)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
