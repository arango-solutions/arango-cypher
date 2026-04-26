"""Agentic tool dispatch endpoints — ``/tools/schemas``, ``/tools/call``,
``/suggest-indexes``.
"""

from __future__ import annotations

import time

from fastapi import Depends

from ..app import app
from ..models import SuggestIndexesRequest, ToolCallRequest
from ..observability import log_endpoint_timing
from ..security import _check_compute_rate_limit


@app.get("/tools/schemas")
def tools_schemas():
    """Return OpenAI-compatible function schemas for all agentic tools."""
    from ...tools import get_tool_schemas

    t0 = time.perf_counter()
    schemas = get_tool_schemas()
    log_endpoint_timing(
        "/tools/schemas",
        round((time.perf_counter() - t0) * 1000, 1),
        tools=len(schemas) if isinstance(schemas, list) else 0,
    )
    return {"tools": schemas}


@app.post("/tools/call")
def tools_call(
    req: ToolCallRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Dispatch a tool call by name with arguments."""
    from ...tools import call_tool

    t0 = time.perf_counter()
    result = call_tool(req.name, req.arguments)
    log_endpoint_timing(
        "/tools/call",
        round((time.perf_counter() - t0) * 1000, 1),
        tool=req.name,
    )
    return result


@app.post("/suggest-indexes")
def suggest_indexes(
    req: SuggestIndexesRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Suggest indexes for the given mapping."""
    from ...tools import suggest_indexes_tool

    t0 = time.perf_counter()
    result = suggest_indexes_tool({"mapping": req.mapping})
    log_endpoint_timing(
        "/suggest-indexes",
        round((time.perf_counter() - t0) * 1000, 1),
        suggestions=len(result.get("suggestions") or []) if isinstance(result, dict) else 0,
    )
    return result
