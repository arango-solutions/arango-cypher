"""Seam tests for :class:`arango_cypher.nl2cypher.adapter.CypherAdapter`.

The adapter is this package's implementation of
:class:`arango_query_core.nl.seams.QueryLanguageAdapter` — step 2 of the
nl-engine extraction. Two invariants matter:

1. Each seam delegates to the pre-existing ``_core`` / tenant-guardrail
   machinery (no behavior of its own), pinned here seam by seam.
2. The adapter actually composes with the shared
   :class:`arango_query_core.nl.NLQueryEngine` loop — generate,
   validate-fail, repair, guardrail-refuse — driven by a fake provider.
"""

from __future__ import annotations

import pytest
from arango_query_core.nl import NLQueryEngine
from arango_query_core.nl.seams import QueryLanguageAdapter, ValidationResult

from arango_cypher.nl2cypher import _SYSTEM_PROMPT, CypherAdapter, PromptBuilder
from arango_cypher.nl2cypher._core import _augment_explain_hint
from arango_cypher.nl2cypher.tenant_guardrail import TenantContext


class FakeProvider:
    """Scripted LLM provider: returns canned responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        self.calls.append((system, user))
        content = self._responses.pop(0)
        return content, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


class TestProtocolConformance:
    def test_satisfies_runtime_protocol(self) -> None:
        assert isinstance(CypherAdapter(), QueryLanguageAdapter)

    def test_language_tag(self) -> None:
        assert CypherAdapter().language == "cypher"


class TestGrammarSeam:
    def test_matches_zero_shot_prompt_builder_byte_for_byte(self) -> None:
        """Seam 1 must render exactly what the legacy pipeline renders.

        ``PromptBuilder.render_system()`` with no extensions is the
        pinned pre-refactor prompt; the adapter must not drift from it.
        """
        schema = "Nodes:\n  (:Person {name})\n"
        adapter = CypherAdapter()
        builder = PromptBuilder(schema_summary=schema)
        assert adapter.grammar_prompt_section(schema) == builder.render_system()

    def test_embeds_schema_context(self) -> None:
        section = CypherAdapter().grammar_prompt_section("MARKER-SCHEMA")
        assert "MARKER-SCHEMA" in section
        assert "{schema}" not in section


class TestFewShotSeam:
    def test_returns_default_index(self) -> None:
        """Seam 2 is the shipped-corpora index ``nl_to_cypher`` uses."""
        from arango_cypher.nl2cypher import _core

        index = CypherAdapter().few_shot_index()
        assert index is _core._get_default_fewshot_index()


class TestValidateSeam:
    def test_valid_cypher_passes(self) -> None:
        result = CypherAdapter().validate("MATCH (n:Person) RETURN n")
        assert result.ok
        assert result.error == ""

    def test_parse_failure_is_reported_with_code(self) -> None:
        result = CypherAdapter().validate("MATCH (n:Person RETURN")
        assert not result.ok
        assert result.error
        assert result.code == "E_CYPHER_PARSE"

    def test_empty_query_rejected(self) -> None:
        assert not CypherAdapter().validate("").ok


class TestRepairSeam:
    def test_plain_error_defers_to_engine(self) -> None:
        """No known EXPLAIN shape → empty string → engine uses bare error."""
        failure = ValidationResult(ok=False, error="mismatched input 'RETRUN'")
        assert CypherAdapter().repair_hint("MATCH", failure) == ""

    def test_multi_assignment_error_gets_targeted_hint(self) -> None:
        err = "variable 'p' is assigned multiple times"
        failure = ValidationResult(ok=False, error=err)
        hint = CypherAdapter().repair_hint("MATCH p = (a)-->(p) RETURN p", failure)
        assert hint == err + _augment_explain_hint(err)
        assert "path variable" in hint


class TestGuardrailSeam:
    def test_no_tenant_context_allows(self) -> None:
        verdict = CypherAdapter().guardrails("MATCH (n) RETURN n", {})
        assert verdict.allowed

    def test_unscoped_query_refused_under_tenant_context(self) -> None:
        ctx = TenantContext(property="NAME", value="Acme")
        verdict = CypherAdapter().guardrails(
            "MATCH (d:Device) RETURN d",
            {"tenant_context": ctx},
        )
        assert not verdict.allowed
        assert verdict.reasons

    def test_tenant_bound_query_allowed(self) -> None:
        ctx = TenantContext(property="NAME", value="Acme")
        verdict = CypherAdapter().guardrails(
            "MATCH (t:Tenant {NAME: 'Acme'})-[:OWNS]->(d:Device) RETURN d",
            {"tenant_context": ctx},
        )
        assert verdict.allowed


class TestEngineComposition:
    """The adapter must drive arango_query_core's shared loop end-to-end."""

    SCHEMA = "Nodes:\n  (:Person {name})\n"

    def test_happy_path(self) -> None:
        provider = FakeProvider(["```cypher\nMATCH (n:Person) RETURN n\n```"])
        engine = NLQueryEngine(provider=provider, adapter=CypherAdapter())
        result = engine.generate("find all people", schema_context=self.SCHEMA)
        assert result.ok
        assert result.query == "MATCH (n:Person) RETURN n"
        assert result.retries == 0
        assert result.total_tokens == 15

    def test_repair_loop_recovers_from_invalid_cypher(self) -> None:
        provider = FakeProvider(
            [
                "```cypher\nMATCH (n:Person RETURN\n```",
                "```cypher\nMATCH (n:Person) RETURN n\n```",
            ]
        )
        engine = NLQueryEngine(provider=provider, adapter=CypherAdapter())
        result = engine.generate("find all people", schema_context=self.SCHEMA)
        assert result.ok
        assert result.retries == 1
        # The retry prompt surfaced the parse failure to the model.
        assert "rejected" in provider.calls[1][1]

    def test_retry_exhaustion_surfaces_last_error(self) -> None:
        provider = FakeProvider(["MATCH (n:Person RETURN"] * 3)
        engine = NLQueryEngine(provider=provider, adapter=CypherAdapter(), max_retries=2)
        result = engine.generate("find all people", schema_context=self.SCHEMA)
        assert not result.ok
        assert result.error
        assert result.validation is not None
        assert result.validation.code == "E_CYPHER_PARSE"

    def test_guardrail_refusal_is_surfaced_not_silent(self) -> None:
        provider = FakeProvider(["```cypher\nMATCH (d:Device) RETURN d\n```"])
        engine = NLQueryEngine(
            provider=provider,
            adapter=CypherAdapter(),
            guardrail_context={"tenant_context": TenantContext(property="NAME", value="Acme")},
        )
        result = engine.generate("show all devices", schema_context=self.SCHEMA)
        assert not result.ok
        assert result.guardrail is not None
        assert not result.guardrail.allowed
        assert result.error

    def test_system_prompt_contains_grammar_and_schema(self) -> None:
        provider = FakeProvider(["```cypher\nMATCH (n:Person) RETURN n\n```"])
        engine = NLQueryEngine(provider=provider, adapter=CypherAdapter())
        engine.generate("find all people", schema_context=self.SCHEMA)
        system = provider.calls[0][0]
        assert system.startswith(_SYSTEM_PROMPT.split("\n", 1)[0])
        assert self.SCHEMA.strip() in system


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
