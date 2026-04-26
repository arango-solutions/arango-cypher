"""Shared mutable translation state for the v0 translator."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from arango_query_core import ExtensionPolicy, ExtensionRegistry, MappingResolver

_active_registry: ContextVar[ExtensionRegistry | None] = ContextVar(
    "_active_registry",
    default=None,
)
_active_resolver: ContextVar[MappingResolver | None] = ContextVar(
    "_active_resolver",
    default=None,
)
_active_warnings: ContextVar[list[str]] = ContextVar(
    "_active_warnings",
    default=[],  # noqa: B039  # always .set() before .get() in translate_v0
)
_active_path_vars: ContextVar[dict[str, tuple[list[str], list[str]]]] = ContextVar(
    "_active_path_vars",
    default={},  # noqa: B039  # always .set() before .get() in translate_v0
)


@dataclass
class _HopMeta:
    """Pre-processed metadata for a single hop in a relationship chain."""

    v_var: str
    v_trav: str
    v_labels: list[str]
    v_primary: str | None
    v_map: dict[str, Any] | None
    v_bound: bool
    v_prop_filters: list[str]
    rel_type: str | None
    rel_var: str
    rel_range: tuple[int, int]
    rel_named: bool
    r_prop_filters: list[str]
    direction: str
    r_map: dict[str, Any]
    r_style: str
    edge_collection: str
    edge_key: str
    r_type_field: str | None
    r_type_value: str | None


@dataclass(frozen=True)
class TranslateOptions:
    extensions: ExtensionPolicy = ExtensionPolicy(enabled=False)
    registry: ExtensionRegistry | None = None
