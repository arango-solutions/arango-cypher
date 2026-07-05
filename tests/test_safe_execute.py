"""Layer 6 / Wave 7 part 4 — ``safe_execute`` boundary helper regressions.

Pins the bind-var precedence contract from
``docs/multitenant_prd.md`` §9 and ``docs/agent_prompts_multitenant.md``
Wave 7 part 4:

* The session's ``tenant_id`` / ``tenant_key`` are spread over the
  caller's ``client_bind_vars`` **last**, so even a deliberate-conflict
  in the client payload silently loses to the session value.
* The validator is *always* called before execute.
* A non-session caller (``session is None``) is refused with
  ``PermissionError`` — there is no anonymous path through
  ``safe_execute``.
* Both the cursor and the final bind vars are returned for UI
  transparency (§9.2).

The service-side adapter in :mod:`arango_cypher.service.safe_exec`
is exercised separately via the existing
``tests/test_session_tenant_binding.py`` end-to-end paths plus a
local smoke for the ``NO_MAPPING_FOR_VALIDATION`` fail-closed
contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest

from arango_cypher.service.safe_exec import safe_execute_aql
from arango_cypher.tenant_plan_validator import TenantScopeViolation
from arango_query_core import safe_execute


@dataclass
class _FakeSession:
    token: str = "session-TOKEN"
    tenant_id: str | None = "tenant-A-uuid"
    tenant_key: str | None = "tenant-A-uuid"
    is_admin: bool = False


class _FakeAql:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def execute(self, aql: str, bind_vars: dict[str, Any], **kwargs: Any) -> str:
        self.calls.append((aql, dict(bind_vars), dict(kwargs)))
        return f"<cursor:{len(self.calls)}>"

    def explain(self, aql: str, bind_vars: dict[str, Any]) -> dict[str, Any]:
        return {"plan": {"nodes": []}, "warnings": []}


class _FakeDb:
    def __init__(self) -> None:
        self.aql = _FakeAql()


# ---------------------------------------------------------------------------
# arango_query_core.safe_execute
# ---------------------------------------------------------------------------


class TestSafeExecuteCore:
    """Direct contract for :func:`arango_query_core.exec.safe_execute`."""

    def test_session_tenant_silently_overrides_client_bindvar(self) -> None:
        """A deliberately-wrong client tenantId is overwritten by the
        session value — this is the load-bearing T7 defence.

        The AQL references both ``@tenantId`` and ``@tenantKey`` so the
        session-spread is active for both (a query that uses neither
        drops the binds entirely; see
        :meth:`test_unused_tenant_binds_are_not_injected`).
        """
        db = _FakeDb()
        called: dict[str, Any] = {}

        def _stub_validator(
            *,
            db,
            aql,
            bind_vars,
            manifest,
            sharding_profile,
            collection_to_entity,
            session,
            admin_bypass=False,
            bypass_reason="",
        ) -> None:
            called["bind_vars"] = dict(bind_vars)
            called["aql"] = aql

        aql = "FOR x IN C FILTER x.t == @tenantId AND x._key == @tenantKey RETURN x"
        cursor, final_bind = safe_execute(
            db=db,
            aql=aql,
            client_bind_vars={"tenantId": "tenant-B-rogue", "tenantKey": "rogue-key", "other": 42},
            session=_FakeSession(tenant_id="tenant-A-uuid", tenant_key="tenant-A-uuid"),
            validator=_stub_validator,
            manifest=object(),
            sharding_profile={},
        )

        assert final_bind == {
            "tenantId": "tenant-A-uuid",
            "tenantKey": "tenant-A-uuid",
            "other": 42,
        }
        # The validator saw the session-tenant bind, not the client one.
        assert called["bind_vars"]["tenantId"] == "tenant-A-uuid"
        assert called["bind_vars"]["tenantKey"] == "tenant-A-uuid"
        # Execute fired with the same bind vars after validation.
        assert db.aql.calls == [(aql, final_bind, {})]
        assert cursor == "<cursor:1>"

    def test_unused_tenant_binds_are_not_injected(self) -> None:
        """Regression (ERR 1552): a query that never references
        ``@tenantId`` / ``@tenantKey`` must NOT carry those binds.

        ArangoDB rejects a query whose ``bind_vars`` declares a value
        the text never uses, so the unconditional session-spread broke
        every safe global / satellite-only read at EXPLAIN time. The
        client's stray ``tenantId`` is inert here and is dropped too.
        """
        db = _FakeDb()
        seen: dict[str, Any] = {}

        def _capture(*, bind_vars, **_kwargs) -> None:
            seen["bind_vars"] = dict(bind_vars)

        cursor, final_bind = safe_execute(
            db=db,
            aql="FOR c IN Country RETURN c",
            client_bind_vars={"tenantId": "client-supplied", "limit": 5},
            session=_FakeSession(tenant_id="tenant-A-uuid", tenant_key="tenant-A-uuid"),
            validator=_capture,
            manifest=object(),
            sharding_profile={},
        )

        assert "tenantId" not in final_bind
        assert "tenantKey" not in final_bind
        assert final_bind == {"limit": 5}
        # The validator's EXPLAIN would have received the same lean
        # bind set — no unused tenant binds to trip ERR 1552.
        assert "tenantId" not in seen["bind_vars"]
        assert "tenantKey" not in seen["bind_vars"]
        assert cursor == "<cursor:1>"

    def test_only_referenced_tenant_bind_is_injected(self) -> None:
        """When the AQL references ``@tenantId`` but not ``@tenantKey``,
        only ``tenantId`` is spread — ``tenantKey`` would be an unused
        bind and is omitted."""
        db = _FakeDb()
        _, final_bind = safe_execute(
            db=db,
            aql="FOR e IN Employee FILTER e.tenantId == @tenantId RETURN e",
            client_bind_vars={},
            session=_FakeSession(tenant_id="tenant-A-uuid", tenant_key="tenant-A-uuid"),
            validator=lambda **_: None,
            manifest=object(),
            sharding_profile={},
        )
        assert final_bind == {"tenantId": "tenant-A-uuid"}

    def test_validator_refusal_blocks_execute(self) -> None:
        """When the validator raises, execute must not run."""
        db = _FakeDb()

        def _refusing(**_kwargs) -> None:
            raise TenantScopeViolation(code="DENY", message="for test")

        with pytest.raises(TenantScopeViolation) as exc_info:
            safe_execute(
                db=db,
                aql="FOR x IN Whatever RETURN x",
                client_bind_vars={},
                session=_FakeSession(),
                validator=_refusing,
                manifest=object(),
                sharding_profile={},
            )

        assert exc_info.value.code == "DENY"
        assert db.aql.calls == []

    def test_session_none_refused(self) -> None:
        """No session → ``PermissionError``; never silently runs."""
        with pytest.raises(PermissionError):
            safe_execute(
                db=_FakeDb(),
                aql="RETURN 1",
                client_bind_vars={},
                session=None,
                validator=lambda **_: None,
                manifest=object(),
                sharding_profile={},
            )

    def test_execute_kwargs_passed_through(self) -> None:
        db = _FakeDb()
        safe_execute(
            db=db,
            aql="RETURN 1",
            client_bind_vars={},
            session=_FakeSession(),
            validator=lambda **_: None,
            manifest=object(),
            sharding_profile={},
            execute_kwargs={"profile": True, "batch_size": 100},
        )
        assert db.aql.calls[0][2] == {"profile": True, "batch_size": 100}

    def test_validator_sees_collection_to_entity(self) -> None:
        seen: dict[str, Any] = {}

        def _capture(**kwargs) -> None:
            seen.update(kwargs)

        coll_to_entity = {"EmployeePhysical": "Employee"}
        safe_execute(
            db=_FakeDb(),
            aql="RETURN 1",
            client_bind_vars={},
            session=_FakeSession(),
            validator=_capture,
            manifest=object(),
            sharding_profile={},
            collection_to_entity=coll_to_entity,
        )
        assert seen["collection_to_entity"] is coll_to_entity


# ---------------------------------------------------------------------------
# arango_cypher.service.safe_exec.safe_execute_aql
# ---------------------------------------------------------------------------


_MINIMAL_MAPPING = {
    "conceptual_schema": {
        "entities": [{"name": "Country", "properties": ["NAME"]}],
        "relationships": [],
    },
    "physical_mapping": {
        "entities": {"Country": {"style": "COLLECTION", "collectionName": "Country"}},
    },
    "metadata": {
        "shardingProfile": {
            "style": "DisjointSmartGraph",
            "members": {"Country": {"kind": "satellite"}},
            "graphs": [],
        }
    },
}


class TestSafeExecuteServiceAdapter:
    """``arango_cypher.service.safe_exec.safe_execute_aql`` integration."""

    def test_unbound_session_without_mapping_falls_through_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pre-Wave-7 single-tenant flow: session has no tenant, no
        mapping supplied → direct execute, WARN logged.
        """
        db = _FakeDb()
        session = _FakeSession(tenant_id=None, tenant_key=None)
        with caplog.at_level(logging.WARNING, logger="arango_cypher.service.safe_exec"):
            cursor, bind_vars = safe_execute_aql(
                db=db,
                aql="RETURN 1",
                bind_vars={},
                session=session,
                mapping_dict=None,
            )
        assert cursor == "<cursor:1>"
        assert bind_vars == {}
        warn_records = [r for r in caplog.records if "bypassing Layer 5" in r.getMessage()]
        assert warn_records, f"expected bypass WARN; got {caplog.records!r}"

    def test_tenant_bound_session_without_mapping_refused(self) -> None:
        """Wave 7 fail-closed contract: tenant-bound session has no
        mapping to validate against → refuse, never execute.
        """
        db = _FakeDb()
        session = _FakeSession()
        with pytest.raises(TenantScopeViolation) as exc_info:
            safe_execute_aql(
                db=db,
                aql="FOR e IN Employee RETURN e",
                bind_vars={},
                session=session,
                mapping_dict=None,
            )
        assert exc_info.value.code == "NO_MAPPING_FOR_VALIDATION"
        assert db.aql.calls == []

    def test_satellite_only_query_with_mapping_executes(self) -> None:
        """End-to-end: mapping with one satellite collection → Layer 5
        accepts, execute fires.

        The query never references ``@tenantId`` / ``@tenantKey``, so —
        per the ERR 1552 fix — those binds are NOT injected. A real
        ArangoDB would reject the query otherwise; the fake DB wouldn't,
        which is exactly why this regression hid until the live smoke
        test.
        """
        db = _FakeDb()
        session = _FakeSession()
        cursor, bind_vars = safe_execute_aql(
            db=db,
            aql="FOR c IN Country RETURN c",
            bind_vars={"limit": 10},
            session=session,
            mapping_dict=_MINIMAL_MAPPING,
        )
        assert cursor == "<cursor:1>"
        assert bind_vars["limit"] == 10
        assert "tenantId" not in bind_vars
        assert "tenantKey" not in bind_vars
        assert db.aql.calls[0][0] == "FOR c IN Country RETURN c"
        # The bind vars passed to ArangoDB carry no unused tenant binds.
        assert "tenantId" not in db.aql.calls[0][1]
        assert "tenantKey" not in db.aql.calls[0][1]

    def test_tenant_scoped_collection_without_filter_refused(self) -> None:
        """Mapping declares Employee as TENANT_SCOPED smartgraph; the
        EXPLAIN plan returned by the fake DB has no tenant predicate
        → Layer 5 refuses.
        """

        class _RogueAql(_FakeAql):
            def explain(self, aql: str, bind_vars: dict[str, Any]) -> dict[str, Any]:
                return {
                    "plan": {
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
                    },
                    "warnings": [],
                }

        class _RogueDb:
            def __init__(self) -> None:
                self.aql = _RogueAql()

        mapping = {
            "conceptual_schema": {
                "entities": [
                    {"name": "Tenant"},
                    {"name": "Employee", "properties": ["TENANT_HEX_ID", "NAME"]},
                ],
                "relationships": [],
            },
            "physical_mapping": {
                "entities": {
                    "Tenant": {"collectionName": "Tenant"},
                    "Employee": {"collectionName": "Employee"},
                }
            },
            "metadata": {
                "shardingProfile": {
                    "style": "DisjointSmartGraph",
                    "members": {
                        "Tenant": {"kind": "tenant-root"},
                        "Employee": {"kind": "smartgraph"},
                    },
                    "graphs": [],
                }
            },
        }

        db = _RogueDb()
        with pytest.raises(TenantScopeViolation) as exc_info:
            safe_execute_aql(
                db=db,
                aql="FOR e IN Employee RETURN e",
                bind_vars={},
                session=_FakeSession(),
                mapping_dict=mapping,
            )
        assert exc_info.value.code == "UNCONSTRAINED_COLLECTION_SCAN"
        assert db.aql.calls == []
