from __future__ import annotations

from tests.tck.analyze_coverage import _OUT_OF_SCOPE_CATS
from tests.tck.analyze_execution import _empty_counts
from tests.tck.render_coverage_report import render_report


def test_core_includes_quantifiers_and_excludes_temporal_and_call():
    assert _OUT_OF_SCOPE_CATS == {"clauses/call", "expressions/temporal"}


def test_execution_counter_exposes_all_four_outcomes():
    counts = _empty_counts()

    assert {
        "parseable",
        "translatable",
        "executed",
        "tck_assertion_passed",
        "correct_rejections",
    } <= set(counts)


def test_rendered_report_includes_four_outcomes_and_core_definition():
    metrics = {
        "measurement": {
            "core_exclusions": ["clauses/call", "expressions/temporal"],
        },
        "full": {
            "total": 10,
            "parseable": 9,
            "translatable": 7,
            "correct_rejections": 1,
            "passable": 8,
            "pass_rate": 80.0,
        },
        "core": {
            "total": 8,
            "parseable": 8,
            "translatable": 7,
            "correct_rejections": 1,
            "passable": 8,
            "pass_rate": 100.0,
        },
        "categories": {
            "expressions/quantifier": {
                "total": 2,
                "parseable": 2,
                "translatable": 2,
                "correct_rejections": 0,
                "passable": 2,
                "harness_skips": 0,
                "translation_failures": 0,
            }
        },
        "translation_failure_reasons": {"unsupported function": 1},
        "harness_skip_reasons": {"procedure step": 1},
    }

    report = render_report(metrics, generated_at="2026-08-06")

    assert "## Four-outcome dashboard" in report
    assert "list quantifiers are included" in report
    assert "`expressions/quantifier` | 2 / 2" in report
    assert "not measured" in report
