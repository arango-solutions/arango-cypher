"""Layer 4 — AQL AST tenant-injection pass (Wave 8a / MT-4).

Why this module exists
----------------------
This is the only tenant-scope defence for the **NL→AQL direct path**
(``/nl2aql``) and for **raw AQL submissions** via ``/execute-aql``.
Layer 3 (Cypher AST rewriter, MT-3) does not run on either path, and
Layer 5 (EXPLAIN-plan validator, MT-5) only *refuses* unsafe queries —
it cannot fix them. Layer 4 sits between the AQL producer (transpiler,
LLM, or raw caller) and Layer 5, mechanically rewriting every
unscoped read over a tenant-scoped collection into one Layer 5 will
accept.

For the Cypher path (``/translate`` / ``/execute``) Layer 4 is
defence-in-depth: if MT-3 misses a case or the transpiler drops a
predicate, this pass still injects ``FILTER <var>.<tenant_field> ==
@tenantId`` at the correct site.

Approach (a) — EXPLAIN-plan-guided textual splicing
---------------------------------------------------
ArangoDB has no public Python AQL-AST library. Two viable options
were considered (PRD §7.4, agent_prompts_multitenant.md "MT-4"):

(a) **EXPLAIN-plan-guided textual splicing** — call
    ``db.aql.explain(aql, bind_vars)``, walk the structured plan, and
    locate each ``EnumerateCollectionNode`` / ``TraversalNode`` in the
    source by name-matching its ``outVariable.name`` against
    ``FOR <var> IN <coll>`` in the AQL text.

(b) **Local recursive-descent mini-parser** for the subset our
    transpiler + NL→AQL emit (~800 LOC).

This module implements (a). It piggybacks on the EXPLAIN call Layer 5
already pays for; the cost is tighter coupling to ArangoDB's plan-node
shape — accepted because Layer 5 already pays that cost and the same
node-shape vocabulary is shared between Layers 4 and 5 (see
``arango_cypher/tenant_plan_validator.py``).

How the textual splice locates each site
----------------------------------------
ArangoDB EXPLAIN does *not* return source positions on plan nodes.
However:

* ``EnumerateCollectionNode`` carries ``outVariable.name`` (the AQL
  identifier introduced by the ``FOR``) and ``collection`` (the
  physical collection name). The pair ``(outvar, coll)`` is unique
  *per lexical scope* — AQL's parser disambiguates shadowed variables
  via separate ``id`` fields, but the **source text** of each FOR
  clause is unique by ``(outvar, coll)`` within its scope.

* ``TraversalNode`` carries ``vertexOutVariable.name`` /
  ``edgeOutVariable.name`` / ``pathOutVariable.name`` and either
  ``graphName`` (named-graph traversal) or ``edgeCollections``
  (anonymous traversal). The header ``FOR v[, e[, p]] IN`` is
  uniquely identified by the vertex outvar.

The rewriter walks plan nodes in source order, locates each site via
regex (``\\bFOR\\s+<outvar>\\s+IN\\s+`?<coll>`?\\b`` for collection
FORs, similar for traversals), and accumulates ``(offset, insertion)``
edits applied in reverse order so earlier offsets aren't shifted.

Public surface
--------------
* :class:`AqlRewriteError` — raised when Layer 4 cannot safely rewrite
  the AQL (e.g. a tenant-scoped collection has neither a denorm field
  nor a smart-graph attribute, or the same ``FOR <var> IN <coll>``
  site cannot be located textually). Callers translate to HTTP 422 or
  403 depending on context.
* :func:`inject_tenant_scope` — single entry point used by the
  ``/translate``, ``/nl2aql``, and ``/execute-aql`` routes.

Bind-var hygiene contract
-------------------------
This pass **never** adds literal tenant values to ``bind_vars`` — it
only references the existing ``@tenantId`` / ``@tenantKey`` binds
defined by Layer 1 (``arango_cypher/service/security.py::_Session``).
The returned ``bind_vars`` dict is a defensive copy of the input;
new collection-name binds (``@@<coll>``) are not currently introduced
because the rewriter inlines collection names directly — if a future
refactor needs ``@@coll`` binds, they must use deterministic names
like ``@@_tenant_subq_<n>`` so idempotency holds.

Idempotency
-----------
``inject_tenant_scope(inject_tenant_scope(aql)) == inject_tenant_scope(aql)``
byte-for-byte. The pass checks every plan node for an *already-present*
tenant predicate (using the shared
:func:`arango_cypher.nl2cypher.tenant_ast_common.is_bindvar_reference`
matcher Layer 5 uses) before deciding to inject. Pinned by
``tests/test_tenant_ast_aql.py::test_idempotent_double_pass``.

PRD source-of-truth
-------------------
* ``docs/multitenant_prd.md`` §7 (Layer 4 — AQL AST tenant injection).
* ``docs/agent_prompts_multitenant.md`` "MT-4" task section.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .nl2cypher.tenant_ast_common import (
    TENANT_ID_BIND,
    UnknownEntityScope,
    predicate_for_collection,
)
from .nl2cypher.tenant_scope import (
    EntityTenantRole,
    TenantScopeManifest,
)

logger = logging.getLogger(__name__)


__all__ = [
    "AqlRewriteError",
    "inject_tenant_scope",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AqlRewriteError(Exception):
    """Layer 4 refused to rewrite the AQL.

    Carries a machine-actionable ``code`` (e.g.
    ``UNCONSTRAINED_COLLECTION_ACCESS``, ``EXPLAIN_FAILED``) and a
    human-readable message that surfaces in the HTTP error body so
    operators can act on the refusal without having to dig through
    logs.

    Mirrors :class:`arango_cypher.tenant_plan_validator.TenantScopeViolation`'s
    public surface so the route adapter can map both to identical
    HTTP 4xx responses (audit-v2 finding #2 — one error shape for the
    whole multi-tenant pipeline).
    """

    def __init__(self, *, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Plan-node shape helpers — operate on EXPLAIN-plan dicts
#
# These mirror the shapes Layer 5 (tenant_plan_validator) uses; keep
# the two in lock-step so a predicate Layer 4 emits is exactly what
# Layer 5 will accept on the very next round-trip.
# ---------------------------------------------------------------------------


def _outvar_name(node: dict[str, Any]) -> str | None:
    out = node.get("outVariable")
    if isinstance(out, dict):
        n = out.get("name")
        if isinstance(n, str) and n:
            return n
    return None


def _vertex_outvar_name(node: dict[str, Any]) -> str | None:
    """Extract a ``TraversalNode``'s vertex output variable name."""
    out = node.get("vertexOutVariable")
    if isinstance(out, dict):
        n = out.get("name")
        if isinstance(n, str) and n:
            return n
    return None


def _expr(node: dict[str, Any]) -> dict[str, Any]:
    e = node.get("expression")
    return e if isinstance(e, dict) else {}


def _is_attribute_access_on(
    expr: dict[str, Any],
    *,
    var_name: str,
    attr: str,
) -> bool:
    if expr.get("type") != "attribute access":
        return False
    if expr.get("name") != attr:
        return False
    subs = expr.get("subNodes") or []
    if not subs or not isinstance(subs, list):
        return False
    inner = subs[0]
    if not isinstance(inner, dict):
        return False
    if inner.get("type") != "reference":
        return False
    return inner.get("name") == var_name


def _is_bindvar_named(expr: dict[str, Any], name: str) -> bool:
    return expr.get("type") == "parameter" and expr.get("name") == name


def _compare_eq_subnodes(expr: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if expr.get("type") not in {"compare ==", "n-ary compare"}:
        return None
    subs = expr.get("subNodes") or []
    if len(subs) != 2 or not all(isinstance(s, dict) for s in subs):
        return None
    return subs[0], subs[1]


def _calc_matches_tenant_eq_bindvar(
    calc: dict[str, Any],
    var_name: str,
    attr: str,
) -> bool:
    """``CalculationNode`` whose expression is ``<var>.<attr> == @tenantId``."""
    expr = _expr(calc)
    sides = _compare_eq_subnodes(expr)
    if sides is None:
        return False
    lhs, rhs = sides
    return (
        _is_attribute_access_on(lhs, var_name=var_name, attr=attr) and _is_bindvar_named(rhs, TENANT_ID_BIND)
    ) or (
        _is_attribute_access_on(rhs, var_name=var_name, attr=attr) and _is_bindvar_named(lhs, TENANT_ID_BIND)
    )


def _condition_covers_tenant(
    cond: Any,
    *,
    outvar: str,
    tenant_field: str,
) -> bool:
    """Walk an IndexNode's ``condition`` tree looking for the
    canonical ``<var>.<tenant_field> == @tenantId`` shape."""
    if not isinstance(cond, dict):
        return False
    sides = _compare_eq_subnodes(cond)
    if sides is not None:
        lhs, rhs = sides
        if (
            _is_attribute_access_on(lhs, var_name=outvar, attr=tenant_field)
            and _is_bindvar_named(rhs, TENANT_ID_BIND)
        ) or (
            _is_attribute_access_on(rhs, var_name=outvar, attr=tenant_field)
            and _is_bindvar_named(lhs, TENANT_ID_BIND)
        ):
            return True
    for sub in cond.get("subNodes") or []:
        if isinstance(sub, dict) and _condition_covers_tenant(sub, outvar=outvar, tenant_field=tenant_field):
            return True
    return False


def _traversal_prune_uses_tenant(node: dict[str, Any]) -> bool:
    """TraversalNode's ``options.prune`` references ``@tenantId``."""
    options = node.get("options") or {}
    if not isinstance(options, dict):
        return False
    prune = options.get("prune")
    if isinstance(prune, str):
        # Older plan shapes serialise prune as the source expression
        # string. We accept both the bare ``@tenantId`` form and the
        # accidentally-doubled ``@@tenantId`` form that some clients
        # emit when stringifying bind references.
        return "@tenantId" in prune or "@@tenantId" in prune
    if isinstance(prune, dict):
        return _expr_references_tenant_bindvar(prune)
    return False


def _expr_references_tenant_bindvar(expr: dict[str, Any]) -> bool:
    if not isinstance(expr, dict):
        return False
    if _is_bindvar_named(expr, TENANT_ID_BIND):
        return True
    for sub in expr.get("subNodes") or []:
        if isinstance(sub, dict) and _expr_references_tenant_bindvar(sub):
            return True
    return False


# ---------------------------------------------------------------------------
# Plan-shape coercion (mirrors Layer 5's _coerce_plan)
# ---------------------------------------------------------------------------


def _coerce_plan(result: Any) -> Any:
    if isinstance(result, dict) and "plan" in result and isinstance(result["plan"], dict):
        return result["plan"]
    return result


def _gather_calcs_by_outvar(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index every CalculationNode in *plan* by its ``outVariable.name``.

    Used during idempotency detection: when we see an
    EnumerateCollectionNode that introduced ``e``, we look across all
    calculations for one whose expression is ``e.<tenant_field> ==
    @tenantId``. If found → already injected → no-op.
    """
    out: dict[str, dict[str, Any]] = {}
    for n in plan.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        if n.get("type") == "CalculationNode":
            name = _outvar_name(n)
            if name:
                out[name] = n
    return out


# ---------------------------------------------------------------------------
# Sharding-profile lookups (mirror Layer 5's _PlanWalker helpers, kept
# inline so this module stays self-contained — Layer 5's walker isn't
# the public-import surface)
# ---------------------------------------------------------------------------


def _smartgraph_attribute(collection: str, sharding_profile: dict[str, Any]) -> str | None:
    """Return the smart-graph attribute for *collection*, or None.

    Walks ``sharding_profile.graphs`` looking for a graph whose
    ``vertexCollections`` or ``edgeCollections`` contains the
    collection name. Both camelCase and snake_case keys are accepted
    (the analyzer has emitted both shapes in the wild).
    """
    graphs = sharding_profile.get("graphs") or []
    if not isinstance(graphs, list):
        return None
    for g in graphs:
        if not isinstance(g, dict):
            continue
        verts = g.get("vertexCollections") or g.get("vertex_collections") or []
        edges = g.get("edgeCollections") or g.get("edge_collections") or []
        if collection in (verts or []) or collection in (edges or []):
            attr = g.get("smartGraphAttribute") or g.get("smart_graph_attribute")
            if isinstance(attr, str) and attr:
                return attr
    return None


def _is_disjoint_smartgraph(graph_name: str | None, sharding_profile: dict[str, Any]) -> bool:
    if not graph_name:
        return False
    for g in sharding_profile.get("graphs") or []:
        if not isinstance(g, dict):
            continue
        if g.get("name") == graph_name and bool(g.get("isDisjoint")):
            return True
    return False


def _layout_kind(collection: str, sharding_profile: dict[str, Any]) -> str | None:
    """Return the collection's physical layout kind (e.g. ``satellite``,
    ``smartgraph``, ``regular``), case-folded; ``None`` when unknown.

    Tolerates both the ``members`` (Layer 5 / analyzer >= 0.5) and
    legacy ``collections`` list / ``collectionKinds`` map shapes.
    """
    members = sharding_profile.get("members") or {}
    if isinstance(members, dict):
        m = members.get(collection)
        if isinstance(m, dict):
            kind = m.get("kind")
            if isinstance(kind, str) and kind:
                return kind.lower()
    cols = sharding_profile.get("collections")
    if isinstance(cols, list):
        for entry in cols:
            if isinstance(entry, dict) and entry.get("name") == collection:
                kind = entry.get("kind")
                if isinstance(kind, str):
                    return kind.lower()
    kinds = sharding_profile.get("collectionKinds")
    if isinstance(kinds, dict):
        v = kinds.get(collection)
        if isinstance(v, str):
            return v.lower()
    return None


def _tenant_field_for(
    collection: str,
    manifest: TenantScopeManifest,
    sharding_profile: dict[str, Any],
    collection_to_entity: dict[str, str],
) -> str | None:
    """Resolve the predicate field for *collection*.

    Resolution order (mirrors Layer 5 / PRD §7.2):

    1. The entity's declared ``denorm_field`` from the manifest.
    2. The collection's smart-graph attribute from
       ``sharding_profile.graphs[*].smartGraphAttribute``.
    3. ``None`` when neither is available — caller should refuse with
       ``UNCONSTRAINED_COLLECTION_ACCESS``.
    """
    entity = collection_to_entity.get(collection, collection)
    scope = manifest.entities.get(entity)
    if scope is not None and scope.denorm_field:
        return scope.denorm_field
    return _smartgraph_attribute(collection, sharding_profile)


def _role_for(
    collection: str,
    manifest: TenantScopeManifest,
    collection_to_entity: dict[str, str],
) -> EntityTenantRole | None:
    """Return the tenant role of *collection*, or None if unmapped."""
    entity = collection_to_entity.get(collection, collection)
    scope = manifest.entities.get(entity)
    return scope.role if scope is not None else None


# ---------------------------------------------------------------------------
# Edit accumulator
# ---------------------------------------------------------------------------


@dataclass
class _Edit:
    """A single textual insertion. ``offset`` is the byte offset in the
    AQL source where *text* should be spliced in.

    Edits are applied in reverse-offset order so earlier offsets
    aren't shifted by later inserts. ``site_key`` is used for
    duplicate-edit suppression when the same plan node is encountered
    twice (e.g. via a subquery whose plan duplicates a parent
    EnumerateCollectionNode; should not happen in practice but cheap
    insurance).
    """

    offset: int
    text: str
    site_key: str
    change: str


@dataclass
class _Rewriter:
    """Stateful helper that holds the AQL source, the plan, and the
    accumulating edit list."""

    aql: str
    sharding_profile: dict[str, Any]
    manifest: TenantScopeManifest
    collection_to_entity: dict[str, str]
    bind_vars: dict[str, Any] = field(default_factory=dict)
    edits: list[_Edit] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    seen_sites: set[str] = field(default_factory=set)
    consumed_for_offsets: set[int] = field(default_factory=set)

    # ---- Source-location helpers -----------------------------------

    def _collection_source_tokens(self, collection: str) -> list[str]:
        """Regex fragments matching how *collection* can appear after
        ``FOR <var> IN`` in the AQL source.

        The transpiler binds the collection name as a *collection bind
        parameter* (``FOR n IN @@collection`` with
        ``bind_vars['@collection'] == 'Alert'``) rather than inlining
        the literal name. The EXPLAIN plan resolves the bind to the
        physical collection, so the plan node reports ``collection ==
        'Alert'`` while the source text says ``@@collection``. To
        locate the splice site we must match *both* forms:

        1. The literal name (backtick-optional) — NL→AQL / hand-written
           AQL path: ``FOR n IN Alert`` or ``FOR n IN `Alert```.
        2. Any collection-bind token ``@@<name>`` whose bound value is
           this collection — the transpiler path. In python-arango a
           collection bind is keyed ``@<name>`` in ``bind_vars`` and
           written ``@@<name>`` in the query, so we map every
           ``bind_vars`` key starting with ``@`` whose value equals the
           collection back to its ``@@<name>`` source token.

        Ordering is literal-first so a query that happens to inline the
        name is matched without depending on bind_vars.
        """
        tokens = [rf"`?{re.escape(collection)}`?"]
        for key, value in (self.bind_vars or {}).items():
            if not isinstance(key, str) or not key.startswith("@"):
                continue
            if value == collection:
                # key == '@collection'  →  source token '@@collection'
                tokens.append(re.escape("@" + key))
        return tokens

    def _locate_for(self, outvar: str, collection: str) -> int | None:
        """Find the offset just after a ``FOR <outvar> IN <coll>``
        clause in :attr:`aql`. Tolerates backticks around the
        collection name, collection-bind parameters (``@@collection``),
        and arbitrary whitespace.

        Returns the offset of the first character *after* the match
        (so an insertion at that offset goes between the FOR header
        and whatever follows). Returns ``None`` if not found.

        Avoids re-using a site that was already consumed by a prior
        edit (the same ``FOR <var> IN <coll>`` can appear at most
        once per lexical scope in well-formed AQL; consumption is
        belt-and-braces against pathological inputs).
        """
        # Use word boundaries on the var name and tolerate optional
        # backticks on the collection name, plus the collection-bind
        # form the transpiler emits (``@@collection``). The transpiler
        # emits backticks for keyword-shadowed names and the NL→AQL
        # path may not — accept all of them.
        coll_alt = "|".join(self._collection_source_tokens(collection))
        pattern = re.compile(
            rf"\bFOR\s+{re.escape(outvar)}\s+IN\s+(?:{coll_alt})",
            re.IGNORECASE,
        )
        for m in pattern.finditer(self.aql):
            end = m.end()
            if end in self.consumed_for_offsets:
                continue
            self.consumed_for_offsets.add(end)
            return end
        return None

    def _locate_traversal_header(
        self,
        vertex_outvar: str,
        edge_outvar: str | None,
        path_outvar: str | None,
    ) -> tuple[int, int] | None:
        """Find the textual ``FOR v[, e[, p]] IN ... <direction> ...``
        header in the AQL source and return the (start, end) offsets
        spanning from the matched start through the end of the
        traversal header line (immediately before the body / OPTIONS /
        FILTER / RETURN).

        Returns ``None`` if not found.

        We anchor on the vertex out-variable (always present on a
        TraversalNode); the optional edge / path variables are matched
        if present, with regex-optional groups when they're not.
        """
        # Build the variable list permutation: v, v,e, v,e,p — match
        # the actual variables the plan reports.
        vars_pat = re.escape(vertex_outvar)
        if edge_outvar:
            vars_pat += rf"\s*,\s*{re.escape(edge_outvar)}"
            if path_outvar:
                vars_pat += rf"\s*,\s*{re.escape(path_outvar)}"
        # Match up to (but not including) the next clause keyword or
        # the literal "RETURN" / "FILTER" / "LET" / "OPTIONS" /
        # "PRUNE" — capturing the full header so we can splice
        # OPTIONS in at the right spot.
        header_re = re.compile(
            rf"\bFOR\s+{vars_pat}\s+IN\b",
            re.IGNORECASE,
        )
        m = header_re.search(self.aql)
        if not m:
            return None
        # Walk forward from the header start to find the end of the
        # traversal-header expression. The header is followed by one
        # of: OPTIONS, FILTER, PRUNE, SORT, LIMIT, COLLECT, RETURN,
        # LET, INSERT, UPDATE, REPLACE, REMOVE, UPSERT, FOR. We stop
        # at the keyword *before* the next clause so the insertion
        # lands between the graph reference and the body.
        end_pat = re.compile(
            r"\b(OPTIONS|FILTER|PRUNE|SORT|LIMIT|COLLECT|RETURN|LET|INSERT|UPDATE|REPLACE|REMOVE|UPSERT|FOR)\b",
            re.IGNORECASE,
        )
        m2 = end_pat.search(self.aql, m.end())
        if not m2:
            # Truncated AQL — fall back to end-of-text.
            return (m.start(), len(self.aql))
        return (m.start(), m2.start())

    # ---- Per-node rewrites ------------------------------------------

    def rewrite_enumerate(
        self,
        node: dict[str, Any],
        calcs_by_outvar: dict[str, dict[str, Any]],
    ) -> None:
        """Inject ``FILTER <var>.<tenant_field> == @tenantId`` after the
        matching ``FOR <var> IN <coll>`` if the collection is tenant-
        scoped and the predicate isn't already present.

        Raises :class:`AqlRewriteError` with code
        ``UNCONSTRAINED_COLLECTION_ACCESS`` when the collection is
        tenant-scoped but has neither a denorm field nor a smart-graph
        attribute, i.e. there is no safe field to constrain on.
        """
        coll = node.get("collection")
        outvar = _outvar_name(node)
        if not isinstance(coll, str) or not outvar:
            return

        kind = _layout_kind(coll, self.sharding_profile)
        if kind in {"satellite", "system"}:
            return

        try:
            shape = predicate_for_collection(
                coll,
                self.manifest,
                {
                    **self.sharding_profile,
                    "collectionToEntity": self.collection_to_entity
                    or self.sharding_profile.get("collectionToEntity"),
                },
            )
        except UnknownEntityScope as exc:
            # Tenant-scoped collection without a renderable predicate
            # (no denorm field, no scoping path) — must refuse.
            # AMBIGUITY: predicate_for_collection raises for *both*
            # "no manifest entry" and "no scopable field". The former
            # is a translation-quality bug (the analyzer didn't surface
            # the entity); the latter is a schema-design issue. We
            # surface them under one code for now and rely on the
            # message to distinguish; a future refactor could split
            # the codes.
            raise AqlRewriteError(
                code="UNCONSTRAINED_COLLECTION_ACCESS",
                message=(
                    f"collection {coll!r} is tenant-scoped but Layer 4 cannot "
                    f"determine a safe tenant predicate: {exc}"
                ),
            ) from exc

        if shape is None:
            # GLOBAL entity — no rewrite needed.
            return

        role = _role_for(coll, self.manifest, self.collection_to_entity)
        if role is EntityTenantRole.TENANT_ROOT:
            # TENANT_ROOT (the Tenant collection itself) is keyed by
            # ``_key == @tenantKey``. Layer 5 enforces this via a
            # separate code path; injecting a FILTER over the
            # Tenant collection on every access would be redundant
            # (and wrong — the Tenant root may be referenced
            # without a `_key` predicate when joining its docs to
            # tenant-scoped entities through a known-tenant subquery).
            # Defer to Layer 5's TENANT_ROOT_UNCONSTRAINED check.
            return

        # Determine the tenant field with the same fallback chain as
        # Layer 5: denorm_field first, then smart-graph attribute.
        tenant_field = _tenant_field_for(
            coll,
            self.manifest,
            self.sharding_profile,
            self.collection_to_entity,
        )
        if not tenant_field:
            raise AqlRewriteError(
                code="UNCONSTRAINED_COLLECTION_ACCESS",
                message=(
                    f"tenant-scoped collection {coll!r} has neither a "
                    "denorm field nor a smart-graph attribute; Layer 4 "
                    "has no safe predicate to inject"
                ),
            )

        # Idempotency: skip if an existing CalculationNode already
        # binds <outvar>.<tenant_field> against @tenantId. Layer 5
        # accepts that exact shape, so re-injecting would be a
        # duplicate FILTER and break the byte-identical-second-pass
        # contract.
        for calc in calcs_by_outvar.values():
            if _calc_matches_tenant_eq_bindvar(calc, outvar, tenant_field):
                return

        site_key = f"enum::{outvar}::{coll}"
        if site_key in self.seen_sites:
            return
        self.seen_sites.add(site_key)

        offset = self._locate_for(outvar, coll)
        if offset is None:
            # The plan says this site exists; the source doesn't
            # match. This is a transpiler bug or a pre-existing
            # rewrite that obscured the FOR header. Fail-closed.
            raise AqlRewriteError(
                code="REWRITE_SITE_NOT_FOUND",
                message=(
                    f"plan reports FOR {outvar} IN {coll} but the AQL "
                    "source does not contain a matching textual site"
                ),
            )

        insertion = f"\n  FILTER {outvar}.{tenant_field} == @{TENANT_ID_BIND}"
        change_text = (
            f"Added FILTER {outvar}.{tenant_field} == @{TENANT_ID_BIND} after FOR {outvar} IN {coll}"
        )
        self.edits.append(_Edit(offset=offset, text=insertion, site_key=site_key, change=change_text))
        self.changes.append(change_text)

    def rewrite_index(
        self,
        node: dict[str, Any],
        calcs_by_outvar: dict[str, dict[str, Any]],
    ) -> None:
        """Same intent as :meth:`rewrite_enumerate` but for the
        optimised ``IndexNode`` shape that ArangoDB emits when a
        ``FOR + FILTER`` over an indexed field is fused into a single
        access node.

        Strategy: treat the IndexNode like an EnumerateCollectionNode
        for the purposes of locating the source FOR clause, but skip
        injection when the index's ``condition`` already covers
        ``outvar.<tenant_field> == @tenantId`` (the canonical
        post-injection plan shape).
        """
        coll = node.get("collection")
        outvar = _outvar_name(node)
        if not isinstance(coll, str) or not outvar:
            return

        kind = _layout_kind(coll, self.sharding_profile)
        if kind in {"satellite", "system"}:
            return

        try:
            shape = predicate_for_collection(
                coll,
                self.manifest,
                {
                    **self.sharding_profile,
                    "collectionToEntity": self.collection_to_entity
                    or self.sharding_profile.get("collectionToEntity"),
                },
            )
        except UnknownEntityScope as exc:
            raise AqlRewriteError(
                code="UNCONSTRAINED_COLLECTION_ACCESS",
                message=(
                    f"collection {coll!r} accessed via index node is "
                    f"tenant-scoped but Layer 4 cannot determine a "
                    f"safe tenant predicate: {exc}"
                ),
            ) from exc
        if shape is None:
            return
        role = _role_for(coll, self.manifest, self.collection_to_entity)
        if role is EntityTenantRole.TENANT_ROOT:
            return

        tenant_field = _tenant_field_for(
            coll,
            self.manifest,
            self.sharding_profile,
            self.collection_to_entity,
        )
        if not tenant_field:
            raise AqlRewriteError(
                code="UNCONSTRAINED_COLLECTION_ACCESS",
                message=(
                    f"tenant-scoped collection {coll!r} (index node) has "
                    "neither a denorm field nor a smart-graph attribute"
                ),
            )

        # Idempotency path A: the IndexNode's own condition already
        # references @tenantId on the tenant field.
        if _condition_covers_tenant(node.get("condition"), outvar=outvar, tenant_field=tenant_field):
            return
        # Idempotency path B: a downstream CalculationNode (uncommon
        # post-optimisation but possible) carries the predicate.
        for calc in calcs_by_outvar.values():
            if _calc_matches_tenant_eq_bindvar(calc, outvar, tenant_field):
                return

        site_key = f"index::{outvar}::{coll}"
        if site_key in self.seen_sites:
            return
        self.seen_sites.add(site_key)

        offset = self._locate_for(outvar, coll)
        if offset is None:
            raise AqlRewriteError(
                code="REWRITE_SITE_NOT_FOUND",
                message=(
                    f"plan reports IndexNode FOR {outvar} IN {coll} but "
                    "the AQL source does not contain a matching FOR clause"
                ),
            )

        insertion = f"\n  FILTER {outvar}.{tenant_field} == @{TENANT_ID_BIND}"
        change_text = (
            f"Added FILTER {outvar}.{tenant_field} == @{TENANT_ID_BIND} "
            f"after FOR {outvar} IN {coll} (index-fused FOR)"
        )
        self.edits.append(_Edit(offset=offset, text=insertion, site_key=site_key, change=change_text))
        self.changes.append(change_text)

    def rewrite_traversal(self, node: dict[str, Any]) -> None:
        """For ``TraversalNode`` over a non-disjoint graph, attach
        ``OPTIONS { prune: <v>.<field> != @tenantId }`` so the
        traversal halts the moment it crosses a tenant boundary.

        Skipped when:

        * The graph is a disjoint smartgraph — storage enforces the
          tenant boundary, no rewrite needed (Layer 5 already
          recognises this path).
        * The traversal's ``options.prune`` already references
          ``@tenantId`` — idempotency.
        * All vertex collections are satellite — there is no tenant
          field to scope on, and Layer 5 accepts the traversal.
        """
        graph_name = node.get("graphName")
        if isinstance(graph_name, str) and _is_disjoint_smartgraph(graph_name, self.sharding_profile):
            return
        if _traversal_prune_uses_tenant(node):
            return

        # Determine the prune field — prefer the vertex outvar's
        # collection denorm field when discoverable, fall back to a
        # smart-graph attribute. We probe every vertex collection the
        # traversal touches and refuse if NONE of them carry a
        # tenant-scoped attribute (i.e. the traversal really is
        # satellite-only and Layer 5 will accept it).
        vertex_colls = _traversal_vertex_collections(node, self.sharding_profile)
        non_satellite_field: str | None = None
        all_satellite = True
        for coll in vertex_colls:
            kind = _layout_kind(coll, self.sharding_profile)
            if kind in {"satellite", "system"}:
                continue
            all_satellite = False
            field_name = _tenant_field_for(
                coll,
                self.manifest,
                self.sharding_profile,
                self.collection_to_entity,
            )
            if field_name:
                non_satellite_field = field_name
                break
        if all_satellite:
            return
        if not non_satellite_field:
            raise AqlRewriteError(
                code="UNCONSTRAINED_TRAVERSAL",
                message=(
                    f"TraversalNode over non-satellite vertex collections "
                    f"{vertex_colls!r} has no resolvable tenant field; "
                    "Layer 4 cannot inject a safe prune predicate"
                ),
            )

        vertex_outvar = _vertex_outvar_name(node) or "v"
        edge_outvar = (
            (node.get("edgeOutVariable") or {}).get("name")
            if isinstance(node.get("edgeOutVariable"), dict)
            else None
        )
        path_outvar = (
            (node.get("pathOutVariable") or {}).get("name")
            if isinstance(node.get("pathOutVariable"), dict)
            else None
        )

        span = self._locate_traversal_header(vertex_outvar, edge_outvar, path_outvar)
        if span is None:
            raise AqlRewriteError(
                code="REWRITE_SITE_NOT_FOUND",
                message=(
                    f"plan reports TraversalNode FOR {vertex_outvar} IN ... "
                    "but the AQL source does not contain a matching "
                    "traversal header"
                ),
            )
        start, end = span

        site_key = f"trav::{vertex_outvar}::{graph_name}"
        if site_key in self.seen_sites:
            return
        self.seen_sites.add(site_key)

        # AMBIGUITY: when the source already has an ``OPTIONS {...}``
        # clause we *don't* try to merge our prune into it — that
        # would require parsing the existing options expression, which
        # is the local-parser path we explicitly chose not to take.
        # Instead, we splice a FILTER after the traversal header,
        # which is observationally weaker than prune (the traversal
        # still visits cross-tenant vertices, just doesn't yield
        # them) but is still accepted by Layer 5 via the per-vertex
        # filter calculation. The prune form is strictly preferred
        # and used whenever there is no pre-existing OPTIONS clause.
        # A future MT-4.1 should add prune-merging when OPTIONS is
        # present.
        existing_options = re.search(
            r"\bOPTIONS\b",
            self.aql[start:end],
            re.IGNORECASE,
        )
        if existing_options:
            insertion = f"\n  FILTER {vertex_outvar}.{non_satellite_field} == @{TENANT_ID_BIND}"
            offset = end
            change_text = (
                f"Added FILTER {vertex_outvar}.{non_satellite_field} == @{TENANT_ID_BIND} "
                f"after TraversalNode header (existing OPTIONS preserved)"
            )
        else:
            insertion = f"\n  OPTIONS {{ prune: {vertex_outvar}.{non_satellite_field} != @{TENANT_ID_BIND} }}"
            offset = end
            change_text = (
                f"Added OPTIONS {{ prune: {vertex_outvar}.{non_satellite_field} != @{TENANT_ID_BIND} }} "
                f"to traversal over {vertex_outvar}"
            )
        self.edits.append(_Edit(offset=offset, text=insertion, site_key=site_key, change=change_text))
        self.changes.append(change_text)

    # ---- Apply -------------------------------------------------------

    def apply(self) -> str:
        if not self.edits:
            return self.aql
        # Sort in reverse so each insertion's offset is still valid
        # after later inserts have already been applied.
        sorted_edits = sorted(self.edits, key=lambda e: e.offset, reverse=True)
        out = self.aql
        for ed in sorted_edits:
            out = out[: ed.offset] + ed.text + out[ed.offset :]
        return out


def _traversal_vertex_collections(node: dict[str, Any], sharding_profile: dict[str, Any]) -> list[str]:
    """Extract the vertex collections a TraversalNode touches.

    Mirrors Layer 5's ``_traversal_vertex_collections``: prefers the
    inline ``graph.vertexCollections`` list; falls back to looking
    up ``graphName`` in the sharding profile.
    """
    graph = node.get("graph") or {}
    if isinstance(graph, dict):
        verts = graph.get("vertexCollections") or graph.get("vertex_collections")
        if isinstance(verts, list):
            return [v for v in verts if isinstance(v, str)]
    graph_name = node.get("graphName")
    if isinstance(graph_name, str) and graph_name:
        for g in sharding_profile.get("graphs") or []:
            if isinstance(g, dict) and g.get("name") == graph_name:
                verts = g.get("vertexCollections") or g.get("vertex_collections")
                if isinstance(verts, list):
                    return [v for v in verts if isinstance(v, str)]
    return []


# ---------------------------------------------------------------------------
# Plan-walking driver
# ---------------------------------------------------------------------------


def _walk_plan(
    plan: dict[str, Any],
    rewriter: _Rewriter,
) -> None:
    """Walk all nodes of *plan*, dispatching to per-node rewrite
    handlers. Recurses into SubqueryNode bodies so a subquery's
    EnumerateCollectionNode is rewritten with the same predicate as
    the top level.

    The walker accepts both the "wrapped" plan shape ``{"plan":
    {"nodes": [...]}}`` and the bare ``{"nodes": [...]}`` shape (some
    python-arango versions strip the wrapper, others don't —
    :func:`_coerce_plan` handles both).
    """
    nodes = plan.get("nodes") if isinstance(plan.get("nodes"), list) else []
    calcs_by_outvar = _gather_calcs_by_outvar(plan)
    for node in nodes:
        if not isinstance(node, dict):
            continue
        ntype = node.get("type")
        if ntype == "EnumerateCollectionNode":
            rewriter.rewrite_enumerate(node, calcs_by_outvar)
        elif ntype == "IndexNode":
            rewriter.rewrite_index(node, calcs_by_outvar)
        elif ntype == "TraversalNode":
            rewriter.rewrite_traversal(node)
        elif ntype == "SubqueryNode":
            sub_plan = node.get("subquery") or {}
            if isinstance(sub_plan, dict):
                _walk_plan(sub_plan, rewriter)
        # CollectNode / AggregateNode / FunctionCallNode (the
        # collection-as-argument form) are handled by the enclosing
        # FOR: if the FOR is unconstrained, _check_node refuses; if
        # constrained, the collect/aggregate naturally inherits the
        # filter.  The PRD §7.2 explicitly states "COLLECT /
        # AGGREGATE / COUNT over a tenant-scoped enumeration — the
        # enclosing FOR's filter handles it via recursion; REJECT
        # only if the enclosing FOR is itself unconstrained."  Layer
        # 4 thus has nothing extra to do here.
        # CalculationNode / FilterNode / ReturnNode / LimitNode / SortNode
        # / SingletonNode / GatherNode / RemoteNode / ScatterNode /
        # DistributeNode: pass through.


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def inject_tenant_scope(
    *,
    db: Any,
    aql: str,
    bind_vars: dict[str, Any] | None,
    manifest: TenantScopeManifest,
    sharding_profile: dict[str, Any] | None,
    tenant_id: str,
    tenant_key: str,
    plan_override: dict[str, Any] | None = None,
    collection_to_entity: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any], list[str]]:
    """Layer 4: rewrite *aql* so every tenant-scoped collection access
    carries an ``@tenantId``-bound FILTER (or prune for traversals).

    Returns ``(rewritten_aql, augmented_bind_vars, changes)`` where:

    * ``rewritten_aql`` is the source AQL with FILTER / OPTIONS
      injections spliced in at the right textual sites.
    * ``augmented_bind_vars`` is a defensive copy of *bind_vars*; this
      pass never introduces literal tenant values, only bind-var
      references (``@tenantId`` / ``@tenantKey`` — which the caller
      must already be supplying via Layer 6's session-spread).
    * ``changes`` is a list of human-readable strings (one per
      injection site) the UI surfaces in its "Tenant rewrites"
      annotation strip. Empty when no rewrites were needed.

    Parameters
    ----------
    db:
        ArangoDB ``StandardDatabase`` for the EXPLAIN round-trip.
        Ignored when *plan_override* is supplied (unit-test path).
    aql:
        The candidate AQL text. May come from the Cypher transpiler,
        NL→AQL direct path, or a raw ``/execute-aql`` submission.
    bind_vars:
        The caller's bind variables. Must already contain
        ``@tenantId`` / ``@tenantKey`` from the session; Layer 4 does
        NOT inject session values (that's Layer 6's job).
    manifest:
        Tenant-scope manifest from
        :func:`arango_cypher.nl2cypher.tenant_scope.analyze_tenant_scope`.
    sharding_profile:
        ``metadata.shardingProfile`` from the schema bundle. ``None`` is
        accepted (older bundles / heuristic mode) — Layer 4 falls back
        to the manifest's denorm_field and rejects collections that
        lack both.
    tenant_id, tenant_key:
        Carried in the signature for symmetry with Layer 5 and to
        document the session-bound values. The rewriter does not use
        them directly — it only emits bind-variable *references* to
        ``@tenantId`` / ``@tenantKey``. Supplied here so future
        single-shot literal validation (e.g. "the caller's
        bind_vars['tenantId'] must equal tenant_id") can be added
        without changing the contract.
    plan_override:
        Test-only: a hand-crafted plan dict that bypasses the
        :meth:`StandardDatabase.aql.explain` round-trip. Production
        callers leave this ``None``.
    collection_to_entity:
        Per-deployment inversion of
        ``physical_mapping.entities[entity].collectionName``. Allows
        Layer 4 to look up a collection's conceptual entity when the
        sharding profile lacks an explicit ``collectionToEntity`` map.
        Defaults to ``{}`` (assume collection name == entity name).

    Raises
    ------
    AqlRewriteError
        On any of:

        * ``UNCONSTRAINED_COLLECTION_ACCESS`` — tenant-scoped
          collection without a denorm field or smart-graph attribute.
        * ``EXPLAIN_FAILED`` — the EXPLAIN round-trip raised. Caller
          should treat as a soft-422 (the AQL is syntactically
          invalid or references an unknown collection).
        * ``EXPLAIN_MALFORMED`` — explain returned a non-dict.
        * ``REWRITE_SITE_NOT_FOUND`` — plan says the site exists, the
          source doesn't match. Indicates a transpiler bug.

    Idempotency
    -----------
    Calling this function twice on the same *aql* produces byte-
    identical output the second time. The second pass observes the
    predicates injected by the first and treats every plan node as
    "already constrained, no-op".
    """
    if plan_override is not None:
        plan = plan_override
    else:
        try:
            result = db.aql.explain(aql, bind_vars=bind_vars or {})
        except Exception as exc:
            raise AqlRewriteError(
                code="EXPLAIN_FAILED",
                message=f"EXPLAIN failed: {type(exc).__name__}: {exc}",
            ) from exc
        plan = _coerce_plan(result)
    if not isinstance(plan, dict):
        raise AqlRewriteError(
            code="EXPLAIN_MALFORMED",
            message=f"EXPLAIN returned non-dict plan: {type(plan).__name__}",
        )

    # Defensive copy of bind_vars. We never mutate the caller's dict
    # (route handlers may reuse it for logging / response).
    augmented = dict(bind_vars or {})

    # Structural short-circuit. The rewriter only has work to do when
    # the manifest classified at least one entity as TENANT_SCOPED.
    #
    # We deliberately do NOT gate on ``manifest.tenant_entity`` here.
    # ``tenant_entity`` is only set when a conceptual entity is literally
    # named ``Tenant`` (the traversal-scoping root), but a schema can be
    # multi-tenant purely through a denormalised tenant column
    # (e.g. ``Alert.tenantId``) with *no* Tenant root collection at all.
    # Gating on ``tenant_entity is None`` made Layer 4 a no-op on those
    # schemas while Layer 5 (which classifies per-entity off the manifest
    # role, independent of any Tenant root) still treated the same
    # collections as scoped and refused the unscoped scan — the two
    # layers disagreed and every tenant-bound query 403'd. Checking
    # ``scoped_entities()`` keeps the two layers in lock-step: if Layer 5
    # would refuse an unscoped read, Layer 4 injects the predicate that
    # makes it safe. We still walk the plan when there *are* scoped
    # entities to honour the idempotency contract.
    if not manifest.entities or not manifest.scoped_entities():
        logger.debug(
            "inject_tenant_scope: manifest has no tenant-scoped entities; "
            "returning aql unchanged (single-tenant / admin-bypass path)"
        )
        return aql, augmented, []

    rewriter = _Rewriter(
        aql=aql,
        sharding_profile=sharding_profile or {},
        manifest=manifest,
        collection_to_entity=collection_to_entity or {},
        bind_vars=augmented,
    )
    _walk_plan(plan, rewriter)
    rewritten = rewriter.apply()
    if rewriter.changes:
        logger.info(
            "TENANT_AST_AQL_REWRITES count=%d tenant=%s changes=%r",
            len(rewriter.changes),
            tenant_id,
            rewriter.changes,
        )
    # Silently unused; declared to match the prompt signature. See
    # docstring "Parameters → tenant_id / tenant_key" for rationale.
    _ = tenant_key
    return rewritten, augmented, list(rewriter.changes)
