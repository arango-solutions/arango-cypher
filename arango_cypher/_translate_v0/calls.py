"""CALL / procedure clause compilation for the v0 translator."""

from __future__ import annotations

from typing import Any

from arango_query_core import AqlQuery, CoreError

from .._antlr.CypherParser import CypherParser
from .state import _active_registry


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
    """Compile a CALL into AQL ``FOR`` / ``LET`` lines."""
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
    from .core import _compile_expression

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
    from .core import _append_return, _compile_expression

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
    from .core import _compile_expression

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
