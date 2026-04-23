"""Shape and byte-identity tests for :class:`PromptBuilder`.

These tests pin the contract between the NL→Cypher pipeline and the
prompt layer. The zero-shot rendering MUST remain byte-identical to
the pre-refactor ``_SYSTEM_PROMPT.format(schema=...)`` output so that
provider-side prefix caching keeps working and Wave 4a sub-agents can
layer on top without regressing the baseline prompt.
"""
from __future__ import annotations

from arango_cypher.nl2cypher import _SYSTEM_PROMPT, PromptBuilder
from arango_cypher.nl2cypher._core import _build_schema_summary, _escape_label
from arango_query_core.mapping import MappingBundle

FROZEN_SYSTEM_PROMPT = (
    "You are a Cypher query expert."
    " Given a natural language question and a graph schema,"
    " generate a valid Cypher query.\n"
    "\n"
    "Rules:\n"
    "- Use only node labels and relationship types from the schema\n"
    "- Use property names from the schema\n"
    "- Return a single Cypher query (no explanation)\n"
    "- Use standard Cypher syntax (MATCH, WHERE, RETURN, ORDER BY, LIMIT, etc.)\n"
    "- For counts, use count()\n"
    "- For aggregations, use collect(), sum(), avg(), min(), max()\n"
    "- Labels and relationship types containing characters other than ASCII"
    " letters, digits, and underscore must be wrapped in backticks, e.g."
    " MATCH (d:`Compliance.rst`) RETURN d.doc_version. The schema below has"
    " already pre-escaped such names; copy them verbatim.\n"
    "- Wrap the query in ```cypher``` code block\n"
    "\n"
    "{schema}"
)


class TestZeroShotByteIdentity:
    def test_matches_pre_refactor_format_call(self) -> None:
        builder = PromptBuilder(schema_summary="X")
        assert builder.render_system() == FROZEN_SYSTEM_PROMPT.format(schema="X")

    def test_matches_current_system_prompt_constant(self) -> None:
        builder = PromptBuilder(schema_summary="Graph:\n  Node :Person (name)")
        expected = _SYSTEM_PROMPT.replace(
            "{schema}", "Graph:\n  Node :Person (name)",
        )
        assert builder.render_system() == expected

    def test_frozen_prompt_matches_module_constant(self) -> None:
        assert FROZEN_SYSTEM_PROMPT == _SYSTEM_PROMPT

    def test_empty_schema_is_valid(self) -> None:
        builder = PromptBuilder(schema_summary="")
        assert builder.render_system() == FROZEN_SYSTEM_PROMPT.format(schema="")


class TestRenderUser:
    def test_no_retry_context_returns_question_unchanged(self) -> None:
        builder = PromptBuilder(schema_summary="X")
        assert builder.render_user("find all people") == "find all people"

    def test_retry_context_appends_same_wording_as_legacy_loop(self) -> None:
        builder = PromptBuilder(schema_summary="X", retry_context="ERR")
        expected = (
            "q\n\n"
            "Your previous Cypher was invalid: ERR. Please fix it."
        )
        assert builder.render_user("q") == expected

    def test_retry_context_cleared_between_uses(self) -> None:
        builder = PromptBuilder(schema_summary="X")
        builder.retry_context = "syntax error"
        first = builder.render_user("q")
        builder.retry_context = ""
        second = builder.render_user("q")
        assert "syntax error" in first
        assert second == "q"


class TestFewShotSection:
    def test_few_shot_examples_appear_after_schema(self) -> None:
        nl = "who directed The Matrix"
        cy = "MATCH (p:Person)-[:DIRECTED]->(m:Movie) RETURN p"
        builder = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[(nl, cy)],
        )
        rendered = builder.render_system()
        schema_idx = rendered.index("SCHEMA")
        examples_idx = rendered.index("Examples")
        assert schema_idx < examples_idx
        assert nl in rendered
        assert cy in rendered

    def test_zero_shot_has_no_examples_section(self) -> None:
        builder = PromptBuilder(schema_summary="SCHEMA")
        assert "Examples" not in builder.render_system()

    def test_multiple_examples_render_in_order(self) -> None:
        builder = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[
                ("q1", "MATCH (n) RETURN n"),
                ("q2", "MATCH (m:Movie) RETURN m"),
            ],
        )
        rendered = builder.render_system()
        assert rendered.index("q1") < rendered.index("q2")


class TestResolvedEntitiesSection:
    def test_resolved_entities_appear_after_schema(self) -> None:
        builder = PromptBuilder(
            schema_summary="SCHEMA",
            resolved_entities=["'Tom Hanks' -> Person {name: 'Tom Hanks'}"],
        )
        rendered = builder.render_system()
        assert "Resolved entities" in rendered
        assert "Tom Hanks" in rendered
        assert rendered.index("SCHEMA") < rendered.index("Resolved entities")

    def test_zero_shot_has_no_resolved_entities_section(self) -> None:
        builder = PromptBuilder(schema_summary="SCHEMA")
        assert "Resolved entities" not in builder.render_system()


class TestExtensionsDoNotBreakSystemPrefix:
    def test_system_prefix_is_preserved_with_extensions(self) -> None:
        """The schema-first prefix MUST remain byte-stable when extensions
        are added, so providers can still cache the prefix across calls."""
        bare = PromptBuilder(schema_summary="SCHEMA").render_system()
        with_examples = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[("q", "MATCH (n) RETURN n")],
        ).render_system()
        assert with_examples.startswith(bare)


# ---------------------------------------------------------------------------
# WP-29: label-escaping in the schema card and system prompt
# ---------------------------------------------------------------------------


def _bundle(
    entities: list[dict] | None = None,
    relationships: list[dict] | None = None,
) -> MappingBundle:
    """Build a minimal MappingBundle shaped like the conceptual schema.

    Kept inline here (rather than in conftest) because these tests are the
    only ones that need a post-WP-27 bundle with exotic label names.
    """
    return MappingBundle(
        conceptual_schema={
            "entities": entities or [],
            "relationships": relationships or [],
        },
        physical_mapping={},
        metadata={},
    )


class TestEscapeLabelHelper:
    def test_escape_label_bare_identifier_unchanged(self) -> None:
        assert _escape_label("Person") == "Person"

    def test_escape_label_leading_underscore_allowed(self) -> None:
        assert _escape_label("_Internal") == "_Internal"

    def test_escape_label_digits_after_first_char_allowed(self) -> None:
        assert _escape_label("Entity42") == "Entity42"

    def test_escape_label_wraps_dotted_name(self) -> None:
        assert _escape_label("Compliance.rst") == "`Compliance.rst`"

    def test_escape_label_wraps_hyphenated_relationship(self) -> None:
        assert _escape_label("HAS-CONTROL") == "`HAS-CONTROL`"

    def test_escape_label_wraps_leading_digit(self) -> None:
        assert _escape_label("1stParty") == "`1stParty`"

    def test_escape_label_wraps_space(self) -> None:
        assert _escape_label("Tax Form") == "`Tax Form`"

    def test_escape_label_empty_returns_empty(self) -> None:
        assert _escape_label("") == ""


class TestSchemaSummaryLabelEscaping:
    def test_schema_summary_escapes_dotted_entity(self) -> None:
        bundle = _bundle(entities=[{"name": "Compliance.rst", "properties": []}])
        summary = _build_schema_summary(bundle)
        assert "Node :`Compliance.rst`" in summary
        assert "Node :Compliance.rst" not in summary

    def test_schema_summary_escapes_hyphenated_relationship_type(self) -> None:
        bundle = _bundle(
            entities=[
                {"name": "Document", "properties": []},
                {"name": "Control", "properties": []},
            ],
            relationships=[
                {
                    "type": "HAS-CONTROL",
                    "fromEntity": "Document",
                    "toEntity": "Control",
                    "properties": [],
                },
            ],
        )
        summary = _build_schema_summary(bundle)
        assert "`HAS-CONTROL`" in summary
        # Bare-identifier endpoints must not be escaped.
        assert "(:Document)" in summary
        assert "(:Control)" in summary

    def test_schema_summary_escapes_both_endpoints_when_dotted(self) -> None:
        bundle = _bundle(
            entities=[
                {"name": "Compliance.rst", "properties": []},
                {"name": "Regulation.v2", "properties": []},
            ],
            relationships=[
                {
                    "type": "REFERENCES",
                    "fromEntity": "Compliance.rst",
                    "toEntity": "Regulation.v2",
                    "properties": [],
                },
            ],
        )
        summary = _build_schema_summary(bundle)
        assert "(:`Compliance.rst`)" in summary
        assert "(:`Regulation.v2`)" in summary
        assert "-[:REFERENCES]->" in summary


class TestZeroShotByteIdenticalForBareNames:
    """Critical regression test: bundles with only bare identifiers must
    render byte-for-byte identically to the frozen pre-WP-29-rule layout,
    modulo the new system-prompt rule. A schema card containing only
    ``[A-Za-z_][A-Za-z0-9_]*`` names MUST never trigger backtick output."""

    def test_bare_name_schema_card_has_no_backticks(self) -> None:
        bundle = _bundle(
            entities=[
                {"name": "Person", "properties": [{"name": "name"}]},
                {"name": "Movie", "properties": [{"name": "title"}]},
            ],
            relationships=[
                {
                    "type": "ACTED_IN",
                    "fromEntity": "Person",
                    "toEntity": "Movie",
                    "properties": [],
                },
            ],
        )
        summary = _build_schema_summary(bundle)
        assert "`" not in summary, (
            "Bare-identifier schema card must be byte-identical to pre-WP-29 "
            f"rendering (no backticks anywhere); got: {summary!r}"
        )
        assert "Node :Person" in summary
        assert "Node :Movie" in summary
        assert "(:Person)-[:ACTED_IN]->(:Movie)" in summary

    def test_zero_shot_system_prompt_for_bare_names_matches_frozen(self) -> None:
        bundle = _bundle(
            entities=[{"name": "Person", "properties": [{"name": "name"}]}],
        )
        summary = _build_schema_summary(bundle)
        builder = PromptBuilder(schema_summary=summary)
        assert builder.render_system() == FROZEN_SYSTEM_PROMPT.format(
            schema=summary,
        )


class TestSystemPromptBacktickRule:
    def test_system_prompt_contains_backtick_rule(self) -> None:
        assert "wrapped in backticks" in _SYSTEM_PROMPT
        assert "pre-escaped" in _SYSTEM_PROMPT

    def test_system_prompt_backtick_rule_cites_example(self) -> None:
        assert "`Compliance.rst`" in _SYSTEM_PROMPT
