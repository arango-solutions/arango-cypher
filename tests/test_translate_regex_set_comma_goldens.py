"""Golden tests for regex =~ support and SET comma-separated properties."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

_CASES_FILE = Path(__file__).resolve().parent / "fixtures" / "cases_v03" / "regex_and_set_comma.yml"
_CASES = yaml.safe_load(_CASES_FILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["id"])
def test_regex_and_set_comma(case: dict) -> None:
    mapping = mapping_bundle_for(case["mapping"])
    out = translate(case["cypher"], mapping=mapping)

    for fragment in case.get("expect_aql_contains", []):
        assert fragment in out.aql, f"[{case['id']}] expected {fragment!r} in AQL:\n{out.aql}"
