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


@pytest.fixture(scope="module")
def naked():
    # All entities share one type-discriminated ``vertices`` collection, so an
    # unlabeled MATCH (n) resolves to a single domain collection.
    return mapping_bundle_for("naked_lpg")


class TestUnwindWriteTail:
    """``UNWIND … CREATE`` / ``UNWIND … MERGE`` — the list becomes a ``FOR``
    prefix the write nests inside. ``UNWIND … MERGE`` previously translated
    but silently dropped the ``FOR``, leaving the unwound var unbound in the
    UPSERT."""

    def test_unwind_create(self, pg):
        out = translate("UNWIND [1, 2, 3] AS x CREATE (n:Movie {released: x})", mapping=pg)
        assert "FOR x IN [1,2,3]" in out.aql
        assert "INSERT {released: x} INTO" in out.aql

    def test_unwind_create_over_maps(self, pg):
        out = translate(
            "UNWIND [{t: 1}, {t: 2}] AS row CREATE (n:Movie {released: row.t})",
            mapping=pg,
        )
        assert "FOR row IN [{t: 1},{t: 2}]" in out.aql
        assert "INSERT {released: row.t} INTO" in out.aql

    def test_unwind_merge_binds_loop_var(self, pg):
        out = translate("UNWIND [1, 2] AS x MERGE (n:Movie {released: x})", mapping=pg)
        assert "FOR x IN [1,2]" in out.aql
        assert "UPSERT {released: x}" in out.aql

    def test_match_then_unwind_create(self, pg):
        # UNWIND over a matched value nests inside the MATCH loop.
        out = translate(
            "MATCH (p:Person) UNWIND [1, 2] AS x CREATE (m:Movie {released: x})",
            mapping=pg,
        )
        assert "FOR p IN " in out.aql
        assert "FOR x IN [1,2]" in out.aql
        assert "INSERT {released: x} INTO" in out.aql

    def test_multiple_unwind_create_nests(self, pg):
        out = translate(
            "UNWIND [1, 2] AS x UNWIND [3, 4] AS y CREATE (n:Movie {released: x})",
            mapping=pg,
        )
        assert "FOR x IN [1,2]" in out.aql
        assert "FOR y IN [3,4]" in out.aql

    def test_unwind_before_match_rejected(self, pg):
        with pytest.raises(CoreError) as exc:
            translate(
                "UNWIND [1, 2] AS x MATCH (p:Person) CREATE (m:Movie {released: x})",
                mapping=pg,
            )
        assert exc.value.code == "NOT_IMPLEMENTED"


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


class TestUnlabeledMutations:
    """MATCH (n) SET/DELETE/REMOVE on an unlabeled anchor.

    Previously rejected with ``SET/DELETE requires labeled node in v0``. Now
    resolves to the single domain collection (same inference the read path
    uses) and fails closed on multi-collection schemas.
    """

    def test_unlabeled_set(self, naked):
        out = translate("MATCH (n) SET n.seen = true", mapping=naked)
        assert "FOR n IN @@collection" in out.aql
        assert "UPDATE n WITH {seen: true} IN @@collection" in out.aql
        # No discriminator filter is emitted: unlabeled means the whole domain.
        assert "@typeField" not in out.aql

    def test_unlabeled_delete_with_where(self, naked):
        out = translate("MATCH (n) WHERE n.age < 18 DELETE n", mapping=naked)
        assert "FILTER" in out.aql
        assert "REMOVE n IN @@collection" in out.aql

    def test_unlabeled_detach_delete(self, naked):
        out = translate("MATCH (n) DETACH DELETE n", mapping=naked)
        assert "ANY n" in out.aql  # incident edges removed first
        assert "REMOVE n IN @@collection" in out.aql

    def test_unlabeled_remove_property(self, naked):
        out = translate("MATCH (n) REMOVE n.tmp", mapping=naked)
        assert 'UNSET(n, "tmp")' in out.aql

    def test_unlabeled_multi_collection_fails_closed(self, pg):
        # movies_pg has many distinct collections; an unlabeled mutation cannot
        # safely pick one, so it must raise rather than silently target one.
        with pytest.raises(CoreError):
            translate("MATCH (n) DELETE n", mapping=pg)


class TestMultipleMerge:
    """Several MERGE clauses in one statement.

    Each merged element runs as a ``LET``-bound UPSERT. AQL forbids re-reading a
    collection after modifying it in the same query (ERR 1579), so every MERGE
    must hit a distinct physical collection; repeats and MATCH-prefixed forms
    fail closed with actionable errors.
    """

    def test_two_node_merges_distinct_collections(self, pg):
        out = translate(
            "MERGE (a:Person {name:'A'}) MERGE (b:Movie {title:'M'})", mapping=pg
        )
        assert out.aql.count("UPSERT") == 2
        assert "LET a = FIRST(UPSERT {name: 'A'}" in out.aql
        assert "LET b = FIRST(UPSERT {title: 'M'}" in out.aql
        # write-only multi-merge needs a terminal RETURN to be valid AQL
        assert out.aql.rstrip().endswith("RETURN null")

    def test_node_node_edge_merge(self, pg):
        out = translate(
            "MERGE (a:Person {name:'A'}) MERGE (b:Movie {title:'M'}) "
            "MERGE (a)-[:ACTED_IN]->(b)",
            mapping=pg,
        )
        assert out.aql.count("UPSERT") == 3
        assert "UPSERT {_from: a._id, _to: b._id}" in out.aql

    def test_multi_merge_with_return(self, pg):
        out = translate(
            "MERGE (a:Person {name:'A'}) MERGE (b:Movie {title:'M'}) RETURN a, b",
            mapping=pg,
        )
        assert "RETURN {a: a, b: b}" in out.aql

    def test_same_collection_fails_closed(self, pg):
        with pytest.raises(CoreError) as exc:
            translate(
                "MERGE (a:Person {name:'A'}) MERGE (b:Person {name:'B'})", mapping=pg
            )
        assert "same collection" in str(exc.value)

    def test_match_prefixed_multi_merge_fails_closed(self, pg):
        with pytest.raises(CoreError) as exc:
            translate(
                "MATCH (x:Person) MERGE (a:Person {name:'A'}) "
                "MERGE (b:Movie {title:'M'})",
                mapping=pg,
            )
        assert "MATCH" in str(exc.value)

    def test_unbound_rel_endpoint_fails_closed(self, pg):
        with pytest.raises(CoreError) as exc:
            translate(
                "MERGE (a:Person {name:'A'}) MERGE (a)-[:ACTED_IN]->(c)", mapping=pg
            )
        assert "endpoint" in str(exc.value)


class TestMultiHopMerge:
    """MERGE (a)-[:R1]->(b)-[:R2]->(c) — one UPSERT per hop.

    Endpoints come from a preceding MATCH (same as single-hop). Each hop must
    map to a distinct edge collection (ERR 1579); ON CREATE/MATCH SET and a
    trailing RETURN are rejected because they're ambiguous across hops.
    """

    def test_two_hop_distinct_edge_collections(self, pg):
        out = translate(
            "MATCH (a:Person {name:'A'}),(b:Person {name:'B'}),(c:Movie {title:'M'}) "
            "MERGE (a)-[:FOLLOWS]->(b)-[:REVIEWED]->(c)",
            mapping=pg,
        )
        assert out.aql.count("UPSERT") == 2
        assert "UPSERT {_from: a._id, _to: b._id}" in out.aql
        assert "UPSERT {_from: b._id, _to: c._id}" in out.aql
        # distinct edge-collection bind keys
        assert "@edgeCollection" in out.bind_vars
        assert "@edgeCollection2" in out.bind_vars

    def test_repeated_edge_collection_fails_closed(self, pg):
        with pytest.raises(CoreError) as exc:
            translate(
                "MATCH (a:Person {name:'A'}),(b:Person {name:'B'}),(c:Person {name:'C'}) "
                "MERGE (a)-[:FOLLOWS]->(b)-[:FOLLOWS]->(c)",
                mapping=pg,
            )
        assert "edge collection" in str(exc.value)

    def test_return_fails_closed(self, pg):
        with pytest.raises(CoreError):
            translate(
                "MATCH (a:Person),(b:Person),(c:Movie) "
                "MERGE (a)-[:FOLLOWS]->(b)-[:REVIEWED]->(c) RETURN a",
                mapping=pg,
            )

    def test_merge_action_fails_closed(self, pg):
        with pytest.raises(CoreError):
            translate(
                "MATCH (a:Person),(b:Person),(c:Movie) "
                "MERGE (a)-[:FOLLOWS]->(b)-[:REVIEWED]->(c) ON CREATE SET a.x = 1",
                mapping=pg,
            )


class TestForeachWrites:
    """CREATE and DELETE inside FOREACH.

    ``FOREACH (x IN list | CREATE …)`` reuses the create compiler under the
    loop; ``FOREACH (x IN list | DELETE x)`` removes each iterated document from
    the inferred domain collection. DETACH DELETE inside FOREACH is rejected.
    """

    def test_foreach_create_node(self, pg):
        out = translate(
            "FOREACH (name IN ['A','B'] | CREATE (p:Person {name: name}))",
            mapping=pg,
        )
        assert "FOR name IN ['A','B']" in out.aql
        assert "INSERT {name: name} INTO @@collection" in out.aql

    def test_foreach_create_keeps_discriminator(self, naked):
        out = translate(
            "FOREACH (x IN [1,2] | CREATE (m:User {n: x}))", mapping=naked
        )
        assert "INSERT {type: @typeValue, n: x} INTO @@collection" in out.aql

    def test_foreach_delete(self, naked):
        out = translate("MATCH (p:User) FOREACH (x IN [p] | DELETE x)", mapping=naked)
        assert "FOR x IN [p]" in out.aql
        assert "REMOVE x IN @@collection" in out.aql

    def test_foreach_detach_delete_fails_closed(self, naked):
        with pytest.raises(CoreError) as exc:
            translate("FOREACH (x IN [1] | DETACH DELETE x)", mapping=naked)
        assert "DETACH" in str(exc.value)

    def test_foreach_delete_multi_collection_fails_closed(self, pg):
        with pytest.raises(CoreError):
            translate("FOREACH (x IN [1] | DELETE x)", mapping=pg)
