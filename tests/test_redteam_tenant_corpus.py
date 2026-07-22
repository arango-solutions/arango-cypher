"""MT-8 — red-team corpus for the tenant-safety boundary (Layer 5).

Per ``docs/multitenant_prd.md`` §11: *"every known attempted escape
becomes a test case that the validator must refuse."* This module is the
single, extensible registry of those escapes. Each case is a concrete
cross-tenant attack expressed as the EXPLAIN plan Layer 5 would see; the
validator MUST refuse it with one of the expected codes. When a new escape
vector is discovered, add a ``_RedTeamCase`` here first (it should fail),
then close the hole in ``arango_cypher/tenant_plan_validator.py``.

The corpus is organised by the PRD threat model:

* **T1 — underconstraint**: a scan/traversal with no tenant predicate.
* **T2 — injection**: a literal (foreign / smuggled) tenant predicate,
  including ``OR``-smuggling and fused-inline-filter forms.
* **T4 — cross-tenant traversal**: a graph traversal not constrained to
  the session tenant.
* **T7 — bind-var override**: a plan whose ``@tenantId`` bind disagrees
  with the session tenant.
* **Privilege escalation (MT-7)**: an unauthorised attempt to use the
  admin cross-tenant bypass.
* **Cache poisoning (MT-6 × MT-7)**: an admin bypass must never leave a
  "safe" certification that a later non-admin call could reuse.

Layer 5 is *the* security boundary (PRD §1.2): these run against
``validate_plan`` directly with hand-crafted plans, so a refusal here is
independent of Layers 2/3/4 doing their job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from arango_cypher.nl2cypher.tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
)
from arango_cypher.tenant_plan_validator import (
    TenantScopeViolation,
    plan_cache_stats,
    validate_plan,
)


@dataclass
class _Session:
    token: str = "session-A"
    tenant_id: str | None = "tenant-A-uuid"
    tenant_key: str | None = "tenant-A-uuid"
    is_admin: bool = False


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
            "Country": EntityScope(role=EntityTenantRole.GLOBAL),
        },
    )


def _sharding() -> dict[str, Any]:
    return {
        "members": {
            "Tenant": {"kind": "tenant-root"},
            "Employee": {"kind": "smartgraph"},
            "Country": {"kind": "satellite"},
        },
        "graphs": [
            {
                "name": "TenantGraph",
                "smartGraphAttribute": "TENANT_HEX_ID",
                "isDisjoint": True,
                "vertexCollections": ["Tenant", "Employee"],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Plan-fragment builders (mirror ArangoDB EXPLAIN node shapes)
# ---------------------------------------------------------------------------


def _attr(var: str, name: str) -> dict[str, Any]:
    return {
        "type": "attribute access",
        "name": name,
        "subNodes": [{"type": "reference", "name": var, "id": 100}],
    }


def _param(name: str) -> dict[str, Any]:
    return {"type": "parameter", "name": name}


def _val(v: Any) -> dict[str, Any]:
    return {"type": "value", "value": v}


def _eq(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {"type": "compare ==", "subNodes": [a, b]}


def _or(*xs: dict[str, Any]) -> dict[str, Any]:
    return {"type": "n-ary or", "subNodes": list(xs)}


def _enum(collection: str, *, flt: dict[str, Any] | None = None, nid: int = 2) -> dict[str, Any]:
    node = {
        "type": "EnumerateCollectionNode",
        "id": nid,
        "collection": collection,
        "outVariable": {"name": "doc", "id": 100},
    }
    if flt is not None:
        node["filter"] = flt
    return node


def _index(collection: str, *, condition: dict[str, Any], nid: int = 2) -> dict[str, Any]:
    return {
        "type": "IndexNode",
        "id": nid,
        "collection": collection,
        "outVariable": {"name": "doc", "id": 100},
        "condition": condition,
    }


def _traversal(*, graph_name: str, vertex_collections: list[str], nid: int = 2) -> dict[str, Any]:
    return {
        "type": "TraversalNode",
        "id": nid,
        "graphName": graph_name,
        "graph": {"vertexCollections": vertex_collections},
    }


def _subquery(inner: list[dict[str, Any]], *, nid: int = 2) -> dict[str, Any]:
    return {"type": "SubqueryNode", "id": nid, "subquery": {"nodes": inner}}


def _plan(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"nodes": [{"type": "SingletonNode", "id": 1}, *nodes, {"type": "ReturnNode", "id": 99}]}


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


@dataclass
class _RedTeamCase:
    name: str
    threat: str
    plan: dict[str, Any]
    expected_codes: frozenset[str]
    bind_vars: dict[str, Any] = field(default_factory=dict)
    session: _Session = field(default_factory=_Session)


_CORPUS: list[_RedTeamCase] = [
    _RedTeamCase(
        name="T1_unconstrained_scan",
        threat="T1",
        plan=_plan(_enum("Employee")),
        expected_codes=frozenset({"UNCONSTRAINED_COLLECTION_SCAN"}),
    ),
    _RedTeamCase(
        name="T2_foreign_literal_inline_filter",
        threat="T2",
        plan=_plan(_enum("Employee", flt=_eq(_attr("doc", "TENANT_HEX_ID"), _val("tenant-B-uuid")))),
        expected_codes=frozenset({"LITERAL_TENANT_PREDICATE"}),
    ),
    _RedTeamCase(
        name="T2_or_smuggled_foreign_literal",
        threat="T2",
        # `tenant == @tenantId OR tenant == 'tenant-B'` — OR must not be
        # treated as scoping, and the foreign literal must be caught.
        plan=_plan(
            _enum(
                "Employee",
                flt=_or(
                    _eq(_attr("doc", "TENANT_HEX_ID"), _param("tenantId")),
                    _eq(_attr("doc", "TENANT_HEX_ID"), _val("tenant-B-uuid")),
                ),
            )
        ),
        expected_codes=frozenset({"LITERAL_TENANT_PREDICATE"}),
    ),
    _RedTeamCase(
        name="T2_or_tautology_bypass",
        threat="T2",
        # `tenant == @tenantId OR 1 == 1` — the OR neutralises the tenant
        # filter; there's no foreign literal on the tenant field, so this
        # must fall through to the unconstrained-scan refusal.
        plan=_plan(
            _enum(
                "Employee",
                flt=_or(
                    _eq(_attr("doc", "TENANT_HEX_ID"), _param("tenantId")),
                    _eq(_val(1), _val(1)),
                ),
            )
        ),
        expected_codes=frozenset({"UNCONSTRAINED_COLLECTION_SCAN"}),
    ),
    _RedTeamCase(
        name="T2_index_without_tenant_predicate",
        threat="T2",
        plan=_plan(_index("Employee", condition={})),
        expected_codes=frozenset({"INDEX_MISSING_TENANT_PREDICATE"}),
    ),
    _RedTeamCase(
        name="T2_tenant_root_unconstrained",
        threat="T2",
        plan=_plan(_enum("Tenant")),
        expected_codes=frozenset({"TENANT_ROOT_UNCONSTRAINED"}),
    ),
    _RedTeamCase(
        name="T4_unconstrained_traversal",
        threat="T4",
        plan=_plan(_traversal(graph_name="RogueGraph", vertex_collections=["Employee"])),
        expected_codes=frozenset({"UNCONSTRAINED_TRAVERSAL"}),
    ),
    _RedTeamCase(
        name="T5_subquery_hides_unconstrained_scan",
        threat="T5",
        plan=_plan(_subquery([{"type": "SingletonNode", "id": 10}, _enum("Employee", nid=11)])),
        expected_codes=frozenset({"UNCONSTRAINED_COLLECTION_SCAN"}),
    ),
    _RedTeamCase(
        name="T7_bind_var_override_mismatch",
        threat="T7",
        # A structurally-valid tenant predicate, but the bind disagrees
        # with the session tenant — the override defence must fire.
        plan=_plan(_enum("Employee", flt=_eq(_attr("doc", "TENANT_HEX_ID"), _param("tenantId")))),
        expected_codes=frozenset({"TENANT_BIND_MISMATCH"}),
        bind_vars={"tenantId": "tenant-B-uuid"},
    ),
    _RedTeamCase(
        name="T1_no_session_tenant",
        threat="T1",
        # Tenant-touching scan while the session carries no tenant at all.
        plan=_plan(_enum("Employee", flt=_eq(_attr("doc", "TENANT_HEX_ID"), _param("tenantId")))),
        expected_codes=frozenset({"NO_SESSION_TENANT"}),
        session=_Session(tenant_id=None, tenant_key=None),
    ),
]


@pytest.mark.parametrize("case", _CORPUS, ids=[c.name for c in _CORPUS])
def test_redteam_case_is_refused(case: _RedTeamCase):
    with pytest.raises(TenantScopeViolation) as exc:
        validate_plan(
            db=None,
            aql=f"-- redteam:{case.name}",
            bind_vars=case.bind_vars,
            manifest=_manifest(),
            sharding_profile=_sharding(),
            collection_to_entity=None,
            session=case.session,
            plan_override=case.plan,
        )
    assert exc.value.code in case.expected_codes, (
        f"{case.name} ({case.threat}) refused with {exc.value.code!r}, "
        f"expected one of {sorted(case.expected_codes)}"
    )


def test_corpus_covers_each_threat_class():
    """Guard against silently dropping a whole threat class from the corpus."""
    threats = {c.threat for c in _CORPUS}
    assert {"T1", "T2", "T4", "T5", "T7"}.issubset(threats)


# ---------------------------------------------------------------------------
# Privilege escalation — abusing the MT-7 admin bypass
# ---------------------------------------------------------------------------


class TestPrivilegeEscalation:
    def test_non_admin_cannot_bypass(self):
        with pytest.raises(TenantScopeViolation) as exc:
            validate_plan(
                db=None,
                aql="-- redteam:non_admin_bypass",
                bind_vars={},
                manifest=_manifest(),
                sharding_profile=_sharding(),
                collection_to_entity=None,
                session=_Session(is_admin=False),
                plan_override=_plan(_enum("Employee")),
                admin_bypass=True,
                bypass_reason="please let me in",
            )
        assert exc.value.code == "UNCONSTRAINED_COLLECTION_SCAN"

    def test_admin_without_reason_cannot_bypass(self):
        with pytest.raises(TenantScopeViolation) as exc:
            validate_plan(
                db=None,
                aql="-- redteam:admin_no_reason",
                bind_vars={},
                manifest=_manifest(),
                sharding_profile=_sharding(),
                collection_to_entity=None,
                session=_Session(is_admin=True),
                plan_override=_plan(_enum("Employee")),
                admin_bypass=True,
                bypass_reason="",
            )
        assert exc.value.code == "UNCONSTRAINED_COLLECTION_SCAN"


# ---------------------------------------------------------------------------
# Cache poisoning — MT-6 (plan LRU) × MT-7 (bypass) must not interact
# ---------------------------------------------------------------------------


class _FakeAql:
    def __init__(self, plan: dict[str, Any]):
        self._plan = plan
        self.explain_calls = 0

    def explain(self, aql: str, bind_vars: dict[str, Any] | None = None) -> dict[str, Any]:
        self.explain_calls += 1
        return {"plan": self._plan, "warnings": []}


class _FakeDb:
    def __init__(self, plan: dict[str, Any]):
        self.aql = _FakeAql(plan)


class TestBypassDoesNotPoisonCache:
    def test_admin_bypass_does_not_certify_shape_for_others(self):
        """An admin bypass on an unsafe shape must NOT populate the plan
        cache — a subsequent non-admin call on the same (aql, schema)
        shape must still be refused (and re-EXPLAINed)."""
        aql = "FOR e IN Employee RETURN e"
        db = _FakeDb(_plan(_enum("Employee")))  # unconstrained shape

        # 1) Authorised admin bypass — allowed, but must not cache a pass.
        validate_plan(
            db=db,
            aql=aql,
            bind_vars={},
            manifest=_manifest(),
            sharding_profile=_sharding(),
            collection_to_entity=None,
            session=_Session(is_admin=True),
            admin_bypass=True,
            bypass_reason="INCIDENT-7",
        )
        assert plan_cache_stats()["size"] == 0

        # 2) Non-admin, same shape — must be refused, and must re-EXPLAIN
        #    (no cached "pass" to ride on).
        with pytest.raises(TenantScopeViolation) as exc:
            validate_plan(
                db=db,
                aql=aql,
                bind_vars={},
                manifest=_manifest(),
                sharding_profile=_sharding(),
                collection_to_entity=None,
                session=_Session(is_admin=False),
            )
        assert exc.value.code == "UNCONSTRAINED_COLLECTION_SCAN"
        assert db.aql.explain_calls == 2
