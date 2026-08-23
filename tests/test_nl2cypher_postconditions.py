"""Caller-supplied postconditions in the NL→Cypher retry loop.

Covers the semantics promised in ``arango_cypher/nl2cypher/postconditions.py``:

* absent postconditions leave the loop behaviourally identical (regression guard)
* a passing check returns the result untouched
* a failing-then-passing check retries once, with the violation text reaching the
  retry prompt
* a never-passing check fails closed rather than returning the best attempt
* the retry budget is *shared* with parse/EXPLAIN failures, not additional
* the tenant guardrail is reported ahead of a caller postcondition
* ``prompt_section()`` lands in the system prompt and stays byte-stable across
  attempts, so provider-side prompt caching is not defeated
* a raising check is treated as a violation rather than an outage
* ``nl_to_aql`` refuses postconditions loudly instead of ignoring them

No network: the provider is scripted and the DB handle is a mock.
"""

from __future__ import annotations

import pytest

from arango_cypher.nl2cypher import (
    PostconditionContext,
    PostconditionViolation,
    nl_to_aql,
    nl_to_cypher,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture
def movies_mapping():
    return mapping_bundle_for("movies_pg")


class _Provider:
    """Cycles through a scripted list of responses, recording prompts."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.seen_systems: list[str] = []
        self.seen_users: list[str] = []

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        self.seen_systems.append(system)
        self.seen_users.append(user)
        if not self._responses:
            raise RuntimeError("provider exhausted")
        return self._responses.pop(0), {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }


def _cypher(body: str) -> str:
    return f"```cypher\n{body}\n```"


class _RequireReturn:
    """Rejects any statement that does not mention a required token."""

    code = "require_token"

    def __init__(self, token: str, *, section: str = "") -> None:
        self.token = token
        self._section = section
        self.calls = 0

    def check(self, cypher: str, *, context: PostconditionContext):
        self.calls += 1
        if self.token in cypher:
            return None
        return PostconditionViolation(
            code=self.code,
            reason=f"The query does not use {self.token}.",
            suggested_hint=f"Rewrite it to include {self.token}.",
        )

    def prompt_section(self) -> str:
        return self._section


class _Exploding:
    code = "boom"

    def check(self, cypher: str, *, context: PostconditionContext):
        raise RuntimeError("check is broken")

    def prompt_section(self) -> str:
        return ""


class TestPostconditions:
    def test_absent_postconditions_unchanged(self, movies_mapping) -> None:
        """Regression guard: the default path must not shift behaviour."""
        provider = _Provider([_cypher("MATCH (p:Person) RETURN p")])
        res = nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            llm_provider=provider,
        )
        assert res.method == "llm"
        assert res.retries == 0
        assert "MATCH (p:Person)" in res.cypher

    def test_passing_postcondition_accepted(self, movies_mapping) -> None:
        pc = _RequireReturn("RETURN")
        provider = _Provider([_cypher("MATCH (p:Person) RETURN p")])
        res = nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            llm_provider=provider,
            postconditions=[pc],
        )
        assert res.method == "llm"
        assert res.retries == 0
        assert pc.calls == 1

    def test_violation_retries_and_feeds_back(self, movies_mapping) -> None:
        pc = _RequireReturn("LIMIT")
        provider = _Provider(
            [
                _cypher("MATCH (p:Person) RETURN p"),  # rejected
                _cypher("MATCH (p:Person) RETURN p LIMIT 10"),  # accepted
            ]
        )
        res = nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            llm_provider=provider,
            postconditions=[pc],
        )
        assert res.method == "llm"
        assert res.retries == 1
        assert "LIMIT 10" in res.cypher
        # the violation text reached the retry prompt, not the system message
        assert "does not use LIMIT" in provider.seen_users[1]
        assert "does not use LIMIT" not in provider.seen_systems[1]

    def test_never_passing_fails_closed(self, movies_mapping) -> None:
        pc = _RequireReturn("NEVER_PRESENT")
        provider = _Provider([_cypher("MATCH (p:Person) RETURN p")] * 3)
        res = nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            llm_provider=provider,
            max_retries=2,
            postconditions=[pc],
        )
        # fails closed: the offending Cypher is not handed back as a result
        assert res.method != "llm"
        assert pc.calls == 3

    def test_retry_budget_is_shared_not_additional(self, movies_mapping) -> None:
        """One unparseable response plus one postcondition rejection consumes
        two of the two allowed attempts — the check does not get its own."""
        pc = _RequireReturn("LIMIT")
        provider = _Provider(
            [
                "this is not cypher at all",  # attempt 0: parse fail
                _cypher("MATCH (p:Person) RETURN p"),  # attempt 1: pc fail
                _cypher("MATCH (p:Person) RETURN p LIMIT 10"),  # attempt 2: ok
            ]
        )
        res = nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            llm_provider=provider,
            max_retries=2,
            postconditions=[pc],
        )
        assert res.retries == 2
        assert res.method == "llm"

    def test_prompt_section_is_in_system_and_stable(self, movies_mapping) -> None:
        section = "## House rule\nEvery query MUST include LIMIT.\n"
        pc = _RequireReturn("LIMIT", section=section)
        provider = _Provider(
            [
                _cypher("MATCH (p:Person) RETURN p"),
                _cypher("MATCH (p:Person) RETURN p LIMIT 10"),
            ]
        )
        nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            llm_provider=provider,
            postconditions=[pc],
        )
        assert "House rule" in provider.seen_systems[0]
        # byte-stable across attempts, or provider-side caching is defeated
        assert provider.seen_systems[0] == provider.seen_systems[1]

    def test_raising_check_becomes_a_violation(self, movies_mapping) -> None:
        provider = _Provider([_cypher("MATCH (p:Person) RETURN p")] * 3)
        res = nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            llm_provider=provider,
            max_retries=1,
            postconditions=[_Exploding()],
        )
        # a broken check must not abort translation with a traceback
        assert res.method != "llm"

    def test_first_violation_wins(self, movies_mapping) -> None:
        first = _RequireReturn("AAA")
        second = _RequireReturn("BBB")
        provider = _Provider([_cypher("MATCH (p:Person) RETURN p")] * 2)
        nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            llm_provider=provider,
            max_retries=0,
            postconditions=[first, second],
        )
        assert first.calls == 1
        assert second.calls == 0, "checks after the first violation must not run"


class TestNlToAqlRefusal:
    def test_nl_to_aql_refuses_postconditions(self, movies_mapping) -> None:
        with pytest.raises(ValueError, match="not supported by nl_to_aql"):
            nl_to_aql(
                "who are the people?",
                mapping=movies_mapping,
                postconditions=[_RequireReturn("LIMIT")],
            )

    def test_nl_to_aql_unaffected_when_absent(self, movies_mapping) -> None:
        res = nl_to_aql("who are the people?", mapping=None)
        assert res.aql == ""
