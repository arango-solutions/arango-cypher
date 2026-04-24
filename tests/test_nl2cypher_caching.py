"""Unit tests for WP-25.4 prompt caching.

Covers:

* Section ordering: schema block precedes few-shot and resolved-entities
  sections so providers can cache the schema-first prefix.
* ``cached_tokens`` propagation: OpenAI's ``prompt_tokens_details.cached_tokens``
  surfaces through :class:`NL2CypherResult.cached_tokens`.
* Default ``cached_tokens`` is ``0`` when the provider doesn't report it.
* Anthropic provider stub produces a cache-control split with the
  schema-first prefix marked ephemeral and the per-question suffix
  uncached.

All tests run offline.  The OpenAI path is exercised via a mocked
``requests.post`` response — no network.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from arango_cypher.nl2cypher import (
    AnthropicProvider,
    NL2CypherResult,
    OpenAIProvider,
    PromptBuilder,
    nl_to_cypher,
    split_system_for_anthropic_cache,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture
def movies_mapping():
    return mapping_bundle_for("movies_pg")


class TestSectionOrdering:
    def test_schema_before_examples_and_resolved_entities(self) -> None:
        """The schema block must appear before both extension sections.

        This is the precondition for provider-side prefix caching: if
        the schema drifts away from the top, each new few-shot retrieval
        or resolved-entity set invalidates the cache.
        """
        builder = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[("q1", "MATCH (n) RETURN n")],
            resolved_entities=["'x' -> Label {name: 'x'}"],
        )
        rendered = builder.render_system()
        schema_idx = rendered.index("SCHEMA")
        examples_idx = rendered.index("## Examples")
        resolved_idx = rendered.index("## Resolved entities")
        assert schema_idx < examples_idx
        assert examples_idx < resolved_idx

    def test_zero_shot_prefix_stable(self) -> None:
        """The schema-first prefix must stay byte-stable when extensions are added."""
        bare = PromptBuilder(schema_summary="SCHEMA").render_system()
        with_few = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[("q", "MATCH (n) RETURN n")],
        ).render_system()
        with_both = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[("q", "MATCH (n) RETURN n")],
            resolved_entities=["x"],
        ).render_system()
        assert with_few.startswith(bare)
        assert with_both.startswith(bare)


class TestCachedTokensPropagation:
    def test_openai_cached_tokens_surface(self) -> None:
        """OpenAI's ``prompt_tokens_details.cached_tokens`` propagates end-to-end."""
        provider = OpenAIProvider(api_key="fake", model="gpt-4o-mini")

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return {
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {
                        "prompt_tokens": 1024,
                        "completion_tokens": 64,
                        "total_tokens": 1088,
                        "prompt_tokens_details": {"cached_tokens": 512},
                    },
                }

        with patch(
            "requests.post",
            return_value=_Resp(),
        ):
            content, usage = provider.generate("system", "user")
        assert content == "OK"
        assert usage["cached_tokens"] == 512
        assert usage["prompt_tokens"] == 1024

    def test_cached_tokens_default_zero_when_absent(self) -> None:
        """Providers that don't report cached_tokens yield 0, not a crash."""
        provider = OpenAIProvider(api_key="fake", model="gpt-4o-mini")

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return {
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "total_tokens": 110,
                    },
                }

        with patch("requests.post", return_value=_Resp()):
            _, usage = provider.generate("system", "user")
        assert usage["cached_tokens"] == 0

    def test_cached_tokens_propagate_into_nl2cypher_result(self, movies_mapping) -> None:
        class _P:
            def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
                return (
                    "```cypher\nMATCH (p:Person) RETURN p\n```",
                    {
                        "prompt_tokens": 800,
                        "completion_tokens": 40,
                        "total_tokens": 840,
                        "cached_tokens": 640,
                    },
                )

        res: NL2CypherResult = nl_to_cypher(
            "q",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=_P(),
        )
        assert res.cached_tokens == 640
        assert res.prompt_tokens == 800

    def test_cached_tokens_accumulates_across_retries(self, movies_mapping) -> None:
        """Retries add up: each attempt's cached_tokens contributes to the total."""
        responses = [
            ("```\nnot valid cypher\n```", 100),
            ("```cypher\nMATCH (p:Person) RETURN p\n```", 200),
        ]

        class _P:
            def __init__(self) -> None:
                self._i = 0

            def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
                content, cached = responses[self._i]
                self._i += 1
                return content, {
                    "prompt_tokens": 1000,
                    "completion_tokens": 10,
                    "total_tokens": 1010,
                    "cached_tokens": cached,
                }

        res = nl_to_cypher(
            "q",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=_P(),
            max_retries=2,
        )
        assert res.cached_tokens == 300
        assert res.retries == 1


class TestAnthropicCacheControl:
    def test_split_without_breakpoint_returns_single_cached_block(self) -> None:
        """Pure schema-only prompts become one cached block."""
        blocks = split_system_for_anthropic_cache(
            "You are a Cypher expert.\n\nSCHEMA:\n  Node :Person",
        )
        assert len(blocks) == 1
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "SCHEMA" in blocks[0]["text"]

    def test_split_with_examples_breakpoint(self) -> None:
        """Schema is cached; examples/resolved-entities form the uncached suffix."""
        system = (
            "Prelude\n\nSCHEMA:\n  Node :Person\n\n## Examples\nQ: who?\n```cypher\nMATCH (n) RETURN n\n```"
        )
        blocks = split_system_for_anthropic_cache(system)
        assert len(blocks) == 2
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "Prelude" in blocks[0]["text"]
        assert "SCHEMA" in blocks[0]["text"]
        assert "Examples" not in blocks[0]["text"]
        assert "cache_control" not in blocks[1]
        assert "Examples" in blocks[1]["text"]

    def test_split_empty_system_safe(self) -> None:
        blocks = split_system_for_anthropic_cache("")
        assert len(blocks) == 1
        assert blocks[0]["text"] == ""

    def test_provider_stub_build_system_blocks(self) -> None:
        provider = AnthropicProvider(api_key="fake")
        system = "Prelude\n\nSCHEMA\n\n## Examples\nQ: x"
        blocks = provider.build_system_blocks(system)
        assert len(blocks) == 2
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "SCHEMA" in blocks[0]["text"]
        assert "Examples" in blocks[1]["text"]

    def test_provider_generate_posts_to_messages_api(self) -> None:
        """generate() POSTs to /v1/messages with the cache-control system blocks."""
        provider = AnthropicProvider(api_key="fake-key", model="claude-3-5-sonnet-latest")

        captured: dict[str, object] = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return {
                    "id": "msg_x",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-5-sonnet-latest",
                    "content": [
                        {"type": "text", "text": "MATCH (n) RETURN n"},
                    ],
                    "usage": {
                        "input_tokens": 32,
                        "output_tokens": 16,
                        "cache_creation_input_tokens": 1024,
                        "cache_read_input_tokens": 0,
                    },
                }

        def _capture(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _Resp()

        with patch("requests.post", side_effect=_capture):
            content, usage = provider.generate(
                "Prelude\n\nSCHEMA\n\n## Examples\nQ: x",
                "user question",
            )

        assert content == "MATCH (n) RETURN n"
        assert captured["url"].endswith("/v1/messages")
        headers = captured["headers"]
        assert headers["x-api-key"] == "fake-key"
        assert headers["anthropic-version"]
        body = captured["json"]
        assert body["model"] == "claude-3-5-sonnet-latest"
        assert body["messages"] == [{"role": "user", "content": "user question"}]
        assert isinstance(body["system"], list)
        assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert "SCHEMA" in body["system"][0]["text"]
        assert usage["completion_tokens"] == 16
        assert usage["cached_tokens"] == 0
        assert usage["prompt_tokens"] == 32 + 0 + 1024
        assert usage["total_tokens"] == usage["prompt_tokens"] + 16

    def test_provider_generate_surfaces_cache_read_tokens(self) -> None:
        """Second-request cache hits land in cached_tokens for downstream telemetry."""
        provider = AnthropicProvider(api_key="fake-key")

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return {
                    "content": [{"type": "text", "text": "OK"}],
                    "usage": {
                        "input_tokens": 32,
                        "output_tokens": 8,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 1024,
                    },
                }

        with patch("requests.post", return_value=_Resp()):
            _, usage = provider.generate("system", "user")
        assert usage["cached_tokens"] == 1024
        assert usage["prompt_tokens"] == 32 + 1024 + 0

    def test_provider_handles_missing_usage_keys(self) -> None:
        """A provider response that omits cache fields returns 0, not a crash."""
        provider = AnthropicProvider(api_key="fake-key")

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return {
                    "content": [{"type": "text", "text": "OK"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }

        with patch("requests.post", return_value=_Resp()):
            _, usage = provider.generate("system", "user")
        assert usage["cached_tokens"] == 0
        assert usage["prompt_tokens"] == 10
        assert usage["total_tokens"] == 15

    def test_provider_concatenates_text_blocks_and_skips_others(self) -> None:
        """Multiple text content blocks are joined; non-text blocks are skipped."""
        provider = AnthropicProvider(api_key="fake-key")

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return {
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "tool_use", "id": "x", "name": "noop", "input": {}},
                        {"type": "text", "text": "world"},
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }

        with patch("requests.post", return_value=_Resp()):
            content, _ = provider.generate("system", "user")
        assert content == "Hello world"


class TestProviderResolution:
    """get_llm_provider should resolve Anthropic via explicit and auto-detect paths."""

    def test_explicit_anthropic_with_key_returns_anthropic_provider(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from arango_cypher.nl2cypher import get_llm_provider

        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        provider = get_llm_provider()
        assert isinstance(provider, AnthropicProvider)
        assert provider.api_key == "ak"

    def test_explicit_anthropic_without_key_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from arango_cypher.nl2cypher import get_llm_provider

        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert get_llm_provider() is None

    def test_autodetect_falls_back_to_anthropic_when_only_anthropic_key_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from arango_cypher.nl2cypher import get_llm_provider

        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
        provider = get_llm_provider()
        assert isinstance(provider, AnthropicProvider)

    def test_autodetect_prefers_openai_over_anthropic(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OpenAI wins on auto-detect because the eval gate baseline is calibrated to it."""
        from arango_cypher.nl2cypher import OpenAIProvider, get_llm_provider

        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "ok")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
        provider = get_llm_provider()
        assert isinstance(provider, OpenAIProvider)


class TestAnthropicLiveSmoke:
    """Opt-in real-API smoke test (requires ANTHROPIC_API_KEY)."""

    @pytest.mark.skipif(
        not __import__("os").environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set; skipping live Anthropic smoke",
    )
    def test_live_cache_hit_on_second_identical_request(self) -> None:
        """Two identical calls in a row should yield ``cached_tokens > 0`` on the second.

        Anthropic's prompt caching has a minimum prefix length (~1024
        tokens for Sonnet at the time of writing).  We pad the system
        prompt past that threshold with a long static block so the
        cache marker on the prefix actually takes effect.
        """
        provider = AnthropicProvider()
        padding = "Static schema padding line.\n" * 200
        system = f"You are a helpful assistant.\n\n{padding}"
        user = "Reply with exactly the word OK and nothing else."

        _, usage1 = provider.generate(system, user)
        _, usage2 = provider.generate(system, user)
        assert usage2["cached_tokens"] > 0, (
            f"expected cache hit on second request, got usage={usage2}; first request usage was {usage1}"
        )


class TestCachedTokensSerializationShape:
    """Smoke-test that the HTTP response keys are stable for the UI."""

    def test_http_response_includes_cached_tokens_key(self, movies_mapping) -> None:
        from fastapi.testclient import TestClient

        from arango_cypher.service import app

        client = TestClient(app)
        resp = client.post(
            "/nl2cypher",
            json={
                "question": "find people",
                "mapping": {
                    "conceptualSchema": movies_mapping.conceptual_schema,
                    "physicalMapping": movies_mapping.physical_mapping,
                    "metadata": {},
                },
                "use_llm": False,
            },
        )
        assert resp.status_code == 200, resp.text
        body = json.loads(resp.text)
        assert "cached_tokens" in body
        assert body["cached_tokens"] == 0
