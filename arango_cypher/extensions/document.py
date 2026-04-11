"""Document-centric arango.* extension compilers for hierarchical JSON access.

These extensions expose ArangoDB's native document and array functions
through the ``arango.*`` namespace, enabling Cypher queries to work with
nested/hierarchical JSON structures that go beyond simple dot-path access.
"""

from __future__ import annotations

from typing import Any

from arango_query_core import CoreError, ExtensionRegistry


def _compile_attributes(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.attributes(doc)`` → ``ATTRIBUTES(doc)``

    Returns the top-level attribute names of a document as an array.
    Optional second arg removes internal attributes.
    """
    if not args or len(args) > 3:
        raise CoreError(
            "arango.attributes expects 1-3 arguments: (doc[, removeInternal, sort])",
            code="UNSUPPORTED",
        )
    return f"ATTRIBUTES({', '.join(args)})"


def _compile_has(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.has(doc, attributeName)`` → ``HAS(doc, attributeName)``

    Returns true if the document has the specified attribute.
    """
    if len(args) != 2:
        raise CoreError(
            "arango.has expects 2 arguments: (doc, attributeName)",
            code="UNSUPPORTED",
        )
    return f"HAS({args[0]}, {args[1]})"


def _compile_merge(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.merge(doc1, doc2, ...)`` → ``MERGE(doc1, doc2, ...)``

    Merges multiple documents into one.
    """
    if len(args) < 2:
        raise CoreError(
            "arango.merge expects at least 2 arguments",
            code="UNSUPPORTED",
        )
    return f"MERGE({', '.join(args)})"


def _compile_unset(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.unset(doc, attr1, attr2, ...)`` → ``UNSET(doc, attr1, ...)``

    Returns a copy of the document with the specified attributes removed.
    """
    if len(args) < 2:
        raise CoreError(
            "arango.unset expects at least 2 arguments: (doc, attr1[, ...])",
            code="UNSUPPORTED",
        )
    return f"UNSET({', '.join(args)})"


def _compile_keep(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.keep(doc, attr1, attr2, ...)`` → ``KEEP(doc, attr1, ...)``

    Returns a copy of the document with only the specified attributes kept.
    """
    if len(args) < 2:
        raise CoreError(
            "arango.keep expects at least 2 arguments: (doc, attr1[, ...])",
            code="UNSUPPORTED",
        )
    return f"KEEP({', '.join(args)})"


def _compile_zip(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.zip(keys, values)`` → ``ZIP(keys, values)``

    Creates a document from arrays of keys and values.
    """
    if len(args) != 2:
        raise CoreError(
            "arango.zip expects 2 arguments: (keys, values)",
            code="UNSUPPORTED",
        )
    return f"ZIP({args[0]}, {args[1]})"


def _compile_value(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.value(doc, path)`` → ``VALUE(doc, path)``

    Returns the value of a document attribute by path (string or array).
    Useful for dynamic attribute access.
    """
    if len(args) != 2:
        raise CoreError(
            "arango.value expects 2 arguments: (doc, path)",
            code="UNSUPPORTED",
        )
    return f"VALUE({args[0]}, {args[1]})"


def _compile_values(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.values(doc)`` → ``VALUES(doc)``

    Returns the values of a document as an array.
    Optional second arg removes internal attributes.
    """
    if not args or len(args) > 2:
        raise CoreError(
            "arango.values expects 1-2 arguments: (doc[, removeInternal])",
            code="UNSUPPORTED",
        )
    return f"VALUES({', '.join(args)})"


def _compile_flatten(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.flatten(array)`` → ``FLATTEN(array)``

    Flattens a nested array. Optional depth argument.
    """
    if not args or len(args) > 2:
        raise CoreError(
            "arango.flatten expects 1-2 arguments: (array[, depth])",
            code="UNSUPPORTED",
        )
    return f"FLATTEN({', '.join(args)})"


def _compile_parse_identifier(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.parse_identifier(id)`` → ``PARSE_IDENTIFIER(id)``

    Splits a document ``_id`` into collection name and key.
    """
    if len(args) != 1:
        raise CoreError(
            "arango.parse_identifier expects 1 argument: (documentId)",
            code="UNSUPPORTED",
        )
    return f"PARSE_IDENTIFIER({args[0]})"


def _compile_document(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.document(id)`` → ``DOCUMENT(id)``

    Looks up a document by ``_id`` or by collection + key.
    Useful for foreign-key-like references without edge collections.
    """
    if not args or len(args) > 2:
        raise CoreError(
            "arango.document expects 1-2 arguments: (id) or (collection, key)",
            code="UNSUPPORTED",
        )
    return f"DOCUMENT({', '.join(args)})"


def register_document_extensions(registry: ExtensionRegistry) -> None:
    """Register all document-centric arango.* extension function compilers."""
    registry.register_function("arango.attributes", _compile_attributes)
    registry.register_function("arango.has", _compile_has)
    registry.register_function("arango.merge", _compile_merge)
    registry.register_function("arango.unset", _compile_unset)
    registry.register_function("arango.keep", _compile_keep)
    registry.register_function("arango.zip", _compile_zip)
    registry.register_function("arango.value", _compile_value)
    registry.register_function("arango.values", _compile_values)
    registry.register_function("arango.flatten", _compile_flatten)
    registry.register_function("arango.parse_identifier", _compile_parse_identifier)
    registry.register_function("arango.document", _compile_document)
