"""Golden tests for WP-9: Full OPTIONAL MATCH (multi-segment, node-only, leading)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

_CASES_FILE = Path(__file__).resolve().parent / "fixtures" / "cases_v03" / "optional_match_full.yml"
_CASES = yaml.safe_load(_CASES_FILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["id"])
def test_optional_match_full(case: dict) -> None:
    mapping = mapping_bundle_for(case["mapping_fixture"])
    out = translate(case["cypher"], mapping=mapping)

    aql = out.aql
    for fragment in case.get("expect_aql_contains", []):
        assert fragment in aql, f"[{case['id']}] expected AQL to contain {fragment!r}, got:\n{aql}"
