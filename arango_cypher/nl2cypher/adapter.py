"""Cypher implementation of the shared NL-engine's language seams.

:class:`CypherAdapter` is this package's answer to
:class:`arango_query_core.nl.seams.QueryLanguageAdapter` — the five
questions the language-agnostic engine
(:class:`arango_query_core.nl.NLQueryEngine`) cannot answer without
committing to a target language. Every answer delegates to machinery
that already exists in :mod:`arango_cypher.nl2cypher._core` and the
tenant-guardrail modules; this file moves it behind the standard
interface, it does not add logic.

Adapters live next to their transpiler (not in ``arango-query-core``)
because seam 3 needs the ANTLR parser and, when execution-grounded,
the Cypher→AQL translator — pulling either into the core package would
invert the dependency direction the extraction established.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from arango_query_core.mapping import MappingBundle
from arango_query_core.nl.fewshot import FewShotIndex
from arango_query_core.nl.seams import GuardrailVerdict, ValidationResult

if TYPE_CHECKING:
    # Seams 6–7 (grounding / predicate) were added to
    # QueryLanguageAdapter after arango-query-core 0.1.0. These types
    # only exist in that later engine; import them under TYPE_CHECKING
    # so this module still loads against the published 0.1.0 (which has
    # neither the types nor the 9-seam protocol) — the annotations stay
    # lazy strings via ``from __future__ import annotations`` and the
    # seam bodies never touch the types at runtime.
    from arango_query_core.nl.grounding import LabelIndex, PredicateIndex

from ._core import (
    _SYSTEM_PROMPT,
    _augment_explain_hint,
    _get_default_fewshot_index,
    _validate_cypher,
    _validate_via_explain,
)
from .tenant_guardrail import (
    check_tenant_scope,
    multitenancy_physical_enforcement,
)


@dataclass
class CypherAdapter:
    """The five Cypher-specific seams, for :class:`~arango_query_core.nl.NLQueryEngine`.

    ``mapping`` and ``db`` are optional collaborators mirroring
    :func:`~arango_cypher.nl2cypher.nl_to_cypher`'s signature: with both
    present, seam 3 upgrades from ANTLR-parse-only to execution-grounded
    validation (transpile to AQL + ``POST /_api/explain``, WP-25.3), and
    seam 5 can consult the analyzer's physical-enforcement
    classification. With neither, the adapter is fully offline.
    """

    mapping: MappingBundle | None = None
    db: Any | None = None

    language: ClassVar[str] = "cypher"

    def grammar_prompt_section(self, schema_context: str) -> str:
        """Seam 1 — the NL→Cypher system prompt over *schema_context*.

        Renders :data:`~arango_cypher.nl2cypher._core._SYSTEM_PROMPT`
        exactly as the zero-shot :class:`PromptBuilder` does, so prompts
        stay byte-identical between the legacy pipeline and the engine.
        """
        return _SYSTEM_PROMPT.replace("{schema}", schema_context)

    def few_shot_index(self) -> FewShotIndex | None:
        """Seam 2 — shipped ``corpora/*.yml`` plus approved NL corrections."""
        return _get_default_fewshot_index()

    def validate(self, query: str) -> ValidationResult:
        """Seam 3 — ANTLR parse, then (mapping+db permitting) EXPLAIN."""
        ok, err = _validate_cypher(query)
        if not ok:
            return ValidationResult(ok=False, error=err, code="E_CYPHER_PARSE")
        explain_ok, explain_err = _validate_via_explain(
            query,
            mapping=self.mapping,
            db=self.db,
        )
        if not explain_ok:
            return ValidationResult(ok=False, error=explain_err, code="E_CYPHER_EXPLAIN")
        return ValidationResult(ok=True)

    def repair_hint(self, query: str, failure: ValidationResult) -> str:
        """Seam 4 — augment known EXPLAIN errors with a targeted fix.

        Matches the legacy retry loop: the raw validator error plus the
        multiple-assignment hint when it applies; the empty string
        otherwise, which tells the engine to retry with the bare error.
        """
        hint = _augment_explain_hint(failure.error)
        if hint:
            return failure.error + hint
        return ""

    def guardrails(self, query: str, context: dict[str, Any]) -> GuardrailVerdict:
        """Seam 5 — tenant-scope enforcement (Wave 4r / MT-2).

        ``context`` carries ``tenant_context`` (:class:`TenantContext`)
        and optionally ``tenant_manifest`` (:class:`TenantScopeManifest`);
        without a tenant context there is no constraint to enforce.
        """
        tenant_context = context.get("tenant_context")
        if tenant_context is None:
            return GuardrailVerdict(allowed=True)
        physical_enforcement = (
            multitenancy_physical_enforcement(self.mapping) if self.mapping is not None else None
        )
        violation = check_tenant_scope(
            query,
            tenant_context=tenant_context,
            manifest=context.get("tenant_manifest"),
            physical_enforcement=physical_enforcement,
        )
        if violation is None:
            return GuardrailVerdict(allowed=True)
        reasons = [violation.reason]
        if violation.suggested_hint:
            reasons.append(violation.suggested_hint)
        return GuardrailVerdict(allowed=False, reasons=reasons)

    def grounding_index(self) -> LabelIndex | None:
        """Seam 6 — engine-level instance/label grounding: not used by Cypher.

        The engine's :class:`LabelIndex` grounding (SPARQL-style "use
        these EXACT IRIs" injection) is orthogonal to the Cypher path,
        which grounds user string literals through its own pre-flight
        :class:`~arango_cypher.nl2cypher.EntityResolver` (WP-25.2) inside
        ``nl_to_cypher`` rather than via a prompt block. Returning
        ``None`` runs the engine ungrounded and leaves that behavior
        untouched.
        """
        return None

    def grounding_prompt_section(self, question: str, index: LabelIndex, k: int = 20) -> str:
        """Seam 6 renderer — unreachable for Cypher.

        The engine only calls this when :meth:`grounding_index` returns a
        non-``None`` index; Cypher always returns ``None``, so this exists
        for protocol conformance and yields nothing if ever invoked.
        """
        return ""

    def predicate_index(self) -> PredicateIndex | None:
        """Seam 7 — engine-level TBox predicate grounding: not used by Cypher.

        Cypher's grammar prompt (seam 1) already carries the conceptual
        schema (labels, relationship types, properties), so there is no
        separate predicate cheat-sheet to inject. Returns ``None``.
        """
        return None

    def predicate_prompt_section(self, question: str, index: PredicateIndex, k: int = 20) -> str:
        """Seam 7 renderer — unreachable for Cypher (see :meth:`predicate_index`)."""
        return ""
