"""Tests for the post-analysis tenant picker endpoints:

* ``POST /tenants/discover`` — enumerate selectable tenants from the
  analysed mapping, supporting both a dedicated ``Tenant`` collection
  and the denormalised-field shape (the common real-world case where
  tenancy lives only as e.g. ``Alert.tenantId``).
* ``POST /session/tenant`` — re-bind (or clear) the active session's
  tenant after analysis, without re-authenticating.

These are the server side of the Connect-dialog rework: the tenant
binding can't be chosen at connect time (we don't yet know the schema
is multi-tenant), so it's a post-analysis action on the live session.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from arango_cypher.service import _get_session, _Session, app

client = TestClient(app)


# A denormalised-tenant mapping: one TENANT_SCOPED entity (Alert) whose
# tenancy is carried by the `tenantId` field, with NO `Tenant`
# collection. Mirrors the live schema the UI is demoed against.
DENORM_MAPPING: dict[str, Any] = {
    "conceptual_schema": {
        "entities": [
            {
                "name": "Alert",
                "labels": ["Alert"],
                "properties": [{"name": "tenantId"}, {"name": "severity"}],
            }
        ],
        "relationships": [],
    },
    "physical_mapping": {
        "entities": {
            "Alert": {
                "style": "COLLECTION",
                "collectionName": "AlertColl",
                "tenantScope": {"role": "tenant_scoped", "tenantField": "tenantId"},
                "properties": {"tenantId": {"type": "string"}},
            }
        },
        "relationships": {},
    },
    "metadata": {},
}

# A Tenant-root mapping: a conceptual `Tenant` entity backed by a
# physical `Tenants` collection.
TENANT_COLLECTION_MAPPING: dict[str, Any] = {
    "conceptual_schema": {
        "entities": [
            {"name": "Tenant", "labels": ["Tenant"], "properties": [{"name": "NAME"}]},
        ],
        "relationships": [],
    },
    "physical_mapping": {
        "entities": {
            "Tenant": {"style": "COLLECTION", "collectionName": "Tenants", "properties": {}},
        },
        "relationships": {},
    },
    "metadata": {},
}


def _install_session(
    *,
    collections: set[str] | None = None,
    distinct_rows: list[dict[str, Any]] | None = None,
    tenant_docs: list[dict[str, Any]] | None = None,
    tenant_get: Any = "__unset__",
    tenant_id: str | None = None,
) -> MagicMock:
    """Install a fake-DB session.

    * ``collections`` — names for which ``has_collection`` returns True.
    * ``distinct_rows`` — rows returned by the denorm COLLECT query.
    * ``tenant_docs`` — rows returned by the Tenant-collection catalog query.
    * ``tenant_get`` — return value of ``collection('Tenant').get(key)``.
    """
    existing = collections or set()
    db = MagicMock()
    db.name = "testdb"
    db.has_collection.side_effect = lambda name: name in existing

    def _execute(aql: str, bind_vars: dict | None = None):  # noqa: ARG001
        # The denorm discovery query binds the field name; the Tenant
        # catalog query does not.
        if bind_vars and "field" in bind_vars:
            return iter(distinct_rows or [])
        return iter(tenant_docs or [])

    db.aql.execute.side_effect = _execute

    if tenant_get != "__unset__":
        coll = MagicMock()
        coll.get.return_value = tenant_get
        db.collection.return_value = coll

    session = _Session.__new__(_Session)
    session.token = "test-token"
    session.db = db
    session.client = MagicMock()
    session.created_at = 0.0
    session.last_used = 0.0
    session.tenant_id = tenant_id
    session.tenant_key = tenant_id
    session.is_admin = False
    app.dependency_overrides[_get_session] = lambda: session
    return session


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.pop(_get_session, None)


class TestTenantsDiscover:
    def test_requires_session(self):
        resp = client.post("/tenants/discover", json={"mapping": DENORM_MAPPING})
        assert resp.status_code == 401

    def test_no_mapping_is_single_tenant(self):
        _install_session()
        resp = client.post("/tenants/discover", json={"mapping": None})
        assert resp.status_code == 200
        body = resp.json()
        assert body["multiTenant"] is False
        assert body["scope"] == "none"
        assert body["tenants"] == []

    def test_denorm_discovery_lists_distinct_tenants(self):
        _install_session(
            collections={"AlertColl"},
            distinct_rows=[
                {"value": "tenant-a", "docs": 245},
                {"value": "tenant-b", "docs": 12},
                # Nulls / non-strings must be ignored, never crash.
                {"value": None, "docs": 9},
                {"value": 123, "docs": 3},
            ],
        )
        resp = client.post("/tenants/discover", json={"mapping": DENORM_MAPPING})
        assert resp.status_code == 200
        body = resp.json()
        assert body["multiTenant"] is True
        assert body["scope"] == "denorm"
        assert body["tenantField"] == "tenantId"
        assert body["collections"] == ["AlertColl"]
        # Two valid string tenants, sorted by doc count descending.
        assert [t["key"] for t in body["tenants"]] == ["tenant-a", "tenant-b"]
        assert body["tenants"][0]["docs"] == 245
        # key/id/name all carry the bare tenant id for the denorm path.
        assert body["tenants"][0]["id"] == "tenant-a"
        assert body["tenants"][0]["name"] == "tenant-a"

    def test_denorm_scoped_but_empty_collection_is_still_multitenant(self):
        # Schema *is* tenant-scoped, but the probed collection has no
        # rows yet — must report multiTenant so the picker still shows.
        _install_session(collections={"AlertColl"}, distinct_rows=[])
        resp = client.post("/tenants/discover", json={"mapping": DENORM_MAPPING})
        body = resp.json()
        assert body["multiTenant"] is True
        assert body["scope"] == "denorm"
        assert body["tenants"] == []

    def test_tenant_collection_path(self):
        _install_session(
            collections={"Tenants"},
            tenant_docs=[
                {"id": "Tenants/t1", "key": "t1", "name": "Acme", "subdomain": "acme", "hex_id": "a1"},
            ],
        )
        resp = client.post("/tenants/discover", json={"mapping": TENANT_COLLECTION_MAPPING})
        assert resp.status_code == 200
        body = resp.json()
        assert body["multiTenant"] is True
        assert body["scope"] == "collection"
        assert body["collections"] == ["Tenants"]
        assert body["tenants"][0]["name"] == "Acme"
        assert body["tenants"][0]["id"] == "Tenants/t1"


class TestSessionTenantBind:
    def test_requires_session(self):
        resp = client.post("/session/tenant", json={"tenantId": "t1"})
        assert resp.status_code == 401

    def test_bind_sets_tenant_on_session(self):
        session = _install_session()
        resp = client.post("/session/tenant", json={"tenantId": "tenant-a"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["bound"] is True
        assert body["tenant_id"] == "tenant-a"
        assert body["tenant_key"] == "tenant-a"
        # The live session object must actually carry the binding —
        # that's what Layers 4–6 read to scope execution.
        assert session.tenant_id == "tenant-a"
        assert session.tenant_key == "tenant-a"

    def test_explicit_tenant_key_is_honoured(self):
        session = _install_session()
        resp = client.post("/session/tenant", json={"tenantId": "t1", "tenantKey": "k1"})
        assert resp.status_code == 200
        assert session.tenant_id == "t1"
        assert session.tenant_key == "k1"

    def test_clearing_tenant_unbinds_session(self):
        session = _install_session(tenant_id="tenant-a")
        resp = client.post("/session/tenant", json={"tenantId": None})
        assert resp.status_code == 200
        body = resp.json()
        assert body["bound"] is False
        assert body["tenant_id"] is None
        assert session.tenant_id is None

    def test_unknown_tenant_with_tenant_collection_is_403(self):
        # When a `Tenant` collection exists, the supplied key must
        # resolve to a document — otherwise the rebind is refused.
        session = _install_session(collections={"Tenant"}, tenant_get=None)
        resp = client.post("/session/tenant", json={"tenantId": "ghost"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "unknown_tenant"
        # Session binding must be left untouched on refusal.
        assert session.tenant_id is None

    def test_known_tenant_with_tenant_collection_binds(self):
        session = _install_session(collections={"Tenant"}, tenant_get={"_key": "t1", "NAME": "Acme"})
        resp = client.post("/session/tenant", json={"tenantId": "t1"})
        assert resp.status_code == 200
        assert session.tenant_id == "t1"
