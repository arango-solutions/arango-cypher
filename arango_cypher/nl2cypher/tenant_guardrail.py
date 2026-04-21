"""Tenant-scoping postcondition for NL → Cypher / AQL translation.

When the mapping bundle declares a ``Tenant`` entity and the caller
supplies an active :class:`TenantContext`, the emitted Cypher MUST be
scoped to that tenant. Otherwise the query silently escapes tenant
isolation and returns cross-tenant rows — a data-leak-class bug in a
multi-tenant graph, not a translation-quality nit.

What "scoped" means is **manifest-driven**, not hardcoded:

* If the query touches a tenant-scoped entity that carries a
  denormalised tenant field (e.g. ``Device.TENANT_ID``), filtering on
  that field with the active tenant value is sufficient — no
  ``:Tenant`` binding required, because the planner can satisfy the
  scope with an indexed equality on the column.
* If the query touches a tenant-scoped entity that has no denorm
  field, the only way to scope it is via traversal from a
  ``:Tenant`` node, and the guardrail enforces that.
* If the query touches **only** GLOBAL entities (e.g. ``Cve``,
  ``AppVersion``), the guardrail does not fire — those collections are
  intentionally cross-tenant reference data and forcing a
  ``:Tenant`` binding would refuse legitimate questions like "list
  all CVEs".

The classification of which entity is which comes from
:func:`arango_cypher.nl2cypher.tenant_scope.analyze_tenant_scope` —
keeping that knowledge in a separate module means we can replace the
heuristic with the upstream schema analyzer's first-class
``tenantScope`` annotation without churning this file.

The guardrail is invoked inside
:func:`arango_cypher.nl2cypher.nl_to_cypher` after every LLM emission.
A violation is fed back into the retry loop so the model can correct
itself; if the retry budget is exhausted the caller receives an
empty-Cypher result with an explanation — the translator
**fails closed**, never silently returning a cross-tenant query.

Back-compat note
----------------
``prompt_section`` and ``check_tenant_scope`` both accept a manifest
optionally. Callers that don't pass one fall through to the v1
behaviour (force a ``:Tenant`` binding, hardcoded ``TENANT_ID`` hint
in the prompt). New code should always pass a manifest — the
fallback exists only to keep the public API stable for downstream
consumers who haven't migrated yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .tenant_scope import (
    EntityTenantRole,
    TenantScopeManifest,
)

# Match `:Tenant` as a standalone label; explicitly reject
# `:TenantUser`, `:TenantCVE`, `:TenantAppVersion`, etc.
_TENANT_LABEL_RE = re.compile(r":\s*Tenant\b(?!\w)")
_TENANT_ENTITY_NAMES = ("Tenant",)

# Capture every node-label occurrence `:LabelName` in a Cypher string.
# We intentionally use a permissive scan rather than a real parser:
# the guardrail runs on every LLM emission and on the retry hot path,
# so a stdlib regex beats spinning up the ANTLR parser. False
# positives (e.g. labels inside string literals) are tolerable
# because the worst case is a rejected query, which the retry loop
# can correct — strictly preferable to letting a real cross-tenant
# query through.
_LABEL_RE = re.compile(r":\s*([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class TenantContext:
    """Ambient tenant scope applied to every NL translation in a session.

    Attributes
    ----------
    property:
        Physical property name on the ``Tenant`` entity used to
        match the scope. The canonical value is ``"_key"`` — the
        ArangoDB document key, which is unique within the
        collection, automatically indexed, and transpiles to the
        cheapest possible filter (``t._key == '<uuid>'``). Older
        clients / hand-supplied contexts may use ``"NAME"``,
        ``"SUBDOMAIN"``, or ``"TENANT_HEX_ID"`` — those still work
        but are slower, schema-specific, and not guaranteed unique.
    value:
        The exact value to match. For ``property="_key"`` this is
        the bare key (e.g. ``"001c463d-500d-47c7-bc32-c824eb42f064"``,
        not the full ``"Tenant/001c463d-..."`` ``_id``).
    display:
        Optional human-readable label for prompts and UI (e.g.
        ``"Dagster Labs"``). Defaults to ``value`` when omitted.
    """

    property: str
    value: str
    display: str | None = None

    @property
    def display_name(self) -> str:
        return self.display or self.value


@dataclass(frozen=True)
class TenantScopeViolation:
    """Diagnostic for a translation that dropped the tenant constraint."""

    tenant_property: str
    tenant_value: str
    reason: str
    suggested_hint: str


# ---------------------------------------------------------------------------
# Mapping introspection helpers (kept for back-compat with v1 callers)
# ---------------------------------------------------------------------------


def has_tenant_entity(bundle_or_dict: Any) -> bool:
    """Return ``True`` if the mapping declares a ``Tenant`` entity.

    Accepts either a :class:`~arango_query_core.mapping.MappingBundle`
    or a plain ``dict`` (in which case both ``conceptual_schema`` and
    ``conceptualSchema`` keys are tried).
    """
    cs: dict[str, Any] | None
    if hasattr(bundle_or_dict, "conceptual_schema"):
        cs = bundle_or_dict.conceptual_schema or {}
    elif isinstance(bundle_or_dict, dict):
        cs = (
            bundle_or_dict.get("conceptual_schema")
            or bundle_or_dict.get("conceptualSchema")
            or {}
        )
    else:
        return False
    if not isinstance(cs, dict):
        return False
    entities = cs.get("entities") or []
    if not isinstance(entities, list):
        return False
    names = {
        e.get("name") for e in entities
        if isinstance(e, dict) and isinstance(e.get("name"), str)
    }
    return any(n in names for n in _TENANT_ENTITY_NAMES)


def cypher_binds_tenant(cypher: str) -> bool:
    """Return ``True`` if any clause binds a ``:Tenant`` node."""
    return bool(_TENANT_LABEL_RE.search(cypher or ""))


def cypher_referenced_labels(cypher: str) -> set[str]:
    """Return the set of node labels referenced in ``cypher``.

    Used by :func:`check_tenant_scope` to decide whether the query
    touches any tenant-scoped entity at all. Never raises — returns
    an empty set on garbage input.
    """
    if not cypher:
        return set()
    return {m.group(1) for m in _LABEL_RE.finditer(cypher)}


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------


def _denorm_filter_satisfied(
    cypher: str,
    *,
    field_name: str,
    tenant_value: str,
) -> bool:
    """Return ``True`` if the Cypher contains a filter equating
    ``field_name`` to ``tenant_value``.

    Accepts the common shapes the LLM emits:

    * ``WHERE x.TENANT_ID = '<value>'``
    * ``WHERE x.TENANT_ID == '<value>'``
    * ``MATCH (x:Device {TENANT_ID: '<value>'})``
    * ``WHERE x.TENANT_ID IN ['<value>']`` (degenerate single-value form)

    Quote style (single vs double) and whitespace are tolerated.
    """
    if not cypher or not field_name or not tenant_value:
        return False
    f = re.escape(field_name)
    v = re.escape(tenant_value)
    # Equality: x.<field> = '<value>' or x.<field> == '<value>'
    eq = re.compile(
        rf"\.\s*{f}\s*={{1,2}}\s*['\"]{v}['\"]",
    )
    if eq.search(cypher):
        return True
    # Inline node properties: {<field>: '<value>'}
    inline = re.compile(rf"\{{[^{{}}]*\b{f}\s*:\s*['\"]{v}['\"]")
    if inline.search(cypher):
        return True
    return False


def check_tenant_scope(
    cypher: str,
    *,
    tenant_context: TenantContext | None,
    manifest: TenantScopeManifest | None = None,
) -> TenantScopeViolation | None:
    """Return a :class:`TenantScopeViolation` if isolation was breached.

    Returns ``None`` in any of these cases:

    * ``tenant_context`` is ``None`` — the caller has not opted into
      tenant scoping, so there is no constraint to enforce.
    * ``manifest`` is provided and the Cypher references **only**
      entities classified as ``GLOBAL`` (e.g. metadata tables) — the
      query is intentionally cross-tenant and forcing a ``:Tenant``
      binding would refuse legitimate metadata-only queries.
    * The emitted Cypher binds a ``:Tenant`` node — assumed to be
      scoped correctly by traversal.
    * ``manifest`` is provided, the Cypher touches at least one
      tenant-scoped entity that carries a denormalised tenant field,
      AND the Cypher contains a filter equating that field to
      ``tenant_context.value`` — the planner can satisfy the scope
      with an indexed equality, no traversal needed.

    A violation is returned only when a tenant context is active AND
    none of the above scope-satisfaction paths are present.
    """
    if tenant_context is None:
        return None

    # Manifest-aware path: skip GLOBAL-only and accept denorm-filter scoping.
    if manifest is not None:
        labels = cypher_referenced_labels(cypher)
        if labels:
            roles = {manifest.role_of(label) for label in labels}
            if roles and EntityTenantRole.TENANT_SCOPED not in roles \
                    and EntityTenantRole.TENANT_ROOT not in roles:
                # Only GLOBAL entities are referenced. The query is
                # tenant-independent by construction (e.g.
                # `MATCH (c:Cve) RETURN c`). Allow.
                return None

        if cypher_binds_tenant(cypher):
            return None

        # Accept a denorm-field filter as scope satisfaction. Look at
        # every scoped entity referenced in the query and check
        # whether the Cypher carries a filter on its denorm field
        # using the active tenant value.
        for label in labels:
            field_name = manifest.denorm_field_of(label)
            if field_name and _denorm_filter_satisfied(
                cypher,
                field_name=field_name,
                tenant_value=tenant_context.value,
            ):
                return None
    elif cypher_binds_tenant(cypher):
        # No manifest: fall back to the v1 contract — a `:Tenant`
        # binding is the only acceptance condition.
        return None

    return _build_violation(cypher, tenant_context, manifest)


def _build_violation(
    cypher: str,
    tenant_context: TenantContext,
    manifest: TenantScopeManifest | None,
) -> TenantScopeViolation:
    """Render a violation with a hint tailored to the schema's denorm fields."""
    if tenant_context.property == "_key":
        match_pattern = f"(t:Tenant {{_key: '{tenant_context.value}'}})"
    else:
        match_pattern = (
            f"(t:Tenant {{{tenant_context.property}: "
            f"{tenant_context.value!r}}})"
        )

    # If the manifest tells us at least one referenced entity has a
    # denorm field, suggest the cheaper denorm-filter form first.
    denorm_hint: str | None = None
    if manifest is not None:
        for label in cypher_referenced_labels(cypher):
            field_name = manifest.denorm_field_of(label)
            if field_name:
                denorm_hint = (
                    f"`MATCH ({label[0].lower()}:{label}) "
                    f"WHERE {label[0].lower()}.{field_name} = "
                    f"'{tenant_context.value}' …`"
                )
                break

    if denorm_hint is not None:
        suggested_hint = (
            "Either filter the target entity directly on its tenant-id "
            f"field, e.g. {denorm_hint}, OR bind the tenant in MATCH "
            f"and traverse to the target via the schema's tenant edges, "
            f"e.g. {match_pattern}<-[:…]-(target)."
        )
    else:
        suggested_hint = (
            "Bind the tenant in MATCH and traverse to the target entity "
            f"through its tenant-scoping path, e.g. {match_pattern}"
            "<-[:…]-(target). The schema does not expose a denormalised "
            "tenant field on the target collection, so a graph traversal "
            "from :Tenant is required."
        )

    return TenantScopeViolation(
        tenant_property=tenant_context.property,
        tenant_value=tenant_context.value,
        reason=(
            f"Query must be scoped to tenant {tenant_context.display_name!r} "
            f"(Tenant.{tenant_context.property}) but the translated Cypher "
            "neither binds a :Tenant node nor filters a tenant-scoped "
            "entity by its denormalised tenant field — it would return "
            "cross-tenant results."
        ),
        suggested_hint=suggested_hint,
    )


# ---------------------------------------------------------------------------
# Prompt section (manifest-driven)
# ---------------------------------------------------------------------------


def prompt_section(
    tenant_context: TenantContext | None,
    manifest: TenantScopeManifest | None = None,
) -> str:
    """Render the "## Current tenant scope" block for the system prompt.

    Returns the empty string when no tenant context is active so the
    prompt remains byte-identical to the pre-guardrail shape for
    single-tenant graphs.

    When a ``manifest`` is supplied, the block is fully data-driven:
    it lists the schema's tenant-scoped entities in two groups (those
    that can be filtered directly via a denormalised field, and those
    that require traversal from ``:Tenant``), and a third group of
    GLOBAL entities the model is told *not* to scope. This avoids the
    v1 footgun of telling the LLM "every collection has a TENANT_ID" —
    which it doesn't, and which led the model to invent fields and
    refuse legitimate metadata queries.

    Without a manifest, falls back to the v1 wording (kept for
    out-of-tree consumers that haven't migrated yet).
    """
    if tenant_context is None:
        return ""

    match_hint, scope_clause = _format_match_hint(tenant_context)

    if manifest is None:
        return _legacy_prompt_body(tenant_context, match_hint, scope_clause)

    return _manifest_prompt_body(
        tenant_context, manifest, match_hint, scope_clause,
    )


def _format_match_hint(
    tenant_context: TenantContext,
) -> tuple[str, str]:
    """Return ``(match_hint, scope_clause)`` strings for the prompt header."""
    if tenant_context.property == "_key":
        match_hint = (
            f"`MATCH (t:Tenant {{_key: '{tenant_context.value}'}})` — "
            "the `_key` property is the ArangoDB document key for the "
            "Tenant document and is the canonical, indexed tenant "
            "identifier."
        )
        scope_clause = f"match Tenant._key == {tenant_context.value!r}"
    else:
        match_hint = (
            f"`MATCH (t:Tenant {{{tenant_context.property}: "
            f"{tenant_context.value!r}}})`"
        )
        scope_clause = (
            f"match Tenant.{tenant_context.property} == "
            f"{tenant_context.value!r}"
        )
    return match_hint, scope_clause


def _manifest_prompt_body(
    tenant_context: TenantContext,
    manifest: TenantScopeManifest,
    match_hint: str,
    scope_clause: str,
) -> str:
    # Group scoped entities by whether they have a denorm field.
    denorm_entries: list[tuple[str, str]] = []  # (entity, field)
    traversal_entries: list[str] = []
    for name in sorted(manifest.scoped_entities()):
        field_name = manifest.denorm_field_of(name)
        if field_name:
            denorm_entries.append((name, field_name))
        else:
            traversal_entries.append(name)
    global_entries = sorted(manifest.global_entities())

    lines: list[str] = [
        "## Current tenant scope",
        f"Active tenant: {tenant_context.display_name!r} ({scope_clause}).",
        "",
        (
            "Per-entity scoping rules (derived from the mapping; do NOT "
            "invent fields that are not listed below):"
        ),
        "",
    ]

    if denorm_entries:
        # The denorm field stores the Tenant `_key`, not whatever
        # property the operator chose to scope by. When the active
        # context is keyed on `_key` we can substitute the literal;
        # otherwise we use a `<Tenant._key>` placeholder so the LLM
        # knows it must first resolve the key (typically by also
        # binding :Tenant) rather than splicing in the wrong value
        # (e.g. a TENANT_HEX_ID).
        if tenant_context.property == "_key":
            denorm_value_repr = f"'{tenant_context.value}'"
        else:
            denorm_value_repr = "<Tenant._key>"
        lines.append(
            "**Tenant-scoped via denormalised field** — prefer a direct "
            "filter on the listed field (cheap, indexed, no traversal):"
        )
        for entity, field_name in denorm_entries:
            var = entity[0].lower()
            example = (
                f"  - `{entity}`: filter on `{field_name}` — "
                f"`MATCH ({var}:{entity}) WHERE "
                f"{var}.{field_name} = {denorm_value_repr}`"
            )
            lines.append(example)
        lines.append("")

    if traversal_entries:
        lines.append(
            "**Tenant-scoped via traversal only** — these entities have no "
            "denormalised tenant field; reach them by binding :Tenant and "
            "traversing the schema's edges:"
        )
        lines.append("  - " + ", ".join(traversal_entries))
        lines.append(f"  - Bind :Tenant first using {match_hint}")
        lines.append("")

    if global_entries:
        lines.append(
            "**Global / metadata (do NOT scope)** — these entities are "
            "intentionally cross-tenant. Querying them must NOT include "
            "any tenant filter or :Tenant binding:"
        )
        lines.append("  - " + ", ".join(global_entries))
        lines.append("")

    lines.append(
        "Do NOT mix scope styles in a single MATCH (don't bind :Tenant by "
        "`_key` and then re-filter the target by some other tenant-ish "
        "field — pick one consistent identifier)."
    )
    return "\n".join(lines)


def _legacy_prompt_body(
    tenant_context: TenantContext,
    match_hint: str,
    scope_clause: str,
) -> str:
    """v1 prompt body (no manifest available).

    Kept verbatim so out-of-tree callers that don't yet pass a
    manifest get the same wording they had before — this preserves
    the byte-identical-prompt invariant pinned in
    ``test_no_tenant_context_leaves_prompt_byte_identical``.
    """
    body = (
        "## Current tenant scope\n"
        f"All queries MUST be scoped to tenant "
        f"{tenant_context.display_name!r} ({scope_clause}).\n"
        "Every MATCH clause must include a :Tenant node bound to this "
        f"value: {match_hint}\n\n"
        "Bind the tenant in MATCH and traverse to the target entity via "
        "the schema's tenant-scoping relationship (e.g. "
        "`(:Tenant)<-[:TENANTUSERTENANT]-(:TenantUser)<-[:GSUITEUSERTENANTUSER]-"
        "(target)`)."
    )
    return body


__all__ = [
    "TenantContext",
    "TenantScopeViolation",
    "check_tenant_scope",
    "cypher_binds_tenant",
    "cypher_referenced_labels",
    "has_tenant_entity",
    "prompt_section",
]
