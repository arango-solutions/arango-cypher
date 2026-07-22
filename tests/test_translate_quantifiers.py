"""List quantifier predicates: ``any`` / ``all`` / ``none`` / ``single``.

``<kind>(x IN list WHERE pred)`` lowers to a count-subquery test over the list,
binding the quantifier variable as the AQL loop variable. (Cypher's
three-valued semantics for ``null`` list elements are not reproduced exactly —
AQL treats a null predicate as false — which is correct for non-null lists.)
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


class TestQuantifiers:
    def test_any_in_where(self, pg):
        out = translate(
            'MATCH (p:Person) WHERE any(x IN p.roles WHERE x = "lead") RETURN p.name',
            mapping=pg,
        )
        assert 'LENGTH(FOR x IN p.roles FILTER (x == "lead") RETURN 1) > 0' in out.aql

    def test_all_in_where(self, pg):
        out = translate(
            'MATCH (p:Person) WHERE all(x IN p.roles WHERE x <> "") RETURN p.name',
            mapping=pg,
        )
        assert "LENGTH(FOR x IN p.roles FILTER NOT (" in out.aql
        assert ") == 0)" in out.aql

    def test_none_in_where(self, pg):
        out = translate(
            'MATCH (p:Person) WHERE none(x IN p.roles WHERE x = "bad") RETURN p.name',
            mapping=pg,
        )
        assert 'FILTER (x == "bad") RETURN 1) == 0)' in out.aql

    def test_single_in_where(self, pg):
        out = translate(
            'MATCH (p:Person) WHERE single(x IN p.roles WHERE x = "lead") RETURN p.name',
            mapping=pg,
        )
        assert "RETURN 1) == 1)" in out.aql

    def test_any_in_return(self, pg):
        out = translate("RETURN any(x IN [1, 2, 3] WHERE x > 2) AS r", mapping=pg)
        assert "LENGTH(FOR x IN [1,2,3] FILTER" in out.aql
        assert "> 0)" in out.aql

    def test_none_on_empty_list(self, pg):
        out = translate("RETURN none(x IN [] WHERE true) AS r", mapping=pg)
        assert "LENGTH(FOR x IN [] FILTER true RETURN 1) == 0)" in out.aql

    def test_all_without_where(self, pg):
        # No predicate → all() is trivially true (NOT(true) filters everything out).
        out = translate("RETURN all(x IN [1, 2, 3]) AS r", mapping=pg)
        assert "FILTER NOT (true) RETURN 1) == 0)" in out.aql

    def test_quantifier_combined_with_other_predicate(self, pg):
        out = translate(
            "MATCH (p:Person) WHERE any(x IN [1, 2] WHERE x > 0) AND p.born > 1970 RETURN p.name",
            mapping=pg,
        )
        assert "LENGTH(FOR x IN [1,2] FILTER" in out.aql
        assert "AND (p.born" in out.aql
