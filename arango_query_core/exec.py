from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .aql import AqlQuery


def _aql_references_bindvar(aql: str, name: str) -> bool:
    """Whether *aql* references the value bind parameter ``@name``.

    ArangoDB rejects a query (``[ERR 1552] bind parameter '<name>' was
    not declared in the query``) when ``bind_vars`` carries a key the
    query text never uses. The Layer-6 session-spread must therefore
    add ``tenantId`` / ``tenantKey`` **only** when the AQL actually
    references them — otherwise a perfectly safe global / satellite-only
    read (e.g. ``FOR c IN Country RETURN c``) explodes at EXPLAIN time
    just because the session injected an unused tenant bind.

    Matches a single-``@`` *value* reference (``@tenantId``) and
    deliberately ignores the doubled *collection* form (``@@tenantId``)
    — tenant scope binds are always value binds. The name boundary
    stops ``@tenantId`` from matching a longer identifier such as
    ``@tenantIdentifier``.

    This is a textual heuristic, mirroring Layer 4's textual splice
    approach. A false positive (``@tenantId`` appearing only inside an
    AQL comment or string literal) would re-introduce ERR 1552, but the
    transpiler and Layer 3/4 only ever emit the token in real predicate
    positions, so the heuristic is safe for produced AQL.
    """
    return re.search(r"(?<![@\w])@" + re.escape(name) + r"(?![\w])", aql) is not None


@dataclass
class AqlExecutor:
    db: Any  # python-arango Database

    def execute(self, query: AqlQuery, *, batch_size: int | None = None, **kwargs: Any) -> Any:
        aql = self.db.aql
        return aql.execute(query.text, bind_vars=query.bind_vars, batch_size=batch_size, **kwargs)


def safe_execute(
    *,
    db: Any,
    aql: str,
    client_bind_vars: dict[str, Any] | None,
    session: Any,
    validator: Callable[..., None],
    manifest: Any,
    sharding_profile: dict[str, Any] | None,
    collection_to_entity: dict[str, str] | None = None,
    execute_kwargs: dict[str, Any] | None = None,
    admin_bypass: bool = False,
    bypass_reason: str = "",
) -> tuple[Any, dict[str, Any]]:
    """Layer 6 — execute AQL only after Layer 5 has certified the plan.

    Implements the contract from ``docs/multitenant_prd.md`` §9 / Wave 7
    part 4. The bind-var spread order is load-bearing:

    .. code-block:: python

        bind_vars = {
            **client_bind_vars,
            "tenantId": session.tenant_id,
            "tenantKey": session.tenant_key,
        }

    The session value silently overrides any caller-supplied
    ``tenantId`` / ``tenantKey``. Layer 5 (``validator``) then
    verifies the bind-var matches the session — closing T7 (bind-var
    override) by construction.

    The session binds are spread **only when the AQL references the
    corresponding ``@tenantId`` / ``@tenantKey`` parameter**. ArangoDB
    refuses a query that declares a bind value it never uses
    (``[ERR 1552] bind parameter '<name>' was not declared in the
    query``), so unconditionally injecting the session tenant would
    blow up every safe global / satellite-only read at EXPLAIN time.
    A query that *does* reference the tenant bind always has the
    session value win; a client-supplied ``tenantId`` on a query that
    never uses ``@tenantId`` is inert and is dropped (it cannot affect
    results and would only trip ERR 1552).

    Returns ``(cursor, bind_vars)`` so the caller can echo the final
    bind vars back to the UI for transparency (§9.2). The validator is
    injected rather than imported here so ``arango_query_core`` stays
    free of an ``arango_cypher`` reverse dependency.

    Raises whatever the validator raises — typically
    ``arango_cypher.tenant_plan_validator.TenantScopeViolation`` — on
    refusal. The execute never runs in that case.
    """
    if session is None:
        raise PermissionError("safe_execute: no authenticated session; cannot bind tenant context")
    bind_vars = dict(client_bind_vars or {})
    if _aql_references_bindvar(aql, "tenantId"):
        bind_vars["tenantId"] = getattr(session, "tenant_id", None)
    else:
        bind_vars.pop("tenantId", None)
    if _aql_references_bindvar(aql, "tenantKey"):
        bind_vars["tenantKey"] = getattr(session, "tenant_key", None)
    else:
        bind_vars.pop("tenantKey", None)

    validator(
        db=db,
        aql=aql,
        bind_vars=bind_vars,
        manifest=manifest,
        sharding_profile=sharding_profile,
        collection_to_entity=collection_to_entity,
        session=session,
        admin_bypass=admin_bypass,
        bypass_reason=bypass_reason,
    )

    cursor = db.aql.execute(aql, bind_vars=bind_vars, **(execute_kwargs or {}))
    return cursor, bind_vars


def explain_aql(
    db: Any,
    aql: str,
    bind_vars: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Plan the query via ``POST /_api/explain`` without executing it.

    Returns ``(ok, error_message)``. On success, *error_message* is the
    empty string. On failure, it's a short, LLM-friendly description of
    the planner error — short enough to paste back into a retry prompt,
    and stripped of stack traces, HTTP payloads, and sensitive detail.

    This is the hook WP-25.3 uses to catch semantic errors
    (non-existent collections/properties, invalid traversal directions)
    that ANTLR's grammar-only check happily waves through.  We never
    execute the query — the ``explain`` endpoint only plans it, so there
    is no cost to row materialization.

    Safe to call against any read-only or read-write database: the AQL
    is only planned, never run.  Caller is responsible for ensuring
    *db* is a valid python-arango database handle.
    """
    bv = dict(bind_vars or {})
    try:
        result = db.aql.explain(aql, bind_vars=bv)
    except Exception as exc:
        return False, _summarize_explain_error(exc)

    if isinstance(result, dict) and result.get("error"):
        msg = str(result.get("errorMessage") or result.get("error") or "EXPLAIN failed")
        return False, msg[:500]
    return True, ""


def _summarize_explain_error(exc: BaseException) -> str:
    """Collapse a python-arango / server error into a single short line.

    python-arango raises ``AQLQueryExplainError`` or ``ArangoServerError``
    whose ``str()`` can include multi-line HTTP payloads and stack frames.
    We strip to the most informative line for LLM feedback.
    """
    msg = str(exc) or exc.__class__.__name__
    msg = msg.splitlines()[0] if "\n" in msg else msg
    msg = msg.strip()
    if len(msg) > 500:
        msg = msg[:497] + "..."
    return msg
