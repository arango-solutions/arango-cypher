"""Soft perf budget for ``arango_cypher.translate()`` (PRD §2.1 / §7.7).

Opt-in via ``RUN_PERF=1`` so the regular test suite is unaffected. The assertions
are deliberately loose — they guard against an order-of-magnitude regression, not
micro-variations. The primary artifact is the measurement report emitted by the
sibling CLI (``scripts/benchmark_translate.py``).

Budget source: PRD §2.1 ("< 50 ms for single-hop queries") + measured headroom
from Wave 4p baseline (2026-04-20):

    cold P95 (worst case across corpus) : 2.74 ms
    warm P95 (LRU hit)                  : 0.05 ms

We assert 25 ms / 1 ms — 9x and 20x above the 2026-04-20 baseline — so
the test only fires on a real regression (GC pause, N^2 blow-up, ANTLR
state churn) and not on shared-runner noise.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arango_cypher import translate  # noqa: E402
from scripts.benchmark_translate import _CORPUS, _load_mapping, _percentile  # noqa: E402

_ITERATIONS = 100


@pytest.mark.skipif(
    os.getenv("RUN_PERF") != "1",
    reason="perf budget gate — opt in with RUN_PERF=1 (runs in ~10s)",
)
class TestTranslatePerfBudget:
    """Enforce order-of-magnitude regression guardrails on translate() latency."""

    @pytest.fixture(scope="class")
    def mappings(self):
        return {
            fixture: _load_mapping(fixture) for fixture, _ in {v[0]: None for v in _CORPUS.values()}.items()
        }

    def _measure_cold(self, cypher: str, mapping) -> list[float]:
        samples: list[float] = []
        for i in range(_ITERATIONS):
            unique = f"{cypher} /*#perf{i}*/"
            t0 = time.perf_counter()
            translate(unique, mapping=mapping)
            samples.append((time.perf_counter() - t0) * 1000.0)
        return samples

    def _measure_warm(self, cypher: str, mapping) -> list[float]:
        translate(cypher, mapping=mapping)
        samples: list[float] = []
        for _ in range(_ITERATIONS):
            t0 = time.perf_counter()
            translate(cypher, mapping=mapping)
            samples.append((time.perf_counter() - t0) * 1000.0)
        return samples

    def test_cold_cache_p95_under_budget(self, mappings):
        """Cold-cache P95 across the full corpus must stay under 25 ms per case."""
        regressions = []
        for case_name, (fixture, cypher) in _CORPUS.items():
            samples = self._measure_cold(cypher, mappings[fixture])
            p95 = _percentile(samples, 95)
            mean = statistics.fmean(samples)
            if p95 > 25.0:
                regressions.append(f"{case_name}: P95={p95:.2f}ms mean={mean:.2f}ms (budget 25ms)")
        assert not regressions, (
            "Cold-cache translate() P95 regressed past the 25ms budget "
            "(baseline 2.74ms). Run `python scripts/benchmark_translate.py` for "
            "a full report. Offending cases:\n  " + "\n  ".join(regressions)
        )

    def test_warm_cache_p95_under_budget(self, mappings):
        """Warm-cache P95 across the full corpus must stay under 1 ms per case."""
        regressions = []
        for case_name, (fixture, cypher) in _CORPUS.items():
            samples = self._measure_warm(cypher, mappings[fixture])
            p95 = _percentile(samples, 95)
            mean = statistics.fmean(samples)
            if p95 > 1.0:
                regressions.append(f"{case_name}: P95={p95:.2f}ms mean={mean:.2f}ms (budget 1ms)")
        assert not regressions, (
            "Warm-cache translate() P95 regressed past the 1ms budget "
            "(baseline 0.05ms — means the LRU cache is broken). Offending cases:\n  "
            + "\n  ".join(regressions)
        )

    def test_single_hop_beats_prd_budget(self, mappings):
        """PRD §2.1 target: single-hop queries translate in < 50 ms. Must hold in cold path."""
        fixture, cypher = _CORPUS["single_hop_simple_match"]
        samples = self._measure_cold(cypher, mappings[fixture])
        p95 = _percentile(samples, 95)
        assert p95 < 50.0, (
            f"Single-hop P95 regressed past the PRD §2.1 50ms target (p95={p95:.2f}ms). "
            "Run `python scripts/benchmark_translate.py` for a full report."
        )
