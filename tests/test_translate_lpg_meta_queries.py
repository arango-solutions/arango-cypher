"""Transpiler support for whole-graph meta queries on a GraphRAG / naked-LPG
schema: one type-discriminated ``Node`` collection plus side document stores
(chunks, ops collections), all edges in one ``relations`` collection.

Covers:
- ``_shared_type_field`` (the discriminator-detection helper behind
  ``labels()`` / ``type()``).
- ``labels(n)`` mapping to the node's discriminator field, with a null-safe
  fallback to the collection name for side stores.
- Unlabeled ``MATCH (n)`` resolving to the single domain (type-discriminated)
  collection while excluding side stores, with a transparency warning.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError, MappingBundle, MappingResolver, MappingSource

from arango_cypher import translate
from arango_cypher._translate_v0.core import _shared_type_field


def _bundle(entities: dict, relationships: dict | None = None) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={"entities": entities, "relationships": relationships or {}},
        metadata={},
        source=MappingSource(kind="explicit", notes="unit-test"),
    )


# A GraphRAG-style schema: Node holds typed entities; chunks is a side store.
GRAPHRAG_ENTITIES = {
    "Person": {"style": "LABEL", "collectionName": "Node", "typeField": "type", "typeValue": "Person"},
    "Org": {"style": "LABEL", "collectionName": "Node", "typeField": "type", "typeValue": "Org"},
    "Chunk": {"style": "COLLECTION", "collectionName": "chunks"},
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


def _warns(out) -> list[str]:
    return [w["message"] if isinstance(w, dict) else str(w) for w in out.warnings]


class TestSharedTypeField:
    def test_common_entity_type_field(self):
        r = MappingResolver(_bundle(GRAPHRAG_ENTITIES, GRAPHRAG_RELS))
        assert _shared_type_field(r, "entities") == "type"

    def test_common_relationship_type_field(self):
        r = MappingResolver(_bundle(GRAPHRAG_ENTITIES, GRAPHRAG_RELS))
        assert _shared_type_field(r, "relationships") == "type"

    def test_no_discriminated_entities_returns_none(self):
        ents = {"Chunk": {"style": "COLLECTION", "collectionName": "chunks"}}
        r = MappingResolver(_bundle(ents))
        assert _shared_type_field(r, "entities") is None

    def test_ambiguous_type_fields_returns_none(self):
        ents = {
            "A": {"style": "LABEL", "collectionName": "Node", "typeField": "type"},
            "B": {"style": "LABEL", "collectionName": "Node", "typeField": "kind"},
        }
        r = MappingResolver(_bundle(ents))
        assert _shared_type_field(r, "entities") is None

    def test_none_resolver_returns_none(self):
        assert _shared_type_field(None, "entities") is None


class TestLabelsFunction:
    def test_labels_on_labeled_node_uses_discriminator(self):
        out = translate(
            "MATCH (n:Person) RETURN labels(n) AS t",
            mapping=_bundle(GRAPHRAG_ENTITIES, GRAPHRAG_RELS),
        )
        assert "n.type != null ? n.type : PARSE_IDENTIFIER(n._id).collection" in out.aql

    def test_labels_fallbacks_to_collection_when_no_discriminator(self):
        ents = {"Chunk": {"style": "COLLECTION", "collectionName": "chunks"}}
        out = translate(
            "MATCH (n:Chunk) RETURN labels(n) AS t",
            mapping=_bundle(ents),
        )
        assert "[PARSE_IDENTIFIER(n._id).collection]" in out.aql


class TestUnlabeledMatchResolution:
    def test_unlabeled_match_resolves_to_domain_collection(self):
        out = translate(
            "MATCH (n) RETURN labels(n) AS t, count(n) AS c ORDER BY c DESC LIMIT 20",
            mapping=_bundle(GRAPHRAG_ENTITIES, GRAPHRAG_RELS),
        )
        assert out.bind_vars["@collection"] == "Node"
        assert "AGGREGATE c = COUNT(n)" in out.aql

    def test_unlabeled_match_warns_about_excluded_side_collections(self):
        out = translate(
            "MATCH (n) RETURN count(n) AS c",
            mapping=_bundle(GRAPHRAG_ENTITIES, GRAPHRAG_RELS),
        )
        msgs = " ".join(_warns(out))
        assert "chunks" in msgs
        assert "Node" in msgs

    def test_unlabeled_match_warning_caps_long_side_collection_list(self):
        # Many side stores (the real-world case: aga_*, benchmark_*, chunks,
        # schema cache). The warning must summarise, not list all of them.
        ents = {
            "Person": {"style": "LABEL", "collectionName": "Node", "typeField": "type", "typeValue": "Person"},
        }
        for i in range(12):
            ents[f"Side{i}"] = {"style": "COLLECTION", "collectionName": f"side_store_{i:02d}"}
        out = translate("MATCH (n) RETURN count(n) AS c", mapping=_bundle(ents))
        msg = " ".join(_warns(out))
        assert "Node" in msg
        assert "12 side collection(s) excluded" in msg
        # Capped preview: 5 shown + "and 7 more", not all 12 inline.
        assert "and 7 more" in msg
        assert "side_store_11" not in msg
        # Both escape hatches surfaced.
        assert "by label" in msg
        assert "named graph" in msg

    def test_single_collection_unlabeled_still_resolves(self):
        ents = {
            "Person": {"style": "LABEL", "collectionName": "Node", "typeField": "type", "typeValue": "Person"},
            "Org": {"style": "LABEL", "collectionName": "Node", "typeField": "type", "typeValue": "Org"},
        }
        out = translate("MATCH (n) RETURN count(n) AS c", mapping=_bundle(ents))
        assert out.bind_vars["@collection"] == "Node"

    def test_two_domain_collections_is_ambiguous(self):
        ents = {
            "Person": {"style": "LABEL", "collectionName": "People", "typeField": "type", "typeValue": "Person"},
            "Widget": {"style": "GENERIC_WITH_TYPE", "collectionName": "Things", "typeField": "type", "typeValue": "Widget"},
        }
        with pytest.raises(CoreError) as exc:
            translate("MATCH (n) RETURN count(n) AS c", mapping=_bundle(ents))
        assert exc.value.code == "UNSUPPORTED"
