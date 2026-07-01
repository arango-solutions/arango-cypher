"""Cypher scalar functions inside an aggregate argument.

``max(size(m.tagline))`` reaches ``_compile_agg_expr`` as raw text and bypasses
the parse-tree function compiler, so ``size`` was left un-lowered and executed as
an unknown AQL function. Rename-only Cypher scalars (size→LENGTH, lower→LOWER,
toInteger→TO_NUMBER, …) are now lowered inside the aggregate argument.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


class TestAggInnerScalarFns:
    def test_size_in_avg(self, pg):
        out = translate("MATCH (m:Movie) RETURN avg(size(m.tagline)) AS a", mapping=pg)
        assert "AVG(LENGTH(m.tagline))" in out.aql
        assert "size(" not in out.aql

    def test_size_in_max_with(self, pg):
        out = translate(
            "MATCH (m:Movie) WITH max(size(m.tagline)) AS x RETURN x", mapping=pg
        )
        assert "MAX(LENGTH(m.tagline))" in out.aql

    def test_tointeger_in_sum(self, pg):
        out = translate(
            "MATCH (m:Movie) RETURN sum(toInteger(m.released)) AS s", mapping=pg
        )
        assert "SUM(TO_NUMBER(m.released))" in out.aql

    def test_lower_in_count_distinct(self, pg):
        out = translate(
            "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) "
            "RETURN count(DISTINCT lower(m.title)) AS c",
            mapping=pg,
        )
        assert "COUNT_DISTINCT(LOWER(m.title))" in out.aql

    def test_plain_count_unaffected(self, pg):
        out = translate(
            "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name, count(m) AS c",
            mapping=pg,
        )
        assert "COUNT(m)" in out.aql
