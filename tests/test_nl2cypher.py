"""Tests for the NL-to-Cypher rule-based pipeline."""

from __future__ import annotations

import pytest

from arango_cypher.nl2cypher import NL2CypherResult, nl_to_cypher
from arango_cypher.nl2cypher._core import _detect_literal_return
from tests.helpers.mapping_fixtures import mapping_bundle_for


class TestLiteralReturnGuardrail:
    """NL-side guardrail: constant-only questions must not become DB queries."""

    @pytest.mark.parametrize(
        "question,expected",
        [
            ('return "hello"', 'RETURN "hello"'),
            ("return 'hello'", "RETURN 'hello'"),
            ("Return 42", "RETURN 42"),
            ("return -3.14", "RETURN -3.14"),
            ("RETURN true", "RETURN true"),
            ("return False", "RETURN false"),
            ("return null", "RETURN null"),
            ('please return "hi"', 'RETURN "hi"'),
            ('just return "hi"', 'RETURN "hi"'),
            ('return "hello" as greeting', 'RETURN "hello" AS greeting'),
            ('Return "done".', 'RETURN "done"'),
        ],
    )
    def test_detects_literal_returns(self, question, expected) -> None:
        assert _detect_literal_return(question) == expected

    @pytest.mark.parametrize(
        "question",
        [
            "",
            "   ",
            "find all people",
            "return the companies that CINF has a stake in",
            "return p.name",
            "return all movies",
            "show me the count of persons",
            "return company names",
        ],
    )
    def test_ignores_non_literal_questions(self, question) -> None:
        assert _detect_literal_return(question) is None

    def test_short_circuits_without_mapping(self) -> None:
        # A literal question needs no schema and no LLM.
        result = nl_to_cypher('return "hello"', mapping=None, use_llm=True)
        assert result.cypher == 'RETURN "hello"'
        assert result.method == "literal_return"
        assert result.confidence == 1.0

    def test_short_circuits_with_mapping_and_no_llm(self, movies_mapping) -> None:
        result = nl_to_cypher("return 1", mapping=movies_mapping, use_llm=False)
        assert result.cypher == "RETURN 1"
        assert result.method == "literal_return"


@pytest.fixture
def movies_mapping():
    return mapping_bundle_for("movies_pg")


@pytest.fixture
def northwind_mapping():
    return mapping_bundle_for("northwind_pg")


class TestRuleBased:
    def test_find_all_entities(self, movies_mapping) -> None:
        result = nl_to_cypher("Find all persons", mapping=movies_mapping, use_llm=False)
        assert "MATCH (n:Person)" in result.cypher
        assert "RETURN" in result.cypher
        assert result.method == "rule_based"
        assert result.confidence > 0

    def test_list_all_entities(self, movies_mapping) -> None:
        result = nl_to_cypher("List all movies", mapping=movies_mapping, use_llm=False)
        assert "MATCH (n:Movie)" in result.cypher
        assert "RETURN" in result.cypher

    def test_count_entities(self, movies_mapping) -> None:
        result = nl_to_cypher("How many persons are there?", mapping=movies_mapping, use_llm=False)
        assert "MATCH (n:Person)" in result.cypher
        assert "count(n)" in result.cypher

    def test_count_variant(self, northwind_mapping) -> None:
        result = nl_to_cypher("Count customers", mapping=northwind_mapping, use_llm=False)
        assert "MATCH (n:Customer)" in result.cypher
        assert "count(n)" in result.cypher

    def test_find_with_filter(self, northwind_mapping) -> None:
        result = nl_to_cypher("Find customers in Germany", mapping=northwind_mapping, use_llm=False)
        assert "MATCH (n:Customer)" in result.cypher
        assert "WHERE" in result.cypher or "country" in result.cypher.lower()

    def test_relationship_in_question(self, movies_mapping) -> None:
        result = nl_to_cypher("Who acted_in movies?", mapping=movies_mapping, use_llm=False)
        assert "ACTED_IN" in result.cypher
        assert "MATCH" in result.cypher

    def test_no_mapping_returns_empty(self) -> None:
        result = nl_to_cypher("Find all people", mapping=None, use_llm=False)
        assert result.cypher == ""
        assert result.confidence == 0.0

    def test_unrecognized_query(self, movies_mapping) -> None:
        result = nl_to_cypher("xyzzy foobar baz", mapping=movies_mapping, use_llm=False)
        assert result.confidence == 0.0

    def test_show_all_variant(self, northwind_mapping) -> None:
        result = nl_to_cypher("Show all products", mapping=northwind_mapping, use_llm=False)
        assert "MATCH (n:Product)" in result.cypher
        assert "RETURN" in result.cypher

    def test_get_all_variant(self, northwind_mapping) -> None:
        result = nl_to_cypher("Get all categories", mapping=northwind_mapping, use_llm=False)
        assert "MATCH (n:Category)" in result.cypher


class TestExtractCypher:
    def test_code_block_extraction(self) -> None:
        from arango_cypher.nl2cypher import _extract_cypher_from_response

        text = "Here's the query:\n```cypher\nMATCH (n:Person) RETURN n\n```\nThat should work."
        assert _extract_cypher_from_response(text) == "MATCH (n:Person) RETURN n"

    def test_plain_text_extraction(self) -> None:
        from arango_cypher.nl2cypher import _extract_cypher_from_response

        text = "MATCH (n:Person) RETURN n"
        assert _extract_cypher_from_response(text) == "MATCH (n:Person) RETURN n"

    def test_code_block_no_language(self) -> None:
        from arango_cypher.nl2cypher import _extract_cypher_from_response

        text = "```\nMATCH (n) RETURN n\n```"
        assert _extract_cypher_from_response(text) == "MATCH (n) RETURN n"


class TestSchemaContext:
    def test_schema_summary_contains_entities(self, movies_mapping) -> None:
        from arango_cypher.nl2cypher import _build_schema_summary

        summary = _build_schema_summary(movies_mapping)
        assert "Person" in summary
        assert "Movie" in summary

    def test_schema_summary_contains_relationships(self, movies_mapping) -> None:
        from arango_cypher.nl2cypher import _build_schema_summary

        summary = _build_schema_summary(movies_mapping)
        assert "ACTED_IN" in summary
