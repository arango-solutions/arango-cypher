"""ArangoSearch extension compilers (arango.bm25, arango.tfidf, arango.analyzer,
and the WP-S3 fuzzy/text family: ngram_match, levenshtein_match, phrase, …)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from arango_query_core import CoreError, ExtensionRegistry


def _passthrough(
    cypher_name: str,
    aql_name: str,
    *,
    min_args: int,
    max_args: int | None,
) -> Callable[[list[str], dict[str, Any]], str]:
    """Build a compiler that renders ``arango.<name>(...)`` as ``AQL_NAME(...)``
    verbatim, validating arity. Used for AQL fulltext/ArangoSearch functions
    whose Cypher and AQL signatures are identical (WP-S3)."""

    def _compile(args: list[str], bind_vars: dict[str, Any]) -> str:  # noqa: ARG001
        n = len(args)
        if n < min_args or (max_args is not None and n > max_args):
            if max_args is None:
                expected = f"at least {min_args}"
            elif min_args == max_args:
                expected = f"exactly {min_args}"
            else:
                expected = f"{min_args}-{max_args}"
            raise CoreError(
                f"arango.{cypher_name} expects {expected} argument(s)",
                code="UNSUPPORTED",
            )
        return f"{aql_name}({', '.join(args)})"

    return _compile


# WP-S3: portable fuzzy/text matching. Each entry maps an ``arango.*`` Cypher
# function to its identically-signatured AQL function with an arity range.
# (NGRAM_MATCH / PHRASE / BOOST / MIN_MATCH require an ArangoSearch SEARCH
# context at runtime; the transpiler renders them — pairs with the resolver's
# IndexAdvisory which offers to create the backing view/inverted index.)
_FUZZY_FUNCTIONS: tuple[tuple[str, str, int, int | None], ...] = (
    ("like", "LIKE", 2, 3),
    ("starts_with", "STARTS_WITH", 2, None),
    ("in_range", "IN_RANGE", 5, 5),
    ("levenshtein_distance", "LEVENSHTEIN_DISTANCE", 2, 2),
    ("levenshtein_match", "LEVENSHTEIN_MATCH", 3, 5),
    ("ngram_match", "NGRAM_MATCH", 3, 4),
    ("ngram_similarity", "NGRAM_SIMILARITY", 3, 3),
    ("phrase", "PHRASE", 2, None),
    ("boost", "BOOST", 2, 2),
    ("min_match", "MIN_MATCH", 2, None),
    ("tokens", "TOKENS", 1, 2),
    ("soundex", "SOUNDEX", 1, 1),
    ("regex_test", "REGEX_TEST", 2, 3),
    ("regex_matches", "REGEX_MATCHES", 2, 3),
    ("regex_replace", "REGEX_REPLACE", 3, 4),
)


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
    # WP-S3 fuzzy/text family.
    for cypher_name, aql_name, min_args, max_args in _FUZZY_FUNCTIONS:
        registry.register_function(
            f"arango.{cypher_name}",
            _passthrough(cypher_name, aql_name, min_args=min_args, max_args=max_args),
        )
