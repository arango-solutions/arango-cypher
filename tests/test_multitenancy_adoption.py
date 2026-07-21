"""Tests for downstream adoption of ``metadata.multitenancy``.

Covers the four code paths that consume the multitenancy classification
emitted by ``arangodb-schema-analyzer>=0.6.0`` (upstream PRD §6.2
bullet 4 / arango-schema-mapper PR #17):

1. ``arango_cypher.nl2cypher.tenant_scope._multitenancy_tenant_keys`` —
   pulls the upstream ``tenantKey[]`` hint, returning ``[]`` for
   ``style == "none"`` / missing block.
2. ``arango_cypher.nl2cypher.tenant_scope.analyze_tenant_scope`` —
   uses upstream keys to extend the field-discovery regex so non-default
   names like ``customerId`` get classified without an env-var override.
3. ``arango_cypher.nl2cypher.tenant_guardrail.multitenancy_physical_enforcement``
   — surfaces ``physicalEnforcement`` so violation diagnostics can label
   storage-enforced vs convention-only deployments correctly.
4. ``arango_cypher.schema_acquire.acquire_mapping_bundle`` — logs the
   classification at INFO and escalates to WARNING when degraded.

All tests are offline; the analyzer is simulated via ``sys.modules``
patching where needed.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

from arango_query_core import MappingBundle, MappingSource

from arango_cypher.nl2cypher.tenant_guardrail import (
    TenantContext,
    TenantScopeViolation,
    check_tenant_scope,
    multitenancy_physical_enforcement,
)
from arango_cypher.nl2cypher.tenant_scope import (
    EntityTenantRole,
    _multitenancy_tenant_keys,
    analyze_tenant_scope,
)
from arango_cypher.schema_acquire import acquire_mapping_bundle

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _bundle(
    *,
    metadata: dict[str, Any] | None = None,
    entities: list[dict[str, Any]] | None = None,
    relationships: list[dict[str, Any]] | None = None,
) -> MappingBundle:
    cs_entities = entities if entities is not None else []
    cs_rels = relationships if relationships is not None else []
    pm_entities: dict[str, Any] = {
        e["name"]: {
            "style": "COLLECTION",
            "collectionName": e["name"].lower(),
            "properties": {p.get("name") if isinstance(p, dict) else p: {} for p in e.get("properties", [])},
        }
        for e in cs_entities
        if isinstance(e, dict) and isinstance(e.get("name"), str)
    }
    return MappingBundle(
        conceptual_schema={"entities": cs_entities, "relationships": cs_rels},
        physical_mapping={"entities": pm_entities, "relationships": {}},
        metadata=metadata or {},
        source=MappingSource(kind="schema_analyzer_export"),
    )


# ---------------------------------------------------------------------------
# _multitenancy_tenant_keys
# ---------------------------------------------------------------------------


class TestMultitenancyTenantKeys:
    def test_returns_keys_for_discriminator_field_style(self):
        bundle = _bundle(
            metadata={
                "multitenancy": {
                    "style": "discriminator_field",
                    "tenantKey": ["customerId"],
                    "physicalEnforcement": False,
                },
            },
        )
        assert _multitenancy_tenant_keys(bundle) == ["customerId"]

    def test_returns_keys_for_shard_key_style(self):
        bundle = _bundle(
            metadata={
                "multitenancy": {
                    "style": "shard_key",
                    "tenantKey": ["tenantId"],
                    "physicalEnforcement": True,
                },
            },
        )
        assert _multitenancy_tenant_keys(bundle) == ["tenantId"]

    def test_returns_empty_when_style_is_none(self):
        bundle = _bundle(
            metadata={
                "multitenancy": {"style": "none", "tenantKey": ["ignored"]},
            },
        )
        assert _multitenancy_tenant_keys(bundle) == []

    def test_returns_empty_when_block_absent(self):
        bundle = _bundle(metadata={})
        assert _multitenancy_tenant_keys(bundle) == []

    def test_filters_non_string_entries(self):
        bundle = _bundle(
            metadata={
                "multitenancy": {
                    "style": "shard_key",
                    "tenantKey": ["good", 42, None, "", "alsoGood"],
                },
            },
        )
        assert _multitenancy_tenant_keys(bundle) == ["good", "alsoGood"]

    def test_tolerates_dict_input(self):
        """Plain-dict bundles (raw JSON imports) are accepted too."""
        keys = _multitenancy_tenant_keys(
            {
                "metadata": {
                    "multitenancy": {
                        "style": "discriminator_field",
                        "tenantKey": ["orgId"],
                    },
                },
            }
        )
        assert keys == ["orgId"]


# ---------------------------------------------------------------------------
# analyze_tenant_scope — uses upstream tenantKey for discovery
# ---------------------------------------------------------------------------


class TestAnalyzeTenantScopeWithUpstreamHint:
    def test_upstream_customerId_is_discovered(self):
        """Without the upstream hint, ``customerId`` would NOT match the
        default ``^tenant[_-]?(id|key)$`` regex and would slip through
        as GLOBAL — leaking cross-tenant rows. With the hint, the
        analyzer auto-classifies the entity as TENANT_SCOPED."""
        bundle = _bundle(
            entities=[
                {"name": "Tenant", "properties": [{"name": "_key"}]},
                {
                    "name": "Order",
                    "properties": [
                        {"name": "amount"},
                        {"name": "customerId"},
                    ],
                },
            ],
            metadata={
                "multitenancy": {
                    "style": "discriminator_field",
                    "tenantKey": ["customerId"],
                    "physicalEnforcement": False,
                },
            },
        )
        manifest = analyze_tenant_scope(bundle)
        assert manifest.role_of("Order") is EntityTenantRole.TENANT_SCOPED
        assert manifest.denorm_field_of("Order") == "customerId"

    def test_default_regex_still_fires_alongside_upstream_keys(self):
        """The combined matcher OR's the upstream keys with the local
        default — entities carrying ``TENANT_ID`` should keep
        classifying even when ``tenantKey`` lists something else."""
        bundle = _bundle(
            entities=[
                {"name": "Tenant", "properties": [{"name": "_key"}]},
                {
                    "name": "Device",
                    "properties": [{"name": "TENANT_ID"}, {"name": "name"}],
                },
                {
                    "name": "Order",
                    "properties": [{"name": "customerId"}],
                },
            ],
            metadata={
                "multitenancy": {
                    "style": "discriminator_field",
                    "tenantKey": ["customerId"],
                },
            },
        )
        manifest = analyze_tenant_scope(bundle)
        assert manifest.denorm_field_of("Device") == "TENANT_ID"
        assert manifest.denorm_field_of("Order") == "customerId"

    def test_no_upstream_hint_preserves_v1_behaviour(self):
        """Pre-0.6 mappings (no multitenancy block) must classify exactly
        as before — same regex, same outcomes. Pinned because this
        path is on the prompt-cache hot path."""
        bundle = _bundle(
            entities=[
                {"name": "Tenant", "properties": [{"name": "_key"}]},
                {
                    "name": "Device",
                    "properties": [{"name": "TENANT_ID"}, {"name": "name"}],
                },
                {
                    "name": "Order",
                    "properties": [{"name": "customerId"}],
                },
            ],
            metadata={},
        )
        manifest = analyze_tenant_scope(bundle)
        assert manifest.denorm_field_of("Device") == "TENANT_ID"
        # customerId is NOT in the v1 regex → falls through to GLOBAL.
        assert manifest.role_of("Order") is EntityTenantRole.GLOBAL


# ---------------------------------------------------------------------------
# multitenancy_physical_enforcement
# ---------------------------------------------------------------------------


class TestMultitenancyPhysicalEnforcement:
    def test_true_for_shard_key_style(self):
        bundle = _bundle(
            metadata={
                "multitenancy": {
                    "style": "shard_key",
                    "physicalEnforcement": True,
                },
            },
        )
        assert multitenancy_physical_enforcement(bundle) is True

    def test_false_for_discriminator_field(self):
        bundle = _bundle(
            metadata={
                "multitenancy": {
                    "style": "discriminator_field",
                    "physicalEnforcement": False,
                },
            },
        )
        assert multitenancy_physical_enforcement(bundle) is False

    def test_none_for_style_none(self):
        bundle = _bundle(
            metadata={
                "multitenancy": {"style": "none", "physicalEnforcement": False},
            },
        )
        assert multitenancy_physical_enforcement(bundle) is None

    def test_none_when_block_absent(self):
        bundle = _bundle(metadata={})
        assert multitenancy_physical_enforcement(bundle) is None


# ---------------------------------------------------------------------------
# check_tenant_scope — propagates physical_enforcement to the violation
# ---------------------------------------------------------------------------


class TestCheckTenantScopePropagatesEnforcement:
    def test_violation_carries_true_for_storage_enforced(self):
        ctx = TenantContext(property="_key", value="t1", display="Tenant 1")
        v = check_tenant_scope(
            "MATCH (n:Order) RETURN n",
            tenant_context=ctx,
            manifest=None,
            physical_enforcement=True,
        )
        assert isinstance(v, TenantScopeViolation)
        assert v.physical_enforcement is True

    def test_violation_carries_false_for_convention_only(self):
        ctx = TenantContext(property="_key", value="t1")
        v = check_tenant_scope(
            "MATCH (n:Order) RETURN n",
            tenant_context=ctx,
            manifest=None,
            physical_enforcement=False,
        )
        assert isinstance(v, TenantScopeViolation)
        assert v.physical_enforcement is False

    def test_violation_default_is_none_for_back_compat(self):
        """Callers that don't pass ``physical_enforcement`` (out-of-tree
        consumers, older call sites) must keep working unchanged."""
        ctx = TenantContext(property="_key", value="t1")
        v = check_tenant_scope(
            "MATCH (n:Order) RETURN n",
            tenant_context=ctx,
            manifest=None,
        )
        assert isinstance(v, TenantScopeViolation)
        assert v.physical_enforcement is None


# ---------------------------------------------------------------------------
# acquire_mapping_bundle — observability for multitenancy block
# ---------------------------------------------------------------------------


def _make_db(name: str = "multitenancy_mock_db") -> MagicMock:
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


class TestAcquireMappingBundleMultitenancyLogging:
    def test_logs_info_for_classified_style(self, caplog):
        db = _make_db()
        payload = {
            "confidence": 0.9,
            "multitenancy": {
                "style": "shard_key",
                "status": "ok",
                "physicalEnforcement": True,
                "tenantKey": ["tenantId"],
            },
            "multitenancyStatus": "ok",
        }
        with patch.dict("sys.modules", _mock_schema_analyzer_modules(metadata_payload=payload)):
            with caplog.at_level(logging.INFO, logger="arango_cypher.schema_acquire"):
                acquire_mapping_bundle(db)

        records = [r for r in caplog.records if "multitenancy" in r.getMessage()]
        assert records, "expected multitenancy observability log"
        msg = records[-1].getMessage()
        assert "style=shard_key" in msg
        assert "physicalEnforcement=True" in msg
        assert records[-1].levelno == logging.INFO

    def test_logs_warning_when_degraded(self, caplog):
        db = _make_db()
        payload = {
            "confidence": 0.9,
            "multitenancy": {
                "style": "discriminator_field",
                "status": "degraded",
                "physicalEnforcement": False,
            },
            "multitenancyStatus": "degraded",
        }
        with patch.dict("sys.modules", _mock_schema_analyzer_modules(metadata_payload=payload)):
            with caplog.at_level(logging.INFO, logger="arango_cypher.schema_acquire"):
                acquire_mapping_bundle(db)

        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING and "multitenancy" in r.getMessage()
        ]
        assert warnings, "degraded status should produce a WARNING log record"
        assert "degraded" in warnings[-1].getMessage().lower()

    def test_silent_when_style_is_none(self, caplog):
        """``style=none`` is the single-tenant default; logging it on every
        acquisition would spam production logs without conveying signal."""
        db = _make_db()
        payload = {
            "confidence": 0.9,
            "multitenancy": {
                "style": "none",
                "status": "ok",
                "physicalEnforcement": False,
            },
        }
        with patch.dict("sys.modules", _mock_schema_analyzer_modules(metadata_payload=payload)):
            with caplog.at_level(logging.INFO, logger="arango_cypher.schema_acquire"):
                acquire_mapping_bundle(db)
        assert not any("multitenancy" in r.getMessage() for r in caplog.records)

    def test_silent_when_block_absent(self, caplog):
        """Pre-0.6 analyzer outputs (no multitenancy key) stay silent."""
        db = _make_db()
        payload = {"confidence": 0.9}
        with patch.dict("sys.modules", _mock_schema_analyzer_modules(metadata_payload=payload)):
            with caplog.at_level(logging.INFO, logger="arango_cypher.schema_acquire"):
                acquire_mapping_bundle(db)
        assert not any("multitenancy" in r.getMessage() for r in caplog.records)
