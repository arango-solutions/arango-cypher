"""Tests pinning the ``Field(max_length=...)`` + ``@field_validator``
constraints added to every user-facing request model in
``arango_cypher/service.py``.

Closes audit-v2 finding #5 — until this batch every ``BaseModel`` had
type-only validation, so a single 10 MB POST body could wedge the ANTLR
parser thread on ``/translate``, push novel-length prompts at an LLM
on ``/nl2cypher``, or balloon the corrections SQLite store via
``/corrections``. The constants the limits are derived from live as
``_MAX_*`` module-level attributes so a deployment that needs to raise
one can monkeypatch and rebuild the affected model.

Each test exercises both the **happy path** (a payload at the limit
succeeds) and the **rejection path** (one byte over → 422 with a
``string_too_long`` Pydantic error type). The URL-shape validator on
``ConnectRequest.url`` gets its own case (the only validator we added
that isn't a length bound).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arango_cypher import service as svc


@pytest.fixture
def client() -> TestClient:
    return TestClient(svc.app)


def _has_too_long(detail: list[dict] | str) -> bool:
    """True iff *any* error in the 422 detail says ``string_too_long``.

    FastAPI's validation-error handler runs through
    ``_sanitize_pydantic_errors`` on this codebase, which preserves
    ``type`` but redacts ``input``. We assert against ``type`` because
    it's the deterministic Pydantic error tag — the message text can
    change across Pydantic patch releases.
    """
    if isinstance(detail, list):
        return any(d.get("type") == "string_too_long" for d in detail)
    return "string_too_long" in str(detail)


# --------------------------------------------------------------------------- #
# /translate — TranslateRequest.cypher                                        #
# --------------------------------------------------------------------------- #


class TestTranslateRequestCypherLength:
    def test_cypher_at_limit_passes_validation(self, client: TestClient) -> None:
        cypher = "RETURN 1 // " + "a" * (svc._MAX_CYPHER_LENGTH - len("RETURN 1 // "))
        assert len(cypher) == svc._MAX_CYPHER_LENGTH
        resp = client.post("/translate", json={"cypher": cypher})
        # The payload may or may not transpile — but it must not 422.
        assert resp.status_code != 422, f"At-limit payload tripped validator: {resp.text[:200]}"

    def test_cypher_over_limit_returns_422(self, client: TestClient) -> None:
        cypher = "a" * (svc._MAX_CYPHER_LENGTH + 1)
        resp = client.post("/translate", json={"cypher": cypher})
        assert resp.status_code == 422
        assert _has_too_long(resp.json()["detail"])


# --------------------------------------------------------------------------- #
# /execute-aql — ExecuteAqlRequest.aql                                        #
# --------------------------------------------------------------------------- #


class TestExecuteAqlRequestLength:
    def test_aql_over_limit_rejected_by_model(self) -> None:
        # ``/execute-aql`` is gated by an unconditional session dep
        # (``_get_session``), which FastAPI resolves before body
        # validation — so an unauthenticated TestClient call returns
        # 401 before the validator gets a turn. We assert the
        # contract at the model boundary directly: any
        # ``ExecuteAqlRequest`` constructed with an oversize body must
        # raise a Pydantic ``ValidationError`` of type
        # ``string_too_long``. That's the same code path FastAPI hits
        # once the session dep is satisfied.
        from pydantic import ValidationError

        aql = "x" * (svc._MAX_AQL_LENGTH + 1)
        with pytest.raises(ValidationError) as ei:
            svc.ExecuteAqlRequest(aql=aql)
        assert any(e["type"] == "string_too_long" for e in ei.value.errors())

    def test_aql_at_limit_passes_model(self) -> None:
        aql = "x" * svc._MAX_AQL_LENGTH
        # Should not raise.
        req = svc.ExecuteAqlRequest(aql=aql)
        assert len(req.aql) == svc._MAX_AQL_LENGTH


# --------------------------------------------------------------------------- #
# /nl2cypher — NL2CypherRequest.question + retry_context                      #
# --------------------------------------------------------------------------- #


class TestNL2CypherRequestLengths:
    def test_question_over_limit_returns_422(self, client: TestClient) -> None:
        question = "q" * (svc._MAX_NL_QUESTION_LENGTH + 1)
        resp = client.post("/nl2cypher", json={"question": question})
        assert resp.status_code == 422
        assert _has_too_long(resp.json()["detail"])

    def test_retry_context_over_limit_returns_422(self, client: TestClient) -> None:
        body = {
            "question": "find all people",
            "retry_context": "x" * (svc._MAX_RETRY_HINT_LENGTH + 1),
        }
        resp = client.post("/nl2cypher", json=body)
        assert resp.status_code == 422
        assert _has_too_long(resp.json()["detail"])


# --------------------------------------------------------------------------- #
# /corrections — CorrectionRequest.{cypher,original_aql,corrected_aql,note}   #
# --------------------------------------------------------------------------- #


class TestCorrectionRequestLengths:
    def _payload(self, **overrides):
        body = {
            "cypher": "MATCH (n) RETURN n",
            "original_aql": "FOR x IN c RETURN x",
            "corrected_aql": "FOR y IN c RETURN y",
            "note": "ok",
        }
        body.update(overrides)
        return body

    def test_oversize_note_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/corrections",
            json=self._payload(note="n" * (svc._MAX_NOTE_LENGTH + 1)),
        )
        assert resp.status_code == 422
        assert _has_too_long(resp.json()["detail"])

    def test_oversize_corrected_aql_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/corrections",
            json=self._payload(corrected_aql="x" * (svc._MAX_AQL_LENGTH + 1)),
        )
        assert resp.status_code == 422
        assert _has_too_long(resp.json()["detail"])


# --------------------------------------------------------------------------- #
# /connect — ConnectRequest.url shape validator                               #
# --------------------------------------------------------------------------- #


class TestConnectRequestUrlValidator:
    def test_malformed_url_returns_422(self, client: TestClient) -> None:
        # Missing scheme — caught at validation time so the SSRF guard
        # at /connect doesn't have to spin up the connect machinery.
        resp = client.post("/connect", json={"url": "not-a-url-at-all"})
        assert resp.status_code == 422
        # type tag is value_error from the @field_validator raise-branch.
        detail = resp.json()["detail"]
        assert any(d.get("type") == "value_error" for d in detail), detail

    def test_oversize_url_returns_422(self, client: TestClient) -> None:
        # Length validator fires before the shape validator on a too-long
        # *valid-shape* URL, so this asserts the field's max_length too.
        resp = client.post(
            "/connect",
            json={"url": "http://" + ("a" * svc._MAX_FIELD_LENGTH) + ".example"},
        )
        assert resp.status_code == 422
        assert _has_too_long(resp.json()["detail"])

    def test_https_url_passes_validator(self, client: TestClient) -> None:
        # Sanity: a well-formed https URL passes the validator. The
        # request still fails at connect-time (no live ArangoDB on the
        # bogus host) but the failure is no longer a 422 — meaning the
        # validator accepted the input.
        resp = client.post(
            "/connect",
            json={"url": "https://example.invalid:8529"},
        )
        assert resp.status_code != 422

    def test_empty_url_passes_validator(self, client: TestClient) -> None:
        # Empty string is the model default; the validator must accept
        # it so a connect call can rely on the default URL without
        # tripping its own validator on a missing field.
        resp = client.post("/connect", json={"url": ""})
        # Same as the https case — fails downstream, not at validation.
        assert resp.status_code != 422
