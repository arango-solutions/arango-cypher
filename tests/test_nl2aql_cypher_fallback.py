"""Cypher->AQL fallback via the NL->AQL path.

When the deterministic transpiler cannot handle a Cypher query, the workbench
offers a "Generate AQL with AI" action that calls ``/nl2aql`` with the failing
Cypher in the ``cypher`` field. The LLM then translates Cypher->AQL using the
same physical-schema system prompt, only the user message changes.

These tests verify:
- ``nl_to_aql(cypher=...)`` reframes the LLM user message as a Cypher
  translation and still returns/validates AQL;
- the question path is unchanged when ``cypher`` is not supplied;
- the ``/nl2aql`` endpoint threads ``cypher`` through to ``nl_to_aql``.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from arango_cypher.nl2cypher import nl_to_aql
from arango_cypher.service import app

client = TestClient(app)


MAPPING: dict[str, Any] = {
    "conceptual_schema": {"entities": [{"name": "Node"}], "relationships": []},
    "physical_mapping": {
        "entities": {
            "Person": {
                "style": "LABEL",
                "collectionName": "Node",
                "typeField": "type",
                "typeValue": "Person",
            },
        },
        "relationships": {
            "KNOWS": {
                "style": "GENERIC_WITH_TYPE",
                "edgeCollectionName": "relations",
                "typeField": "type",
                "typeValue": "KNOWS",
            },
        },
    },
    "metadata": {},
}


class _StubProvider:
    """LLM stub that records the last user message and returns canned AQL."""

    def __init__(self, aql: str = "FOR n IN Node RETURN n") -> None:
        self._aql = aql
        self.last_system: str | None = None
        self.last_user: str | None = None
        self.calls = 0

    def generate(self, system: str, user: str) -> tuple[str, dict]:
        self.calls += 1
        self.last_system = system
        self.last_user = user
        return f"```aql\n{self._aql}\n```", {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cached_tokens": 0,
        }


class TestNlToAqlCypherMode:
    def test_cypher_reframes_user_message(self):
        stub = _StubProvider()
        out = nl_to_aql(
            "",
            mapping=MAPPING,
            llm_provider=stub,
            cypher="MATCH (n) RETURN labels(n), count(n)",
        )
        assert out.aql == "FOR n IN Node RETURN n"
        assert stub.last_user is not None
        assert "translate the following opencypher" in stub.last_user.lower()
        assert "MATCH (n) RETURN labels(n), count(n)" in stub.last_user

    def test_question_mode_unchanged_when_no_cypher(self):
        stub = _StubProvider()
        out = nl_to_aql("how many people are there?", mapping=MAPPING, llm_provider=stub)
        assert out.aql == "FOR n IN Node RETURN n"
        assert stub.last_user == "how many people are there?"

    def test_failure_message_mentions_cypher(self):
        # A provider that never produces valid AQL -> the final explanation
        # should reflect the Cypher-translation context.
        class _BadProvider:
            def generate(self, system: str, user: str) -> tuple[str, dict]:  # noqa: ARG002
                return "no code block here", {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "cached_tokens": 0,
                }

        out = nl_to_aql(
            "",
            mapping=MAPPING,
            llm_provider=_BadProvider(),
            cypher="MATCH (n) RETURN n",
            max_retries=0,
        )
        assert out.aql == ""
        assert "cypher" in out.explanation.lower()


class TestNl2AqlEndpointCypherField:
    def test_endpoint_threads_cypher_to_nl_to_aql(self, monkeypatch):
        captured: dict[str, Any] = {}

        def _fake_nl_to_aql(question, *, mapping=None, tenant_context=None, cypher=None, **kwargs):
            captured["question"] = question
            captured["cypher"] = cypher

            class _R:
                aql = "FOR n IN Node RETURN n"
                bind_vars: dict[str, Any] = {}
                explanation = "ok"
                confidence = 0.8
                method = "llm_direct"
                prompt_tokens = 1
                completion_tokens = 1
                total_tokens = 2
                cached_tokens = 0

            return _R()

        # Patch the symbol imported lazily inside the endpoint.
        monkeypatch.setattr("arango_cypher.nl2cypher.nl_to_aql", _fake_nl_to_aql)

        resp = client.post(
            "/nl2aql",
            json={"question": "", "mapping": MAPPING, "cypher": "MATCH (n) RETURN n"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["aql"] == "FOR n IN Node RETURN n"
        assert captured["cypher"] == "MATCH (n) RETURN n"
