"""Built-in arango.* extension compilers for the Cypher-to-AQL translator."""

from __future__ import annotations

from arango_query_core import ExtensionRegistry

from .document import register_document_extensions
from .geo import register_geo_extensions
from .procedures import register_procedure_extensions
from .search import register_search_extensions
from .vector import register_vector_extensions

__all__ = [
    "register_all_extensions",
    "register_document_extensions",
    "register_geo_extensions",
    "register_procedure_extensions",
    "register_search_extensions",
    "register_vector_extensions",
]


def register_all_extensions(registry: ExtensionRegistry) -> None:
    """Register all built-in arango.* extension compilers (search + vector + geo + document + procedures)."""
    register_search_extensions(registry)
    register_vector_extensions(registry)
    register_geo_extensions(registry)
    register_document_extensions(registry)
    register_procedure_extensions(registry)
