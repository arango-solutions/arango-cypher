"""EXISTS / COUNT pattern-shorthand subqueries: ``exists{ (pattern) }``.

openCypher's newer form omits the ``MATCH`` inside the braces
(``exists{(p)-[:R]->(m)}``); the in-repo grammar needs an explicit reading
clause, so the parser inserts an implicit ``MATCH`` before parsing (string
literals excepted). A *bare* named target correlates to an outer binding
(``_sq_v._id == m._id``); a labeled target stays a fresh subquery-local binding.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


class TestPatternShorthand:
    def test_count_shorthand_in_return(self, pg):
        out = translate(
            "MATCH (p:Person) "
            "RETURN p.name, count{(p)<-[:FOLLOWS]-(:Person)} AS followers "
            "ORDER BY followers DESC LIMIT 1",
            mapping=pg,
        )
        assert "LENGTH(FOR" in out.aql  # count{} → LENGTH subquery

    def test_exists_shorthand_in_where(self, pg):
        out = translate(
            "MATCH (d:Person)-[:DIRECTED]->(:Movie) "
            "WHERE NOT exists{ (d)-[:REVIEWED]->(:Movie) } RETURN d.name LIMIT 3",
            mapping=pg,
        )
        assert "(NOT (LENGTH(FOR" in out.aql

    def test_correlated_bare_target(self, pg):
        # `m` is bound outside → subquery correlates by _id, not by re-binding m
        out = translate(
            "MATCH (p:Person)-[:PRODUCED]->(m:Movie) "
            "WHERE exists{(p)-[:ACTED_IN]->(m)} RETURN p.name LIMIT 3",
            mapping=pg,
        )
        assert "FOR _sq_v, _sq_e" in out.aql
        assert "_sq_v._id == m._id" in out.aql
        assert "FOR m," not in out.aql.split("LENGTH(FOR")[1]  # m not re-bound in subquery

    def test_labeled_target_is_fresh_local(self, pg):
        # (f:Person) is a fresh subquery-local binding, not a correlation.
        out = translate(
            "MATCH (u:Person) WHERE NOT EXISTS { MATCH (u)-[:FOLLOWS]->(f:Person) } "
            "RETURN u.name",
            mapping=pg,
        )
        assert "_sq_v._id ==" not in out.aql

    def test_string_literal_not_rewritten(self, pg):
        # A literal containing `exists{(` must not get an implicit MATCH injected.
        out = translate(
            "MATCH (m:Movie) WHERE m.tagline = 'exists{(x)}' RETURN m.title",
            mapping=pg,
        )
        assert "MATCH (x)" not in out.aql
        assert "'exists{(x)}'" in out.aql
