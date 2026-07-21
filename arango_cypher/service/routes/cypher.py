"""Cypher / AQL execution endpoints — ``/translate``, ``/execute``,
``/execute-aql``, ``/validate``, ``/explain``, ``/aql-profile``.
"""

from __future__ import annotations

import logging as _log
import time

from arango_query_core import CoreError
from fastapi import Depends, HTTPException

from ... import corrections as _corrections
from ...api import translate, validate_cypher_profile
from ...tenant_ast_aql import AqlRewriteError
from ...tenant_plan_validator import TenantScopeViolation
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
from ..observability import log_endpoint_timing
from ..registry import _default_registry
from ..safe_exec import apply_layer4_rewrite, safe_execute_aql
from ..security import (
    _check_compute_rate_limit,
    _get_session,
    _optional_session,
    _sanitize_error,
    _Session,
    _translate_errors,
)


def _tenant_violation_response(v: TenantScopeViolation) -> HTTPException:
    """Build the canonical 403 response for a Layer 5/6 tenant refusal.

    Must be raised **inside** any ``_translate_errors`` block so the
    ``HTTPException`` is re-raised untouched (``_translate_errors`` only
    wraps non-``HTTPException`` errors). A bare ``TenantScopeViolation``
    raised inside that block would otherwise be masked as a generic
    HTTP 500 — hiding an actionable "connect with a tenant" refusal
    behind "Internal Server Error".
    """
    return HTTPException(
        status_code=403,
        detail={
            "error": "tenant_scope_violation",
            "code": v.code,
            "message": v.message,
            "aql_digest": v.aql_digest[:16],
            "plan_digest": v.plan_digest[:16],
        },
    )


def _layer4_error_response(exc: AqlRewriteError) -> HTTPException:
    """Translate a Layer-4 refusal into an HTTP 422 / 403 response.

    ``UNCONSTRAINED_COLLECTION_ACCESS`` is a tenant-safety refusal
    (HTTP 403, same surface as Layer 5 violations); the other codes
    indicate a translator / EXPLAIN-side bug and map to HTTP 422 so
    the caller knows the request can't be retried as-is.
    """
    if exc.code in {"UNCONSTRAINED_COLLECTION_ACCESS"}:
        status = 403
        error_kind = "tenant_scope_violation"
    else:
        status = 422
        error_kind = "aql_rewrite_failed"
    return HTTPException(
        status_code=status,
        detail={
            "error": error_kind,
            "code": exc.code,
            "message": exc.message,
        },
    )


def _resolve_admin_bypass(
    *,
    cross_tenant: bool,
    bypass_reason: str | None,
    session: _Session,
) -> tuple[bool, str]:
    """Validate an MT-7 admin cross-tenant bypass request.

    Returns ``(admin_bypass, bypass_reason)``. A non-cross-tenant
    request resolves to ``(False, "")``. A cross-tenant request is
    refused unless the session is an admin (HTTP 403 ``ADMIN_REQUIRED``)
    and carries a non-empty reason for the audit stream (HTTP 422
    ``BYPASS_REASON_REQUIRED``). Gating here keeps the privilege check
    at the trust boundary; Layer 5 re-checks ``is_admin`` as defence in
    depth before honouring the bypass.
    """
    if not cross_tenant:
        return False, ""
    if not getattr(session, "is_admin", False):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "admin_required",
                "code": "ADMIN_REQUIRED",
                "message": "cross_tenant queries require an admin-flagged session",
            },
        )
    reason = (bypass_reason or "").strip()
    if not reason:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "bypass_reason_required",
                "code": "BYPASS_REASON_REQUIRED",
                "message": "cross_tenant requires a non-empty bypass_reason for the audit log",
            },
        )
    return True, reason


@app.post("/translate", response_model=TranslateResponse)
def translate_endpoint(
    req: TranslateRequest,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session | None = Depends(_optional_session),
):
    """Translate Cypher to AQL.

    When the request carries a session header (``X-Arango-Session``)
    and the session is tenant-bound, Layer 4 (AQL AST tenant-injection,
    Wave 8a / MT-4) runs on the transpiled AQL before the response is
    returned. Sessions without a tenant binding (workbench mode,
    single-tenant deployments) bypass the rewriter; the response's
    ``tenantRewritesAql`` field stays empty.

    Layer 4 needs a live DB handle for the EXPLAIN round-trip; when
    no session is attached we cannot run it, so the transpiled AQL
    is returned unchanged. This is safe because ``/translate`` never
    executes anything — the rewriter would have run inside
    ``/execute`` regardless.
    """
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
        log_endpoint_timing(
            "/translate",
            elapsed_ms,
            cypher_len=len(req.cypher or ""),
            aql_len=len(correction.corrected_aql or ""),
            correction_id=correction.id,
            extensions_enabled=req.extensions_enabled,
        )
        # Layer 4 over the corrected AQL is a no-op when the
        # correction itself was the answer the human gave; we still
        # run it so a stale correction that doesn't carry the tenant
        # predicate gets re-scoped on the way out (defence in depth).
        corrected_aql, corrected_bind, layer4_changes = _maybe_apply_layer4(
            session=session,
            mapping_dict=req.mapping,
            aql=correction.corrected_aql,
            bind_vars=correction.bind_vars or result.bind_vars,
        )
        return TranslateResponse(
            aql=corrected_aql,
            bind_vars=corrected_bind,
            warnings=[{"message": f"Using learned correction #{correction.id}"}]
            + list(result.warnings or []),
            elapsed_ms=elapsed_ms,
            tenantRewritesAql=layer4_changes,
        )

    rewritten_aql, rewritten_bind, layer4_changes = _maybe_apply_layer4(
        session=session,
        mapping_dict=req.mapping,
        aql=result.aql,
        bind_vars=result.bind_vars,
    )

    log_endpoint_timing(
        "/translate",
        elapsed_ms,
        cypher_len=len(req.cypher or ""),
        aql_len=len(rewritten_aql or ""),
        warnings=len(result.warnings or []),
        extensions_enabled=req.extensions_enabled,
        tenant_rewrites=len(layer4_changes),
    )
    return TranslateResponse(
        aql=rewritten_aql,
        bind_vars=rewritten_bind,
        warnings=result.warnings,
        elapsed_ms=elapsed_ms,
        tenantRewritesAql=layer4_changes,
    )


def _maybe_apply_layer4(
    *,
    session: _Session | None,
    mapping_dict: dict[str, object] | None,
    aql: str,
    bind_vars: dict[str, object],
) -> tuple[str, dict[str, object], list[str]]:
    """Run Layer 4 if a tenant-bound session is available.

    Returns ``(rewritten_aql, augmented_bind_vars, changes)``. When
    no session is attached or the session lacks a tenant binding,
    returns the inputs unchanged with an empty changes list — this
    is the workbench / single-tenant pass-through path.

    Translates :class:`AqlRewriteError` into an :class:`HTTPException`
    so the route handler can raise it without an extra try/except.
    """
    if session is None or not getattr(session, "tenant_id", None):
        return aql, dict(bind_vars or {}), []
    try:
        return apply_layer4_rewrite(
            db=session.db,
            aql=aql,
            bind_vars=bind_vars,
            session=session,
            mapping_dict=mapping_dict,
        )
    except AqlRewriteError as exc:
        raise _layer4_error_response(exc) from exc
    except TenantScopeViolation as exc:
        # Layer-6 / Layer-4 adapter raises this when mapping is
        # missing in tenant-bound mode. Re-raise as the canonical
        # 403 response so the UI surfaces it identically.
        raise HTTPException(
            status_code=403,
            detail={
                "error": "tenant_scope_violation",
                "code": exc.code,
                "message": exc.message,
            },
        ) from exc


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

    admin_bypass, bypass_reason = _resolve_admin_bypass(
        cross_tenant=req.cross_tenant,
        bypass_reason=req.bypass_reason,
        session=session,
    )

    # Layer 4 (AQL AST tenant-injection) before Layer 5 sees the AQL.
    # On tenant-bound sessions this rewrites every unscoped read; on
    # workbench / unbound sessions it's a structural no-op. Refusals
    # surface as HTTPException via `_maybe_apply_layer4`. An admin
    # cross-tenant bypass SKIPS Layer 4 — re-injecting a tenant
    # predicate would defeat the very cross-tenant read the operator
    # authorised (Layer 5's bypass then permits the unscoped plan).
    if admin_bypass:
        layer4_changes: list[str] = []
    else:
        run_aql, run_bind, layer4_changes = _maybe_apply_layer4(
            session=session,
            mapping_dict=req.mapping,
            aql=run_aql,
            bind_vars=run_bind,
        )

    with _translate_errors("AQL execution failed"):
        t_exec = time.perf_counter()
        # Convert the tenant refusal to a 403 *here*, inside the block,
        # so `_translate_errors` re-raises it as an HTTPException
        # untouched instead of masking it as a generic 500.
        try:
            cursor, run_bind = safe_execute_aql(
                db=session.db,
                aql=run_aql,
                bind_vars=run_bind,
                session=session,
                mapping_dict=req.mapping,
                admin_bypass=admin_bypass,
                bypass_reason=bypass_reason,
            )
            results = list(cursor)
        except TenantScopeViolation as v:
            raise _tenant_violation_response(v) from v
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)

    log_endpoint_timing(
        "/execute",
        round(translate_ms + exec_ms, 1),
        translate_ms=translate_ms,
        exec_ms=exec_ms,
        rows=len(results),
        cypher_len=len(req.cypher or ""),
        aql_len=len(run_aql or ""),
        used_correction=bool(correction),
        tenant_rewrites=len(layer4_changes),
    )
    return ExecuteResponse(
        results=results,
        aql=run_aql,
        bind_vars=run_bind,
        warnings=warnings,
        exec_ms=exec_ms,
        translate_ms=translate_ms,
        tenantRewritesAql=layer4_changes,
    )


@app.post("/execute-aql")
def execute_aql_endpoint(
    req: ExecuteAqlRequest,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
):
    """Execute a raw AQL query directly (used by NL→AQL direct path).

    Wave 7: gated through Layer 5 via :func:`safe_execute_aql`. When
    the session is tenant-bound and the caller does not supply a
    mapping, the request is refused (the validator has nothing to
    certify against). Unbound / workbench sessions keep working with a
    direct execute and a WARN audit log.
    """
    final_bind = req.bind_vars
    admin_bypass, bypass_reason = _resolve_admin_bypass(
        cross_tenant=req.cross_tenant,
        bypass_reason=req.bypass_reason,
        session=session,
    )
    # Layer 4 — the *only* tenant-scope defence on raw-AQL submissions.
    # When the session is tenant-bound and no mapping was supplied the
    # adapter raises TenantScopeViolation(NO_MAPPING_FOR_VALIDATION),
    # which `_maybe_apply_layer4` translates to a 403. The same
    # condition is checked by Layer 6 (safe_execute_aql) downstream;
    # checking here too means we refuse early without wasting an
    # EXPLAIN round-trip. An admin cross-tenant bypass skips Layer 4
    # (see /execute) so the raw AQL runs unscoped across tenants.
    if admin_bypass:
        run_aql, final_bind, layer4_changes = req.aql, dict(req.bind_vars or {}), []
    else:
        run_aql, final_bind, layer4_changes = _maybe_apply_layer4(
            session=session,
            mapping_dict=req.mapping,
            aql=req.aql,
            bind_vars=req.bind_vars,
        )
    with _translate_errors("AQL execution failed"):
        t_exec = time.perf_counter()
        try:
            cursor, final_bind = safe_execute_aql(
                db=session.db,
                aql=run_aql,
                bind_vars=final_bind,
                session=session,
                mapping_dict=req.mapping,
                admin_bypass=admin_bypass,
                bypass_reason=bypass_reason,
            )
            results = list(cursor)
        except TenantScopeViolation as v:
            raise _tenant_violation_response(v) from v
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)

    log_endpoint_timing(
        "/execute-aql",
        exec_ms,
        rows=len(results),
        aql_len=len(run_aql or ""),
        tenant_rewrites=len(layer4_changes),
    )
    return ExecuteResponse(
        results=results,
        aql=run_aql,
        bind_vars=final_bind,
        warnings=[],
        exec_ms=exec_ms,
        tenantRewritesAql=layer4_changes,
    )


@app.post("/validate", response_model=ValidateResponse)
def validate_endpoint(
    req: ValidateRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Validate Cypher against the translator profile."""
    t0 = time.perf_counter()
    mapping = _mapping_from_dict(req.mapping)
    result = validate_cypher_profile(
        req.cypher,
        mapping=mapping,
        params=req.params,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    log_endpoint_timing(
        "/validate",
        elapsed_ms,
        ok=bool(result.ok),
        error_count=len(result.errors or []),
        cypher_len=len(req.cypher or ""),
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

    t_explain = time.perf_counter()
    with _translate_errors("AQL EXPLAIN failed"):
        plan = session.db.aql.explain(transpiled.aql, bind_vars=transpiled.bind_vars)
    explain_ms = round((time.perf_counter() - t_explain) * 1000, 1)

    log_endpoint_timing(
        "/explain",
        round(translate_ms + explain_ms, 1),
        translate_ms=translate_ms,
        explain_ms=explain_ms,
        cypher_len=len(req.cypher or ""),
        aql_len=len(transpiled.aql or ""),
    )
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

    # Layer 4 over the transpiled AQL before the profiled execute.
    run_aql, run_bind, layer4_changes = _maybe_apply_layer4(
        session=session,
        mapping_dict=req.mapping,
        aql=transpiled.aql,
        bind_vars=transpiled.bind_vars,
    )
    final_bind = run_bind
    t_exec = time.perf_counter()
    with _translate_errors("AQL profiled execution failed"):
        try:
            cursor, final_bind = safe_execute_aql(
                db=session.db,
                aql=run_aql,
                bind_vars=run_bind,
                session=session,
                mapping_dict=req.mapping,
                execute_kwargs={"profile": True},
            )
            results = list(cursor)
            stats = cursor.statistics()
            profile_data = cursor.profile() if hasattr(cursor, "profile") else None
        except TenantScopeViolation as v:
            raise _tenant_violation_response(v) from v
    exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)

    log_endpoint_timing(
        "/aql-profile",
        round(translate_ms + exec_ms, 1),
        translate_ms=translate_ms,
        exec_ms=exec_ms,
        rows=len(results),
        cypher_len=len(req.cypher or ""),
        aql_len=len(run_aql or ""),
        tenant_rewrites=len(layer4_changes),
    )
    return {
        "aql": run_aql,
        "bind_vars": final_bind,
        "results": results,
        "statistics": stats,
        "profile": profile_data,
        "translate_ms": translate_ms,
        "tenantRewritesAql": layer4_changes,
    }
