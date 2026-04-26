"""Name normalisation and fresh-name helpers for translator codegen."""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import CoreError


def _pick_fresh_var(name: str, *, forbidden_vars: set[str]) -> str:
    if name not in forbidden_vars:
        forbidden_vars.add(name)
        return name
    i = 1
    while f"{name}_{i}" in forbidden_vars:
        i += 1
    out = f"{name}_{i}"
    forbidden_vars.add(out)
    return out


def _pick_bind_key(base: str, bind_vars: dict[str, Any]) -> str:
    if base not in bind_vars:
        return base
    i = 2
    while f"{base}{i}" in bind_vars:
        i += 1
    return f"{base}{i}"


def _aql_collection_ref(bind_key: str) -> str:
    if not bind_key.startswith("@"):
        raise CoreError("Collection bind key must start with '@'", code="INTERNAL_ERROR")
    return f"@@{bind_key[1:]}"


def _strip_label_backticks(name: str) -> str:
    """Strip a single pair of enclosing backticks from an escaped label."""
    if len(name) >= 2 and name.startswith("`") and name.endswith("`"):
        return name[1:-1]
    return name


def _rewrite_vars(text: str, var_env: dict[str, str]) -> str:
    """Best-effort variable rewrite for post-WITH scopes."""
    if not text or not var_env:
        return text
    out = text
    for k in sorted(var_env.keys(), key=len, reverse=True):
        v = var_env[k]
        if k == v:
            continue
        out = re.sub(rf"\b{re.escape(k)}\b", v, out)
    return out
