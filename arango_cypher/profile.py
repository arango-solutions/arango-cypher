"""
Arango Cypher profile manifest for NL/agent integration (see docs/python_prd.md §2A).

Single source of truth for *declared* translator capabilities. Keep in sync with README
and fail-fast behavior in translate_v0; tests should assert critical keys exist.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

PROFILE_SCHEMA_VERSION = "1"
"""Bump when the manifest shape changes incompatibly."""

DEFAULT_COMPLETENESS_BAR = "neo4j_flavored_read_plus_extensions"
TRANSLATOR_ID = "arango_cypher.translate_v0"


def distribution_version() -> str:
    try:
        return importlib.metadata.version("arango-cypher-py")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def build_cypher_profile() -> dict[str, Any]:
    """
    JSON-serializable profile: supported surface, limitations, extension policy defaults.

    ``registered_arango_functions`` / ``registered_arango_procedures`` list what the
    registry can expose once implemented; the v0 translator still rejects unknown
    ``arango.*`` calls unless registered.
    """
    return {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "transpiler_version": distribution_version(),
        "translator_id": TRANSLATOR_ID,
        "completeness_bar": DEFAULT_COMPLETENESS_BAR,
        "documentation": {
            "readme_subset_anchor": "supported-cypher-subset",
            "prd_sections": ["§2A", "§7A", "§10A"],
        },
        "extensions": {
            "portable_mode_default": True,
            "namespaces": ["arango"],
            "registered_arango_functions": [],
            "registered_arango_procedures": [],
            "note": (
                "Enable ExtensionPolicy(enabled=True) for future arango.* compilers; "
                "v0 rejects unregistered extensions."
            ),
        },
        "supported": {
            "reading_clauses": ["MATCH", "OPTIONAL_MATCH", "MULTIPLE_MATCH", "UNWIND", "CALL_YIELD"],
            "updating_clauses": [],
            "pipeline_clauses": ["WITH", "RETURN"],
            "set_operations": ["UNION", "UNION_ALL"],
            "match_features": [
                "multiple_pattern_parts",
                "multi_hop_relationships",
                "directed_relationships",
                "bounded_variable_length_relationships",
                "inline_node_properties",
                "inline_relationship_properties",
                "unlabeled_node_single_collection_inference",
            ],
            "where_features": [
                "and_or_not_xor",
                "comparisons",
                "in",
                "is_null",
                "is_not_null",
                "arithmetic_binary",
                "unary_plus_minus",
                "case_expression",
                "starts_with",
                "ends_with",
                "contains",
            ],
            "return_features": [
                "projections",
                "aliases",
                "distinct",
                "order_by",
                "skip",
                "limit",
            ],
            "with_features": [
                "aggregation_count_avg_sum_min_max_collect",
                "pipeline_stages",
                "with_then_match",
            ],
            "parameters": ["named_dollar_param"],
            "property_access": ["dot_paths_nested_documents"],
            "functions": [
                "size",
                "toLower",
                "toUpper",
                "coalesce",
                "type",
            ],
            "extension_functions": [
                "arango.bm25",
                "arango.tfidf",
                "arango.analyzer",
                "arango.cosine_similarity",
                "arango.l2_distance",
                "arango.approx_near_cosine",
                "arango.approx_near_l2",
                "arango.distance",
                "arango.geo_distance",
                "arango.geo_contains",
                "arango.geo_intersects",
                "arango.geo_in_range",
                "arango.geo_point",
            ],
            "extension_functions_document": [
                "arango.attributes",
                "arango.has",
                "arango.merge",
                "arango.unset",
                "arango.keep",
                "arango.zip",
                "arango.value",
                "arango.values",
                "arango.flatten",
                "arango.parse_identifier",
                "arango.document",
            ],
            "extension_procedures": [
                "arango.fulltext",
                "arango.near",
                "arango.within",
                "arango.shortest_path",
                "arango.k_shortest_paths",
            ],
            "embedded_relationship_styles": ["EMBEDDED"],
        },
        "not_yet_supported": [
            "multiple_relationship_types_per_hop",
            "write_clauses_create_merge_set_delete_remove",
            "positional_parameters",
            "multi_label_nodes_without_label_mapping",
        ],
        "validation": {
            "validate_cypher_profile_behavior": (
                "Without mapping: syntax-only (ANTLR parse). "
                "With mapping: parse plus translate (same as translate())."
            ),
        },
    }
