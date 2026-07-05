"""MT-6 — Layer 5 plan-shape certification LRU.

The structural pass/fail of :func:`validate_plan` depends only on the AQL
text and the schema (the EXPLAIN sentinel randomises the tenant *value*),
so a certified ``(aql, schema)`` shape can be reused across sessions and
bind-var values. These tests pin the cache contract:

* a repeat of the same shape skips the EXPLAIN round-trip (hit);
* the per-call bind-var gate (``NO_SESSION_TENANT`` / ``TENANT_BIND_MISMATCH``)
  still runs on a hit — the cache never skips the session-identity check;
* any schema or AQL change invalidates (different key → miss);
* violations and ``plan_override`` calls are never cached;
* the underlying LRU honours ``maxsize`` (incl. the ``0`` kill-switch) and TTL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from arango_cypher.nl2cypher.tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
)
from arango_cypher import tenant_plan_validator as tpv
from arango_cypher.tenant_plan_validator import (
    TenantScopeViolation,
    _CachedCertification,
    _PlanCertificationCache,
    plan_cache_stats,
    reset_plan_cache,
    validate_plan,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_plan_cache()
    yield
    reset_plan_cache()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    token: str = "session-A"
    tenant_id: str | None = "tenant-A-uuid"
    tenant_key: str | None = "tenant-A-uuid"
    is_admin: bool = False


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


def _passing_plan() -> dict[str, Any]:
    """Employee scan with a ``doc.TENANT_HEX_ID == @tenantId`` predicate."""
    return {
        "nodes": [
            {"type": "SingletonNode", "id": 1},
            {
                "type": "EnumerateCollectionNode",
                "id": 2,
                "collection": "Employee",
                "outVariable": {"name": "doc", "id": 100},
            },
            {
                "type": "CalculationNode",
                "id": 3,
                "outVariable": {"name": "_calc3", "id": 203},
                "expression": {
                    "type": "compare ==",
                    "subNodes": [
                        {
                            "type": "attribute access",
                            "name": "TENANT_HEX_ID",
                            "subNodes": [{"type": "reference", "name": "doc", "id": 100}],
                        },
                        {"type": "parameter", "name": "tenantId"},
                    ],
                },
            },
            {"type": "FilterNode", "id": 4, "inVariable": {"name": "_calc3", "id": 203}},
            {"type": "ReturnNode", "id": 5},
        ]
    }


def _unconstrained_plan() -> dict[str, Any]:
    """Employee scan with no tenant predicate → UNCONSTRAINED_COLLECTION_SCAN."""
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


def _validate(
    db: _FakeDb,
    *,
    aql: str = "FOR e IN Employee FILTER e.TENANT_HEX_ID == @tenantId RETURN e",
    bind_vars: dict[str, Any] | None = None,
    session: _Session | None = None,
    sharding: dict[str, Any] | None = None,
) -> None:
    validate_plan(
        db=db,
        aql=aql,
        bind_vars=bind_vars if bind_vars is not None else {"tenantId": "tenant-A-uuid"},
        manifest=_manifest(),
        sharding_profile=sharding if sharding is not None else _sharding(),
        collection_to_entity=None,
        session=session or _Session(),
    )


# ---------------------------------------------------------------------------
# validate_plan integration
# ---------------------------------------------------------------------------


class TestPlanCacheIntegration:
    def test_miss_then_hit_skips_second_explain(self):
        db = _FakeDb(_passing_plan())
        _validate(db)
        _validate(db)
        assert db.aql.explain_calls == 1
        stats = plan_cache_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["size"] == 1

    def test_hit_is_bind_value_independent(self):
        db = _FakeDb(_passing_plan())
        _validate(db, bind_vars={"tenantId": "tenant-A-uuid", "extra": 1})
        _validate(db, bind_vars={"tenantId": "tenant-A-uuid", "extra": 2})
        assert db.aql.explain_calls == 1

    def test_schema_change_invalidates(self):
        db = _FakeDb(_passing_plan())
        _validate(db)
        # A different sharding profile is a different schema fingerprint.
        other = _sharding()
        other["members"]["Employee"] = {"kind": "regular"}
        _validate(db, sharding=other)
        assert db.aql.explain_calls == 2

    def test_aql_change_invalidates(self):
        db = _FakeDb(_passing_plan())
        _validate(db, aql="FOR e IN Employee RETURN e")
        _validate(db, aql="FOR e IN Employee RETURN e.name")
        assert db.aql.explain_calls == 2

    def test_bind_gate_runs_on_hit_no_session_tenant(self):
        db = _FakeDb(_passing_plan())
        _validate(db)  # miss, certifies the shape
        with pytest.raises(TenantScopeViolation) as exc:
            _validate(db, session=_Session(tenant_id=None, tenant_key=None))
        assert exc.value.code == "NO_SESSION_TENANT"
        # The gate fired from the cached verdict — no second EXPLAIN.
        assert db.aql.explain_calls == 1

    def test_bind_mismatch_runs_on_hit(self):
        db = _FakeDb(_passing_plan())
        _validate(db)  # miss
        with pytest.raises(TenantScopeViolation) as exc:
            _validate(db, bind_vars={"tenantId": "tenant-B-uuid"})
        assert exc.value.code == "TENANT_BIND_MISMATCH"
        assert db.aql.explain_calls == 1

    def test_violation_is_not_cached(self):
        db = _FakeDb(_unconstrained_plan())
        for _ in range(2):
            with pytest.raises(TenantScopeViolation) as exc:
                _validate(db)
            assert exc.value.code == "UNCONSTRAINED_COLLECTION_SCAN"
        # A refusal must re-EXPLAIN every time — never cached.
        assert db.aql.explain_calls == 2
        assert plan_cache_stats()["size"] == 0

    def test_plan_override_bypasses_cache(self):
        # plan_override callers (unit tests) never populate or read the cache.
        validate_plan(
            db=None,
            aql="hand-crafted",
            bind_vars={"tenantId": "tenant-A-uuid"},
            manifest=_manifest(),
            sharding_profile=_sharding(),
            collection_to_entity=None,
            session=_Session(),
            plan_override=_passing_plan(),
        )
        assert plan_cache_stats()["size"] == 0

    def test_reset_clears_entries_and_counters(self):
        db = _FakeDb(_passing_plan())
        _validate(db)
        assert plan_cache_stats()["size"] == 1
        reset_plan_cache()
        stats = plan_cache_stats()
        assert stats["size"] == 0
        assert stats["hits"] == 0
        assert stats["misses"] == 0


# ---------------------------------------------------------------------------
# _PlanCertificationCache unit tests
# ---------------------------------------------------------------------------


class TestPlanCertificationCache:
    def _cert(self, digest: str = "d") -> _CachedCertification:
        return _CachedCertification(touches_tenant_data=True, plan_digest=digest)

    def test_maxsize_zero_disables(self):
        cache = _PlanCertificationCache(maxsize=0, ttl_seconds=0)
        assert cache.enabled is False
        cache.put("k", self._cert())
        assert cache.get("k") is None

    def test_lru_eviction(self):
        cache = _PlanCertificationCache(maxsize=2, ttl_seconds=0)
        cache.put("a", self._cert("a"))
        cache.put("b", self._cert("b"))
        cache.put("c", self._cert("c"))  # evicts "a" (least recently used)
        assert cache.get("a") is None
        assert cache.get("b") is not None
        assert cache.get("c") is not None

    def test_lru_get_refreshes_recency(self):
        cache = _PlanCertificationCache(maxsize=2, ttl_seconds=0)
        cache.put("a", self._cert("a"))
        cache.put("b", self._cert("b"))
        cache.get("a")  # "a" now most-recently used
        cache.put("c", self._cert("c"))  # evicts "b", not "a"
        assert cache.get("a") is not None
        assert cache.get("b") is None

    def test_ttl_expiry(self, monkeypatch: pytest.MonkeyPatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(tpv.time, "monotonic", lambda: clock["t"])
        cache = _PlanCertificationCache(maxsize=8, ttl_seconds=10.0)
        cache.put("k", self._cert())
        clock["t"] = 1005.0
        assert cache.get("k") is not None  # within TTL
        clock["t"] = 1011.0
        assert cache.get("k") is None  # expired

    def test_stats_hit_rate(self):
        cache = _PlanCertificationCache(maxsize=8, ttl_seconds=0)
        cache.put("k", self._cert())
        cache.get("k")  # hit
        cache.get("missing")  # miss
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5
