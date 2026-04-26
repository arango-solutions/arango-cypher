"""Index, VCI, and warning helpers for MATCH/traversal compilation."""

from __future__ import annotations

from arango_query_core import IndexInfo, MappingResolver

from .state import _active_warnings, _HopMeta


def _warn_multi_label_collection(labels: list[str], primary: str) -> None:
    """Emit a warning when multi-label matching is used with COLLECTION style."""
    warnings = _active_warnings.get()
    others = [lb for lb in labels if lb != primary]
    msg = (
        f"Multi-label pattern {labels} uses COLLECTION-style mapping. "
        f"Using primary label '{primary}'; additional labels {others} "
        f"cannot be verified (documents exist in exactly one collection)."
    )
    if msg not in warnings:
        warnings.append(msg)


def _warn_missing_vci(resolver: MappingResolver, rel_type: str, r_map: dict) -> None:
    """Emit a warning if a GENERIC_WITH_TYPE relationship lacks VCI."""
    if r_map.get("style") != "GENERIC_WITH_TYPE":
        return
    if resolver.has_vci(rel_type):
        return
    edge_coll = r_map.get("edgeCollectionName", "?")
    type_field = r_map.get("typeField", "type")
    warnings = _active_warnings.get()
    msg = (
        f"Edge collection '{edge_coll}' uses GENERIC_WITH_TYPE for '{rel_type}' "
        f"but has no VCI on field '{type_field}'. "
        f"Traversal performance may be degraded."
    )
    if msg not in warnings:
        warnings.append(msg)


def _build_vci_options(
    hops: list[_HopMeta],
    resolver: MappingResolver,
) -> str | None:
    """Build an OPTIONS { indexHint: ... } clause for traversal VCI hints."""
    hints: dict[str, dict[str, str]] = {}
    for h in hops:
        if h.r_style != "GENERIC_WITH_TYPE" or h.rel_type is None:
            continue
        indexes = resolver.resolve_indexes(h.rel_type)
        vci_indexes = [idx for idx in indexes if idx.vci and idx.name]
        if not vci_indexes:
            continue
        edge_coll = h.r_map.get("edgeCollectionName") or h.r_map.get("collectionName", "")
        if not edge_coll:
            continue
        direction_key = h.direction.lower() if h.direction in ("OUTBOUND", "INBOUND") else "outbound"
        idx_name = vci_indexes[0].name
        hints.setdefault(edge_coll, {})[direction_key] = idx_name

    if not hints:
        return None

    inner_parts: list[str] = []
    for coll, dirs in hints.items():
        dir_parts = ", ".join(f'"{d}": {{"base": "{name}"}}' for d, name in dirs.items())
        inner_parts.append(f'"{coll}": {{{dir_parts}}}')
    return "OPTIONS {indexHint: {" + ", ".join(inner_parts) + "}}"


def _build_collection_index_hint(
    label: str,
    prop_filters: list[str],
    resolver: MappingResolver,
) -> str | None:
    """Build an ``OPTIONS {indexHint: "name"}`` for a FOR-collection loop."""
    if not label or not prop_filters:
        return None

    indexes = resolver.resolve_indexes(label)
    props = resolver.resolve_properties(label)

    filtered_fields: set[str] = set()
    for pf in prop_filters:
        for name, info in props.items():
            if f".{name} " in pf or f".{name})" in pf or f".{info.field} " in pf or f".{info.field})" in pf:
                filtered_fields.add(info.field)

    if not filtered_fields:
        return None

    best_idx: IndexInfo | None = None
    best_overlap = 0
    for idx in indexes:
        if not idx.name or idx.vci:
            continue
        overlap = len(set(idx.fields) & filtered_fields)
        if overlap > best_overlap:
            best_overlap = overlap
            best_idx = idx

    if best_idx:
        return f'OPTIONS {{indexHint: "{best_idx.name}", forceIndexHint: false}}'

    indexed_fields = [f for f in filtered_fields if any(p.field == f and p.indexed for p in props.values())]
    if indexed_fields and indexes:
        warnings = _active_warnings.get()
        msg = (
            f"Filtered field(s) {indexed_fields} on '{label}' are marked indexed "
            f"but no named index found in the mapping — the ArangoDB optimiser "
            f"may still select an appropriate index automatically."
        )
        if msg not in warnings:
            warnings.append(msg)

    return None
