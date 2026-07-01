"""Unlabeled traversal endpoints inferred from a relationship's domain/range.

``(m:Movie)<-[:REVIEWED]-()`` has an unlabeled source, but ``REVIEWED``'s domain
is ``Person`` — so on a multi-collection PG schema the endpoint resolves to the
``persons`` collection instead of failing with "a single label is required".

The inference is gated to COLLECTION-style endpoints: for LABEL / GENERIC_WITH_TYPE
schemas it must NOT add a ``type == value`` discriminator filter (which would
assert more than domain/range safely guarantees), and those schemas already
resolve unlabeled endpoints via single-collection inference.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_query_core import MappingResolver
from arango_query_core.errors import CoreError
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


@pytest.fixture(scope="module")
def lpg():
    return mapping_bundle_for("movies_lpg")


class TestResolverInference:
    def test_inbound_resolves_domain(self, pg):
        # traversing INBOUND along REVIEWED reaches its domain (Person)
        assert MappingResolver(pg).infer_endpoint_label("REVIEWED", "INBOUND") == "Person"

    def test_outbound_resolves_range(self, pg):
        assert MappingResolver(pg).infer_endpoint_label("REVIEWED", "OUTBOUND") == "Movie"

    def test_any_direction_is_ambiguous(self, pg):
        # domain (Person) != range (Movie) → cannot disambiguate an undirected hop
        assert MappingResolver(pg).infer_endpoint_label("REVIEWED", "ANY") is None

    def test_unknown_relationship_returns_none(self, pg):
        assert MappingResolver(pg).infer_endpoint_label("NOPE", "INBOUND") is None

    def test_label_style_endpoint_not_inferred(self, lpg):
        # movies_lpg is LABEL style — inference is intentionally suppressed so no
        # discriminator filter is fabricated.
        assert MappingResolver(lpg).infer_endpoint_label("REVIEWED", "INBOUND") is None


class TestPgEndpointInference:
    def test_trailing_unlabeled_endpoint(self, pg):
        # Before the fix this raised "a single label is required"; now the
        # endpoint resolves to persons (the edge already constrains the target,
        # so the collection filter is optimized away — persons appears in the
        # traversal WITH prelude).
        out = translate(
            "MATCH (m:Movie)<-[r:REVIEWED]-() WHERE r.rating > 70 RETURN m.title",
            mapping=pg,
        )
        assert "INBOUND" in out.aql
        assert "persons" in out.aql  # WITH prelude lists the resolved vertex coll

    def test_leading_unlabeled_anchor(self, pg):
        out = translate(
            "MATCH ()-[:REVIEWED]->(m:Movie) RETURN avg(m.rating)", mapping=pg
        )
        assert out.bind_vars.get("@uCollection") == "persons"

    def test_with_aggregation_pipeline(self, pg):
        out = translate(
            "MATCH (m:Movie)<-[r:REVIEWED]-() WITH m, avg(r.rating) AS a "
            "RETURN m.title, a",
            mapping=pg,
        )
        assert "INBOUND" in out.aql

    def test_tail_match_after_with(self, pg):
        out = translate(
            "MATCH (m:Movie) WHERE m.released > 2000 WITH m "
            "MATCH (m)<-[r:REVIEWED]-() RETURN avg(r.rating)",
            mapping=pg,
        )
        assert "INBOUND" in out.aql

    def test_any_direction_still_fails_closed(self, pg):
        # Undirected + multi-collection is genuinely ambiguous → must not guess.
        with pytest.raises(CoreError):
            translate("MATCH (m:Movie)-[r:REVIEWED]-() RETURN m.title", mapping=pg)


class TestLpgNoDiscriminatorFabrication:
    def test_unlabeled_endpoint_adds_no_type_filter(self, lpg):
        # Single generic `nodes` collection: resolves via the existing fallback,
        # and crucially does NOT fabricate a `type == value` filter for the
        # unlabeled endpoint from the relationship's domain/range.
        out = translate(
            "MATCH (a)-[:ACTED_IN {role: 'Forrest'}]->(m:Movie {title: 'Forrest Gump'}) "
            "RETURN a.name ORDER BY a.name",
            mapping=lpg,
        )
        # the anchor `a` carries no discriminator predicate
        assert "a[@uTypeField]" not in out.aql
