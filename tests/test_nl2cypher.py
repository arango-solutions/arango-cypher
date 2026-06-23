"""Tests for the NL-to-Cypher rule-based pipeline."""

from __future__ import annotations

import pytest

from arango_cypher.nl2cypher import NL2CypherResult, PromptBuilder, nl_to_cypher
from arango_cypher.nl2cypher._core import _detect_graph_intent, _detect_literal_return
from tests.helpers.mapping_fixtures import mapping_bundle_for


class TestGraphIntentDetection:
    """WP-S1: explicit graph/visualization intent steers toward path returns."""

    @pytest.mark.parametrize(
        "question",
        [
            "Show me, as a graph, the companies CINF has a stake in",
            "show the companies as a graph",
            "Visualize the supply chain of Apple",
            "visualise connections between people",
            "Draw the network of dependencies",
            "graph of stakeholders for MSFT",
            "render the relationships around this org",
            "display the network around node 5",
            "show me the subgraph for these risks",
        ],
    )
    def test_detects_graph_intent(self, question) -> None:
        assert _detect_graph_intent(question) is True

    @pytest.mark.parametrize(
        "question",
        [
            "",
            "   ",
            "How many graphs are there?",
            "list the companies CINF has a stake in",
            "count the people who acted in a movie",
            "what is the name of the company",
            "return the stakeholders",
        ],
    )
    def test_ignores_non_graph_intent(self, question) -> None:
        assert _detect_graph_intent(question) is False

    def test_builder_includes_graph_section_when_intent(self) -> None:
        rendered = PromptBuilder(schema_summary="S", graph_intent=True).render_system()
        assert "Output shape: return a graph" in rendered
        assert "RETURN p" in rendered

    def test_builder_omits_graph_section_without_intent(self) -> None:
        rendered = PromptBuilder(schema_summary="S", graph_intent=False).render_system()
        assert "Output shape: return a graph" not in rendered

    def test_graph_intent_reaches_llm_system_prompt(self, movies_mapping) -> None:
        class _RecordingProvider:
            def __init__(self) -> None:
                self.systems: list[str] = []

            def generate(self, system: str, user: str):  # noqa: ARG002
                self.systems.append(system)
                return "```cypher\nMATCH p = (n)-[r]->(m) RETURN p LIMIT 50\n```", {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "cached_tokens": 0,
                }

        provider = _RecordingProvider()
        nl_to_cypher(
            "Show me, as a graph, the movies and their actors",
            mapping=movies_mapping,
            llm_provider=provider,
            use_fewshot=False,
            use_entity_resolution=False,
        )
        assert provider.systems, "provider was not called"
        assert any("Output shape: return a graph" in s for s in provider.systems)

    def test_no_graph_intent_keeps_system_prompt_clean(self, movies_mapping) -> None:
        class _RecordingProvider:
            def __init__(self) -> None:
                self.systems: list[str] = []

            def generate(self, system: str, user: str):  # noqa: ARG002
                self.systems.append(system)
                return "```cypher\nMATCH (n) RETURN n.name\n```", {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "cached_tokens": 0,
                }

        provider = _RecordingProvider()
        nl_to_cypher(
            "list the movie titles",
            mapping=movies_mapping,
            llm_provider=provider,
            use_fewshot=False,
            use_entity_resolution=False,
        )
        assert provider.systems
        assert all("Output shape: return a graph" not in s for s in provider.systems)


class TestIndexAdvisoryThreading:
    """WP-S3c: advisories recorded by the entity resolver must be attached to
    the NL2CypherResult so the service/UI can offer one-click index creation."""

    def _provider(self):
        class _RecordingProvider:
            def generate(self, system: str, user: str):  # noqa: ARG002
                return "```cypher\nMATCH (n) RETURN n\n```", {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "cached_tokens": 0,
                }

        return _RecordingProvider()

    def _resolver_with_advisory(self):
        from arango_cypher.nl2cypher import IndexAdvisory

        class _FakeResolver:
            def __init__(self) -> None:
                self.advisories = [IndexAdvisory(collection="Node", field="name")]

            def resolve(self, question: str):  # noqa: ARG002
                return []

            def format_prompt_section(self, hits):  # noqa: ARG002
                return []

        return _FakeResolver()

    def test_result_carries_resolver_advisories(self, movies_mapping) -> None:
        result = nl_to_cypher(
            "companies cincinnati financial (CINF) has a stake in",
            mapping=movies_mapping,
            llm_provider=self._provider(),
            use_fewshot=False,
            use_entity_resolution=True,
            entity_resolver=self._resolver_with_advisory(),
        )
        assert result.cypher  # LLM path produced cypher
        assert len(result.advisories) == 1
        adv = result.advisories[0]
        assert adv["collection"] == "Node"
        assert adv["field"] == "name"
        assert adv["suggestedIndex"]["type"] == "inverted"

    def test_no_advisories_when_resolver_clean(self, movies_mapping) -> None:
        from arango_cypher.nl2cypher import IndexAdvisory  # noqa: F401

        class _CleanResolver:
            advisories: list = []

            def resolve(self, question: str):  # noqa: ARG002
                return []

            def format_prompt_section(self, hits):  # noqa: ARG002
                return []

        result = nl_to_cypher(
            "find the movies",
            mapping=movies_mapping,
            llm_provider=self._provider(),
            use_fewshot=False,
            use_entity_resolution=True,
            entity_resolver=_CleanResolver(),
        )
        assert result.advisories == []


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


class TestValueShapeHints:
    """WP-S2a: surface value-shape / example signals so the LLM stops
    inventing legal names for token-shaped fields."""

    def test_hint_renders_shape(self) -> None:
        from arango_cypher.nl2cypher._core import _property_quality_hint

        hint = _property_quality_hint({"valueShape": "ticker"})
        assert "shape: ticker" in hint

    def test_hint_renders_examples(self) -> None:
        from arango_cypher.nl2cypher._core import _property_quality_hint

        hint = _property_quality_hint({"exampleValues": ["cinf", "aapl", "msft"]})
        assert 'e.g. "cinf", "aapl", "msft"' in hint

    def test_hint_caps_examples_at_three(self) -> None:
        from arango_cypher.nl2cypher._core import _property_quality_hint

        hint = _property_quality_hint({"examples": ["a", "b", "c", "d", "e"]})
        assert '"d"' not in hint

    def test_hint_empty_without_signals(self) -> None:
        from arango_cypher.nl2cypher._core import _property_quality_hint

        assert _property_quality_hint({"type": "string"}) == ""
        assert _property_quality_hint(None) == ""

    def test_schema_summary_includes_value_shape_block(self) -> None:
        from arango_cypher.nl2cypher import _build_schema_summary
        from arango_query_core.mapping import MappingBundle

        bundle = MappingBundle(
            conceptual_schema={"entityTypes": ["ORG"]},
            physical_mapping={
                "entities": {
                    "ORG": {
                        "collectionName": "Node",
                        "properties": {
                            "name": {
                                "type": "string",
                                "valueShape": "ticker",
                                "exampleValues": ["cinf", "aapl"],
                            }
                        },
                    }
                }
            },
            metadata={},
        )
        summary = _build_schema_summary(bundle)
        assert "shape: ticker" in summary
        assert 'e.g. "cinf", "aapl"' in summary
        assert "Value-shape hints:" in summary

    def test_schema_summary_omits_block_without_shapes(self, movies_mapping) -> None:
        from arango_cypher.nl2cypher import _build_schema_summary

        summary = _build_schema_summary(movies_mapping)
        assert "Value-shape hints:" not in summary
