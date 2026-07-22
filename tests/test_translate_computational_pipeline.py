"""No-MATCH computational multi-part pipelines (leading WITH / UNWIND).

Cypher queries that start with ``WITH`` (constant projection) or ``UNWIND`` — with
no MATCH and no write clauses — are pure computations over literals. They
previously failed the "MATCH is required before WITH" guard; they now translate
into a plain AQL ``LET`` / ``FOR`` pipeline. This is the single largest TCK
unlock (leading-clause constraint).
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


class TestComputationalPipeline:
    def test_leading_with_constant(self, pg):
        out = translate("WITH 42 AS x RETURN x", mapping=pg)
        assert "LET x = 42" in out.aql
        assert "RETURN x" in out.aql

    def test_with_list_then_unwind(self, pg):
        out = translate("WITH [1,2,3] AS list UNWIND list AS x RETURN x", mapping=pg)
        assert "LET list = [1,2,3]" in out.aql
        assert "FOR x IN list" in out.aql

    def test_leading_unwind_with_aggregate_then_unwind(self, pg):
        out = translate(
            "UNWIND range(1,2) AS row WITH collect(row) AS rows UNWIND rows AS x RETURN x",
            mapping=pg,
        )
        assert "FOR row IN RANGE(1, 2)" in out.aql
        assert "COLLECT" in out.aql
        assert "FOR x IN rows" in out.aql

    def test_nested_map_projection(self, pg):
        out = translate("WITH {a: {b: 1}} AS m RETURN m.a.b", mapping=pg)
        assert "LET m = {a: {b: 1}}" in out.aql
        assert "RETURN m.a.b" in out.aql

    def test_leading_unwind_orderby_limit(self, pg):
        out = translate(
            "UNWIND [true,false] AS bools WITH bools ORDER BY bools LIMIT 1 RETURN bools",
            mapping=pg,
        )
        assert "FOR bools IN [true,false]" in out.aql
        assert "SORT bools" in out.aql
        assert "LIMIT 1" in out.aql

    def test_chained_unwind(self, pg):
        out = translate(
            "WITH [[1,2,3],[4,5,6]] AS lol UNWIND lol AS x UNWIND x AS y RETURN y",
            mapping=pg,
        )
        assert out.aql.count("FOR ") == 2

    def test_leading_write_still_rejected(self, pg):
        # A write tail is out of scope for the computational path.
        from arango_query_core.errors import CoreError

        with pytest.raises(CoreError):
            translate("WITH 42 AS var MERGE (c:Person {born: var})", mapping=pg)
