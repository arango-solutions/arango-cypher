"""Tests for downstream adoption of ``metadata.shardingProfile``.

Covers the two new code paths that consume the sharding profile emitted
by ``arangodb-schema-analyzer>=0.5.0`` (upstream PRD §6.2 bullet 3):

1. ``arango_cypher.schema_acquire.acquire_mapping_bundle`` —
   logs ``shardingProfile.style`` at INFO and escalates to WARNING
   when ``status == "degraded"`` (mirrors the existing
   ``reconciliation`` / ``backfilled_collections`` observability
   pattern).
2. ``arango_cypher.nl2cypher._core._deployment_style_hint`` /
   ``_build_schema_summary`` — renders a conceptual, one-line
   deployment hint into the LLM prompt when the profile carries a
   recognised style and is not degraded.

All tests are offline: the analyzer is simulated via ``sys.modules``
patching, not a live DB.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

from arango_cypher.nl2cypher._core import (
    _build_schema_summary,
    _deployment_style_hint,
)
from arango_cypher.schema_acquire import acquire_mapping_bundle
from arango_query_core import MappingBundle, MappingSource


def _make_db(name: str = "shardingprofile_mock_db") -> MagicMock:
    db = MagicMock()
    db.collections.return_value = []
    db.aql.execute = MagicMock(side_effect=lambda *a, **kw: iter([]))
    db.name = name
    col_mock = MagicMock()
    col_mock.count.return_value = 0
    col_mock.indexes.return_value = []
    db.collection.return_value = col_mock
    return db


def _mock_schema_analyzer_modules(*, metadata_payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate ``schema_analyzer`` returning ``metadata_payload`` on export."""
    mock_metadata = MagicMock()
    mock_metadata.model_dump.return_value = metadata_payload

    mock_result = MagicMock()
    mock_result.conceptual_schema = {"entities": [], "relationships": []}
    mock_result.physical_mapping = {"entities": {}, "relationships": {}}
    mock_result.metadata = mock_metadata

    mock_analyzer_cls = MagicMock()
    mock_analyzer_cls.return_value.analyze_physical_schema.return_value = mock_result

    def mock_export(analysis_dict: dict[str, Any], target: str = "cypher"):
        return {
            "conceptualSchema": analysis_dict["conceptualSchema"],
            "physicalMapping": analysis_dict["physicalMapping"],
            "metadata": analysis_dict["metadata"],
        }

    mock_schema_analyzer = MagicMock()
    mock_schema_analyzer.AgenticSchemaAnalyzer = mock_analyzer_cls
    mock_schema_analyzer.export_mapping = mock_export

    return {
        "schema_analyzer": mock_schema_analyzer,
        "schema_analyzer.owl_export": MagicMock(),
    }


# ---------------------------------------------------------------------------
# acquire_mapping_bundle — observability for shardingProfile
# ---------------------------------------------------------------------------


class TestAcquireMappingBundleShardingProfileLogging:
    def test_logs_info_for_healthy_profile(self, caplog):
        db = _make_db()
        payload = {
            "confidence": 0.9,
            "shardingProfile": {
                "style": "OneShard",
                "status": "ok",
                "database": {"sharding": "single"},
            },
            "shardingProfileStatus": "ok",
        }
        with patch.dict("sys.modules", _mock_schema_analyzer_modules(metadata_payload=payload)):
            with caplog.at_level(logging.INFO, logger="arango_cypher.schema_acquire"):
                bundle = acquire_mapping_bundle(db)

        assert isinstance(bundle, MappingBundle)
        profile_records = [r for r in caplog.records if "shardingProfile" in r.getMessage()]
        assert profile_records, "expected shardingProfile observability log"
        msg = profile_records[-1].getMessage()
        assert "style=OneShard" in msg
        assert profile_records[-1].levelno == logging.INFO

    def test_logs_warning_when_degraded(self, caplog):
        db = _make_db()
        payload = {
            "confidence": 0.9,
            "shardingProfile": {
                "style": "Sharded",
                "status": "degraded",
            },
            "shardingProfileStatus": "degraded",
        }
        with patch.dict("sys.modules", _mock_schema_analyzer_modules(metadata_payload=payload)):
            with caplog.at_level(logging.INFO, logger="arango_cypher.schema_acquire"):
                acquire_mapping_bundle(db)

        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING and "shardingProfile" in r.getMessage()
        ]
        assert warnings, "degraded status should produce a WARNING log record"
        assert "degraded" in warnings[-1].getMessage().lower()
        assert "style=Sharded" in warnings[-1].getMessage()

    def test_no_log_when_profile_absent(self, caplog):
        """Old analyzers that don't emit shardingProfile must stay silent."""
        db = _make_db()
        payload = {"confidence": 0.9}  # no shardingProfile key
        with patch.dict("sys.modules", _mock_schema_analyzer_modules(metadata_payload=payload)):
            with caplog.at_level(logging.INFO, logger="arango_cypher.schema_acquire"):
                acquire_mapping_bundle(db)

        assert not any("shardingProfile" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# _deployment_style_hint — pure helper
# ---------------------------------------------------------------------------


def _bundle_with_metadata(metadata: dict[str, Any]) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={"entities": {}, "relationships": {}},
        metadata=metadata,
        source=MappingSource(kind="schema_analyzer_export"),
    )


class TestDeploymentStyleHint:
    def test_oneshard_hint(self):
        bundle = _bundle_with_metadata({"shardingProfile": {"style": "OneShard", "status": "ok"}})
        hint = _deployment_style_hint(bundle)
        assert hint is not None
        assert "single-shard" in hint.lower()

    def test_disjoint_smartgraph_hint_mentions_tenant_isolation(self):
        bundle = _bundle_with_metadata(
            {
                "shardingProfile": {
                    "style": "DisjointSmartGraph",
                    "status": "ok",
                }
            }
        )
        hint = _deployment_style_hint(bundle)
        assert hint is not None
        text = hint.lower()
        assert "tenant" in text
        assert "cannot cross" in text or "single tenant" in text

    def test_smartgraph_hint_mentions_shard_local(self):
        bundle = _bundle_with_metadata({"shardingProfile": {"style": "SmartGraph", "status": "ok"}})
        hint = _deployment_style_hint(bundle)
        assert hint is not None
        assert "shard" in hint.lower()

    def test_satellite_hint(self):
        bundle = _bundle_with_metadata({"shardingProfile": {"style": "SatelliteGraph", "status": "ok"}})
        hint = _deployment_style_hint(bundle)
        assert hint is not None
        assert "satellite" in hint.lower()

    def test_sharded_hint(self):
        bundle = _bundle_with_metadata({"shardingProfile": {"style": "Sharded", "status": "ok"}})
        hint = _deployment_style_hint(bundle)
        assert hint is not None
        assert "sharded" in hint.lower()

    def test_no_hint_when_profile_absent(self):
        bundle = _bundle_with_metadata({})
        assert _deployment_style_hint(bundle) is None

    def test_no_hint_when_degraded(self):
        """A degraded status must suppress the hint — better silent than
        misleading the LLM with a guess."""
        bundle = _bundle_with_metadata(
            {
                "shardingProfile": {
                    "style": "SmartGraph",
                    "status": "degraded",
                }
            }
        )
        assert _deployment_style_hint(bundle) is None

    def test_no_hint_when_degraded_via_top_level_status(self):
        """`shardingProfileStatus` on metadata root is also honoured."""
        bundle = _bundle_with_metadata(
            {
                "shardingProfile": {"style": "SmartGraph"},
                "shardingProfileStatus": "degraded",
            }
        )
        assert _deployment_style_hint(bundle) is None

    def test_no_hint_for_unknown_style(self):
        """Forward-compat: unknown future styles must not render a hint."""
        bundle = _bundle_with_metadata({"shardingProfile": {"style": "WarpDriveGraph", "status": "ok"}})
        assert _deployment_style_hint(bundle) is None

    def test_no_hint_when_style_missing(self):
        bundle = _bundle_with_metadata({"shardingProfile": {"status": "ok"}})
        assert _deployment_style_hint(bundle) is None


# ---------------------------------------------------------------------------
# _build_schema_summary — integration (hint reaches the prompt)
# ---------------------------------------------------------------------------


class TestBuildSchemaSummaryRendersHint:
    def test_disjoint_smartgraph_hint_appears_in_summary(self):
        bundle = MappingBundle(
            conceptual_schema={
                "entities": [
                    {
                        "name": "User",
                        "properties": [{"name": "email"}],
                    },
                ],
                "relationships": [],
            },
            physical_mapping={
                "entities": {
                    "User": {
                        "style": "COLLECTION",
                        "collectionName": "users",
                        "properties": {"email": {}},
                    }
                },
                "relationships": {},
            },
            metadata={
                "shardingProfile": {
                    "style": "DisjointSmartGraph",
                    "status": "ok",
                }
            },
            source=MappingSource(kind="schema_analyzer_export"),
        )
        summary = _build_schema_summary(bundle)
        lines = summary.splitlines()
        assert lines[0].startswith("Graph schema")
        assert any("tenant" in line.lower() and "deployment" in line.lower() for line in lines), (
            f"deployment hint missing; summary was:\n{summary}"
        )

    def test_no_hint_without_profile(self):
        """Summary must be byte-compatible with pre-0.5 analyzer output
        when no shardingProfile is present (prompt-cache safety)."""
        bundle = MappingBundle(
            conceptual_schema={
                "entities": [
                    {"name": "User", "properties": [{"name": "email"}]},
                ],
                "relationships": [],
            },
            physical_mapping={
                "entities": {
                    "User": {
                        "style": "COLLECTION",
                        "collectionName": "users",
                        "properties": {"email": {}},
                    }
                },
                "relationships": {},
            },
            metadata={},
            source=None,
        )
        summary = _build_schema_summary(bundle)
        assert "Deployment:" not in summary
