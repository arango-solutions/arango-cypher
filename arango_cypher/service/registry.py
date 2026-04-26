"""Process-wide :class:`ExtensionRegistry` for ``arango_cypher.service``.

Built once at package import time so every translate / execute / explain
request reuses the same registry instance. Kept in a tiny dedicated
module (rather than co-located with :mod:`.app` or :mod:`.security`)
because every route file that runs the transpiler reads it, and the
translate / execute / explain endpoints would otherwise have to import
the heavyweight :func:`register_all_extensions` indirectly through the
package init's circular wiring.
"""

from __future__ import annotations

from arango_query_core import ExtensionPolicy, ExtensionRegistry

from ..extensions import register_all_extensions


def _build_registry() -> ExtensionRegistry:
    reg = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_all_extensions(reg)
    return reg


_default_registry = _build_registry()
