from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from arango_query_core import AqlQuery, CoreError, ExtensionPolicy, MappingBundle, MappingResolver

from ._antlr.CypherParser import CypherParser
from .parser import parse_cypher


@dataclass(frozen=True)
class TranslateOptions:
    extensions: ExtensionPolicy = ExtensionPolicy(enabled=False)


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
    _ = opts  # reserved for near-term extensions

    bind_vars: dict[str, Any] = dict(params or {})
    resolver = MappingResolver(mapping)

    pr = parse_cypher(cypher)
    tree = pr.tree

    query_ctx = tree.oC_Statement().oC_Query()
    regular = query_ctx.oC_RegularQuery()
    if regular is None:
        raise CoreError("Only regular queries are supported in v0", code="UNSUPPORTED")

    single_query = regular.oC_SingleQuery()
    mpq = single_query.oC_MultiPartQuery()
    if mpq is not None:
        return _translate_multi_part_query(mpq, resolver=resolver, bind_vars=bind_vars)

    spq = single_query.oC_SinglePartQuery()
    if spq is None:
        raise CoreError("Only single-part queries are supported in v0", code="UNSUPPORTED")

    # Fail fast on any updating clause (SET/CREATE/DELETE/etc). v0 is read-only.
    if spq.oC_UpdatingClause():
        raise CoreError("Updating clauses are not supported in v0", code="UNSUPPORTED")

    # Gather reading clauses and return.
    reading_clauses = spq.oC_ReadingClause() or []
    if not reading_clauses:
        raise CoreError("MATCH is required in v0 subset", code="UNSUPPORTED")

    match_ctx: CypherParser.OC_MatchContext | None = None
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is not None:
            if match_ctx is not None:
                raise CoreError("Multiple MATCH clauses not supported yet", code="UNSUPPORTED")
            match_ctx = m
    if match_ctx is None:
        raise CoreError("MATCH is required in v0 subset", code="UNSUPPORTED")

    pattern = match_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart()
    if not parts:
        raise CoreError("MATCH pattern is required", code="UNSUPPORTED")

    # Multi-pattern-part MATCH: MATCH (u:User), (v:User) ...
    if len(parts) > 1:
        lines, forbidden = _compile_match_multi_parts(match_ctx, resolver=resolver, bind_vars=bind_vars)
        var_env = {v: v for v in forbidden}

        ret = spq.oC_Return()
        if ret is None:
            raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
        _append_return(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars, var_env=var_env)
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    # Extract single pattern element (existing v0 behavior)
    anon = parts[0].oC_AnonymousPatternPart()
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
            entity_mapping = resolver.resolve_entity(primary)
            entity_style = entity_mapping.get("style")
            if entity_style == "COLLECTION":
                if len(labels) > 1:
                    raise CoreError("Multi-label node patterns require LABEL-style mappings in v0", code="NOT_IMPLEMENTED")
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

        ret = spq.oC_Return()
        if ret is None:
            raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")

        proj = ret.oC_ProjectionBody()
        distinct = proj.DISTINCT() is not None
        order_ctx = proj.oC_Order()

        skip_value, limit_value = _parse_skip_limit(proj)

        items_ctx = proj.oC_ProjectionItems()
        items = items_ctx.oC_ProjectionItem()
        if not items:
            raise CoreError("RETURN items required", code="UNSUPPORTED")

        compiled_items: list[tuple[str | None, str]] = []
        for it in items:
            expr_ctx = it.oC_Expression()
            expr = _compile_expression(expr_ctx, bind_vars)
            alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None
            compiled_items.append((alias, expr))

        # Build AQL
        lines: list[str] = [for_line]
        for f in filters:
            lines.append(f"  FILTER {f}")

        if distinct:
            if len(compiled_items) != 1:
                raise CoreError("DISTINCT only supported for single expression in v0", code="UNSUPPORTED")
            alias, expr = compiled_items[0]
            col_var = alias or _infer_key(expr) or "value"
            lines.append(f"  COLLECT {col_var} = {expr}")
            if order_ctx is not None:
                # After COLLECT, original vars may be out of scope.
                # In the v0 subset we only support ordering by the distinct value.
                lines.append(f"  SORT {col_var}")
            _append_skip_limit(lines, skip_value, limit_value)
            lines.append(f"  RETURN {col_var}")
            return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

        if order_ctx is not None:
            lines.append("  " + _compile_order_by(order_ctx, bind_vars))
        _append_skip_limit(lines, skip_value, limit_value)

        if len(compiled_items) == 1 and compiled_items[0][0] is None:
            lines.append(f"  RETURN {compiled_items[0][1]}")
            return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

        # Multi-item or aliased single-item: return an object.
        lines.append("  RETURN " + _compile_return_object(compiled_items))
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    # Case B: relationship pattern (1+ hops)
    u_var, u_labels = _extract_node_var_and_labels(node, default_var="u")
    u_prop_filters = _compile_node_pattern_properties(node, var=u_var, bind_vars=bind_vars)

    u_filters: list[str] = []
    if not u_labels:
        bind_vars["@uCollection"] = _infer_unlabeled_collection(resolver)
    else:
        u_primary = _pick_primary_entity_label(u_labels, resolver)
        u_map = resolver.resolve_entity(u_primary)
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
            raise CoreError("Multi-label node patterns require LABEL-style mappings in v0", code="NOT_IMPLEMENTED")

    lines = [f"FOR {u_var} IN @@uCollection"]
    for f in u_filters:
        lines.append(f"  FILTER {f}")
    for f in u_prop_filters:
        lines.append(f"  FILTER {f}")

    forbidden_vars: set[str] = {u_var}
    rel_type_exprs: dict[str, str] = {}
    current_var = u_var

    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

        # Next node
        v_var, v_labels = _extract_node_var_and_labels(v_node, default_var="v")

        # If a named node var is already bound, traverse into a temp and constrain equality by _id.
        v_bound = v_node.oC_Variable() is not None and v_var in forbidden_vars
        v_trav = v_var if not v_bound else _pick_fresh_var(f"{v_var}_m", forbidden_vars=forbidden_vars)

        v_prop_filters = _compile_node_pattern_properties(v_node, var=v_trav, bind_vars=bind_vars)

        # Relationship (type + var)
        rel_type, rel_var = _extract_relationship_type_and_var(rel_pat, default_var="r")
        detail = rel_pat.oC_RelationshipDetail()
        rel_named = detail is not None and detail.oC_Variable() is not None
        if not rel_named and rel_var in forbidden_vars:
            rel_var = _pick_fresh_var(rel_var, forbidden_vars=forbidden_vars)
        elif rel_var in forbidden_vars:
            raise CoreError("Relationship variable must not shadow node variables", code="UNSUPPORTED")
        forbidden_vars.add(rel_var)

        r_prop_filters = _compile_relationship_pattern_properties(rel_pat, var=rel_var, bind_vars=bind_vars)
        direction = _relationship_direction(rel_pat)

        # Map v + relationship
        v_primary: str | None = None
        v_map: dict[str, Any] | None = None
        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(v_primary)
        r_map = resolver.resolve_relationship(rel_type)

        edge_key = _pick_bind_key("@edgeCollection", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
        if not isinstance(bind_vars[edge_key], str) or not bind_vars[edge_key]:
            raise CoreError(f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING")

        v_filters: list[str] = []
        if v_map is None:
            if not v_bound:
                vcoll_key = _pick_bind_key("vCollection", bind_vars)
                bind_vars[vcoll_key] = _infer_unlabeled_collection(resolver)
                v_filters.append(f"IS_SAME_COLLECTION(@{vcoll_key}, {v_trav})")
        else:
            vcoll_key = _pick_bind_key("vCollection", bind_vars)
            bind_vars[vcoll_key] = v_map.get("collectionName")
            if not isinstance(bind_vars[vcoll_key], str) or not bind_vars[vcoll_key]:
                raise CoreError(f"Invalid entity mapping collectionName for: {v_primary}", code="INVALID_MAPPING")
            v_filters.append(f"IS_SAME_COLLECTION(@{vcoll_key}, {v_trav})")

        lines.append(f"  FOR {v_trav}, {rel_var} IN 1..1 {direction} {current_var} {_aql_collection_ref(edge_key)}")

        if v_bound:
            lines.append(f"    FILTER {v_trav}._id == {v_var}._id")

        if v_map is not None and v_primary is not None:
            v_style = v_map.get("style")
            if v_style == "LABEL":
                vtf_key = _pick_bind_key("vTypeField", bind_vars)
                vtv_key = _pick_bind_key("vTypeValue", bind_vars)
                bind_vars[vtf_key] = v_map.get("typeField")
                bind_vars[vtv_key] = v_map.get("typeValue")
                v_filters.append(f"{v_trav}[@{vtf_key}] == @{vtv_key}")
                v_filters.extend(_extra_label_filters(v_trav, v_labels, v_primary))
            elif v_style != "COLLECTION":
                raise CoreError(f"Unsupported entity mapping style: {v_style}", code="INVALID_MAPPING")
            elif len(v_labels) > 1:
                raise CoreError("Multi-label node patterns require LABEL-style mappings in v0", code="NOT_IMPLEMENTED")

        r_filters: list[str] = []
        r_style = r_map.get("style")
        if r_style == "GENERIC_WITH_TYPE":
            rtf_key = _pick_bind_key("relTypeField", bind_vars)
            rtv_key = _pick_bind_key("relTypeValue", bind_vars)
            bind_vars[rtf_key] = r_map.get("typeField")
            bind_vars[rtv_key] = r_map.get("typeValue")
            r_filters.append(f"{rel_var}[@{rtf_key}] == @{rtv_key}")
            rel_type_exprs[rel_var] = f"{rel_var}[@{rtf_key}]"
        elif r_style == "DEDICATED_COLLECTION":
            rel_type_exprs[rel_var] = _aql_string_literal(rel_type)
        else:
            raise CoreError(f"Unsupported relationship mapping style: {r_style}", code="INVALID_MAPPING")

        for f in v_filters + r_filters:
            lines.append(f"    FILTER {f}")
        for f in r_prop_filters:
            lines.append(f"    FILTER {f}")
        for f in v_prop_filters:
            lines.append(f"    FILTER {f}")

        # Advance current traversal variable.
        current_var = v_trav
        if not v_bound:
            forbidden_vars.add(v_var)

    # RETURN clause (shared logic, but with special-casing for type(r))
    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")

    proj = ret.oC_ProjectionBody()
    distinct = proj.DISTINCT() is not None
    order_ctx = proj.oC_Order()

    skip_value, limit_value = _parse_skip_limit(proj)

    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem()
    if not items:
        raise CoreError("RETURN items required", code="UNSUPPORTED")

    compiled_items: list[tuple[str | None, str]] = []
    for it in items:
        expr_ctx = it.oC_Expression()
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None
        expr_txt = expr_ctx.getText().strip()
        if expr_txt.lower().startswith("type(") and expr_txt.endswith(")"):
            inner = expr_txt[5:-1].strip()
            if inner in rel_type_exprs:
                compiled_items.append((alias or "type", rel_type_exprs[inner]))
                continue
        compiled_items.append((alias, _compile_expression(expr_ctx, bind_vars)))

    where_ctx = match_ctx.oC_Where()
    user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None
    if user_filter:
        lines.append(f"    FILTER {user_filter}")

    if distinct:
        if len(compiled_items) != 1:
            raise CoreError("DISTINCT only supported for single expression in v0", code="UNSUPPORTED")
        alias, expr = compiled_items[0]
        col_var = alias or _infer_key(expr) or "value"
        lines.append(f"  COLLECT {col_var} = {expr}")
        if order_ctx is not None:
            # After COLLECT, original vars may be out of scope.
            # In the v0 subset we only support ordering by the distinct value.
            lines.append(f"  SORT {col_var}")
        _append_skip_limit(lines, skip_value, limit_value)
        lines.append(f"  RETURN {col_var}")
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    if order_ctx is not None:
        lines.append("  " + _compile_order_by(order_ctx, bind_vars))
    _append_skip_limit(lines, skip_value, limit_value)

    if len(compiled_items) == 1 and compiled_items[0][0] is None:
        lines.append(f"  RETURN {compiled_items[0][1]}")
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    lines.append("  RETURN " + _compile_return_object(compiled_items))
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    raise CoreError("Unreachable", code="INTERNAL_ERROR")


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

    match_ctx: CypherParser.OC_MatchContext | None = None
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is not None:
            if match_ctx is not None:
                raise CoreError("Multiple MATCH clauses not supported yet", code="UNSUPPORTED")
            match_ctx = m
    if match_ctx is None:
        raise CoreError("MATCH is required before WITH in v0 subset", code="UNSUPPORTED")

    lines, forbidden_vars = _compile_match_pipeline(match_ctx, resolver=resolver, bind_vars=bind_vars)

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
        match_ctx: CypherParser.OC_MatchContext | None = None
        for rc in tail_reading:
            m = rc.oC_Match()
            if m is not None:
                if match_ctx is not None:
                    raise CoreError("Multiple MATCH clauses not supported yet", code="UNSUPPORTED")
                match_ctx = m
        if match_ctx is None:
            raise CoreError("Only MATCH is supported after WITH in v0 subset", code="NOT_IMPLEMENTED")
        var_env, rel_type_exprs = _compile_match_from_bound(
            match_ctx,
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
    """
    Compile MATCH with multiple pattern parts (comma-separated).
    """
    pattern = match_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart()
    if not parts or len(parts) < 2:
        raise CoreError("Expected multiple pattern parts", code="INTERNAL_ERROR")

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

    def emit_entity_filters(var: str, labels: list[str] | None) -> None:
        if not labels:
            return
        primary = _pick_primary_entity_label(labels, resolver)
        if bound_labels.get(var) == primary and len(labels) == 1:
            return
        entity_mapping = resolver.resolve_entity(primary)
        entity_style = entity_mapping.get("style")

        coll_key = _pick_bind_key(f"{var}Collection", bind_vars)
        bind_vars[coll_key] = entity_mapping.get("collectionName")
        if not isinstance(bind_vars[coll_key], str) or not bind_vars[coll_key]:
            raise CoreError(f"Invalid entity mapping collectionName for: {primary}", code="INVALID_MAPPING")

        # Always assert collection membership; helps both COLLECTION and LABEL styles.
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
            raise CoreError("Multi-label node patterns require LABEL-style mappings in v0", code="NOT_IMPLEMENTED")
        bound_labels[var] = primary

    def emit_rel_type_filter(rel_var: str, rel_type: str) -> str | None:
        r_map = resolver.resolve_relationship(rel_type)
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
        var = (node_pat.oC_Variable().getText() if node_pat.oC_Variable() is not None else default_var).strip()
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
            var, labels = extract_node_var_and_optional_labels(node, default_var=f"n{idx+1}")
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
                entity_mapping = resolver.resolve_entity(primary)
                entity_style = entity_mapping.get("style")

                bind_vars[coll_key] = entity_mapping.get("collectionName")
                if not isinstance(bind_vars[coll_key], str) or not bind_vars[coll_key]:
                    raise CoreError(f"Invalid entity mapping collectionName for: {primary}", code="INVALID_MAPPING")
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
                    raise CoreError(f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING")
                elif len(labels) > 1:
                    raise CoreError("Multi-label node patterns require LABEL-style mappings in v0", code="NOT_IMPLEMENTED")

            for f in prop_filters:
                add_filter(f)

            forbidden.add(var)
            if labels:
                bound_labels[var] = primary
            continue

        # Case 2: relationship pattern part: (u)-[:T]->(v) ... (1+ hops)
        u_var, u_labels = extract_node_var_and_optional_labels(node, default_var=f"u{idx+1}")
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
                u_map = resolver.resolve_entity(u_primary)
                u_style = u_map.get("style")
                bind_vars[ucoll_key] = u_map.get("collectionName")
                if not isinstance(bind_vars[ucoll_key], str) or not bind_vars[ucoll_key]:
                    raise CoreError(f"Invalid entity mapping collectionName for: {u_primary}", code="INVALID_MAPPING")

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
                    raise CoreError("Multi-label node patterns require LABEL-style mappings in v0", code="NOT_IMPLEMENTED")
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

            v_var, v_labels = extract_node_var_and_optional_labels(v_node, default_var=f"v{idx+1}_{hop_i+1}")
            v_bound = v_var in forbidden
            if not v_bound and not v_labels:
                # Unlabeled new bindings are only supported when we can infer a single backing collection.
                _ = _infer_unlabeled_collection(resolver)
            v_trav = v_var if not v_bound else _pick_fresh_var(f"{v_var}_m", forbidden_vars=forbidden)

            rel_default = "r" if rel_part_i == 0 else f"r{rel_part_i+1}"
            rel_part_i += 1
            rel_type, rel_var = _extract_relationship_type_and_var(rel_pat, default_var=rel_default)
            r_prop_filters = _compile_relationship_pattern_properties(rel_pat, var=rel_var, bind_vars=bind_vars)

            if rel_var in forbidden:
                raise CoreError("Shared relationship variables across pattern parts not supported in v0", code="NOT_IMPLEMENTED")

            direction = _relationship_direction(rel_pat)

            # Relationship mapping (edge collection + optional type filter)
            r_map = resolver.resolve_relationship(rel_type)
            edge_key = _pick_bind_key("@edgeCollection", bind_vars)
            bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
            if not isinstance(bind_vars[edge_key], str) or not bind_vars[edge_key]:
                raise CoreError(f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING")

            add_for(f"FOR {v_trav}, {rel_var} IN 1..1 {direction} {current_u} {_aql_collection_ref(edge_key)}")

            if v_bound:
                add_filter(f"{v_trav}._id == {v_var}._id")

            if v_labels:
                emit_entity_filters(v_trav, v_labels)
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
            if not v_bound:
                forbidden.add(v_var)
                if v_labels:
                    bound_labels[v_var] = _pick_primary_entity_label(v_labels, resolver)

            current_u = v_trav

    where_ctx = match_ctx.oC_Where()
    if where_ctx is not None:
        f = _compile_where(where_ctx.oC_Expression(), bind_vars)
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
            entity_mapping = resolver.resolve_entity(label)
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
        rel_type, rel_cy = _extract_relationship_type_and_var(rel_pat, default_var=rel_default)

        v_aql = _pick_fresh_var(v_cy, forbidden_vars=forbidden_vars) if v_cy not in out_env else out_env[v_cy]
        r_aql = _pick_fresh_var(rel_cy, forbidden_vars=forbidden_vars) if rel_cy not in out_env else out_env[rel_cy]
        r_prop_filters = _compile_relationship_pattern_properties(rel_pat, var=r_aql, bind_vars=bind_vars)

        out_env[v_cy] = v_aql
        out_env[rel_cy] = r_aql

        direction = _relationship_direction(rel_pat)
        v_primary: str | None = None
        v_map: dict[str, Any] | None = None
        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(v_primary)
        r_map = resolver.resolve_relationship(rel_type)

        edge_key = _pick_bind_key("@edgeCollection", bind_vars)
        vcoll_key = _pick_bind_key("vCollection", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
        if not isinstance(bind_vars[edge_key], str) or not bind_vars[edge_key]:
            raise CoreError(f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING")
        if v_map is None:
            bind_vars[vcoll_key] = _infer_unlabeled_collection(resolver)
        else:
            bind_vars[vcoll_key] = v_map.get("collectionName")
            if not isinstance(bind_vars[vcoll_key], str) or not bind_vars[vcoll_key]:
                raise CoreError(f"Invalid entity mapping collectionName for: {v_primary}", code="INVALID_MAPPING")

        lines.append(f"  FOR {v_aql}, {r_aql} IN 1..1 {direction} {current_aql} {_aql_collection_ref(edge_key)}")

        v_filters: list[str] = [f"IS_SAME_COLLECTION(@{vcoll_key}, {v_aql})"]
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
            entity_mapping = resolver.resolve_entity(primary)
            entity_style = entity_mapping.get("style")
            if entity_style == "COLLECTION":
                if len(labels) > 1:
                    raise CoreError("Multi-label node patterns require LABEL-style mappings in v0", code="NOT_IMPLEMENTED")
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
        u_map = resolver.resolve_entity(u_primary)

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

    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

        v_var, v_labels = _extract_node_var_and_labels(v_node, default_var="v")

        v_prop_filters = _compile_node_pattern_properties(v_node, var=v_var, bind_vars=bind_vars)
        rel_type, rel_var = _extract_relationship_type_and_var(rel_pat, default_var="r")
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

        v_primary: str | None = None
        v_map: dict[str, Any] | None = None
        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(v_primary)
        r_map = resolver.resolve_relationship(rel_type)

        edge_key = _pick_bind_key("@edgeCollection", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
        if not isinstance(bind_vars[edge_key], str) or not bind_vars[edge_key]:
            raise CoreError(f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING")

        vcoll_key = _pick_bind_key("vCollection", bind_vars)
        if v_map is None:
            bind_vars[vcoll_key] = _infer_unlabeled_collection(resolver)
        else:
            bind_vars[vcoll_key] = v_map.get("collectionName")
            if not isinstance(bind_vars[vcoll_key], str) or not bind_vars[vcoll_key]:
                raise CoreError(f"Invalid entity mapping collectionName for: {v_primary}", code="INVALID_MAPPING")

        lines.append(f"  FOR {v_var}, {rel_var} IN 1..1 {direction} {current_var} {_aql_collection_ref(edge_key)}")

        v_filters: list[str] = [f"IS_SAME_COLLECTION(@{vcoll_key}, {v_var})"]
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
                raise CoreError("Multi-label node patterns require LABEL-style mappings in v0", code="NOT_IMPLEMENTED")

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
        lines.append(f"{last_indent}FILTER {user_filter}")

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
            cypher_var = alias or _infer_key(expr_txt) or f"expr{len(compiled_nonagg)+len(compiled_agg)+1}"
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
        cypher_var = alias or _infer_key(expr) or f"expr{len(compiled_nonagg)+len(compiled_agg)+1}"
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
    skip_value, limit_value = _parse_skip_limit(proj)

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
                raise CoreError("collect(...) cannot be mixed with other aggregates in v0", code="NOT_IMPLEMENTED")
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

    skip_value, limit_value = _parse_skip_limit(proj)

    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem()
    if not items:
        raise CoreError("RETURN items required", code="UNSUPPORTED")

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
        if len(compiled_items) != 1:
            raise CoreError("DISTINCT only supported for single expression in v0", code="UNSUPPORTED")
        alias, expr = compiled_items[0]
        col_var = alias or _infer_key(expr) or "value"
        lines.append(f"  COLLECT {col_var} = {expr}")
        if order_ctx is not None:
            # After COLLECT, the original variables used by `expr` may be out of scope.
            # In the v0 subset we only support ordering by the distinct value.
            lines.append(f"  SORT {col_var}")
        _append_skip_limit(lines, skip_value, limit_value)
        lines.append(f"  RETURN {col_var}")
        return

    if order_ctx is not None:
        lines.append("  " + _compile_order_by(order_ctx, bind_vars, var_env=var_env))
    _append_skip_limit(lines, skip_value, limit_value)

    if len(compiled_items) == 1 and compiled_items[0][0] is None:
        lines.append(f"  RETURN {compiled_items[0][1]}")
        return

    lines.append("  RETURN " + _compile_return_object(compiled_items))


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
        key = alias or _infer_key(expr) or f"expr{len(obj_parts)+1}"
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


def _parse_skip_limit(proj: CypherParser.OC_ProjectionBodyContext) -> tuple[int | None, int | None]:
    skip_expr_ctx = proj.oC_Skip().oC_Expression() if proj.oC_Skip() is not None else None
    limit_expr_ctx = proj.oC_Limit().oC_Expression() if proj.oC_Limit() is not None else None

    skip_value: int | None = None
    if skip_expr_ctx is not None:
        txt = skip_expr_ctx.getText().strip()
        if not txt.isdigit():
            raise CoreError("SKIP only supports integer literal in v0", code="UNSUPPORTED")
        skip_value = int(txt)

    limit_value: int | None = None
    if limit_expr_ctx is not None:
        txt = limit_expr_ctx.getText().strip()
        if not txt.isdigit():
            raise CoreError("LIMIT only supports integer literal in v0", code="UNSUPPORTED")
        limit_value = int(txt)

    return skip_value, limit_value


def _append_skip_limit(lines: list[str], skip_value: int | None, limit_value: int | None) -> None:
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


def _pick_primary_entity_label(labels: list[str], resolver: MappingResolver) -> str:
    """
    For multi-label nodes, pick a primary label that exists in the mapping.
    We try from right-to-left to better align with common Neo4j conventions.
    """
    last_err: CoreError | None = None
    for lab in reversed(labels):
        try:
            resolver.resolve_entity(lab)
            return lab
        except CoreError as e:
            last_err = e
            if e.code != "MAPPING_NOT_FOUND":
                raise
    if last_err is not None:
        raise last_err
    raise CoreError("A single label is required in v0 subset", code="UNSUPPORTED")


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
        raise CoreError("Parameterized relationship properties are not supported in v0", code="NOT_IMPLEMENTED")
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


def _extract_node_var_and_label(node: CypherParser.OC_NodePatternContext, *, default_var: str) -> tuple[str, str]:
    var, labels = _extract_node_var_and_labels(node, default_var=default_var)
    if not labels:
        raise CoreError("A single label is required in v0 subset", code="UNSUPPORTED")
    if len(labels) != 1:
        raise CoreError("Exactly one label is required in v0 subset", code="UNSUPPORTED")
    return var, labels[0]


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
) -> tuple[str, str]:
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        raise CoreError("Relationship detail with a single type is required in v0 subset", code="UNSUPPORTED")
    if detail.oC_RangeLiteral() is not None:
        raise CoreError("Relationship ranges not supported in v0", code="UNSUPPORTED")

    rel_var = (detail.oC_Variable().getText() if detail.oC_Variable() is not None else default_var).strip()
    types_ctx = detail.oC_RelationshipTypes()
    if types_ctx is None:
        raise CoreError("Relationship type is required in v0 subset", code="UNSUPPORTED")
    types = types_ctx.oC_RelTypeName()
    if not types or len(types) != 1:
        raise CoreError("Exactly one relationship type is required in v0 subset", code="UNSUPPORTED")
    rel_type = types[0].getText().strip()
    if not rel_type or not rel_var:
        raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")
    return rel_type, rel_var


def _aql_string_literal(value: str) -> str:
    # Minimal safe string literal for AQL (double-quoted).
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _compile_type_of_relationship(rel_type: str, rel_var: str, rel_style: str | None, bind_vars: dict[str, Any]) -> str:
    if rel_style == "GENERIC_WITH_TYPE":
        # We already bind relTypeField when needed for filtering.
        if "relTypeField" not in bind_vars:
            raise CoreError("relTypeField missing for GENERIC_WITH_TYPE", code="INVALID_MAPPING")
        return f"{rel_var}[@relTypeField]"
    return _aql_string_literal(rel_type)


def _compile_where(expr_ctx: CypherParser.OC_ExpressionContext, bind_vars: dict[str, Any]) -> str:
    return _compile_expression(expr_ctx, bind_vars)


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
        return f"({left} {aql_op} {right})"

    if isinstance(ctx, CypherParser.OC_AddOrSubtractExpressionContext):
        # v0 subset doesn't use arithmetic; accept the first term only.
        terms = ctx.oC_MultiplyDivideModuloExpression()
        if len(terms) != 1:
            raise CoreError("Arithmetic not supported in v0", code="UNSUPPORTED")
        return _compile_expression(terms[0], bind_vars)

    if isinstance(ctx, CypherParser.OC_MultiplyDivideModuloExpressionContext):
        terms = ctx.oC_PowerOfExpression()
        if len(terms) != 1:
            raise CoreError("Arithmetic not supported in v0", code="UNSUPPORTED")
        return _compile_expression(terms[0], bind_vars)

    if isinstance(ctx, CypherParser.OC_PowerOfExpressionContext):
        terms = ctx.oC_UnaryAddOrSubtractExpression()
        if len(terms) != 1:
            raise CoreError("Arithmetic not supported in v0", code="UNSUPPORTED")
        return _compile_expression(terms[0], bind_vars)

    if isinstance(ctx, CypherParser.OC_UnaryAddOrSubtractExpressionContext):
        inner = _compile_expression(ctx.oC_StringListNullOperatorExpression(), bind_vars)
        # If a unary +/- is present, it will appear as a terminal child token.
        for i in range(ctx.getChildCount()):
            t = ctx.getChild(i).getText()
            if t in {"+", "-"}:
                raise CoreError("Unary +/- not supported in v0", code="UNSUPPORTED")
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
            raise CoreError("String operators not supported in v0", code="UNSUPPORTED")

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
            raise CoreError("Map literal not supported in v0", code="UNSUPPORTED")
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

        # extension functions are parsed as namespaces; we defer semantics to the registry later.
        if fn_norm.startswith("arango."):
            raise CoreError("arango.* extensions not implemented in v0 translator yet", code="NOT_IMPLEMENTED")

        raise CoreError(f"Unsupported function in v0: {fn}", code="UNSUPPORTED")

    raise CoreError(f"Unsupported expression node: {type(ctx).__name__}", code="UNSUPPORTED")

