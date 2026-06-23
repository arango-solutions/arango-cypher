"""Structural vertex-centric index (VCI) detection in MappingResolver.

ArangoDB does not set a ``vci`` boolean on the persistent edge indexes that
*are* vertex-centric (e.g. ``["_from", "type", "_toType"]``). A flag-only
check therefore produced a false "no VCI" warning on real databases and also
suppressed the traversal index hint. These tests pin the structural detection
(`_is_structural_vci`) and its propagation through ``resolve_indexes`` /
``has_vci`` so the warning stays accurate and hints can be emitted.
"""

from __future__ import annotations

from arango_query_core.mapping import MappingBundle, MappingResolver, _is_structural_vci


def _bundle_with_edge_indexes(indexes: list[dict]) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={},
        physical_mapping={
            "relationships": {
                "REL": {
                    "style": "GENERIC_WITH_TYPE",
                    "edgeCollectionName": "edges",
                    "typeField": "type",
                    "indexes": indexes,
                }
            }
        },
        metadata={},
    )


class TestIsStructuralVci:
    def test_explicit_vci_type(self) -> None:
        assert _is_structural_vci("vertex_centric_index", ("anything",)) is True
        assert _is_structural_vci("vci", ("x",)) is True

    def test_from_plus_property_is_vci(self) -> None:
        assert _is_structural_vci("persistent", ("_from", "type", "_toType")) is True

    def test_to_plus_property_is_vci(self) -> None:
        assert _is_structural_vci("persistent", ("_to", "type", "_fromType")) is True

    def test_bare_edge_index_is_not_vci(self) -> None:
        # _from/_to only, no discriminator property → cannot filter by type.
        assert _is_structural_vci("edge", ("_from", "_to")) is False

    def test_property_only_index_is_not_vci(self) -> None:
        assert _is_structural_vci("persistent", ("ticker", "year")) is False

    def test_non_indexable_type_is_not_vci(self) -> None:
        assert _is_structural_vci("fulltext", ("_from", "text")) is False

    def test_empty_fields_is_not_vci(self) -> None:
        assert _is_structural_vci("persistent", ()) is False


class TestResolverStructuralVci:
    def test_persistent_from_type_index_marks_vci(self) -> None:
        b = _bundle_with_edge_indexes(
            [{"type": "persistent", "fields": ["_from", "type", "_toType"], "name": "vci_ft"}]
        )
        r = MappingResolver(b)
        assert r.has_vci("REL") is True
        idx = r.resolve_indexes("REL")[0]
        assert idx.vci is True
        assert idx.name == "vci_ft"

    def test_explicit_flag_still_honored(self) -> None:
        # Pre-existing fixtures rely on an explicit ``vci: true`` on a
        # property-named index; that must keep working (no regression).
        b = _bundle_with_edge_indexes(
            [{"type": "persistent", "fields": ["relation"], "name": "idx_rel", "vci": True}]
        )
        assert MappingResolver(b).has_vci("REL") is True

    def test_no_vci_when_only_bare_edge_and_property_indexes(self) -> None:
        b = _bundle_with_edge_indexes(
            [
                {"type": "edge", "fields": ["_from", "_to"], "name": "edge"},
                {"type": "persistent", "fields": ["ticker", "year"], "name": "rel_ty"},
            ]
        )
        assert MappingResolver(b).has_vci("REL") is False

    def test_no_indexes_means_no_vci(self) -> None:
        b = _bundle_with_edge_indexes([])
        assert MappingResolver(b).has_vci("REL") is False
