"""Literal and type-expression helpers for the v0 translator."""

from __future__ import annotations

from typing import Any

from arango_query_core import CoreError


def _aql_string_literal(value: str) -> str:
    """Return a minimally escaped AQL double-quoted string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _compile_type_of_relationship(
    rel_type: str, rel_var: str, rel_style: str | None, bind_vars: dict[str, Any]
) -> str:
    if rel_style == "GENERIC_WITH_TYPE":
        if "relTypeField" not in bind_vars:
            raise CoreError("relTypeField missing for GENERIC_WITH_TYPE", code="INVALID_MAPPING")
        return f"{rel_var}[@relTypeField]"
    return _aql_string_literal(rel_type)
