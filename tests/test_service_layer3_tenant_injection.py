"""MT-3b — service wiring for Layer 3 (Cypher AST tenant injection).

The core rewriter (``tenant_ast_cypher.inject_tenant_scope``) is covered by
``test_tenant_ast_cypher.py``. These tests pin the route-adapter
(``safe_exec.apply_layer3_rewrite``) gating that MT-3b adds:

* tenant-unbound sessions bypass the rewriter (workbench / single-tenant);
* tenant-bound sessions with no mapping skip (Layer 5 remains the boundary);
* tenant-bound + mapping → the scoped node pattern gets the bind-var predicate;
* a literal tenant predicate is refused (propagates as a rejection → HTTP 403);
* a traversal-only scoped entity (MT-3a defers) falls back to the original Cypher.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arango_cypher.service.safe_exec import (
    TenantScopeRewriteRejection,
    apply_layer3_rewrite,
)


def _mapping() -> dict:
    return {
        "conceptual_schema": {
            "entities": [
                {"name": "Tenant", "properties": []},
                {"name": "Employee", "properties": [{"name": "name"}]},
                {"name": "Device", "properties": []},
            ],
            "relationships": [],
        },
        "physical_mapping": {
            "entities": {
                "Tenant": {"style": "COLLECTION", "collectionName": "Tenant"},
                "Employee": {
                    "style": "COLLECTION",
                    "collectionName": "Employee",
                    # Explicit annotation → TENANT_SCOPED with a denorm field
                    # (the property-map injection path).
                    "tenantScope": {"role": "tenant_scoped", "tenantField": "TENANT_HEX_ID"},
                },
                "Device": {
                    "style": "COLLECTION",
                    "collectionName": "Device",
                    # Scoped but no denorm field → traversal-only → MT-3a raises
                    # Incomplete → adapter falls back to the original.
                    "tenantScope": {"role": "tenant_scoped"},
                },
            },
            "relationships": {},
        },
        "metadata": {},
    }


def _session(tenant_id: str | None = "tenant-A-uuid"):
    return SimpleNamespace(tenant_id=tenant_id, tenant_key=tenant_id, token="tok12345", db=None)


class TestApplyLayer3Rewrite:
    def test_unbound_session_is_noop(self) -> None:
        cy = "MATCH (e:Employee) RETURN e"
        out, changes = apply_layer3_rewrite(
            cypher=cy, mapping_dict=_mapping(), session=_session(tenant_id=None)
        )
        assert out == cy
        assert changes == []

    def test_no_mapping_skips(self) -> None:
        cy = "MATCH (e:Employee) RETURN e"
        out, changes = apply_layer3_rewrite(cypher=cy, mapping_dict=None, session=_session())
        assert out == cy
        assert changes == []

    def test_scoped_entity_gets_bindvar_predicate(self) -> None:
        out, changes = apply_layer3_rewrite(
            cypher="MATCH (e:Employee) RETURN e",
            mapping_dict=_mapping(),
            session=_session(),
        )
        assert "TENANT_HEX_ID: $tenantId" in out
        assert changes and "TENANT_HEX_ID" in changes[0]

    def test_idempotent_when_bindvar_already_present(self) -> None:
        # A query that already references the tenant bind var is left unchanged.
        cy = "MATCH (e:Employee {TENANT_HEX_ID: $tenantId}) RETURN e"
        out, changes = apply_layer3_rewrite(
            cypher=cy, mapping_dict=_mapping(), session=_session()
        )
        assert out == cy
        assert changes == []

    def test_literal_tenant_predicate_rejected(self) -> None:
        with pytest.raises(TenantScopeRewriteRejection) as exc:
            apply_layer3_rewrite(
                cypher="MATCH (e:Employee {TENANT_HEX_ID: 'tenant-B-uuid'}) RETURN e",
                mapping_dict=_mapping(),
                session=_session(),
            )
        assert exc.value.code == "LITERAL_TENANT_PREDICATE"

    def test_incomplete_rewrite_falls_back_to_original(self) -> None:
        # Multi-label patterns are deferred by MT-3a (raise Incomplete); the
        # adapter must fall back to the original Cypher (Layer 5 enforces).
        cy = "MATCH (e:Employee:Person) RETURN e"
        out, changes = apply_layer3_rewrite(
            cypher=cy, mapping_dict=_mapping(), session=_session()
        )
        assert out == cy
        assert changes == []

    def test_unparseable_cypher_is_noop(self) -> None:
        cy = "this is not cypher {{{"
        out, changes = apply_layer3_rewrite(
            cypher=cy, mapping_dict=_mapping(), session=_session()
        )
        assert out == cy
        assert changes == []
