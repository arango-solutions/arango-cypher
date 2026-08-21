"""Caller-supplied postconditions for the NL → Cypher retry loop.

The pipeline already validates generated Cypher three ways before running it:
ANTLR parse, ``EXPLAIN`` against the database, and — when a tenant context is
active — :func:`~arango_cypher.nl2cypher.tenant_guardrail.check_tenant_scope`.
The first two catch *broken* Cypher. The third catches something more dangerous:
Cypher that parses, plans and returns plausible rows while being semantically
wrong, in that specific case by escaping tenant isolation.

Every domain has its own version of that failure and, before this module, only
tenancy could be defended against. A postcondition lets a caller add their own
invariant to the same retry-and-fail-closed machinery:

    class TimeWindowed:
        code = "time_windowed"

        def prompt_section(self) -> str:
            return "## Aggregates\\nAggregate queries MUST bind a time window.\\n"

        def check(self, cypher, *, context):
            if _aggregates(cypher) and not _binds_window(cypher):
                return PostconditionViolation(
                    code=self.code,
                    reason="The query aggregates without a time window, so it "
                           "would scan the full history.",
                    suggested_hint="Add a WHERE clause bounding the event date.",
                )
            return None

    nl_to_cypher(question, mapping=bundle, postconditions=[TimeWindowed()])

Semantics, all inherited from the existing loop rather than invented here:

* Postconditions run **after** parse and EXPLAIN succeed, so a retry is only
  spent on Cypher that is already syntactically and physically valid.
* The tenant guardrail runs **first**. A statement that violates both reports
  the tenant violation, because a cross-tenant leak is a security failure and a
  domain-correctness problem is not.
* Violations share the caller's ``max_retries`` budget with parse and EXPLAIN
  failures. They do not get their own allowance.
* The **first** violation wins; checks are not accumulated. One clear
  instruction retries better than three at once.
* On budget exhaustion the loop fails closed. A statement that never satisfies
  a postcondition is never returned.
* :meth:`Postcondition.prompt_section` is rendered **once**, into the cacheable
  system prefix. It must not vary between attempts of the same call — only
  ``retry_context`` mutates, which is what keeps the prefix byte-stable for
  provider-side prompt caching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from arango_cypher.catalog.registry import MappingBundle

__all__ = [
    "Postcondition",
    "PostconditionContext",
    "PostconditionViolation",
    "run_postconditions",
]


@dataclass(frozen=True)
class PostconditionViolation:
    """Why an otherwise-valid Cypher statement was rejected.

    Structurally compatible with
    :class:`~arango_cypher.nl2cypher.tenant_guardrail.TenantScopeViolation` —
    both expose ``reason``, ``suggested_hint`` and ``code``, which is all the
    retry loop reads. Deliberately *not* a base class of it: two unrelated types
    named ``TenantScopeViolation`` exist in this package (a dataclass in
    ``tenant_guardrail`` and an exception in ``tenant_plan_validator``), and
    dataclass inheritance would also reorder the existing fields.
    """

    reason: str
    """What is wrong. Fed verbatim into the retry prompt."""

    suggested_hint: str
    """How to fix it. Also fed into the retry prompt, after ``reason``."""

    code: str = "postcondition_violation"
    """Stable identifier for logs, tests and result metadata."""


@dataclass(frozen=True)
class PostconditionContext:
    """What a check is given besides the Cypher itself.

    Passed as a single object so a future field does not change every
    implementer's signature.
    """

    schema_summary: str
    question: str
    attempt: int
    mapping: "MappingBundle | None" = None


@runtime_checkable
class Postcondition(Protocol):
    """A caller-supplied check on generated Cypher."""

    code: str
    """Stable identifier, e.g. ``"analog_conditional"``."""

    def check(
        self,
        cypher: str,
        *,
        context: PostconditionContext,
    ) -> PostconditionViolation | None:
        """Return ``None`` to accept, or a violation to trigger a retry."""
        ...

    def prompt_section(self) -> str:
        """Text injected into the system prompt so the model is told the rule
        up front rather than only corrected after breaking it.

        Return ``""`` to contribute nothing. Rendered once per call — must not
        vary between attempts, or provider-side prompt caching is defeated.
        """
        ...


def run_postconditions(
    cypher: str,
    postconditions: Sequence[Postcondition] | None,
    context: PostconditionContext,
) -> PostconditionViolation | None:
    """Run *postconditions* in order, returning the first violation.

    A check that raises is treated as a violation rather than being allowed to
    abort the translation: a broken check must not be able to turn a safety
    mechanism into an outage, and failing closed is the conservative reading.
    """
    if not postconditions:
        return None

    for pc in postconditions:
        code = getattr(pc, "code", pc.__class__.__name__)
        try:
            violation = pc.check(cypher, context=context)
        except Exception as exc:  # noqa: BLE001 - see docstring
            return PostconditionViolation(
                code=f"{code}_error",
                reason=f"Postcondition {code!r} raised {type(exc).__name__}: {exc}",
                suggested_hint="Simplify the query; the check could not evaluate it.",
            )
        if violation is not None:
            return violation
    return None


def postcondition_prompt_sections(
    postconditions: Sequence[Postcondition] | None,
) -> list[str]:
    """Collect non-empty ``prompt_section()`` output, in caller order.

    A check that raises here is skipped rather than failing the call — losing an
    advisory prompt block degrades quality, while ``check()`` still enforces the
    invariant.
    """
    if not postconditions:
        return []

    sections: list[str] = []
    for pc in postconditions:
        section: Any
        try:
            section = pc.prompt_section()
        except Exception:  # noqa: BLE001 - advisory only, see docstring
            continue
        if isinstance(section, str) and section.strip():
            sections.append(section.strip())
    return sections
