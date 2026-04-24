#!/usr/bin/env python3
"""Analyze TCK coverage by attempting translation only (no DB needed).

Parses all .feature files, extracts the main Cypher query from each scenario,
checks step compatibility with the harness, and attempts to translate.

Reports:
  - Total scenarios (full + core)
  - Translatable (parse + translate succeeds)
  - Error-expected (correctly rejected)
  - Skipped categories and reasons
  - Projected pass rate (full and core)
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from arango_cypher import translate
from arango_query_core import CoreError
from tests.tck.gherkin import Scenario, Step, parse_feature
from tests.tck.runner import _build_mapping_for_scenario  # noqa: PLC2701

_FEATURES_DIR = Path(__file__).resolve().parent / "features"

_OUT_OF_SCOPE_CATS = frozenset(
    {
        "expressions/temporal",
        "expressions/quantifier",
        "clauses/call",
    }
)

_ERROR_STEP_PREFIXES = (
    "a SyntaxError should be raised",
    "a TypeError should be raised",
    "a SemanticError should be raised",
    "a ParameterMissing error should be raised",
    "a ArgumentError should be raised",
    "a EntityNotFound should be raised",
    "an error should be raised",
    "a ProcedureError should be raised",
    "a ConstraintVerification",
)

_ROW_COUNT_RE = re.compile(r"the result should have (\d+) rows?")

_SUPPORTED_STEP_TEXTS = {
    "an empty graph",
    "the empty graph",
    "any graph",
    "an existing graph",
    "the existing graph",
    "the result should be empty",
    "no side effects",
}


def _step_is_supported(step: Step) -> tuple[bool, str]:
    """Check if the harness can handle a given step (keyword-agnostic)."""
    s = step.text

    if s in _SUPPORTED_STEP_TEXTS:
        return True, ""

    if s.startswith("having executed:"):
        return True, ""
    if s.startswith("parameters are:"):
        return True, ""
    if s.startswith("executing query:"):
        return True, ""
    if s.startswith("executing control query:"):
        return True, ""
    if s.startswith("the result should be, in order:"):
        return True, ""
    if s.startswith("the result should be, in any order:"):
        return True, ""
    if s.startswith("the result should be (ignoring element order for lists):"):
        return True, ""
    if s.startswith("the result should be, in order (ignoring element order for lists):"):
        return True, ""
    if s.startswith("the result should be:"):
        return True, ""
    if s.startswith("the result should contain"):
        return True, ""
    if s.startswith("the side effects should be:"):
        return True, ""
    if _ROW_COUNT_RE.match(s):
        return True, ""

    for prefix in _ERROR_STEP_PREFIXES:
        if s.startswith(prefix):
            return True, ""

    if s.startswith("there exists a procedure"):
        return False, "procedure step"

    return False, f"unsupported: {s[:60]}"


def _scenario_steps_supported(sc: Scenario) -> tuple[bool, str]:
    for step in sc.steps:
        ok, reason = _step_is_supported(step)
        if not ok:
            return False, reason
    return True, ""


def _scenario_expects_error(sc: Scenario) -> bool:
    for step in sc.steps:
        if "Error should be raised" in step.text or "error should be raised" in step.text:
            return True
    return False


def _get_main_query(sc: Scenario) -> str | None:
    for step in sc.steps:
        if "executing query:" in step.text:
            if step.doc_string:
                return step.doc_string.strip()
    return None


def analyze() -> None:
    full_total = 0
    core_total = 0
    full_passable = 0
    core_passable = 0

    harness_skip_reasons: Counter = Counter()
    translate_fail_reasons: Counter = Counter()
    by_category: dict[str, Counter] = {}

    for feat_file in sorted(_FEATURES_DIR.rglob("*.feature")):
        if feat_file.name == "sample.feature":
            continue
        rel = feat_file.relative_to(_FEATURES_DIR)
        category = str(rel.parent)
        is_core = category not in _OUT_OF_SCOPE_CATS
        if category not in by_category:
            by_category[category] = Counter()

        feat = parse_feature(feat_file)
        for sc in feat.scenarios:
            full_total += 1
            by_category[category]["total"] += 1
            if is_core:
                core_total += 1

            steps_ok, step_reason = _scenario_steps_supported(sc)
            if not steps_ok:
                harness_skip_reasons[step_reason] += 1
                by_category[category]["harness_skip"] += 1
                continue

            query = _get_main_query(sc)
            if not query:
                by_category[category]["no_query"] += 1
                continue

            expects_error = _scenario_expects_error(sc)

            try:
                mapping = _build_mapping_for_scenario(sc, "lpg")
                translate(query, mapping=mapping)
                full_passable += 1
                by_category[category]["translatable"] += 1
                if is_core:
                    core_passable += 1
            except CoreError as e:
                if expects_error:
                    full_passable += 1
                    by_category[category]["error_ok"] += 1
                    if is_core:
                        core_passable += 1
                else:
                    translate_fail_reasons[str(e)[:60]] += 1
                    by_category[category]["translate_fail"] += 1
            except Exception:
                if expects_error:
                    full_passable += 1
                    by_category[category]["error_ok"] += 1
                    if is_core:
                        core_passable += 1
                else:
                    by_category[category]["translate_fail"] += 1

    full_rate = (full_passable / full_total * 100) if full_total else 0
    core_rate = (core_passable / core_total * 100) if core_total else 0

    print("=" * 72)
    print("TCK DRY-RUN COVERAGE ANALYSIS")
    print("=" * 72)
    print()
    print(f"FULL TCK (all {full_total} scenarios):")
    print(f"  Passable:          {full_passable:5d} / {full_total}")
    print(f"  Pass rate:         {full_rate:5.1f}%")
    print()
    print(f"CORE TCK (excl. temporal+quantifier — {core_total} scenarios):")
    print(f"  Passable:          {core_passable:5d} / {core_total}")
    print(f"  Pass rate:         {core_rate:5.1f}%")
    print()

    print("-" * 72)
    print("TOP HARNESS SKIP REASONS")
    print("-" * 72)
    for reason, count in harness_skip_reasons.most_common(10):
        print(f"  {count:4d}  {reason}")
    print()

    print("-" * 72)
    print("TOP TRANSLATE FAILURE REASONS")
    print("-" * 72)
    for reason, count in translate_fail_reasons.most_common(15):
        print(f"  {count:4d}  {reason}")
    print()

    print("-" * 72)
    print("BREAKDOWN BY CATEGORY")
    print("-" * 72)
    for cat in sorted(by_category):
        c = by_category[cat]
        t = c["total"]
        p = c["translatable"] + c["error_ok"]
        r = (p / t * 100) if t else 0
        oos = " [OUT OF SCOPE]" if cat in _OUT_OF_SCOPE_CATS else ""
        print(f"  {cat:45s}  {p:3d}/{t:3d}  ({r:5.1f}%){oos}")
    print()


if __name__ == "__main__":
    analyze()
