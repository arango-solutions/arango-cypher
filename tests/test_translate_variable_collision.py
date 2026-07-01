"""Regression tests for AQL variable-collision bugs (ArangoDB ERR 1511,
"variable '…' is assigned multiple times").

Surfaced by the Text2Cypher corpus execution smoke. Two distinct causes are
covered here:

1. **Back-reference self-cycle** in the WITH-aggregation MATCH path
   (`_compile_match_pipeline`): ``(p)-[:R]->(m)<-[:R2]-(p)`` re-opened a FOR loop
   on the bound ``p`` instead of traversing to a fresh var + ``_id`` filter.
2. **Unnamed-edge collision** across MATCH clauses
   (`_compile_match_from_bound`): a second, unnamed edge's synthetic default name
   ``r`` aliased the first MATCH's edge ``r`` (``FOR m, r … FOR w, r``).
"""

from __future__ import annotations

import re

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


def _for_loop_vars(aql: str) -> list[str]:
    """All variables introduced by ``FOR <v>[, <e>] IN`` clauses."""
    out: list[str] = []
    for m in re.finditer(r"\bFOR\s+([A-Za-z_]\w*)(?:\s*,\s*([A-Za-z_]\w*))?\s+IN\b", aql):
        out.append(m.group(1))
        if m.group(2):
            out.append(m.group(2))
    return out


def _assert_unique_for_vars(aql: str) -> None:
    vs = _for_loop_vars(aql)
    dupes = {v for v in vs if vs.count(v) > 1}
    assert not dupes, f"duplicate FOR variables {dupes} in AQL:\n{aql}"


class TestBackReferenceSelfCycle:
    def test_self_cycle_aggregation(self, pg):
        out = translate(
            "MATCH (p:Person)-[:DIRECTED]->(m:Movie)<-[:PRODUCED]-(p) "
            "WITH p, count(m) AS n ORDER BY n DESC LIMIT 3 RETURN p.name, n",
            mapping=pg,
        )
        _assert_unique_for_vars(out.aql)
        # the back-reference is enforced by an _id equality, not a re-bound FOR
        assert "._id == p._id" in out.aql

    def test_self_cycle_two_labels(self, pg):
        out = translate(
            "MATCH (p:Person)-[:ACTED_IN]->(m:Movie)<-[:DIRECTED]-(p) "
            "WITH p, count(m) AS c RETURN p.name, c",
            mapping=pg,
        )
        _assert_unique_for_vars(out.aql)


class TestUnnamedEdgeCollisionAcrossMatches:
    def test_two_matches_shared_anchor(self, pg):
        out = translate(
            "MATCH (d:Person)-[:DIRECTED]->(:Movie) "
            "MATCH (d)-[:ACTED_IN]->(m:Movie) "
            "WITH m, count(DISTINCT d) AS ac ORDER BY ac DESC LIMIT 1 "
            "RETURN m.title",
            mapping=pg,
        )
        _assert_unique_for_vars(out.aql)
        # the second unnamed edge gets a fresh var rather than reusing r
        assert "r_1" in out.aql

    def test_unnamed_edge_not_treated_as_backref(self, pg):
        out = translate(
            "MATCH (p:Person)-[:ACTED_IN]->(:Movie) "
            "MATCH (p)-[:DIRECTED]->(m:Movie) RETURN m.title",
            mapping=pg,
        )
        _assert_unique_for_vars(out.aql)


class TestCollectGroupVarShadowing:
    """A COLLECT group/aggregate/INTO variable must not reuse a live FOR var.

    ``COLLECT director = director.name`` / ``COLLECT m = m`` are rejected by AQL
    ("assigned multiple times"); the group var is reallocated to a fresh name
    while the projection key keeps the original alias.
    """

    def test_return_aggregation_alias_equals_loop_var(self, pg):
        out = translate(
            "MATCH (director:Person)-[:DIRECTED]->(movie:Movie)<-[:ACTED_IN]-(director) "
            "RETURN director.name AS director, collect(movie.title) AS movies",
            mapping=pg,
        )
        assert "COLLECT director = director.name" not in out.aql
        assert "COLLECT director_g1 = director.name" in out.aql
        # projection key stays the Cypher-visible alias
        assert "director: director_g1" in out.aql

    def test_with_distinct_passthrough(self, pg):
        out = translate(
            "MATCH (p:Person)-[:WROTE]->(m:Movie) WHERE p.born > 1975 "
            "WITH DISTINCT m MATCH (m)<-[r:REVIEWED]-() WHERE r.rating < 75 "
            "RETURN count(DISTINCT m)",
            mapping=pg,
        )
        assert "COLLECT m = m" not in out.aql
        assert "COLLECT m_1 = m" in out.aql

    def test_double_aggregation_reuses_group_key(self, pg):
        out = translate(
            "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) "
            "WITH floor(p.born / 10) * 10 AS decade, count(m) AS movies_acted_in "
            "RETURN decade, avg(movies_acted_in) AS average_movies ORDER BY decade",
            mapping=pg,
        )
        # the second COLLECT groups by the prior `decade` without reusing the name
        assert "COLLECT decade_g1 = decade" in out.aql
        assert "decade: decade_g1" in out.aql

    def test_plain_aggregation_not_over_renamed(self, pg):
        # No collision → names must stay natural (regression guard).
        out = translate(
            "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) "
            "RETURN p.name AS name, count(m) AS c ORDER BY c DESC",
            mapping=pg,
        )
        assert "COLLECT name = p.name AGGREGATE c = COUNT(m)" in out.aql
