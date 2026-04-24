"""Agentic tool wrappers for LLM function-calling and tool-use patterns.

Provides JSON-in/JSON-out functions suitable for OpenAI function calling,
LangChain tools, MCP tool servers, and similar agent frameworks.

Usage::

    from arango_cypher.tools import translate_tool, suggest_indexes_tool

    # Function-calling style
    result = translate_tool({
        "cypher": "MATCH (p:Person) RETURN p.name",
        "mapping": {...},
    })
"""

from __future__ import annotations

from typing import Any

from arango_query_core import CoreError, MappingBundle, MappingResolver, mapping_from_wire_dict

from .api import get_cypher_profile, translate
from .parser import parse_cypher

# ---------------------------------------------------------------------------
# Tool: translate
# ---------------------------------------------------------------------------

TRANSLATE_TOOL_SCHEMA = {
    "name": "cypher_translate",
    "description": (
        "Translate a Cypher graph query into ArangoDB AQL. "
        "Returns the AQL text, bind variables, and any warnings."
    ),
    "parameters": {
        "type": "object",
        "required": ["cypher", "mapping"],
        "properties": {
            "cypher": {
                "type": "string",
                "description": "The Cypher query to translate.",
            },
            "mapping": {
                "type": "object",
                "description": (
                    "Schema mapping with conceptualSchema and physicalMapping. "
                    "Defines how Cypher labels map to ArangoDB collections."
                ),
            },
            "params": {
                "type": "object",
                "description": "Optional query parameters (bind variables).",
            },
        },
    },
}


def translate_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Translate Cypher to AQL — designed for agent function-calling.

    Args:
        request: Dict with keys ``cypher``, ``mapping``, and optional ``params``.

    Returns:
        Dict with ``aql``, ``bind_vars``, ``warnings``, or ``error``.
    """
    cypher = request.get("cypher", "")
    mapping_dict = request.get("mapping")
    params = request.get("params")

    if not cypher:
        return {"error": "cypher is required", "code": "INVALID_ARGUMENT"}
    if not mapping_dict:
        return {"error": "mapping is required", "code": "INVALID_ARGUMENT"}

    try:
        bundle = _dict_to_bundle(mapping_dict)
        result = translate(cypher, mapping=bundle, params=params)
        return {
            "aql": result.aql,
            "bind_vars": result.bind_vars,
            "warnings": result.warnings,
        }
    except CoreError as e:
        return {"error": str(e), "code": e.code}
    except Exception as e:
        return {"error": str(e), "code": "INTERNAL_ERROR"}


# ---------------------------------------------------------------------------
# Tool: suggest_indexes
# ---------------------------------------------------------------------------

SUGGEST_INDEXES_TOOL_SCHEMA = {
    "name": "suggest_indexes",
    "description": (
        "Analyze a schema mapping and suggest ArangoDB indexes that would improve query performance."
    ),
    "parameters": {
        "type": "object",
        "required": ["mapping"],
        "properties": {
            "mapping": {
                "type": "object",
                "description": "Schema mapping with conceptualSchema and physicalMapping.",
            },
        },
    },
}


def suggest_indexes_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Suggest indexes for an ArangoDB graph schema.

    Returns:
        Dict with ``suggestions`` list of index recommendations.
    """
    mapping_dict = request.get("mapping")
    if not mapping_dict:
        return {"error": "mapping is required", "code": "INVALID_ARGUMENT"}

    try:
        bundle = _dict_to_bundle(mapping_dict)
        return {"suggestions": _analyze_indexes(bundle)}
    except Exception as e:
        return {"error": str(e), "code": "INTERNAL_ERROR"}


def _analyze_indexes(bundle: MappingBundle) -> list[dict[str, Any]]:
    """Produce index suggestions based on the mapping."""
    suggestions: list[dict[str, Any]] = []
    pm = bundle.physical_mapping
    cs = bundle.conceptual_schema
    resolver = MappingResolver(bundle)

    rels = pm.get("relationships", {})
    for rtype, rmap in rels.items():
        if not isinstance(rmap, dict):
            continue
        style = rmap.get("style", "")

        if style == "GENERIC_WITH_TYPE":
            edge_coll = rmap.get("edgeCollectionName", "")
            type_field = rmap.get("typeField", "type")
            if not resolver.has_vci(rtype):
                suggestions.append(
                    {
                        "type": "persistent_index",
                        "collection": edge_coll,
                        "fields": [type_field],
                        "reason": (
                            f"Relationship '{rtype}' uses GENERIC_WITH_TYPE pattern. "
                            f"A persistent index on '{type_field}' enables vertex-centric "
                            f"filtering during traversals."
                        ),
                        "priority": "high",
                    }
                )

    entities = pm.get("entities", {})
    for ename, emap in entities.items():
        if not isinstance(emap, dict):
            continue
        coll = emap.get("collectionName", "")
        style = emap.get("style", "")

        if style == "LABEL":
            type_field = emap.get("typeField", "type")
            suggestions.append(
                {
                    "type": "persistent_index",
                    "collection": coll,
                    "fields": [type_field],
                    "reason": (
                        f"Entity '{ename}' uses LABEL style in collection '{coll}'. "
                        f"An index on '{type_field}' speeds up type-based lookups."
                    ),
                    "priority": "medium",
                }
            )

    for entity_def in cs.get("entities", []):
        ename = entity_def.get("name", "")
        emap = entities.get(ename, {})
        coll = emap.get("collectionName", "") if isinstance(emap, dict) else ""
        if not coll:
            continue
        for prop in entity_def.get("properties", []):
            pname = prop.get("name", "")
            if pname in ("name", "title", "email", "username", "key", "code", "id"):
                suggestions.append(
                    {
                        "type": "persistent_index",
                        "collection": coll,
                        "fields": [pname],
                        "reason": (
                            f"Property '{pname}' on '{ename}' is commonly used in lookups and WHERE clauses."
                        ),
                        "priority": "medium",
                    }
                )

    return suggestions


# ---------------------------------------------------------------------------
# Tool: explain_mapping
# ---------------------------------------------------------------------------

EXPLAIN_MAPPING_TOOL_SCHEMA = {
    "name": "explain_mapping",
    "description": (
        "Explain how a Cypher label or relationship type maps to ArangoDB collections and traversals."
    ),
    "parameters": {
        "type": "object",
        "required": ["mapping", "name"],
        "properties": {
            "mapping": {"type": "object", "description": "Schema mapping."},
            "name": {"type": "string", "description": "Label or relationship type to explain."},
        },
    },
}


def explain_mapping_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Explain how a label/type maps to ArangoDB physical storage."""
    mapping_dict = request.get("mapping")
    name = request.get("name", "")
    if not mapping_dict or not name:
        return {"error": "mapping and name are required", "code": "INVALID_ARGUMENT"}

    try:
        bundle = _dict_to_bundle(mapping_dict)
        resolver = MappingResolver(bundle)

        try:
            entity = resolver.resolve_entity(name)
            if entity:
                return {
                    "kind": "entity",
                    "name": name,
                    "physical": entity,
                    "explanation": (
                        f"Cypher label :{name} maps to ArangoDB collection "
                        f"'{entity.get('collectionName', '?')}' using "
                        f"{entity.get('style', 'COLLECTION')} style."
                    ),
                }
        except CoreError:
            pass

        try:
            rel = resolver.resolve_relationship(name)
            if rel:
                return {
                    "kind": "relationship",
                    "name": name,
                    "physical": rel,
                    "explanation": (
                        f"Cypher relationship type :{name} maps to edge collection "
                        f"'{rel.get('edgeCollectionName', '?')}' using "
                        f"{rel.get('style', 'DEDICATED_COLLECTION')} style."
                    ),
                }
        except CoreError:
            pass

        return {"error": f"'{name}' not found in mapping", "code": "NOT_FOUND"}
    except Exception as e:
        return {"error": str(e), "code": "INTERNAL_ERROR"}


# ---------------------------------------------------------------------------
# Tool: cypher_profile
# ---------------------------------------------------------------------------

CYPHER_PROFILE_TOOL_SCHEMA = {
    "name": "cypher_profile",
    "description": "Get the supported Cypher subset profile (what constructs are supported).",
    "parameters": {"type": "object", "properties": {}},
}


def cypher_profile_tool(_request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the Cypher profile manifest."""
    return get_cypher_profile()


# ---------------------------------------------------------------------------
# Tool: propose_mapping_overrides
# ---------------------------------------------------------------------------

PROPOSE_MAPPING_OVERRIDES_TOOL_SCHEMA = {
    "name": "propose_mapping_overrides",
    "description": (
        "Analyze a schema mapping and propose overrides where the "
        "auto-detected mapping may be suboptimal. Returns suggestions "
        "for style changes, missing relationships, or incorrect domain/range."
    ),
    "parameters": {
        "type": "object",
        "required": ["mapping"],
        "properties": {
            "mapping": {"type": "object", "description": "Schema mapping."},
            "context": {"type": "string", "description": "Optional context about the data model."},
        },
    },
}


def propose_mapping_overrides_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Analyze a mapping and propose overrides for suboptimal auto-detection."""
    mapping_dict = request.get("mapping")
    if not mapping_dict:
        return {"error": "mapping is required", "code": "INVALID_ARGUMENT"}

    try:
        bundle = _dict_to_bundle(mapping_dict)
        overrides = _analyze_mapping_overrides(bundle, request.get("context", ""))
        return {"overrides": overrides}
    except Exception as e:
        return {"error": str(e), "code": "INTERNAL_ERROR"}


def _analyze_mapping_overrides(
    bundle: MappingBundle,
    context: str,
) -> list[dict[str, Any]]:
    """Produce mapping override suggestions."""
    overrides: list[dict[str, Any]] = []
    pm = bundle.physical_mapping
    cs = bundle.conceptual_schema

    rels = pm.get("relationships", {})
    if isinstance(rels, dict):
        edge_coll_types: dict[str, list[str]] = {}
        for rtype, rmap in rels.items():
            if not isinstance(rmap, dict):
                continue
            ec = rmap.get("edgeCollectionName", "")
            if ec:
                edge_coll_types.setdefault(ec, []).append(rtype)

        for rtype, rmap in rels.items():
            if not isinstance(rmap, dict):
                continue
            style = rmap.get("style", "")
            ec = rmap.get("edgeCollectionName", "")

            if style == "GENERIC_WITH_TYPE" and ec:
                types_in_coll = edge_coll_types.get(ec, [])
                if len(types_in_coll) == 1:
                    overrides.append(
                        {
                            "target": rtype,
                            "kind": "relationship",
                            "field": "style",
                            "current": "GENERIC_WITH_TYPE",
                            "suggested": "DEDICATED_COLLECTION",
                            "rationale": (
                                f"Edge collection '{ec}' contains only one relationship "
                                f"type '{rtype}'. DEDICATED_COLLECTION avoids the "
                                f"type-discriminator filter overhead."
                            ),
                        }
                    )

            domain = rmap.get("domain")
            range_ = rmap.get("range")
            if not domain or not range_:
                overrides.append(
                    {
                        "target": rtype,
                        "kind": "relationship",
                        "field": "domain/range",
                        "current": f"domain={domain}, range={range_}",
                        "suggested": "Specify explicit domain and range entities",
                        "rationale": (
                            f"Relationship '{rtype}' has missing domain or range. "
                            f"Without these, IS_SAME_COLLECTION optimizations cannot "
                            f"be applied."
                        ),
                    }
                )

    entities = pm.get("entities", {})
    if isinstance(entities, dict):
        cs_entities = {e.get("name", ""): e for e in cs.get("entities", []) if isinstance(e, dict)}
        for ename, emap in entities.items():
            if not isinstance(emap, dict):
                continue
            cs_ent = cs_entities.get(ename, {})
            cs_props = cs_ent.get("properties", [])
            pm_props = emap.get("properties", {})
            if not cs_props and not pm_props:
                overrides.append(
                    {
                        "target": ename,
                        "kind": "entity",
                        "field": "properties",
                        "current": "none",
                        "suggested": "Add property definitions",
                        "rationale": (
                            f"Entity '{ename}' has no properties defined in either "
                            f"the conceptual schema or physical mapping. This may "
                            f"indicate incomplete schema introspection."
                        ),
                    }
                )

        coll_labels: dict[str, list[str]] = {}
        for ename, emap in entities.items():
            if not isinstance(emap, dict):
                continue
            style = emap.get("style", "")
            if style == "LABEL":
                coll = emap.get("collectionName", "")
                tf = emap.get("typeField", "type")
                key = f"{coll}:{tf}"
                coll_labels.setdefault(key, []).append(ename)
        for key, labels in coll_labels.items():
            if len(labels) <= 2:
                coll_name = key.split(":")[0]
                overrides.append(
                    {
                        "target": ", ".join(labels),
                        "kind": "entity",
                        "field": "style",
                        "current": "LABEL (few types in shared collection)",
                        "suggested": "Consider COLLECTION style with dedicated collections",
                        "rationale": (
                            f"Collection '{coll_name}' has only {len(labels)} "
                            f"label(s) ({', '.join(labels)}). A dedicated collection "
                            f"per entity may be simpler and faster."
                        ),
                    }
                )

    return overrides


# ---------------------------------------------------------------------------
# Tool: explain_translation
# ---------------------------------------------------------------------------

EXPLAIN_TRANSLATION_TOOL_SCHEMA = {
    "name": "explain_translation",
    "description": (
        "Translate Cypher to AQL and explain the translation decisions. "
        "Shows which mapping entries were used, what optimizations were applied, "
        "and why certain AQL patterns were chosen."
    ),
    "parameters": {
        "type": "object",
        "required": ["cypher", "mapping"],
        "properties": {
            "cypher": {"type": "string", "description": "The Cypher query."},
            "mapping": {"type": "object", "description": "Schema mapping."},
        },
    },
}


def explain_translation_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Translate Cypher to AQL and explain the translation decisions."""
    cypher = request.get("cypher", "")
    mapping_dict = request.get("mapping")

    if not cypher:
        return {"error": "cypher is required", "code": "INVALID_ARGUMENT"}
    if not mapping_dict:
        return {"error": "mapping is required", "code": "INVALID_ARGUMENT"}

    try:
        bundle = _dict_to_bundle(mapping_dict)
        result = translate(cypher, mapping=bundle)

        resolver = MappingResolver(bundle)
        explanation = _build_explanation(cypher, bundle, resolver, result)
        return explanation
    except CoreError as e:
        return {"error": str(e), "code": e.code}
    except Exception as e:
        return {"error": str(e), "code": "INTERNAL_ERROR"}


def _build_explanation(
    cypher: str,
    bundle: MappingBundle,
    resolver: MappingResolver,
    result: Any,
) -> dict[str, Any]:
    """Build a structured explanation of the translation."""
    import re

    mappings_used: list[dict[str, Any]] = []
    optimizations: list[str] = []

    label_pattern = re.compile(r":(\w+)")
    labels_found = label_pattern.findall(cypher)

    entities = bundle.physical_mapping.get("entities", {})
    rels = bundle.physical_mapping.get("relationships", {})

    for label in labels_found:
        if isinstance(entities, dict) and label in entities:
            emap = entities[label]
            mappings_used.append(
                {
                    "name": label,
                    "kind": "entity",
                    "collection": emap.get("collectionName", ""),
                    "style": emap.get("style", ""),
                }
            )
        elif isinstance(rels, dict) and label in rels:
            rmap = rels[label]
            mappings_used.append(
                {
                    "name": label,
                    "kind": "relationship",
                    "edgeCollection": rmap.get("edgeCollectionName", ""),
                    "style": rmap.get("style", ""),
                }
            )

    aql = result.aql
    if "IS_SAME_COLLECTION" not in aql:
        for label in labels_found:
            if isinstance(rels, dict) and label in rels:
                rmap = rels[label]
                if rmap.get("domain") and rmap.get("range"):
                    optimizations.append(
                        f"IS_SAME_COLLECTION eliminated for '{label}' "
                        f"(domain/range constraints guarantee target type)"
                    )

    if "OPTIONS" in aql and "indexHint" in aql:
        optimizations.append("VCI index hint applied for edge traversal filtering")

    if isinstance(rels, dict):
        for label in labels_found:
            if label in rels:
                rmap = rels[label]
                if rmap.get("style") == "GENERIC_WITH_TYPE":
                    optimizations.append(
                        f"Type discriminator filter added for GENERIC_WITH_TYPE relationship '{label}'"
                    )

    return {
        "aql": aql,
        "bind_vars": result.bind_vars,
        "warnings": result.warnings,
        "mappings_used": mappings_used,
        "optimizations": optimizations,
    }


# ---------------------------------------------------------------------------
# Tool: validate_cypher
# ---------------------------------------------------------------------------

VALIDATE_CYPHER_TOOL_SCHEMA = {
    "name": "validate_cypher",
    "description": "Validate Cypher syntax and return parse errors if any.",
    "parameters": {
        "type": "object",
        "required": ["cypher"],
        "properties": {
            "cypher": {"type": "string", "description": "The Cypher query to validate."},
        },
    },
}


def validate_cypher_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Validate Cypher syntax without translating."""
    cypher = request.get("cypher", "")
    if not cypher:
        return {"valid": False, "errors": [{"message": "cypher is required"}]}

    try:
        parse_cypher(cypher)
        return {"valid": True}
    except CoreError as e:
        return {"valid": False, "errors": [{"message": str(e), "code": e.code}]}
    except Exception as e:
        return {"valid": False, "errors": [{"message": str(e)}]}


# ---------------------------------------------------------------------------
# Tool: schema_summary
# ---------------------------------------------------------------------------

SCHEMA_SUMMARY_TOOL_SCHEMA = {
    "name": "schema_summary",
    "description": "Return a human-readable summary of the schema mapping.",
    "parameters": {
        "type": "object",
        "required": ["mapping"],
        "properties": {
            "mapping": {"type": "object", "description": "Schema mapping."},
        },
    },
}


def schema_summary_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Return a structured, human-readable schema summary."""
    mapping_dict = request.get("mapping")
    if not mapping_dict:
        return {"error": "mapping is required", "code": "INVALID_ARGUMENT"}

    try:
        bundle = _dict_to_bundle(mapping_dict)
        resolver = MappingResolver(bundle)
        summary = resolver.schema_summary()

        entities = summary.get("entities", [])
        relationships = summary.get("relationships", [])
        styles = set()
        for e in entities:
            styles.add(e.get("style", ""))
        for r in relationships:
            styles.add(r.get("style", ""))

        return {
            "entity_count": len(entities),
            "relationship_count": len(relationships),
            "entity_labels": [e.get("label", "") for e in entities],
            "relationship_types": [r.get("type", "") for r in relationships],
            "styles_used": sorted(s for s in styles if s),
            "details": summary,
        }
    except Exception as e:
        return {"error": str(e), "code": "INTERNAL_ERROR"}


# ---------------------------------------------------------------------------
# All tools registry
# ---------------------------------------------------------------------------

ALL_TOOLS = [
    {"schema": TRANSLATE_TOOL_SCHEMA, "function": translate_tool},
    {"schema": SUGGEST_INDEXES_TOOL_SCHEMA, "function": suggest_indexes_tool},
    {"schema": EXPLAIN_MAPPING_TOOL_SCHEMA, "function": explain_mapping_tool},
    {"schema": CYPHER_PROFILE_TOOL_SCHEMA, "function": cypher_profile_tool},
    {"schema": PROPOSE_MAPPING_OVERRIDES_TOOL_SCHEMA, "function": propose_mapping_overrides_tool},
    {"schema": EXPLAIN_TRANSLATION_TOOL_SCHEMA, "function": explain_translation_tool},
    {"schema": VALIDATE_CYPHER_TOOL_SCHEMA, "function": validate_cypher_tool},
    {"schema": SCHEMA_SUMMARY_TOOL_SCHEMA, "function": schema_summary_tool},
]


def get_tool_schemas() -> list[dict[str, Any]]:
    """Return OpenAI-compatible function schemas for all tools."""
    return [t["schema"] for t in ALL_TOOLS]


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool call by name."""
    for tool in ALL_TOOLS:
        if tool["schema"]["name"] == name:
            return tool["function"](arguments)
    return {"error": f"Unknown tool: {name}", "code": "NOT_FOUND"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dict_to_bundle(d: dict[str, Any]) -> MappingBundle:
    """Tool-calling harness alias for :func:`mapping_from_wire_dict`.

    No ``MappingSource`` attached — tool-calling payloads come from an
    LLM and do not carry an audit-worthy provenance (unlike the HTTP
    endpoint path in ``service.py``).
    """
    return mapping_from_wire_dict(d)
