"""Pydantic request / response models + ``_MAX_*`` length constants.

Every model in this module is re-exported from
:mod:`arango_cypher.service` so the historical
``from arango_cypher.service import TranslateRequest`` style imports
keep working across the audit-v2 #8 split. The route modules under
:mod:`arango_cypher.service.routes` import them via the package init
to keep cross-module dependencies one-directional (routes depend on
models, never the other way round).

The ``_MAX_*`` constants live as module-level attributes (intentionally
underscore-prefixed) so an operator who needs to raise one for a
specific deployment can grep, monkeypatch, and rebuild the affected
model. Closes audit-v2 finding #5 — see that audit's section 5
close-out for the per-field rationale.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

# Stricter-than-type field bounds for the user-controlled string fields on
# every request model below. Closes audit-v2 finding #5 — without these,
# a single 10 MB POST body can wedge the ANTLR parser thread, push novel-
# length prompts at an LLM, or balloon the corrections SQLite store.
# These constants are intentionally generous (a real Cypher query is
# almost never above ~10 KB) so they bound an attack rather than restrict
# normal use, and they live as module-level constants so an operator who
# needs to raise one for a specific deployment can grep + monkeypatch.
_MAX_CYPHER_LENGTH = 100_000  # ~100 KB; a real interactive query is < 10 KB
_MAX_AQL_LENGTH = 100_000  # raw AQL on /execute-aql; same envelope
_MAX_NL_QUESTION_LENGTH = 4_000  # bounds LLM context-window cost
_MAX_RETRY_HINT_LENGTH = 8_000  # WP-29 retry context (parser/EXPLAIN error blob)
_MAX_TURTLE_LENGTH = 1_000_000  # OWL ontologies can be sizeable; 1 MB cap
_MAX_NOTE_LENGTH = 4_000  # correction notes — keep them human-readable
_MAX_FIELD_LENGTH = 256  # urls, usernames, db names, tool names, tenant fields


class ConnectRequest(BaseModel):
    url: str = Field(default="http://localhost:8529", max_length=_MAX_FIELD_LENGTH)
    database: str = Field(default="_system", max_length=_MAX_FIELD_LENGTH)
    username: str = Field(default="root", max_length=_MAX_FIELD_LENGTH)
    password: str = Field(default="", max_length=_MAX_FIELD_LENGTH)

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: str) -> str:
        # Defensive shape-check before the SSRF guard at /connect runs.
        # The /connect endpoint already rejects bad targets at runtime via
        # the SSRF allowlist (PR #7); this validator catches the cheaper,
        # more obviously-broken cases at request-validation time so the
        # caller gets a 422 with a clear "url is malformed" hint instead
        # of a deeper 4xx/5xx after the connect machinery has spun up.
        if not v:
            return v
        lowered = v.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class ConnectResponse(BaseModel):
    token: str
    databases: list[str]


class TranslateRequest(BaseModel):
    cypher: str = Field(..., max_length=_MAX_CYPHER_LENGTH)
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    extensions_enabled: bool = True


class TranslateResponse(BaseModel):
    aql: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]]
    elapsed_ms: float | None = None


class ExecuteRequest(BaseModel):
    cypher: str = Field(..., max_length=_MAX_CYPHER_LENGTH)
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    extensions_enabled: bool = True


class ExecuteResponse(BaseModel):
    results: list[Any]
    aql: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]] = []
    exec_ms: float | None = None
    # Wall-clock time spent in the Cypher → AQL transpiler on this
    # request. Surfaced separately from `exec_ms` so the UI can show
    # both badges side-by-side after a Run; otherwise users lose
    # visibility into translation cost the moment they execute.
    translate_ms: float | None = None


class ValidateRequest(BaseModel):
    cypher: str = Field(..., max_length=_MAX_CYPHER_LENGTH)
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class ValidateResponse(BaseModel):
    ok: bool
    errors: list[dict[str, str]]


class ErrorResponse(BaseModel):
    error: str
    code: str


class ExecuteAqlRequest(BaseModel):
    aql: str = Field(..., max_length=_MAX_AQL_LENGTH)
    bind_vars: dict[str, Any] = Field(default_factory=dict)


class ToolCallRequest(BaseModel):
    name: str = Field(..., max_length=_MAX_FIELD_LENGTH)
    arguments: dict[str, Any] = {}


class SuggestIndexesRequest(BaseModel):
    mapping: dict[str, Any]


class TenantContextPayload(BaseModel):
    """Ambient tenant scope applied to NL translations in a session.

    Mirrors :class:`arango_cypher.nl2cypher.tenant_guardrail.TenantContext`
    on the wire. See ``/tenants`` for how the UI sources this.
    """

    property: str = Field(
        ...,
        max_length=_MAX_FIELD_LENGTH,
        description=(
            "Physical property name on the Tenant entity (e.g. 'TENANT_HEX_ID', 'NAME', 'SUBDOMAIN')."
        ),
    )
    value: str = Field(..., max_length=_MAX_FIELD_LENGTH, description="Exact value to match.")
    display: str | None = Field(
        default=None,
        max_length=_MAX_FIELD_LENGTH,
        description="Optional human-readable label for prompts / UI.",
    )


class NL2CypherRequest(BaseModel):
    question: str = Field(..., max_length=_MAX_NL_QUESTION_LENGTH)
    mapping: dict[str, Any] | None = None
    use_llm: bool = True
    use_fewshot: bool = True
    use_entity_resolution: bool = True
    session_token: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)
    tenant_context: TenantContextPayload | None = None
    # WP-29 Part 4: WP-30 hand-off contract. When supplied, the NL
    # retry loop seeds ``PromptBuilder.retry_context`` on the very
    # first attempt with the caller-provided hint (typically the
    # parse / EXPLAIN error from a prior translate). WP-30 wires
    # this from the UI's "Regenerate from NL with error hint"
    # button; without a caller it stays ``None`` and the prompt is
    # byte-identical to the pre-WP-29 shape for zero-shot bare-name
    # schemas.
    retry_context: str | None = Field(default=None, max_length=_MAX_RETRY_HINT_LENGTH)


class NLSuggestRequest(BaseModel):
    mapping: dict[str, Any] | None = None
    count: int = Field(default=8, ge=1, le=20)
    use_llm: bool = True


class NL2AqlRequest(BaseModel):
    question: str = Field(..., max_length=_MAX_NL_QUESTION_LENGTH)
    mapping: dict[str, Any] | None = None
    tenant_context: TenantContextPayload | None = None


class OwlExportRequest(BaseModel):
    mapping: dict[str, Any]


class OwlImportRequest(BaseModel):
    turtle: str = Field(..., max_length=_MAX_TURTLE_LENGTH)


class CorrectionRequest(BaseModel):
    cypher: str = Field(..., max_length=_MAX_CYPHER_LENGTH)
    mapping: dict[str, Any] = Field(default_factory=dict)
    database: str = Field(default="", max_length=_MAX_FIELD_LENGTH)
    original_aql: str = Field(..., max_length=_MAX_AQL_LENGTH)
    corrected_aql: str = Field(..., max_length=_MAX_AQL_LENGTH)
    bind_vars: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=_MAX_NOTE_LENGTH)


class NLCorrectionRequest(BaseModel):
    question: str = Field(..., max_length=_MAX_NL_QUESTION_LENGTH)
    cypher: str = Field(..., max_length=_MAX_CYPHER_LENGTH)
    mapping: dict[str, Any] = Field(default_factory=dict)
    database: str = Field(default="", max_length=_MAX_FIELD_LENGTH)
    note: str = Field(default="", max_length=_MAX_NOTE_LENGTH)
