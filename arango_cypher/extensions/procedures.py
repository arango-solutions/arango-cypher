"""Built-in arango.* procedure compilers for CALL ... YIELD translation.

Procedure compilers receive ``(args: list[str], bind_vars: dict)`` and return
an AQL expression string that produces the iterable result set.  The translator
wraps the result with ``FOR <yield_var> IN (<expr>)`` or multi-variable
destructuring.
"""

from __future__ import annotations

from typing import Any

from arango_query_core import CoreError, ExtensionRegistry


def _compile_fulltext_search(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``CALL arango.fulltext(collection, attribute, query)``

    Translates to AQL::

        FULLTEXT(@@collection, attribute, query)

    Returns an iterable expression of matching documents.
    """
    if len(args) != 3:
        raise CoreError(
            "arango.fulltext expects 3 arguments: (collection, attribute, queryString)",
            code="UNSUPPORTED",
        )
    return f"FULLTEXT({args[0]}, {args[1]}, {args[2]})"


def _compile_near(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``CALL arango.near(collection, lat, lon[, limit])``

    Translates to AQL ``NEAR(collection, lat, lon[, limit])``.
    """
    if len(args) < 3 or len(args) > 4:
        raise CoreError(
            "arango.near expects 3-4 arguments: (collection, lat, lon[, limit])",
            code="UNSUPPORTED",
        )
    return f"NEAR({', '.join(args)})"


def _compile_within(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``CALL arango.within(collection, lat, lon, radius)``

    Translates to AQL ``WITHIN(collection, lat, lon, radius)``.
    """
    if len(args) != 4:
        raise CoreError(
            "arango.within expects 4 arguments: (collection, lat, lon, radius)",
            code="UNSUPPORTED",
        )
    return f"WITHIN({', '.join(args)})"


def _compile_shortest_path(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``CALL arango.shortest_path(startVertex, targetVertex, edgeCollection, direction)``

    Translates to AQL::

        (FOR v IN OUTBOUND SHORTEST_PATH startVertex TO targetVertex edgeCollection RETURN v)
    """
    if len(args) != 4:
        raise CoreError(
            "arango.shortest_path expects 4 arguments: "
            "(startVertex, targetVertex, edgeCollection, direction)",
            code="UNSUPPORTED",
        )
    return f"(FOR v IN {args[3]} SHORTEST_PATH {args[0]} TO {args[1]} {args[2]} RETURN v)"


def _compile_k_shortest_paths(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``CALL arango.k_shortest_paths(startVertex, targetVertex, edgeCollection, direction)``"""
    if len(args) != 4:
        raise CoreError(
            "arango.k_shortest_paths expects 4 arguments: "
            "(startVertex, targetVertex, edgeCollection, direction)",
            code="UNSUPPORTED",
        )
    return f"(FOR p IN {args[3]} K_SHORTEST_PATHS {args[0]} TO {args[1]} {args[2]} RETURN p)"


def register_procedure_extensions(registry: ExtensionRegistry) -> None:
    """Register all built-in arango.* procedure compilers."""
    registry.register_procedure("arango.fulltext", _compile_fulltext_search)
    registry.register_procedure("arango.near", _compile_near)
    registry.register_procedure("arango.within", _compile_within)
    registry.register_procedure("arango.shortest_path", _compile_shortest_path)
    registry.register_procedure("arango.k_shortest_paths", _compile_k_shortest_paths)
