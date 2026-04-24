"""Tests for downstream adoption of ``physicalMapping.shardFamilies``.

Covers ``arango_cypher.nl2cypher._core._shard_families_block`` and its
integration into ``_build_schema_summary``. Closes defect D7 in
``docs/schema_inference_bugfix_prd.md`` (the IBEX/MAROCCHINO/MOR1KX/
OR1200 first-in-summary bias) by ensuring the LLM prompt now lists
every member of a shard family with an explicit UNION-or-pick-one
directive instead of silently picking one alphabetically.

All tests are offline; no live analyzer or DB required.
"""

from __future__ import annotations

from typing import Any

from arango_cypher.nl2cypher._core import (
    _build_schema_summary,
    _shard_families_block,
)
from arango_query_core import MappingBundle, MappingSource

# ---------------------------------------------------------------------------
# Bundle helpers
# ---------------------------------------------------------------------------


def _bundle_with_shard_families(
    families: Any,
    *,
    entities: list[dict[str, Any]] | None = None,
) -> MappingBundle:
    """Build a minimal bundle whose physical_mapping carries ``families``.

    The conceptual schema is intentionally tiny — these tests focus on
    the families block, not on the rest of the schema rendering.
    """
    cs_entities = entities or [
        {"name": "User", "properties": [{"name": "email"}]},
    ]
    pm_entities: dict[str, Any] = {}
    for e in cs_entities:
        name = e["name"]
        pm_entities[name] = {
            "style": "COLLECTION",
            "collectionName": name.lower() + "s",
            "properties": {p["name"]: {} for p in e.get("properties", [])},
        }
    return MappingBundle(
        conceptual_schema={"entities": cs_entities, "relationships": []},
        physical_mapping={
            "entities": pm_entities,
            "relationships": {},
            "shardFamilies": families,
        },
        metadata={},
        source=MappingSource(kind="schema_analyzer_export"),
    )


def _doc_family(*, with_field_discriminator: bool = False) -> dict[str, Any]:
    """Canonical IBEX/MAROCCHINO/MOR1KX/OR1200 Document family."""
    discriminator: dict[str, Any]
    if with_field_discriminator:
        discriminator = {"source": "field", "field": "repo"}
    else:
        discriminator = {"source": "collection_prefix"}
    return {
        "name": "Document",
        "suffix": "Document",
        "discriminator": discriminator,
        "sharedProperties": ["doc_version", "label", "path", "source_commit"],
        "members": [
            {
                "entity": "IBEXDocument",
                "collectionName": "IBEX_Documents",
                "discriminatorValue": "IBEX",
            },
            {
                "entity": "MAROCCHINODocument",
                "collectionName": "MAROCCHINO_Documents",
                "discriminatorValue": "MAROCCHINO",
            },
            {
                "entity": "MOR1KXDocument",
                "collectionName": "MOR1KX_Documents",
                "discriminatorValue": "MOR1KX",
            },
            {
                "entity": "OR1200Document",
                "collectionName": "OR1200_Documents",
                "discriminatorValue": "OR1200",
            },
        ],
    }


# ---------------------------------------------------------------------------
# _shard_families_block — pure helper
# ---------------------------------------------------------------------------


class TestShardFamiliesBlock:
    def test_renders_member_labels_when_family_present(self):
        bundle = _bundle_with_shard_families([_doc_family()])
        block = _shard_families_block(bundle)
        assert block is not None
        assert "Family `Document`" in block
        for label in (
            "IBEXDocument",
            "MAROCCHINODocument",
            "MOR1KXDocument",
            "OR1200Document",
        ):
            assert label in block, f"missing member {label} in block:\n{block}"

    def test_collection_names_never_leak(self):
        """§1.2 contract: only conceptual entity names reach the prompt.

        Physical collection names like ``IBEX_Documents`` (with the
        trailing 's' / underscore) carry no meaning to the LLM and
        would let it generate AQL-ish identifiers — we only emit the
        conceptual entity names from each member.
        """
        bundle = _bundle_with_shard_families([_doc_family()])
        block = _shard_families_block(bundle)
        assert block is not None
        for collection_name in (
            "IBEX_Documents",
            "MAROCCHINO_Documents",
            "MOR1KX_Documents",
            "OR1200_Documents",
        ):
            assert collection_name not in block, (
                f"physical collection name {collection_name} leaked into prompt:\n{block}"
            )

    def test_field_discriminator_renders_filter_hint(self):
        bundle = _bundle_with_shard_families(
            [_doc_family(with_field_discriminator=True)],
        )
        block = _shard_families_block(bundle)
        assert block is not None
        assert "`repo`" in block
        assert "UNION" in block

    def test_collection_prefix_discriminator_renders_generic_hint(self):
        bundle = _bundle_with_shard_families(
            [_doc_family(with_field_discriminator=False)],
        )
        block = _shard_families_block(bundle)
        assert block is not None
        assert "UNION" in block
        # No specific field name should be promised when the
        # discriminator source is the collection prefix.
        assert "`repo`" not in block

    def test_returns_none_when_no_families(self):
        bundle = _bundle_with_shard_families([])
        assert _shard_families_block(bundle) is None

    def test_returns_none_when_field_absent(self):
        """Older analyzer outputs (pre-0.6) have no shardFamilies key."""
        bundle = MappingBundle(
            conceptual_schema={"entities": [], "relationships": []},
            physical_mapping={"entities": {}, "relationships": {}},
            metadata={},
            source=MappingSource(kind="schema_analyzer_export"),
        )
        assert _shard_families_block(bundle) is None

    def test_skips_singleton_member_lists(self):
        """A 'family' of one is just an entity — never useful in the prompt."""
        bundle = _bundle_with_shard_families(
            [
                {
                    "name": "Document",
                    "suffix": "Document",
                    "discriminator": {"source": "collection_prefix"},
                    "sharedProperties": ["body"],
                    "members": [
                        {
                            "entity": "IBEXDocument",
                            "collectionName": "IBEX_Documents",
                            "discriminatorValue": "IBEX",
                        },
                    ],
                }
            ],
        )
        assert _shard_families_block(bundle) is None

    def test_tolerates_garbage_entries(self):
        """A malformed family (non-dict, missing name, weird members) is dropped silently."""
        bundle = _bundle_with_shard_families(
            [
                "not-a-family",
                {"members": [{"entity": "X"}]},
                {"name": "Document", "members": "not-a-list"},
                _doc_family(),
            ],
        )
        block = _shard_families_block(bundle)
        assert block is not None
        assert "Family `Document`" in block
        # Nothing else got rendered as a family.
        assert block.count("Family `") == 1


# ---------------------------------------------------------------------------
# _build_schema_summary integration
# ---------------------------------------------------------------------------


class TestBuildSchemaSummaryWithShardFamilies:
    def test_block_appears_in_summary_after_deployment_hint(self):
        """Block must follow the (optional) deployment hint and precede entities.

        Order matters for prompt readability: deployment context →
        cross-cutting structural patterns → entities → relationships.
        """
        bundle = _bundle_with_shard_families([_doc_family()])
        bundle.metadata["shardingProfile"] = {
            "style": "OneShard",
            "status": "ok",
        }
        summary = _build_schema_summary(bundle)
        lines = summary.splitlines()
        # Find indices of the three sections we care about.
        deploy_idx = next(
            (i for i, line in enumerate(lines) if "Deployment:" in line),
            -1,
        )
        family_idx = next(
            (i for i, line in enumerate(lines) if "Family `Document`" in line),
            -1,
        )
        node_idx = next(
            (i for i, line in enumerate(lines) if line.lstrip().startswith("Node :")),
            -1,
        )
        assert deploy_idx >= 0, f"deployment hint missing:\n{summary}"
        assert family_idx >= 0, f"shard family block missing:\n{summary}"
        assert node_idx >= 0, f"entity rendering missing:\n{summary}"
        assert deploy_idx < family_idx < node_idx, (
            f"section order wrong (deploy={deploy_idx}, family={family_idx}, node={node_idx}):\n{summary}"
        )

    def test_no_block_when_no_families(self):
        """Summary stays byte-compatible with pre-0.6 analyzer output
        when ``shardFamilies`` is absent (prompt-cache safety)."""
        bundle = MappingBundle(
            conceptual_schema={
                "entities": [{"name": "User", "properties": [{"name": "email"}]}],
                "relationships": [],
            },
            physical_mapping={
                "entities": {
                    "User": {
                        "style": "COLLECTION",
                        "collectionName": "users",
                        "properties": {"email": {}},
                    },
                },
                "relationships": {},
            },
            metadata={},
            source=None,
        )
        summary = _build_schema_summary(bundle)
        assert "Family `" not in summary
        assert "Shard families" not in summary
