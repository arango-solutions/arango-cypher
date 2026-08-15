from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def test_xor_lowers_without_an_aql_xor_operator() -> None:
    out = translate("RETURN true XOR false AS value", mapping=mapping_bundle_for("lpg"))

    assert " XOR " not in out.aql
    assert "== null" in out.aql
    assert "!= (false)" in out.aql


def test_xor_chain_preserves_null_propagation() -> None:
    out = translate("RETURN true XOR null XOR false AS value", mapping=mapping_bundle_for("lpg"))

    # The first pair is embedded again as the left operand of the second pair,
    # so text expansion may duplicate its null guard.
    assert out.aql.count("? null :") >= 2
    assert " XOR " not in out.aql


@pytest.mark.parametrize("operator", ["AND", "OR", "XOR"])
@pytest.mark.parametrize("literal", ["123", "'text'", "[]", "{}"])
def test_boolean_operators_reject_obvious_non_boolean_literal(literal: str, operator: str) -> None:
    with pytest.raises(CoreError, match=f"{operator} requires boolean operands"):
        translate(f"RETURN {literal} {operator} true", mapping=mapping_bundle_for("lpg"))


@pytest.mark.parametrize("operator", ["AND", "OR"])
def test_boolean_binary_operators_preserve_null_semantics(operator: str) -> None:
    out = translate(f"RETURN null {operator} false AS value", mapping=mapping_bundle_for("lpg"))

    assert "? true :" in out.aql or "? false :" in out.aql
    assert " ? " in out.aql
