"""Write-clause gap closures: whole-map CREATE parameters + CREATE→SET/REMOVE.

These pin the two high-impact gaps that were previously rejected or silently
mis-dispatched:

1. ``CREATE (n $props)`` — the entire property map is a parameter.
2. ``CREATE (n) SET …`` / ``REMOVE …`` — CREATE followed by writes on the
   created variable (previously routed to the read-MATCH mutating path, which
   ignored the CREATE).
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_query_core.errors import CoreError
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


@pytest.fixture(scope="module")
def lpg():
    return mapping_bundle_for("finreflectkg")


class TestWholeMapCreateParam:
    def test_collection_style_inserts_param_directly(self, pg):
        out = translate("CREATE (n:Person $props) RETURN n", mapping=pg)
        assert "INSERT @props INTO" in out.aql
        assert "RETURN n" in out.aql

    def test_label_style_merges_discriminator_over_param(self, lpg):
        # The type discriminator must still be applied, so the param is merged.
        out = translate("CREATE (o:ORG $props)", mapping=lpg)
        assert "MERGE(@props, {" in out.aql
        assert "type:" in out.aql  # discriminator field merged in

    def test_relationship_param(self, pg):
        out = translate(
            "MATCH (a:Person), (b:Movie) CREATE (a)-[r:ACTED_IN $props]->(b)",
            mapping=pg,
        )
        assert "MERGE(@props, {" in out.aql
        assert "_from:" in out.aql and "_to:" in out.aql


class TestCreateThenWrite:
    def test_create_then_set(self, pg):
        out = translate(
            "CREATE (n:Person {name: 'A'}) SET n.born = 1990", mapping=pg
        )
        assert "INSERT {name: 'A'} INTO" in out.aql
        # SET becomes a subquery-wrapped UPDATE on the created var.
        assert "LET _w0 = (UPDATE n WITH {born: 1990} IN" in out.aql

    def test_create_then_set_map(self, pg):
        out = translate(
            "CREATE (n:Person {name: 'A'}) SET n += {born: 1990}", mapping=pg
        )
        assert "MERGE(n," in out.aql

    def test_create_then_remove(self, pg):
        out = translate(
            "CREATE (n:Person {name: 'A', tmp: 1}) REMOVE n.tmp", mapping=pg
        )
        assert 'UNSET(n, "tmp")' in out.aql

    def test_create_then_set_label_keeps_discriminator(self, lpg):
        out = translate("CREATE (o:ORG {name: 'x'}) SET o.score = 5", mapping=lpg)
        assert "INSERT {" in out.aql and "type:" in out.aql
        assert "UPDATE o WITH {score: 5} IN" in out.aql

    def test_create_combined_with_delete_is_rejected(self, pg):
        with pytest.raises(CoreError) as exc:
            translate(
                "MATCH (n:Person) CREATE (m:Person {name: 'A'}) DELETE n", mapping=pg
            )
        assert "DELETE" in str(exc.value)
