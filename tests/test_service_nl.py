"""Contract tests for the ``/nl2cypher`` service endpoint.

Scope: pin the request/response contract between the UI and
:func:`arango_cypher.nl2cypher.nl_to_cypher` — specifically the WP-29
``retry_context`` plumbing introduced as a WP-30 dependency.

Philosophy: keep these tests lightweight and free of a real LLM
provider or live ArangoDB. We monkeypatch ``nl_to_cypher`` to capture
the keyword arguments the endpoint forwards, then assert on the
contract. Integration tests for the actual translation live in
``tests/test_nl2cypher_execution_grounded.py`` and the eval harness.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arango_cypher import service
from arango_cypher.service import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _fake_result(**overrides: Any) -> SimpleNamespace:
    base = {
        "cypher": "MATCH (p:Person) RETURN p",
        "explanation": "ok",
        "confidence": 0.8,
        "method": "llm",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "retries": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRetryContextPlumbing:
    """WP-29 Part 4 / WP-30 contract: ``retry_context`` survives the
    POST body → endpoint → :func:`nl_to_cypher` hop unchanged."""

    def test_retry_context_forwarded_to_nl_to_cypher(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_nl_to_cypher(question: str, **kwargs: Any):
            captured["question"] = question
            captured.update(kwargs)
            return _fake_result()

        # The endpoint imports lazily from ``.nl2cypher``; patch the
        # module-level symbol that the endpoint calls.
        from arango_cypher import nl2cypher as nl_module

        monkeypatch.setattr(nl_module, "nl_to_cypher", _fake_nl_to_cypher)

        resp = client.post(
            "/nl2cypher",
            json={
                "question": "find people",
                "mapping": {"conceptualSchema": {}, "physicalMapping": {}},
                "retry_context": "parser: unexpected ')' at position 17",
            },
        )
        assert resp.status_code == 200, resp.text
        assert captured["retry_context"] == ("parser: unexpected ')' at position 17")
        assert captured["question"] == "find people"

    def test_missing_retry_context_forwards_none(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Backward compat: pre-WP-29 clients that omit the field must
        see ``retry_context=None`` forwarded (not KeyError, not empty
        string) so the prompt builder stays byte-identical."""
        captured: dict[str, Any] = {}

        def _fake_nl_to_cypher(question: str, **kwargs: Any):
            captured.update(kwargs)
            return _fake_result()

        from arango_cypher import nl2cypher as nl_module

        monkeypatch.setattr(nl_module, "nl_to_cypher", _fake_nl_to_cypher)

        resp = client.post(
            "/nl2cypher",
            json={
                "question": "find people",
                "mapping": {"conceptualSchema": {}, "physicalMapping": {}},
            },
        )
        assert resp.status_code == 200, resp.text
        assert captured["retry_context"] is None


class TestValidationFailedResponseShape:
    """The UI relies on ``method === "validation_failed"`` + empty
    ``cypher`` to route into the red banner instead of the editor."""

    def test_validation_failed_result_passes_method_through(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _fake_nl_to_cypher(*args: Any, **kwargs: Any):
            return _fake_result(
                cypher="",
                explanation="NL → Cypher validation failed after 3 attempts...",
                confidence=0.0,
                method="validation_failed",
                retries=2,
            )

        from arango_cypher import nl2cypher as nl_module

        monkeypatch.setattr(nl_module, "nl_to_cypher", _fake_nl_to_cypher)

        resp = client.post(
            "/nl2cypher",
            json={
                "question": "find people",
                "mapping": {"conceptualSchema": {}, "physicalMapping": {}},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["method"] == "validation_failed"
        assert body["cypher"] == ""
        assert body["confidence"] == 0.0
        assert "validation failed" in body["explanation"]

    def test_session_token_is_optional_for_retry_context_flow(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """WP-30 regenerate-with-hint must work without a session
        (entity resolution is just disabled; retry_context still
        threads through)."""
        captured: dict[str, Any] = {}

        def _fake_nl_to_cypher(*args: Any, **kwargs: Any):
            captured.update(kwargs)
            return _fake_result()

        from arango_cypher import nl2cypher as nl_module

        monkeypatch.setattr(nl_module, "nl_to_cypher", _fake_nl_to_cypher)

        resp = client.post(
            "/nl2cypher",
            json={
                "question": "find people",
                "mapping": {"conceptualSchema": {}, "physicalMapping": {}},
                "retry_context": "translate error: unknown collection X",
            },
        )
        assert resp.status_code == 200
        assert captured["retry_context"] == ("translate error: unknown collection X")
        assert captured.get("db") is None


class TestIndexAdvisoriesPassThrough:
    """WP-S3c: the NL entity resolver's index advisories must survive the
    ``nl_to_cypher`` → endpoint → JSON hop so the UI can offer one-click
    index creation."""

    def test_advisories_included_in_response(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        advisory = {
            "collection": "Node",
            "field": "name",
            "reason": "fuzzy name matching falls back to a full collection scan",
            "suggestedIndex": {
                "type": "inverted",
                "name": "idx_fuzzy_name",
                "fields": [{"name": "name", "analyzer": "text_en"}],
            },
        }

        def _fake_nl_to_cypher(*args: Any, **kwargs: Any):
            return _fake_result(advisories=[advisory])

        from arango_cypher import nl2cypher as nl_module

        monkeypatch.setattr(nl_module, "nl_to_cypher", _fake_nl_to_cypher)

        resp = client.post(
            "/nl2cypher",
            json={
                "question": "companies cincinnati financial cinf has a stake in",
                "mapping": {"conceptualSchema": {}, "physicalMapping": {}},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["advisories"] == [advisory]

    def test_no_advisories_yields_empty_list(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A result without advisories (or a mock lacking the attr) must
        produce ``advisories: []``, never a 500 or a missing key."""

        def _fake_nl_to_cypher(*args: Any, **kwargs: Any):
            return _fake_result()  # SimpleNamespace without `advisories`

        from arango_cypher import nl2cypher as nl_module

        monkeypatch.setattr(nl_module, "nl_to_cypher", _fake_nl_to_cypher)

        resp = client.post(
            "/nl2cypher",
            json={
                "question": "find people",
                "mapping": {"conceptualSchema": {}, "physicalMapping": {}},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["advisories"] == []


# Make the `service` import a used symbol for ruff (keeps the module
# reference explicit so future assertions on module state are easy).
_ = service
