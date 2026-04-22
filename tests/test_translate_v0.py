"""Unit tests for transpiler-internal primitives in ``arango_cypher.translate_v0``.

Complements the golden-driven `test_translate_*_goldens.py` suite with
targeted cases for label-resolution normalisation (WP-27 D5 backtick strip).
"""

from __future__ import annotations

from arango_cypher import translate
from arango_cypher.translate_v0 import _strip_label_backticks
from arango_query_core import MappingBundle, MappingSource
from tests.helpers.mapping_fixtures import mapping_bundle_for


class TestStripLabelBackticks:
    def test_strips_enclosing_pair(self):
        assert _strip_label_backticks("`Movie`") == "Movie"
        assert _strip_label_backticks("`Compliance.rst`") == "Compliance.rst"

    def test_passes_bare_identifier_through(self):
        assert _strip_label_backticks("Movie") == "Movie"
        assert _strip_label_backticks("Person") == "Person"

    def test_does_not_strip_single_backtick(self):
        assert _strip_label_backticks("`") == "`"
        assert _strip_label_backticks("") == ""


def _mapping_with_dotted_entity() -> MappingBundle:
    """Build a MappingBundle whose entity's canonical name contains a ``.``
    character — the precise situation defect D5 is pinned against.
    """
    return MappingBundle(
        conceptual_schema={
            "entities": [
                {
                    "labels": ["Compliance.rst"],
                    "name": "Compliance.rst",
                    "properties": [
                        {"name": "doc_version", "type": "string"},
                    ],
                }
            ],
            "relationships": [],
            "properties": [],
        },
        physical_mapping={
            "entities": {
                "Compliance.rst": {
                    "collectionName": "ibex_documents",
                    "style": "COLLECTION",
                    "properties": {
                        "doc_version": {"field": "doc_version", "type": "string"},
                    },
                },
            },
            "relationships": {},
        },
        metadata={
            "provider": "fixture",
            "timestamp": "2026-04-22T00:00:00Z",
            "confidence": 1.0,
            "warnings": [],
        },
        source=MappingSource(kind="test_fixture", notes="WP-27 D5 round-trip"),
    )


class TestBacktickedLabelResolution:
    def test_backticked_label_resolves_same_as_bare_label(self):
        """``MATCH (m:Movie)`` and ``MATCH (m:`Movie`)`` must transpile to
        byte-identical AQL when ``Movie`` is mapped.
        """
        mapping = mapping_bundle_for("movies_pg")

        bare = translate("MATCH (m:Movie) RETURN m", mapping=mapping)
        escaped = translate("MATCH (m:`Movie`) RETURN m", mapping=mapping)

        assert bare.aql == escaped.aql
        assert bare.bind_vars == escaped.bind_vars

    def test_backticked_label_with_dot_resolves(self):
        """An entity whose canonical name is not a bare ``SymbolicName``
        (``Compliance.rst``) must resolve via the backtick-escaped form.

        Before WP-27 this raised ``No entity mapping for: `Compliance.rst``.
        """
        mapping = _mapping_with_dotted_entity()
        out = translate("MATCH (d:`Compliance.rst`) RETURN d.doc_version", mapping=mapping)

        assert "ibex_documents" in (out.bind_vars.get("@collection") or "") or \
               any(v == "ibex_documents" for v in out.bind_vars.values())
        assert "doc_version" in out.aql
