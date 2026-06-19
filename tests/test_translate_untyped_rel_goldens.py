"""Transpiler support for *untyped* relationships: ``-->``, ``-[]->`` and
``-[r]->`` with no ``:TYPE``.

An untyped relationship traverses a single inferred edge collection with **no**
type-discriminator filter, so every relationship type in that collection is
returned. This is the Cypher-path analogue of the NL->AQL meta query
"what relationship types exist / how many of each".

Resolution rules (see ``_infer_unlabeled_edge_collection``):
- one edge collection in the mapping -> use it;
- several, but exactly one type-discriminated -> use it, warn about the rest;
- otherwise raise ``UNSUPPORTED`` (naming a type is required).

``type(r)`` on an untyped edge uses the shared discriminator field with a
null-safe fallback to the physical collection name.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0.core import (
    _infer_unlabeled_edge_collection,
    _resolve_relationship_for_pattern,
    _untyped_rel_type_expr,
)
from arango_query_core import CoreError, MappingBundle, MappingResolver, MappingSource
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _bundle(entities: dict, relationships: dict | None = None) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={"entities": entities, "relationships": relationships or {}},
        metadata={},
        source=MappingSource(kind="explicit", notes="unit-test"),
    )


def _warns(out) -> list[str]:
    return [w["message"] if isinstance(w, dict) else str(w) for w in out.warnings]


# GraphRAG shape: all edges in one shared ``relations`` collection.
GRAPHRAG_ENTITIES = {
    "Person": {"style": "LABEL", "collectionName": "Node", "typeField": "type", "typeValue": "Person"},
    "Org": {"style": "LABEL", "collectionName": "Node", "typeField": "type", "typeValue": "Org"},
}
GRAPHRAG_RELS = {
    "KNOWS": {
        "style": "GENERIC_WITH_TYPE",
        "edgeCollectionName": "relations",
        "typeField": "type",
        "typeValue": "KNOWS",
    },
    "WORKS_AT": {
        "style": "GENERIC_WITH_TYPE",
        "edgeCollectionName": "relations",
        "typeField": "type",
        "typeValue": "WORKS_AT",
    },
}

# Multiple distinct edge collections -> untyped is ambiguous.
MULTI_EDGE_RELS = {
    "FOLLOWS": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "follows"},
    "LIKES": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "likes"},
}


class TestInferUnlabeledEdgeCollection:
    def test_single_edge_collection(self):
        r = MappingResolver(_bundle(GRAPHRAG_ENTITIES, GRAPHRAG_RELS))
        assert _infer_unlabeled_edge_collection(r) == "relations"

    def test_no_relationships_raises(self):
        r = MappingResolver(_bundle(GRAPHRAG_ENTITIES, {}))
        with pytest.raises(CoreError) as exc:
            _infer_unlabeled_edge_collection(r)
        assert exc.value.code == "UNSUPPORTED"

    def test_multiple_edge_collections_raises(self):
        r = MappingResolver(_bundle(GRAPHRAG_ENTITIES, MULTI_EDGE_RELS))
        with pytest.raises(CoreError) as exc:
            _infer_unlabeled_edge_collection(r)
        assert exc.value.code == "UNSUPPORTED"
        assert "multiple edge collections" in str(exc.value).lower()

    def test_narrows_to_single_discriminated_edge_collection(self):
        rels = {
            "KNOWS": {
                "style": "GENERIC_WITH_TYPE",
                "edgeCollectionName": "relations",
                "typeField": "type",
                "typeValue": "KNOWS",
            },
            # An embedded relationship has no edge collection and must be ignored.
            "HAS_NOTE": {"style": "EMBEDDED", "embeddedPath": "notes"},
        }
        r = MappingResolver(_bundle(GRAPHRAG_ENTITIES, rels))
        assert _infer_unlabeled_edge_collection(r) == "relations"


class TestResolveRelationshipForPattern:
    def test_untyped_synthesises_dedicated_over_single_collection(self):
        r = MappingResolver(_bundle(GRAPHRAG_ENTITIES, GRAPHRAG_RELS))
        rmap = _resolve_relationship_for_pattern(r, None)
        assert rmap["style"] == "DEDICATED_COLLECTION"
        assert rmap["edgeCollectionName"] == "relations"

    def test_typed_delegates_to_resolver(self):
        r = MappingResolver(_bundle(GRAPHRAG_ENTITIES, GRAPHRAG_RELS))
        rmap = _resolve_relationship_for_pattern(r, "KNOWS")
        assert rmap["style"] == "GENERIC_WITH_TYPE"
        assert rmap["typeValue"] == "KNOWS"


class TestUntypedRelTypeExpr:
    def test_uses_shared_discriminator_field(self):
        r = MappingResolver(_bundle(GRAPHRAG_ENTITIES, GRAPHRAG_RELS))
        expr = _untyped_rel_type_expr("r", r)
        assert "r.type != null ? r.type : PARSE_IDENTIFIER(r._id).collection" in expr

    def test_falls_back_to_collection_without_discriminator(self):
        rels = {"FOLLOWS": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "follows"}}
        r = MappingResolver(_bundle(GRAPHRAG_ENTITIES, rels))
        assert _untyped_rel_type_expr("r", r) == "PARSE_IDENTIFIER(r._id).collection"


class TestUntypedTraversalTranslation:
    def test_untyped_single_hop_dedicated(self):
        out = translate("MATCH (u:User)-[r]->(v:User) RETURN u, v LIMIT 5", mapping=mapping_bundle_for("pg"))
        assert out.bind_vars["@edgeCollection"] == "follows"
        assert "OUTBOUND u @@edgeCollection" in out.aql
        # No type-discriminator filter for an untyped edge.
        assert "relTypeField" not in out.bind_vars

    def test_bare_arrows_no_brackets(self):
        out = translate("MATCH (u:User)-->(v:User) RETURN u LIMIT 1", mapping=mapping_bundle_for("pg"))
        assert "OUTBOUND u @@edgeCollection" in out.aql
        assert out.bind_vars["@edgeCollection"] == "follows"

    def test_untyped_over_shared_generic_collection_omits_type_filter(self):
        out = translate("MATCH (a)-[r]->(b) RETURN r LIMIT 3", mapping=mapping_bundle_for("lpg"))
        assert out.bind_vars["@edgeCollection"] == "edges"
        assert "relTypeValue" not in out.bind_vars

    def test_untyped_any_direction(self):
        out = translate("MATCH (u:User)-[r]-(v:User) RETURN r LIMIT 2", mapping=mapping_bundle_for("lpg"))
        assert "ANY u @@edgeCollection" in out.aql

    def test_type_of_untyped_edge_uses_runtime_discriminator(self):
        out = translate("MATCH ()-[r]->() RETURN type(r) LIMIT 3", mapping=mapping_bundle_for("lpg"))
        assert "r.type != null ? r.type : PARSE_IDENTIFIER(r._id).collection" in out.aql

    def test_relationship_type_histogram(self):
        # The canonical "relationship types and counts" meta query.
        out = translate(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC LIMIT 30",
            mapping=mapping_bundle_for("lpg"),
        )
        assert "COLLECT t = (r.type != null ? r.type : PARSE_IDENTIFIER(r._id).collection)" in out.aql
        assert "AGGREGATE c = COUNT(1)" in out.aql
        # Group key is well-formed (regression: previously yielded ``collection)``).
        assert "collection)" not in out.aql.split("RETURN")[-1].split("COLLECT")[0]

    def test_untyped_two_hop(self):
        out = translate(
            "MATCH (u:User)-[r]->(x:User)-[r2]->(w:User) RETURN w LIMIT 5", mapping=mapping_bundle_for("pg")
        )
        assert "OUTBOUND" in out.aql
        assert out.bind_vars["@edgeCollection"] == "follows"

    def test_untyped_varlen(self):
        out = translate(
            "MATCH (u:User)-[r*1..2]->(v:User) RETURN v LIMIT 5", mapping=mapping_bundle_for("pg")
        )
        assert "1..2 OUTBOUND" in out.aql

    def test_untyped_optional_match(self):
        out = translate(
            "MATCH (u:User) OPTIONAL MATCH (u)-[r]->(v:User) RETURN u, v",
            mapping=mapping_bundle_for("pg"),
        )
        assert "OUTBOUND" in out.aql

    def test_untyped_exists_subquery(self):
        out = translate(
            "MATCH (u:User) WHERE EXISTS { MATCH (u)-[r]->(v:User) } RETURN u",
            mapping=mapping_bundle_for("pg"),
        )
        assert "LENGTH(" in out.aql

    def test_untyped_pattern_predicate(self):
        out = translate(
            "MATCH (u:User) WHERE (u)-->(:User) RETURN u",
            mapping=mapping_bundle_for("pg"),
        )
        assert "LENGTH(" in out.aql

    def test_multi_edge_collection_untyped_is_unsupported(self):
        out_msg = None
        with pytest.raises(CoreError) as exc:
            translate("MATCH (a)-[r]->(b) RETURN r", mapping=_bundle(GRAPHRAG_ENTITIES, MULTI_EDGE_RELS))
        out_msg = str(exc.value).lower()
        assert exc.value.code == "UNSUPPORTED"
        assert "relationship type" in out_msg

    def test_untyped_delete_traversal(self):
        out = translate(
            "MATCH (u:User)-[r]->(v:User) DELETE r",
            mapping=mapping_bundle_for("pg"),
        )
        assert out.bind_vars["@edgeCollection"] == "follows"
        assert "REMOVE" in out.aql.upper()
