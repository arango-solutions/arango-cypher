from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .aql import AqlQuery


@dataclass
class AqlExecutor:
    db: Any  # python-arango Database

    def execute(self, query: AqlQuery, *, batch_size: int | None = None, **kwargs: Any) -> Any:
        aql = self.db.aql
        return aql.execute(query.text, bind_vars=query.bind_vars, batch_size=batch_size, **kwargs)


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

