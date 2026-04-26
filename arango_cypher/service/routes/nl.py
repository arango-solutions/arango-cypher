"""NL-to-Cypher / NL-to-AQL endpoints + tenant catalog —
``/nl2cypher``, ``/nl-samples``, ``/nl2aql``, ``/tenants``.
"""

from __future__ import annotations

import time

from fastapi import Depends, HTTPException

from ..app import _PUBLIC_MODE, app
from ..models import NL2AqlRequest, NL2CypherRequest, NLSuggestRequest
from ..security import (
    _COLLECTION_NAME_RE,
    _check_nl_rate_limit,
    _get_session,
    _require_session_in_public_mode,
    _Session,
    _sessions,
    _translate_errors,
)


@app.post("/nl2cypher")
def nl2cypher_endpoint(
    req: NL2CypherRequest,
    _: None = Depends(_check_nl_rate_limit),
    auth_session: _Session | None = Depends(_require_session_in_public_mode),
):
    """Translate a natural language question into Cypher.

    When ``session_token`` is supplied and entity resolution is enabled,
    the session's live ``StandardDatabase`` is passed through to
    :func:`nl_to_cypher` so mentions in the question can be rewritten to
    their database-correct form (WP-25.2).  Without a token the resolver
    is silently disabled and the prompt falls back to its pre-WP-25.2
    shape.

    In ``ARANGO_CYPHER_PUBLIC_MODE`` the request body's
    ``session_token`` field is ignored — the authenticated session
    (resolved from ``X-Arango-Session`` / ``Authorization``) is used
    instead, so a caller cannot point one user's NL request at another
    user's database by guessing the body field.
    """
    from ...nl2cypher import nl_to_cypher
    from ...nl2cypher.tenant_guardrail import TenantContext

    db = None
    if _PUBLIC_MODE:
        if auth_session is not None and req.use_entity_resolution:
            db = auth_session.db
            auth_session.touch()
    elif req.use_entity_resolution and req.session_token:
        sess = _sessions.get(req.session_token)
        if sess is not None:
            db = sess.db
            sess.touch()

    tenant_ctx = None
    if req.tenant_context is not None:
        tenant_ctx = TenantContext(
            property=req.tenant_context.property,
            value=req.tenant_context.value,
            display=req.tenant_context.display,
        )

    t0 = time.perf_counter()
    result = nl_to_cypher(
        req.question,
        mapping=req.mapping,
        use_llm=req.use_llm,
        use_fewshot=req.use_fewshot,
        use_entity_resolution=req.use_entity_resolution,
        db=db,
        tenant_context=tenant_ctx,
        retry_context=req.retry_context,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "cypher": result.cypher,
        "explanation": result.explanation,
        "confidence": result.confidence,
        "method": result.method,
        "elapsed_ms": elapsed_ms,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "cached_tokens": result.cached_tokens,
        "retries": result.retries,
    }


@app.post("/nl-samples")
def nl_samples_endpoint(
    req: NLSuggestRequest,
    _: None = Depends(_check_nl_rate_limit),
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Return a representative set of NL questions for the given schema.

    Used by the UI to seed the "Ask" history after schema mapping. Falls back
    to rule-based generation when no LLM provider is configured.
    """
    from ...nl2cypher import suggest_nl_queries

    t0 = time.perf_counter()
    queries = suggest_nl_queries(
        req.mapping,
        count=req.count,
        use_llm=req.use_llm,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {"queries": queries, "elapsed_ms": elapsed_ms}


@app.post("/nl2aql")
def nl2aql_endpoint(
    req: NL2AqlRequest,
    _: None = Depends(_check_nl_rate_limit),
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Translate a natural language question directly into AQL (bypassing Cypher)."""
    from ...nl2cypher import nl_to_aql
    from ...nl2cypher.tenant_guardrail import TenantContext

    tenant_ctx = None
    if req.tenant_context is not None:
        tenant_ctx = TenantContext(
            property=req.tenant_context.property,
            value=req.tenant_context.value,
            display=req.tenant_context.display,
        )

    t0 = time.perf_counter()
    result = nl_to_aql(
        req.question,
        mapping=req.mapping,
        tenant_context=tenant_ctx,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "aql": result.aql,
        "bind_vars": result.bind_vars,
        "explanation": result.explanation,
        "confidence": result.confidence,
        "method": result.method,
        "elapsed_ms": elapsed_ms,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "cached_tokens": result.cached_tokens,
    }


# ---------------------------------------------------------------------------
# Tenant catalog (multi-tenant graphs)
# ---------------------------------------------------------------------------

# Maximum number of tenants to surface in a single catalog response. 10k is
# ample headroom for the target schemas (Dagster-style graphs top out around
# 10³); clients that need more should paginate via a follow-up API.
_TENANT_CATALOG_LIMIT = 10000


@app.get("/tenants")
def tenants_endpoint(
    collection: str | None = None,
    session: _Session = Depends(_get_session),
):
    """Return the list of tenants in the connected database, if any.

    The optional ``collection`` query parameter lets the UI tell the
    server which ArangoDB collection backs the conceptual ``Tenant``
    entity (typically derived client-side from
    ``physical_mapping.entities.Tenant.collectionName``). When omitted,
    the endpoint falls back to the literal name ``Tenant`` — the
    pre-Wave-4r behaviour, kept for compatibility with stale UIs.

    Why a query param instead of POST-with-mapping? Three reasons:

    1. POST-with-body for a pure read trips CORS preflights in
       cross-origin deployments.
    2. A new UI bundle deployed against an older service (the common
       case during rolling deploys) would 405 on POST and silently
       hide the selector with no diagnostic.
    3. The mapping already lives in the UI's state; sending it back
       just so the server can pluck a single string out wastes a
       megabyte of payload per call on real schemas.

    The response includes ``collection`` (the resolved name we
    queried) and ``source`` (``"client"`` when the caller supplied
    the name, ``"heuristic"`` when we fell back to ``"Tenant"``)
    so the UI can show *why* detection succeeded or failed.
    """
    db = session.db
    if collection:
        resolved, source = collection, "client"
    else:
        resolved, source = "Tenant", "heuristic"

    # Defence-in-depth against AQL identifier injection: the resolved name is
    # interpolated into the AQL f-string below inside backticks, so anything
    # that isn't a valid ArangoDB collection identifier must be rejected at
    # the edge. `has_collection()` returns False for names that don't exist
    # but does *not* reject syntactically invalid names on all client
    # versions, hence the explicit gate.
    if not _COLLECTION_NAME_RE.fullmatch(resolved):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid collection name: must be 1–256 characters, start "
                "with a letter or underscore, and contain only letters, "
                "digits, underscore, or hyphen."
            ),
        )

    with _translate_errors("Failed to inspect collections"):
        has_collection = db.has_collection(resolved)

    if not has_collection:
        return {
            "detected": False,
            "tenants": [],
            "collection": resolved,
            "source": source,
        }

    aql = (
        f"FOR t IN `{resolved}` "
        f"LIMIT {_TENANT_CATALOG_LIMIT} "
        "SORT t.NAME "
        "RETURN { "
        # `id` (full _id, e.g. 'Tenant/<uuid>') is the canonical
        # tenant identifier — what the guardrail uses to scope
        # generated Cypher. `key` is exposed too for tooltips and
        # for the Cypher `{_key: '...'}` shorthand. The schema-
        # specific NAME / SUBDOMAIN / TENANT_HEX_ID fields are
        # surfaced for human display and search but are not
        # required to exist; the LIMIT-projection tolerates nulls.
        "id: t._id, "
        "key: t._key, "
        "name: t.NAME, "
        "subdomain: t.SUBDOMAIN, "
        "hex_id: t.TENANT_HEX_ID "
        "}"
    )
    with _translate_errors("Tenant catalog query failed"):
        cursor = db.aql.execute(aql)
        tenants = list(cursor)

    return {
        "detected": True,
        "tenants": tenants,
        "collection": resolved,
        "source": source,
    }
