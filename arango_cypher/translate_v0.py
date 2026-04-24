from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from arango_query_core import (
    AqlQuery,
    CoreError,
    ExtensionPolicy,
    ExtensionRegistry,
    IndexInfo,
    MappingBundle,
    MappingResolver,
)

from ._antlr.CypherParser import CypherParser
from .parser import parse_cypher

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
    default=[],  # noqa: B039  # always .set() before .get() in _run_translation
)
_active_path_vars: ContextVar[dict[str, tuple[list[str], list[str]]]] = ContextVar(
    "_active_path_vars",
    default={},  # noqa: B039  # always .set() before .get() in _run_translation
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
    """Build an OPTIONS { indexHint: ... } clause for traversal VCI hints.

    Returns the OPTIONS string (e.g. ``OPTIONS { indexHint: { ... } }``)
    or ``None`` if no VCI indexes apply.
    """
    hints: dict[str, dict[str, str]] = {}  # edgeColl -> {direction -> indexName}
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
    """Build an ``OPTIONS {indexHint: "name"}`` for a FOR-collection loop.

    Checks whether any property referenced in *prop_filters* has a matching
    index (via ``resolve_indexes``).  Returns the OPTIONS string when a named
    index covers at least one filtered field, or ``None`` otherwise.  When
    ``PropertyInfo.indexed`` is set but no named index exists, a warning is
    emitted so the user knows the optimiser may still pick an index.
    """
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
    # Only warn when the mapping *does* carry index metadata for this entity
    # but none of the named indexes happens to cover the filtered fields.
    # When the entity has zero ``IndexInfo`` entries (a common shape from the
    # schema-analyzer export, which sets ``PropertyInfo.indexed=true`` from
    # ArangoDB's catalog without round-tripping the named-index list) the
    # warning has no actionable advice — every indexed field would trip it,
    # and the user has nothing to fix in the mapping. Suppress that case so
    # the warning bubble only fires when there's a real mapping gap.
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


@dataclass(frozen=True)
class TranslateOptions:
    extensions: ExtensionPolicy = ExtensionPolicy(enabled=False)
    registry: ExtensionRegistry | None = None


def _prepend_with_collections(result: AqlQuery, resolver: MappingResolver) -> AqlQuery:
    """Prepend ``WITH coll1, coll2, ...`` listing all vertex collections.

    ArangoDB requires a leading ``WITH`` declaration of all vertex collections
    accessed during graph traversals.  This is mandatory in cluster deployments
    and harmless in single-server mode.  Edge collections are excluded — they
    are referenced directly in the traversal syntax.
    """
    has_traversal = re.search(
        r"\b(?:OUTBOUND|INBOUND|ANY|SHORTEST_PATH|ALL_SHORTEST_PATHS)\b",
        result.text,
    )
    if not has_traversal:
        return result

    edge_collections: set[str] = set()
    rels = resolver.bundle.physical_mapping.get("relationships", {})
    if isinstance(rels, dict):
        for rmap in rels.values():
            ec = rmap.get("edgeCollectionName") or rmap.get("collectionName")
            if isinstance(ec, str) and ec:
                edge_collections.add(ec)

    vertex_collections: set[str] = set()

    # Collect from bind vars (@@collection references)
    for key, val in result.bind_vars.items():
        if key.startswith("@") and isinstance(val, str) and val:
            if val not in edge_collections:
                vertex_collections.add(val)

    # Also include entity collections from the mapping that appear as
    # traversal endpoints — these may not be in bind vars when
    # IS_SAME_COLLECTION filters are optimized away.
    entities = resolver.bundle.physical_mapping.get("entities", {})
    if isinstance(entities, dict):
        for emap in entities.values():
            coll = emap.get("collectionName")
            if isinstance(coll, str) and coll and coll not in edge_collections:
                vertex_collections.add(coll)

    if not vertex_collections:
        return result

    with_line = "WITH " + ", ".join(sorted(vertex_collections))
    return AqlQuery(
        text=with_line + "\n" + result.text,
        bind_vars=result.bind_vars,
        debug=result.debug,
        warnings=result.warnings,
    )


_INDENT = "  "
_FOR_RE = re.compile(r"^\s*FOR\b")
_BLOCK_OPEN_RE = re.compile(r"^\s*(?:FOR|LET\s+\w+\s*=\s*\()")
_TERMINAL_RE = re.compile(r"^\s*(?:RETURN|SORT|LIMIT|COLLECT)\b")
_FILTER_LET_RE = re.compile(r"^\s*(?:FILTER|LET|PRUNE)\b")


def _reindent_aql(text: str) -> str:
    """Re-indent AQL to reflect the nesting structure of FOR loops.

    Each ``FOR`` increases the indent depth by one level.  All body
    statements (``FILTER``, ``LET``, ``SORT``, ``LIMIT``, ``RETURN``,
    ``COLLECT``) sit inside the innermost ``FOR`` scope.
    """
    raw_lines = text.split("\n")
    out: list[str] = []
    depth = 0
    first_for_depth = -1

    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            out.append("")
            continue

        if stripped.startswith("WITH "):
            out.append(stripped)
            continue

        if _FOR_RE.match(stripped):
            indent = _INDENT * depth
            out.append(indent + stripped)
            if first_for_depth < 0:
                first_for_depth = depth
            depth += 1
        elif _TERMINAL_RE.match(stripped):
            # SORT/LIMIT/RETURN/COLLECT belong inside the innermost FOR scope
            out.append(_INDENT * depth + stripped)
        elif _FILTER_LET_RE.match(stripped):
            out.append(_INDENT * depth + stripped)
        else:
            out.append(_INDENT * depth + stripped)

    return "\n".join(out)


def translate_v0(
    cypher: str,
    *,
    mapping: MappingBundle,
    params: dict[str, Any] | None = None,
    options: TranslateOptions | None = None,
) -> AqlQuery:
    """
    v0 translation for a small Cypher subset:
    - Single MATCH with a single node pattern: MATCH (n:Label)
    - Optional WHERE over simple expressions
    - RETURN with projection items / aliases / DISTINCT
    - LIMIT (only if present in RETURN clause)
    """
    if not mapping:
        raise CoreError("mapping is required", code="INVALID_ARGUMENT")

    opts = options or TranslateOptions()
    registry = opts.registry
    reg_token = _active_registry.set(registry)
    resolver = MappingResolver(mapping)
    res_token = _active_resolver.set(resolver)
    warnings: list[str] = []
    warn_token = _active_warnings.set(warnings)
    path_vars: dict[str, tuple[list[str], list[str]]] = {}
    path_token = _active_path_vars.set(path_vars)

    try:
        result = _translate_v0_inner(
            cypher,
            mapping=mapping,
            params=params,
        )
        result = _prepend_with_collections(result, resolver)
        result = AqlQuery(
            text=_reindent_aql(result.text),
            bind_vars=result.bind_vars,
            debug=result.debug,
            warnings=tuple(warnings) if warnings else result.warnings,
        )
        return result
    finally:
        _active_path_vars.reset(path_token)
        _active_warnings.reset(warn_token)
        _active_resolver.reset(res_token)
        _active_registry.reset(reg_token)


def _translate_v0_inner(
    cypher: str,
    *,
    mapping: MappingBundle,
    params: dict[str, Any] | None = None,
) -> AqlQuery:
    bind_vars: dict[str, Any] = dict(params or {})
    resolver = MappingResolver(mapping)

    pr = parse_cypher(cypher)
    tree = pr.tree

    query_ctx = tree.oC_Statement().oC_Query()

    standalone_call = query_ctx.oC_StandaloneCall()
    if standalone_call is not None:
        return _translate_standalone_call(standalone_call, bind_vars=bind_vars)

    regular = query_ctx.oC_RegularQuery()
    if regular is None:
        raise CoreError("Only regular queries are supported in v0", code="UNSUPPORTED")

    union_clauses = regular.oC_Union()
    if union_clauses:
        return _translate_union(
            regular,
            resolver=resolver,
            bind_vars=bind_vars,
        )

    single_query = regular.oC_SingleQuery()
    return _translate_single_query(single_query, resolver=resolver, bind_vars=bind_vars)


def _translate_single_query(
    sq: CypherParser.OC_SingleQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate one ``oC_SingleQuery`` (single- or multi-part) into AQL."""

    mpq = sq.oC_MultiPartQuery()
    if mpq is not None:
        return _translate_multi_part_query(
            mpq,
            resolver=resolver,
            bind_vars=bind_vars,
        )

    spq = sq.oC_SinglePartQuery()
    if spq is None:
        raise CoreError(
            "Only single-part queries are supported in v0",
            code="UNSUPPORTED",
        )

    updating_clauses = spq.oC_UpdatingClause() or []
    if updating_clauses:
        create_clauses: list[CypherParser.OC_CreateContext] = []
        set_clauses: list[CypherParser.OC_SetContext] = []
        delete_clauses: list[CypherParser.OC_DeleteContext] = []
        remove_clauses: list[Any] = []
        merge_clauses: list[CypherParser.OC_MergeContext] = []
        foreach_clauses: list[CypherParser.OC_ForeachContext] = []
        for uc in updating_clauses:
            if uc.oC_Create() is not None:
                create_clauses.append(uc.oC_Create())
            elif uc.oC_Set() is not None:
                set_clauses.append(uc.oC_Set())
            elif uc.oC_Delete() is not None:
                delete_clauses.append(uc.oC_Delete())
            elif uc.oC_Remove() is not None:
                remove_clauses.append(uc.oC_Remove())
            elif uc.oC_Merge() is not None:
                merge_clauses.append(uc.oC_Merge())
            elif uc.oC_Foreach() is not None:
                foreach_clauses.append(uc.oC_Foreach())
            else:
                raise CoreError("Unsupported updating clause", code="UNSUPPORTED")

        if foreach_clauses:
            return _translate_foreach_query(
                spq,
                foreach_clauses=foreach_clauses,
                resolver=resolver,
                bind_vars=bind_vars,
            )

        if merge_clauses:
            return _translate_merge_query(
                spq,
                merge_clauses=merge_clauses,
                resolver=resolver,
                bind_vars=bind_vars,
            )

        if create_clauses and not set_clauses and not delete_clauses and not remove_clauses:
            return _translate_create_query(
                spq,
                create_clauses=create_clauses,
                resolver=resolver,
                bind_vars=bind_vars,
            )

        if set_clauses or delete_clauses or remove_clauses:
            return _translate_mutating_query(
                spq,
                set_clauses=set_clauses,
                delete_clauses=delete_clauses,
                remove_clauses=remove_clauses,
                resolver=resolver,
                bind_vars=bind_vars,
            )

    # Gather reading clauses and return.
    reading_clauses = spq.oC_ReadingClause() or []
    if not reading_clauses:
        raise CoreError("MATCH is required in v0 subset", code="UNSUPPORTED")

    mandatory_matches: list[CypherParser.OC_MatchContext] = []
    optional_matches: list[CypherParser.OC_MatchContext] = []
    pre_unwinds: list[CypherParser.OC_UnwindContext] = []
    post_unwinds: list[CypherParser.OC_UnwindContext] = []
    in_query_calls: list[CypherParser.OC_InQueryCallContext] = []
    seen_match = False
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is not None:
            seen_match = True
            if m.OPTIONAL() is not None:
                optional_matches.append(m)
            else:
                mandatory_matches.append(m)
            continue
        uw = rc.oC_Unwind()
        if uw is not None:
            if seen_match:
                post_unwinds.append(uw)
            else:
                pre_unwinds.append(uw)
            continue
        iqc = rc.oC_InQueryCall()
        if iqc is not None:
            in_query_calls.append(iqc)
    unwind_clauses = pre_unwinds + post_unwinds

    # Standalone in-query CALL (no MATCH, no UNWIND)
    if not mandatory_matches and not optional_matches and not unwind_clauses:
        if in_query_calls:
            return _translate_standalone_in_query_call(
                in_query_calls,
                spq=spq,
                bind_vars=bind_vars,
            )
        raise CoreError("MATCH is required in v0 subset", code="UNSUPPORTED")

    # Standalone UNWIND (no MATCH at all)
    if not mandatory_matches and not optional_matches:
        if not unwind_clauses:
            raise CoreError("MATCH is required in v0 subset", code="UNSUPPORTED")
        return _translate_standalone_unwind(
            unwind_clauses,
            spq=spq,
            bind_vars=bind_vars,
        )

    sole_optional = False
    if not mandatory_matches:
        mandatory_matches.append(optional_matches.pop(0))
        sole_optional = True

    result = _translate_match_body(
        mandatory_matches,
        spq=spq,
        optional_matches=optional_matches,
        resolver=resolver,
        bind_vars=bind_vars,
    )

    if pre_unwinds or post_unwinds:
        result_lines = result.text.splitlines()
        final_lines: list[str] = []
        if pre_unwinds:
            for uw in pre_unwinds:
                expr = _compile_expression(uw.oC_Expression(), bind_vars)
                var = uw.oC_Variable().getText().strip()
                final_lines.append(f"FOR {var} IN {expr}")
            for ln in result_lines:
                final_lines.append(f"  {ln}")
        else:
            return_line = result_lines[-1] if result_lines else ""
            body_lines = result_lines[:-1]
            final_lines.extend(body_lines)
            for uw in post_unwinds:
                expr = _compile_expression(uw.oC_Expression(), bind_vars)
                var = uw.oC_Variable().getText().strip()
                final_lines.append(f"  FOR {var} IN {expr}")
            final_lines.append(return_line)
        result = AqlQuery(
            text="\n".join(final_lines),
            bind_vars=result.bind_vars,
        )

    if in_query_calls:
        result = _inject_in_query_calls(result, in_query_calls, bind_vars)

    if sole_optional:
        return _wrap_optional_match(result, spq, bind_vars)
    return result


def _wrap_optional_match(
    inner: AqlQuery,
    spq: CypherParser.OC_SinglePartQueryContext,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Wrap an inner AQL query with OPTIONAL MATCH null-fallback semantics.

    AQL pattern::

        LET _rows = ( <inner AQL> )
        FOR _r IN (LENGTH(_rows) > 0 ? _rows : [<null-row>])
          RETURN _r
    """
    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    proj = ret.oC_ProjectionBody()
    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem() or []

    if len(items) == 1 and items[0].oC_Variable() is None:
        null_row = "null"
    else:
        keys = []
        for it in items:
            alias = it.oC_Variable()
            if alias is not None:
                keys.append(alias.getText().strip())
            else:
                keys.append(_infer_key(_compile_expression(it.oC_Expression(), {})))
        null_row = "{" + ", ".join(f"{k}: null" for k in keys) + "}"

    indented = "\n".join(f"  {ln}" for ln in inner.text.splitlines())
    aql = f"LET _rows = (\n{indented}\n)\nFOR _r IN (LENGTH(_rows) > 0 ? _rows : [{null_row}])\n  RETURN _r"
    return AqlQuery(text=aql, bind_vars=bind_vars)


def _translate_standalone_unwind(
    unwind_clauses: list[CypherParser.OC_UnwindContext],
    *,
    spq: CypherParser.OC_SinglePartQueryContext,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate one or more UNWIND clauses without any MATCH."""
    lines: list[str] = []
    for uw in unwind_clauses:
        expr = _compile_expression(uw.oC_Expression(), bind_vars)
        var = uw.oC_Variable().getText().strip()
        lines.append(f"FOR {var} IN {expr}")

    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    _append_return(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars)
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _compile_unwind_lines(
    unwind_clauses: list[CypherParser.OC_UnwindContext],
    bind_vars: dict[str, Any],
) -> list[str]:
    """Compile UNWIND clauses into ``FOR var IN expr`` lines."""
    lines: list[str] = []
    for uw in unwind_clauses:
        expr = _compile_expression(uw.oC_Expression(), bind_vars)
        var = uw.oC_Variable().getText().strip()
        lines.append(f"FOR {var} IN {expr}")
    return lines


def _extract_procedure_name(
    proc_ctx: CypherParser.OC_ExplicitProcedureInvocationContext
    | CypherParser.OC_ImplicitProcedureInvocationContext,
) -> str:
    """Extract the fully-qualified procedure name (e.g. ``arango.search``)."""
    return proc_ctx.oC_ProcedureName().getText().strip()


def _compile_call_aql(
    proc_name: str,
    args: list[str],
    yield_vars: list[tuple[str, str | None]],
    bind_vars: dict[str, Any],
) -> list[str]:
    """Compile a CALL into AQL ``FOR`` / ``LET`` lines.

    Parameters
    ----------
    proc_name:
        Fully-qualified procedure name (e.g. ``arango.search``).
    args:
        Already-compiled AQL argument expressions.
    yield_vars:
        List of ``(variable, procedure_result_field_or_None)`` from YIELD.
    bind_vars:
        Mutable bind-var dict, updated by procedure compilers.

    Returns
    -------
    list[str]
        AQL lines implementing the CALL.
    """
    proc_norm = proc_name.lower()

    if proc_norm.startswith("arango."):
        registry = _active_registry.get()
        if registry is None:
            raise CoreError(
                f"arango.* procedure '{proc_name}' requires a registry "
                f"(pass TranslateOptions(registry=...) to translate)",
                code="EXTENSIONS_DISABLED",
            )
        aql_expr = registry.compile_procedure(proc_norm, args, bind_vars)
    else:
        raise CoreError(
            f"Unsupported procedure in v0: {proc_name}",
            code="UNSUPPORTED",
        )

    lines: list[str] = []
    if len(yield_vars) == 1:
        var_name = yield_vars[0][0]
        lines.append(f"FOR {var_name} IN {aql_expr}")
    else:
        lines.append(f"FOR _call_row IN {aql_expr}")
        for var_name, result_field in yield_vars:
            key = result_field or var_name
            lines.append(f"  LET {var_name} = _call_row.{key}")
    return lines


def _extract_yield_vars(
    yield_items_ctx: CypherParser.OC_YieldItemsContext | None,
) -> list[tuple[str, str | None]]:
    """Return ``[(variable, procedureResultField|None), ...]`` from a YIELD clause."""
    if yield_items_ctx is None:
        return []
    items = yield_items_ctx.oC_YieldItem() or []
    result: list[tuple[str, str | None]] = []
    for item in items:
        var = item.oC_Variable().getText().strip()
        prf = item.oC_ProcedureResultField()
        field_name = prf.getText().strip() if prf else None
        result.append((var, field_name))
    return result


def _translate_standalone_call(
    sc: CypherParser.OC_StandaloneCallContext,
    *,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate a top-level ``CALL proc() YIELD ...`` (no RETURN)."""
    expl = sc.oC_ExplicitProcedureInvocation()
    impl = sc.oC_ImplicitProcedureInvocation()

    if expl is not None:
        proc_name = _extract_procedure_name(expl)
        raw_args = expl.oC_Expression() or []
        compiled_args = [_compile_expression(a, bind_vars) for a in raw_args]
    elif impl is not None:
        proc_name = _extract_procedure_name(impl)
        compiled_args = []
    else:
        raise CoreError("Invalid CALL syntax", code="UNSUPPORTED")

    yield_vars = _extract_yield_vars(sc.oC_YieldItems())

    lines = _compile_call_aql(proc_name, compiled_args, yield_vars, bind_vars)

    if yield_vars:
        if len(yield_vars) == 1:
            lines.append(f"  RETURN {yield_vars[0][0]}")
        else:
            parts = ", ".join(f"{v}: {v}" for v, _ in yield_vars)
            lines.append(f"  RETURN {{{parts}}}")
    else:
        lines.append("  RETURN _call_row")

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_standalone_in_query_call(
    calls: list[CypherParser.OC_InQueryCallContext],
    *,
    spq: CypherParser.OC_SinglePartQueryContext,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate in-query CALL(s) that appear without MATCH or UNWIND."""
    lines: list[str] = []
    for iqc in calls:
        expl = iqc.oC_ExplicitProcedureInvocation()
        if expl is None:
            raise CoreError(
                "Only explicit procedure invocations (with parentheses) are supported for in-query CALL",
                code="UNSUPPORTED",
            )
        proc_name = _extract_procedure_name(expl)
        raw_args = expl.oC_Expression() or []
        compiled_args = [_compile_expression(a, bind_vars) for a in raw_args]
        yield_vars = _extract_yield_vars(iqc.oC_YieldItems())
        call_lines = _compile_call_aql(proc_name, compiled_args, yield_vars, bind_vars)
        lines.extend(call_lines)

    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    _append_return(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars)
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _inject_in_query_calls(
    result: AqlQuery,
    calls: list[CypherParser.OC_InQueryCallContext],
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Inject in-query CALL lines into an existing AQL result, before the RETURN."""
    call_lines: list[str] = []
    for iqc in calls:
        expl = iqc.oC_ExplicitProcedureInvocation()
        if expl is None:
            raise CoreError(
                "Only explicit procedure invocations are supported for in-query CALL",
                code="UNSUPPORTED",
            )
        proc_name = _extract_procedure_name(expl)
        raw_args = expl.oC_Expression() or []
        compiled_args = [_compile_expression(a, bind_vars) for a in raw_args]
        yield_vars = _extract_yield_vars(iqc.oC_YieldItems())
        call_lines.extend(
            _compile_call_aql(proc_name, compiled_args, yield_vars, bind_vars),
        )

    result_lines = result.text.splitlines()
    return_line = result_lines[-1] if result_lines else ""
    body_lines = result_lines[:-1]
    final_lines = body_lines + [f"  {cl}" for cl in call_lines] + [return_line]
    return AqlQuery(text="\n".join(final_lines), bind_vars=result.bind_vars)


def _emit_single_hop(
    h: _HopMeta,
    *,
    current_var: str,
    lines: list[str],
    bind_vars: dict[str, Any],
    rel_type_exprs: dict[str, str],
    resolver: MappingResolver,
) -> None:
    """Emit AQL for a single relationship hop (the original per-chain logic)."""
    if h.r_style == "EMBEDDED":
        embedded_path = h.r_map.get("embeddedPath")
        if not embedded_path:
            raise CoreError(
                f"EMBEDDED relationship '{h.rel_type}' must declare 'embeddedPath'",
                code="INVALID_MAPPING",
            )
        is_array = h.r_map.get("embeddedArray", False)
        if is_array:
            lines.append(f"  FOR {h.v_trav} IN TO_ARRAY({current_var}.{embedded_path})")
        else:
            lines.append(f"  LET {h.v_trav} = {current_var}.{embedded_path}")
        rel_type_exprs[h.rel_var] = _aql_string_literal(h.rel_type)
        for f in h.v_prop_filters:
            lines.append(f"    FILTER {f}")
        return

    v_filters: list[str] = []
    if h.v_map is None:
        if not h.v_bound:
            vcoll_key = _pick_bind_key("vCollection", bind_vars)
            bind_vars[vcoll_key] = _infer_unlabeled_collection(resolver)
            v_filters.append(f"IS_SAME_COLLECTION(@{vcoll_key}, {h.v_trav})")
    else:
        skip_coll_filter = (
            h.v_primary is not None
            and h.rel_type is not None
            and resolver.edge_constrains_target(h.rel_type, h.v_primary, h.direction)
        )
        if not skip_coll_filter:
            vcoll_key = _pick_bind_key("vCollection", bind_vars)
            bind_vars[vcoll_key] = h.v_map.get("collectionName")
            if not isinstance(bind_vars[vcoll_key], str) or not bind_vars[vcoll_key]:
                raise CoreError(
                    f"Invalid entity mapping collectionName for: {h.v_primary}", code="INVALID_MAPPING"
                )
            v_filters.append(f"IS_SAME_COLLECTION(@{vcoll_key}, {h.v_trav})")

    rmin, rmax = h.rel_range
    trav_line = f"  FOR {h.v_trav}, {h.rel_var} IN {rmin}..{rmax} {h.direction} {current_var} {_aql_collection_ref(h.edge_key)}"
    vci_opts = _build_vci_options([h], resolver)
    if vci_opts:
        trav_line += f" {vci_opts}"
    lines.append(trav_line)

    if h.v_bound:
        lines.append(f"    FILTER {h.v_trav}._id == {h.v_var}._id")

    if h.v_map is not None and h.v_primary is not None:
        v_style = h.v_map.get("style")
        if v_style == "LABEL":
            vtf_key = _pick_bind_key("vTypeField", bind_vars)
            vtv_key = _pick_bind_key("vTypeValue", bind_vars)
            bind_vars[vtf_key] = h.v_map.get("typeField")
            bind_vars[vtv_key] = h.v_map.get("typeValue")
            v_filters.append(f"{h.v_trav}[@{vtf_key}] == @{vtv_key}")
            v_filters.extend(_extra_label_filters(h.v_trav, h.v_labels, h.v_primary))
        elif v_style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {v_style}", code="INVALID_MAPPING")
        elif len(h.v_labels) > 1:
            _warn_multi_label_collection(h.v_labels, h.v_primary)

    r_filters: list[str] = []
    if h.r_style == "GENERIC_WITH_TYPE":
        rtf_key = _pick_bind_key("relTypeField", bind_vars)
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtf_key] = h.r_map.get("typeField")
        bind_vars[rtv_key] = h.r_map.get("typeValue")
        r_filters.append(f"{h.rel_var}[@{rtf_key}] == @{rtv_key}")
        rel_type_exprs[h.rel_var] = f"{h.rel_var}[@{rtf_key}]"
        if h.rel_type is not None:
            _warn_missing_vci(resolver, h.rel_type, h.r_map)
    elif h.r_style == "DEDICATED_COLLECTION":
        rel_type_exprs[h.rel_var] = _aql_string_literal(h.rel_type)
    else:
        raise CoreError(f"Unsupported relationship mapping style: {h.r_style}", code="INVALID_MAPPING")

    for f in v_filters + r_filters:
        lines.append(f"    FILTER {f}")
    for f in h.r_prop_filters:
        lines.append(f"    FILTER {f}")
    for f in h.v_prop_filters:
        lines.append(f"    FILTER {f}")


def _emit_merged_hops(
    hops: list[_HopMeta],
    *,
    current_var: str,
    lines: list[str],
    bind_vars: dict[str, Any],
    rel_type_exprs: dict[str, str],
    resolver: MappingResolver,
    forbidden_vars: set[str],
    path_node_vars: list[str],
    path_edge_vars: list[str],
) -> None:
    """Emit a single multi-hop AQL traversal for N consecutive mergeable hops.

    Instead of N nested ``FOR ... IN 1..1`` traversals, emits one
    ``FOR v, e, p IN N..N`` and extracts intermediate vertices/edges
    from the path variable.

    LET bindings for intermediate vertices/edges are only emitted when
    they are needed for filters (labels, properties).  Edge type
    discriminator filters reference the path directly to avoid
    unnecessary bindings.  Any remaining unused LETs are cleaned up by
    ``_eliminate_dead_lets`` at the end of query assembly.
    """
    n = len(hops)
    last = hops[-1]
    path_var = _pick_fresh_var("_path", forbidden_vars=forbidden_vars)

    edge_key = hops[0].edge_key
    for h in hops[1:]:
        if h.edge_key != edge_key and h.edge_key in bind_vars:
            del bind_vars[h.edge_key]

    trav_line = (
        f"  FOR {last.v_trav}, {last.rel_var}, {path_var}"
        f" IN {n}..{n} {last.direction} {current_var}"
        f" {_aql_collection_ref(edge_key)}"
    )
    vci_opts = _build_vci_options(hops, resolver)
    if vci_opts:
        trav_line += f" {vci_opts}"
    lines.append(trav_line)

    for idx, h in enumerate(hops[:-1]):
        vertex_idx = idx + 1
        edge_ref = f"{path_var}.edges[{idx}]"
        vertex_ref = f"{path_var}.vertices[{vertex_idx}]"

        # Only emit LET for the vertex if it has filters that need it,
        # or unconditionally so downstream WHERE/RETURN can reference it.
        # _eliminate_dead_lets will clean up if truly unused.
        lines.append(f"    LET {h.v_trav} = {vertex_ref}")
        path_node_vars.append(h.v_trav)

        # Use path expression directly for edge type filters;
        # only create a LET if the edge has property filters or was
        # explicitly named by the user.
        needs_edge_let = h.r_prop_filters or h.rel_named
        if needs_edge_let:
            lines.append(f"    LET {h.rel_var} = {edge_ref}")
            edge_filter_ref = h.rel_var
        else:
            edge_filter_ref = edge_ref
        path_edge_vars.append(h.rel_var)

        _emit_vertex_filters_for_hop(h, lines=lines, bind_vars=bind_vars, resolver=resolver)
        _emit_edge_type_filters_for_hop(
            h,
            lines=lines,
            bind_vars=bind_vars,
            rel_type_exprs=rel_type_exprs,
            resolver=resolver,
            edge_ref_override=edge_filter_ref,
        )
        for f in h.v_prop_filters:
            lines.append(f"    FILTER {f}")
        for f in h.r_prop_filters:
            lines.append(f"    FILTER {f}")

    _emit_vertex_filters_for_hop(last, lines=lines, bind_vars=bind_vars, resolver=resolver)
    _emit_edge_type_filters_for_hop(
        last, lines=lines, bind_vars=bind_vars, rel_type_exprs=rel_type_exprs, resolver=resolver
    )
    for f in last.v_prop_filters:
        lines.append(f"    FILTER {f}")
    for f in last.r_prop_filters:
        lines.append(f"    FILTER {f}")

    path_node_vars.append(last.v_trav)
    path_edge_vars.append(last.rel_var)


def _emit_vertex_filters_for_hop(
    h: _HopMeta,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    resolver: MappingResolver,
) -> None:
    """Emit FILTER lines for a hop's target vertex (label/collection checks)."""
    if h.v_map is None:
        if not h.v_bound:
            vcoll_key = _pick_bind_key("vCollection", bind_vars)
            bind_vars[vcoll_key] = _infer_unlabeled_collection(resolver)
            lines.append(f"    FILTER IS_SAME_COLLECTION(@{vcoll_key}, {h.v_trav})")
        return

    skip_coll_filter = (
        h.v_primary is not None
        and h.rel_type is not None
        and resolver.edge_constrains_target(h.rel_type, h.v_primary, h.direction)
    )
    if not skip_coll_filter:
        vcoll_key = _pick_bind_key("vCollection", bind_vars)
        bind_vars[vcoll_key] = h.v_map.get("collectionName")
        if not isinstance(bind_vars[vcoll_key], str) or not bind_vars[vcoll_key]:
            raise CoreError(
                f"Invalid entity mapping collectionName for: {h.v_primary}", code="INVALID_MAPPING"
            )
        lines.append(f"    FILTER IS_SAME_COLLECTION(@{vcoll_key}, {h.v_trav})")

    if h.v_primary is not None:
        v_style = h.v_map.get("style")
        if v_style == "LABEL":
            vtf_key = _pick_bind_key("vTypeField", bind_vars)
            vtv_key = _pick_bind_key("vTypeValue", bind_vars)
            bind_vars[vtf_key] = h.v_map.get("typeField")
            bind_vars[vtv_key] = h.v_map.get("typeValue")
            lines.append(f"    FILTER {h.v_trav}[@{vtf_key}] == @{vtv_key}")
            for f in _extra_label_filters(h.v_trav, h.v_labels, h.v_primary):
                lines.append(f"    FILTER {f}")
        elif v_style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {v_style}", code="INVALID_MAPPING")
        elif len(h.v_labels) > 1:
            _warn_multi_label_collection(h.v_labels, h.v_primary)


def _emit_edge_type_filters_for_hop(
    h: _HopMeta,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    rel_type_exprs: dict[str, str],
    resolver: MappingResolver,
    edge_ref_override: str | None = None,
) -> None:
    """Emit FILTER lines for a hop's edge type discriminator.

    *edge_ref_override*, when provided, is used in the FILTER expression
    instead of the hop's ``rel_var``.  This allows merged traversals to
    reference path edges directly (e.g. ``_path.edges[0]``) without
    creating a LET binding.
    """
    ref = edge_ref_override or h.rel_var
    if h.r_style == "GENERIC_WITH_TYPE":
        rtf_key = _pick_bind_key("relTypeField", bind_vars)
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtf_key] = h.r_map.get("typeField")
        bind_vars[rtv_key] = h.r_map.get("typeValue")
        lines.append(f"    FILTER {ref}[@{rtf_key}] == @{rtv_key}")
        rel_type_exprs[h.rel_var] = f"{ref}[@{rtf_key}]"
        if h.rel_type is not None:
            _warn_missing_vci(resolver, h.rel_type, h.r_map)
    elif h.r_style == "DEDICATED_COLLECTION":
        rel_type_exprs[h.rel_var] = _aql_string_literal(h.rel_type)
    else:
        raise CoreError(f"Unsupported relationship mapping style: {h.r_style}", code="INVALID_MAPPING")


_LET_PATTERN = re.compile(r"^\s*LET\s+(\w+)\s*=")


def _eliminate_dead_lets(lines: list[str]) -> list[str]:
    """Remove LET bindings whose variable is not referenced elsewhere in the query."""
    result: list[str] = []
    for i, line in enumerate(lines):
        m = _LET_PATTERN.match(line)
        if m:
            var = m.group(1)
            var_re = re.compile(r"\b" + re.escape(var) + r"\b")
            used = any(var_re.search(other) for j, other in enumerate(lines) if j != i)
            if not used:
                continue
        result.append(line)
    return result


_SIMPLE_CMP_RE = re.compile(r"^\(?\s*(\w+(?:\.\w+(?:\[.*?\])?)*)\s*(==|!=|<|>|<=|>=)\s*(.+?)\s*\)?$")


def _is_prunable_condition(
    filter_expr: str,
    trav_var: str,
    all_vars: set[str],
) -> bool:
    """Return True if *filter_expr* is a simple comparison referencing only *trav_var*.

    We are conservative: only ``==``, ``!=``, ``<``, ``>``, ``<=``, ``>=``
    on a single property access of the traversal target variable qualify.
    """
    stripped = filter_expr.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    upper = stripped.upper()
    if " AND " in upper or " OR " in upper or upper.startswith("NOT "):
        return False

    m = _SIMPLE_CMP_RE.match(stripped)
    if not m:
        return False
    lhs = m.group(1)
    if not lhs.startswith(f"{trav_var}."):
        return False
    for v in all_vars:
        if v == trav_var:
            continue
        if re.search(r"\b" + re.escape(v) + r"\b", stripped):
            return False
    return True


def _emit_relationship_uniqueness(
    named_rel_vars: list[str],
    lines: list[str],
    indent: str = "    ",
) -> None:
    """Emit FILTER to enforce Cypher's relationship uniqueness guarantee.

    When a pattern contains 2+ explicitly named relationship variables,
    Cypher guarantees no edge appears in more than one binding.  ArangoDB
    handles this within a single traversal but NOT across multiple
    relationship variables in the same pattern.
    """
    if len(named_rel_vars) < 2:
        return
    for i in range(len(named_rel_vars)):
        for j in range(i + 1, len(named_rel_vars)):
            lines.append(f"{indent}FILTER {named_rel_vars[i]}._id != {named_rel_vars[j]}._id")


def _emit_prune_and_filter(
    filter_expr: str,
    trav_var: str,
    all_vars: set[str],
    is_varlen: bool,
    lines: list[str],
    indent: str = "    ",
) -> None:
    """Emit PRUNE (for variable-length traversals) and FILTER lines.

    For variable-length traversals with simple, single-variable conditions,
    a ``PRUNE`` is emitted before the ``FILTER`` to let ArangoDB terminate
    branches early.
    """
    if is_varlen and _is_prunable_condition(filter_expr, trav_var, all_vars):
        lines.append(f"{indent}PRUNE NOT ({filter_expr})")
    lines.append(f"{indent}FILTER {filter_expr}")


def _translate_match_body(
    match_ctxs: list[CypherParser.OC_MatchContext],
    *,
    spq: CypherParser.OC_SinglePartQueryContext,
    optional_matches: list[CypherParser.OC_MatchContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Core MATCH translation logic shared by regular and optional-match paths."""
    all_parts: list = []
    extra_wheres: list[CypherParser.OC_WhereContext] = []
    for mc in match_ctxs:
        pattern = mc.oC_Pattern()
        pp = pattern.oC_PatternPart()
        if not pp:
            raise CoreError("MATCH pattern is required", code="UNSUPPORTED")
        all_parts.extend(pp)
        wc = mc.oC_Where()
        if wc is not None:
            extra_wheres.append(wc)

    # Multi-pattern-part or multi-MATCH: compile all pattern parts together
    if len(all_parts) > 1 or len(match_ctxs) > 1:
        lines, forbidden = _compile_match_multi_parts_from_parts(
            all_parts,
            extra_wheres=extra_wheres,
            resolver=resolver,
            bind_vars=bind_vars,
        )
        var_env = {v: v for v in forbidden}

        if optional_matches:
            opt_env = _compile_optional_matches(
                optional_matches,
                resolver=resolver,
                bind_vars=bind_vars,
                forbidden_vars=forbidden,
                lines=lines,
            )
            var_env.update(opt_env)

        ret = spq.oC_Return()
        if ret is None:
            raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
        _append_return(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars, var_env=var_env)
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    match_ctx = match_ctxs[0]

    # Extract single pattern element (existing v0 behavior)
    anon = all_parts[0].oC_AnonymousPatternPart()
    elem = anon.oC_PatternElement()
    node = elem.oC_NodePattern()
    if node is None:
        raise CoreError("Only node patterns are supported in v0", code="UNSUPPORTED")
    chains = elem.oC_PatternElementChain() or []

    # Case A: single node pattern (existing v0 behavior)
    if not chains:
        var, labels = _extract_node_var_and_labels(node, default_var="n")
        prop_filters = _compile_node_pattern_properties(node, var=var, bind_vars=bind_vars)

        base_filters: list[str] = []
        if not labels:
            # Unlabeled node match: only supported when we can infer a single underlying collection.
            bind_vars["@collection"] = _infer_unlabeled_collection(resolver)
            for_line = f"FOR {var} IN @@collection"
        else:
            primary = _pick_primary_entity_label(labels, resolver)
            entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
            entity_style = entity_mapping.get("style")
            if entity_style == "COLLECTION":
                if len(labels) > 1:
                    _warn_multi_label_collection(labels, primary)
                bind_vars["@collection"] = entity_mapping["collectionName"]
                for_line = f"FOR {var} IN @@collection"
            elif entity_style == "LABEL":
                bind_vars["@collection"] = entity_mapping["collectionName"]
                bind_vars["typeField"] = entity_mapping["typeField"]
                bind_vars["typeValue"] = entity_mapping["typeValue"]
                for_line = f"FOR {var} IN @@collection"
                base_filters.append(f"{var}[@typeField] == @typeValue")
                base_filters.extend(_extra_label_filters(var, labels, primary))
            else:
                raise CoreError(f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING")

        where_ctx = match_ctx.oC_Where()
        user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None

        filters = list(base_filters) + prop_filters
        if user_filter:
            filters.append(user_filter)

        # Build AQL
        lines: list[str] = [for_line]
        if labels:
            idx_hint = _build_collection_index_hint(primary, prop_filters, resolver)
            if idx_hint:
                lines.append(f"  {idx_hint}")
        for f in filters:
            lines.append(f"  FILTER {f}")

        opt_var_env: dict[str, str] = {}
        if optional_matches:
            fv: set[str] = {var}
            opt_var_env = _compile_optional_matches(
                optional_matches,
                resolver=resolver,
                bind_vars=bind_vars,
                forbidden_vars=fv,
                lines=lines,
            )

        ret = spq.oC_Return()
        if ret is None:
            raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
        _append_return(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
            var_env=opt_var_env,
        )
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    # Check for named path: p = (a)-[:REL]->(b)
    path_var_name: str | None = None
    pp_var_ctx = all_parts[0].oC_Variable()
    if pp_var_ctx is not None:
        path_var_name = pp_var_ctx.getText().strip()

    # Case B: relationship pattern (1+ hops)
    u_var, u_labels = _extract_node_var_and_labels(node, default_var="u")
    u_prop_filters = _compile_node_pattern_properties(node, var=u_var, bind_vars=bind_vars)

    u_filters: list[str] = []
    if not u_labels:
        bind_vars["@uCollection"] = _infer_unlabeled_collection(resolver)
    else:
        u_primary = _pick_primary_entity_label(u_labels, resolver)
        u_map = resolver.resolve_entity(_strip_label_backticks(u_primary))
        bind_vars["@uCollection"] = u_map.get("collectionName")
        if not isinstance(bind_vars["@uCollection"], str) or not bind_vars["@uCollection"]:
            raise CoreError(f"Invalid entity mapping collectionName for: {u_primary}", code="INVALID_MAPPING")

        u_style = u_map.get("style")
        if u_style == "LABEL":
            bind_vars["uTypeField"] = u_map.get("typeField")
            bind_vars["uTypeValue"] = u_map.get("typeValue")
            u_filters.append(f"{u_var}[@uTypeField] == @uTypeValue")
            u_filters.extend(_extra_label_filters(u_var, u_labels, u_primary))
        elif u_style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {u_style}", code="INVALID_MAPPING")
        elif len(u_labels) > 1:
            _warn_multi_label_collection(u_labels, u_primary)

    lines = [f"FOR {u_var} IN @@uCollection"]
    for f in u_filters:
        lines.append(f"  FILTER {f}")
    for f in u_prop_filters:
        lines.append(f"  FILTER {f}")

    forbidden_vars: set[str] = {u_var}
    rel_type_exprs: dict[str, str] = {}
    current_var = u_var
    path_node_vars: list[str] = [u_var]
    path_edge_vars: list[str] = []

    # --- Phase 1: pre-process chains into _HopMeta list ---
    hops: list[_HopMeta] = []
    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

        v_var, v_labels = _extract_node_var_and_labels(v_node, default_var="v")
        v_bound = v_node.oC_Variable() is not None and v_var in forbidden_vars
        v_trav = v_var if not v_bound else _pick_fresh_var(f"{v_var}_m", forbidden_vars=forbidden_vars)
        v_prop_filters = _compile_node_pattern_properties(v_node, var=v_trav, bind_vars=bind_vars)

        rel_type, rel_var, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="r")
        detail = rel_pat.oC_RelationshipDetail()
        rel_named = detail is not None and detail.oC_Variable() is not None
        if not rel_named and rel_var in forbidden_vars:
            rel_var = _pick_fresh_var(rel_var, forbidden_vars=forbidden_vars)
        elif rel_var in forbidden_vars:
            raise CoreError("Relationship variable must not shadow node variables", code="UNSUPPORTED")
        forbidden_vars.add(rel_var)

        r_prop_filters = _compile_relationship_pattern_properties(rel_pat, var=rel_var, bind_vars=bind_vars)
        direction = _relationship_direction(rel_pat)

        if direction == "ANY" and rel_type:
            stats_dir = resolver.preferred_traversal_direction(rel_type)
            if stats_dir:
                direction = stats_dir

        v_primary: str | None = None
        v_map: dict[str, Any] | None = None
        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(_strip_label_backticks(v_primary))
        r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
        r_style = r_map.get("style")

        edge_collection = ""
        edge_key = ""
        r_type_field: str | None = None
        r_type_value: str | None = None

        if r_style != "EMBEDDED":
            edge_key = _pick_bind_key("@edgeCollection", bind_vars)
            edge_collection = r_map.get("edgeCollectionName") or r_map.get("collectionName") or ""
            bind_vars[edge_key] = edge_collection
            if not isinstance(bind_vars[edge_key], str) or not bind_vars[edge_key]:
                raise CoreError(
                    f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING"
                )
            r_type_field = r_map.get("typeField")
            r_type_value = r_map.get("typeValue")

        hops.append(
            _HopMeta(
                v_var=v_var,
                v_trav=v_trav,
                v_labels=v_labels,
                v_primary=v_primary,
                v_map=v_map,
                v_bound=v_bound,
                v_prop_filters=v_prop_filters,
                rel_type=rel_type,
                rel_var=rel_var,
                rel_range=rel_range,
                rel_named=rel_named,
                r_prop_filters=r_prop_filters,
                direction=direction,
                r_map=r_map,
                r_style=r_style,
                edge_collection=edge_collection,
                edge_key=edge_key,
                r_type_field=r_type_field,
                r_type_value=r_type_value,
            )
        )

        if not v_bound:
            forbidden_vars.add(v_var)

    # --- Phase 2: group consecutive mergeable hops ---
    groups: list[list[int]] = []
    for i, h in enumerate(hops):
        if not groups:
            groups.append([i])
            continue
        prev = hops[groups[-1][-1]]
        mergeable = (
            h.r_style != "EMBEDDED"
            and prev.r_style != "EMBEDDED"
            and h.edge_collection == prev.edge_collection
            and h.direction == prev.direction
            and h.rel_range == (1, 1)
            and prev.rel_range == (1, 1)
            and not h.v_bound
            and not prev.v_bound
            and h.r_type_field == prev.r_type_field
            and h.r_type_value == prev.r_type_value
            and not prev.r_prop_filters
        )
        if mergeable:
            groups[-1].append(i)
        else:
            groups.append([i])

    # --- Phase 3: emit AQL for each group ---
    for group in groups:
        if len(group) == 1:
            _emit_single_hop(
                hops[group[0]],
                current_var=current_var,
                lines=lines,
                bind_vars=bind_vars,
                rel_type_exprs=rel_type_exprs,
                resolver=resolver,
            )
            h = hops[group[0]]
            current_var = h.v_trav
            path_node_vars.append(h.v_trav)
            path_edge_vars.append(h.rel_var)
        else:
            _emit_merged_hops(
                [hops[i] for i in group],
                current_var=current_var,
                lines=lines,
                bind_vars=bind_vars,
                rel_type_exprs=rel_type_exprs,
                resolver=resolver,
                forbidden_vars=forbidden_vars,
                path_node_vars=path_node_vars,
                path_edge_vars=path_edge_vars,
            )
            last = hops[group[-1]]
            current_var = last.v_trav

    # Cypher's relationship-uniqueness rule states that no edge may bind
    # to two relationship slots in the same MATCH pattern, regardless of
    # whether the user assigned a variable.  AQL traversals enforce this
    # within a single FOR/IN, so explicit filters are only needed across
    # *separate* traversal groups.  Within merged groups the engine
    # already prevents repeated edges.
    if len(groups) > 1:
        cross_group_rels: list[str] = []
        for _gi, group in enumerate(groups):
            if len(group) != 1:
                # Merged group: intermediate edges may not have a stable
                # binding.  Skip cross-uniqueness emission for now; the
                # last hop's edge is still bound but enforcing partial
                # uniqueness would risk referencing absent LETs.
                continue
            h = hops[group[0]]
            if h.r_style == "EMBEDDED":
                continue
            if h.rel_range != (1, 1):
                # Variable-length traversals bind ``rel_var`` to only the
                # final edge; a strict cross-pattern check would need a
                # path-level NOT-IN comparison.  Skip for now.
                continue
            cross_group_rels.append(h.rel_var)
        _emit_relationship_uniqueness(cross_group_rels, lines)
    else:
        # Single group: only named rels need cross-checks if any exist
        # (merged groups already enforce uniqueness internally).
        named_rels = [h.rel_var for h in hops if h.rel_named]
        _emit_relationship_uniqueness(named_rels, lines)

    # Emit named path LET if a path variable was declared
    if path_var_name is not None:
        nodes_arr = "[" + ", ".join(path_node_vars) + "]"
        edges_arr = "[" + ", ".join(path_edge_vars) + "]"
        lines.append(f"    LET {path_var_name} = {{nodes: {nodes_arr}, edges: {edges_arr}}}")
        forbidden_vars.add(path_var_name)
        pvars = _active_path_vars.get()
        pvars[path_var_name] = (path_node_vars, path_edge_vars)

    is_varlen_trav = any(h.rel_range[0] != h.rel_range[1] for h in hops)

    where_ctx = match_ctx.oC_Where()
    user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None
    if user_filter:
        _emit_prune_and_filter(
            user_filter,
            current_var,
            forbidden_vars,
            is_varlen=is_varlen_trav,
            lines=lines,
        )

    opt_var_env_rel: dict[str, str] = {}
    if optional_matches:
        opt_var_env_rel = _compile_optional_matches(
            optional_matches,
            resolver=resolver,
            bind_vars=bind_vars,
            forbidden_vars=forbidden_vars,
            lines=lines,
        )

    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    _append_return(
        ret.oC_ProjectionBody(),
        lines=lines,
        bind_vars=bind_vars,
        var_env=opt_var_env_rel,
        rel_type_exprs=rel_type_exprs,
    )
    lines = _eliminate_dead_lets(lines)
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_foreach_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    foreach_clauses: list[CypherParser.OC_ForeachContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate ``MATCH ... FOREACH (x IN list | SET ...)`` to AQL.

    ``FOREACH (x IN list | SET x.prop = val)``
    becomes:
    ``FOR x IN list UPDATE x WITH {prop: val} IN @@collection``
    """
    reading_clauses = spq.oC_ReadingClause() or []

    lines: list[str] = []

    if reading_clauses:
        match_ctxs: list[CypherParser.OC_MatchContext] = []
        for rc in reading_clauses:
            m = rc.oC_Match()
            if m is not None:
                match_ctxs.append(m)

        if match_ctxs:
            if len(match_ctxs) == 1:
                match_lines, _ = _compile_match_pipeline(
                    match_ctxs[0],
                    resolver=resolver,
                    bind_vars=bind_vars,
                )
            else:
                all_parts = []
                extra_wheres = []
                for mc_item in match_ctxs:
                    pattern = mc_item.oC_Pattern()
                    all_parts.extend(pattern.oC_PatternPart() or [])
                    w = mc_item.oC_Where()
                    if w is not None:
                        extra_wheres.append(w)
                match_lines, _ = _compile_match_multi_parts_from_parts(
                    all_parts,
                    extra_wheres=extra_wheres,
                    resolver=resolver,
                    bind_vars=bind_vars,
                )
            lines.extend(match_lines)

    for fe in foreach_clauses:
        var_name = fe.oC_Variable().getText().strip()
        list_expr = _compile_expression(fe.oC_Expression(), bind_vars)

        lines.append(f"FOR {var_name} IN {list_expr}")

        inner_updating = fe.oC_UpdatingClause() or []
        for uc in inner_updating:
            if uc.oC_Set() is not None:
                sc = uc.oC_Set()
                set_items = sc.oC_SetItem() or []
                update_fields: dict[str, dict[str, str]] = {}
                for si in set_items:
                    prop_expr = si.oC_PropertyExpression()
                    if prop_expr is not None:
                        atom = prop_expr.oC_Atom()
                        target_var = (
                            atom.oC_Variable().getText().strip()
                            if atom.oC_Variable() is not None
                            else var_name
                        )
                        lookups = prop_expr.oC_PropertyLookup() or []
                        if not lookups:
                            raise CoreError("SET requires a property expression", code="UNSUPPORTED")
                        prop_name = lookups[-1].oC_PropertyKeyName().getText().strip()
                        val = _compile_expression(si.oC_Expression(), bind_vars)
                        update_fields.setdefault(target_var, {})[prop_name] = val

                for target_var, fields in update_fields.items():
                    pairs = ", ".join(f"{k}: {v}" for k, v in fields.items())
                    if "@collection" in bind_vars:
                        coll_ref = "@@collection"
                    else:
                        coll_key = _pick_bind_key("@feCollection", bind_vars)
                        all_labels = resolver.all_entity_labels()
                        if all_labels:
                            e_map = resolver.resolve_entity(_strip_label_backticks(all_labels[0]))
                            bind_vars[coll_key] = e_map.get("collectionName")
                        else:
                            bind_vars[coll_key] = "unknown"
                        coll_ref = _aql_collection_ref(coll_key)
                    lines.append(f"  UPDATE {target_var} WITH {{{pairs}}} IN {coll_ref}")
            elif uc.oC_Create() is not None:
                raise CoreError("CREATE inside FOREACH not yet supported", code="NOT_IMPLEMENTED")
            elif uc.oC_Delete() is not None:
                raise CoreError("DELETE inside FOREACH not yet supported", code="NOT_IMPLEMENTED")
            else:
                raise CoreError("Unsupported clause inside FOREACH", code="UNSUPPORTED")

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_mutating_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    set_clauses: list[CypherParser.OC_SetContext],
    delete_clauses: list[CypherParser.OC_DeleteContext],
    remove_clauses: list,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate MATCH ... SET / DELETE / REMOVE queries to AQL.

    SET n.prop = val  →  UPDATE n WITH {prop: val} IN @@collection
    DELETE n          →  REMOVE n IN @@collection
    DETACH DELETE n   →  (remove edges first, then REMOVE node)
    REMOVE n.prop     →  UPDATE n WITH {} IN @@collection ... (UNSET)
    """
    reading_clauses = spq.oC_ReadingClause() or []
    match_ctxs: list[CypherParser.OC_MatchContext] = []
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is not None:
            match_ctxs.append(m)

    if not match_ctxs:
        raise CoreError("MATCH is required before SET/DELETE", code="UNSUPPORTED")

    # Compile the MATCH body into FOR/FILTER lines
    mc = match_ctxs[0]
    pattern = mc.oC_Pattern()
    parts = pattern.oC_PatternPart() or []
    if not parts:
        raise CoreError("MATCH pattern is required", code="UNSUPPORTED")

    anon = parts[0].oC_AnonymousPatternPart()
    elem = anon.oC_PatternElement()
    start_node = elem.oC_NodePattern()
    if start_node is None:
        raise CoreError("MATCH requires a node pattern", code="UNSUPPORTED")

    chains = elem.oC_PatternElementChain() or []
    var, labels = _extract_node_var_and_labels(start_node, default_var="n")
    prop_filters = _compile_node_pattern_properties(start_node, var=var, bind_vars=bind_vars)

    if not labels:
        raise CoreError("SET/DELETE requires labeled node in v0", code="UNSUPPORTED")

    primary = _pick_primary_entity_label(labels, resolver)
    entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
    coll_key = "@collection"
    bind_vars[coll_key] = entity_mapping["collectionName"]

    lines: list[str] = [f"FOR {var} IN @@collection"]

    style = entity_mapping.get("style")
    if style == "LABEL":
        bind_vars["typeField"] = entity_mapping["typeField"]
        bind_vars["typeValue"] = entity_mapping["typeValue"]
        lines.append(f"  FILTER {var}[@typeField] == @typeValue")

    for f in prop_filters:
        lines.append(f"  FILTER {f}")

    # Handle relationship chain if present
    trav_vars: dict[str, str] = {var: var}
    forbidden: set[str] = {var}
    current = var
    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid pattern in SET/DELETE", code="UNSUPPORTED")

        v_var, v_labels = _extract_node_var_and_labels(v_node, default_var="v")
        rel_type, rel_var, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="r")
        direction = _relationship_direction(rel_pat)

        r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
        edge_key = _pick_bind_key("@edgeCollection", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")

        rmin, rmax = rel_range
        edge_ref = _aql_collection_ref(edge_key)
        lines.append(f"  FOR {v_var}, {rel_var} IN {rmin}..{rmax} {direction} {current} {edge_ref}")

        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(_strip_label_backticks(v_primary))
            v_style = v_map.get("style")
            if v_style == "LABEL":
                vtf = _pick_bind_key("vTypeField", bind_vars)
                vtv = _pick_bind_key("vTypeValue", bind_vars)
                bind_vars[vtf] = v_map.get("typeField")
                bind_vars[vtv] = v_map.get("typeValue")
                lines.append(f"    FILTER {v_var}[@{vtf}] == @{vtv}")

        r_style = r_map.get("style")
        if r_style == "GENERIC_WITH_TYPE":
            rtf = _pick_bind_key("relTypeField", bind_vars)
            rtv = _pick_bind_key("relTypeValue", bind_vars)
            bind_vars[rtf] = r_map.get("typeField")
            bind_vars[rtv] = r_map.get("typeValue")
            lines.append(f"    FILTER {rel_var}[@{rtf}] == @{rtv}")

        current = v_var
        trav_vars[v_var] = v_var
        trav_vars[rel_var] = rel_var
        forbidden.add(v_var)
        forbidden.add(rel_var)

    where_ctx = mc.oC_Where()
    if where_ctx is not None:
        wf = _compile_where(where_ctx.oC_Expression(), bind_vars)
        lines.append(f"  FILTER {wf}")

    # Compile SET items
    for sc in set_clauses:
        set_items = sc.oC_SetItem() or []
        update_fields: dict[str, dict[str, str]] = {}
        for si in set_items:
            prop_expr = si.oC_PropertyExpression()
            if prop_expr is not None:
                # n.prop = val
                atom = prop_expr.oC_Atom()
                target_var = atom.oC_Variable().getText().strip() if atom.oC_Variable() is not None else var
                lookups = prop_expr.oC_PropertyLookup() or []
                if not lookups:
                    raise CoreError("SET requires a property expression", code="UNSUPPORTED")
                prop_name = lookups[-1].oC_PropertyKeyName().getText().strip()
                val = _compile_expression(si.oC_Expression(), bind_vars)
                update_fields.setdefault(target_var, {})[prop_name] = val
            else:
                si_var = si.oC_Variable()
                if si_var is not None:
                    target_var = si_var.getText().strip()
                    val = _compile_expression(si.oC_Expression(), bind_vars)
                    txt = si.getText()
                    if "+=" in txt:
                        lines.append(f"  UPDATE {target_var} WITH MERGE({target_var}, {val}) IN @@collection")
                    else:
                        lines.append(f"  REPLACE {target_var} WITH {val} IN @@collection")

        for target_var, fields in update_fields.items():
            pairs = ", ".join(f"{k}: {v}" for k, v in fields.items())
            target_coll = "@@collection"
            if target_var != var and target_var in trav_vars:
                tc_key = _pick_bind_key("@setCollection", bind_vars)
                # Determine collection from traversal context
                bind_vars[tc_key] = bind_vars.get("@collection", "")
                target_coll = f"@{tc_key}"
            lines.append(f"  UPDATE {target_var} WITH {{{pairs}}} IN {target_coll}")

    # Compile DELETE
    for dc in delete_clauses:
        is_detach = dc.DETACH() is not None
        del_exprs = dc.oC_Expression() or []
        for de in del_exprs:
            del_var = _compile_expression(de, bind_vars)
            if is_detach:
                edge_colls = resolver.all_edge_collections()
                for idx, ec in enumerate(edge_colls):
                    ec_key = _pick_bind_key("@detachEdge", bind_vars)
                    bind_vars[ec_key] = ec
                    ec_ref = _aql_collection_ref(ec_key)
                    de_var = f"_de{idx}"
                    lines.append(
                        f"  LET _edgeRm{idx} = (FOR {de_var} IN 1..1 ANY {del_var} {ec_ref} REMOVE {de_var} IN {ec_ref})"
                    )
            lines.append(f"  REMOVE {del_var} IN @@collection")

    # Compile REMOVE (property removal)
    for rc in remove_clauses:
        rm_items = rc.oC_RemoveItem() or []
        for ri in rm_items:
            prop_expr = ri.oC_PropertyExpression()
            if prop_expr is not None:
                atom = prop_expr.oC_Atom()
                target_var = atom.oC_Variable().getText().strip() if atom.oC_Variable() is not None else var
                lookups = prop_expr.oC_PropertyLookup() or []
                if lookups:
                    prop_name = lookups[-1].oC_PropertyKeyName().getText().strip()
                    lines.append(
                        f"  UPDATE {target_var} WITH {{}} IN @@collection OPTIONS {{keepNull: false}}"
                    )
                    # Use UNSET approach
                    lines[-1] = (
                        f'  UPDATE {target_var} WITH UNSET({target_var}, "{prop_name}") IN @@collection'
                    )

    # Optional RETURN
    ret = spq.oC_Return()
    if ret is not None:
        _append_return(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars)

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_union(
    regular: CypherParser.OC_RegularQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """
    Translate ``query UNION [ALL] query ...`` into AQL using
    ``UNION()`` / ``UNION_DISTINCT()`` array functions with subqueries.

    AQL shape::

        FOR _u IN UNION_DISTINCT(
          (FOR ... RETURN ...),
          (FOR ... RETURN ...)
        )
          RETURN _u
    """
    first_sq = regular.oC_SingleQuery()
    union_clauses = regular.oC_Union()
    if not union_clauses:
        raise CoreError(
            "Internal: _translate_union called without union clauses",
            code="INTERNAL_ERROR",
        )

    branches: list[CypherParser.OC_SingleQueryContext] = [first_sq]
    is_all = True
    for uc in union_clauses:
        branches.append(uc.oC_SingleQuery())
        if uc.ALL() is None:
            is_all = False

    subqueries: list[str] = []
    for branch in branches:
        branch_bv: dict[str, Any] = {}
        bq = _translate_single_query(
            branch,
            resolver=resolver,
            bind_vars=branch_bv,
        )
        _merge_bind_vars(bind_vars, branch_bv)
        subqueries.append(f"({bq.text})")

    fn = "UNION" if is_all else "UNION_DISTINCT"
    joined = ",\n  ".join(subqueries)
    aql = f"FOR _u IN {fn}(\n  {joined}\n)\n  RETURN _u"
    return AqlQuery(text=aql, bind_vars=bind_vars)


def _merge_bind_vars(
    target: dict[str, Any],
    source: dict[str, Any],
) -> None:
    """
    Merge *source* bind vars into *target*.  Raise on key collision with
    different values (bind var names must be unique across UNION branches).
    """
    for k, v in source.items():
        if k in target:
            if target[k] != v:
                raise CoreError(
                    f"Bind variable collision across UNION branches: @{k} has conflicting values",
                    code="UNSUPPORTED",
                )
        else:
            target[k] = v


def _translate_create_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    create_clauses: list[CypherParser.OC_CreateContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate a single-part query containing CREATE clause(s)."""
    reading_clauses = spq.oC_ReadingClause() or []
    ret = spq.oC_Return()

    lines: list[str] = []
    var_env: dict[str, str] = {}
    indent = ""

    match_clauses: list[CypherParser.OC_MatchContext] = []
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is not None:
            match_clauses.append(m)
        else:
            raise CoreError(
                "Only MATCH reading clauses are supported with CREATE",
                code="NOT_IMPLEMENTED",
            )

    if match_clauses:
        all_parts: list = []
        extra_wheres: list[CypherParser.OC_WhereContext] = []
        for mc in match_clauses:
            pattern = mc.oC_Pattern()
            pp = pattern.oC_PatternPart() or []
            all_parts.extend(pp)
            wc = mc.oC_Where()
            if wc is not None:
                extra_wheres.append(wc)

        if len(all_parts) == 1 and len(match_clauses) == 1:
            match_lines, forbidden = _compile_match_pipeline(
                match_clauses[0],
                resolver=resolver,
                bind_vars=bind_vars,
            )
        else:
            match_lines, forbidden = _compile_match_multi_parts_from_parts(
                all_parts,
                extra_wheres=extra_wheres,
                resolver=resolver,
                bind_vars=bind_vars,
            )
        lines.extend(match_lines)
        var_env = {v: v for v in forbidden}
        indent = "  "

    num_creates = len(create_clauses)
    for ci, cc in enumerate(create_clauses):
        force_let = ci < num_creates - 1
        _compile_create(
            cc,
            resolver=resolver,
            bind_vars=bind_vars,
            var_env=var_env,
            lines=lines,
            indent=indent,
            has_return=ret is not None or force_let,
        )

    if ret is not None:
        _compile_return_for_create(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
            indent=indent,
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _compile_create(
    create_ctx: CypherParser.OC_CreateContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    var_env: dict[str, str],
    lines: list[str],
    indent: str,
    has_return: bool,
) -> None:
    """Compile a single CREATE clause into AQL INSERT lines."""
    pattern = create_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart() or []

    @dataclass
    class _CreateOp:
        kind: str  # "node" or "rel"
        var: str
        labels: list[str] | None = None
        node_ctx: Any = None
        rel_pat: Any = None
        from_var: str = ""
        to_var: str = ""

    ops: list[_CreateOp] = []
    create_counter = 0
    anon_counter = 0

    def _unique_anon(var: str) -> str:
        nonlocal anon_counter
        if var != "_anon" or var not in var_env:
            return var
        while f"_anon{anon_counter}" in var_env:
            anon_counter += 1
        result = f"_anon{anon_counter}"
        anon_counter += 1
        return result

    for part in parts:
        anon = part.oC_AnonymousPatternPart()
        elem = anon.oC_PatternElement()
        node = elem.oC_NodePattern()
        chains = elem.oC_PatternElementChain() or []

        start_var, start_labels = _extract_node_var_and_labels(node, default_var="_anon")
        start_var = _unique_anon(start_var)

        if start_var not in var_env:
            ops.append(_CreateOp(kind="node", var=start_var, labels=start_labels, node_ctx=node))
            var_env[start_var] = start_var

        current_var = start_var
        for chain in chains:
            rel_pat = chain.oC_RelationshipPattern()
            end_node = chain.oC_NodePattern()
            if rel_pat is None or end_node is None:
                raise CoreError("Invalid CREATE pattern", code="UNSUPPORTED")

            end_var, end_labels = _extract_node_var_and_labels(end_node, default_var="_anon")
            end_var = _unique_anon(end_var)

            if end_var not in var_env:
                ops.append(_CreateOp(kind="node", var=end_var, labels=end_labels, node_ctx=end_node))
                var_env[end_var] = end_var

            detail = rel_pat.oC_RelationshipDetail()
            rel_var_name = ""
            if detail is not None and detail.oC_Variable() is not None:
                rel_var_name = detail.oC_Variable().getText().strip()
            if not rel_var_name:
                rel_var_name = f"_c{create_counter}"
                create_counter += 1

            direction = _relationship_direction(rel_pat)
            if direction == "INBOUND":
                from_v, to_v = end_var, current_var
            else:
                from_v, to_v = current_var, end_var

            ops.append(
                _CreateOp(
                    kind="rel",
                    var=rel_var_name,
                    rel_pat=rel_pat,
                    from_var=from_v,
                    to_var=to_v,
                )
            )

            current_var = end_var

    for i, op in enumerate(ops):
        is_last = i == len(ops) - 1
        needs_let = has_return or not is_last

        if op.kind == "node":
            _compile_create_node(
                op.var,
                op.labels or [],
                op.node_ctx,
                resolver=resolver,
                bind_vars=bind_vars,
                lines=lines,
                indent=indent,
                needs_let=needs_let,
            )
        elif op.kind == "rel":
            _compile_create_rel(
                op.var,
                op.rel_pat,
                op.from_var,
                op.to_var,
                resolver=resolver,
                bind_vars=bind_vars,
                lines=lines,
                indent=indent,
                needs_let=needs_let,
            )


def _find_or_create_collection_bind_key(
    base: str,
    collection_name: str,
    bind_vars: dict[str, Any],
) -> str:
    """Reuse an existing bind key if it already points to the same collection."""
    if base in bind_vars and bind_vars[base] == collection_name:
        return base
    i = 2
    while f"{base}{i}" in bind_vars:
        if bind_vars[f"{base}{i}"] == collection_name:
            return f"{base}{i}"
        i += 1
    key = _pick_bind_key(base, bind_vars)
    bind_vars[key] = collection_name
    return key


def _compile_create_node(
    var: str,
    labels: list[str],
    node_ctx: CypherParser.OC_NodePatternContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    lines: list[str],
    indent: str,
    needs_let: bool,
) -> None:
    """Compile a single node INSERT."""
    props = _compile_create_props(node_ctx.oC_Properties(), bind_vars)
    extra_fields: list[str] = []

    if not labels:
        coll_name = _infer_unlabeled_collection(resolver)
        coll_key = _find_or_create_collection_bind_key("@collection", coll_name, bind_vars)
    else:
        primary = _pick_primary_entity_label(labels, resolver)
        entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
        style = entity_mapping.get("style")

        coll_key = _find_or_create_collection_bind_key(
            "@collection",
            entity_mapping["collectionName"],
            bind_vars,
        )

        if style == "LABEL":
            type_field = entity_mapping.get("typeField", "type")
            tv_key = _pick_bind_key("typeValue", bind_vars)
            bind_vars[tv_key] = entity_mapping.get("typeValue")
            extra_fields.append(f"{type_field}: @{tv_key}")
        elif style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {style}", code="INVALID_MAPPING")

    doc = _build_insert_doc(props, extra_fields)
    coll_ref = _aql_collection_ref(coll_key)

    if needs_let:
        lines.append(f"{indent}LET {var} = FIRST(INSERT {doc} INTO {coll_ref} RETURN NEW)")
    else:
        lines.append(f"{indent}INSERT {doc} INTO {coll_ref}")


def _compile_create_rel(
    var: str,
    rel_pat: CypherParser.OC_RelationshipPatternContext,
    from_var: str,
    to_var: str,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    lines: list[str],
    indent: str,
    needs_let: bool,
) -> None:
    """Compile a single relationship INSERT."""
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        raise CoreError("Relationship type is required for CREATE", code="UNSUPPORTED")
    types_ctx = detail.oC_RelationshipTypes()
    if types_ctx is None:
        raise CoreError("Relationship type is required for CREATE", code="UNSUPPORTED")
    types = types_ctx.oC_RelTypeName()
    if not types or len(types) != 1:
        raise CoreError("Exactly one relationship type is required for CREATE", code="UNSUPPORTED")
    rel_type = types[0].getText().strip()

    r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
    r_style = r_map.get("style")

    edge_coll_name = r_map.get("edgeCollectionName") or r_map.get("collectionName")
    if not isinstance(edge_coll_name, str) or not edge_coll_name:
        raise CoreError(
            f"Invalid relationship mapping collection for: {rel_type}",
            code="INVALID_MAPPING",
        )

    edge_coll_key = _find_or_create_collection_bind_key(
        "@edgeCollection",
        edge_coll_name,
        bind_vars,
    )

    extra_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]

    if r_style == "GENERIC_WITH_TYPE":
        type_field = r_map.get("typeField", "type")
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtv_key] = r_map.get("typeValue")
        extra_fields.append(f"{type_field}: @{rtv_key}")
    elif r_style == "EMBEDDED":
        raise CoreError(
            "EMBEDDED relationships are not supported for CREATE",
            code="UNSUPPORTED",
        )
    elif r_style != "DEDICATED_COLLECTION":
        raise CoreError(
            f"Unsupported relationship mapping style for CREATE: {r_style}",
            code="INVALID_MAPPING",
        )

    props = _compile_create_rel_props(rel_pat, bind_vars)
    doc = _build_insert_doc(props, extra_fields)
    coll_ref = _aql_collection_ref(edge_coll_key)

    if needs_let:
        lines.append(f"{indent}LET {var} = FIRST(INSERT {doc} INTO {coll_ref} RETURN NEW)")
    else:
        lines.append(f"{indent}INSERT {doc} INTO {coll_ref}")


def _compile_create_props(
    props_ctx: CypherParser.OC_PropertiesContext | None,
    bind_vars: dict[str, Any],
) -> list[tuple[str, str]]:
    """Extract property key-value pairs from a pattern's properties for INSERT."""
    if props_ctx is None:
        return []
    if props_ctx.oC_Parameter() is not None:
        raise CoreError(
            "Parameterized properties are not supported in CREATE",
            code="NOT_IMPLEMENTED",
        )
    m = props_ctx.oC_MapLiteral()
    if m is None:
        return []
    keys = m.oC_PropertyKeyName() or []
    vals = m.oC_Expression() or []
    if len(keys) != len(vals):
        raise CoreError("Invalid properties map in CREATE", code="UNSUPPORTED")
    out: list[tuple[str, str]] = []
    for k_ctx, v_ctx in zip(keys, vals, strict=False):
        key = k_ctx.getText().strip()
        if not key:
            raise CoreError("Invalid property key in CREATE", code="UNSUPPORTED")
        expr = _compile_expression(v_ctx, bind_vars)
        out.append((key, expr))
    return out


def _compile_create_rel_props(
    rel_pat: CypherParser.OC_RelationshipPatternContext,
    bind_vars: dict[str, Any],
) -> list[tuple[str, str]]:
    """Extract properties from a relationship pattern for CREATE."""
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        return []
    return _compile_create_props(detail.oC_Properties(), bind_vars)


def _build_insert_doc(
    props: list[tuple[str, str]],
    extra_fields: list[str] | None = None,
) -> str:
    """Build an AQL object literal for INSERT."""
    fields: list[str] = list(extra_fields or [])
    fields.extend(f"{k}: {v}" for k, v in props)
    if not fields:
        return "{}"
    return "{" + ", ".join(fields) + "}"


def _compile_return_for_create(
    proj: CypherParser.OC_ProjectionBodyContext,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    indent: str = "",
) -> None:
    """Compile a RETURN clause for a CREATE query (respects indent level)."""
    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem()
    if not items:
        raise CoreError("RETURN items required", code="UNSUPPORTED")

    compiled_items: list[tuple[str | None, str]] = []
    for it in items:
        expr = _compile_expression(it.oC_Expression(), bind_vars)
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None
        compiled_items.append((alias, expr))

    if len(compiled_items) == 1 and compiled_items[0][0] is None:
        lines.append(f"{indent}RETURN {compiled_items[0][1]}")
    else:
        lines.append(f"{indent}RETURN " + _compile_return_object(compiled_items))


def _translate_merge_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    merge_clauses: list[CypherParser.OC_MergeContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate MERGE clause(s) into AQL UPSERT."""
    if len(merge_clauses) != 1:
        raise CoreError(
            "Only a single MERGE clause is supported",
            code="NOT_IMPLEMENTED",
        )
    merge_ctx = merge_clauses[0]
    pattern_part = merge_ctx.oC_PatternPart()
    anon = pattern_part.oC_AnonymousPatternPart()
    elem = anon.oC_PatternElement()
    node = elem.oC_NodePattern()
    chains = elem.oC_PatternElementChain() or []

    if chains:
        return _translate_merge_relationship(
            spq,
            merge_ctx=merge_ctx,
            node=node,
            chains=chains,
            resolver=resolver,
            bind_vars=bind_vars,
        )

    var, labels = _extract_node_var_and_labels(node, default_var="n")
    if not labels:
        raise CoreError("MERGE requires a labeled node", code="UNSUPPORTED")

    primary = _pick_primary_entity_label(labels, resolver)
    entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
    coll_key = _find_or_create_collection_bind_key(
        "@collection",
        entity_mapping["collectionName"],
        bind_vars,
    )
    coll_ref = _aql_collection_ref(coll_key)

    props = _compile_create_props(node.oC_Properties(), bind_vars)
    extra_fields: list[str] = []
    style = entity_mapping.get("style")
    if style == "LABEL":
        type_field = entity_mapping.get("typeField", "type")
        tv_key = _pick_bind_key("typeValue", bind_vars)
        bind_vars[tv_key] = entity_mapping.get("typeValue")
        extra_fields.append(f"{type_field}: @{tv_key}")

    search_doc = _build_insert_doc(props, extra_fields)
    insert_doc = search_doc

    on_create_fields, on_match_fields = _extract_merge_actions(merge_ctx, bind_vars)

    if on_create_fields:
        all_insert_fields = list(extra_fields)
        all_insert_fields.extend(f"{k}: {v}" for k, v in props)
        all_insert_fields.extend(on_create_fields)
        insert_doc = "{" + ", ".join(all_insert_fields) + "}" if all_insert_fields else "{}"

    update_doc = "{" + ", ".join(on_match_fields) + "}" if on_match_fields else "{}"

    lines: list[str] = []
    _compile_merge_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars, lines=lines)

    lines.append(f"UPSERT {search_doc}")
    lines.append(f"INSERT {insert_doc}")
    lines.append(f"UPDATE {update_doc}")
    lines.append(f"IN {coll_ref}")

    ret = spq.oC_Return()
    if ret is not None:
        ret_var = var
        lines.append(f"LET {ret_var} = NEW")
        _compile_return_for_create(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _compile_merge_reading_clauses(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    lines: list[str],
) -> None:
    """Compile preceding MATCH clauses for a MERGE statement."""
    reading_clauses = spq.oC_ReadingClause() or []
    if not reading_clauses:
        return
    all_parts: list = []
    extra_wheres: list = []
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is None:
            continue
        pattern = m.oC_Pattern()
        pp = pattern.oC_PatternPart() or []
        all_parts.extend(pp)
        wc = m.oC_Where()
        if wc is not None:
            extra_wheres.append(wc)
    if not all_parts:
        return
    if len(all_parts) == 1 and not extra_wheres:
        match_lines, _ = _compile_match_pipeline(
            reading_clauses[0].oC_Match(),
            resolver=resolver,
            bind_vars=bind_vars,
        )
    else:
        match_lines, _ = _compile_match_multi_parts_from_parts(
            all_parts,
            extra_wheres=extra_wheres,
            resolver=resolver,
            bind_vars=bind_vars,
        )
    lines.extend(match_lines)


def _extract_merge_actions(
    merge_ctx: CypherParser.OC_MergeContext,
    bind_vars: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Extract ON CREATE SET / ON MATCH SET fields from a MERGE clause."""
    on_create_fields: list[str] = []
    on_match_fields: list[str] = []

    merge_actions = merge_ctx.oC_MergeAction() or []
    for action in merge_actions:
        is_on_create = action.CREATE() is not None
        set_ctx = action.oC_Set()
        if set_ctx is None:
            continue
        set_items = set_ctx.oC_SetItem() or []
        for si in set_items:
            prop_expr = si.oC_PropertyExpression()
            if prop_expr is not None:
                lookups = prop_expr.oC_PropertyLookup() or []
                if not lookups:
                    continue
                prop_name = lookups[-1].oC_PropertyKeyName().getText().strip()
                val = _compile_expression(si.oC_Expression(), bind_vars)
                if is_on_create:
                    on_create_fields.append(f"{prop_name}: {val}")
                else:
                    on_match_fields.append(f"{prop_name}: {val}")

    return on_create_fields, on_match_fields


def _translate_merge_relationship(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    merge_ctx: CypherParser.OC_MergeContext,
    node: CypherParser.OC_NodePatternContext,
    chains: list,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate ``MERGE (a)-[:REL {props}]->(b)`` into AQL UPSERT on an edge collection."""
    if len(chains) != 1:
        raise CoreError(
            "Only single-hop relationship MERGE is supported",
            code="NOT_IMPLEMENTED",
        )

    chain = chains[0]
    rel_pat = chain.oC_RelationshipPattern()
    target_node = chain.oC_NodePattern()
    if rel_pat is None or target_node is None:
        raise CoreError("Invalid relationship MERGE pattern", code="UNSUPPORTED")

    start_var, _ = _extract_node_var_and_labels(node, default_var="a")
    end_var, _ = _extract_node_var_and_labels(target_node, default_var="b")

    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        raise CoreError("Relationship type is required for MERGE", code="UNSUPPORTED")
    types_ctx = detail.oC_RelationshipTypes()
    if types_ctx is None:
        raise CoreError("Relationship type is required for MERGE", code="UNSUPPORTED")
    type_names = types_ctx.oC_RelTypeName()
    if not type_names or len(type_names) != 1:
        raise CoreError("Exactly one relationship type is required for MERGE", code="UNSUPPORTED")
    rel_type = type_names[0].getText().strip()

    direction = _relationship_direction(rel_pat)
    if direction == "INBOUND":
        from_var, to_var = end_var, start_var
    else:
        from_var, to_var = start_var, end_var

    r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
    r_style = r_map.get("style")
    edge_coll_name = r_map.get("edgeCollectionName") or r_map.get("collectionName")
    if not isinstance(edge_coll_name, str) or not edge_coll_name:
        raise CoreError(
            f"Invalid relationship mapping collection for: {rel_type}",
            code="INVALID_MAPPING",
        )
    edge_coll_key = _find_or_create_collection_bind_key(
        "@edgeCollection",
        edge_coll_name,
        bind_vars,
    )
    edge_coll_ref = _aql_collection_ref(edge_coll_key)

    search_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]
    insert_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]

    if r_style == "GENERIC_WITH_TYPE":
        type_field = r_map.get("typeField", "type")
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtv_key] = r_map.get("typeValue")
        search_fields.append(f"{type_field}: @{rtv_key}")
        insert_fields.append(f"{type_field}: @{rtv_key}")

    rel_props = _compile_create_rel_props(rel_pat, bind_vars)
    for k, v in rel_props:
        insert_fields.append(f"{k}: {v}")

    on_create_fields, on_match_fields = _extract_merge_actions(merge_ctx, bind_vars)

    search_doc = "{" + ", ".join(search_fields) + "}"

    if on_create_fields:
        all_insert = list(insert_fields) + on_create_fields
        insert_doc = "{" + ", ".join(all_insert) + "}"
    else:
        insert_doc = "{" + ", ".join(insert_fields) + "}"

    update_doc = "{" + ", ".join(on_match_fields) + "}" if on_match_fields else "{}"

    lines: list[str] = []
    _compile_merge_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars, lines=lines)

    lines.append(f"UPSERT {search_doc}")
    lines.append(f"INSERT {insert_doc}")
    lines.append(f"UPDATE {update_doc}")
    lines.append(f"IN {edge_coll_ref}")

    ret = spq.oC_Return()
    if ret is not None:
        rel_var_name = ""
        if detail.oC_Variable() is not None:
            rel_var_name = detail.oC_Variable().getText().strip()
        if not rel_var_name:
            rel_var_name = "r"
        lines.append(f"LET {rel_var_name} = NEW")
        _compile_return_for_create(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_multi_part_query(
    mpq: CypherParser.OC_MultiPartQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """
    Minimal MultiPartQuery support for v0.1 corpus:
    MATCH ... WITH ... RETURN ...
    """
    with_ctxs = mpq.oC_With() or []
    if not with_ctxs:
        raise CoreError("WITH is required for multi-part queries", code="UNSUPPORTED")
    if mpq.oC_UpdatingClause():
        raise CoreError("Updating clauses are not supported in v0", code="UNSUPPORTED")

    reading_clauses = mpq.oC_ReadingClause() or []
    if not reading_clauses:
        raise CoreError("MATCH is required before WITH in v0 subset", code="UNSUPPORTED")

    match_ctxs: list[CypherParser.OC_MatchContext] = []
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is not None:
            match_ctxs.append(m)
    if not match_ctxs:
        raise CoreError("MATCH is required before WITH in v0 subset", code="UNSUPPORTED")

    lines, forbidden_vars = _compile_match_pipeline(match_ctxs[0], resolver=resolver, bind_vars=bind_vars)

    for extra_match in match_ctxs[1:]:
        extra_var_env, _ = _compile_match_from_bound(
            extra_match,
            resolver=resolver,
            bind_vars=bind_vars,
            forbidden_vars=forbidden_vars,
            var_env={v: v for v in forbidden_vars},
            lines=lines,
        )
        forbidden_vars.update(extra_var_env.keys())

    # Apply all WITH stages
    var_env: dict[str, str] = {v: v for v in forbidden_vars}
    for w in with_ctxs:
        var_env = _apply_with(
            w,
            lines=lines,
            bind_vars=bind_vars,
            forbidden_vars=forbidden_vars,
            incoming_env=var_env,
        )

    # Final RETURN is in the trailing SinglePartQuery
    tail = mpq.oC_SinglePartQuery()
    if tail is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")

    # Fail fast on any updating clause (SET/CREATE/DELETE/etc). v0 is read-only.
    if tail.oC_UpdatingClause():
        raise CoreError("Updating clauses are not supported in v0", code="UNSUPPORTED")

    # Support a tail MATCH after WITH (e.g. MATCH ... WITH ... MATCH ... RETURN ...).
    # This is limited to patterns that start from an already-bound variable.
    tail_reading = tail.oC_ReadingClause() or []
    rel_type_exprs: dict[str, str] = {}
    if tail_reading:
        tail_match_ctxs: list[CypherParser.OC_MatchContext] = []
        for rc in tail_reading:
            m = rc.oC_Match()
            if m is not None:
                tail_match_ctxs.append(m)
        if not tail_match_ctxs:
            raise CoreError("Only MATCH is supported after WITH in v0 subset", code="NOT_IMPLEMENTED")
        for tm in tail_match_ctxs:
            var_env, rel_type_exprs = _compile_match_from_bound(
                tm,
                resolver=resolver,
                bind_vars=bind_vars,
                forbidden_vars=forbidden_vars,
                var_env=var_env,
                lines=lines,
            )

    ret = tail.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    _append_return(
        ret.oC_ProjectionBody(),
        lines=lines,
        bind_vars=bind_vars,
        var_env=var_env,
        rel_type_exprs=rel_type_exprs,
    )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _compile_optional_matches(
    opt_matches: list[CypherParser.OC_MatchContext],
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    lines: list[str],
) -> dict[str, str]:
    """Compile OPTIONAL MATCH clauses into LET + subquery (FIRST) pattern.

    Supports:
    - Single-hop: ``OPTIONAL MATCH (a)-[:REL]->(b)``
    - Multi-segment: ``OPTIONAL MATCH (a)-[:R1]->(b)-[:R2]->(c)``
    - Node-only: ``OPTIONAL MATCH (n:Person {name: "Nobody"})``
    """
    extra_env: dict[str, str] = {}
    for om in opt_matches:
        pattern = om.oC_Pattern()
        om_parts = pattern.oC_PatternPart()
        if not om_parts:
            raise CoreError(
                "OPTIONAL MATCH requires at least one pattern part",
                code="UNSUPPORTED",
            )

        for part in om_parts:
            anon = part.oC_AnonymousPatternPart()
            elem = anon.oC_PatternElement()
            start_node = elem.oC_NodePattern()
            if start_node is None:
                raise CoreError("OPTIONAL MATCH requires a node pattern", code="UNSUPPORTED")
            chains = elem.oC_PatternElementChain() or []

            if not chains:
                _compile_optional_node_only(
                    om,
                    start_node,
                    resolver=resolver,
                    bind_vars=bind_vars,
                    forbidden_vars=forbidden_vars,
                    lines=lines,
                    extra_env=extra_env,
                )
                continue

            _compile_optional_with_chains(
                om,
                start_node,
                chains,
                resolver=resolver,
                bind_vars=bind_vars,
                forbidden_vars=forbidden_vars,
                lines=lines,
                extra_env=extra_env,
            )

    return extra_env


def _compile_optional_node_only(
    om: CypherParser.OC_MatchContext,
    start_node: Any,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    lines: list[str],
    extra_env: dict[str, str],
) -> None:
    """Compile node-only OPTIONAL MATCH: ``OPTIONAL MATCH (n:Label {prop: val})``."""
    u_var, u_labels = _extract_node_var_and_labels(start_node, default_var="n")
    if not u_labels:
        raise CoreError(
            "Node-only OPTIONAL MATCH requires at least one label",
            code="UNSUPPORTED",
        )

    primary = _pick_primary_entity_label(u_labels, resolver)
    emap = resolver.resolve_entity(_strip_label_backticks(primary))
    coll_key = _pick_bind_key("@optCollection", bind_vars)
    bind_vars[coll_key] = emap.get("collectionName")

    inner_var = _pick_fresh_var(f"{u_var}_0", forbidden_vars=forbidden_vars)
    sub_filters: list[str] = []

    style = emap.get("style")
    if style == "LABEL":
        tf_key = _pick_bind_key("optTF", bind_vars)
        tv_key = _pick_bind_key("optTV", bind_vars)
        bind_vars[tf_key] = emap.get("typeField")
        bind_vars[tv_key] = emap.get("typeValue")
        sub_filters.append(f"{inner_var}[@{tf_key}] == @{tv_key}")

    props = start_node.oC_Properties()
    if props is not None:
        map_lit = props.oC_MapLiteral()
        if map_lit is not None:
            keys = map_lit.oC_PropertyKeyName() or []
            vals = map_lit.oC_Expression() or []
            for k_ctx, v_ctx in zip(keys, vals, strict=False):
                k = k_ctx.getText().strip()
                val = _compile_expression(v_ctx, bind_vars)
                sub_filters.append(f"{inner_var}.{k} == {val}")

    where_ctx = om.oC_Where()
    if where_ctx is not None:
        wf = _compile_where(where_ctx.oC_Expression(), bind_vars)
        wf = re.sub(rf"\b{re.escape(u_var)}\b", inner_var, wf)
        sub_filters.append(wf)

    coll_ref = _aql_collection_ref(coll_key)
    sub_lines = [f"FOR {inner_var} IN {coll_ref}"]
    for sf in sub_filters:
        sub_lines.append(f"  FILTER {sf}")
    sub_lines.append("  LIMIT 1")
    sub_lines.append(f"  RETURN {inner_var}")
    subquery = "\n    ".join(sub_lines)

    let_var = _pick_fresh_var(u_var, forbidden_vars=forbidden_vars)
    lines.append(f"  LET {let_var} = FIRST(\n    {subquery}\n  )")
    extra_env[u_var] = let_var


def _compile_optional_with_chains(
    om: CypherParser.OC_MatchContext,
    start_node: Any,
    chains: list[Any],
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    lines: list[str],
    extra_env: dict[str, str],
) -> None:
    """Compile OPTIONAL MATCH with one or more relationship chains."""
    u_var, _ = _extract_node_var_and_labels(start_node, default_var="n")
    if u_var not in forbidden_vars:
        raise CoreError(
            f"OPTIONAL MATCH start variable '{u_var}' is not bound",
            code="UNSUPPORTED",
        )

    # For multi-segment, we chain traversals inside the subquery
    current_start = u_var
    sub_lines: list[str] = []
    all_filters: list[str] = []
    all_target_vars: list[tuple[str, str]] = []  # (cypher_var, inner_var)
    all_rel_vars: list[tuple[str, str, Any]] = []  # (cypher_var, inner_var, rel_pat)

    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid OPTIONAL MATCH pattern", code="UNSUPPORTED")

        v_var, v_labels = _extract_node_var_and_labels(v_node, default_var="v")
        rel_type, rel_var, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="r")
        direction = _relationship_direction(rel_pat)

        inner_v = _pick_fresh_var(f"{v_var}_0", forbidden_vars=forbidden_vars)
        inner_r = _pick_fresh_var(f"{rel_var}_0", forbidden_vars=forbidden_vars)

        r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
        edge_key = _pick_bind_key("@edgeCollection", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
        if not isinstance(bind_vars[edge_key], str) or not bind_vars[edge_key]:
            raise CoreError(
                f"Invalid relationship mapping collection for: {rel_type}",
                code="INVALID_MAPPING",
            )

        hop_filters: list[str] = []
        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(_strip_label_backticks(v_primary))
            skip_coll_filter = resolver.edge_constrains_target(rel_type, v_primary, direction)
            if not skip_coll_filter:
                vcoll_key = _pick_bind_key("vCollection", bind_vars)
                bind_vars[vcoll_key] = v_map.get("collectionName")
                hop_filters.append(f"IS_SAME_COLLECTION(@{vcoll_key}, {inner_v})")
            v_style = v_map.get("style")
            if v_style == "LABEL":
                vtf_key = _pick_bind_key("vTypeField", bind_vars)
                vtv_key = _pick_bind_key("vTypeValue", bind_vars)
                bind_vars[vtf_key] = v_map.get("typeField")
                bind_vars[vtv_key] = v_map.get("typeValue")
                hop_filters.append(f"{inner_v}[@{vtf_key}] == @{vtv_key}")

        r_style = r_map.get("style")
        if r_style == "GENERIC_WITH_TYPE":
            rtf_key = _pick_bind_key("relTypeField", bind_vars)
            rtv_key = _pick_bind_key("relTypeValue", bind_vars)
            bind_vars[rtf_key] = r_map.get("typeField")
            bind_vars[rtv_key] = r_map.get("typeValue")
            hop_filters.append(f"{inner_r}[@{rtf_key}] == @{rtv_key}")

        # Inline property filters on target node
        props = v_node.oC_Properties()
        if props is not None:
            map_lit = props.oC_MapLiteral()
            if map_lit is not None:
                keys = map_lit.oC_PropertyKeyName() or []
                vals = map_lit.oC_Expression() or []
                for k_ctx, v_ctx in zip(keys, vals, strict=False):
                    k = k_ctx.getText().strip()
                    val = _compile_expression(v_ctx, bind_vars)
                    hop_filters.append(f"{inner_v}.{k} == {val}")

        rmin, rmax = rel_range
        edge_ref = _aql_collection_ref(edge_key)
        indent = "  " * len(all_target_vars)
        sub_lines.append(
            f"{indent}FOR {inner_v}, {inner_r} IN {rmin}..{rmax} {direction} {current_start} {edge_ref}"
        )
        for hf in hop_filters:
            sub_lines.append(f"{indent}  FILTER {hf}")

        current_start = inner_v
        all_target_vars.append((v_var, inner_v))

        detail = rel_pat.oC_RelationshipDetail()
        if detail is not None and detail.oC_Variable() is not None:
            all_rel_vars.append((rel_var, inner_r, rel_pat))

    where_ctx = om.oC_Where()
    if where_ctx is not None:
        wf = _compile_where(where_ctx.oC_Expression(), bind_vars)
        for cv, iv in all_target_vars:
            wf = re.sub(rf"\b{re.escape(cv)}\b", iv, wf)
        all_filters.append(wf)

    final_indent = "  " * len(all_target_vars)
    for af in all_filters:
        sub_lines.append(f"{final_indent}  FILTER {af}")

    # Build the return object with all target vars
    if len(all_target_vars) == 1:
        sub_lines.append(f"{final_indent}  RETURN {all_target_vars[0][1]}")
    else:
        ret_fields = ", ".join(f"{cv}: {iv}" for cv, iv in all_target_vars)
        for cv, iv, _ in all_rel_vars:
            ret_fields += f", {cv}: {iv}"
        sub_lines.append(f"{final_indent}  RETURN {{{ret_fields}}}")

    subquery = "\n    ".join(sub_lines)

    if len(all_target_vars) == 1:
        cv, _ = all_target_vars[0]
        let_var = _pick_fresh_var(cv, forbidden_vars=forbidden_vars)
        lines.append(f"  LET {let_var} = FIRST(\n    {subquery}\n  )")
        extra_env[cv] = let_var

        # Expose relationship var
        for rel_cv, rel_iv, _rel_p in all_rel_vars:
            rel_let = _pick_fresh_var(rel_cv, forbidden_vars=forbidden_vars)
            # Re-emit the traversal to get the edge
            r_sub = list(sub_lines)
            r_sub[-1] = f"{final_indent}  RETURN {rel_iv}"
            r_subquery = "\n    ".join(r_sub)
            lines.append(f"  LET {rel_let} = FIRST(\n    {r_subquery}\n  )")
            extra_env[rel_cv] = rel_let
    else:
        combo_var = _pick_fresh_var("_opt_combo", forbidden_vars=forbidden_vars)
        lines.append(f"  LET {combo_var} = FIRST(\n    {subquery}\n  )")
        for cv, _ in all_target_vars:
            let_var = _pick_fresh_var(cv, forbidden_vars=forbidden_vars)
            lines.append(f"  LET {let_var} = {combo_var}.{cv}")
            extra_env[cv] = let_var
        for rel_cv, _, _ in all_rel_vars:
            rel_let = _pick_fresh_var(rel_cv, forbidden_vars=forbidden_vars)
            lines.append(f"  LET {rel_let} = {combo_var}.{rel_cv}")
            extra_env[rel_cv] = rel_let


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


def _compile_match_multi_parts(
    match_ctx: CypherParser.OC_MatchContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[list[str], set[str]]:
    """Compile MATCH with multiple pattern parts (comma-separated)."""
    pattern = match_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart()
    if not parts or len(parts) < 2:
        raise CoreError("Expected multiple pattern parts", code="INTERNAL_ERROR")
    wheres = []
    wc = match_ctx.oC_Where()
    if wc is not None:
        wheres.append(wc)
    return _compile_match_multi_parts_from_parts(
        parts,
        extra_wheres=wheres,
        resolver=resolver,
        bind_vars=bind_vars,
    )


def _compile_match_multi_parts_from_parts(
    parts: list,
    *,
    extra_wheres: list[CypherParser.OC_WhereContext] | None = None,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[list[str], set[str]]:
    """Compile multiple pattern parts (from one or more MATCH clauses) into AQL."""

    lines: list[str] = []
    forbidden: set[str] = set()
    for_count = 0
    bound_labels: dict[str, str] = {}

    def indent_for_line() -> str:
        # For readability and stable goldens, we keep all FOR lines after the first
        # at a single indentation level (2 spaces), even if there are multiple loops.
        return "" if for_count == 0 else "  "

    def indent_filter_line() -> str:
        # Filters align under the most recent FOR block:
        # - after 1 FOR: 2 spaces
        # - after >=2 FORs: 4 spaces
        level = 1 if for_count <= 1 else 2
        return "  " * level

    def add_for(line: str) -> None:
        nonlocal for_count
        lines.append(f"{indent_for_line()}{line}")
        for_count += 1

    def add_filter(expr: str) -> None:
        lines.append(f"{indent_filter_line()}FILTER {expr}")

    def emit_entity_filters(
        var: str,
        labels: list[str] | None,
        *,
        via_edge: str | None = None,
        edge_direction: str | None = None,
    ) -> None:
        if not labels:
            return
        primary = _pick_primary_entity_label(labels, resolver)
        if bound_labels.get(var) == primary and len(labels) == 1:
            return
        entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
        entity_style = entity_mapping.get("style")

        skip_coll_filter = (
            via_edge is not None
            and edge_direction is not None
            and resolver.edge_constrains_target(via_edge, primary, edge_direction)
        )
        if not skip_coll_filter:
            coll_key = _pick_bind_key(f"{var}Collection", bind_vars)
            bind_vars[coll_key] = entity_mapping.get("collectionName")
            if not isinstance(bind_vars[coll_key], str) or not bind_vars[coll_key]:
                raise CoreError(
                    f"Invalid entity mapping collectionName for: {primary}", code="INVALID_MAPPING"
                )
            add_filter(f"IS_SAME_COLLECTION(@{coll_key}, {var})")

        if entity_style == "LABEL":
            tf_key = _pick_bind_key(f"{var}TypeField", bind_vars)
            tv_key = _pick_bind_key(f"{var}TypeValue", bind_vars)
            bind_vars[tf_key] = entity_mapping.get("typeField")
            bind_vars[tv_key] = entity_mapping.get("typeValue")
            add_filter(f"{var}[@{tf_key}] == @{tv_key}")
            for extra in _extra_label_filters(var, labels, primary):
                add_filter(extra.strip("()"))
        elif entity_style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING")
        elif len(labels) > 1:
            _warn_multi_label_collection(labels, primary)
        bound_labels[var] = primary

    def emit_rel_type_filter(rel_var: str, rel_type: str) -> str | None:
        r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
        r_style = r_map.get("style")
        if r_style == "GENERIC_WITH_TYPE":
            rtf_key = _pick_bind_key(f"{rel_var}TypeField", bind_vars)
            rtv_key = _pick_bind_key(f"{rel_var}TypeValue", bind_vars)
            bind_vars[rtf_key] = r_map.get("typeField")
            bind_vars[rtv_key] = r_map.get("typeValue")
            return f"{rel_var}[@{rtf_key}] == @{rtv_key}"
        if r_style == "DEDICATED_COLLECTION":
            return None
        raise CoreError(f"Unsupported relationship mapping style: {r_style}", code="INVALID_MAPPING")

    def extract_node_var_and_optional_labels(
        node_pat: CypherParser.OC_NodePatternContext, *, default_var: str
    ) -> tuple[str, list[str]]:
        var = (
            node_pat.oC_Variable().getText() if node_pat.oC_Variable() is not None else default_var
        ).strip()
        labels_ctx = node_pat.oC_NodeLabels()
        if labels_ctx is None:
            return var, []
        node_labels = labels_ctx.oC_NodeLabel() or []
        labels: list[str] = []
        for nl in node_labels:
            label = nl.oC_LabelName().getText().strip()
            if not label:
                raise CoreError("Invalid label in MATCH", code="UNSUPPORTED")
            labels.append(label)
        return var, labels

    rel_part_i = 0
    all_named_rel_vars: list[str] = []
    for idx, part in enumerate(parts):
        anon = part.oC_AnonymousPatternPart()
        if anon is None:
            raise CoreError("Named pattern parts are not supported in v0", code="UNSUPPORTED")
        elem = anon.oC_PatternElement()
        node = elem.oC_NodePattern()
        if node is None:
            raise CoreError("Only node-based pattern parts are supported in v0", code="UNSUPPORTED")
        chains = elem.oC_PatternElementChain() or []

        # Case 1: node-only pattern part: (u:Label {props})
        if not chains:
            var, labels = extract_node_var_and_optional_labels(node, default_var=f"n{idx + 1}")
            prop_filters = _compile_node_pattern_properties(node, var=var, bind_vars=bind_vars)

            if var in forbidden:
                # Join semantics: variable already bound by a previous pattern part.
                emit_entity_filters(var, labels)
                for f in prop_filters:
                    add_filter(f)
                continue

            coll_key = _pick_bind_key("@collection", bind_vars)
            if not labels:
                bind_vars[coll_key] = _infer_unlabeled_collection(resolver)
                add_for(f"FOR {var} IN {_aql_collection_ref(coll_key)}")
            else:
                primary = _pick_primary_entity_label(labels, resolver)
                entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
                entity_style = entity_mapping.get("style")

                bind_vars[coll_key] = entity_mapping.get("collectionName")
                if not isinstance(bind_vars[coll_key], str) or not bind_vars[coll_key]:
                    raise CoreError(
                        f"Invalid entity mapping collectionName for: {primary}", code="INVALID_MAPPING"
                    )
                add_for(f"FOR {var} IN {_aql_collection_ref(coll_key)}")

                if entity_style == "LABEL":
                    tf_key = _pick_bind_key(f"{var}TypeField", bind_vars)
                    tv_key = _pick_bind_key(f"{var}TypeValue", bind_vars)
                    bind_vars[tf_key] = entity_mapping.get("typeField")
                    bind_vars[tv_key] = entity_mapping.get("typeValue")
                    add_filter(f"{var}[@{tf_key}] == @{tv_key}")
                    for extra in _extra_label_filters(var, labels, primary):
                        add_filter(extra.strip("()"))
                elif entity_style != "COLLECTION":
                    raise CoreError(
                        f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING"
                    )
                elif len(labels) > 1:
                    _warn_multi_label_collection(labels, primary)

            for f in prop_filters:
                add_filter(f)

            forbidden.add(var)
            if labels:
                bound_labels[var] = primary
            continue

        # Case 2: relationship pattern part: (u)-[:T]->(v) ... (1+ hops)
        u_var, u_labels = extract_node_var_and_optional_labels(node, default_var=f"u{idx + 1}")
        u_prop_filters = _compile_node_pattern_properties(node, var=u_var, bind_vars=bind_vars)

        # Bind (or refine) start node u.
        if u_var in forbidden:
            emit_entity_filters(u_var, u_labels)
            for f in u_prop_filters:
                add_filter(f)
        else:
            ucoll_key = _pick_bind_key(f"@{u_var}Collection", bind_vars)
            add_for(f"FOR {u_var} IN {_aql_collection_ref(ucoll_key)}")
            if not u_labels:
                bind_vars[ucoll_key] = _infer_unlabeled_collection(resolver)
            else:
                u_primary = _pick_primary_entity_label(u_labels, resolver)
                u_map = resolver.resolve_entity(_strip_label_backticks(u_primary))
                u_style = u_map.get("style")
                bind_vars[ucoll_key] = u_map.get("collectionName")
                if not isinstance(bind_vars[ucoll_key], str) or not bind_vars[ucoll_key]:
                    raise CoreError(
                        f"Invalid entity mapping collectionName for: {u_primary}", code="INVALID_MAPPING"
                    )

                if u_style == "LABEL":
                    tf_key = _pick_bind_key(f"{u_var}TypeField", bind_vars)
                    tv_key = _pick_bind_key(f"{u_var}TypeValue", bind_vars)
                    bind_vars[tf_key] = u_map.get("typeField")
                    bind_vars[tv_key] = u_map.get("typeValue")
                    add_filter(f"{u_var}[@{tf_key}] == @{tv_key}")
                    for extra in _extra_label_filters(u_var, u_labels, u_primary):
                        add_filter(extra.strip("()"))
                elif u_style != "COLLECTION":
                    raise CoreError(f"Unsupported entity mapping style: {u_style}", code="INVALID_MAPPING")
                elif len(u_labels) > 1:
                    _warn_multi_label_collection(u_labels, u_primary)
            for f in u_prop_filters:
                add_filter(f)
            forbidden.add(u_var)
            if u_labels:
                bound_labels[u_var] = u_primary

        current_u = u_var
        for hop_i, chain in enumerate(chains):
            rel_pat = chain.oC_RelationshipPattern()
            v_node = chain.oC_NodePattern()
            if rel_pat is None or v_node is None:
                raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

            v_var, v_labels = extract_node_var_and_optional_labels(
                v_node, default_var=f"v{idx + 1}_{hop_i + 1}"
            )
            v_bound = v_var in forbidden
            if not v_bound and not v_labels:
                # Unlabeled new bindings are only supported when we can infer a single backing collection.
                _ = _infer_unlabeled_collection(resolver)
            v_trav = v_var if not v_bound else _pick_fresh_var(f"{v_var}_m", forbidden_vars=forbidden)

            rel_default = "r" if rel_part_i == 0 else f"r{rel_part_i + 1}"
            rel_part_i += 1
            rel_type, rel_var, rel_range = _extract_relationship_type_and_var(
                rel_pat, default_var=rel_default
            )
            detail = rel_pat.oC_RelationshipDetail()
            rel_is_named = detail is not None and detail.oC_Variable() is not None
            r_prop_filters = _compile_relationship_pattern_properties(
                rel_pat, var=rel_var, bind_vars=bind_vars
            )

            if rel_var in forbidden:
                raise CoreError(
                    "Shared relationship variables across pattern parts not supported in v0",
                    code="NOT_IMPLEMENTED",
                )

            direction = _relationship_direction(rel_pat)

            # Relationship mapping (edge collection + optional type filter)
            r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
            edge_key = _pick_bind_key("@edgeCollection", bind_vars)
            bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
            if not isinstance(bind_vars[edge_key], str) or not bind_vars[edge_key]:
                raise CoreError(
                    f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING"
                )

            rmin, rmax = rel_range
            add_for(
                f"FOR {v_trav}, {rel_var} IN {rmin}..{rmax} {direction} {current_u} {_aql_collection_ref(edge_key)}"
            )

            if v_bound:
                add_filter(f"{v_trav}._id == {v_var}._id")

            if v_labels:
                emit_entity_filters(v_trav, v_labels, via_edge=rel_type, edge_direction=direction)
            elif not v_bound:
                vcoll_key = _pick_bind_key(f"{v_trav}Collection", bind_vars)
                bind_vars[vcoll_key] = _infer_unlabeled_collection(resolver)
                add_filter(f"IS_SAME_COLLECTION(@{vcoll_key}, {v_trav})")
            for f in _compile_node_pattern_properties(v_node, var=v_trav, bind_vars=bind_vars):
                add_filter(f)

            rel_type_filter = emit_rel_type_filter(rel_var, rel_type)
            if rel_type_filter:
                add_filter(rel_type_filter)
            for f in r_prop_filters:
                add_filter(f)

            forbidden.add(rel_var)
            if rel_is_named:
                all_named_rel_vars.append(rel_var)
            if not v_bound:
                forbidden.add(v_var)
                if v_labels:
                    bound_labels[v_var] = _pick_primary_entity_label(v_labels, resolver)

            current_u = v_trav

    _emit_relationship_uniqueness(all_named_rel_vars, lines, indent=indent_filter_line())

    for wc in extra_wheres or []:
        f = _compile_where(wc.oC_Expression(), bind_vars)
        add_filter(f)

    return lines, forbidden


def _compile_match_from_bound(
    match_ctx: CypherParser.OC_MatchContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    var_env: dict[str, str],
    lines: list[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Compile a MATCH that occurs after a WITH, where the start variable is already bound.
    Supported:
    - MATCH (u) WHERE ... (filter only)
    - MATCH (u)-[:TYPE]->(v:Label) (1 hop)
    """
    pattern = match_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart()
    if not parts or len(parts) != 1:
        raise CoreError("Only a single pattern part is supported in v0", code="UNSUPPORTED")
    anon = parts[0].oC_AnonymousPatternPart()
    elem = anon.oC_PatternElement()
    node = elem.oC_NodePattern()
    if node is None:
        raise CoreError("Only node patterns are supported in v0", code="UNSUPPORTED")
    chains = elem.oC_PatternElementChain() or []

    if node.oC_Variable() is None:
        raise CoreError("MATCH after WITH must bind a variable", code="UNSUPPORTED")
    u_cy = node.oC_Variable().getText().strip()
    bound_start = u_cy in var_env
    u_aql = var_env.get(u_cy)

    # If the start is unbound, start a fresh scan over (u:Label) using the mapping.
    out_env = dict(var_env)
    rel_type_exprs: dict[str, str] = {}
    if not bound_start:
        labels_ctx = node.oC_NodeLabels()
        label: str | None = None
        if labels_ctx is not None:
            node_labels = labels_ctx.oC_NodeLabel()
            if not node_labels or len(node_labels) != 1:
                raise CoreError("MATCH after WITH requires exactly one label", code="UNSUPPORTED")
            label = node_labels[0].oC_LabelName().getText().strip()
            if not label:
                raise CoreError("Invalid label in MATCH", code="UNSUPPORTED")

        u_aql = _pick_fresh_var(u_cy, forbidden_vars=forbidden_vars)
        out_env[u_cy] = u_aql

        coll_key = _pick_bind_key("@tailCollection", bind_vars)
        tail_filters: list[str] = []
        if label is None:
            bind_vars[coll_key] = _infer_unlabeled_collection(resolver)
        else:
            entity_mapping = resolver.resolve_entity(_strip_label_backticks(label))
            entity_style = entity_mapping.get("style")
            bind_vars[coll_key] = entity_mapping.get("collectionName")
            if not isinstance(bind_vars[coll_key], str) or not bind_vars[coll_key]:
                raise CoreError(f"Invalid entity mapping collectionName for: {label}", code="INVALID_MAPPING")

            if entity_style == "LABEL":
                tf_key = _pick_bind_key("tailTypeField", bind_vars)
                tv_key = _pick_bind_key("tailTypeValue", bind_vars)
                bind_vars[tf_key] = entity_mapping.get("typeField")
                bind_vars[tv_key] = entity_mapping.get("typeValue")
                tail_filters.append(f"{u_aql}[@{tf_key}] == @{tv_key}")
            elif entity_style != "COLLECTION":
                raise CoreError(f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING")

        lines.append(f"  FOR {u_aql} IN {_aql_collection_ref(coll_key)}")
        for f in tail_filters:
            lines.append(f"    FILTER {f}")

        # Inline properties on fresh scan node.
        for f in _compile_node_pattern_properties(node, var=u_aql, bind_vars=bind_vars):
            lines.append(f"    FILTER {f}")

    # Optional WHERE filter for node-only patterns.
    if not chains:
        where_ctx = match_ctx.oC_Where()
        if where_ctx is not None:
            f = _compile_where(where_ctx.oC_Expression(), bind_vars)
            f = _rewrite_vars(f, out_env)
            lines.append(f"  FILTER {f}")
        return out_env, rel_type_exprs

    if len(chains) not in (1, 2):
        raise CoreError("Only 1-hop or 2-hop patterns are supported in v0 subset", code="UNSUPPORTED")

    current_aql = u_aql
    for hop_idx, chain in enumerate(chains):
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

        node_default = "v" if hop_idx == 0 else "w"
        rel_default = "r" if hop_idx == 0 else "r_1"
        v_cy, v_labels = _extract_node_var_and_labels(v_node, default_var=node_default)
        rel_type, rel_cy, rel_range = _extract_relationship_type_and_var(rel_pat, default_var=rel_default)

        v_aql = _pick_fresh_var(v_cy, forbidden_vars=forbidden_vars) if v_cy not in out_env else out_env[v_cy]
        r_aql = (
            _pick_fresh_var(rel_cy, forbidden_vars=forbidden_vars)
            if rel_cy not in out_env
            else out_env[rel_cy]
        )
        r_prop_filters = _compile_relationship_pattern_properties(rel_pat, var=r_aql, bind_vars=bind_vars)

        out_env[v_cy] = v_aql
        out_env[rel_cy] = r_aql

        direction = _relationship_direction(rel_pat)
        v_primary: str | None = None
        v_map: dict[str, Any] | None = None
        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(_strip_label_backticks(v_primary))
        r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))

        edge_key = _pick_bind_key("@edgeCollection", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
        if not isinstance(bind_vars[edge_key], str) or not bind_vars[edge_key]:
            raise CoreError(
                f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING"
            )

        skip_coll_filter = (
            v_primary is not None
            and rel_type is not None
            and resolver.edge_constrains_target(rel_type, v_primary, direction)
        )
        if not skip_coll_filter:
            vcoll_key = _pick_bind_key("vCollection", bind_vars)
            if v_map is None:
                bind_vars[vcoll_key] = _infer_unlabeled_collection(resolver)
            else:
                bind_vars[vcoll_key] = v_map.get("collectionName")
                if not isinstance(bind_vars[vcoll_key], str) or not bind_vars[vcoll_key]:
                    raise CoreError(
                        f"Invalid entity mapping collectionName for: {v_primary}", code="INVALID_MAPPING"
                    )

        rmin, rmax = rel_range
        lines.append(
            f"  FOR {v_aql}, {r_aql} IN {rmin}..{rmax} {direction} {current_aql} {_aql_collection_ref(edge_key)}"
        )

        v_filters: list[str] = []
        if not skip_coll_filter:
            v_filters.append(f"IS_SAME_COLLECTION(@{vcoll_key}, {v_aql})")
        if v_map is not None and v_primary is not None:
            v_style = v_map.get("style")
            if v_style == "LABEL":
                vtf_key = _pick_bind_key("vTypeField", bind_vars)
                vtv_key = _pick_bind_key("vTypeValue", bind_vars)
                bind_vars[vtf_key] = v_map.get("typeField")
                bind_vars[vtv_key] = v_map.get("typeValue")
                v_filters.append(f"{v_aql}[@{vtf_key}] == @{vtv_key}")
                v_filters.extend(_extra_label_filters(v_aql, v_labels, v_primary))
            elif v_style != "COLLECTION":
                raise CoreError(f"Unsupported entity mapping style: {v_style}", code="INVALID_MAPPING")

        r_filters: list[str] = []
        r_style = r_map.get("style")
        if r_style == "GENERIC_WITH_TYPE":
            rtf_key = _pick_bind_key("relTypeField", bind_vars)
            rtv_key = _pick_bind_key("relTypeValue", bind_vars)
            bind_vars[rtf_key] = r_map.get("typeField")
            bind_vars[rtv_key] = r_map.get("typeValue")
            r_filters.append(f"{r_aql}[@{rtf_key}] == @{rtv_key}")
            rel_type_exprs[rel_cy] = f"{r_aql}[@{rtf_key}]"
        elif r_style != "DEDICATED_COLLECTION":
            raise CoreError(f"Unsupported relationship mapping style: {r_style}", code="INVALID_MAPPING")
        else:
            rel_type_exprs[rel_cy] = _aql_string_literal(rel_type)

        for f in v_filters + r_filters:
            lines.append(f"    FILTER {f}")
        for f in r_prop_filters:
            lines.append(f"    FILTER {f}")

        # Inline properties on traversed node.
        for f in _compile_node_pattern_properties(v_node, var=v_aql, bind_vars=bind_vars):
            lines.append(f"    FILTER {f}")

        current_aql = v_aql

    where_ctx = match_ctx.oC_Where()
    user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None
    if user_filter:
        user_filter = _rewrite_vars(user_filter, out_env)
        lines.append(f"    FILTER {user_filter}")

    return out_env, rel_type_exprs


def _compile_match_pipeline(
    match_ctx: CypherParser.OC_MatchContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[list[str], set[str]]:
    """
    Compile the MATCH clause into AQL lines, without emitting RETURN.
    Supports:
    - MATCH (n:Label)
    - MATCH (u:Label)-[:TYPE]->(v:Label) (1 hop)
    """
    pattern = match_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart()
    if not parts or len(parts) != 1:
        raise CoreError("Only a single pattern part is supported in v0", code="UNSUPPORTED")
    anon = parts[0].oC_AnonymousPatternPart()
    elem = anon.oC_PatternElement()
    node = elem.oC_NodePattern()
    if node is None:
        raise CoreError("Only node patterns are supported in v0", code="UNSUPPORTED")
    chains = elem.oC_PatternElementChain() or []

    # Node-only
    if not chains:
        var, labels = _extract_node_var_and_labels(node, default_var="n")
        prop_filters = _compile_node_pattern_properties(node, var=var, bind_vars=bind_vars)
        lines: list[str]
        base_filters: list[str] = []
        if not labels:
            bind_vars["@collection"] = _infer_unlabeled_collection(resolver)
            lines = [f"FOR {var} IN @@collection"]
        else:
            primary = _pick_primary_entity_label(labels, resolver)
            entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
            entity_style = entity_mapping.get("style")
            if entity_style == "COLLECTION":
                if len(labels) > 1:
                    _warn_multi_label_collection(labels, primary)
                bind_vars["@collection"] = entity_mapping["collectionName"]
                lines = [f"FOR {var} IN @@collection"]
            elif entity_style == "LABEL":
                bind_vars["@collection"] = entity_mapping["collectionName"]
                bind_vars["typeField"] = entity_mapping["typeField"]
                bind_vars["typeValue"] = entity_mapping["typeValue"]
                lines = [f"FOR {var} IN @@collection"]
                base_filters.append(f"{var}[@typeField] == @typeValue")
                base_filters.extend(_extra_label_filters(var, labels, primary))
            else:
                raise CoreError(f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING")

        where_ctx = match_ctx.oC_Where()
        user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None
        filters = list(base_filters)
        if user_filter:
            filters.append(user_filter)
        filters.extend(prop_filters)
        if labels:
            idx_hint = _build_collection_index_hint(primary, prop_filters, resolver)
            if idx_hint:
                lines.append(f"  {idx_hint}")
        for f in filters:
            lines.append(f"  FILTER {f}")
        return lines, {var}

    # Relationship pattern (1+ hops)
    u_var, u_labels = _extract_node_var_and_labels(node, default_var="u")
    u_prop_filters = _compile_node_pattern_properties(node, var=u_var, bind_vars=bind_vars)

    u_filters: list[str] = []
    if not u_labels:
        bind_vars["@uCollection"] = _infer_unlabeled_collection(resolver)
    else:
        u_primary = _pick_primary_entity_label(u_labels, resolver)
        u_map = resolver.resolve_entity(_strip_label_backticks(u_primary))

        bind_vars["@uCollection"] = u_map.get("collectionName")
        if not isinstance(bind_vars["@uCollection"], str) or not bind_vars["@uCollection"]:
            raise CoreError(f"Invalid entity mapping collectionName for: {u_primary}", code="INVALID_MAPPING")

        u_style = u_map.get("style")
        if u_style == "LABEL":
            bind_vars["uTypeField"] = u_map.get("typeField")
            bind_vars["uTypeValue"] = u_map.get("typeValue")
            u_filters.append(f"{u_var}[@uTypeField] == @uTypeValue")
            u_filters.extend(_extra_label_filters(u_var, u_labels, u_primary))
        elif u_style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {u_style}", code="INVALID_MAPPING")

    lines = [f"FOR {u_var} IN @@uCollection"]
    for f in u_filters:
        lines.append(f"  FILTER {f}")
    for f in u_prop_filters:
        lines.append(f"  FILTER {f}")

    forbidden: set[str] = {u_var}
    current_var = u_var
    last_indent = "    "
    has_varlen = False

    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

        v_var, v_labels = _extract_node_var_and_labels(v_node, default_var="v")

        v_prop_filters = _compile_node_pattern_properties(v_node, var=v_var, bind_vars=bind_vars)
        rel_type, rel_var, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="r")
        detail = rel_pat.oC_RelationshipDetail()
        rel_named = detail is not None and detail.oC_Variable() is not None
        if not rel_named and rel_var in forbidden:
            rel_var = _pick_fresh_var(rel_var, forbidden_vars=forbidden)
        elif rel_var in forbidden:
            raise CoreError("Relationship variable must not shadow node variables", code="UNSUPPORTED")
        if rel_var in {u_var, v_var}:
            raise CoreError("Relationship variable must not shadow node variables", code="UNSUPPORTED")

        r_prop_filters = _compile_relationship_pattern_properties(rel_pat, var=rel_var, bind_vars=bind_vars)
        direction = _relationship_direction(rel_pat)

        if direction == "ANY" and rel_type:
            stats_dir = resolver.preferred_traversal_direction(rel_type)
            if stats_dir:
                direction = stats_dir

        v_primary: str | None = None
        v_map: dict[str, Any] | None = None
        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(_strip_label_backticks(v_primary))
        r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))

        edge_key = _pick_bind_key("@edgeCollection", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
        if not isinstance(bind_vars[edge_key], str) or not bind_vars[edge_key]:
            raise CoreError(
                f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING"
            )

        skip_coll_filter = (
            v_primary is not None
            and rel_type is not None
            and resolver.edge_constrains_target(rel_type, v_primary, direction)
        )
        if not skip_coll_filter:
            vcoll_key = _pick_bind_key("vCollection", bind_vars)
            if v_map is None:
                bind_vars[vcoll_key] = _infer_unlabeled_collection(resolver)
            else:
                bind_vars[vcoll_key] = v_map.get("collectionName")
                if not isinstance(bind_vars[vcoll_key], str) or not bind_vars[vcoll_key]:
                    raise CoreError(
                        f"Invalid entity mapping collectionName for: {v_primary}", code="INVALID_MAPPING"
                    )

        rmin, rmax = rel_range
        if rmin != rmax:
            has_varlen = True
        lines.append(
            f"  FOR {v_var}, {rel_var} IN {rmin}..{rmax} {direction} {current_var} {_aql_collection_ref(edge_key)}"
        )

        v_filters: list[str] = []
        if not skip_coll_filter:
            v_filters.append(f"IS_SAME_COLLECTION(@{vcoll_key}, {v_var})")
        if v_map is not None and v_primary is not None:
            v_style = v_map.get("style")
            if v_style == "LABEL":
                vtf_key = _pick_bind_key("vTypeField", bind_vars)
                vtv_key = _pick_bind_key("vTypeValue", bind_vars)
                bind_vars[vtf_key] = v_map.get("typeField")
                bind_vars[vtv_key] = v_map.get("typeValue")
                v_filters.append(f"{v_var}[@{vtf_key}] == @{vtv_key}")
                v_filters.extend(_extra_label_filters(v_var, v_labels, v_primary))
            elif v_style != "COLLECTION":
                raise CoreError(f"Unsupported entity mapping style: {v_style}", code="INVALID_MAPPING")
            elif len(v_labels) > 1:
                _warn_multi_label_collection(v_labels, v_primary)

        r_filters: list[str] = []
        r_style = r_map.get("style")
        if r_style == "GENERIC_WITH_TYPE":
            rtf_key = _pick_bind_key("relTypeField", bind_vars)
            rtv_key = _pick_bind_key("relTypeValue", bind_vars)
            bind_vars[rtf_key] = r_map.get("typeField")
            bind_vars[rtv_key] = r_map.get("typeValue")
            r_filters.append(f"{rel_var}[@{rtf_key}] == @{rtv_key}")
        elif r_style != "DEDICATED_COLLECTION":
            raise CoreError(f"Unsupported relationship mapping style: {r_style}", code="INVALID_MAPPING")

        for f in v_filters + r_filters:
            lines.append(f"    FILTER {f}")
        for f in r_prop_filters:
            lines.append(f"    FILTER {f}")
        for f in v_prop_filters:
            lines.append(f"    FILTER {f}")

        forbidden.add(v_var)
        forbidden.add(rel_var)
        current_var = v_var
        last_indent = "    "

    where_ctx = match_ctx.oC_Where()
    user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None
    if user_filter:
        _emit_prune_and_filter(
            user_filter,
            current_var,
            forbidden,
            is_varlen=has_varlen,
            lines=lines,
            indent=last_indent,
        )

    return lines, forbidden


def _compile_agg_expr(expr_text: str) -> tuple[str, str] | None:
    """
    Compile simple Cypher aggregate calls used in corpus WITH:
    - count(*), count(x)
    - avg(x)
    - collect(x)
    """
    t = expr_text.strip()
    m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\((.*)\)", t)
    if not m:
        return None
    fn = m.group(1).lower()
    inner = m.group(2).strip()
    if fn == "count":
        if inner.lower().startswith("distinct"):
            inner_expr = inner[len("distinct") :].strip()
            return ("aggregate", f"COUNT_DISTINCT({inner_expr})")
        if inner == "*":
            return ("aggregate", "COUNT(1)")
        return ("aggregate", f"COUNT({inner})")
    if fn == "avg":
        return ("aggregate", f"AVG({inner})")
    if fn == "sum":
        return ("aggregate", f"SUM({inner})")
    if fn == "min":
        return ("aggregate", f"MIN({inner})")
    if fn == "max":
        return ("aggregate", f"MAX({inner})")
    if fn == "collect":
        # Use COLLECT ... INTO for list collection; ArangoDB 3.11 doesn't accept PUSH() in AGGREGATE.
        return ("into", inner)
    return None


def _apply_with(
    with_ctx: CypherParser.OC_WithContext,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    incoming_env: dict[str, str] | None = None,
) -> dict[str, str]:
    proj = with_ctx.oC_ProjectionBody()
    distinct = proj.DISTINCT() is not None

    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem()
    if not items:
        raise CoreError("WITH projection items required", code="UNSUPPORTED")

    compiled_nonagg: list[tuple[str, str, str]] = []  # (cypher_var, aql_var, expr)
    compiled_agg: list[tuple[str, str, str]] = []  # (cypher_var, aql_var, agg_expr)
    compiled_into: tuple[str, str, str] | None = None  # (cypher_var, aql_var, expr)
    pre_existing = set(forbidden_vars)

    def pick_name(cypher_var: str) -> str:
        if cypher_var not in forbidden_vars:
            return cypher_var
        i = 1
        while f"{cypher_var}_{i}" in forbidden_vars:
            i += 1
        return f"{cypher_var}_{i}"

    for it in items:
        expr_ctx = it.oC_Expression()
        expr_txt = expr_ctx.getText().strip()
        expr_txt_for_agg = _rewrite_vars(expr_txt, incoming_env) if incoming_env else expr_txt
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None

        agg = _compile_agg_expr(expr_txt_for_agg)
        if agg is not None:
            kind, agg_expr = agg
            cypher_var = (
                alias or _infer_key(expr_txt) or f"expr{len(compiled_nonagg) + len(compiled_agg) + 1}"
            )
            aql_var = pick_name(cypher_var)
            forbidden_vars.add(aql_var)
            if kind == "into":
                if compiled_into is not None:
                    raise CoreError("Only one collect(...) is supported in v0", code="NOT_IMPLEMENTED")
                compiled_into = (cypher_var, aql_var, agg_expr)
            else:
                compiled_agg.append((cypher_var, aql_var, agg_expr))
            continue

        expr = _compile_expression(expr_ctx, bind_vars)
        if incoming_env:
            expr = _rewrite_vars(expr, incoming_env)
        cypher_var = alias or _infer_key(expr) or f"expr{len(compiled_nonagg) + len(compiled_agg) + 1}"
        # If this is a pass-through (WITH u) and doesn't introduce a binding, keep it.
        if alias is None and cypher_var == expr:
            compiled_nonagg.append((cypher_var, cypher_var, expr))
            continue

        aql_var = cypher_var if cypher_var not in forbidden_vars else pick_name(cypher_var)
        forbidden_vars.add(aql_var)
        compiled_nonagg.append((cypher_var, aql_var, expr))

    # If this WITH performs a COLLECT (aggregation), any non-aggregate projected variables
    # will be (re)bound by COLLECT, so they must not collide with pre-existing vars.
    if compiled_agg or compiled_into is not None:
        adjusted: list[tuple[str, str, str]] = []
        for cypher_var, aql_var, expr in compiled_nonagg:
            if aql_var in pre_existing:
                new_name = pick_name(cypher_var)
                forbidden_vars.add(new_name)
                adjusted.append((cypher_var, new_name, expr))
            else:
                adjusted.append((cypher_var, aql_var, expr))
        compiled_nonagg = adjusted

    where_ctx = with_ctx.oC_Where()
    with_filter_raw = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None

    order_ctx = proj.oC_Order()
    skip_value, limit_value = _parse_skip_limit(proj, bind_vars)

    env: dict[str, str] = {cy: aql for cy, aql, _ in compiled_nonagg}
    env.update({cy: aql for cy, aql, _ in compiled_agg})
    if compiled_into is not None:
        cy, aql, _expr = compiled_into
        env[cy] = aql

    # Aggregation path (AGGREGATE and/or INTO)
    if compiled_agg or compiled_into is not None:
        group_parts = ", ".join(f"{aql} = {expr}" for _, aql, expr in compiled_nonagg)

        if compiled_into is not None:
            if compiled_agg:
                raise CoreError(
                    "collect(...) cannot be mixed with other aggregates in v0", code="NOT_IMPLEMENTED"
                )
            cy, aql, expr = compiled_into
            if group_parts:
                lines.append(f"  COLLECT {group_parts} INTO {aql} = {expr}")
            else:
                lines.append(f"  COLLECT INTO {aql} = {expr}")
        else:
            agg_parts = ", ".join(f"{aql} = {expr}" for _, aql, expr in compiled_agg)
            if group_parts and agg_parts:
                lines.append(f"  COLLECT {group_parts} AGGREGATE {agg_parts}")
            elif agg_parts:
                lines.append(f"  COLLECT AGGREGATE {agg_parts}")
            else:
                lines.append(f"  COLLECT {group_parts}")

        with_filter = _rewrite_vars(with_filter_raw, env) if with_filter_raw else None
        if with_filter:
            lines.append(f"  FILTER {with_filter}")
        if order_ctx is not None:
            lines.append("  " + _compile_order_by(order_ctx, bind_vars, var_env=env))
        _append_skip_limit(lines, skip_value, limit_value)
        return env

    # Non-aggregation path: LETs + (optional) DISTINCT (COLLECT)
    if distinct:
        collect_parts = ", ".join(f"{aql} = {expr}" for _, aql, expr in compiled_nonagg)
        lines.append(f"  COLLECT {collect_parts}")
        with_filter = _rewrite_vars(with_filter_raw, env) if with_filter_raw else None
        if with_filter:
            lines.append(f"  FILTER {with_filter}")
    else:
        for _, aql, expr in compiled_nonagg:
            if aql != expr:
                lines.append(f"  LET {aql} = {expr}")
        with_filter = _rewrite_vars(with_filter_raw, env) if with_filter_raw else None
        if with_filter:
            lines.append(f"  FILTER {with_filter}")

    if order_ctx is not None:
        lines.append("  " + _compile_order_by(order_ctx, bind_vars, var_env=env))
    _append_skip_limit(lines, skip_value, limit_value)

    return env


def _compile_order_by(
    order_ctx: CypherParser.OC_OrderContext,
    bind_vars: dict[str, Any],
    *,
    var_env: dict[str, str] | None = None,
) -> str:
    items = order_ctx.oC_SortItem() or []
    if not items:
        raise CoreError("ORDER BY requires at least one sort item", code="UNSUPPORTED")
    parts: list[str] = []
    for it in items:
        expr = _compile_expression(it.oC_Expression(), bind_vars)
        if var_env:
            expr = _rewrite_vars(expr, var_env)
        if it.DESC() is not None or it.DESCENDING() is not None:
            parts.append(f"{expr} DESC")
        elif it.ASC() is not None or it.ASCENDING() is not None:
            parts.append(f"{expr} ASC")
        else:
            parts.append(expr)
    return "SORT " + ", ".join(parts)


def _append_return(
    proj: CypherParser.OC_ProjectionBodyContext,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    var_env: dict[str, str] | None = None,
    rel_type_exprs: dict[str, str] | None = None,
) -> None:
    distinct = proj.DISTINCT() is not None
    order_ctx = proj.oC_Order()

    skip_value, limit_value = _parse_skip_limit(proj, bind_vars)

    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem()
    if not items:
        raise CoreError("RETURN items required", code="UNSUPPORTED")

    # --- Aggregation detection ---
    has_agg = False
    for it in items:
        expr_txt = it.oC_Expression().getText().strip()
        if var_env:
            expr_txt = _rewrite_vars(expr_txt, var_env)
        if _compile_agg_expr(expr_txt) is not None:
            has_agg = True
            break

    if has_agg:
        _append_return_aggregation(
            items,
            lines=lines,
            bind_vars=bind_vars,
            var_env=var_env,
            order_ctx=order_ctx,
            skip_value=skip_value,
            limit_value=limit_value,
        )
        return

    # --- Non-aggregation path ---
    compiled_items: list[tuple[str | None, str]] = []
    for it in items:
        expr_ctx = it.oC_Expression()
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None
        expr_txt = expr_ctx.getText().strip()
        if rel_type_exprs and expr_txt.lower().startswith("type(") and expr_txt.endswith(")"):
            inner = expr_txt[5:-1].strip()
            if inner in rel_type_exprs:
                compiled_items.append((alias or "type", rel_type_exprs[inner]))
                continue

        expr = _compile_expression(expr_ctx, bind_vars)
        if var_env:
            expr = _rewrite_vars(expr, var_env)
        compiled_items.append((alias, expr))

    if distinct:
        if len(compiled_items) == 1:
            alias, expr = compiled_items[0]
            col_var = alias or _infer_key(expr) or "value"
            lines.append(f"  COLLECT {col_var} = {expr}")
            if order_ctx is not None:
                lines.append(f"  SORT {col_var}")
            _append_skip_limit(lines, skip_value, limit_value)
            lines.append(f"  RETURN {col_var}")
        else:
            if order_ctx is not None:
                lines.append("  " + _compile_order_by(order_ctx, bind_vars, var_env=var_env))
            _append_skip_limit(lines, skip_value, limit_value)
            lines.append("  RETURN DISTINCT " + _compile_return_object(compiled_items))
        return

    if order_ctx is not None:
        lines.append("  " + _compile_order_by(order_ctx, bind_vars, var_env=var_env))
    _append_skip_limit(lines, skip_value, limit_value)

    if len(compiled_items) == 1 and compiled_items[0][0] is None:
        lines.append(f"  RETURN {compiled_items[0][1]}")
        return

    lines.append("  RETURN " + _compile_return_object(compiled_items))


def _append_return_aggregation(
    items: list,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    var_env: dict[str, str] | None = None,
    order_ctx: Any = None,
    skip_value: str | None = None,
    limit_value: str | None = None,
) -> None:
    compiled_nonagg: list[tuple[str, str]] = []
    compiled_agg: list[tuple[str, str]] = []
    compiled_into: tuple[str, str] | None = None

    for it in items:
        expr_ctx = it.oC_Expression()
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None
        expr_txt = expr_ctx.getText().strip()
        if var_env:
            expr_txt = _rewrite_vars(expr_txt, var_env)

        agg = _compile_agg_expr(expr_txt)
        if agg is not None:
            kind, agg_expr = agg
            if kind == "into":
                fn_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\(", expr_txt)
                fn_key = fn_match.group(1).lower() if fn_match else None
                idx = len(compiled_nonagg) + len(compiled_agg) + 1
                var_name = alias or fn_key or f"expr{idx}"
                if compiled_into is not None:
                    raise CoreError("Only one collect() is supported in RETURN", code="NOT_IMPLEMENTED")
                compiled_into = (var_name, agg_expr)
                continue
            fn_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\(", expr_txt)
            fn_key = fn_match.group(1).lower() if fn_match else None
            idx = len(compiled_nonagg) + len(compiled_agg) + 1
            var_name = alias or fn_key or f"expr{idx}"
            compiled_agg.append((var_name, agg_expr))
            continue

        expr = _compile_expression(expr_ctx, bind_vars)
        if var_env:
            expr = _rewrite_vars(expr, var_env)
        var_name = alias or _infer_key(expr) or f"expr{len(compiled_nonagg) + len(compiled_agg) + 1}"
        compiled_nonagg.append((var_name, expr))

    group_parts = ", ".join(f"{v} = {e}" for v, e in compiled_nonagg)
    agg_parts = ", ".join(f"{v} = {e}" for v, e in compiled_agg)

    if compiled_into is not None:
        if compiled_agg:
            raise CoreError(
                "collect() cannot be mixed with other aggregates in RETURN", code="NOT_IMPLEMENTED"
            )
        into_var, into_expr = compiled_into
        if group_parts:
            lines.append(f"  COLLECT {group_parts} INTO {into_var} = {into_expr}")
        else:
            lines.append(f"  COLLECT INTO {into_var} = {into_expr}")
    elif group_parts and agg_parts:
        lines.append(f"  COLLECT {group_parts} AGGREGATE {agg_parts}")
    elif agg_parts:
        lines.append(f"  COLLECT AGGREGATE {agg_parts}")
    else:
        lines.append(f"  COLLECT {group_parts}")

    agg_env = {v: v for v, _ in compiled_nonagg}
    agg_env.update({v: v for v, _ in compiled_agg})
    if compiled_into is not None:
        agg_env[compiled_into[0]] = compiled_into[0]

    # Map each grouped Cypher expression (e.g. ``p.name``) to its COLLECT
    # alias.  After ``COLLECT name = p.name`` the original Cypher variable
    # ``p`` is out of scope in AQL, so a Cypher ORDER BY referring to
    # ``p.name`` must be rewritten to the alias ``name``.
    for var_name, expr in compiled_nonagg:
        if expr not in agg_env:
            agg_env[expr] = var_name

    if order_ctx is not None:
        lines.append("  " + _compile_order_by(order_ctx, bind_vars, var_env=agg_env))
    _append_skip_limit(lines, skip_value, limit_value)

    all_items = compiled_nonagg + compiled_agg
    if compiled_into is not None:
        all_items.append(compiled_into)
    if len(all_items) == 1:
        lines.append(f"  RETURN {all_items[0][0]}")
    else:
        parts = ", ".join(f"{v}: {v}" for v, _ in all_items)
        lines.append(f"  RETURN {{{parts}}}")


def _infer_key(expr: str) -> str | None:
    # Best-effort key inference for `n.prop.subprop` → `subprop`
    if "." in expr and all(part.strip() for part in expr.split(".")):
        return expr.split(".")[-1]
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expr):
        return expr
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\(", expr)
    if m:
        return m.group(1)
    return None


def _compile_return_object(compiled_items: list[tuple[str | None, str]]) -> str:
    obj_parts: list[str] = []
    used: set[str] = set()
    for alias, expr in compiled_items:
        key = alias or _infer_key(expr) or f"expr{len(obj_parts) + 1}"
        if key in used:
            # Prefer `u.id` → `u_id` to avoid collisions when projecting same property from multiple vars.
            m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", expr)
            if m:
                key = f"{m.group(1)}_{m.group(2)}"
        if key in used:
            i = 2
            while f"{key}_{i}" in used:
                i += 1
            key = f"{key}_{i}"
        used.add(key)
        obj_parts.append(f"{key}: {expr}")
    return "{" + ", ".join(obj_parts) + "}"


def _rewrite_vars(text: str, var_env: dict[str, str]) -> str:
    """
    Best-effort variable rewrite for post-WITH scopes.

    This is intentionally simple (word-boundary substitution) and is designed
    for the restricted v0 corpus expressions.
    """
    if not text or not var_env:
        return text
    out = text
    for k in sorted(var_env.keys(), key=len, reverse=True):
        v = var_env[k]
        if k == v:
            continue
        out = re.sub(rf"\b{re.escape(k)}\b", v, out)
    return out


def _parse_skip_limit(
    proj: CypherParser.OC_ProjectionBodyContext,
    bind_vars: dict[str, Any],
) -> tuple[str | None, str | None]:
    skip_expr_ctx = proj.oC_Skip().oC_Expression() if proj.oC_Skip() is not None else None
    limit_expr_ctx = proj.oC_Limit().oC_Expression() if proj.oC_Limit() is not None else None

    skip_value: str | None = None
    if skip_expr_ctx is not None:
        skip_value = _compile_expression(skip_expr_ctx, bind_vars)

    limit_value: str | None = None
    if limit_expr_ctx is not None:
        limit_value = _compile_expression(limit_expr_ctx, bind_vars)

    return skip_value, limit_value


def _append_skip_limit(lines: list[str], skip_value: str | None, limit_value: str | None) -> None:
    if skip_value is None and limit_value is None:
        return
    if skip_value is not None and limit_value is not None:
        lines.append(f"  LIMIT {skip_value}, {limit_value}")
        return
    if skip_value is not None:
        # SKIP without LIMIT: use a large count.
        lines.append(f"  LIMIT {skip_value}, 1000000000")
        return
    lines.append(f"  LIMIT {limit_value}")


def _extract_node_var_and_labels(
    node: CypherParser.OC_NodePatternContext, *, default_var: str
) -> tuple[str, list[str]]:
    var = (node.oC_Variable().getText() if node.oC_Variable() is not None else default_var).strip()
    labels_ctx = node.oC_NodeLabels()
    if labels_ctx is None:
        return var, []
    node_labels = labels_ctx.oC_NodeLabel() or []
    labels: list[str] = []
    for nl in node_labels:
        label = nl.oC_LabelName().getText().strip()
        if not label:
            raise CoreError("Invalid node pattern", code="UNSUPPORTED")
        labels.append(label)
    if not var:
        raise CoreError("Invalid node pattern", code="UNSUPPORTED")
    return var, labels


def _strip_label_backticks(name: str) -> str:
    """Strip a single pair of enclosing backticks from an escaped label.

    The parser preserves backtick-escaped identifiers verbatim (e.g. the AST
    text for ``MATCH (m:`Foo.Bar`)`` is `` `Foo.Bar` ``), but resolver keys
    are the raw conceptual names.  This is the canonical normalisation point
    at the resolution boundary; we deliberately do not rewrite the AST.
    """
    if len(name) >= 2 and name.startswith("`") and name.endswith("`"):
        return name[1:-1]
    return name


def _pick_primary_entity_label(labels: list[str], resolver: MappingResolver) -> str:
    """For multi-label nodes, pick a primary label that exists in the mapping.

    When cardinality statistics are available, prefer the label with the
    smallest estimated count (most selective).  Otherwise fall back to
    right-to-left ordering to align with common Neo4j conventions.
    """
    valid: list[str] = []
    last_err: CoreError | None = None
    for lab in labels:
        try:
            resolver.resolve_entity(_strip_label_backticks(lab))
            valid.append(lab)
        except CoreError as e:
            last_err = e
            if e.code != "MAPPING_NOT_FOUND":
                raise
    if not valid:
        if last_err is not None:
            raise last_err
        raise CoreError("A single label is required in v0 subset", code="UNSUPPORTED")
    if len(valid) == 1:
        return valid[0]

    best: str | None = None
    best_count: int | None = None
    for lab in valid:
        cnt = resolver.estimated_count(lab)
        if cnt is not None:
            if best_count is None or cnt < best_count:
                best = lab
                best_count = cnt
    if best is not None:
        return best
    return valid[-1]


def _extra_label_filters(var: str, labels: list[str], primary: str) -> list[str]:
    out: list[str] = []
    for lab in labels:
        if lab == primary:
            continue
        out.append(f"({_aql_string_literal(lab)} IN {var}.labels)")
    return out


def _infer_unlabeled_collection(resolver: MappingResolver) -> str:
    pm = resolver.bundle.physical_mapping
    entities = pm.get("entities") if isinstance(pm.get("entities"), dict) else {}
    if not isinstance(entities, dict) or not entities:
        raise CoreError("A single label is required in v0 subset", code="UNSUPPORTED")
    colls: set[str] = set()
    for m in entities.values():
        if isinstance(m, dict):
            c = m.get("collectionName")
            if isinstance(c, str) and c:
                colls.add(c)
    if len(colls) == 1:
        return next(iter(colls))
    raise CoreError("A single label is required in v0 subset", code="UNSUPPORTED")


def _compile_node_pattern_properties(
    node_pat: CypherParser.OC_NodePatternContext, *, var: str, bind_vars: dict[str, Any]
) -> list[str]:
    """
    Compile inline node properties in patterns, e.g. (n:User {id: "u1"}).

    v0 subset: only supports map literals with simple expressions for values.
    Parameterized maps (e.g. {..} via $param) are NOT_IMPLEMENTED.
    """
    props_ctx = node_pat.oC_Properties()
    if props_ctx is None:
        return []
    if props_ctx.oC_Parameter() is not None:
        raise CoreError("Parameterized node properties are not supported in v0", code="NOT_IMPLEMENTED")
    m = props_ctx.oC_MapLiteral()
    if m is None:
        return []
    keys = m.oC_PropertyKeyName() or []
    vals = m.oC_Expression() or []
    if len(keys) != len(vals):
        raise CoreError("Invalid node properties map", code="UNSUPPORTED")
    out: list[str] = []
    for k_ctx, v_ctx in zip(keys, vals, strict=False):
        key = k_ctx.getText().strip()
        if not key:
            raise CoreError("Invalid node properties map", code="UNSUPPORTED")
        expr = _compile_expression(v_ctx, bind_vars)
        # Use case-insensitive comparison for string literals
        if expr.startswith('"') or expr.startswith("'"):
            out.append(f"(LOWER({var}.{key}) == LOWER({expr}))")
        else:
            out.append(f"({var}.{key} == {expr})")
    return out


def _compile_relationship_pattern_properties(
    rel_pat: CypherParser.OC_RelationshipPatternContext, *, var: str, bind_vars: dict[str, Any]
) -> list[str]:
    """
    Compile inline relationship properties in patterns, e.g. -[:ACTED_IN {role: "Forrest"}]->

    v0 subset: only supports map literals with simple expressions for values.
    Parameterized properties maps are NOT_IMPLEMENTED.
    """
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        return []
    props_ctx = detail.oC_Properties()
    if props_ctx is None:
        return []
    if props_ctx.oC_Parameter() is not None:
        raise CoreError(
            "Parameterized relationship properties are not supported in v0", code="NOT_IMPLEMENTED"
        )
    m = props_ctx.oC_MapLiteral()
    if m is None:
        return []
    keys = m.oC_PropertyKeyName() or []
    vals = m.oC_Expression() or []
    if len(keys) != len(vals):
        raise CoreError("Invalid relationship properties map", code="UNSUPPORTED")
    out: list[str] = []
    for k_ctx, v_ctx in zip(keys, vals, strict=False):
        key = k_ctx.getText().strip()
        if not key:
            raise CoreError("Invalid relationship properties map", code="UNSUPPORTED")
        expr = _compile_expression(v_ctx, bind_vars)
        out.append(f"({var}.{key} == {expr})")
    return out


def _relationship_direction(rel_pat: CypherParser.OC_RelationshipPatternContext) -> str:
    left = rel_pat.oC_LeftArrowHead() is not None
    right = rel_pat.oC_RightArrowHead() is not None
    if left and not right:
        return "INBOUND"
    if right and not left:
        return "OUTBOUND"
    return "ANY"


def _extract_relationship_type_and_var(
    rel_pat: CypherParser.OC_RelationshipPatternContext, *, default_var: str
) -> tuple[str, str, tuple[int, int]]:
    """Returns (rel_type, rel_var, (min_hops, max_hops))."""
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        raise CoreError(
            "Relationship detail with a single type is required in v0 subset",
            code="UNSUPPORTED",
        )

    min_hops, max_hops = 1, 1
    range_ctx = detail.oC_RangeLiteral()
    if range_ctx is not None:
        min_hops, max_hops = _parse_range_literal(range_ctx)

    rel_var = (detail.oC_Variable().getText() if detail.oC_Variable() is not None else default_var).strip()
    types_ctx = detail.oC_RelationshipTypes()
    if types_ctx is None:
        raise CoreError("Relationship type is required in v0 subset", code="UNSUPPORTED")
    types = types_ctx.oC_RelTypeName()
    if not types or len(types) != 1:
        raise CoreError(
            "Exactly one relationship type is required in v0 subset",
            code="UNSUPPORTED",
        )
    rel_type = types[0].getText().strip()
    if not rel_type or not rel_var:
        raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")
    return rel_type, rel_var, (min_hops, max_hops)


_MAX_VLP_DEPTH = 10


def _parse_range_literal(
    range_ctx: CypherParser.OC_RangeLiteralContext,
) -> tuple[int, int]:
    """Parse ``*``, ``*2``, ``*1..3``, ``*..5`` etc. into (min, max)."""
    ints = range_ctx.oC_IntegerLiteral() or []
    has_dots = ".." in range_ctx.getText()

    if not ints and not has_dots:
        # bare ``*`` → 1..max
        return 1, _MAX_VLP_DEPTH

    if len(ints) == 1 and not has_dots:
        # ``*3`` → exactly 3 hops
        n = int(ints[0].getText())
        return n, n

    if len(ints) == 2 and has_dots:
        lo = int(ints[0].getText())
        hi = int(ints[1].getText())
        if lo > hi:
            raise CoreError(
                f"Invalid range: min ({lo}) > max ({hi})",
                code="UNSUPPORTED",
            )
        return lo, hi

    if len(ints) == 1 and has_dots:
        raw = range_ctx.getText().strip()
        n = int(ints[0].getText())
        if raw.startswith("*") and ".." in raw:
            before_dots = raw[1 : raw.index("..")]
            if before_dots.strip().isdigit():
                return n, _MAX_VLP_DEPTH
            return 1, n
        return n, _MAX_VLP_DEPTH

    return 1, _MAX_VLP_DEPTH


def _extract_interleaved_op(ctx: Any, term_index: int, valid_ops: set[str]) -> str:
    """
    In ANTLR's openCypher grammar, binary operators are interleaved between
    sub-rule children and SP tokens.  Walk the raw children to find the
    operator token that sits between term (term_index-1) and term (term_index).
    """
    term_count = 0
    for i in range(ctx.getChildCount()):
        child = ctx.getChild(i)
        if hasattr(child, "getRuleIndex"):
            term_count += 1
            if term_count > term_index:
                break
            continue
        t = child.getText().strip()
        if t in valid_ops and term_count == term_index:
            return t
    return next(iter(valid_ops))


_OBVIOUS_NON_NULL_RE = re.compile(
    r"""
    ^\s*
    (?:
        -?\d+(?:\.\d+)?           # numeric literal
        | "(?:[^"\\]|\\.)*"       # double-quoted string literal
        | '(?:[^'\\]|\\.)*'       # single-quoted string literal
        | true | false            # booleans (non-null)
    )
    \s*$
    """,
    re.VERBOSE,
)


def _is_obvious_non_null(expr: str) -> bool:
    """True when *expr* is a textual form that AQL evaluates as non-null.

    Used to suppress redundant ``!= null`` guards on comparison operands
    that are clearly numeric/string/boolean literals.
    """
    return bool(_OBVIOUS_NON_NULL_RE.match(expr))


def _aql_string_literal(value: str) -> str:
    # Minimal safe string literal for AQL (double-quoted).
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _compile_type_of_relationship(
    rel_type: str, rel_var: str, rel_style: str | None, bind_vars: dict[str, Any]
) -> str:
    if rel_style == "GENERIC_WITH_TYPE":
        # We already bind relTypeField when needed for filtering.
        if "relTypeField" not in bind_vars:
            raise CoreError("relTypeField missing for GENERIC_WITH_TYPE", code="INVALID_MAPPING")
        return f"{rel_var}[@relTypeField]"
    return _aql_string_literal(rel_type)


def _compile_where(expr_ctx: CypherParser.OC_ExpressionContext, bind_vars: dict[str, Any]) -> str:
    return _compile_expression(expr_ctx, bind_vars)


def _compile_subquery_body(
    sq_ctx: CypherParser.OC_SubqueryBodyContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile the inner SubqueryBody of an EXISTS{} or COUNT{} subquery to AQL.

    The inner query is expected to contain MATCH clauses with a relationship
    pattern.  We compile it into an AQL subquery expression (the FOR ...
    RETURN part, without the outer LENGTH wrapper).
    """
    resolver = _active_resolver.get()
    if resolver is None:
        raise CoreError("Subquery requires a mapping resolver", code="UNSUPPORTED")

    reading_clauses = sq_ctx.oC_ReadingClause() or []
    if not reading_clauses:
        raise CoreError("EXISTS/COUNT subquery must contain a MATCH clause", code="UNSUPPORTED")

    match_clauses: list[CypherParser.OC_MatchContext] = []
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is not None:
            match_clauses.append(m)

    if not match_clauses:
        raise CoreError("EXISTS/COUNT subquery must contain a MATCH clause", code="UNSUPPORTED")

    match_ctx = match_clauses[0]
    pattern = match_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart() or []
    if not parts:
        raise CoreError("Empty MATCH pattern in subquery", code="UNSUPPORTED")

    pp = parts[0]
    anon = pp.oC_AnonymousPatternPart()
    if anon is None:
        raise CoreError("Named patterns not supported in subquery", code="NOT_IMPLEMENTED")
    pe = anon.oC_PatternElement()

    start_node = pe.oC_NodePattern()
    chains = pe.oC_PatternElementChain() or []
    if not chains:
        raise CoreError("Subquery MATCH requires a relationship pattern", code="UNSUPPORTED")

    start_var, start_labels = _extract_node_var_and_labels(start_node, default_var="_sq_start")
    chain = chains[0]
    rel_pat = chain.oC_RelationshipPattern()
    target_node = chain.oC_NodePattern()
    if rel_pat is None:
        raise CoreError("Invalid subquery pattern", code="UNSUPPORTED")

    rel_type, _, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="_sq_r")
    direction = _relationship_direction(rel_pat)

    r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
    edge_key = _pick_bind_key("@sqEdge", bind_vars)
    bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")

    sq_v = "_sq_v"
    sq_e = "_sq_e"
    target_var, t_labels = _extract_node_var_and_labels(target_node, default_var="_sq_t")
    if target_var != "_sq_t":
        sq_v = target_var

    filters: list[str] = []

    r_style = r_map.get("style")
    if r_style == "GENERIC_WITH_TYPE":
        rtf = _pick_bind_key("sqRelTF", bind_vars)
        rtv = _pick_bind_key("sqRelTV", bind_vars)
        bind_vars[rtf] = r_map.get("typeField")
        bind_vars[rtv] = r_map.get("typeValue")
        filters.append(f"{sq_e}[@{rtf}] == @{rtv}")

    if t_labels:
        t_primary = _pick_primary_entity_label(t_labels, resolver)
        try:
            t_map = resolver.resolve_entity(_strip_label_backticks(t_primary))
            if t_map.get("style") == "LABEL":
                ttf = _pick_bind_key("sqTTF", bind_vars)
                ttv = _pick_bind_key("sqTTV", bind_vars)
                bind_vars[ttf] = t_map.get("typeField")
                bind_vars[ttv] = t_map.get("typeValue")
                filters.append(f"{sq_v}[@{ttf}] == @{ttv}")
        except CoreError:
            pass

    where_ctx = match_ctx.oC_Where()
    if where_ctx is not None:
        where_cond = _compile_expression(where_ctx.oC_Expression(), bind_vars)
        filters.append(where_cond)

    rmin, rmax = rel_range
    edge_ref = _aql_collection_ref(edge_key)
    sub = f"FOR {sq_v}, {sq_e} IN {rmin}..{rmax} {direction} {start_var} {edge_ref}"
    for f in filters:
        sub += f" FILTER {f}"
    sub += " RETURN 1"

    return sub


def _compile_exists_subquery(
    ctx: CypherParser.OC_ExistsSubqueryContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile ``EXISTS { MATCH ... }`` to ``LENGTH(FOR ... RETURN 1) > 0``."""
    sq = ctx.oC_SubqueryBody()
    sub = _compile_subquery_body(sq, bind_vars)
    return f"(LENGTH({sub}) > 0)"


def _compile_count_subquery(
    ctx: CypherParser.OC_CountSubqueryContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile ``COUNT { MATCH ... }`` to ``LENGTH(FOR ... RETURN 1)``."""
    sq = ctx.oC_SubqueryBody()
    sub = _compile_subquery_body(sq, bind_vars)
    return f"LENGTH({sub})"


def _compile_pattern_predicate(
    ctx: CypherParser.OC_RelationshipsPatternContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile a pattern predicate like ``(n)-[:KNOWS]->()`` to an AQL
    existence subquery.

    Generates::

        LENGTH(FOR _pp_v, _pp_e IN 1..1 OUTBOUND n @@edge
                 FILTER ... RETURN 1) > 0
    """
    start_node = ctx.oC_NodePattern()
    chains = ctx.oC_PatternElementChain() or []
    if not chains:
        raise CoreError("Pattern predicate requires at least one relationship", code="UNSUPPORTED")

    start_var, _ = _extract_node_var_and_labels(start_node, default_var="_pp_start")
    chain = chains[0]
    rel_pat = chain.oC_RelationshipPattern()
    target_node = chain.oC_NodePattern()
    if rel_pat is None:
        raise CoreError("Invalid pattern predicate", code="UNSUPPORTED")

    rel_type, _, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="_pp_r")
    direction = _relationship_direction(rel_pat)

    resolver = _active_resolver.get()
    if resolver is None:
        raise CoreError("Pattern predicates require a mapping resolver", code="UNSUPPORTED")

    r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
    edge_key = _pick_bind_key("@ppEdge", bind_vars)
    bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")

    pp_v = "_pp_v"
    pp_e = "_pp_e"
    filters: list[str] = []

    r_style = r_map.get("style")
    if r_style == "GENERIC_WITH_TYPE":
        rtf = _pick_bind_key("ppRelTF", bind_vars)
        rtv = _pick_bind_key("ppRelTV", bind_vars)
        bind_vars[rtf] = r_map.get("typeField")
        bind_vars[rtv] = r_map.get("typeValue")
        filters.append(f"{pp_e}[@{rtf}] == @{rtv}")

    if target_node is not None:
        _, t_labels = _extract_node_var_and_labels(target_node, default_var="_pp_t")
        if t_labels:
            t_primary = _pick_primary_entity_label(t_labels, resolver)
            t_map = resolver.resolve_entity(_strip_label_backticks(t_primary))
            t_style = t_map.get("style")
            if t_style == "LABEL":
                ttf = _pick_bind_key("ppTTF", bind_vars)
                ttv = _pick_bind_key("ppTTV", bind_vars)
                bind_vars[ttf] = t_map.get("typeField")
                bind_vars[ttv] = t_map.get("typeValue")
                filters.append(f"{pp_v}[@{ttf}] == @{ttv}")

    rmin, rmax = rel_range
    edge_ref = _aql_collection_ref(edge_key)
    sub = f"FOR {pp_v}, {pp_e} IN {rmin}..{rmax} {direction} {start_var} {edge_ref}"
    for f in filters:
        sub += f" FILTER {f}"
    sub += " LIMIT 1 RETURN 1"

    return f"(LENGTH({sub}) > 0)"


def _compile_list_comprehension(
    ctx: CypherParser.OC_ListComprehensionContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile ``[x IN list WHERE cond | expr]`` to AQL subquery.

    Generates::
        (FOR x IN list FILTER cond RETURN expr)
    """
    filt_expr = ctx.oC_FilterExpression()
    id_in_coll = filt_expr.oC_IdInColl()
    var_name = id_in_coll.oC_Variable().getText().strip()
    list_expr = _compile_expression(id_in_coll.oC_Expression(), bind_vars)

    where = filt_expr.oC_Where()
    filter_clause = ""
    if where is not None:
        cond = _compile_expression(where.oC_Expression(), bind_vars)
        filter_clause = f" FILTER {cond}"

    map_expr = ctx.oC_Expression()
    if map_expr is not None:
        projection = _compile_expression(map_expr, bind_vars)
    else:
        projection = var_name

    return f"(FOR {var_name} IN {list_expr}{filter_clause} RETURN {projection})"


def _compile_pattern_comprehension(
    ctx: CypherParser.OC_PatternComprehensionContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile ``[(a)-[:REL]->(b) WHERE cond | expr]`` to AQL subquery.

    Generates::
        (FOR _pc_v, _pc_e IN 1..1 OUTBOUND a @@edge
            FILTER ... RETURN expr)
    """
    rel_pattern = ctx.oC_RelationshipsPattern()
    start_node = rel_pattern.oC_NodePattern()
    chains = rel_pattern.oC_PatternElementChain() or []
    if not chains:
        raise CoreError("Pattern comprehension requires a relationship", code="UNSUPPORTED")

    start_var, _ = _extract_node_var_and_labels(start_node, default_var="_pc_start")
    chain = chains[0]
    rel_pat = chain.oC_RelationshipPattern()
    target_node = chain.oC_NodePattern()
    if rel_pat is None:
        raise CoreError("Invalid pattern comprehension", code="UNSUPPORTED")

    rel_type, rel_var, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="_pc_r")
    direction = _relationship_direction(rel_pat)

    resolver = _active_resolver.get()
    if resolver is None:
        raise CoreError("Pattern comprehension requires a mapping resolver", code="UNSUPPORTED")

    r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
    edge_key = _pick_bind_key("@pcEdge", bind_vars)
    bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")

    target_var, t_labels = _extract_node_var_and_labels(target_node, default_var="_pc_v")
    pc_v = target_var if target_var != "_pc_v" else "_pc_v"
    pc_e = rel_var if rel_var != "_pc_r" else "_pc_e"
    filters: list[str] = []

    r_style = r_map.get("style")
    if r_style == "GENERIC_WITH_TYPE":
        rtf = _pick_bind_key("pcRelTF", bind_vars)
        rtv = _pick_bind_key("pcRelTV", bind_vars)
        bind_vars[rtf] = r_map.get("typeField")
        bind_vars[rtv] = r_map.get("typeValue")
        filters.append(f"{pc_e}[@{rtf}] == @{rtv}")

    if t_labels:
        t_primary = _pick_primary_entity_label(t_labels, resolver)
        try:
            t_map = resolver.resolve_entity(_strip_label_backticks(t_primary))
            if t_map.get("style") == "LABEL":
                ttf = _pick_bind_key("pcTTF", bind_vars)
                ttv = _pick_bind_key("pcTTV", bind_vars)
                bind_vars[ttf] = t_map.get("typeField")
                bind_vars[ttv] = t_map.get("typeValue")
                filters.append(f"{pc_v}[@{ttf}] == @{ttv}")
        except CoreError:
            pass

    all_exprs = ctx.oC_Expression()
    if not isinstance(all_exprs, list):
        all_exprs = [all_exprs] if all_exprs else []

    where_node = None
    for child in ctx.children or []:
        text = getattr(child, "symbol", None)
        if text and hasattr(text, "text") and text.text == "WHERE":
            where_node = all_exprs[0] if all_exprs else None
            break

    projection_expr = all_exprs[-1] if all_exprs else pc_v
    projection = _compile_expression(projection_expr, bind_vars) if projection_expr else pc_v

    if where_node is not None and len(all_exprs) > 1:
        where_compiled = _compile_expression(all_exprs[0], bind_vars)
        filters.append(where_compiled)
        projection = _compile_expression(all_exprs[-1], bind_vars)

    rmin, rmax = rel_range
    edge_ref = _aql_collection_ref(edge_key)
    sub = f"FOR {pc_v}, {pc_e} IN {rmin}..{rmax} {direction} {start_var} {edge_ref}"
    for f in filters:
        sub += f" FILTER {f}"
    sub += f" RETURN {projection}"

    return f"({sub})"


def _compile_case(
    ctx: CypherParser.OC_CaseExpressionContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile Cypher CASE expression to AQL.

    AQL only supports the generic form (``CASE WHEN cond THEN val ... END``),
    so the simple form (``CASE expr WHEN val THEN res ...``) is expanded by
    comparing the test expression to each WHEN value with ``==``.
    """
    alternatives = ctx.oC_CaseAlternatives() or []
    all_exprs = ctx.oC_Expression() or []
    has_else = ctx.ELSE() is not None

    # Direct oC_Expression children of the CASE node:
    #   Generic form: 0 (no ELSE) or 1 (ELSE value)
    #   Simple form:  1 (test, no ELSE) or 2 (test + ELSE value)
    is_simple = len(all_exprs) > (1 if has_else else 0)

    if is_simple:
        test_expr = _compile_expression(all_exprs[0], bind_vars)
        else_expr_idx = 1
    else:
        test_expr = None
        else_expr_idx = 0

    parts = ["CASE"]
    for alt in alternatives:
        alt_exprs = alt.oC_Expression()
        when_e = _compile_expression(alt_exprs[0], bind_vars)
        then_e = _compile_expression(alt_exprs[1], bind_vars)
        if test_expr is not None:
            parts.append(f"WHEN ({test_expr} == {when_e}) THEN {then_e}")
        else:
            parts.append(f"WHEN {when_e} THEN {then_e}")

    has_else = ctx.ELSE() is not None
    if has_else and else_expr_idx < len(all_exprs):
        else_e = _compile_expression(all_exprs[else_expr_idx], bind_vars)
        parts.append(f"ELSE {else_e}")

    parts.append("END")
    return " ".join(parts)


def _compile_expression(ctx: Any, bind_vars: dict[str, Any]) -> str:
    # We only implement what the v0.1 basic corpus needs.
    # For everything else, fail fast with a structured error.

    if isinstance(ctx, CypherParser.OC_ExpressionContext):
        return _compile_expression(ctx.oC_OrExpression(), bind_vars)

    if isinstance(ctx, CypherParser.OC_OrExpressionContext):
        parts = ctx.oC_XorExpression()
        if len(parts) == 1:
            return _compile_expression(parts[0], bind_vars)
        return "(" + " OR ".join(_compile_expression(p, bind_vars) for p in parts) + ")"

    if isinstance(ctx, CypherParser.OC_XorExpressionContext):
        parts = ctx.oC_AndExpression()
        if len(parts) == 1:
            return _compile_expression(parts[0], bind_vars)
        return "(" + " XOR ".join(_compile_expression(p, bind_vars) for p in parts) + ")"

    if isinstance(ctx, CypherParser.OC_AndExpressionContext):
        parts = ctx.oC_NotExpression()
        if len(parts) == 1:
            return _compile_expression(parts[0], bind_vars)
        return "(" + " AND ".join(_compile_expression(p, bind_vars) for p in parts) + ")"

    if isinstance(ctx, CypherParser.OC_NotExpressionContext):
        # Grammar: (NOT SP?)* comparison
        inner = _compile_expression(ctx.oC_ComparisonExpression(), bind_vars)
        n_nots = len(ctx.NOT())
        if n_nots % 2 == 1:
            return f"(NOT {inner})"
        return inner

    if isinstance(ctx, CypherParser.OC_ComparisonExpressionContext):
        left = _compile_expression(ctx.oC_AddOrSubtractExpression(), bind_vars)
        partials = ctx.oC_PartialComparisonExpression()
        if not partials:
            return left
        if len(partials) != 1:
            raise CoreError("Chained comparisons not supported in v0", code="UNSUPPORTED")
        p = partials[0]
        op = p.getChild(0).getText()
        right = _compile_expression(p.oC_AddOrSubtractExpression(), bind_vars)
        aql_op = {"=": "==", "<>": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}.get(op)
        if not aql_op:
            raise CoreError(f"Unsupported comparison op: {op}", code="UNSUPPORTED")
        # Cypher uses 3-valued logic: ordered comparisons against null
        # return null (treated as false in WHERE).  AQL coerces null to
        # the lowest sortable value, so ``null < 1950`` is true.  Guard
        # ordered comparisons explicitly so WHERE clauses match Cypher.
        # Equality/inequality already match Cypher semantics in AQL.
        if aql_op in {"<", "<=", ">", ">="}:
            guards: list[str] = []
            if not _is_obvious_non_null(left):
                guards.append(f"{left} != null")
            if not _is_obvious_non_null(right):
                guards.append(f"{right} != null")
            if guards:
                return "(" + " AND ".join(guards + [f"{left} {aql_op} {right}"]) + ")"
        return f"({left} {aql_op} {right})"

    if isinstance(ctx, CypherParser.OC_AddOrSubtractExpressionContext):
        terms = ctx.oC_MultiplyDivideModuloExpression()
        result = _compile_expression(terms[0], bind_vars)
        if len(terms) > 1:
            for i in range(1, len(terms)):
                op = _extract_interleaved_op(ctx, i, {"+", "-"})
                right = _compile_expression(terms[i], bind_vars)
                result = f"({result} {op} {right})"
        return result

    if isinstance(ctx, CypherParser.OC_MultiplyDivideModuloExpressionContext):
        terms = ctx.oC_PowerOfExpression()
        result = _compile_expression(terms[0], bind_vars)
        if len(terms) > 1:
            for i in range(1, len(terms)):
                op = _extract_interleaved_op(ctx, i, {"*", "/", "%"})
                right = _compile_expression(terms[i], bind_vars)
                result = f"({result} {op} {right})"
        return result

    if isinstance(ctx, CypherParser.OC_PowerOfExpressionContext):
        terms = ctx.oC_UnaryAddOrSubtractExpression()
        result = _compile_expression(terms[0], bind_vars)
        if len(terms) > 1:
            for i in range(1, len(terms)):
                right = _compile_expression(terms[i], bind_vars)
                result = f"POW({result}, {right})"
        return result

    if isinstance(ctx, CypherParser.OC_UnaryAddOrSubtractExpressionContext):
        inner = _compile_expression(ctx.oC_StringListNullOperatorExpression(), bind_vars)
        has_minus = False
        for i in range(ctx.getChildCount()):
            t = ctx.getChild(i).getText()
            if t == "-":
                has_minus = not has_minus
            elif t == "+":
                pass
        if has_minus:
            return f"(-{inner})"
        return inner

    if isinstance(ctx, CypherParser.OC_StringListNullOperatorExpressionContext):
        base = _compile_expression(ctx.oC_PropertyOrLabelsExpression(), bind_vars)
        # Handle null operator: IS NULL / IS NOT NULL
        null_ops = ctx.oC_NullOperatorExpression()
        if null_ops:
            if len(null_ops) != 1:
                raise CoreError("Multiple null operators not supported in v0", code="UNSUPPORTED")
            t = null_ops[0].getText().upper().replace(" ", "")
            if t == "ISNULL":
                return f"({base} == null)"
            if t == "ISNOTNULL":
                return f"({base} != null)"
            raise CoreError(f"Unsupported null operator: {t}", code="UNSUPPORTED")

        list_ops = ctx.oC_ListOperatorExpression()
        if list_ops:
            if len(list_ops) != 1:
                raise CoreError("Multiple list operators not supported in v0", code="UNSUPPORTED")
            lop = list_ops[0]
            # Only support "expr IN listExpr"
            if lop.IN() is not None:
                right = _compile_expression(lop.oC_PropertyOrLabelsExpression(), bind_vars)
                return f"({base} IN {right})"
            raise CoreError("Only IN operator is supported in v0", code="UNSUPPORTED")

        string_ops = ctx.oC_StringOperatorExpression()
        if string_ops:
            if len(string_ops) != 1:
                raise CoreError(
                    "Chained string operators not supported in v0",
                    code="UNSUPPORTED",
                )
            sop = string_ops[0]
            rhs = _compile_expression(sop.oC_PropertyOrLabelsExpression(), bind_vars)
            if sop.STARTS() is not None:
                return f"STARTS_WITH({base}, {rhs})"
            if sop.ENDS() is not None:
                return f"(RIGHT({base}, LENGTH({rhs})) == {rhs})"
            if sop.CONTAINS() is not None:
                return f"CONTAINS({base}, {rhs})"
            if sop.getText().startswith("=~") or "=~" in sop.getText():
                return f"REGEX_TEST({base}, {rhs})"
            raise CoreError("Unknown string operator", code="UNSUPPORTED")

        return base

    if isinstance(ctx, CypherParser.OC_PropertyOrLabelsExpressionContext):
        atom = _compile_expression(ctx.oC_Atom(), bind_vars)
        lookups = ctx.oC_PropertyLookup()
        for lk in lookups or []:
            key = lk.oC_PropertyKeyName().getText().strip()
            atom = f"{atom}.{key}"
        # Ignore labels suffix in v0
        return atom

    if isinstance(ctx, CypherParser.OC_AtomContext):
        if ctx.oC_Literal() is not None:
            return _compile_expression(ctx.oC_Literal(), bind_vars)
        if ctx.oC_Parameter() is not None:
            return _compile_expression(ctx.oC_Parameter(), bind_vars)
        if ctx.oC_FunctionInvocation() is not None:
            return _compile_expression(ctx.oC_FunctionInvocation(), bind_vars)
        if ctx.oC_Variable() is not None:
            return ctx.oC_Variable().getText().strip()
        if ctx.oC_ParenthesizedExpression() is not None:
            return _compile_expression(ctx.oC_ParenthesizedExpression().oC_Expression(), bind_vars)
        if ctx.oC_CaseExpression() is not None:
            return _compile_case(ctx.oC_CaseExpression(), bind_vars)
        if ctx.oC_ExistsSubquery() is not None:
            return _compile_exists_subquery(ctx.oC_ExistsSubquery(), bind_vars)
        if ctx.oC_CountSubquery() is not None:
            return _compile_count_subquery(ctx.oC_CountSubquery(), bind_vars)
        if ctx.oC_RelationshipsPattern() is not None:
            return _compile_pattern_predicate(ctx.oC_RelationshipsPattern(), bind_vars)
        if ctx.oC_ListComprehension() is not None:
            return _compile_list_comprehension(ctx.oC_ListComprehension(), bind_vars)
        if ctx.oC_PatternComprehension() is not None:
            return _compile_pattern_comprehension(ctx.oC_PatternComprehension(), bind_vars)
        # COUNT(*) aggregate
        if ctx.COUNT() is not None:
            raise CoreError("COUNT(*) subquery not supported in v0", code="NOT_IMPLEMENTED")
        raise CoreError("Unsupported atom in v0", code="UNSUPPORTED")

    if isinstance(ctx, CypherParser.OC_LiteralContext):
        if ctx.oC_BooleanLiteral() is not None:
            t = ctx.oC_BooleanLiteral().getText().lower()
            return "true" if t == "true" else "false"
        if ctx.oC_NumberLiteral() is not None:
            return ctx.oC_NumberLiteral().getText()
        if ctx.StringLiteral() is not None:
            return ctx.StringLiteral().getText()
        if ctx.NULL() is not None:
            return "null"
        if ctx.oC_ListLiteral() is not None:
            inner = ctx.oC_ListLiteral()
            exprs = inner.oC_Expression() or []
            return "[" + ",".join(_compile_expression(e, bind_vars) for e in exprs) + "]"
        if ctx.oC_MapLiteral() is not None:
            ml = ctx.oC_MapLiteral()
            keys = ml.oC_PropertyKeyName() or []
            vals = ml.oC_Expression() or []
            pairs = [
                f"{k.getText().strip()}: {_compile_expression(v, bind_vars)}"
                for k, v in zip(keys, vals, strict=False)
            ]
            return "{" + ", ".join(pairs) + "}"
        raise CoreError("Unsupported literal in v0", code="UNSUPPORTED")

    if isinstance(ctx, CypherParser.OC_ParameterContext):
        txt = ctx.getText()
        if not txt.startswith("$"):
            raise CoreError("Invalid parameter syntax", code="UNSUPPORTED")
        name = txt[1:]
        if name.isdigit():
            raise CoreError("Positional parameters not supported in v0", code="UNSUPPORTED")
        # Bind var value (if provided) is already in bind_vars; leave as-is.
        return f"@{name}"

    if isinstance(ctx, CypherParser.OC_FunctionInvocationContext):
        fn = ctx.oC_FunctionName().getText()
        fn_norm = fn.lower()
        args = ctx.oC_Expression() or []
        compiled_args = [_compile_expression(a, bind_vars) for a in args]

        # minimal function mapping for corpus
        if fn_norm == "size":
            if len(compiled_args) != 1:
                raise CoreError("size expects 1 arg", code="UNSUPPORTED")
            return f"LENGTH({compiled_args[0]})"
        if fn_norm == "tolower":
            if len(compiled_args) != 1:
                raise CoreError("toLower expects 1 arg", code="UNSUPPORTED")
            return f"LOWER({compiled_args[0]})"
        if fn_norm == "toupper":
            if len(compiled_args) != 1:
                raise CoreError("toUpper expects 1 arg", code="UNSUPPORTED")
            return f"UPPER({compiled_args[0]})"
        if fn_norm == "coalesce":
            if not compiled_args:
                raise CoreError("coalesce expects at least 1 arg", code="UNSUPPORTED")
            # ArangoDB AQL doesn't have COALESCE(); emulate Cypher coalesce by
            # picking the first non-null value.
            return f"FIRST(REMOVE_VALUES([{', '.join(compiled_args)}], null))"
        if fn_norm == "id":
            if len(compiled_args) != 1:
                raise CoreError("id expects 1 arg", code="UNSUPPORTED")
            return f"{compiled_args[0]}._id"
        if fn_norm == "keys":
            if len(compiled_args) != 1:
                raise CoreError("keys expects 1 arg", code="UNSUPPORTED")
            return f"ATTRIBUTES({compiled_args[0]})"
        if fn_norm == "properties":
            if len(compiled_args) != 1:
                raise CoreError("properties expects 1 arg", code="UNSUPPORTED")
            return f'UNSET({compiled_args[0]}, "_id", "_key", "_rev")'
        if fn_norm == "tostring":
            if len(compiled_args) != 1:
                raise CoreError("toString expects 1 arg", code="UNSUPPORTED")
            return f"TO_STRING({compiled_args[0]})"
        if fn_norm in ("tointeger", "toint"):
            if len(compiled_args) != 1:
                raise CoreError("toInteger expects 1 arg", code="UNSUPPORTED")
            return f"TO_NUMBER({compiled_args[0]})"
        if fn_norm == "tofloat":
            if len(compiled_args) != 1:
                raise CoreError("toFloat expects 1 arg", code="UNSUPPORTED")
            return f"TO_NUMBER({compiled_args[0]})"
        if fn_norm in ("toboolean", "tobool"):
            if len(compiled_args) != 1:
                raise CoreError("toBoolean expects 1 arg", code="UNSUPPORTED")
            return f"TO_BOOL({compiled_args[0]})"

        if fn_norm == "exists":
            if len(compiled_args) != 1:
                raise CoreError("exists expects 1 arg", code="UNSUPPORTED")
            return f"({compiled_args[0]} != null)"
        if fn_norm == "abs":
            if len(compiled_args) != 1:
                raise CoreError("abs expects 1 arg", code="UNSUPPORTED")
            return f"ABS({compiled_args[0]})"
        if fn_norm == "ceil":
            if len(compiled_args) != 1:
                raise CoreError("ceil expects 1 arg", code="UNSUPPORTED")
            return f"CEIL({compiled_args[0]})"
        if fn_norm == "floor":
            if len(compiled_args) != 1:
                raise CoreError("floor expects 1 arg", code="UNSUPPORTED")
            return f"FLOOR({compiled_args[0]})"
        if fn_norm == "round":
            if len(compiled_args) != 1:
                raise CoreError("round expects 1 arg", code="UNSUPPORTED")
            return f"ROUND({compiled_args[0]})"
        if fn_norm == "sign":
            if len(compiled_args) != 1:
                raise CoreError("sign expects 1 arg", code="UNSUPPORTED")
            return f"(({compiled_args[0]}) > 0 ? 1 : (({compiled_args[0]}) < 0 ? -1 : 0))"
        if fn_norm == "rand":
            return "RAND()"
        if fn_norm == "length":
            if len(compiled_args) != 1:
                raise CoreError("length expects 1 arg", code="UNSUPPORTED")
            pvars = _active_path_vars.get()
            path_arg = args[0].getText().strip() if args else compiled_args[0]
            if path_arg in pvars:
                return f"LENGTH({path_arg}.edges)"
            return f"LENGTH({compiled_args[0]})"
        if fn_norm == "left":
            if len(compiled_args) != 2:
                raise CoreError("left expects 2 args", code="UNSUPPORTED")
            return f"LEFT({compiled_args[0]}, {compiled_args[1]})"
        if fn_norm == "right":
            if len(compiled_args) != 2:
                raise CoreError("right expects 2 args", code="UNSUPPORTED")
            return f"RIGHT({compiled_args[0]}, {compiled_args[1]})"
        if fn_norm == "ltrim":
            if len(compiled_args) != 1:
                raise CoreError("lTrim expects 1 arg", code="UNSUPPORTED")
            return f"LTRIM({compiled_args[0]})"
        if fn_norm == "rtrim":
            if len(compiled_args) != 1:
                raise CoreError("rTrim expects 1 arg", code="UNSUPPORTED")
            return f"RTRIM({compiled_args[0]})"
        if fn_norm == "trim":
            if len(compiled_args) != 1:
                raise CoreError("trim expects 1 arg", code="UNSUPPORTED")
            return f"TRIM({compiled_args[0]})"
        if fn_norm == "replace":
            if len(compiled_args) != 3:
                raise CoreError("replace expects 3 args", code="UNSUPPORTED")
            return f"SUBSTITUTE({compiled_args[0]}, {compiled_args[1]}, {compiled_args[2]})"
        if fn_norm == "substring":
            if len(compiled_args) == 2:
                return f"SUBSTRING({compiled_args[0]}, {compiled_args[1]})"
            if len(compiled_args) == 3:
                return f"SUBSTRING({compiled_args[0]}, {compiled_args[1]}, {compiled_args[2]})"
            raise CoreError("substring expects 2-3 args", code="UNSUPPORTED")
        if fn_norm == "reverse":
            if len(compiled_args) != 1:
                raise CoreError("reverse expects 1 arg", code="UNSUPPORTED")
            return f"REVERSE({compiled_args[0]})"
        if fn_norm == "split":
            if len(compiled_args) not in (1, 2):
                raise CoreError("split expects 1-2 args", code="UNSUPPORTED")
            return f"SPLIT({', '.join(compiled_args)})"
        if fn_norm == "nodes":
            if len(compiled_args) != 1:
                raise CoreError("nodes expects 1 arg", code="UNSUPPORTED")
            pvars = _active_path_vars.get()
            path_arg = args[0].getText().strip() if args else compiled_args[0]
            if path_arg in pvars:
                return f"{path_arg}.nodes"
            return f"{compiled_args[0]}.nodes"
        if fn_norm == "relationships" or fn_norm == "rels":
            if len(compiled_args) != 1:
                raise CoreError("relationships expects 1 arg", code="UNSUPPORTED")
            pvars = _active_path_vars.get()
            path_arg = args[0].getText().strip() if args else compiled_args[0]
            if path_arg in pvars:
                return f"{path_arg}.edges"
            return f"{compiled_args[0]}.edges"
        if fn_norm == "head":
            if len(compiled_args) != 1:
                raise CoreError("head expects 1 arg", code="UNSUPPORTED")
            return f"FIRST({compiled_args[0]})"
        if fn_norm == "last":
            if len(compiled_args) != 1:
                raise CoreError("last expects 1 arg", code="UNSUPPORTED")
            return f"LAST({compiled_args[0]})"
        if fn_norm == "tail":
            if len(compiled_args) != 1:
                raise CoreError("tail expects 1 arg", code="UNSUPPORTED")
            return f"SLICE({compiled_args[0]}, 1)"
        if fn_norm == "range":
            if len(compiled_args) == 2:
                return f"RANGE({compiled_args[0]}, {compiled_args[1]})"
            if len(compiled_args) == 3:
                return f"RANGE({compiled_args[0]}, {compiled_args[1]}, {compiled_args[2]})"
            raise CoreError("range expects 2-3 args", code="UNSUPPORTED")
        if fn_norm == "type":
            if len(compiled_args) != 1:
                raise CoreError("type expects 1 arg", code="UNSUPPORTED")
            resolver = _active_resolver.get()
            if resolver is not None:
                r_var = compiled_args[0]
                rel_type_field = resolver.bundle.physical_mapping.get(
                    "relationshipTypes",
                    {},
                ).get("defaultTypeField")
                if rel_type_field:
                    return f"{r_var}.{rel_type_field}"
            return f"PARSE_IDENTIFIER({compiled_args[0]}._id).collection"
        if fn_norm == "labels":
            if len(compiled_args) != 1:
                raise CoreError("labels expects 1 arg", code="UNSUPPORTED")
            resolver = _active_resolver.get()
            if resolver is not None:
                entity_defs = resolver.bundle.physical_mapping.get("entityLabels", {})
                for _ek, ev in entity_defs.items():
                    if ev.get("style") == "LABEL" and ev.get("typeField"):
                        tf = ev["typeField"]
                        return f"[{compiled_args[0]}.{tf}]"
            return f"[PARSE_IDENTIFIER({compiled_args[0]}._id).collection]"
        if fn_norm == "timestamp":
            return "DATE_NOW()"
        if fn_norm == "date":
            if not compiled_args:
                return "DATE_ISO8601(DATE_NOW())"
            return f"DATE_ISO8601({compiled_args[0]})"
        if fn_norm == "localdatetime":
            if not compiled_args:
                return "DATE_ISO8601(DATE_NOW())"
            return f"DATE_ISO8601({compiled_args[0]})"
        if fn_norm == "e":
            return "2.718281828459045"
        if fn_norm == "pi":
            return "PI()"
        if fn_norm == "log":
            if len(compiled_args) != 1:
                raise CoreError("log expects 1 arg", code="UNSUPPORTED")
            return f"LOG({compiled_args[0]})"
        if fn_norm == "log10":
            if len(compiled_args) != 1:
                raise CoreError("log10 expects 1 arg", code="UNSUPPORTED")
            return f"LOG10({compiled_args[0]})"
        if fn_norm == "sqrt":
            if len(compiled_args) != 1:
                raise CoreError("sqrt expects 1 arg", code="UNSUPPORTED")
            return f"SQRT({compiled_args[0]})"

        if fn_norm.startswith("arango."):
            registry = _active_registry.get()
            if registry is None:
                raise CoreError(
                    f"arango.* extension '{fn}' requires a registry "
                    f"(pass TranslateOptions(registry=...) to translate)",
                    code="EXTENSIONS_DISABLED",
                )
            return registry.compile_function(fn_norm, compiled_args, bind_vars)

        raise CoreError(f"Unsupported function in v0: {fn}", code="UNSUPPORTED")

    raise CoreError(f"Unsupported expression node: {type(ctx).__name__}", code="UNSUPPORTED")
