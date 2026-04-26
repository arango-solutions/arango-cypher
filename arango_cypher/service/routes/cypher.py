"""Cypher / AQL execution endpoints — ``/translate``, ``/execute``,
``/execute-aql``, ``/validate``, ``/explain``, ``/aql-profile``.
"""

from __future__ import annotations

import logging as _log
import time

from fastapi import Depends, HTTPException

from arango_query_core import CoreError

from ... import corrections as _corrections
from ...api import translate, validate_cypher_profile
from ..app import app
from ..mapping import _mapping_from_dict
from ..models import (
    ExecuteAqlRequest,
    ExecuteRequest,
    ExecuteResponse,
    TranslateRequest,
    TranslateResponse,
    ValidateRequest,
    ValidateResponse,
)
from ..registry import _default_registry
from ..security import (
    _check_compute_rate_limit,
    _get_session,
    _sanitize_error,
    _Session,
    _translate_errors,
)


@app.post("/translate", response_model=TranslateResponse)
def translate_endpoint(
    req: TranslateRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Translate Cypher to AQL."""
    _log.getLogger("arango_cypher.service").info(
        "translate request: cypher=%r, mapping_keys=%s",
        req.cypher[:80] if req.cypher else "(empty)",
        list(req.mapping.keys()) if req.mapping else "(none)",
    )
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")

    registry = _default_registry if req.extensions_enabled else None
    t0 = time.perf_counter()
    try:
        result = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
    except CoreError as e:
        _log.getLogger("arango_cypher.service").warning(
            "translate CoreError: %s (code=%s) for cypher=%r",
            e,
            e.code,
            req.cypher[:80],
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(e)), "code": e.code},
        ) from e
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    correction = _corrections.lookup(req.cypher, req.mapping)
    if correction:
        return TranslateResponse(
            aql=correction.corrected_aql,
            bind_vars=correction.bind_vars or result.bind_vars,
            warnings=[{"message": f"Using learned correction #{correction.id}"}]
            + list(result.warnings or []),
            elapsed_ms=elapsed_ms,
        )

    return TranslateResponse(
        aql=result.aql,
        bind_vars=result.bind_vars,
        warnings=result.warnings,
        elapsed_ms=elapsed_ms,
    )


@app.post("/execute", response_model=ExecuteResponse)
def execute_endpoint(
    req: ExecuteRequest,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
):
    """Translate Cypher to AQL and execute against the connected ArangoDB."""
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")

    registry = _default_registry if req.extensions_enabled else None
    try:
        t_translate = time.perf_counter()
        transpiled = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
        translate_ms = round((time.perf_counter() - t_translate) * 1000, 1)
    except CoreError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(e)), "code": e.code},
        ) from e

    correction = _corrections.lookup(req.cypher, req.mapping)
    run_aql = correction.corrected_aql if correction else transpiled.aql
    run_bind = (correction.bind_vars or transpiled.bind_vars) if correction else transpiled.bind_vars
    warnings = list(transpiled.warnings or [])
    if correction:
        warnings.insert(0, {"message": f"Using learned correction #{correction.id}"})

    with _translate_errors("AQL execution failed"):
        t_exec = time.perf_counter()
        cursor = session.db.aql.execute(run_aql, bind_vars=run_bind)
        results = list(cursor)
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)

    return ExecuteResponse(
        results=results,
        aql=run_aql,
        bind_vars=run_bind,
        warnings=warnings,
        exec_ms=exec_ms,
        translate_ms=translate_ms,
    )


@app.post("/execute-aql")
def execute_aql_endpoint(
    req: ExecuteAqlRequest,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
):
    """Execute a raw AQL query directly (used by NL→AQL direct path)."""
    with _translate_errors("AQL execution failed"):
        t_exec = time.perf_counter()
        cursor = session.db.aql.execute(req.aql, bind_vars=req.bind_vars)
        results = list(cursor)
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)

    return ExecuteResponse(
        results=results,
        aql=req.aql,
        bind_vars=req.bind_vars,
        warnings=[],
        exec_ms=exec_ms,
    )


@app.post("/validate", response_model=ValidateResponse)
def validate_endpoint(
    req: ValidateRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Validate Cypher against the translator profile."""
    mapping = _mapping_from_dict(req.mapping)
    result = validate_cypher_profile(
        req.cypher,
        mapping=mapping,
        params=req.params,
    )
    return ValidateResponse(
        ok=result.ok,
        errors=[dict(e) for e in result.errors],
    )


@app.post("/explain")
def explain_endpoint(
    req: TranslateRequest,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
):
    """Translate Cypher to AQL, then run AQL EXPLAIN to get the execution plan."""
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")

    registry = _default_registry if req.extensions_enabled else None
    try:
        t_translate = time.perf_counter()
        transpiled = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
        translate_ms = round((time.perf_counter() - t_translate) * 1000, 1)
    except CoreError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(e)), "code": e.code},
        ) from e

    with _translate_errors("AQL EXPLAIN failed"):
        plan = session.db.aql.explain(transpiled.aql, bind_vars=transpiled.bind_vars)

    return {
        "aql": transpiled.aql,
        "bind_vars": transpiled.bind_vars,
        "plan": plan,
        "translate_ms": translate_ms,
    }


@app.post("/aql-profile")
def aql_profile_endpoint(
    req: TranslateRequest,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
):
    """Translate Cypher to AQL, execute with profiling, return runtime stats + results."""
    mapping = _mapping_from_dict(req.mapping)
    if mapping is None:
        raise HTTPException(status_code=400, detail="mapping is required")

    registry = _default_registry if req.extensions_enabled else None
    try:
        t_translate = time.perf_counter()
        transpiled = translate(
            req.cypher,
            mapping=mapping,
            registry=registry,
            params=req.params,
        )
        translate_ms = round((time.perf_counter() - t_translate) * 1000, 1)
    except CoreError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(e)), "code": e.code},
        ) from e

    with _translate_errors("AQL profiled execution failed"):
        cursor = session.db.aql.execute(
            transpiled.aql,
            bind_vars=transpiled.bind_vars,
            profile=True,
        )
        results = list(cursor)
        stats = cursor.statistics()
        profile_data = cursor.profile() if hasattr(cursor, "profile") else None

    return {
        "aql": transpiled.aql,
        "bind_vars": transpiled.bind_vars,
        "results": results,
        "statistics": stats,
        "profile": profile_data,
        "translate_ms": translate_ms,
    }
