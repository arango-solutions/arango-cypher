#!/usr/bin/env python3
"""Render the checked-in TCK coverage report from dry-run and execution metrics."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.tck.analyze_coverage import analyze

_REPORT_PATH = Path(__file__).with_name("COVERAGE_REPORT.md")


def _rate(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator * 100:.1f}%" if denominator else "n/a"


def _execution_scope(execution: dict[str, Any], excluded_categories: set[str]) -> dict[str, int]:
    selected = [
        values for category, values in execution["categories"].items() if category not in excluded_categories
    ]
    return {
        "executed": sum(values["executed"] for values in selected),
        "tck_assertion_passed": sum(values["tck_assertion_passed"] for values in selected),
    }


def _execution_cells(
    execution: dict[str, Any] | None,
    *,
    total: int,
    excluded_categories: set[str],
) -> tuple[str, str]:
    if execution is None:
        return "not measured", "not measured"
    scope = _execution_scope(execution, excluded_categories)
    return (
        f"{scope['executed']} ({_rate(scope['executed'], total)})",
        f"{scope['tck_assertion_passed']} ({_rate(scope['tck_assertion_passed'], total)})",
    )


def render_report(
    metrics: dict[str, Any],
    *,
    generated_at: str,
    execution: dict[str, Any] | None = None,
) -> str:
    """Render parse/translation metrics and optional live execution metrics."""
    full = metrics["full"]
    core = metrics["core"]
    exclusions = ", ".join(f"`{category}`" for category in metrics["measurement"]["core_exclusions"])
    full_execution, full_equivalence = _execution_cells(
        execution,
        total=full["total"],
        excluded_categories=set(),
    )
    core_execution, core_equivalence = _execution_cells(
        execution,
        total=core["total"],
        excluded_categories=set(metrics["measurement"]["core_exclusions"]),
    )
    lines = [
        "# openCypher TCK coverage",
        "",
        f"> Generated: {generated_at}",
        "> Command: `./.venv/bin/python tests/tck/render_coverage_report.py --write`",
        "",
        "## Scope and limitations",
        "",
        "Dry-run results measure parse and translation feasibility for the bundled TCK corpus.",
        "When execution data is supplied, the report also records AQL execution and whether it",
        "satisfies the TCK's declared result/error assertion. That assertion is not a direct",
        "per-scenario Neo4j comparison; use the Neo4j cross-validation suites for that evidence.",
        "",
        "A scenario that is expected by the TCK to fail counts as a correct rejection when the parser",
        "or translator rejects it. The Core subset excludes only "
        f"{exclusions}; list quantifiers are included because they are substantially implemented.",
        "",
        "## Four-outcome dashboard",
        "",
        "| Population | Parses | Translates | Correctly rejects | Executes | TCK assertion passed (executed) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| Full ({full['total']} scenarios) | {full['parseable']} ({_rate(full['parseable'], full['total'])}) "
            f"| {full['translatable']} ({_rate(full['translatable'], full['total'])}) "
            f"| {full['correct_rejections']} ({_rate(full['correct_rejections'], full['total'])}) "
            f"| {full_execution} | {full_equivalence} |"
        ),
        (
            f"| Core ({core['total']} scenarios) | {core['parseable']} ({_rate(core['parseable'], core['total'])}) "
            f"| {core['translatable']} ({_rate(core['translatable'], core['total'])}) "
            f"| {core['correct_rejections']} ({_rate(core['correct_rejections'], core['total'])}) "
            f"| {core_execution} | {core_equivalence} |"
        ),
        "",
        "The headline dry-run passability (translation or correct rejection) is "
        f"**{full['passable']} / {full['total']} ({full['pass_rate']:.1f}%)** for Full and "
        f"**{core['passable']} / {core['total']} ({core['pass_rate']:.1f}%)** for Core.",
        "",
        "## Category breakdown",
        "",
        "| Category | Parseable | Translates | Correct rejections | Passable | Rate |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for category, values in metrics["categories"].items():
        total = values["total"]
        out_of_scope = " (out of Core)" if category in metrics["measurement"]["core_exclusions"] else ""
        lines.append(
            f"| `{category}`{out_of_scope} | {values['parseable']} / {total} "
            f"| {values['translatable']} / {total} | {values['correct_rejections']} / {total} "
            f"| {values['passable']} / {total} | {_rate(values['passable'], total)} |"
        )

    lines.extend(
        [
            "",
            "## Top dry-run blockers",
            "",
            "| Count | Failure reason |",
            "| ---: | --- |",
        ]
    )
    for reason, count in sorted(
        metrics["translation_failure_reasons"].items(), key=lambda item: item[1], reverse=True
    )[:15]:
        lines.append(f"| {count} | `{reason}` |")

    lines.extend(
        [
            "",
            "## Harness exclusions",
            "",
            "| Count | Reason |",
            "| ---: | --- |",
        ]
    )
    for reason, count in sorted(
        metrics["harness_skip_reasons"].items(), key=lambda item: item[1], reverse=True
    ):
        lines.append(f"| {count} | {reason} |")

    lines.extend(
        [
            "",
            "## Reproduce",
            "",
            "```bash",
            "# Translation-only metrics and checked-in report",
            "./.venv/bin/python tests/tck/analyze_coverage.py",
            "./.venv/bin/python tests/tck/render_coverage_report.py --write",
            "",
            "# Execution + TCK assertion outcomes (requires ArangoDB)",
            "RUN_INTEGRATION=1 ./.venv/bin/python tests/tck/analyze_execution.py > tck-execution.json",
            "./.venv/bin/python tests/tck/render_coverage_report.py --execution-json tck-execution.json --write",
            "",
            "# Reference-engine comparison for fixture corpora (requires ArangoDB + Neo4j)",
            "RUN_INTEGRATION=1 RUN_CROSS=1 pytest -m cross",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help=f"Write {_REPORT_PATH}.")
    parser.add_argument(
        "--execution-json",
        type=Path,
        help="Optional JSON emitted by tests/tck/analyze_execution.py.",
    )
    args = parser.parse_args()
    generated_at = datetime.now(UTC).date().isoformat()
    execution = json.loads(args.execution_json.read_text(encoding="utf-8")) if args.execution_json else None
    report = render_report(analyze(), generated_at=generated_at, execution=execution)
    if args.write:
        _REPORT_PATH.write_text(report, encoding="utf-8")
    else:
        print(report)


if __name__ == "__main__":
    main()
