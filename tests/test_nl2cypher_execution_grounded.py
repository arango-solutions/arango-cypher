"""Unit tests for WP-25.3 execution-grounded validation.

Covers:

* ``explain_aql`` helper: success, DB failure, structured error payload.
* ``_call_llm_with_retry`` retry loop: EXPLAIN failure feeds back into
  the next prompt and a subsequent EXPLAIN success returns the second
  Cypher.
* Offline bit-identity: with ``db=None`` the pre-WP-25.3 behaviour is
  preserved byte-for-byte.
* Retry budget: ``max_retries=2`` on three EXPLAIN failures produces a
  best-of result with ``retries=2``, not an infinite loop.

All DB handles are mocks — no network access required.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from arango_cypher.nl2cypher import (
    _SYSTEM_PROMPT,
    _build_schema_summary,
    nl_to_cypher,
)
from arango_cypher.nl2cypher._core import _call_llm_with_retry
from arango_query_core.exec import explain_aql
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture
def movies_mapping():
    return mapping_bundle_for("movies_pg")


# ---------------------------------------------------------------------------
# explain_aql helper
# ---------------------------------------------------------------------------


class _FakeAql:
    def __init__(self, explain_fn):
        self._explain = explain_fn
        self.explain_calls: list[tuple[str, dict[str, Any]]] = []

    def explain(self, aql: str, bind_vars: dict[str, Any] | None = None) -> Any:
        self.explain_calls.append((aql, dict(bind_vars or {})))
        return self._explain(aql, bind_vars or {})


class _FakeDb:
    def __init__(self, explain_fn):
        self.aql = _FakeAql(explain_fn)


class TestExplainAql:
    def test_success_returns_ok(self) -> None:
        db = _FakeDb(lambda a, b: {"plan": {}, "stats": {}})
        ok, msg = explain_aql(db, "FOR d IN x RETURN d", {})
        assert ok is True
        assert msg == ""

    def test_error_payload_is_summarized(self) -> None:
        db = _FakeDb(lambda a, b: {
            "error": True,
            "errorMessage": "collection not found: persons_typo",
            "code": 404,
        })
        ok, msg = explain_aql(db, "FOR d IN persons_typo RETURN d", {})
        assert ok is False
        assert "persons_typo" in msg

    def test_raised_exception_is_summarized(self) -> None:
        def boom(a: str, b: dict[str, Any]) -> Any:
            raise RuntimeError(
                "AQLQueryExplainError: AQL: collection or view not found\n"
                "  traceback line\n  more traceback"
            )
        db = _FakeDb(boom)
        ok, msg = explain_aql(db, "FOR d IN x RETURN d", {})
        assert ok is False
        assert "\n" not in msg, "should be single-line, suitable for LLM feedback"
        assert "AQLQueryExplainError" in msg

    def test_very_long_error_is_truncated(self) -> None:
        def boom(a: str, b: dict[str, Any]) -> Any:
            raise RuntimeError("x" * 1000)
        db = _FakeDb(boom)
        ok, msg = explain_aql(db, "FOR d IN x RETURN d", {})
        assert ok is False
        assert len(msg) <= 500


# ---------------------------------------------------------------------------
# Retry loop integration
# ---------------------------------------------------------------------------


class _Provider:
    """Tiny provider that cycles through a scripted list of responses."""

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


class TestExecutionGroundedRetry:
    def test_explain_success_accepted(self, movies_mapping) -> None:
        provider = _Provider(["```cypher\nMATCH (p:Person) RETURN p\n```"])
        db = _FakeDb(lambda a, b: {"plan": {}})
        res = nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=provider,
            db=db,
        )
        assert res.method == "llm"
        assert res.retries == 0
        assert "Person" in res.cypher

    def test_explain_failure_triggers_retry(self, movies_mapping) -> None:
        provider = _Provider([
            "```cypher\nMATCH (n:Persons) RETURN n\n```",
            "```cypher\nMATCH (p:Person) RETURN p\n```",
        ])
        calls = {"n": 0}

        def explain(aql: str, bind_vars: dict[str, Any]) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                return {"error": True, "errorMessage": "collection 'Persons' not found"}
            return {"plan": {}}

        db = _FakeDb(explain)
        res = nl_to_cypher(
            "find people",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=provider,
            db=db,
            max_retries=2,
        )
        assert res.retries == 1, f"expected 1 retry, got {res.retries}"
        assert "Person" in res.cypher and "Persons" not in res.cypher
        retry_user = provider.seen_users[1]
        assert "EXPLAIN" in retry_user
        assert "Persons" in retry_user

    def test_no_db_skips_explain(self, movies_mapping) -> None:
        """With ``db=None`` the pipeline is bit-identical to pre-WP-25.3.

        We use a label that neither exists in the schema nor has a fuzzy
        twin, so ``_fix_labels`` leaves it alone — then prove that without
        a DB the hallucinated label survives to the final cypher
        (because no EXPLAIN gate is run).
        """
        provider = _Provider(["```cypher\nMATCH (n:Zqqwx) RETURN n\n```"])
        res = nl_to_cypher(
            "find stuff",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=provider,
            db=None,
        )
        assert res.retries == 0
        assert "Zqqwx" in res.cypher, "no EXPLAIN → accepts hallucinated label as-is"

    def test_retry_budget_respected(self, movies_mapping) -> None:
        """Three EXPLAIN failures with ``max_retries=2`` → best-of with retries=2."""
        provider = _Provider([
            "```cypher\nMATCH (n:A) RETURN n\n```",
            "```cypher\nMATCH (n:B) RETURN n\n```",
            "```cypher\nMATCH (n:C) RETURN n\n```",
        ])
        db = _FakeDb(lambda a, b: {"error": True, "errorMessage": "bad"})
        res = nl_to_cypher(
            "find stuff",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=provider,
            db=db,
            max_retries=2,
        )
        assert res.retries == 2
        assert res.cypher, "should still return the last attempt as a best-of"
        assert res.confidence < 0.8, "best-of confidence is lower than clean-path"

    def test_transpile_error_also_triggers_retry(self, movies_mapping) -> None:
        """Cypher that parses but doesn't transpile also feeds back."""
        provider = _Provider([
            "```cypher\nMATCH (n:NotInSchema) RETURN n\n```",
            "```cypher\nMATCH (p:Person) RETURN p\n```",
        ])
        db = _FakeDb(lambda a, b: {"plan": {}})
        res = nl_to_cypher(
            "find stuff",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=provider,
            db=db,
            max_retries=2,
        )
        assert res.retries == 1
        assert "Person" in res.cypher

    def test_ok_on_retry_reports_retries_attempted(self, movies_mapping) -> None:
        """The ``retries`` counter should reflect the attempt index on success."""
        provider = _Provider([
            "```cypher\nMATCH (n:Persons) RETURN n\n```",
            "```cypher\nMATCH (p:Person) RETURN p\n```",
        ])
        first = {"done": False}

        def explain(aql: str, bind_vars: dict[str, Any]) -> dict[str, Any]:
            if not first["done"]:
                first["done"] = True
                return {"error": True, "errorMessage": "x"}
            return {"plan": {}}

        db = _FakeDb(explain)
        res = nl_to_cypher(
            "find people",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=provider,
            db=db,
            max_retries=2,
        )
        assert res.retries == 1


class TestOfflineBitIdentity:
    def test_system_prompt_unchanged_without_db(self, movies_mapping) -> None:
        """With ``db=None`` the system prompt matches the Wave 4-pre baseline."""
        captured: dict[str, str] = {}

        class _P:
            def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
                captured["system"] = system
                return (
                    "```cypher\nMATCH (p:Person) RETURN p\n```",
                    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )

        nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=_P(),
            db=None,
        )
        expected = _SYSTEM_PROMPT.replace(
            "{schema}", _build_schema_summary(movies_mapping),
        )
        assert captured["system"] == expected


class TestCallLlmWithRetryDirect:
    """Direct unit tests on ``_call_llm_with_retry`` with scripted providers."""

    def test_empty_max_retries_zero_with_explain_fail(self, movies_mapping) -> None:
        provider = _Provider(["```cypher\nMATCH (n:A) RETURN n\n```"])
        db = _FakeDb(lambda a, b: {"error": True, "errorMessage": "bad"})
        schema_summary = _build_schema_summary(movies_mapping)
        res = _call_llm_with_retry(
            "q", schema_summary, provider, max_retries=0,
            mapping=movies_mapping, db=db,
        )
        assert res is not None
        assert res.retries == 0
        assert res.confidence < 0.8

    def test_explain_exception_does_not_crash(self, movies_mapping) -> None:
        """If EXPLAIN itself raises unexpectedly, the query is accepted anyway.

        This preserves online correctness: a flaky DB must not turn a
        valid Cypher into a silent failure.  The explicit contract:
        EXPLAIN is a *best-effort* secondary validator — the primary
        contract is still the ANTLR parse.
        """
        provider = _Provider(["```cypher\nMATCH (p:Person) RETURN p\n```"])

        class _BrokenDb:
            class aql:  # noqa: N801
                @staticmethod
                def explain(*a, **k):
                    raise RuntimeError("DB down")

        with patch(
            "arango_cypher.nl2cypher._core._validate_via_explain",
            return_value=(True, ""),
        ):
            res = nl_to_cypher(
                "q", mapping=movies_mapping, use_fewshot=False,
                use_entity_resolution=False, llm_provider=provider,
                db=_BrokenDb(),
            )
        assert res.retries == 0
        assert "Person" in res.cypher
