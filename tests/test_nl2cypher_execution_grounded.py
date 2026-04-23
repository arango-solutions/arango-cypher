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
        """Three EXPLAIN failures with ``max_retries=2`` → fail-closed (WP-29 D4).

        Pre-WP-29 this path returned the last invalid attempt with a
        WARNING-prefixed ``explanation`` and ``confidence=0.3``; the UI
        then wrote that invalid Cypher into the editor. WP-29 mirrors
        the tenant-guardrail pattern and returns an empty ``cypher``
        with ``method="validation_failed"`` so the UI surfaces a red
        banner and leaves the editor untouched. The last attempted
        Cypher is preserved inside ``explanation`` for inspection.
        """
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
        assert res.cypher == "", "WP-29 D4: must NOT write invalid Cypher into the editor"
        assert res.method == "validation_failed"
        assert res.confidence == 0.0
        assert "Last attempted Cypher was:" in res.explanation
        # The last scripted attempt must still be inspectable inside the
        # explanation body (WP-30 can consume this for error hinting).
        assert "MATCH (n:C) RETURN n" in res.explanation

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
        """``max_retries=0`` with a single EXPLAIN-failing response → fail-closed.

        WP-29 D4: the retry loop no longer returns the invalid Cypher
        on exhaustion; it emits ``method="validation_failed"`` with
        ``cypher=""`` so the UI cannot accidentally load invalid
        Cypher into the editor.
        """
        provider = _Provider(["```cypher\nMATCH (n:A) RETURN n\n```"])
        db = _FakeDb(lambda a, b: {"error": True, "errorMessage": "bad"})
        schema_summary = _build_schema_summary(movies_mapping)
        res = _call_llm_with_retry(
            "q", schema_summary, provider, max_retries=0,
            mapping=movies_mapping, db=db,
        )
        assert res is not None
        assert res.retries == 0
        assert res.method == "validation_failed"
        assert res.cypher == ""
        assert res.confidence == 0.0

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


# ---------------------------------------------------------------------------
# WP-29: fail-closed retry exhaustion + retry_context seeding
# ---------------------------------------------------------------------------


class TestValidationFailedFailClosed:
    """WP-29 D4: retry-budget exhaustion returns empty-cypher + structured method."""

    def test_call_llm_with_retry_fails_closed_on_exhaustion(
        self, movies_mapping,
    ) -> None:
        """Provider returns unparseable text every time; after retries we
        must not leak ``best_cypher`` into the result — ``cypher`` must be
        ``""`` and ``method`` must be ``"validation_failed"``."""
        provider = _Provider([
            "this is not cypher at all",
            "still not cypher",
            "nope",
        ])
        schema_summary = _build_schema_summary(movies_mapping)
        res = _call_llm_with_retry(
            "q", schema_summary, provider, max_retries=2,
            mapping=movies_mapping, db=None,
        )
        assert res is not None
        assert res.cypher == ""
        assert res.method == "validation_failed"
        assert res.confidence == 0.0
        assert res.retries == 2
        assert "validation failed after 3 attempts" in res.explanation
        assert "Last attempted Cypher was:" in res.explanation

    def test_call_llm_with_retry_does_not_write_invalid_cypher(
        self, movies_mapping,
    ) -> None:
        """A caller that forgets to branch on ``method`` still cannot
        accidentally populate the editor — ``result.cypher`` is empty
        even though an invalid query was attempted."""
        provider = _Provider(["garbage garbage garbage"])
        schema_summary = _build_schema_summary(movies_mapping)
        res = _call_llm_with_retry(
            "q", schema_summary, provider, max_retries=0,
            mapping=movies_mapping, db=None,
        )
        assert res is not None
        assert not res.cypher, (
            "validation_failed contract: UI must be unable to write "
            "invalid Cypher into the editor via the cypher field"
        )

    def test_validation_failed_logs_warning(
        self, movies_mapping, caplog,
    ) -> None:
        """Exhaustion emits a WARN log so operators can audit the rate."""
        import logging

        provider = _Provider(["not cypher"])
        schema_summary = _build_schema_summary(movies_mapping)
        with caplog.at_level(logging.WARNING, logger="arango_cypher.nl2cypher._core"):
            res = _call_llm_with_retry(
                "q", schema_summary, provider, max_retries=0,
                mapping=movies_mapping, db=None,
            )
        assert res is not None and res.method == "validation_failed"
        matching = [
            r for r in caplog.records
            if "validation_failed" in r.getMessage()
            and r.levelno == logging.WARNING
        ]
        assert matching, (
            "expected at least one WARN record citing validation_failed; "
            f"got: {[r.getMessage() for r in caplog.records]}"
        )

    def test_nl_to_cypher_returns_validation_failed_without_falling_back(
        self, movies_mapping,
    ) -> None:
        """``nl_to_cypher`` must surface ``validation_failed`` directly
        rather than falling through to the rule-based translator (which
        could emit a stale or partial query)."""
        provider = _Provider(["not cypher", "also not cypher"])
        res = nl_to_cypher(
            "who are the people?",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=provider,
            db=None,
            max_retries=1,
        )
        assert res.method == "validation_failed"
        assert res.cypher == ""


class TestRetryContextSeeding:
    """WP-29 Part 4: caller-supplied ``retry_context`` seeds the first
    attempt's user message, enabling WP-30's 'regenerate with hint' UX."""

    def test_retry_context_seeded_on_first_attempt_when_provided(
        self, movies_mapping,
    ) -> None:
        provider = _Provider(["```cypher\nMATCH (p:Person) RETURN p\n```"])
        schema_summary = _build_schema_summary(movies_mapping)
        _call_llm_with_retry(
            "who are the people?",
            schema_summary,
            provider,
            max_retries=0,
            mapping=movies_mapping,
            db=None,
            retry_context="parser: expected ')' at position 17",
        )
        first_user = provider.seen_users[0]
        assert first_user.endswith(
            "Your previous Cypher was invalid: "
            "parser: expected ')' at position 17. Please fix it."
        )

    def test_no_retry_context_keeps_first_user_message_byte_identical(
        self, movies_mapping,
    ) -> None:
        """Without ``retry_context`` the first user message is the
        question verbatim — byte-identical to the pre-WP-29 shape."""
        provider = _Provider(["```cypher\nMATCH (p:Person) RETURN p\n```"])
        schema_summary = _build_schema_summary(movies_mapping)
        _call_llm_with_retry(
            "who are the people?",
            schema_summary,
            provider,
            max_retries=0,
            mapping=movies_mapping,
            db=None,
        )
        assert provider.seen_users[0] == "who are the people?"

    def test_retry_context_plumbed_through_nl_to_cypher(
        self, movies_mapping,
    ) -> None:
        """End-to-end: the public ``nl_to_cypher`` accepts
        ``retry_context`` and forwards it into the builder."""
        provider = _Provider(["```cypher\nMATCH (p:Person) RETURN p\n```"])
        nl_to_cypher(
            "find people",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=provider,
            db=None,
            retry_context="translate error: unknown property x",
        )
        assert provider.seen_users, "provider must have been called"
        assert (
            "Your previous Cypher was invalid: translate error: "
            "unknown property x. Please fix it."
        ) in provider.seen_users[0]
