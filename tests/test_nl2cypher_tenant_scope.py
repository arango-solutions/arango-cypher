"""Tests for the per-entity tenant-scope manifest.

These pin the discovery rules so we don't accidentally regress when
the upstream schema analyzer starts emitting first-class
``tenantScope`` annotations and we have to coexist with both the new
annotated mappings and the old un-annotated ones.
"""

from __future__ import annotations

import os
import re
from unittest.mock import patch

import pytest

from arango_cypher.nl2cypher.tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
    analyze_tenant_scope,
)


def _mapping(
    entities: list[dict],
    relationships: list[dict] | None = None,
    physical_overrides: dict[str, dict] | None = None,
) -> dict:
    """Build a minimal mapping dict shaped like the schema analyzer
    output, with optional per-entity physical-mapping overrides for
    testing the explicit-annotation path."""
    pm_entities = {e["name"]: {"style": "COLLECTION", "collectionName": e["name"]} for e in entities}
    for name, extra in (physical_overrides or {}).items():
        pm_entities.setdefault(name, {})
        pm_entities[name].update(extra)
    return {
        "conceptual_schema": {
            "entities": entities,
            "relationships": relationships or [],
        },
        "physical_mapping": {"entities": pm_entities, "relationships": {}},
    }


# ---------------------------------------------------------------------------
# Default discovery rules
# ---------------------------------------------------------------------------


class TestDiscoveryDefaults:
    def test_no_tenant_entity_classifies_everything_as_global(self) -> None:
        m = analyze_tenant_scope(
            _mapping(
                [
                    {"name": "User", "properties": [{"name": "email"}]},
                    {"name": "Movie", "properties": [{"name": "title"}]},
                ]
            )
        )
        assert m.tenant_entity is None
        assert m.role_of("User") is EntityTenantRole.GLOBAL
        assert m.role_of("Movie") is EntityTenantRole.GLOBAL
        assert m.scoped_entities() == []

    def test_tenant_entity_is_root(self) -> None:
        m = analyze_tenant_scope(
            _mapping(
                [
                    {"name": "Tenant", "properties": [{"name": "NAME"}]},
                ]
            )
        )
        assert m.tenant_entity == "Tenant"
        assert m.role_of("Tenant") is EntityTenantRole.TENANT_ROOT
        assert m.denorm_field_of("Tenant") is None

    def test_denorm_field_marks_entity_scoped_with_field_name(self) -> None:
        # Default regex picks up TENANT_ID, tenant_id, tenantId, tenant_key.
        m = analyze_tenant_scope(
            _mapping(
                [
                    {"name": "Tenant", "properties": [{"name": "NAME"}]},
                    {
                        "name": "Device",
                        "properties": [
                            {"name": "TENANT_ID"},
                            {"name": "MAC"},
                        ],
                    },
                    {
                        "name": "GSuiteUser",
                        "properties": [
                            {"name": "tenant_id"},
                            {"name": "EMAIL"},
                        ],
                    },
                    {
                        "name": "Library",
                        "properties": [
                            {"name": "tenantKey"},
                            {"name": "NAME"},
                        ],
                    },
                ]
            )
        )
        assert m.role_of("Device") is EntityTenantRole.TENANT_SCOPED
        assert m.denorm_field_of("Device") == "TENANT_ID"
        assert m.role_of("GSuiteUser") is EntityTenantRole.TENANT_SCOPED
        assert m.denorm_field_of("GSuiteUser") == "tenant_id"
        assert m.role_of("Library") is EntityTenantRole.TENANT_SCOPED
        assert m.denorm_field_of("Library") == "tenantKey"

    def test_field_name_match_preserves_original_casing(self) -> None:
        # Critical for emitting the right AQL: filter must match the
        # *exact* field name as stored on the document. We must not
        # normalise to upper- or lowercase.
        m = analyze_tenant_scope(
            _mapping(
                [
                    {"name": "Tenant", "properties": []},
                    {"name": "X", "properties": [{"name": "Tenant_Id"}]},
                ]
            )
        )
        assert m.denorm_field_of("X") == "Tenant_Id"

    def test_traversal_only_scoping_when_no_denorm(self) -> None:
        # Library has no tenant column but is reachable from Tenant
        # within the BFS budget → tenant-scoped via traversal only.
        m = analyze_tenant_scope(
            _mapping(
                entities=[
                    {"name": "Tenant", "properties": []},
                    {"name": "Library", "properties": [{"name": "NAME"}]},
                ],
                relationships=[
                    {"type": "TENANTLIBRARY", "from": "Tenant", "to": "Library"},
                ],
            )
        )
        assert m.role_of("Library") is EntityTenantRole.TENANT_SCOPED
        assert m.denorm_field_of("Library") is None

    def test_unreachable_no_field_is_global(self) -> None:
        # Cve has no tenant field and no edge to Tenant → global metadata.
        m = analyze_tenant_scope(
            _mapping(
                entities=[
                    {"name": "Tenant", "properties": []},
                    {"name": "Cve", "properties": [{"name": "ID"}]},
                ],
                relationships=[],
            )
        )
        assert m.role_of("Cve") is EntityTenantRole.GLOBAL
        assert "Cve" in m.global_entities()

    def test_bfs_respects_max_hops(self) -> None:
        # Chain Tenant -> A -> B -> C; with max_hops=2, C is unreachable.
        m = analyze_tenant_scope(
            _mapping(
                entities=[
                    {"name": "Tenant", "properties": []},
                    {"name": "A", "properties": []},
                    {"name": "B", "properties": []},
                    {"name": "C", "properties": []},
                ],
                relationships=[
                    {"type": "AB", "from": "Tenant", "to": "A"},
                    {"type": "BC", "from": "A", "to": "B"},
                    {"type": "CD", "from": "B", "to": "C"},
                ],
            ),
            max_traversal_hops=2,
        )
        assert m.role_of("A") is EntityTenantRole.TENANT_SCOPED
        assert m.role_of("B") is EntityTenantRole.TENANT_SCOPED
        assert m.role_of("C") is EntityTenantRole.GLOBAL

    def test_relationship_dict_endpoint_shape_supported(self) -> None:
        # The schema analyzer sometimes emits {label: ..., ...} dicts.
        m = analyze_tenant_scope(
            _mapping(
                entities=[
                    {"name": "Tenant", "properties": []},
                    {"name": "Device", "properties": []},
                ],
                relationships=[
                    {
                        "type": "TENANTDEVICE",
                        "from": {"label": "Tenant"},
                        "to": {"label": "Device"},
                    },
                ],
            )
        )
        assert m.role_of("Device") is EntityTenantRole.TENANT_SCOPED

    def test_fromEntity_toEntity_endpoint_shape_supported(self) -> None:
        # Regression test for the classifier silently dropping every
        # edge in the conceptual graph when the analyzer emits
        # ``fromEntity`` / ``toEntity`` (the current schema-analyzer
        # shape) rather than ``from`` / ``to``. Before the fix this
        # same fixture produced ``reachable_from_tenant=False`` for
        # ``Device`` and ``GSuiteUser``, misclassifying ``GSuiteUser``
        # as ``GLOBAL`` (because it has no TENANT_ID column either)
        # and exempting it from the tenant guardrail.
        m = analyze_tenant_scope(
            _mapping(
                entities=[
                    {"name": "Tenant", "properties": []},
                    {"name": "Device", "properties": [{"name": "TENANT_ID"}]},
                    {"name": "GSuiteUser", "properties": [{"name": "EMAIL"}]},
                ],
                relationships=[
                    {
                        "type": "TENANTDEVICE",
                        "fromEntity": "Tenant",
                        "toEntity": "Device",
                    },
                    {
                        "type": "DEVICEGSUITEUSER",
                        "fromEntity": "Device",
                        "toEntity": "GSuiteUser",
                    },
                ],
            )
        )
        # Device is reachable AND has denorm → TENANT_SCOPED with field.
        assert m.role_of("Device") is EntityTenantRole.TENANT_SCOPED
        assert m.entities["Device"].reachable_from_tenant is True
        assert m.denorm_field_of("Device") == "TENANT_ID"

        # GSuiteUser has no TENANT_ID but is reachable via Device →
        # must be TENANT_SCOPED (traversal-only), not GLOBAL.
        assert m.role_of("GSuiteUser") is EntityTenantRole.TENANT_SCOPED
        assert m.entities["GSuiteUser"].reachable_from_tenant is True
        assert m.denorm_field_of("GSuiteUser") is None

    def test_reverse_fromEntity_toEntity_edge_still_reaches(self) -> None:
        # Reachability is undirected, so TenantUser --> Tenant should
        # make TenantUser reachable from Tenant too. This mirrors the
        # TENANTUSERTENANT edge we see in the context-model-poc DB.
        m = analyze_tenant_scope(
            _mapping(
                entities=[
                    {"name": "Tenant", "properties": []},
                    {"name": "TenantUser", "properties": []},
                ],
                relationships=[
                    {
                        "type": "TENANTUSERTENANT",
                        "fromEntity": "TenantUser",
                        "toEntity": "Tenant",
                    },
                ],
            )
        )
        assert m.role_of("TenantUser") is EntityTenantRole.TENANT_SCOPED
        assert m.entities["TenantUser"].reachable_from_tenant is True

    def test_string_property_form_supported(self) -> None:
        # Some schemas emit properties as bare strings, not dicts.
        m = analyze_tenant_scope(
            _mapping(
                [
                    {"name": "Tenant", "properties": []},
                    {"name": "Device", "properties": ["TENANT_ID", "MAC"]},
                ]
            )
        )
        assert m.denorm_field_of("Device") == "TENANT_ID"


# ---------------------------------------------------------------------------
# Explicit annotation in the physical mapping (forward-compat with the
# upstream schema-analyzer PR)
# ---------------------------------------------------------------------------


class TestExplicitAnnotationOverride:
    def test_explicit_global_overrides_denorm_heuristic(self) -> None:
        # Even when an entity has a TENANT_ID column, an explicit
        # `tenantScope.role = "global"` annotation must win — used for
        # cases where the column exists but is, say, a vestigial
        # cross-tenant aggregator that intentionally spans tenants.
        m = analyze_tenant_scope(
            _mapping(
                entities=[
                    {"name": "Tenant", "properties": []},
                    {"name": "X", "properties": [{"name": "TENANT_ID"}]},
                ],
                physical_overrides={
                    "X": {"tenantScope": {"role": "global"}},
                },
            )
        )
        assert m.role_of("X") is EntityTenantRole.GLOBAL
        # Explicit GLOBAL must clear any denorm field — surfacing one
        # would mislead the prompt builder.
        assert m.denorm_field_of("X") is None

    def test_explicit_scoped_with_field_overrides_discovery(self) -> None:
        m = analyze_tenant_scope(
            _mapping(
                entities=[
                    {"name": "Tenant", "properties": []},
                    {"name": "Device", "properties": [{"name": "owner_id"}]},
                ],
                physical_overrides={
                    "Device": {
                        "tenantScope": {
                            "role": "tenant_scoped",
                            "tenantField": "owner_id",
                        },
                    },
                },
            )
        )
        assert m.role_of("Device") is EntityTenantRole.TENANT_SCOPED
        assert m.denorm_field_of("Device") == "owner_id"

    def test_invalid_role_falls_back_to_discovery(self) -> None:
        # Garbage role string from a misconfigured deployment must not
        # break translation; we ignore the bad annotation and fall
        # back to heuristic discovery.
        m = analyze_tenant_scope(
            _mapping(
                entities=[
                    {"name": "Tenant", "properties": []},
                    {"name": "Cve", "properties": []},
                ],
                physical_overrides={
                    "Cve": {"tenantScope": {"role": "totally_made_up"}},
                },
            )
        )
        assert m.role_of("Cve") is EntityTenantRole.GLOBAL


class TestUpstreamAnalyzerFullyAnnotated:
    """As of arangodb-schema-analyzer v0.4 (issue #13), every entity in
    the physical mapping carries a ``tenantScope`` block. These tests
    pin the post-0.4.0 reality: the upstream annotation is the source
    of truth and the local heuristic should be a no-op.

    Concretely they assert that a mapping which would be classified
    DIFFERENTLY by the local heuristic still ends up matching the
    upstream annotations — proving precedence is correct."""

    def _fully_annotated_mapping(self) -> dict:
        """Mapping shaped like a real v0.4 analyzer export. The
        annotations DELIBERATELY contradict what the local heuristic
        would derive:

        * ``Device`` has a ``TENANT_ID`` column but the annotation
          marks it ``global`` (operator override scenario).
        * ``Library`` has no column or relationship but the
          annotation marks it ``tenant_scoped`` via traversal-only
          (an analyzer that knows about a relationship the local
          heuristic doesn't see).
        * ``Cve`` has no column and no edge, so both the heuristic
          and the annotation agree on ``global``.
        """
        return {
            "conceptual_schema": {
                "entities": [
                    {"name": "Tenant", "properties": []},
                    {"name": "Device", "properties": [{"name": "TENANT_ID"}]},
                    {"name": "Library", "properties": []},
                    {"name": "Cve", "properties": []},
                ],
                "relationships": [],
            },
            "physical_mapping": {
                "entities": {
                    "Tenant": {
                        "style": "COLLECTION",
                        "collectionName": "Tenant",
                        "tenantScope": {"role": "tenant_root"},
                    },
                    "Device": {
                        "style": "COLLECTION",
                        "collectionName": "Device",
                        "tenantScope": {"role": "global"},
                    },
                    "Library": {
                        "style": "COLLECTION",
                        "collectionName": "Library",
                        "tenantScope": {
                            "role": "tenant_scoped",
                            "tenantEntity": "Tenant",
                        },
                    },
                    "Cve": {
                        "style": "COLLECTION",
                        "collectionName": "Cve",
                        "tenantScope": {"role": "global"},
                    },
                },
                "relationships": {},
            },
        }

    def test_upstream_annotations_win_over_local_heuristic(self) -> None:
        m = analyze_tenant_scope(self._fully_annotated_mapping())

        assert m.tenant_entity == "Tenant"
        assert m.role_of("Tenant") is EntityTenantRole.TENANT_ROOT

        # Heuristic alone would mark Device tenant_scoped (TENANT_ID
        # field present); annotation forces global.
        assert m.role_of("Device") is EntityTenantRole.GLOBAL
        assert m.denorm_field_of("Device") is None

        # Heuristic alone would mark Library global (no field, no
        # edge); annotation forces tenant_scoped via traversal.
        assert m.role_of("Library") is EntityTenantRole.TENANT_SCOPED
        assert m.denorm_field_of("Library") is None

        assert m.role_of("Cve") is EntityTenantRole.GLOBAL

    def test_fully_annotated_mapping_logs_upstream_short_circuit(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Observability: a fully-annotated mapping should emit the
        DEBUG line confirming the local heuristic was bypassed. This
        is what we'll grep for in production logs to track migration
        completeness across deployments."""
        import logging

        caplog.set_level(logging.DEBUG, logger="arango_cypher.nl2cypher.tenant_scope")

        analyze_tenant_scope(self._fully_annotated_mapping())

        msgs = [r.message for r in caplog.records]
        assert any("classified 4/4 entities from upstream" in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# Regex configuration via env var
# ---------------------------------------------------------------------------


class TestRegexConfig:
    def test_env_var_override_picks_up_customer_id(self) -> None:
        # ISVs that call tenants "customers" can override the field
        # pattern without re-deploying. The default would not match
        # `customer_id`.
        with patch.dict(
            os.environ,
            {"NL2CYPHER_TENANT_FIELD_REGEX": r"^customer[_-]?id$"},
        ):
            m = analyze_tenant_scope(
                _mapping(
                    [
                        {"name": "Tenant", "properties": []},
                        {"name": "Device", "properties": [{"name": "customer_id"}]},
                    ]
                )
            )
            assert m.role_of("Device") is EntityTenantRole.TENANT_SCOPED
            assert m.denorm_field_of("Device") == "customer_id"

    def test_invalid_env_regex_falls_back_silently(self) -> None:
        # Bad pattern in env must not crash translation. We drop back
        # to the default regex and continue.
        with patch.dict(
            os.environ,
            {"NL2CYPHER_TENANT_FIELD_REGEX": r"["},  # invalid
        ):
            m = analyze_tenant_scope(
                _mapping(
                    [
                        {"name": "Tenant", "properties": []},
                        {"name": "Device", "properties": [{"name": "TENANT_ID"}]},
                    ]
                )
            )
            assert m.denorm_field_of("Device") == "TENANT_ID"

    def test_explicit_regex_argument_overrides_env(self) -> None:
        with patch.dict(
            os.environ,
            {"NL2CYPHER_TENANT_FIELD_REGEX": r"^never_matches$"},
        ):
            m = analyze_tenant_scope(
                _mapping(
                    [
                        {"name": "Tenant", "properties": []},
                        {"name": "Device", "properties": [{"name": "TENANT_ID"}]},
                    ]
                ),
                tenant_field_regex=re.compile(r"^TENANT_ID$"),
            )
            assert m.denorm_field_of("Device") == "TENANT_ID"


# ---------------------------------------------------------------------------
# Bundle / dict shape compatibility
# ---------------------------------------------------------------------------


class TestMappingShapeCompat:
    def test_camelcase_keys_supported(self) -> None:
        m = analyze_tenant_scope(
            {
                "conceptualSchema": {
                    "entities": [
                        {"name": "Tenant", "properties": []},
                        {"name": "Device", "properties": [{"name": "TENANT_ID"}]},
                    ],
                    "relationships": [],
                },
                "physicalMapping": {"entities": {}, "relationships": {}},
            }
        )
        assert m.tenant_entity == "Tenant"
        assert m.denorm_field_of("Device") == "TENANT_ID"

    def test_object_with_attributes_supported(self) -> None:
        # Mimics the dataclass shape returned by the schema analyzer.
        from types import SimpleNamespace

        bundle = SimpleNamespace(
            conceptual_schema={
                "entities": [
                    {"name": "Tenant", "properties": []},
                    {"name": "Device", "properties": [{"name": "TENANT_ID"}]},
                ],
                "relationships": [],
            },
            physical_mapping={"entities": {}, "relationships": {}},
        )
        m = analyze_tenant_scope(bundle)
        assert m.role_of("Device") is EntityTenantRole.TENANT_SCOPED

    def test_completely_empty_mapping_is_safe(self) -> None:
        m = analyze_tenant_scope({})
        assert m.tenant_entity is None
        assert m.entities == {}

    def test_default_role_for_unknown_entity_is_global(self) -> None:
        m = analyze_tenant_scope(
            _mapping(
                [
                    {"name": "Tenant", "properties": []},
                ]
            )
        )
        # Asking about an entity not in the manifest must return
        # GLOBAL, not raise — the guardrail relies on this so a typo
        # in a Cypher label doesn't crash the translator.
        assert m.role_of("DoesNotExist") is EntityTenantRole.GLOBAL
        assert m.denorm_field_of("DoesNotExist") is None


# ---------------------------------------------------------------------------
# Realistic Dagster-like schema sanity check
# ---------------------------------------------------------------------------


@pytest.fixture
def dagster_like() -> dict:
    return _mapping(
        entities=[
            {"name": "Tenant", "properties": [{"name": "NAME"}, {"name": "TENANT_HEX_ID"}]},
            {"name": "Device", "properties": [{"name": "TENANT_ID"}, {"name": "MAC"}]},
            {"name": "GSuiteUser", "properties": [{"name": "TENANT_ID"}, {"name": "EMAIL"}]},
            {"name": "Library", "properties": [{"name": "NAME"}]},  # no TENANT_ID
            {"name": "Cve", "properties": [{"name": "ID"}, {"name": "SEVERITY"}]},  # global
            {"name": "AppVersion", "properties": [{"name": "VERSION"}]},  # global
        ],
        relationships=[
            {"type": "TENANTDEVICE", "from": "Tenant", "to": "Device"},
            {"type": "TENANTGSUITEUSER", "from": "Tenant", "to": "GSuiteUser"},
            {"type": "TENANTLIBRARY", "from": "Tenant", "to": "Library"},
            # No edge from Tenant to Cve or AppVersion.
        ],
    )


class TestDagsterLikeSchema:
    def test_classification(self, dagster_like) -> None:
        m = analyze_tenant_scope(dagster_like)
        assert m.tenant_entity == "Tenant"
        # Tenant root.
        assert m.role_of("Tenant") is EntityTenantRole.TENANT_ROOT
        # Denorm scoped.
        assert m.role_of("Device") is EntityTenantRole.TENANT_SCOPED
        assert m.denorm_field_of("Device") == "TENANT_ID"
        assert m.role_of("GSuiteUser") is EntityTenantRole.TENANT_SCOPED
        assert m.denorm_field_of("GSuiteUser") == "TENANT_ID"
        # Traversal-only scoped.
        assert m.role_of("Library") is EntityTenantRole.TENANT_SCOPED
        assert m.denorm_field_of("Library") is None
        # Global metadata.
        assert m.role_of("Cve") is EntityTenantRole.GLOBAL
        assert m.role_of("AppVersion") is EntityTenantRole.GLOBAL
        assert set(m.global_entities()) == {"Cve", "AppVersion"}
