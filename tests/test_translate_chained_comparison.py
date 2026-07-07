"""Chained comparisons: ``a < b < c`` ≡ ``a < b AND b < c``.

openCypher allows chained relational comparisons; each adjacent pair is
compiled (with the usual null-guard on ordered operators) and AND-ed together.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


class TestChainedComparison:
    def test_range_lt(self, pg):
        out = translate("MATCH (p:Person) WHERE 1950 < p.born < 2000 RETURN p.name", mapping=pg)
        assert "1950 < p.born" in out.aql
        assert "p.born < 2000" in out.aql
        assert " AND " in out.aql

    def test_range_le(self, pg):
        out = translate("MATCH (p:Person) WHERE 1950 <= p.born <= 2000 RETURN p.name", mapping=pg)
        assert "1950 <= p.born" in out.aql
        assert "p.born <= 2000" in out.aql

    def test_null_guard_on_middle_operand(self, pg):
        out = translate("MATCH (p:Person) WHERE 1950 < p.born < 2000 RETURN p.name", mapping=pg)
        # ordered comparisons null-guard the property operand
        assert "p.born != null" in out.aql

    def test_three_way_chain(self, pg):
        out = translate("MATCH (p:Person) WHERE 1 < 2 < 3 < 4 RETURN p.name", mapping=pg)
        assert "(1 < 2)" in out.aql and "(2 < 3)" in out.aql and "(3 < 4)" in out.aql

    def test_chain_in_return(self, pg):
        out = translate("RETURN 1 < 2 AS r", mapping=pg)
        assert "(1 < 2)" in out.aql

    def test_single_comparison_regression(self, pg):
        out = translate("MATCH (p:Person) WHERE p.born > 1970 RETURN p.name", mapping=pg)
        assert "(p.born != null AND p.born > 1970)" in out.aql
