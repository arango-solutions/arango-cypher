"""Caller-supplied postconditions for the NL → Cypher retry loop.

The **mechanism** lives in the shared NL engine
(:mod:`arango_query_core.nl.postconditions`) so ``nl2cypher`` and ``nl2sparql``
cannot re-diverge; this module re-exports it under the Cypher-facing name that
``_core`` / ``_aql`` and callers already import, and is the home for
Cypher-specific checks built on top of it.

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

        def check(self, query, *, context):
            if _aggregates(query) and not _binds_window(query):
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

``PostconditionContext.mapping`` is typed ``Any`` upstream rather than
``MappingBundle | None``: the shared engine does not depend on any one repo's
schema-bundle type. That is strictly wider, so passing a ``MappingBundle`` and
reading ``context.mapping.<field>`` in a check is unaffected.
"""

from __future__ import annotations

from arango_query_core.nl import (
    Postcondition,
    PostconditionContext,
    PostconditionViolation,
    postcondition_prompt_sections,
    run_postconditions,
)

__all__ = [
    "Postcondition",
    "PostconditionContext",
    "PostconditionViolation",
    "postcondition_prompt_sections",
    "run_postconditions",
]
