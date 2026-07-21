"""WP-S2c: label predicates on untyped variables.

Cypher allows a label test in expression position, most commonly a WHERE
clause over a variable that carries no label in the MATCH pattern::

    MATCH (risk) WHERE risk:RISK_FACTOR RETURN risk

Before WP-S2c the ``:Label`` suffix was dropped during expression compilation,
so the predicate collapsed to a no-op (``risk``) and the query returned every
row. These tests pin the discriminator-filter translation for both mapping
styles (type-discriminated LPG and dedicated collection) and the
multi-label / OR combinations the corpus (q10/q11/q20) needs.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError, MappingBundle, MappingSource

from arango_cypher import translate


def _bundle(entities: dict, relationships: dict | None = None) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={"entities": entities, "relationships": relationships or {}},
        metadata={},
        source=MappingSource(kind="explicit", notes="unit-test"),
    )


# Type-discriminated LPG: one ``Node`` collection, rows distinguished by ``type``.
LPG_ENTITIES = {
    "RISK_FACTOR": {
        "style": "LABEL",
        "collectionName": "Node",
        "typeField": "type",
        "typeValue": "RISK_FACTOR",
    },
    "EVENT": {
        "style": "LABEL",
        "collectionName": "Node",
        "typeField": "type",
        "typeValue": "EVENT",
    },
    "COMP": {
        "style": "LABEL",
        "collectionName": "Node",
        "typeField": "type",
        "typeValue": "COMP",
    },
}

# Dedicated-collection schema: the label is its own physical collection.
# Single collection so the unlabeled ``MATCH (x)`` resolves; the WHERE label
# predicate then exercises the IS_SAME_COLLECTION branch.
COLLECTION_ENTITIES = {
    "Person": {"style": "COLLECTION", "collectionName": "persons"},
}


class TestLabelPredicateDiscriminator:
    def test_single_label_predicate_emits_type_filter(self):
        out = translate(
            "MATCH (risk) WHERE risk:RISK_FACTOR RETURN risk",
            mapping=_bundle(LPG_ENTITIES),
        )
        # The label resolves to the type-discriminator equality, not a no-op.
        assert "riskLabelField" in out.bind_vars
        assert out.bind_vars["riskLabelField"] == "type"
        assert out.bind_vars["riskLabelValue"] == "RISK_FACTOR"
        assert "risk[@riskLabelField] == @riskLabelValue" in out.aql

    def test_label_predicate_is_not_a_noop(self):
        out = translate(
            "MATCH (risk) WHERE risk:RISK_FACTOR RETURN risk",
            mapping=_bundle(LPG_ENTITIES),
        )
        # Regression guard: the old behaviour produced a bare ``risk`` filter.
        assert "FILTER risk\n" not in out.aql
        assert "FILTER (risk)" not in out.aql

    def test_or_of_label_predicates(self):
        out = translate(
            "MATCH (n) WHERE n:RISK_FACTOR OR n:EVENT RETURN n",
            mapping=_bundle(LPG_ENTITIES),
        )
        # Two distinct type values, ORed.
        values = {v for k, v in out.bind_vars.items() if k.endswith("LabelValue") or "LabelValue" in k}
        assert "RISK_FACTOR" in values
        assert "EVENT" in values
        assert " OR " in out.aql

    def test_multi_label_predicate_is_anded(self):
        # ``n:A:B`` means "has both labels" → AND in Cypher semantics.
        out = translate(
            "MATCH (n) WHERE n:RISK_FACTOR:EVENT RETURN n",
            mapping=_bundle(LPG_ENTITIES),
        )
        assert " AND " in out.aql
        vals = [v for k, v in out.bind_vars.items() if "LabelValue" in k]
        assert "RISK_FACTOR" in vals
        assert "EVENT" in vals

    def test_label_predicate_combined_with_property_filter(self):
        out = translate(
            'MATCH (n) WHERE n:COMP AND n.name = "cinf" RETURN n',
            mapping=_bundle(LPG_ENTITIES),
        )
        assert "n[@nLabelField] == @nLabelValue" in out.aql
        assert out.bind_vars["nLabelValue"] == "COMP"
        # The property predicate still compiles alongside the label test.
        assert "cinf" in out.aql or any(v == "cinf" for v in out.bind_vars.values())


class TestLabelPredicateCollectionStyle:
    def test_collection_style_emits_is_same_collection(self):
        out = translate(
            "MATCH (x) WHERE x:Person RETURN x",
            mapping=_bundle(COLLECTION_ENTITIES),
        )
        assert "IS_SAME_COLLECTION(@xLabelColl, x)" in out.aql
        assert out.bind_vars["xLabelColl"] == "persons"


class TestLabelPredicateErrors:
    def test_unknown_label_surfaces_mapping_error(self):
        with pytest.raises(CoreError) as exc:
            translate(
                "MATCH (n) WHERE n:NOPE RETURN n",
                mapping=_bundle(LPG_ENTITIES),
            )
        assert exc.value.code == "MAPPING_NOT_FOUND"
