"""HTTP-shaped mapping helper for ``arango_cypher.service``.

Single-function module; kept separate from :mod:`.models` so the route
modules can depend on the (lighter, no-Pydantic) helper without pulling
in the full request/response surface, and so ``scripts/benchmark_translate``
can keep importing :func:`_mapping_from_dict` via the package init at
its historical name.
"""

from __future__ import annotations

from typing import Any

from arango_query_core import MappingBundle, MappingSource, mapping_from_wire_dict


def _mapping_from_dict(d: dict[str, Any] | None) -> MappingBundle | None:
    """Thin wrapper around :func:`arango_query_core.mapping_from_wire_dict`.

    Kept as a module-private alias because ``scripts/benchmark_translate``
    imports this name. The wrapper adds the ``None``-short-circuit and
    the ``MappingSource`` tag that identify the bundle as an HTTP-posted
    mapping in downstream logs.
    """
    if d is None:
        return None
    return mapping_from_wire_dict(
        d,
        source=MappingSource(kind="explicit", notes="supplied via HTTP"),
    )
