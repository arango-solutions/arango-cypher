"""Compatibility shim for the v0 Cypher-to-AQL translator.

The audit-v2 #8 split moved the implementation into the private
``arango_cypher._translate_v0`` package. Keep this module as the stable import
surface for existing callers and tests that import ``arango_cypher.translate_v0``
or selected helper internals from it.
"""

from __future__ import annotations

from ._translate_v0 import (
    TranslateOptions,
    _aql_collection_ref,
    _compile_type_of_relationship,
    _pick_bind_key,
    _pick_fresh_var,
    _strip_label_backticks,
    translate_v0,
)

__all__ = [
    "TranslateOptions",
    "_aql_collection_ref",
    "_compile_type_of_relationship",
    "_pick_bind_key",
    "_pick_fresh_var",
    "_strip_label_backticks",
    "translate_v0",
]
