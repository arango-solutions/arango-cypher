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
from ..models import ConnectRequest, ConnectResponse
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

    _evict_lru()
    token = secrets.token_urlsafe(32)
    _sessions[token] = _Session(token=token, db=db, client=client)

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
    )
    return ConnectResponse(token=token, databases=databases)


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
