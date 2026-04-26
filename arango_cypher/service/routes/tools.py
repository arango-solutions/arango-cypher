"""Agentic tool dispatch endpoints — ``/tools/schemas``, ``/tools/call``,
``/suggest-indexes``.
"""

from __future__ import annotations

from fastapi import Depends

from ..app import app
from ..models import SuggestIndexesRequest, ToolCallRequest
from ..security import _check_compute_rate_limit


@app.get("/tools/schemas")
def tools_schemas():
    """Return OpenAI-compatible function schemas for all agentic tools."""
    from ...tools import get_tool_schemas

    return {"tools": get_tool_schemas()}


@app.post("/tools/call")
def tools_call(
    req: ToolCallRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Dispatch a tool call by name with arguments."""
    from ...tools import call_tool

    return call_tool(req.name, req.arguments)


@app.post("/suggest-indexes")
def suggest_indexes(
    req: SuggestIndexesRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Suggest indexes for the given mapping."""
    from ...tools import suggest_indexes_tool

    return suggest_indexes_tool({"mapping": req.mapping})
