"""Tests for agentic tool wrappers (full suite)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arango_cypher.tools import (
    call_tool,
    cypher_profile_tool,
    explain_mapping_tool,
    explain_translation_tool,
    get_tool_schemas,
    propose_mapping_overrides_tool,
    schema_summary_tool,
    suggest_indexes_tool,
    translate_tool,
    validate_cypher_tool,
)


def _load_mapping(name: str) -> dict:
    root = Path(__file__).resolve().parent / "fixtures" / "mappings"
    p = root / f"{name}.export.json"
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# translate_tool
# ---------------------------------------------------------------------------

class TestTranslateTool:
    def test_basic_translation(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = translate_tool({
            "cypher": "MATCH (p:Person) RETURN p.name",
            "mapping": mapping,
        })
        assert "error" not in result
        assert "FOR" in result["aql"]
        assert "RETURN" in result["aql"]

    def test_missing_cypher(self) -> None:
        result = translate_tool({"mapping": {}})
        assert result["code"] == "INVALID_ARGUMENT"

    def test_missing_mapping(self) -> None:
        result = translate_tool({"cypher": "MATCH (n) RETURN n"})
        assert result["code"] == "INVALID_ARGUMENT"

    def test_invalid_cypher(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = translate_tool({"cypher": "NOT VALID CYPHER AT ALL !!!", "mapping": mapping})
        assert "error" in result

    def test_with_params(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = translate_tool({
            "cypher": "MATCH (p:Person) WHERE p.name = $name RETURN p",
            "mapping": mapping,
            "params": {"name": "Alice"},
        })
        assert "error" not in result
        assert "name" in result["bind_vars"]


# ---------------------------------------------------------------------------
# explain_mapping_tool
# ---------------------------------------------------------------------------

class TestExplainMapping:
    def test_explain_entity(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = explain_mapping_tool({"mapping": mapping, "name": "Person"})
        assert result["kind"] == "entity"
        assert "persons" in result["explanation"]

    def test_explain_relationship(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = explain_mapping_tool({"mapping": mapping, "name": "ACTED_IN"})
        assert result["kind"] == "relationship"
        assert "acted_in" in result["explanation"]

    def test_explain_not_found(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = explain_mapping_tool({"mapping": mapping, "name": "NONEXISTENT"})
        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_missing_args(self) -> None:
        result = explain_mapping_tool({"mapping": {}})
        assert result["code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# propose_mapping_overrides
# ---------------------------------------------------------------------------

class TestProposeMappingOverrides:
    def test_detects_single_type_generic(self) -> None:
        """A GENERIC_WITH_TYPE edge collection with only one rel type should
        suggest switching to DEDICATED_COLLECTION."""
        mapping = {
            "physicalMapping": {
                "entities": {
                    "Person": {"collectionName": "persons", "style": "COLLECTION"},
                },
                "relationships": {
                    "KNOWS": {
                        "edgeCollectionName": "knows_edges",
                        "style": "GENERIC_WITH_TYPE",
                        "typeField": "type",
                        "typeValue": "KNOWS",
                    },
                },
            },
            "conceptualSchema": {"entities": [], "relationships": []},
        }
        result = propose_mapping_overrides_tool({"mapping": mapping})
        assert "error" not in result
        overrides = result["overrides"]
        style_overrides = [o for o in overrides if o["field"] == "style" and o["target"] == "KNOWS"]
        assert len(style_overrides) == 1
        assert style_overrides[0]["suggested"] == "DEDICATED_COLLECTION"

    def test_detects_missing_domain_range(self) -> None:
        mapping = {
            "physicalMapping": {
                "entities": {},
                "relationships": {
                    "ACTED_IN": {
                        "edgeCollectionName": "acted_in",
                        "style": "DEDICATED_COLLECTION",
                    },
                },
            },
            "conceptualSchema": {"entities": [], "relationships": []},
        }
        result = propose_mapping_overrides_tool({"mapping": mapping})
        overrides = result["overrides"]
        dr = [o for o in overrides if o["field"] == "domain/range"]
        assert len(dr) == 1
        assert "ACTED_IN" in dr[0]["target"]

    def test_detects_no_properties(self) -> None:
        mapping = {
            "physicalMapping": {
                "entities": {
                    "Foo": {"collectionName": "foos", "style": "COLLECTION"},
                },
                "relationships": {},
            },
            "conceptualSchema": {"entities": [{"name": "Foo"}], "relationships": []},
        }
        result = propose_mapping_overrides_tool({"mapping": mapping})
        overrides = result["overrides"]
        prop_overrides = [o for o in overrides if o["field"] == "properties"]
        assert len(prop_overrides) == 1

    def test_detects_few_labels_in_shared_collection(self) -> None:
        mapping = {
            "physicalMapping": {
                "entities": {
                    "Admin": {
                        "collectionName": "users",
                        "style": "LABEL",
                        "typeField": "role",
                        "typeValue": "Admin",
                    },
                },
                "relationships": {},
            },
            "conceptualSchema": {"entities": [], "relationships": []},
        }
        result = propose_mapping_overrides_tool({"mapping": mapping})
        overrides = result["overrides"]
        label_overrides = [o for o in overrides if o["field"] == "style" and o["kind"] == "entity"]
        assert len(label_overrides) >= 1

    def test_missing_mapping(self) -> None:
        result = propose_mapping_overrides_tool({})
        assert result["code"] == "INVALID_ARGUMENT"

    def test_with_real_lpg_mapping(self) -> None:
        mapping = _load_mapping("movies_lpg_naked")
        result = propose_mapping_overrides_tool({"mapping": mapping})
        assert "error" not in result
        assert isinstance(result["overrides"], list)


# ---------------------------------------------------------------------------
# explain_translation
# ---------------------------------------------------------------------------

class TestExplainTranslation:
    def test_basic_explanation(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = explain_translation_tool({
            "cypher": "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name, m.title",
            "mapping": mapping,
        })
        assert "error" not in result
        assert "aql" in result
        assert "bind_vars" in result
        assert "mappings_used" in result
        assert "optimizations" in result
        assert "warnings" in result

    def test_mappings_identified(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = explain_translation_tool({
            "cypher": "MATCH (p:Person) RETURN p.name",
            "mapping": mapping,
        })
        assert "error" not in result
        names = {m["name"] for m in result["mappings_used"]}
        assert "Person" in names

    def test_missing_cypher(self) -> None:
        result = explain_translation_tool({"mapping": {}})
        assert result["code"] == "INVALID_ARGUMENT"

    def test_missing_mapping(self) -> None:
        result = explain_translation_tool({"cypher": "MATCH (n) RETURN n"})
        assert result["code"] == "INVALID_ARGUMENT"

    def test_invalid_cypher(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = explain_translation_tool({
            "cypher": "THIS IS NOT CYPHER!!!",
            "mapping": mapping,
        })
        assert "error" in result

    def test_lpg_generic_type_optimization(self) -> None:
        mapping = _load_mapping("movies_lpg")
        result = explain_translation_tool({
            "cypher": "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name",
            "mapping": mapping,
        })
        assert "error" not in result
        has_type_discriminator = any(
            "GENERIC_WITH_TYPE" in o for o in result["optimizations"]
        )
        assert has_type_discriminator


# ---------------------------------------------------------------------------
# validate_cypher
# ---------------------------------------------------------------------------

class TestValidateCypher:
    def test_valid_cypher(self) -> None:
        result = validate_cypher_tool({"cypher": "MATCH (n) RETURN n"})
        assert result["valid"] is True
        assert "errors" not in result

    def test_valid_complex_cypher(self) -> None:
        result = validate_cypher_tool({
            "cypher": "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) WHERE p.name = 'Tom' RETURN m.title"
        })
        assert result["valid"] is True

    def test_invalid_cypher(self) -> None:
        result = validate_cypher_tool({"cypher": "NOT VALID CYPHER"})
        assert result["valid"] is False
        assert len(result["errors"]) > 0

    def test_empty_cypher(self) -> None:
        result = validate_cypher_tool({"cypher": ""})
        assert result["valid"] is False

    def test_missing_cypher(self) -> None:
        result = validate_cypher_tool({})
        assert result["valid"] is False


# ---------------------------------------------------------------------------
# schema_summary
# ---------------------------------------------------------------------------

class TestSchemaSummary:
    def test_pg_summary(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = schema_summary_tool({"mapping": mapping})
        assert "error" not in result
        assert result["entity_count"] == 2
        assert result["relationship_count"] == 6
        assert "Person" in result["entity_labels"]
        assert "Movie" in result["entity_labels"]
        assert "ACTED_IN" in result["relationship_types"]
        assert isinstance(result["styles_used"], list)
        assert "details" in result

    def test_lpg_summary(self) -> None:
        mapping = _load_mapping("movies_lpg")
        result = schema_summary_tool({"mapping": mapping})
        assert "error" not in result
        assert result["entity_count"] == 4
        assert "LABEL" in result["styles_used"]
        assert "GENERIC_WITH_TYPE" in result["styles_used"]

    def test_missing_mapping(self) -> None:
        result = schema_summary_tool({})
        assert result["code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

class TestToolRegistry:
    ALL_TOOL_NAMES = {
        "cypher_translate",
        "suggest_indexes",
        "explain_mapping",
        "cypher_profile",
        "propose_mapping_overrides",
        "explain_translation",
        "validate_cypher",
        "schema_summary",
    }

    def test_get_schemas_returns_all(self) -> None:
        schemas = get_tool_schemas()
        names = {s["name"] for s in schemas}
        assert names == self.ALL_TOOL_NAMES

    def test_call_tool_dispatch_profile(self) -> None:
        result = call_tool("cypher_profile", {})
        assert isinstance(result, dict)

    def test_call_tool_dispatch_validate(self) -> None:
        result = call_tool("validate_cypher", {"cypher": "MATCH (n) RETURN n"})
        assert result["valid"] is True

    def test_call_tool_dispatch_schema_summary(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = call_tool("schema_summary", {"mapping": mapping})
        assert result["entity_count"] == 2

    def test_call_tool_dispatch_overrides(self) -> None:
        mapping = _load_mapping("movies_lpg_naked")
        result = call_tool("propose_mapping_overrides", {"mapping": mapping})
        assert "overrides" in result

    def test_call_tool_dispatch_explain_translation(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = call_tool("explain_translation", {
            "cypher": "MATCH (p:Person) RETURN p.name",
            "mapping": mapping,
        })
        assert "aql" in result

    def test_call_unknown_tool(self) -> None:
        result = call_tool("nonexistent_tool", {})
        assert result["code"] == "NOT_FOUND"
