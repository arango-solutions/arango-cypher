from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener

from arango_query_core import CoreError

from ._antlr.CypherLexer import CypherLexer
from ._antlr.CypherParser import CypherParser


@dataclass(frozen=True)
class ParseResult:
    tree: Any


class _RaisingErrorListener(ErrorListener):
    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):  # noqa: N802
        raise CoreError(f"Cypher syntax error at {line}:{column}: {msg}", code="CYPHER_SYNTAX_ERROR")


def parse_cypher(cypher: str) -> ParseResult:
    if not isinstance(cypher, str) or not cypher.strip():
        raise CoreError("cypher must be a non-empty string", code="INVALID_ARGUMENT")

    stream = InputStream(cypher)
    lexer = CypherLexer(stream)
    lexer.removeErrorListeners()
    lexer.addErrorListener(_RaisingErrorListener())

    tokens = CommonTokenStream(lexer)
    parser = CypherParser(tokens)
    parser.removeErrorListeners()
    parser.addErrorListener(_RaisingErrorListener())

    tree = parser.oC_Cypher()
    return ParseResult(tree=tree)

