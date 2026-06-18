"""Connection management endpoints — ``/connect``, ``/disconnect``,
``/connections``, ``/connect/defaults``, ``/cypher-profile``.
"""

from __future__ import annotations

import os
import secrets
import time

from fastapi import Depends, HTTPException

from ..._env import read_arango_password
from ...api import get_cypher_profile
from ..app import _PUBLIC_MODE, _svc_logger, app
from ..models import BindGraphRequest, BindTenantRequest, ConnectRequest, ConnectResponse
from ..observability import log_endpoint_timing
from ..security import (
    _check_connect_target,
    _describe_connect_error,
    _evict_lru,
    _get_session,
    _prune_expired,
    _require_session_in_public_mode,
    _Session,
    _sessions,
)


@app.post("/connect", response_model=ConnectResponse)
def connect(req: ConnectRequest):
    """Authenticate to ArangoDB; returns a session token."""
    # ``ArangoClient`` is read off the package init at call time so the
    # ``monkeypatch.setattr("arango_cypher.service.ArangoClient", _FakeClient)``
    # pattern in tests/test_service_hardening.py keeps flowing through to
    # this endpoint after the audit-v2 #8 split. A direct
    # ``from arango import ArangoClient`` here would capture a snapshot
    # at module-import time and bypass the monkeypatch.
    from arango_cypher import service as _svc

    t0 = time.perf_counter()
    _check_connect_target(req.url)
    try:
        url = req.url.rstrip("/")
        client = _svc.ArangoClient(hosts=url)
        db = client.db(req.database, username=req.username, password=req.password)
        db.version()
    except Exception as e:
        detail = _describe_connect_error(e)
        _svc_logger.warning(
            "connect failed for db=%r user=%r: %s",
            req.database,
            req.username,
            detail,
        )
        log_endpoint_timing(
            "/connect",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            database=req.database,
            error_type=type(e).__name__,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Connection failed: {detail}",
        ) from e

    # ------------------------------------------------------------------
    # Tenant binding (PRD docs/multitenant_prd.md §4 / Wave 7 part 1).
    # ------------------------------------------------------------------
    # When a tenantId is supplied, validate that a matching document
    # exists in the database's Tenant collection (or its physical-mapping
    # alias). If the collection doesn't exist (single-tenant /
    # workbench-style deployments) we still accept the request — the
    # session simply binds the caller-supplied id verbatim and Layer 5
    # falls back to "session has no tenant_id" semantics for any query
    # that touches a TENANT_SCOPED collection. The acceptance rule is
    # intentionally permissive here so that legacy single-tenant
    # deployments keep working; the hard refusal lives at Layer 5 for
    # tenant-scoped reads.
    tenant_id = req.tenantId
    tenant_key = req.tenantKey if req.tenantKey is not None else tenant_id
    if tenant_id is not None and tenant_key is not None:
        try:
            has_tenant_collection = db.has_collection("Tenant")
        except Exception:
            has_tenant_collection = False
        if has_tenant_collection:
            try:
                tenant_doc = db.collection("Tenant").get(tenant_key)
            except Exception as exc:
                _svc_logger.warning(
                    "tenant lookup failed for tenantKey=%r: %s",
                    tenant_key,
                    exc,
                )
                tenant_doc = None
            if tenant_doc is None:
                client.close()
                log_endpoint_timing(
                    "/connect",
                    round((time.perf_counter() - t0) * 1000, 1),
                    status="error",
                    database=req.database,
                    error_type="unknown_tenant",
                )
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "unknown_tenant",
                        "tenantId": tenant_id,
                        "tenantKey": tenant_key,
                    },
                )

    _evict_lru()
    token = secrets.token_urlsafe(32)
    _sessions[token] = _Session(
        token=token,
        db=db,
        client=client,
        tenant_id=tenant_id,
        tenant_key=tenant_key,
        is_admin=bool(req.isAdmin),
    )

    try:
        databases = [
            d for d in client.db("_system", username=req.username, password=req.password).databases()
        ]
    except Exception:
        databases = [req.database]

    log_endpoint_timing(
        "/connect",
        round((time.perf_counter() - t0) * 1000, 1),
        database=req.database,
        databases_visible=len(databases),
        tenant_id=tenant_id,
        is_admin=bool(req.isAdmin),
    )
    return ConnectResponse(
        token=token,
        databases=databases,
        tenant_id=tenant_id,
        tenant_key=tenant_key,
        is_admin=bool(req.isAdmin),
    )


@app.post("/session/tenant")
def bind_session_tenant(
    req: BindTenantRequest,
    session: _Session = Depends(_get_session),
):
    """Re-bind (or clear) the active session's tenant after schema
    analysis, without re-authenticating.

    The tenant binding cannot be chosen at ``/connect`` time because the
    caller does not yet know whether the schema is multi-tenant or what
    the tenant ids are — that's only known after introspection +
    :func:`analyze_tenant_scope`. This endpoint lets the UI's
    post-analysis tenant picker set the binding on the existing session;
    Layers 4–6 then scope every subsequent query to it.

    ``tenantId`` of ``None`` clears the binding (reference-only / "all
    tenants" mode). ``tenantKey`` defaults to ``tenantId``. Acceptance
    mirrors ``/connect``: when a ``Tenant`` collection exists the key is
    validated against it; otherwise the id is bound verbatim (denormalised
    schemas) and Layer 5 enforces scoping on tenant-touching reads.
    """
    t0 = time.perf_counter()
    tenant_id = req.tenantId or None
    tenant_key = (req.tenantKey if req.tenantKey is not None else tenant_id) or None

    if tenant_id is not None and tenant_key is not None:
        try:
            has_tenant_collection = session.db.has_collection("Tenant")
        except Exception:
            has_tenant_collection = False
        if has_tenant_collection:
            try:
                tenant_doc = session.db.collection("Tenant").get(tenant_key)
            except Exception as exc:
                _svc_logger.warning("tenant rebind lookup failed for key=%r: %s", tenant_key, exc)
                tenant_doc = None
            if tenant_doc is None:
                log_endpoint_timing(
                    "/session/tenant",
                    round((time.perf_counter() - t0) * 1000, 1),
                    status="error",
                    error_type="unknown_tenant",
                )
                raise HTTPException(
                    status_code=403,
                    detail={"error": "unknown_tenant", "tenantId": tenant_id, "tenantKey": tenant_key},
                )

    session.tenant_id = tenant_id
    session.tenant_key = tenant_key
    log_endpoint_timing(
        "/session/tenant",
        round((time.perf_counter() - t0) * 1000, 1),
        tenant_id=tenant_id,
        bound=tenant_id is not None,
    )
    return {"tenant_id": tenant_id, "tenant_key": tenant_key, "bound": tenant_id is not None}


@app.get("/graphs")
def list_graphs(session: _Session = Depends(_get_session)):
    """List the connected database's named graphs and their collections.

    Used by the UI's named-graph scope selector (PRD §17). Each entry carries
    the graph's edge definitions plus the flattened vertex / orphan collection
    lists and a ``collectionCount`` so the picker can show "scope to N
    collections" without a second round-trip. Returns ``{"graphs": []}`` cleanly
    for databases with no named graphs so the UI can be mechanical about
    show/hide.
    """
    t0 = time.perf_counter()
    graphs: list[dict] = []
    try:
        raw = session.db.graphs()
    except Exception as exc:
        _svc_logger.warning("listing named graphs failed: %s", exc)
        raw = []

    for g in raw:
        edge_defs: list[dict] = []
        vertex: set[str] = set()
        edges: set[str] = set()
        for ed in g.get("edge_definitions") or g.get("edgeDefinitions") or []:
            edge_col = ed.get("edge_collection") or ed.get("edgeCollection")
            frm = ed.get("from_vertex_collections") or ed.get("fromVertexCollections") or []
            to = ed.get("to_vertex_collections") or ed.get("toVertexCollections") or []
            if edge_col:
                edges.add(edge_col)
            vertex.update(frm)
            vertex.update(to)
            edge_defs.append({"edgeCollection": edge_col, "from": list(frm), "to": list(to)})
        orphans = list(g.get("orphan_collections") or g.get("orphanCollections") or [])
        vertex.update(orphans)
        graphs.append(
            {
                "name": g.get("name"),
                "edgeDefinitions": edge_defs,
                "vertexCollections": sorted(vertex),
                "orphanCollections": sorted(orphans),
                "collectionCount": len(vertex | edges),
            }
        )

    graphs.sort(key=lambda gg: gg.get("name") or "")
    log_endpoint_timing(
        "/graphs",
        round((time.perf_counter() - t0) * 1000, 1),
        graphs=len(graphs),
    )
    return {"graphs": graphs}


@app.post("/session/graph")
def bind_session_graph(
    req: BindGraphRequest,
    session: _Session = Depends(_get_session),
):
    """Bind (or clear) the active session's named-graph scope (PRD §17).

    ``graphName`` of ``None`` clears the binding ("all collections" mode);
    otherwise it must name an existing graph in the connected database
    (validated here — HTTP 404 on a miss). Once bound, every mapping-consuming
    endpoint restricts schema introspection to that graph's collections.
    """
    t0 = time.perf_counter()
    graph_name = req.graphName or None

    if graph_name is not None:
        try:
            exists = session.db.has_graph(graph_name)
        except Exception as exc:
            _svc_logger.warning("graph existence probe failed for %r: %s", graph_name, exc)
            exists = False
        if not exists:
            log_endpoint_timing(
                "/session/graph",
                round((time.perf_counter() - t0) * 1000, 1),
                status="error",
                error_type="unknown_graph",
            )
            raise HTTPException(
                status_code=404,
                detail={"error": "unknown_graph", "graphName": graph_name},
            )

    session.graph_name = graph_name
    log_endpoint_timing(
        "/session/graph",
        round((time.perf_counter() - t0) * 1000, 1),
        graph_name=graph_name or "",
        bound=graph_name is not None,
    )
    return {"graph_name": graph_name, "bound": graph_name is not None}


@app.post("/disconnect")
def disconnect(session: _Session = Depends(_get_session)):
    """Tear down session and release the python-arango client."""
    t0 = time.perf_counter()
    _sessions.pop(session.token, None)
    session.client.close()
    log_endpoint_timing(
        "/disconnect",
        round((time.perf_counter() - t0) * 1000, 1),
    )
    return {"status": "disconnected"}


@app.get("/connections")
def list_connections(_auth: _Session | None = Depends(_require_session_in_public_mode)):
    """List active sessions (admin/debug). Requires auth in public mode."""
    t0 = time.perf_counter()
    _prune_expired()
    payload = {
        "active": len(_sessions),
        "sessions": [
            {
                "token_prefix": s.token[:8] + "...",
                "created_at": s.created_at,
                "last_used": s.last_used,
                "expired": s.expired,
            }
            for s in _sessions.values()
        ],
    }
    log_endpoint_timing(
        "/connections",
        round((time.perf_counter() - t0) * 1000, 1),
        active=payload["active"],
    )
    return payload


@app.get("/connect/defaults")
def connect_defaults():
    """Return .env default values for pre-filling the connection dialog.

    Uses ARANGO_URL directly if set, otherwise builds from
    ARANGO_HOST/ARANGO_PORT/ARANGO_PROTOCOL.

    Disabled entirely when ``ARANGO_CYPHER_PUBLIC_MODE=true``. The
    password is omitted from the response by default — the field is
    still present (the UI's connect dialog binds against it) but the
    value is the empty string so a curious anonymous caller can't
    pull the credential out of the .env on a single-user dev box.
    Operators who want the legacy "auto-fill the password" convenience
    on a trusted laptop can set ``ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD``
    to ``1``. The password value itself is read via
    :func:`arango_cypher._env.read_arango_password`, which prefers
    ``ARANGO_PASSWORD`` (canonical) over ``ARANGO_PASS`` (deprecated
    fallback).
    """
    if _PUBLIC_MODE:
        raise HTTPException(status_code=404, detail="Not available in public mode")

    t0 = time.perf_counter()
    arango_url = os.getenv("ARANGO_URL", "")
    if not arango_url:
        host = os.getenv("ARANGO_HOST", "localhost")
        port = os.getenv("ARANGO_PORT", "8529")
        protocol = os.getenv("ARANGO_PROTOCOL", "http")
        arango_url = f"{protocol}://{host}:{port}"

    expose_pw = os.getenv("ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD", "").lower() in (
        "1",
        "true",
        "yes",
    )
    payload = {
        "url": arango_url.rstrip("/"),
        "database": os.getenv("ARANGO_DB", "_system"),
        "username": os.getenv("ARANGO_USER", "root"),
        "password": (read_arango_password(caller="arango_cypher.service") if expose_pw else ""),
    }
    log_endpoint_timing(
        "/connect/defaults",
        round((time.perf_counter() - t0) * 1000, 1),
        expose_pw=expose_pw,
    )
    return payload


@app.get("/cypher-profile")
def cypher_profile():
    """Return the Arango Cypher profile manifest."""
    t0 = time.perf_counter()
    profile = get_cypher_profile()
    log_endpoint_timing(
        "/cypher-profile",
        round((time.perf_counter() - t0) * 1000, 1),
    )
    return profile
