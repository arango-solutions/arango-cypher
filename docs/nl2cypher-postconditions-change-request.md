# Change request — caller-supplied postconditions in the NL→Cypher retry loop

**Raised by:** gdelt-market-impact (Domyn POC) · 2026-08-21
**Affects:** `arango_cypher.nl2cypher`
**Status:** ✅ **implemented** 2026-08-21 — see `arango_cypher/nl2cypher/postconditions.py`
and `tests/test_nl2cypher_postconditions.py` (10 tests). Full suite: 1,804 passed, the
two failures pre-existing and unrelated (an opt-in live-API smoke test, and a stale
`ui/dist` build).
**Backwards compatible:** yes — no existing type or signature changed behaviour.

> **One deviation from §3.1 below, and the original was wrong.** This document
> proposed that `TenantScopeViolation` become a subclass of the new violation type.
> It should not, for two reasons found during implementation: there are **two
> unrelated classes with that name** — a dataclass in `tenant_guardrail.py` and an
> *exception* in `tenant_plan_validator.py`, constructed with different arguments —
> and dataclass inheritance would have reordered the existing fields. Instead
> `PostconditionViolation` is standalone and **structurally** compatible: both expose
> `reason`, `suggested_hint` and `code`, which is all the retry loop reads. No
> existing type was touched.
>
> §6 (`nl_to_aql`) was resolved to the loud option: it raises `ValueError` pointing
> at `nl_to_cypher` rather than silently ignoring the argument.

---

## 1. The ask

Let a caller pass their own postcondition into `nl_to_cypher()`, so that
domain-specific "this query is valid but semantically wrong" checks can use the
retry-and-fail-closed machinery that already exists for tenant scoping.

```python
nl_to_cypher(question, mapping=..., postconditions=[my_check])
```

Nothing about the tenant guardrail changes. This generalises the *mechanism* it
already proves out.

---

## 2. Why

`_call_llm_with_retry` already implements a genuinely valuable pattern: generate,
validate, feed the failure back into the next prompt, and **fail closed** when the
budget is exhausted. It currently runs three validations:

1. ANTLR parse
2. `EXPLAIN` against the database (WP-25.3)
3. `check_tenant_scope()` — the tenant-binding postcondition

The first two are universal. The third is the interesting one, because it catches a
different class of failure: **Cypher that parses, executes, and returns plausible
rows while being wrong.** For a multi-tenant workspace that means a cross-tenant
leak. Every domain has its own version of that failure, and right now only tenancy
can be defended against.

### The second instance, concretely

In the GDELT causal-impact graph, a question like *"what if the US sanctions China
again?"* can produce:

```cypher
MATCH (e:Event)-[:taggedWith]->(t:Theme)-[:correlatesWith]->(s:Sector)
RETURN s.name, avg(...)
```

That parses, EXPLAINs cleanly, and returns eleven sectors with credible numbers.
It is also **wrong**: it reads pre-computed *theme-level* statistics rather than
computing the outcome over the analog events the question actually retrieved. The
consequence is that the answer is identical for every hypothetical, and the events
cited as evidence are not the events the numbers came from.

No existing check catches it, and no human catches it in a demo either — the output
looks right. It is exactly the shape the tenant guardrail was built for, pointed at
a different invariant.

Other plausible instances: "aggregate queries must carry a time window", "any
traversal of a supernode collection must be bounded", "PII collections must not
appear in a projection".

---

## 3. Proposed API

### 3.1 The violation type

Generalise the existing shape. `TenantScopeViolation` already carries exactly the
right fields — `reason`, `suggested_hint`, `code` — so lift a base out of it:

```python
@dataclass(frozen=True)
class PostconditionViolation:
    """Why an otherwise-valid Cypher statement was rejected."""
    reason: str           # what is wrong — goes into the retry prompt
    suggested_hint: str   # how to fix it — also goes into the retry prompt
    code: str             # stable identifier for logging and tests
```

`TenantScopeViolation` becomes a subclass, keeping `tenant_property`,
`tenant_value` and `physical_enforcement`. No caller of it changes.

### 3.2 The protocol

```python
class Postcondition(Protocol):
    """A caller-supplied check on generated Cypher.

    Runs after parse and EXPLAIN succeed, so a retry is only spent on Cypher
    that is already syntactically and physically valid.
    """

    code: str
    """Stable identifier, e.g. "analog_conditional". Used in logs and results."""

    def check(self, cypher: str, *, context: PostconditionContext) -> PostconditionViolation | None:
        """Return None to accept, or a violation to trigger a retry."""

    def prompt_section(self) -> str:
        """Optional. Text injected into the system prompt so the model is told
        the rule up front rather than only being corrected after breaking it.
        Return "" to contribute nothing."""
```

`PostconditionContext` gives the check what it needs without widening the
signature later:

```python
@dataclass(frozen=True)
class PostconditionContext:
    mapping: MappingBundle | None
    schema_summary: str
    question: str
    attempt: int
```

### 3.3 Entry points

Add `postconditions: Sequence[Postcondition] | None = None` to:

- `nl_to_cypher()` — `_core.py:1561`
- `_call_llm_with_retry()` — `_core.py:767`
- `nl_to_aql()` — `_aql.py:664` — see §6

Default `None`, meaning "none beyond the built-ins". Existing callers are
unaffected.

---

## 4. Where it hooks in

`_core.py`, the block at **line 855–885**. Today:

```python
if explain_ok:
    violation = check_tenant_scope(cypher, tenant_context=..., ...)
    if violation is None:
        return NL2CypherResult(...)
    best_cypher = cypher
    builder.retry_context = f"{violation.reason} {violation.suggested_hint}"
    logger.warning("Tenant-scoping violation (attempt %d/%d): %s", ...)
    continue
```

Proposed — the tenant check keeps its position, caller postconditions run after it,
first violation wins:

```python
if explain_ok:
    violation = check_tenant_scope(cypher, tenant_context=..., ...)

    if violation is None and postconditions:
        ctx = PostconditionContext(
            mapping=mapping, schema_summary=schema_summary,
            question=question, attempt=attempt,
        )
        for pc in postconditions:
            violation = pc.check(cypher, context=ctx)
            if violation is not None:
                break

    if violation is None:
        return NL2CypherResult(...)

    best_cypher = cypher
    builder.retry_context = f"{violation.reason} {violation.suggested_hint}"
    logger.warning(
        "Postcondition violation [%s] (attempt %d/%d): %s",
        violation.code, attempt + 1, 1 + max_retries, violation.reason,
    )
    continue
```

Tenant first is deliberate: a cross-tenant leak is a security failure and should be
reported ahead of a domain-correctness one when a statement violates both.

`PromptBuilder` (`_core.py:543`) gains the optional prompt sections. It already
composes a tenant block via `prompt_section()`, so this follows the same path.

---

## 5. Constraints the implementation must preserve

These are properties of the current design that are easy to break.

**Prompt-cache stability.** The docstring at `_core.py:543` is explicit: *"only
`retry_context` mutates between iterations so the (cacheable) system prefix stays
byte-stable."* Postcondition `prompt_section()` output is therefore rendered
**once** into the system prefix at builder construction — it varies by caller
config but must not vary between attempts of the same call. Violation feedback goes
through `retry_context` (the user message) only.

**Shared retry budget.** `max_retries` is shared across failure kinds today — a
query failing ANTLR on attempt 1 and EXPLAIN on attempt 2 gets one more try.
Postcondition failures join the same budget. **Do not** give them a separate
allowance.

**Run after EXPLAIN, not before.** The existing comment says it: only burn a retry
on Cypher that is already semantically valid. Postconditions are about meaning, and
checking meaning on unparseable Cypher wastes the budget.

**Fail closed.** On exhaustion the existing path returns the fail-closed result
rather than the best attempt. Postcondition exhaustion must behave identically —
a query that never satisfies the caller's invariant must not be handed back.

**First violation wins.** Do not accumulate. One clear instruction retries better
than three at once, and it matches how parse and EXPLAIN errors already work.

---

## 6. `nl_to_aql()`

`nl_to_aql()` (`_aql.py:664`) is a separate path that generates AQL directly and
deliberately exposes physical mapping. It has no Cypher to check, so a
Cypher-shaped postcondition cannot apply.

**Recommended:** add the parameter and pass it through **only** on the branch where
`cypher=` is supplied or where it delegates to the Cypher path. On the direct-AQL
branch, either skip postconditions or raise if any are passed, rather than silently
ignoring them. Silently ignoring a safety check is the worst option.

Please confirm which — the caller (gdelt-market-impact) uses the Cypher path, so
this is not blocking for us.

---

## 7. Backwards compatibility

- New keyword argument, defaulted to `None`
- No change to any existing signature's positional order
- `TenantScopeViolation` keeps its fields and gains a base class
- `check_tenant_scope()` unchanged
- With `postconditions=None` the loop is behaviourally identical

---

## 8. Tests worth having

1. **No postconditions** — behaviour identical to today (regression guard)
2. **Passing postcondition** — result returned, `retries` unchanged
3. **Failing then passing** — violation feeds `retry_context`, second attempt
   succeeds, `retries == 1`
4. **Never passing** — fail-closed after `max_retries`, no query returned
5. **Budget is shared** — one ANTLR failure plus one postcondition failure with
   `max_retries=2` leaves exactly one attempt
6. **Ordering** — a statement violating both tenant scope and a caller
   postcondition reports the tenant violation
7. **Cache stability** — the rendered system prefix is byte-identical across
   attempts when a postcondition supplies a `prompt_section()`

---

## 9. Optional, not requested

`check_tenant_scope` could be refactored into a built-in `Postcondition` instance,
making tenancy one entry in a list rather than a special case. Cleaner, and it
would let callers reorder or disable it — which is also the argument against, since
tenant scoping should not be disable-able by accident.

**Recommendation: don't**, at least not in this change. Ship the extension point,
leave tenancy hard-wired and privileged.

---

## 10. What the requesting project will do with it

For reference, the postcondition we intend to supply:

```python
class AnalogConditional:
    """The outcome must be aggregated over retrieved analog events, not over
    pre-computed theme-level statistics."""

    code = "analog_conditional"

    def prompt_section(self) -> str:
        return (
            "## Outcome computation\n"
            "Sector outcomes MUST be aggregated over the Event nodes retrieved as "
            "analogs. Do NOT read the pre-computed `correlatesWith` relationship — "
            "it is a theme-level summary and is identical for every question.\n"
        )

    def check(self, cypher, *, context):
        if "correlatesWith" in cypher and "Event" not in _aggregation_scope(cypher):
            return PostconditionViolation(
                code=self.code,
                reason=("The query aggregates over theme-level `correlatesWith` "
                        "statistics rather than the retrieved analog events, so the "
                        "answer would be identical for any question."),
                suggested_hint=("Aggregate over the Event nodes matched as analogs and "
                                "compute the outcome from PriceSeries at those dates."),
            )
        return None
```

That is roughly 25 lines living in *our* repo — no GDELT vocabulary enters this
library, which is the point of making the hook generic rather than adding a second
built-in check.

---

## 11. Contact

Raised from `gdelt-market-impact`. The blocking question for us is §6 (`nl_to_aql`
behaviour) and the release timeline — if shipping a new version is slow, we will
run the check outside the loop as a stopgap and switch over when this lands.
