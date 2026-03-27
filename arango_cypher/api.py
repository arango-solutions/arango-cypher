from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arango_query_core import AqlQuery, CoreError, ExtensionPolicy, MappingBundle

from .translate_v0 import TranslateOptions, translate_v0


@dataclass(frozen=True)
class TranspiledQuery:
    aql: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]]
    debug: dict[str, Any] | None = None

    def to_aql_query(self) -> AqlQuery:
        return AqlQuery(text=self.aql, bind_vars=self.bind_vars, debug=self.debug)


def translate(
    cypher: str,
    *,
    mapping: MappingBundle | None = None,
    extensions: ExtensionPolicy | None = None,
    params: dict[str, Any] | None = None,
) -> TranspiledQuery:
    """
    v0.0 scaffold.

    For now we return a clear "not implemented" error in a structured way
    so the test harness can be wired end-to-end before the translator lands.
    """
    if not isinstance(cypher, str) or not cypher.strip():
        raise CoreError("cypher must be a non-empty string", code="INVALID_ARGUMENT")

    if mapping is None:
        raise CoreError("mapping is required", code="INVALID_ARGUMENT")

    options = TranslateOptions(extensions=(extensions or ExtensionPolicy(enabled=False)))
    q = translate_v0(cypher, mapping=mapping, params=params, options=options)

    return TranspiledQuery(aql=q.text, bind_vars=q.bind_vars, warnings=[], debug=q.debug)

