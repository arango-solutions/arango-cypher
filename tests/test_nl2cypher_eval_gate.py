"""WP-25.5 unit tests for the evaluation runner + regression gate.

Two concerns:

1. Runner behaviour is exercised with a mocked provider that returns a
   hand-rolled Cypher for the first two cases, so we can assert on
   :class:`Report` shape and per-category roll-up without touching a
   real LLM.

2. The regression gate (``test_gate_against_baseline``) is gated behind
   ``RUN_NL2CYPHER_EVAL=1`` — it requires a live LLM and a committed
   ``baseline.json``.  In the standard unit-test run it is skipped.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.nl2cypher.eval.runner import (
    CORPUS_PATH,
    EvalCase,
    Report,
    load_configs,
    load_corpus,
    run_eval,
)

BASELINE_PATH = Path(__file__).parent / "nl2cypher" / "eval" / "baseline.json"


class _ScriptedProvider:
    """Returns scripted Cypher responses keyed by question substring."""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        self.calls.append(user)
        for key, cypher in self._responses.items():
            if key.lower() in user.lower():
                return (
                    f"```cypher\n{cypher}\n```",
                    {
                        "prompt_tokens": 500,
                        "completion_tokens": 30,
                        "total_tokens": 530,
                        "cached_tokens": 100,
                    },
                )
        return (
            "```cypher\nMATCH (n) RETURN n\n```",
            {
                "prompt_tokens": 500,
                "completion_tokens": 20,
                "total_tokens": 520,
                "cached_tokens": 0,
            },
        )


class TestLoading:
    def test_load_corpus(self) -> None:
        cases = load_corpus()
        assert len(cases) >= 10
        assert all(c.id for c in cases)
        assert all(c.question for c in cases)
        assert all(c.mapping_fixture for c in cases)

    def test_load_configs(self) -> None:
        cfgs = load_configs()
        names = {c["name"] for c in cfgs}
        assert {"zero_shot", "few_shot", "few_shot_plus_entity", "full"}.issubset(names)


class TestPatternMatchLogic:
    def test_pattern_match_all_required(self) -> None:
        """All declared patterns must match — ANY-style would over-accept."""
        from tests.nl2cypher.eval.runner import _pattern_match
        assert _pattern_match(
            'MATCH (p:Person {name: "Tom Hanks"})-[:ACTED_IN]->(m:Movie) RETURN m',
            [r"(?is)MATCH.*Person.*Tom Hanks", r"(?is)ACTED_IN.*Movie"],
        )
        assert not _pattern_match(
            "MATCH (p:Person)-[:ACTED_IN]->(m:Movie)",
            [r"(?is)MATCH.*Person", r"(?is)Tom Hanks"],
        )

    def test_pattern_match_invalid_regex_treated_as_fail(self) -> None:
        from tests.nl2cypher.eval.runner import _pattern_match
        assert not _pattern_match("MATCH (n) RETURN n", ["("])

    def test_empty_patterns_vacuously_match(self) -> None:
        from tests.nl2cypher.eval.runner import _pattern_match
        assert _pattern_match("anything", [])


class TestRunner:
    def test_runner_produces_report_fields(self) -> None:
        cases = [
            EvalCase(
                id="t1",
                mapping_fixture="movies_pg",
                question="Which movies did Tom Hanks act in?",
                expected_patterns=[
                    r"(?is)Person.*Tom Hanks",
                    r"(?is)ACTED_IN.*Movie",
                ],
                category="baseline",
            ),
            EvalCase(
                id="t2",
                mapping_fixture="movies_pg",
                question="Who directed The Matrix?",
                expected_patterns=[r"(?is)DIRECTED.*The Matrix"],
                category="baseline",
            ),
        ]
        provider = _ScriptedProvider({
            "tom hanks": (
                'MATCH (p:Person {name: "Tom Hanks"})-[:ACTED_IN]->(m:Movie) '
                'RETURN m.title'
            ),
            "matrix": (
                'MATCH (p:Person)-[:DIRECTED]->(m:Movie {title: "The Matrix"}) '
                'RETURN p.name'
            ),
        })
        report: Report = run_eval(
            config={"name": "test", "use_fewshot": False, "use_entity_resolution": False},
            provider=provider,
            cases=cases,
        )
        assert report.case_count == 2
        assert report.parse_ok_rate == 1.0
        assert report.pattern_match_rate == 1.0
        assert "baseline" in report.by_category
        assert report.by_category["baseline"]["n"] == 2
        assert report.cached_tokens_mean > 0

    def test_runner_detects_pattern_miss(self) -> None:
        cases = [
            EvalCase(
                id="m",
                mapping_fixture="movies_pg",
                question="Who acted in Forest Gump?",
                expected_patterns=[r"(?is)Forrest Gump"],
                category="typo",
            ),
        ]
        provider = _ScriptedProvider({
            "forest gump": 'MATCH (m:Movie {title: "Forest Gump"}) RETURN m',
        })
        report = run_eval(
            config={"name": "zero", "use_fewshot": False, "use_entity_resolution": False},
            provider=provider,
            cases=cases,
        )
        assert report.parse_ok_rate == 1.0
        assert report.pattern_match_rate == 0.0

    def test_runner_case_level_error_handling(self) -> None:
        """A provider exception on one case doesn't kill the whole run."""

        class _BoomProvider:
            def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
                raise RuntimeError("LLM unreachable")

        cases = [EvalCase(
            id="err",
            mapping_fixture="movies_pg",
            question="anything",
            expected_patterns=[],
            category="baseline",
        )]
        report = run_eval(
            config={"name": "x", "use_fewshot": False, "use_entity_resolution": False},
            provider=_BoomProvider(),
            cases=cases,
        )
        assert report.case_count == 1
        assert report.results[0].parse_ok is False or report.results[0].cypher == ""

    def test_markdown_render_has_expected_sections(self) -> None:
        cases = load_corpus()[:2]
        provider = _ScriptedProvider({})
        report = run_eval(
            config={"name": "md_test", "use_fewshot": False, "use_entity_resolution": False},
            provider=provider,
            cases=cases,
        )
        md = report.to_markdown()
        assert "# NL→Cypher eval report" in md
        assert "By category" in md
        assert "Per-case" in md


class TestGateRegressionLogic:
    def _baseline_like(self, *, parse_ok=1.0, pattern_match=0.8, tokens=500.0, retries=0.1):
        return {
            "config_name": "full",
            "generated_at": "2024-01-01T00:00:00Z",
            "case_count": 10,
            "parse_ok_rate": parse_ok,
            "pattern_match_rate": pattern_match,
            "tokens_mean": tokens,
            "cached_tokens_mean": 0.0,
            "retries_mean": retries,
            "latency_mean_ms": 100.0,
            "by_category": {},
            "results": [],
        }

    def test_no_regression_passes(self) -> None:
        base = self._baseline_like()
        fresh = self._baseline_like()
        assert _gate_ok(base, fresh)

    def test_parse_drop_over_threshold_fails(self) -> None:
        base = self._baseline_like(parse_ok=1.0)
        fresh = self._baseline_like(parse_ok=0.9)  # 10pp drop
        assert not _gate_ok(base, fresh)

    def test_parse_drop_under_threshold_passes(self) -> None:
        base = self._baseline_like(parse_ok=1.0)
        fresh = self._baseline_like(parse_ok=0.97)  # 3pp drop — under 5pp cap
        assert _gate_ok(base, fresh)

    def test_pattern_drop_over_threshold_fails(self) -> None:
        base = self._baseline_like(pattern_match=0.8)
        fresh = self._baseline_like(pattern_match=0.74)  # 6pp drop — over 5pp cap
        assert not _gate_ok(base, fresh)

    def test_tokens_blow_up_fails(self) -> None:
        base = self._baseline_like(tokens=500.0)
        fresh = self._baseline_like(tokens=700.0)  # +40%
        assert not _gate_ok(base, fresh)

    def test_retries_blow_up_fails(self) -> None:
        base = self._baseline_like(retries=0.1)
        fresh = self._baseline_like(retries=0.5)  # +0.4 > 0.3
        assert not _gate_ok(base, fresh)


def _gate_ok(baseline: dict, fresh: dict) -> bool:
    """Regression-gate policy mirrored from ``test_gate_against_baseline``."""
    if baseline["parse_ok_rate"] - fresh["parse_ok_rate"] > 0.05:
        return False
    if baseline["pattern_match_rate"] - fresh["pattern_match_rate"] > 0.05:
        return False
    if baseline["tokens_mean"] > 0 and (
        fresh["tokens_mean"] / baseline["tokens_mean"] > 1.20
    ):
        return False
    if fresh["retries_mean"] - baseline["retries_mean"] > 0.3:
        return False
    return True


@pytest.mark.skipif(
    os.environ.get("RUN_NL2CYPHER_EVAL") != "1",
    reason="Live eval requires RUN_NL2CYPHER_EVAL=1 and a configured LLM provider.",
)
def test_gate_against_baseline() -> None:
    """Live regression gate: fresh run must not regress beyond baseline.

    Enable with ``RUN_NL2CYPHER_EVAL=1 OPENAI_API_KEY=... pytest``.
    The baseline is committed at
    ``tests/nl2cypher/eval/baseline.json`` — refresh by running the
    runner with the ``full`` config and copying the fresh report over
    the baseline (and writing a PR explaining why).
    """
    if not BASELINE_PATH.exists():
        pytest.skip(f"baseline not present at {BASELINE_PATH}; create it first")

    from arango_cypher.nl2cypher import get_llm_provider

    provider = get_llm_provider()
    if provider is None:
        pytest.skip("no LLM provider configured (set OPENAI_API_KEY or OPENROUTER_API_KEY)")

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    full_cfg = next(
        (c for c in load_configs() if c.get("name") == "full"),
        {"name": "full"},
    )
    report = run_eval(config=full_cfg, provider=provider, cases=load_corpus())
    fresh = report.to_dict()

    assert fresh["case_count"] > 0
    assert _gate_ok(baseline, fresh), (
        f"NL→Cypher eval regressed beyond tolerance. "
        f"baseline={baseline.get('parse_ok_rate'):.2f}/"
        f"{baseline.get('pattern_match_rate'):.2f}; "
        f"fresh={fresh['parse_ok_rate']:.2f}/{fresh['pattern_match_rate']:.2f}"
    )


def test_corpus_file_exists() -> None:
    """Sanity check: corpus.yml must ship with the package."""
    assert CORPUS_PATH.exists(), f"missing {CORPUS_PATH}"
