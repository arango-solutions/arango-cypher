"""Private implementation package for the v0 Cypher-to-AQL translator.

The public/back-compat import surface remains ``arango_cypher.translate_v0``.
This package exists only so the former 5k-line module can be split into
focused implementation modules without forcing downstream import changes.
"""

from .core import (
    TranslateOptions,
    _aql_collection_ref,
    _pick_bind_key,
    _pick_fresh_var,
    _strip_label_backticks,
    translate_v0,
)
from .literals import _compile_type_of_relationship

__all__ = [
    "TranslateOptions",
    "_aql_collection_ref",
    "_compile_type_of_relationship",
    "_pick_bind_key",
    "_pick_fresh_var",
    "_strip_label_backticks",
    "translate_v0",
]
