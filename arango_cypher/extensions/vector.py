"""ArangoDB vector search extension compilers (arango.cosine_similarity, arango.l2_distance, etc.)."""

from __future__ import annotations

from typing import Any

from arango_query_core import CoreError, ExtensionRegistry


def _compile_cosine_similarity(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.cosine_similarity(v1, v2)`` → ``COSINE_SIMILARITY(v1, v2)``."""
    if len(args) != 2:
        raise CoreError(
            "arango.cosine_similarity expects 2 arguments: (vector1, vector2)",
            code="UNSUPPORTED",
        )
    return f"COSINE_SIMILARITY({', '.join(args)})"


def _compile_l2_distance(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.l2_distance(v1, v2)`` → ``L2_DISTANCE(v1, v2)``."""
    if len(args) != 2:
        raise CoreError(
            "arango.l2_distance expects 2 arguments: (vector1, vector2)",
            code="UNSUPPORTED",
        )
    return f"L2_DISTANCE({', '.join(args)})"


def _compile_approx_near_cosine(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.approx_near_cosine(v1, v2[, opts])`` → ``APPROX_NEAR_COSINE(v1, v2[, opts])``."""
    if len(args) < 2 or len(args) > 3:
        raise CoreError(
            "arango.approx_near_cosine expects 2-3 arguments: (vector1, vector2[, options])",
            code="UNSUPPORTED",
        )
    return f"APPROX_NEAR_COSINE({', '.join(args)})"


def _compile_approx_near_l2(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.approx_near_l2(v1, v2[, opts])`` → ``APPROX_NEAR_L2(v1, v2[, opts])``."""
    if len(args) < 2 or len(args) > 3:
        raise CoreError(
            "arango.approx_near_l2 expects 2-3 arguments: (vector1, vector2[, options])",
            code="UNSUPPORTED",
        )
    return f"APPROX_NEAR_L2({', '.join(args)})"


def register_vector_extensions(registry: ExtensionRegistry) -> None:
    """Register ArangoDB vector search extension function compilers."""
    registry.register_function("arango.cosine_similarity", _compile_cosine_similarity)
    registry.register_function("arango.l2_distance", _compile_l2_distance)
    registry.register_function("arango.approx_near_cosine", _compile_approx_near_cosine)
    registry.register_function("arango.approx_near_l2", _compile_approx_near_l2)
