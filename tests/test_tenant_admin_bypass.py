"""MT-7 — admin cross-tenant bypass (Layer 5 + Layer 6 adapter).

Pins the boundary conditions from ``docs/multitenant_prd.md`` §10:

* An authorised admin (``is_admin`` + a non-empty ``bypass_reason``) may
  run a query that would otherwise be refused for spanning tenants; the
  event is recorded on the ``arango_cypher.tenant_audit`` stream.
* Structural checks are NOT bypassed — a failed / malformed EXPLAIN is
  still refused.
* A bypass requested without admin rights, or without a reason, is
  ignored (normal tenant-scope enforcement still applies) — defence in
  depth behind the route's own gate.
* The adapter (:func:`safe_execute_aql`) skips the no-mapping
  fail-closed refusal for an authorised bypass and threads the flag to
  Layer 5.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest

from arango_cypher.nl2cypher.tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
)
from arango_cypher.service.safe_exec import safe_execute_aql
from arango_cypher.tenant_plan_validator import TenantScopeViolation, validate_plan


@dataclass
class _Session:
    token: str = "admin-token"
    tenant_id: str | None = "tenant-A-uuid"
    tenant_key: str | None = "tenant-A-uuid"
    is_admin: bool = True


def _manifest() -> TenantScopeManifest:
    return TenantScopeManifest(
        tenant_entity="Tenant",
        entities={
            "Tenant": EntityScope(role=EntityTenantRole.TENANT_ROOT, reachable_from_tenant=True),
            "Employee": EntityScope(
                role=EntityTenantRole.TENANT_SCOPED,
                denorm_field="TENANT_HEX_ID",
                reachable_from_tenant=True,
            ),
        },
    )


def _sharding() -> dict[str, Any]:
    return {
        "members": {
            "Tenant": {"kind": "tenant-root"},
            "Employee": {"kind": "smartgraph"},
        },
        "graphs": [
            {
                "name": "G",
                "smartGraphAttribute": "TENANT_HEX_ID",
                "isDisjoint": True,
                "vertexCollections": ["Tenant", "Employee"],
            }
        ],
    }


def _unconstrained_plan() -> dict[str, Any]:
    """Employee scan with no tenant predicate → normally UNCONSTRAINED."""
    return {
        "nodes": [
            {"type": "SingletonNode", "id": 1},
            {
                "type": "EnumerateCollectionNode",
                "id": 2,
                "collection": "Employee",
                "outVariable": {"name": "doc", "id": 100},
            },
            {"type": "ReturnNode", "id": 3},
        ]
    }


# ---------------------------------------------------------------------------
# validate_plan — bypass semantics (via plan_override, no db needed)
# ---------------------------------------------------------------------------


class TestValidatePlanAdminBypass:
    def test_authorised_bypass_allows_unconstrained_scan(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING, logger="arango_cypher.tenant_audit"):
            validate_plan(
                db=None,
                aql="FOR e IN Employee RETURN e",
                bind_vars={},
                manifest=_manifest(),
                sharding_profile=_sharding(),
                collection_to_entity=None,
                session=_Session(),
                plan_override=_unconstrained_plan(),
                admin_bypass=True,
                bypass_reason="INCIDENT-42 cross-tenant audit",
            )
        messages = [r.getMessage() for r in caplog.records]
        assert any("ADMIN_CROSS_TENANT_BYPASS" in m for m in messages)
        assert any("INCIDENT-42" in m for m in messages)

    def test_non_admin_bypass_is_ignored(self):
        with pytest.raises(TenantScopeViolation) as exc:
            validate_plan(
                db=None,
                aql="FOR e IN Employee RETURN e",
                bind_vars={},
                manifest=_manifest(),
                sharding_profile=_sharding(),
                collection_to_entity=None,
                session=_Session(is_admin=False),
                plan_override=_unconstrained_plan(),
                admin_bypass=True,
                bypass_reason="not enough on its own",
            )
        assert exc.value.code == "UNCONSTRAINED_COLLECTION_SCAN"

    def test_empty_reason_is_ignored(self):
        with pytest.raises(TenantScopeViolation) as exc:
            validate_plan(
                db=None,
                aql="FOR e IN Employee RETURN e",
                bind_vars={},
                manifest=_manifest(),
                sharding_profile=_sharding(),
                collection_to_entity=None,
                session=_Session(),  # admin, but no reason below
                plan_override=_unconstrained_plan(),
                admin_bypass=True,
                bypass_reason="",
            )
        assert exc.value.code == "UNCONSTRAINED_COLLECTION_SCAN"

    def test_no_bypass_flag_still_refuses(self):
        with pytest.raises(TenantScopeViolation) as exc:
            validate_plan(
                db=None,
                aql="FOR e IN Employee RETURN e",
                bind_vars={},
                manifest=_manifest(),
                sharding_profile=_sharding(),
                collection_to_entity=None,
                session=_Session(),
                plan_override=_unconstrained_plan(),
            )
        assert exc.value.code == "UNCONSTRAINED_COLLECTION_SCAN"

    def test_structural_failure_not_bypassed(self):
        """A failed EXPLAIN is refused even for an authorised admin."""

        class _BoomAql:
            def explain(self, aql: str, bind_vars: dict[str, Any] | None = None):
                raise RuntimeError("planner exploded")

        class _BoomDb:
            aql = _BoomAql()

        with pytest.raises(TenantScopeViolation) as exc:
            validate_plan(
                db=_BoomDb(),
                aql="FOR e IN Employee RETURN e",
                bind_vars={},
                manifest=_manifest(),
                sharding_profile=_sharding(),
                collection_to_entity=None,
                session=_Session(),
                admin_bypass=True,
                bypass_reason="INCIDENT-42",
            )
        assert exc.value.code == "EXPLAIN_FAILED"


# ---------------------------------------------------------------------------
# safe_execute_aql adapter — bypass threading + no-mapping allowance
# ---------------------------------------------------------------------------


class _FakeAql:
    def __init__(self, plan: dict[str, Any]):
        self._plan = plan
        self.explain_calls = 0
        self.exec_calls = 0

    def explain(self, aql: str, bind_vars: dict[str, Any] | None = None) -> dict[str, Any]:
        self.explain_calls += 1
        return {"plan": self._plan, "warnings": []}

    def execute(self, aql: str, bind_vars: dict[str, Any] | None = None, **kwargs: Any) -> list[str]:
        self.exec_calls += 1
        return ["row-1", "row-2"]


class _FakeDb:
    def __init__(self, plan: dict[str, Any]):
        self.aql = _FakeAql(plan)


_MAPPING = {
    "conceptual_schema": {
        "entities": [
            {"name": "Tenant", "properties": []},
            {"name": "Employee", "properties": [{"name": "name"}]},
        ],
        "relationships": [],
    },
    "physical_mapping": {
        "entities": {
            "Tenant": {"style": "COLLECTION", "collectionName": "Tenant"},
            "Employee": {
                "style": "COLLECTION",
                "collectionName": "Employee",
                "tenantScope": {"role": "tenant_scoped", "tenantField": "TENANT_HEX_ID"},
            },
        },
        "relationships": {},
    },
    "metadata": {
        "shardingProfile": {
            "members": {"Employee": {"kind": "smartgraph"}, "Tenant": {"kind": "tenant-root"}},
            "graphs": [
                {
                    "name": "G",
                    "smartGraphAttribute": "TENANT_HEX_ID",
                    "isDisjoint": True,
                    "vertexCollections": ["Tenant", "Employee"],
                }
            ],
        }
    },
}


class TestSafeExecuteAqlAdminBypass:
    def test_authorised_bypass_executes_unscoped_query(self):
        db = _FakeDb(_unconstrained_plan())
        cursor, _bind = safe_execute_aql(
            db=db,
            aql="FOR e IN Employee RETURN e",
            bind_vars={},
            session=_Session(),
            mapping_dict=_MAPPING,
            admin_bypass=True,
            bypass_reason="INCIDENT-42",
        )
        assert list(cursor) == ["row-1", "row-2"]
        assert db.aql.exec_calls == 1

    def test_authorised_bypass_allows_missing_mapping(self):
        db = _FakeDb(_unconstrained_plan())
        cursor, _bind = safe_execute_aql(
            db=db,
            aql="FOR e IN Employee RETURN e",
            bind_vars={},
            session=_Session(),
            mapping_dict=None,
            admin_bypass=True,
            bypass_reason="INCIDENT-42",
        )
        assert list(cursor) == ["row-1", "row-2"]

    def test_non_admin_bypass_flag_still_enforced_at_adapter(self):
        db = _FakeDb(_unconstrained_plan())
        with pytest.raises(TenantScopeViolation) as exc:
            safe_execute_aql(
                db=db,
                aql="FOR e IN Employee RETURN e",
                bind_vars={},
                session=_Session(is_admin=False),
                mapping_dict=_MAPPING,
                admin_bypass=True,
                bypass_reason="INCIDENT-42",
            )
        assert exc.value.code == "UNCONSTRAINED_COLLECTION_SCAN"
        assert db.aql.exec_calls == 0
