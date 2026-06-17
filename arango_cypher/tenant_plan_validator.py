"""Layer 5 — EXPLAIN-plan tenant-scope validator (the security boundary).

This module implements the safety boundary defined by
``docs/multitenant_prd.md`` §1.2 / §8 and ``docs/agent_prompts_multitenant.md``
Wave 7 part 3:

  A query is *safe* if, for every collection it reads, one of these
  holds:
    1. The collection's physical layout kind is ``satellite``.
    2. The collection's physical layout kind is ``smartgraph`` AND the
       plan's access node carries a bind-var-based filter / index
       predicate of the form ``doc.<smartGraphAttribute> == @tenantId``
       whose ``@tenantId`` value equals the session's tenant id.
    3. The collection is ``Tenant`` (TENANT_ROOT) AND the access is
       keyed by ``@tenantKey`` (the session tenant's ``_key``).
    4. The access occurs inside a subquery or traversal whose
       enclosing constraint already guarantees the per-document
       tenant id matches ``@tenantId``.

``validate_plan`` calls ArangoDB ``EXPLAIN`` once per query and walks
the resulting plan, refusing anything that does not satisfy the above
**regardless of which upstream layer produced the AQL**. It trusts no
LLM, no guardrail, no AST pass, no transpiler — if Layer 5 passes, the
query is safe; if it refuses, the query does not execute.

This is the security boundary. Every refusal emits a structured
``TENANT_SCOPE_VIOLATION`` audit log line with both the AQL and plan
digests; every pass emits a ``TENANT_SCOPE_OK`` line with the same
digests. Both are required for audit replay — do not silence the OK
line for performance.

Wired through :func:`arango_query_core.exec.safe_execute` (Wave 7
part 4), which spreads ``{tenantId, tenantKey}`` from the session
over the client-supplied bind vars **last** so the session value
silently wins.

Layer 5 is independent of Wave 8a's Layers 3 / 4 (AST rewrites). The
predicates Layer 3 / 4 inject (``doc.<field> == @tenantId``) are the
shapes Layer 5 recognises here.

Bind-resolution sentinel (real-ArangoDB EXPLAIN)
------------------------------------------------
ArangoDB's EXPLAIN **resolves value bind parameters into literal
``value`` nodes** in the returned plan — a ``@tenantId`` reference
becomes ``{"type": "value", "value": "<the tenant id>"}``, and the
optimiser frequently fuses the injected ``FILTER`` *into* the
``EnumerateCollectionNode`` (``node["filter"]``) rather than leaving a
standalone ``CalculationNode``. A naïve "only accept a ``parameter``
node named tenantId" check therefore fails on every real query, while
a "accept any literal on the tenant field" check would re-open the T2
literal-smuggling hole (a caller could hard-code another tenant's id).

To get both safety and real-DB compatibility, :func:`validate_plan`
EXPLAINs with a fresh, unguessable **sentinel** substituted for
``tenantId`` (``bind_vars`` for execution are untouched). The plan
walker then accepts a tenant-field equality only when it compares
against (a) a ``parameter`` named ``tenantId`` — the hand-crafted /
``plan_override`` shape used by tests and any ArangoDB build that does
not fold the bind — or (b) a ``value`` literal **equal to the
per-call sentinel**. Because the sentinel is random per validation, a
caller cannot smuggle a matching literal, so a literal that is *not*
the sentinel is still refused as ``LITERAL_TENANT_PREDICATE``. The
walker also descends through ``AND`` conjunctions (never ``OR``) and
inspects the inline ``EnumerateCollectionNode.filter`` so a fused
predicate is recognised.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .nl2cypher.tenant_scope import EntityTenantRole, TenantScopeManifest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class TenantScopeViolation(Exception):
    """Layer 5 refused the query as unsafe.

    Carries machine-actionable diagnostic fields:

    * ``code`` — short reason code (e.g. ``UNCONSTRAINED_COLLECTION_SCAN``,
      ``LITERAL_TENANT_PREDICATE``, ``TENANT_BIND_MISMATCH``). Used for
      log aggregation and red-team-corpus assertions.
    * ``message`` — human-readable description of the violation.
    * ``aql_digest`` — sha256 of the AQL plus its sorted bind vars; lets
      auditors replay the exact submission without storing the raw text.
    * ``plan_digest`` — sha256 of the EXPLAIN plan as JSON with
      ``sort_keys=True``; lets auditors recover the exact refused plan
      shape.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        aql_digest: str = "",
        plan_digest: str = "",
    ):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.aql_digest = aql_digest
        self.plan_digest = plan_digest


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_plan(
    *,
    db: Any,
    aql: str,
    bind_vars: dict[str, Any],
    manifest: TenantScopeManifest,
    sharding_profile: dict[str, Any] | None,
    collection_to_entity: dict[str, str] | None = None,
    session: Any,
    plan_override: dict[str, Any] | None = None,
) -> None:
    """Refuse the query if its EXPLAIN plan violates §1.2.

    Algorithm (PRD §8.2):

    1. Fetch ``plan = db.aql.explain(aql, bind_vars=bind_vars)["plan"]``.
       (When ``plan_override`` is supplied — used by unit tests — skip
       the round-trip and use it directly. The override path is only
       activated when explicitly passed; it never substitutes for a
       missing ``db`` in production.)
    2. Walk the plan nodes and classify each into ``satellite``,
       ``tenant_root`` (Tenant collection), ``tenant_scoped``, or
       ``unknown``. If any node is ``tenant_scoped`` (i.e. the plan
       reads a smartgraph collection or a manifest-tagged
       ``TENANT_SCOPED`` entity), the query is *tenant-touching* and
       the bind-var sanity check must pass:

       * ``session.tenant_id`` must not be ``None``.
       * **If** ``bind_vars`` carries a ``tenantId``, it must equal
         ``session.tenant_id``. When ``tenantId`` is absent (the AQL
         never references ``@tenantId`` — e.g. an unconstrained scan, or
         a storage-isolated disjoint-smartgraph traversal) the precise
         per-node walk below decides: it refuses an unconstrained scan
         with ``UNCONSTRAINED_COLLECTION_SCAN`` and accepts a disjoint
         smartgraph. Layer 6 (``safe_execute``) only injects the
         ``@tenantId`` bind when the query references it, so ``tenantId``
         being present here already implies it equals the session value
         — the equality check is retained as defence in depth.

       If the plan touches **only** satellite / global / Tenant-by-key
       collections, the bind-var check is **not** required — pure
       reference-data queries (e.g. ``FOR c IN Country RETURN c``)
       remain executable without a tenant binding.

    3. Per node type, refuse anything not covered by §1.2 (see helpers
       below).

    4. Emit a structured audit log line (pass *and* refuse).

    Raises :class:`TenantScopeViolation` on refusal.
    """
    # Bind-resolution sentinel: real ArangoDB EXPLAIN folds the
    # ``@tenantId`` value bind into literal ``value`` nodes, so we
    # cannot tell a session-injected ``@tenantId`` from a caller-
    # smuggled literal by node *type* alone. We EXPLAIN with a fresh
    # unguessable sentinel substituted for ``tenantId`` (execution
    # bind_vars are NOT touched — Layer 6 executes with the real
    # value); the walker then accepts a tenant-field equality against
    # the sentinel as proof the predicate is the resolved session
    # bind, and refuses any *other* literal. ``plan_override`` callers
    # (unit tests) skip EXPLAIN entirely and so have no sentinel — the
    # walker falls back to accepting only ``parameter``-typed
    # ``@tenantId`` nodes, exactly as before.
    tenant_sentinel: str | None = None
    if plan_override is not None:
        plan = plan_override
    else:
        explain_bind_vars = dict(bind_vars or {})
        if "tenantId" in explain_bind_vars:
            tenant_sentinel = f"__tenant_sentinel_{uuid4().hex}__"
            explain_bind_vars["tenantId"] = tenant_sentinel
        try:
            result = db.aql.explain(aql, bind_vars=explain_bind_vars)
        except Exception as exc:
            digests = _digests(aql=aql, bind_vars=bind_vars, plan=None)
            violation = TenantScopeViolation(
                code="EXPLAIN_FAILED",
                message=f"EXPLAIN failed: {type(exc).__name__}: {exc}",
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation from exc
        plan = _coerce_plan(result)

    if not isinstance(plan, dict):
        digests = _digests(aql=aql, bind_vars=bind_vars, plan=None)
        violation = TenantScopeViolation(
            code="EXPLAIN_MALFORMED",
            message=f"EXPLAIN returned non-dict plan: {type(plan).__name__}",
            **digests,
        )
        _log_violation(violation, session=session)
        raise violation

    nodes = plan.get("nodes") if isinstance(plan.get("nodes"), list) else []

    walker = _PlanWalker(
        plan=plan,
        bind_vars=bind_vars,
        manifest=manifest,
        sharding_profile=sharding_profile or {},
        collection_to_entity=collection_to_entity or {},
        tenant_sentinel=tenant_sentinel,
    )
    touches_tenant_data = walker.classify_touches_tenant_data(nodes)

    digests = _digests(aql=aql, bind_vars=bind_vars, plan=plan)

    if touches_tenant_data:
        session_tenant = getattr(session, "tenant_id", None)
        if session_tenant is None:
            violation = TenantScopeViolation(
                code="NO_SESSION_TENANT",
                message=(
                    "session has no tenant_id; cannot validate tenant-scoped query under tenant-user mode"
                ),
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation

        # Only a *present* tenantId bind that disagrees with the session
        # is a mismatch. An absent tenantId (the query never references
        # ``@tenantId``) is not a mismatch — it means the query carries
        # no tenant predicate, which the per-node walk below refuses
        # precisely (UNCONSTRAINED_COLLECTION_SCAN) or accepts for a
        # storage-isolated disjoint smartgraph. Layer 6 guarantees that
        # when ``tenantId`` *is* present it equals the session value, so
        # this branch is defence in depth against an upstream that
        # bypasses the session-spread.
        if "tenantId" in bind_vars and bind_vars["tenantId"] != session_tenant:
            bv_tenant = bind_vars["tenantId"]
            violation = TenantScopeViolation(
                code="TENANT_BIND_MISMATCH",
                message=(
                    f"bind_vars['tenantId']={bv_tenant!r} does not match session.tenant_id={session_tenant!r}"
                ),
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation

    walker.walk(nodes, digests=digests, session=session)
    _log_pass(session=session, **digests)


# ---------------------------------------------------------------------------
# Plan walker (the heart of Layer 5)
# ---------------------------------------------------------------------------


@dataclass
class _PlanWalker:
    plan: dict[str, Any]
    bind_vars: dict[str, Any]
    manifest: TenantScopeManifest
    sharding_profile: dict[str, Any]
    collection_to_entity: dict[str, str]
    # Per-call random sentinel substituted for ``tenantId`` at EXPLAIN
    # time so a resolved bind literal can be told apart from a smuggled
    # one. ``None`` on the ``plan_override`` path (no EXPLAIN ran) — the
    # matchers then accept only ``parameter``-typed ``@tenantId`` nodes.
    tenant_sentinel: str | None = None

    _nodes_by_id: dict[Any, dict[str, Any]] = field(default_factory=dict)
    _calc_by_outvar: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        nodes = self.plan.get("nodes") or []
        if isinstance(nodes, list):
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                nid = n.get("id")
                if nid is not None:
                    self._nodes_by_id[nid] = n
                if n.get("type") == "CalculationNode":
                    outvar = _outvar_name(n)
                    if outvar:
                        self._calc_by_outvar[outvar] = n

    # ---- Layout / role lookups ---------------------------------------------

    def _layout_kind(self, collection: str) -> str:
        """Return ``satellite`` / ``smartgraph`` / ``regular`` / ``system`` /
        ``tenant-root`` / ``unknown`` for *collection*."""
        members = self.sharding_profile.get("members") or {}
        if isinstance(members, dict):
            member = members.get(collection)
            if isinstance(member, dict):
                kind = member.get("kind")
                if isinstance(kind, str) and kind:
                    return kind.lower()
        return "unknown"

    def _entity_of(self, collection: str) -> str | None:
        return self.collection_to_entity.get(collection) or (
            collection if collection in self.manifest.entities else None
        )

    def _tenant_field_for(self, collection: str) -> str | None:
        """Return the entity's denormalised tenant column, or the
        graph-level ``smartGraphAttribute`` when no denorm field is
        declared but the collection is sharded by the attribute."""
        entity = self._entity_of(collection)
        if entity is not None:
            scope = self.manifest.entities.get(entity)
            if scope is not None and scope.denorm_field:
                return scope.denorm_field
        attr = self._smartgraph_attribute(collection)
        return attr

    def _smartgraph_attribute(self, collection: str) -> str | None:
        graphs = self.sharding_profile.get("graphs") or []
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

    def _is_disjoint_smartgraph(self, graph_name: str) -> bool:
        for g in self.sharding_profile.get("graphs") or []:
            if not isinstance(g, dict):
                continue
            if g.get("name") == graph_name and bool(g.get("isDisjoint")):
                return True
        return False

    def _graph_vertex_collections(self, graph_name: str) -> list[str]:
        for g in self.sharding_profile.get("graphs") or []:
            if isinstance(g, dict) and g.get("name") == graph_name:
                verts = g.get("vertexCollections") or g.get("vertex_collections") or []
                if isinstance(verts, list):
                    return [v for v in verts if isinstance(v, str)]
        return []

    def _role_of_collection(self, collection: str) -> EntityTenantRole | None:
        entity = self._entity_of(collection)
        if entity is None:
            return None
        return self.manifest.role_of(entity)

    def _is_tenant_touching_collection(self, collection: str) -> bool:
        """Whether reading *collection* requires a tenant predicate.

        Returns True iff the collection's physical layout is
        ``smartgraph`` / ``regular`` and the manifest classifies it as
        ``TENANT_SCOPED`` or ``TENANT_ROOT``, **or** the layout kind is
        unknown and the manifest tags the entity as tenant-touching
        (defence-in-depth: if we don't know the physical kind, we
        defer to the conceptual role).
        """
        kind = self._layout_kind(collection)
        if kind in {"satellite", "system"}:
            return False
        role = self._role_of_collection(collection)
        if role in {EntityTenantRole.TENANT_SCOPED, EntityTenantRole.TENANT_ROOT}:
            return True
        # Unknown collection on a smartgraph deployment — fail-closed.
        if kind in {"smartgraph", "regular"}:
            return True
        return False

    # ---- Classification pre-pass ------------------------------------------

    def classify_touches_tenant_data(self, nodes: list[dict[str, Any]]) -> bool:
        """Return True iff ANY node accesses a tenant-touching collection.

        Drives the bind-var sanity check in :func:`validate_plan`.
        Walks subquery bodies too so the check is plan-wide.
        """
        for node in nodes:
            if not isinstance(node, dict):
                continue
            t = node.get("type")
            if t in {"EnumerateCollectionNode", "IndexNode"}:
                coll = node.get("collection")
                if isinstance(coll, str) and self._is_tenant_touching_collection(coll):
                    return True
            elif t == "TraversalNode":
                for coll in self._traversal_vertex_collections(node):
                    if self._is_tenant_touching_collection(coll):
                        return True
            elif t == "SubqueryNode":
                sub_nodes = ((node.get("subquery") or {}).get("nodes")) or []
                if isinstance(sub_nodes, list) and self.classify_touches_tenant_data(sub_nodes):
                    return True
        return False

    # ---- Main walk --------------------------------------------------------

    def walk(
        self,
        nodes: list[dict[str, Any]],
        *,
        digests: dict[str, str],
        session: Any,
    ) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            self._check_node(node, digests=digests, session=session)

    def _check_node(
        self,
        node: dict[str, Any],
        *,
        digests: dict[str, str],
        session: Any,
    ) -> None:
        t = node.get("type")
        if t == "EnumerateCollectionNode":
            self._check_enumerate(node, digests=digests, session=session)
        elif t == "IndexNode":
            self._check_index(node, digests=digests, session=session)
        elif t == "TraversalNode":
            self._check_traversal(node, digests=digests, session=session)
        elif t == "SubqueryNode":
            sub_plan = node.get("subquery") or {}
            sub_nodes = sub_plan.get("nodes") if isinstance(sub_plan, dict) else []
            if isinstance(sub_nodes, list):
                # Build a fresh walker so the inner ``CalculationNode``
                # index sees only the subquery's calcs — accepting a
                # filter from the outer scope would be a tenant-scope
                # leak vector.
                inner = _PlanWalker(
                    plan=sub_plan,
                    bind_vars=self.bind_vars,
                    manifest=self.manifest,
                    sharding_profile=self.sharding_profile,
                    collection_to_entity=self.collection_to_entity,
                    tenant_sentinel=self.tenant_sentinel,
                )
                inner.walk(sub_nodes, digests=digests, session=session)
        # CalculationNode / FilterNode / ReturnNode / LimitNode / SortNode /
        # CollectNode / SingletonNode / GatherNode / RemoteNode / ScatterNode
        # / DistributeNode: not collection accesses; pass through.

    def _check_enumerate(
        self,
        node: dict[str, Any],
        *,
        digests: dict[str, str],
        session: Any,
    ) -> None:
        coll = node.get("collection")
        if not isinstance(coll, str):
            return
        kind = self._layout_kind(coll)
        if kind in {"satellite", "system"}:
            return
        role = self._role_of_collection(coll)
        if role is EntityTenantRole.GLOBAL and kind == "unknown":
            return
        if role is EntityTenantRole.TENANT_ROOT:
            if self._has_tenant_root_predicate(node):
                return
            violation = TenantScopeViolation(
                code="TENANT_ROOT_UNCONSTRAINED",
                message=(f"Tenant-root collection {coll!r} scanned without @tenantKey predicate"),
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation

        # Accept first: a tenant-field equality against ``@tenantId``
        # (parameter form) or the per-call sentinel (resolved-bind
        # form) — checking both the inline ``node["filter"]`` and any
        # standalone CalculationNode, descending only through AND. When
        # such a conjunct is present the scan is safe even if the query
        # *also* carries a mismatching literal (the AND with the
        # session value makes that conjunct return nothing — no leak).
        if self._has_tenant_predicate(node, coll):
            return

        literal_hit = self._has_mismatching_tenant_literal(node, coll)
        if literal_hit is not None:
            violation = TenantScopeViolation(
                code="LITERAL_TENANT_PREDICATE",
                message=(
                    f"{coll!r} scanned with a literal tenant predicate "
                    f"({literal_hit!r}); only the session @tenantId bind is "
                    "accepted"
                ),
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation

        violation = TenantScopeViolation(
            code="UNCONSTRAINED_COLLECTION_SCAN",
            message=(f"{coll!r} scanned without @tenantId predicate (physical kind=" + kind + ")"),
            **digests,
        )
        _log_violation(violation, session=session)
        raise violation

    def _check_index(
        self,
        node: dict[str, Any],
        *,
        digests: dict[str, str],
        session: Any,
    ) -> None:
        coll = node.get("collection")
        if not isinstance(coll, str):
            return
        kind = self._layout_kind(coll)
        if kind in {"satellite", "system"}:
            return
        role = self._role_of_collection(coll)
        if role is EntityTenantRole.GLOBAL and kind == "unknown":
            return
        if role is EntityTenantRole.TENANT_ROOT:
            if _index_keyed_by_tenant_key(node):
                return
            violation = TenantScopeViolation(
                code="TENANT_ROOT_UNCONSTRAINED",
                message=(f"Tenant-root index lookup on {coll!r} not keyed by @tenantKey"),
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation

        if _index_keyed_by_tenant_key(node):
            # Smartgraph collections may legitimately accept a _key
            # equality if the smart-graph attribute is part of the
            # composite _key. Accept the same _key=@tenantKey shape
            # we use for TENANT_ROOT — Layer 5 still rejects scans
            # that don't carry this predicate via _check_enumerate.
            return

        tenant_field = self._tenant_field_for(coll)
        if not _index_covers_tenant(node, tenant_field, sentinel=self.tenant_sentinel):
            violation = TenantScopeViolation(
                code="INDEX_MISSING_TENANT_PREDICATE",
                message=(f"IndexNode on {coll!r} does not equality-match {tenant_field!r} == @tenantId"),
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation

    def _check_traversal(
        self,
        node: dict[str, Any],
        *,
        digests: dict[str, str],
        session: Any,
    ) -> None:
        # 1) Every vertex collection in play is satellite → OK.
        vertex_colls = self._traversal_vertex_collections(node)
        if vertex_colls and all(self._layout_kind(c) == "satellite" for c in vertex_colls):
            return
        # 2) prune references @tenantId → OK.
        if _traversal_prune_uses_tenant(node):
            return
        # 3) graphName resolves to a disjoint smartgraph → OK.
        graph_name = node.get("graphName")
        if isinstance(graph_name, str) and graph_name and self._is_disjoint_smartgraph(graph_name):
            return
        violation = TenantScopeViolation(
            code="UNCONSTRAINED_TRAVERSAL",
            message=(
                "TraversalNode "
                f"graph={graph_name!r} vertex_collections={vertex_colls!r}"
                " is not constrained to the session tenant (no satellite-only "
                "path, no prune on @tenantId, no disjoint smartgraph)"
            ),
            **digests,
        )
        _log_violation(violation, session=session)
        raise violation

    def _traversal_vertex_collections(self, node: dict[str, Any]) -> list[str]:
        # The plan exposes the resolved vertex collections under
        # ``vertices`` (per-step) or under ``graph.vertexCollections``
        # (named-graph case).
        graph = node.get("graph") or {}
        if isinstance(graph, dict):
            verts = graph.get("vertexCollections") or graph.get("vertex_collections")
            if isinstance(verts, list):
                return [v for v in verts if isinstance(v, str)]
        graph_name = node.get("graphName")
        if isinstance(graph_name, str) and graph_name:
            return self._graph_vertex_collections(graph_name)
        return []

    # ---- Predicate detection ---------------------------------------------

    def _has_tenant_predicate(self, enum_node: dict[str, Any], collection: str) -> bool:
        """Whether the plan constrains the EnumerateCollectionNode's
        output variable to the session tenant on the collection's
        tenant field.

        Inspects both the inline ``EnumerateCollectionNode.filter`` (the
        fused shape real ArangoDB emits) and every standalone
        CalculationNode, descending only through ``AND`` conjunctions.
        A conjunct is accepted when the tenant field is compared ``==``
        to either the ``@tenantId`` *parameter* or a *value* literal
        equal to the per-call ``tenant_sentinel``.
        """
        outvar = _outvar_name(enum_node)
        if not outvar:
            return False
        tenant_field = self._tenant_field_for(collection)
        if not tenant_field:
            return False
        for expr in self._tenant_field_exprs(enum_node, outvar):
            if _expr_scopes_tenant(
                expr, var_name=outvar, attr=tenant_field, sentinel=self.tenant_sentinel
            ):
                return True
        return False

    def _has_mismatching_tenant_literal(
        self,
        enum_node: dict[str, Any],
        collection: str,
    ) -> Any:
        """Return a literal value if the plan compares the enum's tenant
        field against a string literal that is **not** the per-call
        sentinel — i.e. a caller-smuggled tenant id Layer 5 refuses to
        honour. A literal equal to the sentinel is the resolved
        ``@tenantId`` bind and is not flagged (and is handled by
        :meth:`_has_tenant_predicate` anyway). Descends through both
        ``AND`` and ``OR`` since a mismatching literal anywhere is
        suspicious.
        """
        outvar = _outvar_name(enum_node)
        if not outvar:
            return None
        tenant_field = self._tenant_field_for(collection)
        if not tenant_field:
            return None
        for expr in self._tenant_field_exprs(enum_node, outvar):
            literal = _expr_mismatching_tenant_literal(
                expr, var_name=outvar, attr=tenant_field, sentinel=self.tenant_sentinel
            )
            if literal is not None:
                return literal
        return None

    def _tenant_field_exprs(self, enum_node: dict[str, Any], outvar: str) -> list[dict[str, Any]]:
        """Candidate expressions that may carry the tenant predicate for
        *enum_node*: the inline fused ``filter`` plus every standalone
        CalculationNode's expression.
        """
        exprs: list[dict[str, Any]] = []
        inline = enum_node.get("filter")
        if isinstance(inline, dict):
            exprs.append(inline)
        for calc in self._calc_by_outvar.values():
            e = _expr(calc)
            if e:
                exprs.append(e)
        return exprs

    def _has_tenant_root_predicate(self, enum_node: dict[str, Any]) -> bool:
        outvar = _outvar_name(enum_node)
        if not outvar:
            return False
        for calc in self._calc_by_outvar.values():
            if _calc_matches_key_eq_bindvar(calc, outvar, "tenantKey"):
                return True
            if _calc_matches_tenant_eq_bindvar(calc, outvar, "_key"):
                return True
        return False


# ---------------------------------------------------------------------------
# Pure expression-matchers (extracted so they're unit-testable)
# ---------------------------------------------------------------------------


def _outvar_name(node: dict[str, Any]) -> str | None:
    out = node.get("outVariable")
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
    """Match ``<var_name>.<attr>`` — i.e. an ``attribute access`` node
    whose subNode is a ``reference`` to *var_name* and whose ``name``
    is *attr*."""
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
    """Match ``@<name>`` — a ``parameter`` node with ``name == name``."""
    return expr.get("type") == "parameter" and expr.get("name") == name


def _is_value_literal(expr: dict[str, Any]) -> bool:
    return expr.get("type") == "value"


def _value_of_literal(expr: dict[str, Any]) -> Any:
    return expr.get("value")


def _compare_eq_subnodes(expr: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """If *expr* is an equality compare, return its two operand subNodes."""
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
    """``CalculationNode`` whose expression is
    ``<var_name>.<attr> == @tenantId`` (either operand order)."""
    expr = _expr(calc)
    sides = _compare_eq_subnodes(expr)
    if sides is None:
        return False
    lhs, rhs = sides
    return (
        _is_attribute_access_on(lhs, var_name=var_name, attr=attr) and _is_bindvar_named(rhs, "tenantId")
    ) or (_is_attribute_access_on(rhs, var_name=var_name, attr=attr) and _is_bindvar_named(lhs, "tenantId"))


def _calc_matches_tenant_eq_literal(
    calc: dict[str, Any],
    var_name: str,
    attr: str,
) -> Any:
    """If the calculation's expression is
    ``<var_name>.<attr> == <literal>``, return the literal. Else None.

    Treats the equality as symmetric and only fires when the literal
    operand is a ``value`` node — bind-var equality returns None and
    is the path :func:`_calc_matches_tenant_eq_bindvar` accepts.
    """
    expr = _expr(calc)
    sides = _compare_eq_subnodes(expr)
    if sides is None:
        return None
    lhs, rhs = sides
    if _is_attribute_access_on(lhs, var_name=var_name, attr=attr) and _is_value_literal(rhs):
        return _value_of_literal(rhs)
    if _is_attribute_access_on(rhs, var_name=var_name, attr=attr) and _is_value_literal(lhs):
        return _value_of_literal(lhs)
    return None


def _is_tenant_value_operand(expr: dict[str, Any], *, sentinel: str | None) -> bool:
    """Operand that ties a comparison to the session tenant.

    Accepts the ``@tenantId`` bind *parameter* (the hand-crafted /
    plan-override shape, and any ArangoDB build that does not fold the
    bind) or a ``value`` literal equal to the per-call *sentinel* (the
    resolved-bind shape real EXPLAIN produces). A literal that is not
    the sentinel — including ``None`` sentinel — is rejected so a
    caller cannot smuggle another tenant's id as a literal.
    """
    if _is_bindvar_named(expr, "tenantId"):
        return True
    if sentinel is not None and _is_value_literal(expr) and _value_of_literal(expr) == sentinel:
        return True
    return False


def _eq_scopes_tenant(
    expr: dict[str, Any],
    *,
    var_name: str,
    attr: str,
    sentinel: str | None,
) -> bool:
    """``<var>.<attr> == (@tenantId | sentinel-literal)`` in either order."""
    sides = _compare_eq_subnodes(expr)
    if sides is None:
        return False
    lhs, rhs = sides
    return (
        _is_attribute_access_on(lhs, var_name=var_name, attr=attr)
        and _is_tenant_value_operand(rhs, sentinel=sentinel)
    ) or (
        _is_attribute_access_on(rhs, var_name=var_name, attr=attr)
        and _is_tenant_value_operand(lhs, sentinel=sentinel)
    )


def _expr_scopes_tenant(
    expr: dict[str, Any],
    *,
    var_name: str,
    attr: str,
    sentinel: str | None,
) -> bool:
    """Whether *expr* constrains ``<var>.<attr>`` to the session tenant.

    A bare equality compare is checked directly. Conjunctions
    (``logical and`` / ``n-ary and``) are descended — a tenant
    predicate ANDed with anything still scopes the scan. ``OR`` is
    **never** descended: ``tenantId == @t OR x`` does not guarantee
    scoping, so it must not be accepted.
    """
    if not isinstance(expr, dict):
        return False
    if _eq_scopes_tenant(expr, var_name=var_name, attr=attr, sentinel=sentinel):
        return True
    if expr.get("type") in {"logical and", "n-ary and"}:
        for sub in expr.get("subNodes") or []:
            if isinstance(sub, dict) and _expr_scopes_tenant(
                sub, var_name=var_name, attr=attr, sentinel=sentinel
            ):
                return True
    return False


def _expr_mismatching_tenant_literal(
    expr: dict[str, Any],
    *,
    var_name: str,
    attr: str,
    sentinel: str | None,
) -> Any:
    """Return a literal if *expr* compares ``<var>.<attr>`` to a string
    literal that is not the *sentinel*, else ``None``.

    Descends through both ``AND`` and ``OR`` — a mismatching literal
    anywhere in the predicate tree is a smuggling attempt. A literal
    equal to the sentinel is the resolved ``@tenantId`` bind and is not
    flagged.
    """
    if not isinstance(expr, dict):
        return None
    sides = _compare_eq_subnodes(expr)
    if sides is not None:
        lhs, rhs = sides
        for field_side, val_side in ((lhs, rhs), (rhs, lhs)):
            if _is_attribute_access_on(field_side, var_name=var_name, attr=attr) and _is_value_literal(val_side):
                value = _value_of_literal(val_side)
                if sentinel is None or value != sentinel:
                    return value
    for sub in expr.get("subNodes") or []:
        if isinstance(sub, dict):
            hit = _expr_mismatching_tenant_literal(sub, var_name=var_name, attr=attr, sentinel=sentinel)
            if hit is not None:
                return hit
    return None


def _calc_matches_key_eq_bindvar(
    calc: dict[str, Any],
    var_name: str,
    bindvar_name: str,
) -> bool:
    expr = _expr(calc)
    sides = _compare_eq_subnodes(expr)
    if sides is None:
        return False
    lhs, rhs = sides
    return (
        _is_attribute_access_on(lhs, var_name=var_name, attr="_key") and _is_bindvar_named(rhs, bindvar_name)
    ) or (
        _is_attribute_access_on(rhs, var_name=var_name, attr="_key") and _is_bindvar_named(lhs, bindvar_name)
    )


def _index_covers_tenant(
    node: dict[str, Any],
    tenant_field: str | None,
    *,
    sentinel: str | None = None,
) -> bool:
    """IndexNode condition references *tenant_field* == @tenantId.

    ArangoDB's IndexNode embeds the resolved index condition in
    ``node["condition"]["subNodes"]`` as an n-ary tree. We walk it
    looking for the ``attribute access`` compared against the
    ``@tenantId`` parameter or the resolved-bind *sentinel* literal.
    """
    if not tenant_field:
        return False
    outvar = _outvar_name(node)
    if not outvar:
        return False
    cond = node.get("condition")
    return _condition_covers_tenant(cond, outvar=outvar, tenant_field=tenant_field, sentinel=sentinel)


def _condition_covers_tenant(
    cond: Any,
    *,
    outvar: str,
    tenant_field: str,
    sentinel: str | None = None,
) -> bool:
    if not isinstance(cond, dict):
        return False
    sides = _compare_eq_subnodes(cond)
    if sides is not None:
        lhs, rhs = sides
        if (
            _is_attribute_access_on(lhs, var_name=outvar, attr=tenant_field)
            and _is_tenant_value_operand(rhs, sentinel=sentinel)
        ) or (
            _is_attribute_access_on(rhs, var_name=outvar, attr=tenant_field)
            and _is_tenant_value_operand(lhs, sentinel=sentinel)
        ):
            return True
    for sub in cond.get("subNodes") or []:
        if isinstance(sub, dict) and _condition_covers_tenant(
            sub, outvar=outvar, tenant_field=tenant_field, sentinel=sentinel
        ):
            return True
    return False


def _index_keyed_by_tenant_key(node: dict[str, Any]) -> bool:
    """IndexNode condition references ``_key == @tenantKey``."""
    outvar = _outvar_name(node)
    if not outvar:
        return False
    cond = node.get("condition")
    return _condition_keyed_by_tenant_key(cond, outvar=outvar)


def _condition_keyed_by_tenant_key(cond: Any, *, outvar: str) -> bool:
    if not isinstance(cond, dict):
        return False
    sides = _compare_eq_subnodes(cond)
    if sides is not None:
        lhs, rhs = sides
        if (
            _is_attribute_access_on(lhs, var_name=outvar, attr="_key") and _is_bindvar_named(rhs, "tenantKey")
        ) or (
            _is_attribute_access_on(rhs, var_name=outvar, attr="_key") and _is_bindvar_named(lhs, "tenantKey")
        ):
            return True
    for sub in cond.get("subNodes") or []:
        if isinstance(sub, dict) and _condition_keyed_by_tenant_key(sub, outvar=outvar):
            return True
    return False


def _traversal_prune_uses_tenant(node: dict[str, Any]) -> bool:
    """TraversalNode's ``options.prune`` references ``@tenantId``."""
    options = node.get("options") or {}
    if not isinstance(options, dict):
        return False
    prune = options.get("prune")
    if isinstance(prune, str):
        return "@tenantId" in prune or "@@tenantId" in prune
    if isinstance(prune, dict):
        return _expr_references_tenant_bindvar(prune)
    return False


def _expr_references_tenant_bindvar(expr: dict[str, Any]) -> bool:
    """Recursive: does *expr* contain a ``parameter`` node named
    ``tenantId``?"""
    if not isinstance(expr, dict):
        return False
    if _is_bindvar_named(expr, "tenantId"):
        return True
    for sub in expr.get("subNodes") or []:
        if isinstance(sub, dict) and _expr_references_tenant_bindvar(sub):
            return True
    return False


# ---------------------------------------------------------------------------
# Plan / bind-var coercion + digests + logging
# ---------------------------------------------------------------------------


def _coerce_plan(result: Any) -> Any:
    """``db.aql.explain`` returns ``{"plan": ..., "warnings": ...}`` in
    most python-arango versions; in older ones it returns the plan
    directly. Tolerate both shapes."""
    if isinstance(result, dict) and "plan" in result and isinstance(result["plan"], dict):
        return result["plan"]
    return result


def _digests(
    *,
    aql: str,
    bind_vars: dict[str, Any],
    plan: dict[str, Any] | None,
) -> dict[str, str]:
    aql_payload = aql + "\n" + json.dumps(bind_vars, sort_keys=True, default=str)
    aql_digest = hashlib.sha256(aql_payload.encode("utf-8")).hexdigest()
    if plan is None:
        plan_digest = ""
    else:
        plan_json = json.dumps(plan, sort_keys=True, default=str)
        plan_digest = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
    return {"aql_digest": aql_digest, "plan_digest": plan_digest}


def _log_violation(violation: TenantScopeViolation, *, session: Any) -> None:
    token_prefix = _session_token_prefix(session)
    logger.warning(
        "TENANT_SCOPE_VIOLATION code=%s session=%s tenant=%s aql_digest=%s plan_digest=%s message=%s",
        violation.code,
        token_prefix,
        getattr(session, "tenant_id", None),
        violation.aql_digest[:16],
        violation.plan_digest[:16],
        violation.message,
    )


def _log_pass(
    *,
    session: Any,
    aql_digest: str,
    plan_digest: str,
) -> None:
    token_prefix = _session_token_prefix(session)
    logger.info(
        "TENANT_SCOPE_OK session=%s tenant=%s aql_digest=%s plan_digest=%s",
        token_prefix,
        getattr(session, "tenant_id", None),
        aql_digest[:16],
        plan_digest[:16],
    )


def _session_token_prefix(session: Any) -> str:
    token = getattr(session, "token", "") or ""
    return token[:8] if token else "-"


__all__ = [
    "TenantScopeViolation",
    "validate_plan",
]
