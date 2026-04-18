"""Pluggable LLM provider interface for the NL→Cypher / NL→AQL pipelines.

Keeps network-facing concerns (HTTP, auth, model selection) isolated from
the prompt-construction and schema-analysis code in ``_core`` and
``_aql``.  Per PRD §1.2, providers receive pre-rendered ``system`` and
``user`` strings and never touch physical schema details on their own.
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for LLM backends that generate text from a prompt.

    ``generate`` accepts pre-rendered ``system`` and ``user`` strings and
    returns ``(response_text, usage_dict)`` where *usage_dict* contains
    ``prompt_tokens``, ``completion_tokens``, and ``total_tokens``.  The
    caller is responsible for rendering the system prompt (see
    :class:`arango_cypher.nl2cypher.PromptBuilder` for the NL→Cypher
    path); providers no longer format schema context themselves, which
    keeps the §1.2 invariant auditable at a single site and lets future
    waves extend the prompt without touching every provider.
    """

    def generate(
        self, system: str, user: str,
    ) -> tuple[str, dict[str, int]]:
        """Return ``(content, usage_dict)`` for the given system/user pair."""
        ...


class _BaseChatProvider:
    """Shared HTTP-based chat completion logic for OpenAI-compatible APIs."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        temperature: float = 0.1,
        timeout: int = 30,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self._extra_headers = extra_headers or {}

    def _chat(
        self, system: str, user: str,
    ) -> tuple[str, dict[str, int]]:
        import requests

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return content, {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }


class OpenAIProvider(_BaseChatProvider):
    """OpenAI-compatible chat completion provider.

    Reads configuration from constructor args or environment variables:
      - ``api_key`` / ``OPENAI_API_KEY``
      - ``base_url`` / ``OPENAI_BASE_URL``  (default: OpenAI)
      - ``model``   / ``OPENAI_MODEL``      (default: gpt-4o-mini)
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        timeout: int = 30,
    ) -> None:
        super().__init__(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=temperature,
            timeout=timeout,
        )

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        return self._chat(system, user)


class OpenRouterProvider(_BaseChatProvider):
    """OpenRouter-compatible chat completion provider.

    Reads configuration from constructor args or environment variables:
      - ``api_key`` / ``OPENROUTER_API_KEY``
      - ``model``   / ``OPENROUTER_MODEL``   (default: openai/gpt-4o-mini)
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        timeout: int = 30,
    ) -> None:
        super().__init__(
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY", ""),
            base_url="https://openrouter.ai/api/v1",
            model=model or os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            temperature=temperature,
            timeout=timeout,
        )

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        return self._chat(system, user)


# TODO(WP-25.4): AnthropicProvider goes here — prompt-caching wave will
# add a Claude-native provider that opts into Anthropic's prompt_cache
# header so the schema-first prefix benefits from server-side caching.


def get_llm_provider() -> OpenAIProvider | OpenRouterProvider | None:
    """Create an LLM provider from environment configuration.

    Resolution order:
    1. ``LLM_PROVIDER=openai``      → :class:`OpenAIProvider`
    2. ``LLM_PROVIDER=openrouter``  → :class:`OpenRouterProvider`
    3. Auto-detect: if ``OPENROUTER_API_KEY`` is set and ``OPENAI_API_KEY``
       is not, use :class:`OpenRouterProvider`; otherwise
       :class:`OpenAIProvider`.

    Returns ``None`` when no API key is available.
    """
    explicit = os.environ.get("LLM_PROVIDER", "").lower().strip()
    if explicit == "openrouter":
        p = OpenRouterProvider()
        return p if p.api_key else None
    if explicit == "openai":
        p = OpenAIProvider()
        return p if p.api_key else None

    has_openai = bool(os.environ.get("OPENAI_API_KEY", ""))
    has_openrouter = bool(os.environ.get("OPENROUTER_API_KEY", ""))
    if has_openrouter and not has_openai:
        return OpenRouterProvider()
    if has_openai:
        return OpenAIProvider()
    return None


_DEFAULT_PROVIDER: _BaseChatProvider | None = None
_DEFAULT_PROVIDER_RESOLVED = False


def _get_default_provider() -> _BaseChatProvider | None:
    """Lazily create a default LLM provider via :func:`get_llm_provider`."""
    global _DEFAULT_PROVIDER, _DEFAULT_PROVIDER_RESOLVED
    if _DEFAULT_PROVIDER_RESOLVED:
        return _DEFAULT_PROVIDER
    _DEFAULT_PROVIDER = get_llm_provider()
    _DEFAULT_PROVIDER_RESOLVED = True
    return _DEFAULT_PROVIDER
