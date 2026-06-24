"""Tests for open-vocabulary edge normalization (cap + per-type endpoints).

GraphRAG graphs put every relationship in one shared, type-discriminated edge
collection with tens of thousands of `type` values. ``_normalize_open_vocab_edges``
caps to the top-K by edge volume and sets each kept type's domain/range from the
edges' ``_fromType``/``_toType`` discriminators. These tests pin that contract
with a fake DB (no live ArangoDB).
"""

from __future__ import annotations

from typing import Any

from arango_cypher.schema_acquire import _normalize_open_vocab_edges
from arango_query_core import MappingBundle, MappingSource


class _FakeAql:
    def __init__(self, freq, endpoints, total, sample_doc, sample_props_docs):
        self.freq = freq
        self.endpoints = endpoints
        self.total = total
        self.sample_doc = sample_doc
        self.sample_props_docs = sample_props_docs

    def execute(self, query: str, bind_vars: dict[str, Any] | None = None, **_: Any):
        # Real python-arango cursors are iterators (support both list(...) and
        # next(...)), so return iterators here too.
        bind_vars = bind_vars or {}
        if "RETURN LENGTH(" in query:
            return iter([self.total])
        if "LIMIT 1 RETURN e" in query:
            return iter([self.sample_doc])
        if "ft = e._fromType" in query:
            return iter(list(self.endpoints))
        if "SORT n DESC" in query:  # frequency top-K
            k = int(bind_vars.get("k", len(self.freq)))
            return iter(list(self.freq)[:k])
        if "FOR doc IN @@col LIMIT @n RETURN doc" in query:  # _sample_properties
            return iter(list(self.sample_props_docs))
        return iter([])


class _FakeDb:
    def __init__(self, aql):
        self.aql = aql


def _bundle() -> MappingBundle:
    return MappingBundle(
        conceptual_schema={
            "entities": [
                {"name": "ORG", "labels": ["ORG"], "properties": []},
                {"name": "COMP", "labels": ["COMP"], "properties": []},
                {"name": "GPE", "labels": ["GPE"], "properties": []},
            ],
            "relationships": [
                {"type": "has_stake_in", "fromEntity": "Any", "toEntity": "Any", "properties": []},
            ],
        },
        physical_mapping={
            "entities": {
                "ORG": {"style": "LABEL", "collectionName": "Node", "typeField": "type", "typeValue": "ORG"},
                "COMP": {"style": "LABEL", "collectionName": "Node", "typeField": "type", "typeValue": "COMP"},
                "GPE": {"style": "LABEL", "collectionName": "Node", "typeField": "type", "typeValue": "GPE"},
            },
            "relationships": {
                "has_stake_in": {
                    "style": "GENERIC_WITH_TYPE",
                    "edgeCollectionName": "relations",
                    "typeField": "type",
                    "typeValue": "has_stake_in",
                },
            },
        },
        metadata={},
        source=MappingSource(kind="test"),
    )


def _db() -> _FakeDb:
    return _FakeDb(
        _FakeAql(
            freq=[
                {"t": "has_stake_in", "n": 100},
                {"t": "operates_in", "n": 50},
                {"t": "rare_predicate", "n": 1},
            ],
            endpoints=[
                {"t": "has_stake_in", "ft": "ORG", "tt": "COMP", "n": 90},
                {"t": "operates_in", "ft": "ORG", "tt": "GPE", "n": 48},
            ],
            total=3,
            sample_doc={"_fromType": "ORG", "_toType": "COMP", "type": "has_stake_in"},
            sample_props_docs=[{"_from": "Node/1", "_to": "Node/2", "type": "x", "startDate": "2020"}],
        )
    )


def test_sets_per_type_endpoints_from_fromtype_totype():
    out = _normalize_open_vocab_edges(_db(), _bundle(), max_types=200)
    rels = out.physical_mapping["relationships"]
    assert rels["has_stake_in"]["domain"] == "ORG"
    assert rels["has_stake_in"]["range"] == "COMP"
    assert rels["operates_in"]["domain"] == "ORG"
    assert rels["operates_in"]["range"] == "GPE"
    # Conceptual side mirrors it.
    cs = {r["type"]: r for r in out.conceptual_schema["relationships"]}
    assert cs["has_stake_in"]["fromEntity"] == "ORG"
    assert cs["has_stake_in"]["toEntity"] == "COMP"


def test_caps_to_top_k_and_records_tail():
    out = _normalize_open_vocab_edges(_db(), _bundle(), max_types=2)
    rels = out.physical_mapping["relationships"]
    assert set(rels) == {"has_stake_in", "operates_in"}  # rare_predicate dropped
    caps = out.metadata.get("relationshipTypeCaps")
    assert caps and caps[0]["totalTypes"] == 3 and caps[0]["keptTypes"] == 2


def test_dedicated_relationships_pass_through():
    b = _bundle()
    b.physical_mapping["relationships"]["WORKS_AT"] = {
        "style": "DEDICATED_COLLECTION",
        "edgeCollectionName": "works_at",
        "domain": "PERSON",
        "range": "ORG",
    }
    out = _normalize_open_vocab_edges(_db(), b, max_types=200)
    assert out.physical_mapping["relationships"]["WORKS_AT"]["domain"] == "PERSON"


def test_noop_without_generic_with_type():
    b = MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={
            "entities": {},
            "relationships": {
                "WORKS_AT": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "works_at"}
            },
        },
        metadata={},
        source=MappingSource(kind="test"),
    )
    out = _normalize_open_vocab_edges(_db(), b, max_types=200)
    assert out is b  # untouched


def test_unmapped_endpoint_type_kept_raw_not_collapsed():
    # An endpoint type with no entity label maps to itself, not to a wrong label.
    db = _FakeDb(
        _FakeAql(
            freq=[{"t": "mentions", "n": 10}],
            endpoints=[{"t": "mentions", "ft": "ORG", "tt": "UNMAPPED_TYPE", "n": 9}],
            total=1,
            sample_doc={"_fromType": "ORG", "_toType": "UNMAPPED_TYPE", "type": "mentions"},
            sample_props_docs=[{"type": "mentions"}],
        )
    )
    out = _normalize_open_vocab_edges(db, _bundle(), max_types=200)
    rels = out.physical_mapping["relationships"]
    assert rels["mentions"]["domain"] == "ORG"
    assert rels["mentions"]["range"] == "UNMAPPED_TYPE"
