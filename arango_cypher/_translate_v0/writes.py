"""Write-clause compilation for the v0 translator.

This module owns CREATE, MERGE, SET, DELETE, REMOVE, and FOREACH. It keeps
lazy wrappers for helpers that still live in ``core.py`` so the package can be
split without a core <-> writes import cycle at module import time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .naming import _aql_collection_ref, _pick_bind_key, _strip_label_backticks

# Pipeline scanners used by WITH … SET/DELETE/REMOVE tails.  ``FOR x IN @@coll``
# binds ``x`` to collection bind-key ``@coll``; traversal ``FOR v, r IN … @@edge``
# binds the relationship variable ``r``; plain ``LET alias = src`` copies that
# binding so renamed WITH projections stay writable.
_FOR_COLLECTION_RE = re.compile(r"^\s*FOR\s+(\w+)\s+IN\s+@@(\w+)\s*$")
_FOR_TRAVERSAL_RE = re.compile(
    r"^\s*FOR\s+(\w+),\s*(\w+)\s+IN\s+\d+\.\.\d+\s+(?:OUTBOUND|INBOUND|ANY)\s+\w+\s+@@(\w+)\s*$"
)
_LET_ALIAS_RE = re.compile(r"^\s*LET\s+(\w+)\s+=\s+(\w+)\s*$")


def _infer_var_collections_from_pipeline(lines: list[str]) -> dict[str, str]:
    """Map AQL document variables to their collection bind keys.

    Only identity aliases are tracked (``LET x = p``).  Computed projections
    such as ``LET born = p.born`` are intentionally omitted so SET/DELETE on a
    non-document value fails closed instead of mutating the wrong collection.
    """
    out: dict[str, str] = {}
    for line in lines:
        m = _FOR_COLLECTION_RE.match(line)
        if m is not None:
            out[m.group(1)] = f"@{m.group(2)}"
            continue
        m = _FOR_TRAVERSAL_RE.match(line)
        if m is not None:
            # Vertex collection is not always a @@ bind on this line (LPG often
            # filters with IS_SAME_COLLECTION).  The edge variable is.
            out[m.group(2)] = f"@{m.group(3)}"
            continue
        m = _LET_ALIAS_RE.match(line)
        if m is not None and m.group(2) in out:
            out[m.group(1)] = out[m.group(2)]
    return out


def _append_multipart_mutate_tail(
    *,
    set_clauses: list[CypherParser.OC_SetContext],
    delete_clauses: list[CypherParser.OC_DeleteContext],
    remove_clauses: list,
    tail: CypherParser.OC_SinglePartQueryContext,
    lines: list[str],
    var_env: dict[str, str],
    bind_vars: dict[str, Any],
    resolver: MappingResolver,
) -> AqlQuery:
    """Emit SET / DELETE / REMOVE after a WITH pipeline.

    Handles ``MATCH … WITH … SET/DELETE/REMOVE … [RETURN …]``.  Collection
    targets are recovered from the already-built MATCH/WITH AQL so aliased
    document projections (``WITH p AS x``) stay bound to the source collection.
    """
    if not (set_clauses or delete_clauses or remove_clauses):
        raise CoreError(
            "SET/DELETE/REMOVE after WITH requires at least one mutating clause",
            code="UNSUPPORTED",
        )

    var_collections = _infer_var_collections_from_pipeline(lines)
    if not var_collections:
        raise CoreError(
            "SET/DELETE/REMOVE after WITH requires a document variable from MATCH",
            code="NOT_IMPLEMENTED",
        )

    def _coll_ref_for(target_var: str) -> str:
        aql_var = var_env.get(target_var, target_var)
        key = var_collections.get(aql_var) or var_collections.get(target_var)
        if key is None:
            raise CoreError(
                f"SET/DELETE/REMOVE after WITH targets {target_var!r}, which is not a "
                "MATCH-bound document variable",
                code="NOT_IMPLEMENTED",
            )
        if key not in bind_vars:
            raise CoreError(
                f"Missing collection bind for SET/DELETE/REMOVE target {target_var!r}",
                code="UNSUPPORTED",
            )
        return _aql_collection_ref(key)

    def _rewrite_target(name: str) -> str:
        return var_env.get(name, name)

    for sc in set_clauses:
        update_fields: dict[str, dict[str, str]] = {}
        for si in sc.oC_SetItem() or []:
            prop_expr = si.oC_PropertyExpression()
            if prop_expr is not None:
                atom = prop_expr.oC_Atom()
                if atom is None or atom.oC_Variable() is None:
                    raise CoreError("SET requires a property expression", code="UNSUPPORTED")
                target_var = _rewrite_target(atom.oC_Variable().getText().strip())
                lookups = prop_expr.oC_PropertyLookup() or []
                if not lookups:
                    raise CoreError("SET requires a property expression", code="UNSUPPORTED")
                prop_name = lookups[-1].oC_PropertyKeyName().getText().strip()
                val = _compile_expression(si.oC_Expression(), bind_vars)
                if var_env:
                    val = _rewrite_vars(val, var_env)
                update_fields.setdefault(target_var, {})[prop_name] = val
            else:
                si_var = si.oC_Variable()
                if si_var is None:
                    raise CoreError("Unsupported SET item after WITH", code="UNSUPPORTED")
                target_var = _rewrite_target(si_var.getText().strip())
                val = _compile_expression(si.oC_Expression(), bind_vars)
                if var_env:
                    val = _rewrite_vars(val, var_env)
                ref = _coll_ref_for(target_var)
                if "+=" in si.getText():
                    lines.append(f"  UPDATE {target_var} WITH MERGE({target_var}, {val}) IN {ref}")
                else:
                    lines.append(f"  REPLACE {target_var} WITH {val} IN {ref}")

        for target_var, fields in update_fields.items():
            pairs = ", ".join(f"{k}: {v}" for k, v in fields.items())
            lines.append(f"  UPDATE {target_var} WITH {{{pairs}}} IN {_coll_ref_for(target_var)}")

    for dc in delete_clauses:
        is_detach = dc.DETACH() is not None
        for de in dc.oC_Expression() or []:
            del_var = _compile_expression(de, bind_vars)
            if var_env:
                del_var = _rewrite_vars(del_var, var_env)
            # Expression must resolve to a simple variable for collection lookup.
            simple = del_var.strip()
            if not re.fullmatch(r"\w+", simple):
                raise CoreError(
                    "DELETE after WITH only supports a simple MATCH-bound variable",
                    code="NOT_IMPLEMENTED",
                )
            if is_detach:
                for idx, ec in enumerate(resolver.all_edge_collections()):
                    ec_key = _pick_bind_key("@detachEdge", bind_vars)
                    bind_vars[ec_key] = ec
                    ec_ref = _aql_collection_ref(ec_key)
                    de_edge = f"_de{idx}"
                    lines.append(
                        f"  LET _edgeRm{idx} = (FOR {de_edge} IN 1..1 ANY {simple} {ec_ref} "
                        f"REMOVE {de_edge} IN {ec_ref})"
                    )
            lines.append(f"  REMOVE {simple} IN {_coll_ref_for(simple)}")

    for rc in remove_clauses:
        for ri in rc.oC_RemoveItem() or []:
            prop_expr = ri.oC_PropertyExpression()
            if prop_expr is None:
                continue
            atom = prop_expr.oC_Atom()
            if atom is None or atom.oC_Variable() is None:
                raise CoreError("REMOVE requires a property expression", code="UNSUPPORTED")
            target_var = _rewrite_target(atom.oC_Variable().getText().strip())
            lookups = prop_expr.oC_PropertyLookup() or []
            if not lookups:
                raise CoreError("REMOVE requires a property expression", code="UNSUPPORTED")
            prop_name = lookups[-1].oC_PropertyKeyName().getText().strip()
            lines.append(
                f'  UPDATE {target_var} WITH UNSET({target_var}, "{prop_name}") IN {_coll_ref_for(target_var)}'
            )

    ret = tail.oC_Return()
    if ret is not None:
        _append_return(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
            var_env=var_env,
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _core_helper(name: str):
    from . import core

    return getattr(core, name)


def _compile_expression(*args, **kwargs):
    return _core_helper("_compile_expression")(*args, **kwargs)


def _compile_where(*args, **kwargs):
    return _core_helper("_compile_where")(*args, **kwargs)


def _compile_match_pipeline(*args, **kwargs):
    return _core_helper("_compile_match_pipeline")(*args, **kwargs)


def _compile_match_multi_parts_from_parts(*args, **kwargs):
    return _core_helper("_compile_match_multi_parts_from_parts")(*args, **kwargs)


def _append_return(*args, **kwargs):
    return _core_helper("_append_return")(*args, **kwargs)


def _compile_return_object(*args, **kwargs):
    return _core_helper("_compile_return_object")(*args, **kwargs)


def _pick_primary_entity_label(*args, **kwargs):
    return _core_helper("_pick_primary_entity_label")(*args, **kwargs)


def _extra_label_filters(*args, **kwargs):
    return _core_helper("_extra_label_filters")(*args, **kwargs)


def _extract_node_var_and_labels(*args, **kwargs):
    return _core_helper("_extract_node_var_and_labels")(*args, **kwargs)


def _relationship_direction(*args, **kwargs):
    return _core_helper("_relationship_direction")(*args, **kwargs)


def _extract_relationship_type_and_var(*args, **kwargs):
    return _core_helper("_extract_relationship_type_and_var")(*args, **kwargs)


def _resolve_relationship_for_pattern(*args, **kwargs):
    return _core_helper("_resolve_relationship_for_pattern")(*args, **kwargs)


def _infer_unlabeled_collection(*args, **kwargs):
    return _core_helper("_infer_unlabeled_collection")(*args, **kwargs)


def _compile_node_pattern_properties(*args, **kwargs):
    return _core_helper("_compile_node_pattern_properties")(*args, **kwargs)


def _rewrite_vars(*args, **kwargs):
    return _core_helper("_rewrite_vars")(*args, **kwargs)


def _emit_unwind_for(
    uw: CypherParser.OC_UnwindContext,
    *,
    bind_vars: dict[str, Any],
    var_env: dict[str, str],
) -> str:
    """Compile ``UNWIND <expr> AS <var>`` to an AQL ``FOR <var> IN <expr>`` line.

    Binds ``var`` into ``var_env`` and rewrites already-bound variables in the
    list expression (so ``MATCH (a) UNWIND a.items AS x`` resolves ``a``).
    """
    expr = _compile_expression(uw.oC_Expression(), bind_vars)
    if var_env:
        expr = _rewrite_vars(expr, var_env)
    var = uw.oC_Variable().getText().strip()
    var_env[var] = var
    return f"FOR {var} IN {expr}"


def _compile_write_reading_clauses(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[list[str], dict[str, str]]:
    """Compile the MATCH/UNWIND reading clauses preceding a write clause.

    Returns ``(lines, var_env)`` where ``lines`` is the ``FOR …`` prefix that
    the write (CREATE/MERGE) nests inside (sequential ``FOR``s in AQL are
    nested loops). Supports MATCH-only, UNWIND-only, and MATCH-then-UNWIND
    (a following ``UNWIND`` may iterate a matched list). ``UNWIND`` before
    ``MATCH`` and non-MATCH/UNWIND reading clauses are refused rather than
    mis-compiled.
    """
    reading_clauses = spq.oC_ReadingClause() or []
    lines: list[str] = []
    var_env: dict[str, str] = {}
    if not reading_clauses:
        return lines, var_env

    match_clauses: list[CypherParser.OC_MatchContext] = []
    unwind_clauses: list[CypherParser.OC_UnwindContext] = []
    seen_unwind = False
    for rc in reading_clauses:
        m = rc.oC_Match()
        uw = rc.oC_Unwind()
        if m is not None:
            if seen_unwind:
                raise CoreError(
                    "MATCH after UNWIND is not supported with write clauses",
                    code="NOT_IMPLEMENTED",
                )
            match_clauses.append(m)
        elif uw is not None:
            seen_unwind = True
            unwind_clauses.append(uw)
        else:
            raise CoreError(
                "Only MATCH/UNWIND reading clauses are supported with write clauses",
                code="NOT_IMPLEMENTED",
            )

    if match_clauses:
        all_parts: list = []
        extra_wheres: list = []
        for mc in match_clauses:
            all_parts.extend(mc.oC_Pattern().oC_PatternPart() or [])
            wc = mc.oC_Where()
            if wc is not None:
                extra_wheres.append(wc)
        if len(all_parts) == 1 and len(match_clauses) == 1:
            match_lines, forbidden = _compile_match_pipeline(
                match_clauses[0], resolver=resolver, bind_vars=bind_vars
            )
        else:
            match_lines, forbidden = _compile_match_multi_parts_from_parts(
                all_parts, extra_wheres=extra_wheres, resolver=resolver, bind_vars=bind_vars
            )
        lines.extend(match_lines)
        var_env = {v: v for v in forbidden}

    for uw in unwind_clauses:
        lines.append(_emit_unwind_for(uw, bind_vars=bind_vars, var_env=var_env))

    return lines, var_env


def _compile_relationship_pattern_properties(*args, **kwargs):
    return _core_helper("_compile_relationship_pattern_properties")(*args, **kwargs)


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
                # FOREACH (x IN list | CREATE (n {... x ...})) becomes
                # ``FOR x IN list  INSERT {...} IN coll``. Reuse the create
                # compiler with the loop variable in scope; no LET binding is
                # needed since nothing downstream references the new rows.
                _compile_create(
                    uc.oC_Create(),
                    resolver=resolver,
                    bind_vars=bind_vars,
                    var_env={var_name: var_name},
                    lines=lines,
                    indent="  ",
                    has_return=False,
                )
            elif uc.oC_Delete() is not None:
                _compile_foreach_delete(
                    uc.oC_Delete(),
                    var_name=var_name,
                    resolver=resolver,
                    bind_vars=bind_vars,
                    lines=lines,
                )
            else:
                raise CoreError("Unsupported clause inside FOREACH", code="UNSUPPORTED")

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _compile_foreach_delete(
    delete_ctx: CypherParser.OC_DeleteContext,
    *,
    var_name: str,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    lines: list[str],
) -> None:
    """Compile ``FOREACH (x IN list | DELETE x)`` into ``REMOVE x IN coll``.

    The deleted documents come from the iterated list, so their collection is
    inferred (same single-domain inference the read/mutating paths use) and the
    statement fails closed on a multi-collection schema. ``DETACH DELETE``
    inside FOREACH is rejected: removing incident edges per element across all
    edge collections in a nested loop is both rare and prone to ERR-1579
    collisions; use a top-level ``MATCH … DETACH DELETE`` instead.
    """
    if delete_ctx.DETACH() is not None:
        raise CoreError(
            "DETACH DELETE inside FOREACH is not supported; use a top-level MATCH … DETACH DELETE",
            code="NOT_IMPLEMENTED",
        )

    coll_key = _find_or_create_collection_bind_key(
        "@collection", _infer_unlabeled_collection(resolver), bind_vars
    )
    coll_ref = _aql_collection_ref(coll_key)
    for de in delete_ctx.oC_Expression() or []:
        del_target = _compile_expression(de, bind_vars)
        lines.append(f"  REMOVE {del_target} IN {coll_ref}")


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

    lines: list[str] = [f"FOR {var} IN @@collection"]

    if labels:
        primary = _pick_primary_entity_label(labels, resolver)
        entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
        bind_vars["@collection"] = entity_mapping["collectionName"]

        style = entity_mapping.get("style")
        if style == "LABEL":
            bind_vars["typeField"] = entity_mapping["typeField"]
            bind_vars["typeValue"] = entity_mapping["typeValue"]
            lines.append(f"  FILTER {var}[@typeField] == @typeValue")
    else:
        # Unlabeled ``MATCH (n) SET/DELETE/REMOVE …``: resolve to the single
        # domain collection using the same inference the read path uses. Fails
        # closed (CoreError) when the schema has more than one candidate
        # collection, so we never silently mutate the wrong store.
        bind_vars["@collection"] = _infer_unlabeled_collection(resolver)

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

        r_map = _resolve_relationship_for_pattern(resolver, rel_type)
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


def _translate_create_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    create_clauses: list[CypherParser.OC_CreateContext],
    set_clauses: list[CypherParser.OC_SetContext] | None = None,
    remove_clauses: list | None = None,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate a single-part query containing CREATE clause(s), optionally
    followed by SET/REMOVE on the created variables."""
    set_clauses = set_clauses or []
    remove_clauses = remove_clauses or []
    has_writes = bool(set_clauses or remove_clauses)
    ret = spq.oC_Return()

    # Maps each created variable to the bind key of the collection it was
    # inserted into, so a trailing SET/REMOVE can target the right collection.
    var_collections: dict[str, str] = {}

    # MATCH/UNWIND reading clauses become the FOR prefix the CREATE nests in
    # (``UNWIND [..] AS x CREATE (n {p: x})`` → ``FOR x IN [..] INSERT {p: x}``).
    lines, var_env = _compile_write_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars)
    indent = "  " if lines else ""

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
            # SET/REMOVE need every created var LET-bound so they can be
            # referenced, so force LET when there are trailing writes.
            has_return=ret is not None or force_let or has_writes,
            var_collections=var_collections,
        )

    if has_writes:
        _apply_create_writes(
            set_clauses,
            remove_clauses,
            var_collections=var_collections,
            bind_vars=bind_vars,
            lines=lines,
            indent=indent,
        )

    if ret is not None:
        _compile_return_for_create(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
            indent=indent,
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _apply_create_writes(
    set_clauses: list[CypherParser.OC_SetContext],
    remove_clauses: list,
    *,
    var_collections: dict[str, str],
    bind_vars: dict[str, Any],
    lines: list[str],
    indent: str,
) -> None:
    """Emit UPDATE/REPLACE/UNSET for SET/REMOVE that follow CREATE.

    Each modification is wrapped in a ``LET _w<n> = ( … )`` subquery — the same
    pattern the mutating translator uses for DETACH edge removal — so it
    coexists with the ``FIRST(INSERT …)`` create subqueries without tripping
    AQL's restriction on multiple top-level data-modification operations.
    """
    counter = 0

    def _coll_ref_for(target_var: str) -> str:
        key = var_collections.get(target_var)
        if not key:
            raise CoreError(
                f"SET/REMOVE after CREATE targets variable {target_var!r} that was not created in this query",
                code="NOT_IMPLEMENTED",
            )
        return _aql_collection_ref(key)

    def _emit(body: str) -> None:
        nonlocal counter
        lines.append(f"{indent}LET _w{counter} = ({body} RETURN NEW)")
        counter += 1

    for sc in set_clauses:
        for si in sc.oC_SetItem() or []:
            prop_expr = si.oC_PropertyExpression()
            if prop_expr is not None:
                atom = prop_expr.oC_Atom()
                target_var = atom.oC_Variable().getText().strip() if atom.oC_Variable() is not None else None
                lookups = prop_expr.oC_PropertyLookup() or []
                if target_var is None or not lookups:
                    raise CoreError("SET requires a property expression", code="UNSUPPORTED")
                prop_name = lookups[-1].oC_PropertyKeyName().getText().strip()
                val = _compile_expression(si.oC_Expression(), bind_vars)
                _emit(f"UPDATE {target_var} WITH {{{prop_name}: {val}}} IN {_coll_ref_for(target_var)}")
            else:
                si_var = si.oC_Variable()
                if si_var is None:
                    raise CoreError("Unsupported SET item after CREATE", code="UNSUPPORTED")
                target_var = si_var.getText().strip()
                val = _compile_expression(si.oC_Expression(), bind_vars)
                ref = _coll_ref_for(target_var)
                if "+=" in si.getText():
                    _emit(f"UPDATE {target_var} WITH MERGE({target_var}, {val}) IN {ref}")
                else:
                    _emit(f"REPLACE {target_var} WITH {val} IN {ref}")

    for rc in remove_clauses:
        for ri in rc.oC_RemoveItem() or []:
            prop_expr = ri.oC_PropertyExpression()
            if prop_expr is None:
                continue
            atom = prop_expr.oC_Atom()
            target_var = atom.oC_Variable().getText().strip() if atom.oC_Variable() is not None else None
            lookups = prop_expr.oC_PropertyLookup() or []
            if target_var is None or not lookups:
                raise CoreError("REMOVE requires a property expression", code="UNSUPPORTED")
            prop_name = lookups[-1].oC_PropertyKeyName().getText().strip()
            _emit(
                f'UPDATE {target_var} WITH UNSET({target_var}, "{prop_name}") IN {_coll_ref_for(target_var)}'
            )


def _compile_create(
    create_ctx: CypherParser.OC_CreateContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    var_env: dict[str, str],
    lines: list[str],
    indent: str,
    has_return: bool,
    var_collections: dict[str, str] | None = None,
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
                var_collections=var_collections,
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
                var_collections=var_collections,
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
    var_collections: dict[str, str] | None = None,
) -> None:
    """Compile a single node INSERT."""
    param_ref = _create_props_param_ref(node_ctx.oC_Properties(), bind_vars)
    props = [] if param_ref else _compile_create_props(node_ctx.oC_Properties(), bind_vars)
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

    doc = _build_insert_doc(props, extra_fields, base=param_ref)
    coll_ref = _aql_collection_ref(coll_key)
    if var_collections is not None:
        var_collections[var] = coll_key

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
    var_collections: dict[str, str] | None = None,
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

    rel_props_ctx = detail.oC_Properties()
    param_ref = _create_props_param_ref(rel_props_ctx, bind_vars)
    props = [] if param_ref else _compile_create_rel_props(rel_pat, bind_vars)
    doc = _build_insert_doc(props, extra_fields, base=param_ref)
    coll_ref = _aql_collection_ref(edge_coll_key)
    if var_collections is not None:
        var_collections[var] = edge_coll_key

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


def _create_props_param_ref(
    props_ctx: CypherParser.OC_PropertiesContext | None,
    bind_vars: dict[str, Any],
) -> str | None:
    """Return the AQL bind ref (``@name``) when a pattern's properties are a
    whole-map parameter (``CREATE (n $props)``), else ``None``."""
    if props_ctx is None:
        return None
    param = props_ctx.oC_Parameter()
    if param is None:
        return None
    return _compile_expression(param, bind_vars)


def _build_insert_doc(
    props: list[tuple[str, str]],
    extra_fields: list[str] | None = None,
    base: str | None = None,
) -> str:
    """Build an AQL object literal for INSERT.

    When ``base`` is set (a whole-map parameter like ``@props``), the explicit
    ``extra_fields``/``props`` are merged over it via ``MERGE`` so server-set
    fields (type discriminator, ``_from``/``_to``) still apply.
    """
    fields: list[str] = list(extra_fields or [])
    fields.extend(f"{k}: {v}" for k, v in props)
    inner = "{" + ", ".join(fields) + "}" if fields else "{}"
    if base:
        return base if not fields else f"MERGE({base}, {inner})"
    return inner


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
        return _translate_multi_merge_query(
            spq,
            merge_clauses=merge_clauses,
            resolver=resolver,
            bind_vars=bind_vars,
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

    var, _coll_key, coll_ref, search_doc, insert_doc, update_doc = _build_merge_node_docs(
        node, merge_ctx, resolver=resolver, bind_vars=bind_vars
    )

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
    """Compile the MATCH/UNWIND reading clauses preceding a MERGE statement.

    Emits the ``FOR …`` prefix (matched pipeline and/or ``UNWIND`` loops) that
    the UPSERT nests inside, so ``UNWIND [..] AS x MERGE (n {p: x})`` becomes
    ``FOR x IN [..] UPSERT {p: x} …`` — previously the ``UNWIND`` was silently
    dropped, leaving ``x`` unbound in the UPSERT.
    """
    read_lines, _ = _compile_write_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars)
    lines.extend(read_lines)


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
        return _translate_merge_multi_hop(
            spq,
            merge_ctx=merge_ctx,
            node=node,
            chains=chains,
            resolver=resolver,
            bind_vars=bind_vars,
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


def _translate_merge_multi_hop(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    merge_ctx: CypherParser.OC_MergeContext,
    node: CypherParser.OC_NodePatternContext,
    chains: list,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate a multi-hop relationship MERGE — ``MERGE (a)-[:R1]->(b)-[:R2]->(c)``.

    Each hop becomes its own edge UPSERT. Endpoints must be bound by a preceding
    MATCH (same requirement as single-hop relationship MERGE — the UPSERT
    references each node's ``_id``). AQL cannot write a collection twice in one
    query (ERR 1579), so each hop must map to a *distinct* edge collection; a
    repeat fails closed. ``ON CREATE``/``ON MATCH SET`` is ambiguous across hops
    and a trailing RETURN only sees the last edge, so both are rejected.
    """
    if merge_ctx.oC_MergeAction():
        raise CoreError(
            "ON CREATE/ON MATCH SET is not supported with multi-hop MERGE; use single-hop MERGE statements",
            code="NOT_IMPLEMENTED",
        )

    hops: list[tuple[Any, Any, Any]] = []
    current_node = node
    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        end_node = chain.oC_NodePattern()
        if rel_pat is None or end_node is None:
            raise CoreError("Invalid relationship MERGE pattern", code="UNSUPPORTED")
        hops.append((current_node, rel_pat, end_node))
        current_node = end_node

    lines: list[str] = []
    _compile_merge_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars, lines=lines)

    used_edge_keys: set[str] = set()
    for from_node, rel_pat, to_node in hops:
        start_var, _ = _extract_node_var_and_labels(from_node, default_var="a")
        end_var, _ = _extract_node_var_and_labels(to_node, default_var="b")

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
        edge_coll_key = _find_or_create_collection_bind_key("@edgeCollection", edge_coll_name, bind_vars)
        if edge_coll_key in used_edge_keys:
            raise CoreError(
                f"Multi-hop MERGE reuses edge collection ({edge_coll_name!r}); AQL "
                "cannot write a collection twice in one query (ERR 1579). Split "
                "into separate statements.",
                code="NOT_IMPLEMENTED",
            )
        used_edge_keys.add(edge_coll_key)
        edge_coll_ref = _aql_collection_ref(edge_coll_key)

        search_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]
        insert_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]
        if r_style == "GENERIC_WITH_TYPE":
            type_field = r_map.get("typeField", "type")
            rtv_key = _pick_bind_key("relTypeValue", bind_vars)
            bind_vars[rtv_key] = r_map.get("typeValue")
            search_fields.append(f"{type_field}: @{rtv_key}")
            insert_fields.append(f"{type_field}: @{rtv_key}")
        for k, v in _compile_create_rel_props(rel_pat, bind_vars):
            insert_fields.append(f"{k}: {v}")

        lines.append("UPSERT {" + ", ".join(search_fields) + "}")
        lines.append("INSERT {" + ", ".join(insert_fields) + "}")
        lines.append("UPDATE {}")
        lines.append(f"IN {edge_coll_ref}")

    if spq.oC_Return() is not None:
        raise CoreError(
            "RETURN after multi-hop MERGE is not supported; a trailing RETURN only sees the last edge",
            code="NOT_IMPLEMENTED",
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _build_merge_node_docs(
    node: CypherParser.OC_NodePatternContext,
    merge_ctx: CypherParser.OC_MergeContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[str, str, str, str, str, str]:
    """Build the UPSERT docs for a node MERGE.

    Returns ``(var, coll_key, coll_ref, search_doc, insert_doc, update_doc)``.
    Shared by the single- and multi-MERGE translators so both emit identical
    document literals (only the surrounding statement form differs).
    """
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
    return var, coll_key, coll_ref, search_doc, insert_doc, update_doc


def _build_merge_rel_docs(
    node: CypherParser.OC_NodePatternContext,
    chain: CypherParser.OC_PatternElementChainContext,
    merge_ctx: CypherParser.OC_MergeContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    var_ids: dict[str, str],
) -> tuple[str, str, str, str, str, str]:
    """Build the UPSERT docs for a single-hop relationship MERGE whose endpoints
    are already bound by earlier MERGE clauses in the same statement.

    Returns ``(var, edge_coll_key, edge_coll_ref, search_doc, insert_doc,
    update_doc)``. Endpoints are referenced by their bound ``_id`` (via
    ``var_ids``) rather than re-scanned, because the multi-MERGE form runs each
    element as a ``LET``-bound UPSERT.
    """
    rel_pat = chain.oC_RelationshipPattern()
    target_node = chain.oC_NodePattern()
    if rel_pat is None or target_node is None:
        raise CoreError("Invalid relationship MERGE pattern", code="UNSUPPORTED")

    start_var, _ = _extract_node_var_and_labels(node, default_var="a")
    end_var, _ = _extract_node_var_and_labels(target_node, default_var="b")
    for endpoint in (start_var, end_var):
        if endpoint not in var_ids:
            raise CoreError(
                f"MERGE relationship endpoint {endpoint!r} must be created by an "
                "earlier MERGE in the same statement (multi-MERGE does not scan "
                "matched nodes); add a MERGE for it or use a separate statement",
                code="NOT_IMPLEMENTED",
            )

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
        from_id, to_id = var_ids[end_var], var_ids[start_var]
    else:
        from_id, to_id = var_ids[start_var], var_ids[end_var]

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

    search_fields = [f"_from: {from_id}", f"_to: {to_id}"]
    insert_fields = [f"_from: {from_id}", f"_to: {to_id}"]
    if r_style == "GENERIC_WITH_TYPE":
        type_field = r_map.get("typeField", "type")
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtv_key] = r_map.get("typeValue")
        search_fields.append(f"{type_field}: @{rtv_key}")
        insert_fields.append(f"{type_field}: @{rtv_key}")

    for k, v in _compile_create_rel_props(rel_pat, bind_vars):
        insert_fields.append(f"{k}: {v}")

    on_create_fields, on_match_fields = _extract_merge_actions(merge_ctx, bind_vars)
    search_doc = "{" + ", ".join(search_fields) + "}"
    insert_doc = "{" + ", ".join(insert_fields + on_create_fields) + "}"
    update_doc = "{" + ", ".join(on_match_fields) + "}" if on_match_fields else "{}"

    rel_var_name = ""
    if detail.oC_Variable() is not None:
        rel_var_name = detail.oC_Variable().getText().strip()
    if not rel_var_name:
        rel_var_name = "r"

    return rel_var_name, edge_coll_key, edge_coll_ref, search_doc, insert_doc, update_doc


def _translate_multi_merge_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    merge_clauses: list[CypherParser.OC_MergeContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate several MERGE clauses into a sequence of ``LET``-bound UPSERTs.

    AQL forbids reading a collection after it has been modified in the same
    query (ERR 1579), so each MERGE must target a *distinct* physical
    collection; a repeat is rejected with an actionable error. Relationship
    MERGE endpoints must be bound by an earlier node MERGE in the same
    statement. Preceding MATCH is not supported here (scoping/nesting differs);
    split such queries into separate statements.
    """
    reading_clauses = spq.oC_ReadingClause() or []
    if any(rc.oC_Match() is not None for rc in reading_clauses):
        raise CoreError(
            "MATCH combined with multiple MERGE clauses is not supported; split into separate statements",
            code="NOT_IMPLEMENTED",
        )

    lines: list[str] = []
    used_collection_keys: set[str] = set()
    var_ids: dict[str, str] = {}

    def _claim(coll_key: str) -> None:
        if coll_key in used_collection_keys:
            raise CoreError(
                f"Multiple MERGE clauses target the same collection "
                f"({bind_vars.get(coll_key)!r}); AQL cannot read a collection "
                "after modifying it in the same query (ERR 1579). Split into "
                "separate statements.",
                code="NOT_IMPLEMENTED",
            )
        used_collection_keys.add(coll_key)

    for mc in merge_clauses:
        pattern_part = mc.oC_PatternPart()
        elem = pattern_part.oC_AnonymousPatternPart().oC_PatternElement()
        node = elem.oC_NodePattern()
        chains = elem.oC_PatternElementChain() or []

        if len(chains) > 1:
            raise CoreError(
                "Only single-hop relationship MERGE is supported",
                code="NOT_IMPLEMENTED",
            )

        if chains:
            var, coll_key, coll_ref, search_doc, insert_doc, update_doc = _build_merge_rel_docs(
                node, chains[0], mc, resolver=resolver, bind_vars=bind_vars, var_ids=var_ids
            )
        else:
            var, coll_key, coll_ref, search_doc, insert_doc, update_doc = _build_merge_node_docs(
                node, mc, resolver=resolver, bind_vars=bind_vars
            )

        _claim(coll_key)
        lines.append(
            f"LET {var} = FIRST(UPSERT {search_doc} INSERT {insert_doc} "
            f"UPDATE {update_doc} IN {coll_ref} RETURN NEW)"
        )
        var_ids[var] = f"{var}._id"

    ret = spq.oC_Return()
    if ret is not None:
        _compile_return_for_create(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars)
    else:
        # The LET-bound UPSERTs are subqueries, not top-level modifications, so
        # the query needs a terminal RETURN to be valid AQL. The writes still
        # execute even though the result is discarded.
        lines.append("RETURN null")

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)
