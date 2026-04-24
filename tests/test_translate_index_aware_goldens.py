"""Golden tests for WP-18: Index-aware transpilation (VCI warnings)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

_CASES_FILE = Path(__file__).resolve().parent / "fixtures" / "cases_v03" / "index_aware.yml"
_CASES = yaml.safe_load(_CASES_FILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["id"])
def test_index_aware(case: dict) -> None:
    mapping = mapping_bundle_for(case["mapping_fixture"])
    out = translate(case["cypher"], mapping=mapping)

    aql = out.aql
    for fragment in case.get("expect_aql_contains", []):
        assert fragment in aql, f"[{case['id']}] expected AQL to contain {fragment!r}, got:\n{aql}"

    if case.get("expect_no_warnings"):
        assert not out.warnings, f"[{case['id']}] expected no warnings, got: {out.warnings!r}"

    needle = case.get("expect_warnings_contain")
    if needle:
        msgs = " ".join(w.get("message", "") for w in out.warnings)
        assert needle in msgs, f"[{case['id']}] expected warnings to contain {needle!r}, got: {msgs!r}"
