"""ArangoSearch extension compilers (arango.bm25, arango.tfidf, arango.analyzer)."""

from __future__ import annotations

from typing import Any

from arango_query_core import CoreError, ExtensionRegistry


def _compile_bm25(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.bm25(doc)`` → ``BM25(doc)`` or ``arango.bm25(doc, k, b)`` → ``BM25(doc, k, b)``."""
    if not args or len(args) > 3:
        raise CoreError("arango.bm25 expects 1-3 arguments: (doc[, k, b])", code="UNSUPPORTED")
    return f"BM25({', '.join(args)})"


def _compile_tfidf(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.tfidf(doc)`` → ``TFIDF(doc)`` or ``arango.tfidf(doc, normalize)`` → ``TFIDF(doc, normalize)``."""
    if not args or len(args) > 2:
        raise CoreError("arango.tfidf expects 1-2 arguments: (doc[, normalize])", code="UNSUPPORTED")
    return f"TFIDF({', '.join(args)})"


def _compile_analyzer(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.analyzer(expr, analyzerName)`` → ``ANALYZER(expr, analyzerName)``."""
    if len(args) != 2:
        raise CoreError("arango.analyzer expects 2 arguments: (expr, analyzerName)", code="UNSUPPORTED")
    return f"ANALYZER({', '.join(args)})"


def register_search_extensions(registry: ExtensionRegistry) -> None:
    """Register all ArangoSearch extension function compilers."""
    registry.register_function("arango.bm25", _compile_bm25)
    registry.register_function("arango.tfidf", _compile_tfidf)
    registry.register_function("arango.analyzer", _compile_analyzer)
