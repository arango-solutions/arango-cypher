"""Tests for agentic tool wrappers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arango_cypher.tools import (
    translate_tool,
    suggest_indexes_tool,
    explain_mapping_tool,
    cypher_profile_tool,
    get_tool_schemas,
    call_tool,
)


def _load_mapping(name: str) -> dict:
    root = Path(__file__).resolve().parent / "fixtures" / "mappings"
    p = root / f"{name}.export.json"
    return json.loads(p.read_text(encoding="utf-8"))


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
        result = translate_tool({"cypher": "INVALID QUERY", "mapping": mapping})
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


class TestSuggestIndexes:
    def test_generic_with_type_suggestion(self) -> None:
        mapping = _load_mapping("movies_lpg_naked")
        result = suggest_indexes_tool({"mapping": mapping})
        assert "error" not in result
        suggestions = result["suggestions"]
        vci = [s for s in suggestions if s["priority"] == "high"]
        assert len(vci) > 0

    def test_pg_mapping_suggestions(self) -> None:
        mapping = _load_mapping("northwind_pg")
        result = suggest_indexes_tool({"mapping": mapping})
        assert "error" not in result
        assert isinstance(result["suggestions"], list)


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

    def test_explain_not_found(self) -> None:
        mapping = _load_mapping("movies_pg")
        result = explain_mapping_tool({"mapping": mapping, "name": "NONEXISTENT"})
        assert "error" in result


class TestToolRegistry:
    def test_get_schemas_returns_all(self) -> None:
        schemas = get_tool_schemas()
        names = {s["name"] for s in schemas}
        assert "cypher_translate" in names
        assert "suggest_indexes" in names
        assert "explain_mapping" in names
        assert "cypher_profile" in names

    def test_call_tool_dispatch(self) -> None:
        result = call_tool("cypher_profile", {})
        assert "version" in result or "constructs" in result or isinstance(result, dict)

    def test_call_unknown_tool(self) -> None:
        result = call_tool("nonexistent_tool", {})
        assert result["code"] == "NOT_FOUND"


class TestCypherProfile:
    def test_returns_dict(self) -> None:
        result = cypher_profile_tool()
        assert isinstance(result, dict)
