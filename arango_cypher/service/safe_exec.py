"""Service-side adapter for :func:`arango_query_core.exec.safe_execute`.

The wave-7 Layer-6 wrapper (``safe_execute``) is intentionally placed
in :mod:`arango_query_core.exec` so the lower core layer stays free of
any reverse dependency on :mod:`arango_cypher`. This module wires the
two halves together for the FastAPI routes:

* Builds a :class:`~arango_cypher.nl2cypher.tenant_scope.TenantScopeManifest`
  from the request's mapping bundle.
* Extracts the ``shardingProfile`` block and computes a
  ``collection_to_entity`` map from the physical mapping.
* Injects :func:`arango_cypher.tenant_plan_validator.validate_plan` as
  the Layer-5 validator and calls ``safe_execute``.

Routes call :func:`safe_execute_aql` and receive ``(cursor, bind_vars)``
just like the legacy ``db.aql.execute`` site, except the cursor only
ever materialises if Layer 5 certified the plan first.

When the request carries no mapping (e.g. the historical
``/execute-aql`` flow), Layer 5 cannot be invoked — there is no
manifest to validate against. In that case the wrapper refuses for
tenant-bound sessions (fail-closed) and falls through to a direct
execute for unbound / workbench sessions so the legacy single-tenant
use-case keeps working.
"""

from __future__ import annotations

import logging
from typing import Any

from arango_query_core import CoreError
from arango_query_core import safe_execute as _core_safe_execute

from ..nl2cypher.tenant_ast_cypher import (
    TenantScopeRewriteIncomplete,
    TenantScopeRewriteRejection,
)
from ..nl2cypher.tenant_ast_cypher import (
    inject_tenant_scope as _inject_tenant_scope_cypher,
)
from ..nl2cypher.tenant_scope import (
    TenantScopeManifest,
    analyze_tenant_scope,
)
from ..parser import parse_cypher
from ..tenant_ast_aql import (
    AqlRewriteError,
    inject_tenant_scope,
)
from ..tenant_plan_validator import (
    TenantScopeViolation,
    validate_plan,
)
from .mapping import _mapping_from_dict
from .security import _Session

logger = logging.getLogger(__name__)


def _physical_mapping(mapping: Any) -> dict[str, Any]:
    if mapping is None:
        return {}
    if hasattr(mapping, "physical_mapping"):
        pm = mapping.physical_mapping or {}
    elif isinstance(mapping, dict):
        pm = mapping.get("physical_mapping") or mapping.get("physicalMapping") or {}
    else:
        pm = {}
    return pm if isinstance(pm, dict) else {}


def _metadata(mapping: Any) -> dict[str, Any]:
    if mapping is None:
        return {}
    if hasattr(mapping, "metadata"):
        md = mapping.metadata or {}
    elif isinstance(mapping, dict):
        md = mapping.get("metadata") or {}
    else:
        md = {}
    return md if isinstance(md, dict) else {}


def _collection_to_entity_map(mapping: Any) -> dict[str, str]:
    """Map each physical collection name back to its conceptual entity.

    Inverts the ``physical_mapping.entities[entity].collectionName``
    relation. When the analyzer didn't supply a collection name (older
    bundles, hand-crafted fixtures) the entity name is assumed to
    double as the collection name — the convention every existing
    test relies on.
    """
    pm = _physical_mapping(mapping).get("entities") or {}
    if not isinstance(pm, dict):
        return {}
    out: dict[str, str] = {}
    for entity_name, entry in pm.items():
        if not isinstance(entity_name, str):
            continue
        coll = (
            entry.get("collectionName")
            if isinstance(entry, dict) and isinstance(entry.get("collectionName"), str)
            else entity_name
        )
        out[coll] = entity_name
    return out


def _build_validator_inputs(
    mapping_dict: dict[str, Any] | None,
) -> tuple[TenantScopeManifest | None, dict[str, Any] | None, dict[str, str]]:
    """Return ``(manifest, sharding_profile, collection_to_entity)``
    for the validator. Each may be ``None`` / empty when the bundle is
    missing the corresponding block; callers fail-closed when the
    bundle is incomplete and the session is tenant-bound.
    """
    if not mapping_dict:
        return None, None, {}
    mapping = _mapping_from_dict(mapping_dict)
    if mapping is None:
        return None, None, {}
    manifest = analyze_tenant_scope(mapping)
    md = _metadata(mapping)
    sharding_profile = md.get("shardingProfile") if isinstance(md.get("shardingProfile"), dict) else None
    coll_to_entity = _collection_to_entity_map(mapping)
    return manifest, sharding_profile, coll_to_entity


def safe_execute_aql(
    *,
    db: Any,
    aql: str,
    bind_vars: dict[str, Any] | None,
    session: _Session,
    mapping_dict: dict[str, Any] | None,
    execute_kwargs: dict[str, Any] | None = None,
    admin_bypass: bool = False,
    bypass_reason: str = "",
) -> tuple[Any, dict[str, Any]]:
    """Layer-6 entry point used by every Cypher- or AQL-execute route.

    When *mapping_dict* is supplied, Layer 5 fully validates the
    EXPLAIN plan against the manifest derived from it. When no mapping
    is supplied:

    * If the session is tenant-bound (``session.tenant_id is not None``)
      the call is refused with :class:`TenantScopeViolation`
      (``code="NO_MAPPING_FOR_VALIDATION"``). This is the strict
      tenant-user-mode contract — no mapping, no validation, no
      execute.
    * If the session is unbound (workbench / single-tenant), the call
      falls through to a direct ``db.aql.execute`` with the session
      tenant bind vars still spread on top of the caller's. A WARNING
      records the bypass for audit.

    MT-7 — when ``admin_bypass`` is set and the session is an admin,
    Layer 5's tenant-scope enforcement is skipped (the structural
    EXPLAIN check still runs) so an operator may span tenants; the
    validator records the event on the ``arango_cypher.tenant_audit``
    stream. The no-mapping fail-closed refusal above is skipped for a
    bypass call — the cross-tenant read intentionally has no single
    tenant to certify against, and Layer 5 still structurally EXPLAINs.
    """
    manifest, sharding_profile, coll_to_entity = _build_validator_inputs(mapping_dict)

    bypass_active = bool(admin_bypass) and bool(getattr(session, "is_admin", False)) and bool(bypass_reason)

    if manifest is None and not bypass_active:
        if getattr(session, "tenant_id", None) is not None:
            raise TenantScopeViolation(
                code="NO_MAPPING_FOR_VALIDATION",
                message=(
                    "tenant-bound session requires a mapping bundle so "
                    "Layer 5 (EXPLAIN-plan validator) can certify the "
                    "plan against the schema — refusing fail-closed"
                ),
            )
        logger.warning(
            "safe_execute_aql: no mapping supplied and session has no "
            "tenant_id; bypassing Layer 5 for unbound session=%s",
            (session.token[:8] if getattr(session, "token", None) else "-"),
        )
        bv = dict(bind_vars or {})
        cursor = db.aql.execute(aql, bind_vars=bv, **(execute_kwargs or {}))
        return cursor, bv

    return _core_safe_execute(
        db=db,
        aql=aql,
        client_bind_vars=bind_vars,
        session=session,
        validator=validate_plan,
        manifest=manifest,
        sharding_profile=sharding_profile,
        collection_to_entity=coll_to_entity,
        execute_kwargs=execute_kwargs,
        admin_bypass=admin_bypass,
        bypass_reason=bypass_reason,
    )


def apply_layer4_rewrite(
    *,
    db: Any,
    aql: str,
    bind_vars: dict[str, Any] | None,
    session: _Session,
    mapping_dict: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], list[str]]:
    """Layer 4 (AQL AST tenant-injection) wrapper used by every route
    that produces or accepts AQL before handing it to Layer 5 +
    Layer 6.

    Returns ``(rewritten_aql, augmented_bind_vars, changes)``. When
    the rewrite is a structural no-op (no mapping, no tenant entity,
    session not tenant-bound), returns the inputs unchanged and an
    empty changes list.

    The route adapter centralises the manifest / sharding-profile
    derivation so the routes stay slim and so the rewriter call
    contract is identical at every site (defence in depth — one
    integration bug to fix, not three). Mirrors the
    :func:`safe_execute_aql` adapter for Layer 5.

    Tenant-unbound sessions (workbench / single-tenant) bypass the
    rewriter entirely: there is no session tenant to scope against,
    and Layer 5 already accepts global / satellite-only queries
    without a tenant bind. This matches the same "tenant-bound or
    bypass" gating Layer 6 already implements.
    """
    tenant_id = getattr(session, "tenant_id", None)
    tenant_key = getattr(session, "tenant_key", None) or tenant_id or ""
    if not tenant_id:
        # Workbench / unbound — Layer 4 is a structural no-op.
        return aql, dict(bind_vars or {}), []

    manifest, sharding_profile, coll_to_entity = _build_validator_inputs(mapping_dict)
    if manifest is None:
        # No mapping in tenant-bound mode. We honour the same fail-
        # closed posture as Layer 6: refuse — the rewriter cannot
        # tell which collections are tenant-scoped, and silently
        # passing through would expose every read.
        raise TenantScopeViolation(
            code="NO_MAPPING_FOR_VALIDATION",
            message=(
                "tenant-bound session requires a mapping bundle so "
                "Layer 4 (AQL AST tenant-injection) can scope the "
                "query against the schema — refusing fail-closed"
            ),
        )
    return inject_tenant_scope(
        db=db,
        aql=aql,
        bind_vars=bind_vars,
        manifest=manifest,
        sharding_profile=sharding_profile,
        tenant_id=tenant_id,
        tenant_key=tenant_key,
        collection_to_entity=coll_to_entity,
    )


def apply_layer3_rewrite(
    *,
    cypher: str,
    mapping_dict: dict[str, Any] | None,
    session: _Session,
) -> tuple[str, list[str]]:
    """Layer 3 (Cypher AST tenant-injection) wrapper for routes that
    produce Cypher (currently ``/nl2cypher``).

    Returns ``(rewritten_cypher, changes)``. Mirrors
    :func:`apply_layer4_rewrite` for the AQL path:

    * **Tenant-unbound sessions** (workbench / single-tenant) bypass the
      rewriter — there is no session tenant to scope against.
    * **Tenant-bound, no mapping** — skip (return the input unchanged).
      The generated Cypher will be transpiled and submitted via
      ``/execute``, where Layer 5 refuses fail-closed if it actually
      leaks. This matches the ``/nl2aql`` Layer-4 gating, which also
      skips when the request carries no mapping.
    * **Unparseable Cypher** — return unchanged; the transpiler will
      surface the parse error to the caller.
    * **:class:`TenantScopeRewriteIncomplete`** (traversal-path /
      multi-label cases MT-3a defers) — fall back to the original Cypher
      and log; Layer 5 is the security boundary and still enforces.
    * **:class:`TenantScopeRewriteRejection`** (literal tenant predicate,
      unknown label) — propagates to the route, which surfaces it as an
      HTTP 403 (fail-closed).

    The tree's token offsets are relative to the *normalized* source
    (``parse_cypher`` may insert an implicit ``MATCH`` into
    ``exists{(…)}`` shorthand), so the rewriter is fed
    ``ParseResult.normalized`` rather than the caller's original text.
    """
    tenant_id = getattr(session, "tenant_id", None)
    tenant_key = getattr(session, "tenant_key", None) or tenant_id or ""
    if not tenant_id or not cypher:
        return cypher, []

    manifest, _sharding, _coll_to_entity = _build_validator_inputs(mapping_dict)
    if manifest is None:
        # No mapping in tenant-bound mode. Skip Layer 3 (best-effort
        # rewrite) — Layer 5 enforces fail-closed at execute time.
        return cypher, []

    try:
        parsed = parse_cypher(cypher)
    except CoreError:
        return cypher, []

    try:
        return _inject_tenant_scope_cypher(
            cypher=parsed.normalized,
            parse_tree=parsed.tree,
            manifest=manifest,
            tenant_id=tenant_id,
            tenant_key=tenant_key,
        )
    except TenantScopeRewriteIncomplete as exc:
        logger.info(
            "Layer 3 (Cypher AST) rewrite incomplete for %r; falling back to "
            "original Cypher (Layer 5 enforces at execute)",
            exc.label,
        )
        return cypher, []


__all__ = [
    "AqlRewriteError",
    "TenantScopeRewriteRejection",
    "apply_layer3_rewrite",
    "apply_layer4_rewrite",
    "safe_execute_aql",
]
