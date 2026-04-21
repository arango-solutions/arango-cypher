# `arango_cypher.nl2cypher`

NL → Cypher → AQL pipeline. The LLM generates **conceptual** Cypher against
the schema, and the transpiler converts it to AQL. This preserves the PRD
§1.2 invariant: the LLM never sees physical mapping details.

## Module layout

| File | Role |
|---|---|
| `_core.py` | Schema summarization, `PromptBuilder`, rule-based fallback, `nl_to_cypher()`, `suggest_nl_queries()` |
| `_aql.py` | Direct `nl_to_aql()` (exposes physical mapping — deliberately separate from the Cypher path) |
| `providers.py` | `LLMProvider` protocol + `OpenAIProvider` / `OpenRouterProvider` / `AnthropicProvider` (Messages API + native prompt caching) |
| `fewshot.py` | `FewShotIndex` + `BM25Retriever` (WP-25.1) |
| `entity_resolution.py` | `EntityResolver` for pre-flight entity resolution (WP-25.2) |
| `tenant_guardrail.py` | Wave 4r tenant-scoping postcondition — `TenantContext`, `check_tenant_scope()`, `prompt_section()`. Fires when a context is active and the emitted Cypher contains no `:Tenant` binding; translator fails closed after retry exhaustion. |
| `corpora/*.yml` | Seed corpora for the default few-shot index |

## Multi-tenant scoping (Wave 4r)

Callers pass a `TenantContext(property, value, display)` when the
workspace is multi-tenant. The NL pipeline then:

1. Injects a "## Current tenant scope" block into the system prompt
   between the schema and the few-shot section (so the LLM sees the
   scope before the examples).
2. After each LLM emission runs `check_tenant_scope(cypher, context)`;
   a violation (Cypher with no `:Tenant` binding) feeds a structured
   hint into the retry prompt.
3. If the retry budget is exhausted with the violation still in
   place, returns `NL2CypherResult(cypher="", method="tenant_guardrail_blocked", ...)` — **the translator never silently emits a cross-tenant query**.

The label check is anchored (`:Tenant\b(?!\w)`) so prefix-collision
labels like `:TenantUser`, `:TenantCVE`, `:TenantAppVersion` do **not**
satisfy the constraint — handling those is the whole point of the
guardrail.

Zero-shot rendering stays byte-identical to the pre-4r shape when
`tenant_context=None`, so single-tenant users pay zero prompt tokens
for this feature (pinned by
`tests/test_nl2cypher_tenant_guardrail.py::test_no_tenant_context_leaves_prompt_byte_identical`).

## Prompt composition (WP-25.4 prompt caching)

`PromptBuilder.render_system()` assembles the system prompt in this order:

1. **Prelude + rules** — tiny, static.
2. **Schema summary** — large, static *per mapping*. This is the cache target.
3. **Few-shot examples** (WP-25.1) — medium, varies per question.
4. **Resolved entities** (WP-25.2) — small, varies per question.

The schema-first layout lets provider-side prefix caching kick in:

- **OpenAI** caches input prefixes ≥ 1024 tokens automatically. The schema
  block is far larger than that for any nontrivial mapping, so subsequent
  calls against the same mapping pay only the per-question tail. The
  provider surfaces the cache hit via
  `usage.prompt_tokens_details.cached_tokens`, which we propagate to
  `NL2CypherResult.cached_tokens`.

- **Anthropic** requires an explicit `cache_control: {type: "ephemeral"}`
  marker on each cached content block. `split_system_for_anthropic_cache()`
  splits the system prompt at the `## Examples` breakpoint (the first
  per-question section): the prefix goes in a cached block, the suffix
  in an uncached block. `AnthropicProvider.generate()` POSTs to
  `/v1/messages` with that split as the `system=[...]` payload, then
  reads `usage.cache_read_input_tokens` from the response and surfaces
  it as `cached_tokens` on the result — same shape as the OpenAI path
  so downstream telemetry stays provider-agnostic. Configured via
  `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL`
  (default model: `claude-sonnet-4-5`). Auto-detected by
  `get_llm_provider()` when no OpenAI/OpenRouter key is present, or
  selected explicitly via `LLM_PROVIDER=anthropic`.

## Reading `cached_tokens`

Every `NL2CypherResult` / `NL2AqlResult` carries a `cached_tokens` field:

```python
result = nl_to_cypher("...", mapping=bundle, llm_provider=OpenAIProvider(...))
print(
    f"prompt={result.prompt_tokens} cached={result.cached_tokens} "
    f"cache_hit_rate={result.cached_tokens / max(result.prompt_tokens, 1):.0%}"
)
```

It's also surfaced in the `/nl2cypher` and `/nl2aql` HTTP responses so the
UI can render cache-hit-rate badges.

Values count across retry attempts — if the first attempt misses the cache
(cold start) and the second attempt hits, both contribute to the total.

## Wiring the pipeline together

```python
from arango_cypher.nl2cypher import (
    nl_to_cypher,
    OpenAIProvider,
    EntityResolver,
)

result = nl_to_cypher(
    "who acted in 'Forest Gump'?",
    mapping=bundle,
    llm_provider=OpenAIProvider(),
    # WP-25.1: dynamic few-shot (on by default)
    use_fewshot=True,
    # WP-25.2: pre-flight entity resolution — supply a DB handle to enable
    use_entity_resolution=True,
    db=arango_db,
    # WP-25.3: execution-grounded validation — also driven by `db`
    # (EXPLAIN is called automatically when `db` is present)
)
```

## Degrading without network / DB

- No LLM provider configured → `_rule_based_translate()` fallback.
- No DB handle → `EntityResolver.resolve()` returns `[]`, EXPLAIN is skipped.
- `rank_bm25` not installed → `FewShotIndex` returns an empty list, prompt
  falls back to zero-shot.

In every flag-off path, the system prompt is byte-identical to the
Wave 4-pre baseline. This is pinned by `tests/test_nl2cypher_prompt_builder.py`
and the `*_bit_identical` tests in each WP-25 test module.
