from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

NAKED_LPG_CASE_IDS = [f"C{n}" for n in range(800, 807)]

VCI_WARNING_CASE_IDS = ["C802", "C803", "C804", "C805", "C806"]


@pytest.mark.parametrize("case_id", NAKED_LPG_CASE_IDS)
def test_translate_naked_lpg_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params)

    assert case.expected_aql is not None, "Golden AQL must be filled"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars


@pytest.mark.parametrize("case_id", VCI_WARNING_CASE_IDS)
def test_naked_lpg_emits_vci_warning(corpus_cases, case_id: str):
    """Traversal cases on naked LPG must emit a VCI warning."""
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params)

    warning_messages = [
        w["message"] if isinstance(w, dict) else str(w)
        for w in out.warnings
    ]
    assert any(
        "VCI" in msg or "vertex-centric" in msg.lower()
        for msg in warning_messages
    ), f"Expected VCI warning but got: {warning_messages}"


def test_naked_lpg_no_options_index_hint(corpus_cases):
    """No OPTIONS indexHint should appear in any naked LPG AQL output."""
    for case_id in NAKED_LPG_CASE_IDS:
        case = next(c for c in corpus_cases if c.id == case_id)
        mapping = mapping_bundle_for(case.mapping_fixture)
        out = translate(case.cypher, mapping=mapping, params=case.params)
        assert "OPTIONS" not in out.aql, (
            f"Case {case_id}: naked LPG should not emit OPTIONS indexHint"
        )
        assert "indexHint" not in out.aql, (
            f"Case {case_id}: naked LPG should not emit indexHint"
        )


def test_naked_lpg_node_only_no_vci_warning(corpus_cases):
    """Node-only queries (no traversal) should NOT emit VCI warnings."""
    for case_id in ["C800", "C801"]:
        case = next(c for c in corpus_cases if c.id == case_id)
        mapping = mapping_bundle_for(case.mapping_fixture)
        out = translate(case.cypher, mapping=mapping, params=case.params)
        warning_messages = [
            w["message"] if isinstance(w, dict) else str(w)
            for w in out.warnings
        ]
        assert not any(
            "VCI" in msg or "vertex-centric" in msg.lower()
            for msg in warning_messages
        ), f"Case {case_id}: node-only query should not warn about VCI"
