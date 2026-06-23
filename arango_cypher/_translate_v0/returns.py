"""Standalone ``RETURN`` compilation for the v0 translator.

A Cypher query may consist of a single ``RETURN`` of constant or computed
expressions with no reading clause — e.g. ``RETURN "hello"`` or
``RETURN 1 + 1 AS x``. AQL supports the same shape as a top-level
``RETURN <expr>`` (one output row), so we translate it directly instead of
rejecting it with "MATCH is required".

Row-stream scaffolding (``DISTINCT``, ``ORDER BY``, ``SKIP``/``LIMIT``,
``RETURN *``, aggregations) has no meaning without a source and is rejected
with a precise message rather than silently mistranslated.
"""

from __future__ import annotations

from typing import Any

from arango_query_core import AqlQuery, CoreError

from .._antlr.CypherParser import CypherParser


def _translate_standalone_return(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate a ``RETURN``-only query (no MATCH/UNWIND/CALL) into AQL."""
    from .core import _compile_agg_expr, _compile_expression, _compile_return_object

    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("MATCH is required in v0 subset", code="UNSUPPORTED")

    proj = ret.oC_ProjectionBody()
    if proj.DISTINCT() is not None:
        raise CoreError(
            "DISTINCT requires a MATCH/UNWIND source in v0 subset", code="UNSUPPORTED"
        )
    if proj.oC_Order() is not None or proj.oC_Skip() is not None or proj.oC_Limit() is not None:
        raise CoreError(
            "ORDER BY/SKIP/LIMIT require a MATCH/UNWIND source in v0 subset",
            code="UNSUPPORTED",
        )

    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem() if items_ctx is not None else []
    if not items:
        # Covers ``RETURN *`` (no concrete items) — meaningless with no
        # bound variables.
        raise CoreError(
            "RETURN * requires a MATCH/UNWIND source in v0 subset", code="UNSUPPORTED"
        )

    compiled_items: list[tuple[str | None, str]] = []
    for it in items:
        expr_ctx = it.oC_Expression()
        expr_txt = expr_ctx.getText().strip()
        if _compile_agg_expr(expr_txt) is not None:
            raise CoreError(
                "Aggregation in a bare RETURN is not supported in v0 subset",
                code="UNSUPPORTED",
            )
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None
        expr = _compile_expression(expr_ctx, bind_vars)
        compiled_items.append((alias, expr))

    # A single unaliased expression returns a scalar column (Cypher
    # ``RETURN "hello"`` → AQL ``RETURN "hello"``); anything else returns an
    # object keyed by alias/inferred name to preserve column identity.
    if len(compiled_items) == 1 and compiled_items[0][0] is None:
        body = compiled_items[0][1]
    else:
        body = _compile_return_object(compiled_items)

    return AqlQuery(text=f"RETURN {body}", bind_vars=bind_vars)
