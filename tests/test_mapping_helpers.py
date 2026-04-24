"""Direct unit tests for ``arango_query_core.mapping_from_wire_dict`` and
``arango_query_core.mapping_hash``.

These two helpers are the spelling-normalising entry point for every
caller that submits a mapping (HTTP service, CLI, tool-calling harness)
and the deterministic key used by the corrections / nl_corrections
lookup tables. Both have been exercised indirectly through the service
and translator integration tests, but the 2026-04-28 post-hardening
audit (see ``docs/audits/2026-04-28-post-hardening-audit.md`` finding
#4) flagged the lack of dedicated coverage as a maintenance hazard:
any future refactor of the wire shape can break the camelCase /
snake_case symmetry or the hash stability without a focused test
catching it.

This module covers:

* shape & key normalisation (camelCase ↔ snake_case)
* metadata pass-through
* ``MappingSource`` propagation
* hash stability across spellings
* hash sensitivity to ``conceptual_schema`` / ``physical_mapping``
* hash insensitivity to ``metadata`` and ``owl_turtle`` (per the
  documented contract — those fields are not part of the canonical
  ``{cs, pm}`` payload)
* dict / object / unknown-type input acceptance for ``mapping_hash``
"""

from __future__ import annotations

from arango_query_core import (
    MappingBundle,
    MappingSource,
    mapping_from_wire_dict,
    mapping_hash,
)

# --------------------------------------------------------------------------- #
# mapping_from_wire_dict                                                      #
# --------------------------------------------------------------------------- #


class TestMappingFromWireDict:
    def test_snake_case_keys_round_trip(self) -> None:
        d = {
            "conceptual_schema": {"entities": {"Person": {}}},
            "physical_mapping": {"entities": {"Person": {"collectionName": "person"}}},
            "metadata": {"version": "1.0"},
        }
        bundle = mapping_from_wire_dict(d)
        assert isinstance(bundle, MappingBundle)
        assert bundle.conceptual_schema == d["conceptual_schema"]
        assert bundle.physical_mapping == d["physical_mapping"]
        assert bundle.metadata == d["metadata"]
        assert bundle.source is None

    def test_camel_case_keys_round_trip(self) -> None:
        d = {
            "conceptualSchema": {"entities": {"Person": {}}},
            "physicalMapping": {"entities": {"Person": {"collectionName": "person"}}},
            "metadata": {"version": "1.0"},
        }
        bundle = mapping_from_wire_dict(d)
        assert bundle.conceptual_schema == d["conceptualSchema"]
        assert bundle.physical_mapping == d["physicalMapping"]
        assert bundle.metadata == d["metadata"]

    def test_snake_case_wins_when_both_present(self) -> None:
        # Defensive contract: if a buggy caller submits *both* spellings,
        # the snake_case spelling is the canonical one (matches the
        # Python-side dataclass field names).
        d = {
            "conceptual_schema": {"snake": True},
            "conceptualSchema": {"camel": True},
            "physical_mapping": {"snake": True},
            "physicalMapping": {"camel": True},
        }
        bundle = mapping_from_wire_dict(d)
        assert bundle.conceptual_schema == {"snake": True}
        assert bundle.physical_mapping == {"snake": True}

    def test_missing_keys_default_to_empty_dicts(self) -> None:
        bundle = mapping_from_wire_dict({})
        assert bundle.conceptual_schema == {}
        assert bundle.physical_mapping == {}
        assert bundle.metadata == {}
        assert bundle.source is None
        assert bundle.owl_turtle is None

    def test_empty_dict_values_are_normalised_to_empty_dicts(self) -> None:
        # Falsy values (empty dict, None) on either spelling fall through
        # to the ``or {}`` branch — verify that explicitly so a future
        # refactor doesn't accidentally change empty-dict to None.
        bundle = mapping_from_wire_dict({"conceptual_schema": {}, "physical_mapping": None})
        assert bundle.conceptual_schema == {}
        assert bundle.physical_mapping == {}

    def test_mapping_source_passes_through(self) -> None:
        src = MappingSource(
            kind="schema_analyzer_export",
            fingerprint="abc123",
            generated_at_iso="2026-04-28T00:00:00Z",
            notes="from analyzer 0.6.1",
        )
        bundle = mapping_from_wire_dict({}, source=src)
        assert bundle.source is src

    def test_owl_turtle_is_intentionally_not_read(self) -> None:
        # The wire contract documented in the docstring (and in
        # python_prd.md §14 open item) explicitly excludes owl_turtle
        # from the wire dict — verify the helper honours that even when
        # a caller supplies it.
        d = {"owl_turtle": "@prefix : <urn:x:> ."}
        bundle = mapping_from_wire_dict(d)
        assert bundle.owl_turtle is None


# --------------------------------------------------------------------------- #
# mapping_hash                                                                #
# --------------------------------------------------------------------------- #


class TestMappingHash:
    _CS = {"entities": {"Person": {"properties": {"name": {}}}}}
    _PM = {"entities": {"Person": {"collectionName": "person"}}}

    def test_hash_is_stable_string_of_expected_shape(self) -> None:
        h = mapping_hash({"conceptual_schema": self._CS, "physical_mapping": self._PM})
        assert isinstance(h, str)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_stable_across_camel_and_snake_spellings(self) -> None:
        snake = {"conceptual_schema": self._CS, "physical_mapping": self._PM}
        camel = {"conceptualSchema": self._CS, "physicalMapping": self._PM}
        assert mapping_hash(snake) == mapping_hash(camel)

    def test_hash_stable_for_dict_vs_bundle(self) -> None:
        d = {"conceptual_schema": self._CS, "physical_mapping": self._PM}
        bundle = mapping_from_wire_dict(d)
        assert mapping_hash(d) == mapping_hash(bundle)

    def test_hash_is_deterministic_across_key_insertion_order(self) -> None:
        # JSON-canonicalisation via sort_keys=True is the contract that
        # makes the lookup-table key stable when the UI serialises in
        # one order and the Python API serialises in another.
        a = {"conceptual_schema": {"x": 1, "y": 2}, "physical_mapping": {"a": 1}}
        b = {"conceptual_schema": {"y": 2, "x": 1}, "physical_mapping": {"a": 1}}
        assert mapping_hash(a) == mapping_hash(b)

    def test_hash_changes_when_conceptual_schema_changes(self) -> None:
        base = {"conceptual_schema": self._CS, "physical_mapping": self._PM}
        mutated_cs = {"entities": {"Movie": {"properties": {"title": {}}}}}
        mutated = {"conceptual_schema": mutated_cs, "physical_mapping": self._PM}
        assert mapping_hash(base) != mapping_hash(mutated)

    def test_hash_changes_when_physical_mapping_changes(self) -> None:
        base = {"conceptual_schema": self._CS, "physical_mapping": self._PM}
        mutated_pm = {"entities": {"Person": {"collectionName": "people"}}}
        mutated = {"conceptual_schema": self._CS, "physical_mapping": mutated_pm}
        assert mapping_hash(base) != mapping_hash(mutated)

    def test_hash_ignores_metadata(self) -> None:
        # Documented contract: only ``{cs, pm}`` enter the hash.
        # Metadata churn (timestamps, fingerprints from the analyzer)
        # must not invalidate the corrections-store key.
        without_meta = {"conceptual_schema": self._CS, "physical_mapping": self._PM}
        with_meta = {**without_meta, "metadata": {"generated_at": "2026-04-28T00:00:00Z"}}
        assert mapping_hash(without_meta) == mapping_hash(with_meta)

    def test_hash_ignores_owl_turtle(self) -> None:
        # Same contract — owl_turtle is not part of the canonical key.
        without = {"conceptual_schema": self._CS, "physical_mapping": self._PM}
        with_turtle = {**without, "owl_turtle": "@prefix : <urn:x:> ."}
        assert mapping_hash(without) == mapping_hash(with_turtle)

    def test_hash_handles_empty_inputs(self) -> None:
        # The empty-mapping hash must still be a stable 16-hex-char string
        # — corrections-store lookups against an as-yet-unmapped DB rely on
        # this baseline being a valid key (rather than blowing up).
        h_empty_dict = mapping_hash({})
        h_empty_bundle = mapping_hash(mapping_from_wire_dict({}))
        assert h_empty_dict == h_empty_bundle
        assert len(h_empty_dict) == 16

    def test_hash_handles_unknown_object_type(self) -> None:
        # The fallback branch (``else: cs, pm = {}, {}``) keys an
        # unrecognised input to the same hash as an empty mapping —
        # this is the documented "graceful degradation" branch and a
        # silent failure mode worth pinning.
        h_unknown = mapping_hash(object())
        h_empty = mapping_hash({})
        assert h_unknown == h_empty

    def test_hash_handles_object_with_attributes(self) -> None:
        # The hasattr() branch supports duck-typed callers (e.g. a
        # subclass of MappingBundle, or a test stub).
        class _StubBundle:
            conceptual_schema = TestMappingHash._CS
            physical_mapping = TestMappingHash._PM

        h_stub = mapping_hash(_StubBundle())
        h_dict = mapping_hash(
            {
                "conceptual_schema": _StubBundle.conceptual_schema,
                "physical_mapping": _StubBundle.physical_mapping,
            }
        )
        assert h_stub == h_dict
