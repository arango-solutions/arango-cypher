"""Natural Language to Cypher / AQL translation pipeline.

Converts plain-English questions into Cypher (conceptual) or AQL
(physical) using schema context from a :class:`MappingBundle`.
Supports pluggable LLM backends via :class:`LLMProvider` and includes
a rule-based fallback for common NL→Cypher patterns when no LLM is
configured.

Wave 4a split the former single-file module into submodules:

* ``providers`` — HTTP backends (OpenAI, OpenRouter, …) and provider resolution.
* ``_core``     — schema summarization, :class:`PromptBuilder`,
  rule-based fallback, :func:`nl_to_cypher`, :func:`suggest_nl_queries`.
* ``_aql``      — :func:`nl_to_aql` direct translation path.
* ``fewshot``   — :class:`FewShotIndex` / :class:`BM25Retriever` used
  by :func:`nl_to_cypher` to inject dynamic few-shot examples
  (WP-25.1).

The public surface is re-exported here; downstream code should import
from ``arango_cypher.nl2cypher`` and not reach into the submodules.

Usage::

    from arango_cypher.nl2cypher import nl_to_cypher

    result = nl_to_cypher(
        "Find all people who acted in The Matrix",
        mapping=my_mapping_bundle,
    )
    print(result.cypher)

    # With a custom provider:
    from arango_cypher.nl2cypher import OpenAIProvider, nl_to_cypher
    provider = OpenAIProvider(model="gpt-4o", api_key="sk-...")
    result = nl_to_cypher("...", mapping=bundle, llm_provider=provider)
"""
from __future__ import annotations

from ._aql import NL2AqlResult, nl_to_aql
from ._core import (
    _SYSTEM_PROMPT,
    NL2CypherResult,
    PromptBuilder,
    _build_schema_summary,
    _extract_cypher_from_response,
    nl_to_cypher,
    suggest_nl_queries,
)
from .entity_resolution import EntityResolver, ResolvedEntity
from .fewshot import BM25Retriever, FewShotIndex, Retriever
from .providers import (
    AnthropicProvider,
    LLMProvider,
    OpenAIProvider,
    OpenRouterProvider,
    get_llm_provider,
    split_system_for_anthropic_cache,
)
from .tenant_guardrail import (
    TenantContext,
    TenantScopeViolation,
    check_tenant_scope,
    has_tenant_entity,
)
from .tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
    analyze_tenant_scope,
)

__all__ = [
    "AnthropicProvider",
    "BM25Retriever",
    "EntityResolver",
    "EntityScope",
    "EntityTenantRole",
    "FewShotIndex",
    "LLMProvider",
    "NL2AqlResult",
    "NL2CypherResult",
    "OpenAIProvider",
    "OpenRouterProvider",
    "PromptBuilder",
    "ResolvedEntity",
    "Retriever",
    "TenantContext",
    "TenantScopeManifest",
    "TenantScopeViolation",
    "_SYSTEM_PROMPT",
    "_build_schema_summary",
    "_extract_cypher_from_response",
    "analyze_tenant_scope",
    "check_tenant_scope",
    "get_llm_provider",
    "has_tenant_entity",
    "nl_to_aql",
    "nl_to_cypher",
    "split_system_for_anthropic_cache",
    "suggest_nl_queries",
]
