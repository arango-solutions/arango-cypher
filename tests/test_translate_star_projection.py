"""``RETURN *`` / ``WITH *`` star projection.

`*` expands to every *user-named* variable in scope, in declaration order.
Auto-generated traversal / anonymous-node bindings must never appear (they are
tracked separately from user variables); after a `WITH`, `*` reflects the
projected variables.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


def _return(out) -> str:
    return out.aql.split("RETURN")[-1]


class TestReturnStar:
    def test_single_node(self, pg):
        out = translate("MATCH (a:Person) RETURN *", mapping=pg)
        assert _return(out).strip().startswith("{a: a}")

    def test_named_relationship_included(self, pg):
        out = translate("MATCH (a:Person)-[r:ACTED_IN]->(m:Movie) RETURN *", mapping=pg)
        r = _return(out)
        assert "a: a" in r and "r: r" in r and "m: m" in r

    def test_anonymous_relationship_excluded(self, pg):
        out = translate("MATCH (a:Person)-[:ACTED_IN]->(m:Movie) RETURN *", mapping=pg)
        r = _return(out)
        assert "a: a" in r and "m: m" in r
        # the auto traversal edge var must not leak
        assert "r:" not in r and "_sq" not in r

    def test_anonymous_target_excluded(self, pg):
        out = translate("MATCH (a:Person)-[:ACTED_IN]->(:Movie) RETURN *", mapping=pg)
        assert _return(out).strip().startswith("{a: a}")

    def test_multihop_only_named(self, pg):
        out = translate(
            "MATCH (a:Person)-[:ACTED_IN]->(m:Movie)<-[:DIRECTED]-(d:Person) RETURN *",
            mapping=pg,
        )
        r = _return(out)
        assert "a: a" in r and "m: m" in r and "d: d" in r
        assert "r_1" not in r

    def test_named_path_included(self, pg):
        out = translate("MATCH p = (a:Person)-[:ACTED_IN]->(m:Movie) RETURN p", mapping=pg)
        # sanity: path var works; now RETURN * should include it
        out2 = translate("MATCH p = (a:Person)-[:ACTED_IN]->(m:Movie) RETURN *", mapping=pg)
        assert "p: p" in _return(out2)

    def test_distinct_star_uses_object_form(self, pg):
        out = translate("MATCH (a:Person) RETURN DISTINCT *", mapping=pg)
        assert "RETURN DISTINCT {a: a}" in out.aql

    def test_star_with_order_by(self, pg):
        out = translate("MATCH (a:Person) RETURN * ORDER BY a.name", mapping=pg)
        assert "SORT a.name" in out.aql
        assert "{a: a}" in out.aql

    def test_star_with_aggregation_rejected(self, pg):
        with pytest.raises(CoreError) as exc:
            translate("MATCH (a:Person) RETURN *, count(a)", mapping=pg)
        assert exc.value.code == "NOT_IMPLEMENTED"


class TestWithStar:
    def test_passthrough(self, pg):
        out = translate("MATCH (a:Person) WITH * RETURN a.name", mapping=pg)
        assert "RETURN a.name" in out.aql

    def test_passthrough_then_return_star(self, pg):
        out = translate(
            "MATCH (a:Person)-[:ACTED_IN]->(m:Movie) WITH * RETURN *",
            mapping=pg,
        )
        r = _return(out)
        assert "a: a" in r and "m: m" in r
        assert "r:" not in r  # anon edge var not carried through WITH *

    def test_with_star_where(self, pg):
        out = translate("MATCH (a:Person) WITH * WHERE a.born > 1970 RETURN a.name", mapping=pg)
        assert "FILTER (a.born != null AND a.born > 1970)" in out.aql

    def test_explicit_with_then_return_star(self, pg):
        out = translate(
            "MATCH (a:Person)-[:ACTED_IN]->(m:Movie) WITH a, m RETURN *",
            mapping=pg,
        )
        r = _return(out)
        assert "a: a" in r and "m: m" in r

    def test_with_star_and_items_rejected(self, pg):
        with pytest.raises(CoreError) as exc:
            translate("MATCH (a:Person) WITH *, a.name AS n RETURN n", mapping=pg)
        assert exc.value.code == "NOT_IMPLEMENTED"


class TestStarNoScope:
    def test_return_star_requires_scope(self, pg):
        # A pure-computational RETURN * with no bound variables is refused.
        with pytest.raises(CoreError):
            translate("RETURN *", mapping=pg)
