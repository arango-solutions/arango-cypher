"""Case/separator-insensitive label & relationship-type resolution.

Regression for the FinReflectKG vocabulary bug
(``docs/finreflectkg-cypher-vocabulary-bug-report.md``): hand-written / ported
Cypher whose label or relationship spelling differs from the mapping key only
by case or ``_``/``-``/space must still resolve — including the analyzer's
lossy rename (``FIN_METRIC`` exported as ``FINMETRIC``). Exact match is tried
first; an ambiguous normalized collision raises rather than guessing.
"""

from __future__ import annotations

import pytest

from arango_query_core import CoreError
from arango_query_core.mapping import MappingBundle, MappingResolver


def _bundle() -> MappingBundle:
    pm = {
        "entities": {
            # analyzer stripped the underscore + upper-cased the real `FIN_METRIC`
            "FINMETRIC": {
                "style": "LABEL",
                "collectionName": "Node",
                "typeField": "type",
                "typeValue": "FIN_METRIC",
            },
            "ORG": {
                "style": "LABEL",
                "collectionName": "Node",
                "typeField": "type",
                "typeValue": "ORG",
            },
        },
        "relationships": {
            "has_stake_in": {
                "style": "GENERIC_WITH_TYPE",
                "edgeCollectionName": "relations",
                "typeField": "type",
                "typeValue": "has_stake_in",
            },
        },
    }
    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping=pm,
        metadata={},
    )


@pytest.fixture
def resolver() -> MappingResolver:
    return MappingResolver(_bundle())


class TestEntityNormalization:
    def test_exact_match(self, resolver):
        assert resolver.resolve_entity("FINMETRIC")["typeValue"] == "FIN_METRIC"

    def test_analyzer_rename_underscore(self, resolver):
        # the real data value FIN_METRIC resolves to the exported FINMETRIC key
        assert resolver.resolve_entity("FIN_METRIC")["typeValue"] == "FIN_METRIC"

    def test_case_insensitive(self, resolver):
        assert resolver.resolve_entity("fin_metric")["typeValue"] == "FIN_METRIC"
        assert resolver.resolve_entity("org")["typeValue"] == "ORG"

    def test_still_not_found(self, resolver):
        with pytest.raises(CoreError) as exc:
            resolver.resolve_entity("ORG_REG")  # genuinely absent (analyzer cap)
        assert exc.value.code == "MAPPING_NOT_FOUND"


class TestRelationshipNormalization:
    def test_exact_match(self, resolver):
        assert resolver.resolve_relationship("has_stake_in")["typeValue"] == "has_stake_in"

    def test_neo4j_vocabulary(self, resolver):
        # ported Neo4j spelling Has_Stake_In → exported has_stake_in
        assert resolver.resolve_relationship("Has_Stake_In")["typeValue"] == "has_stake_in"

    def test_case_insensitive(self, resolver):
        assert resolver.resolve_relationship("HAS_STAKE_IN")["typeValue"] == "has_stake_in"

    def test_still_not_found(self, resolver):
        with pytest.raises(CoreError) as exc:
            resolver.resolve_relationship("knows")
        assert exc.value.code == "MAPPING_NOT_FOUND"


class TestAmbiguity:
    def test_ambiguous_collision_raises(self):
        pm = {
            "entities": {
                "FooBar": {"style": "COLLECTION", "collectionName": "FooBar"},
                "foo_bar": {"style": "COLLECTION", "collectionName": "foo_bar"},
            },
            "relationships": {},
        }
        b = MappingBundle(
            conceptual_schema={"entities": [], "relationships": []},
            physical_mapping=pm,
            metadata={},
        )
        r = MappingResolver(b)
        # exact still works for each
        assert r.resolve_entity("FooBar")["collectionName"] == "FooBar"
        assert r.resolve_entity("foo_bar")["collectionName"] == "foo_bar"
        # but a non-exact spelling that normalizes to both is refused
        with pytest.raises(CoreError) as exc:
            r.resolve_entity("FOOBAR")
        assert exc.value.code == "AMBIGUOUS_MAPPING"
