"""Tests for WP-27 heuristic type-field detection hardening.

Covers the tier-1/tier-2 split in ``_detect_type_field``, the cardinality-ratio
and class-like-value rejection rules for tier-2 candidates, and the
``metadata.heuristic_notes`` observability surface attached by
``_build_heuristic_mapping``.

See docs/schema_inference_bugfix_prd.md defect D1 and docs/agent_prompts.md
WP-27 for the full requirements.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

from arango_cypher.schema_acquire import (
    _build_heuristic_mapping,
    _detect_type_field,
    _looks_class_like,
)
from arango_query_core import MappingBundle

_COLLECT_RE = re.compile(r"COLLECT val = doc\.`([^`]+)`")


def _make_heuristic_mock(
    *,
    doc_collections: list[str],
    edge_collections: list[str] | None = None,
    sample_docs_by_collection: dict[str, list[dict[str, Any]]] | None = None,
    distinct_values: dict[str, dict[str, list[str]]] | None = None,
    row_counts: dict[str, int] | None = None,
) -> MagicMock:
    """Build a mock StandardDatabase suitable for driving the heuristic path.

    Unlike the helper in ``tests/test_schema_acquire.py``, this mock routes
    ``COLLECT val = doc.`<field>` RETURN val`` queries to a per-field distinct
    list and exposes a controllable ``collection(name).count()`` so the
    cardinality-ratio rule can be exercised deterministically.
    """
    db = MagicMock()
    db.name = "mock_heuristic_db"

    cols: list[dict[str, Any]] = []
    for n in doc_collections:
        cols.append({"name": n, "type": 2})
    for n in edge_collections or []:
        cols.append({"name": n, "type": 3})
    db.collections.return_value = cols

    samples = sample_docs_by_collection or {}
    distincts = distinct_values or {}
    counts = row_counts or {}

    def _execute(query: str, bind_vars: dict[str, Any] | None = None, **_kw: Any):
        bv = bind_vars or {}
        col = bv.get("@col")
        m = _COLLECT_RE.search(query)
        if m:
            return iter(distincts.get(col, {}).get(m.group(1), []))
        if col and col in samples:
            return iter(samples[col])
        return iter([])

    db.aql.execute = MagicMock(side_effect=_execute)

    def _collection(name: str) -> MagicMock:
        col_mock = MagicMock()
        col_mock.count.return_value = counts.get(name, 0)
        col_mock.indexes.return_value = []
        return col_mock

    db.collection.side_effect = _collection
    return db


def _docs_with_field(field: str, values: list[str]) -> list[dict[str, Any]]:
    return [{field: v, "name": f"row-{i}"} for i, v in enumerate(values)]


class TestLooksClassLike:
    def test_accepts_bare_identifier(self):
        assert _looks_class_like("Movie") is True
        assert _looks_class_like("Person") is True
        assert _looks_class_like("V001") is True

    def test_rejects_empty(self):
        assert _looks_class_like("") is False
        assert _looks_class_like("   ") is False

    def test_rejects_dot(self):
        assert _looks_class_like("Compliance.rst") is False
        assert _looks_class_like("a.b") is False

    def test_rejects_slash_and_whitespace(self):
        assert _looks_class_like("a/b") is False
        assert _looks_class_like("Foo Bar") is False
        assert _looks_class_like("x\ty") is False

    def test_rejects_file_extensions(self):
        assert _looks_class_like("README.md") is False
        assert _looks_class_like("notes.pdf") is False


class TestDetectTypeField:
    def test_tier2_label_rejected_when_high_cardinality(self):
        """High distinct-count tier-2 ``label`` values are rejected even when
        every value is individually class-like."""
        col = "HighCardColl"
        sample_vals = [f"V{i:03d}" for i in range(20)]
        distinct_vals = [f"V{i:03d}" for i in range(120)]
        db = _make_heuristic_mock(
            doc_collections=[col],
            sample_docs_by_collection={col: _docs_with_field("label", sample_vals)},
            distinct_values={col: {"label": distinct_vals}},
            row_counts={col: 200},
        )
        notes: list[dict[str, Any]] = []
        assert _detect_type_field(db, col, notes_sink=notes) is None
        assert any(n["field"] == "label" and n["tier"] == 2 and "cardinality" in n["reason"] for n in notes)

    def test_tier2_label_accepted_when_class_like(self):
        """Class-like label values below the cardinality cap are accepted."""
        col = "PeopleAndMovies"
        sample_vals = ["Movie", "Person"] * 10
        distinct_vals = ["Movie", "Person"]
        db = _make_heuristic_mock(
            doc_collections=[col],
            sample_docs_by_collection={col: _docs_with_field("label", sample_vals)},
            distinct_values={col: {"label": distinct_vals}},
            row_counts={col: 173},
        )
        notes: list[dict[str, Any]] = []
        assert _detect_type_field(db, col, notes_sink=notes) == "label"
        assert notes == []

    def test_tier2_label_rejected_when_value_has_dot(self):
        """Dotted/file-extension values flunk the class-like check."""
        col = "IBEX_Documents"
        sample_vals = ["Compliance.rst", "index.rst"] * 10
        distinct_vals = ["Compliance.rst", "index.rst"]
        db = _make_heuristic_mock(
            doc_collections=[col],
            sample_docs_by_collection={col: _docs_with_field("label", sample_vals)},
            distinct_values={col: {"label": distinct_vals}},
            row_counts={col: 36},
        )
        notes: list[dict[str, Any]] = []
        assert _detect_type_field(db, col, notes_sink=notes) is None
        reasons = " | ".join(n["reason"] for n in notes if n["field"] == "label")
        assert "class-like" in reasons or "." in reasons

    def test_tier1_type_always_wins_over_tier2_label(self):
        """When both ``type`` and ``label`` cover ≥80% of sampled docs,
        the tier-1 ``type`` field is chosen regardless of ``label``'s shape."""
        col = "MixedDiscriminators"
        sample_docs = [{"type": "TypeA", "label": "Anything.rst", "k": i} for i in range(10)] + [
            {"type": "TypeB", "label": "Other.pdf", "k": i} for i in range(10)
        ]
        db = _make_heuristic_mock(
            doc_collections=[col],
            sample_docs_by_collection={col: sample_docs},
            distinct_values={
                col: {
                    "type": ["TypeA", "TypeB"],
                    "label": ["Anything.rst", "Other.pdf"],
                }
            },
            row_counts={col: 20},
        )
        assert _detect_type_field(db, col) == "type"

    def test_no_candidate_falls_through_to_collection(self):
        """A collection lacking any discriminator field emits one
        ``COLLECTION`` entity using the collection-derived label."""
        col = "plain_docs"
        sample_docs = [{"name": "alice"}, {"name": "bob"}, {"name": "cara"}]
        db = _make_heuristic_mock(
            doc_collections=[col],
            sample_docs_by_collection={col: sample_docs},
            distinct_values={},
            row_counts={col: 3},
        )
        assert _detect_type_field(db, col) is None

        bundle = _build_heuristic_mapping(db, "hybrid")
        entities = bundle.physical_mapping["entities"]
        assert len(entities) == 1
        ((label, pm),) = entities.items()
        assert pm["style"] == "COLLECTION"
        assert pm["collectionName"] == col


class TestBuildHeuristicMappingNotes:
    def test_heuristic_notes_structure_on_rejection(self):
        """Rejections are recorded at ``metadata.heuristic_notes`` keyed by
        collection name, with ``rejected_candidates`` / ``accepted_field`` /
        ``resolved_style`` sub-keys."""
        col = "IBEX_Documents"
        sample_vals = ["Compliance.rst", "index.rst"] * 10
        db = _make_heuristic_mock(
            doc_collections=[col],
            sample_docs_by_collection={col: _docs_with_field("label", sample_vals)},
            distinct_values={col: {"label": ["Compliance.rst", "index.rst"]}},
            row_counts={col: 36},
        )
        bundle = _build_heuristic_mapping(db, "hybrid")
        assert isinstance(bundle, MappingBundle)

        notes = (bundle.metadata or {}).get("heuristic_notes")
        assert isinstance(notes, dict)
        assert col in notes
        entry = notes[col]
        assert set(entry.keys()) >= {"rejected_candidates", "accepted_field", "resolved_style"}
        assert entry["accepted_field"] is None
        assert entry["resolved_style"] == "COLLECTION"
        assert any(r["field"] == "label" and r["tier"] == 2 for r in entry["rejected_candidates"])

    def test_acceptance_criterion_36_dotted_values_become_one_collection_entity(self):
        """Matches WP-27 acceptance criterion: a ``*_Documents``-shaped
        collection with 36 rows and 36 distinct dotted ``label`` values
        produces exactly one ``style=COLLECTION`` entity and zero dotted
        entities."""
        col = "IBEX_Documents"
        distinct_vals = [f"doc_{i}.rst" for i in range(36)]
        sample_docs = [{"label": v, "body": "..."} for v in distinct_vals[:20]]
        db = _make_heuristic_mock(
            doc_collections=[col],
            sample_docs_by_collection={col: sample_docs},
            distinct_values={col: {"label": distinct_vals}},
            row_counts={col: 36},
        )
        bundle = _build_heuristic_mapping(db, "hybrid")
        entities = bundle.physical_mapping["entities"]
        assert len(entities) == 1
        ((label, pm),) = entities.items()
        assert pm["style"] == "COLLECTION"
        assert "." not in label

    def test_acceptance_criterion_lpg_two_types_become_two_label_entities(self):
        """Matches WP-27 acceptance criterion: an LPG-shaped collection with
        173 rows and 2 distinct class-like ``type`` values produces two
        ``style=LABEL`` entities."""
        col = "nodes"
        sample_docs = [{"type": "Person"} for _ in range(10)] + [{"type": "Movie"} for _ in range(10)]
        db = _make_heuristic_mock(
            doc_collections=[col],
            sample_docs_by_collection={col: sample_docs},
            distinct_values={col: {"type": ["Movie", "Person"]}},
            row_counts={col: 173},
        )
        bundle = _build_heuristic_mapping(db, "lpg")
        entities = bundle.physical_mapping["entities"]
        assert len(entities) == 2
        styles = {pm["style"] for pm in entities.values()}
        assert styles == {"LABEL"}
