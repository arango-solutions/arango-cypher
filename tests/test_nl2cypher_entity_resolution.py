"""Unit tests for WP-25.2 pre-flight entity resolution.

These tests run fully offline — the DB handle is either ``None`` or a
minimal duck-typed mock (see :class:`_FakeDb`).  They pin:

* Candidate extraction: precision over recall.
* Schema-keyword rejection (no resolving "Person" against Person labels).
* Mock-DB resolution: typo → corrected string.
* Offline fallback: resolver with ``db=None`` returns ``[]``.
* Prompt-section format: bullet rendering matches the PromptBuilder contract.
* Zero-shot bit-identity: with ``use_entity_resolution=False`` the system
  prompt is byte-identical to the Wave 4-pre baseline.
"""
from __future__ import annotations

from typing import Any

import pytest

from arango_cypher.nl2cypher import (
    EntityResolver,
    PromptBuilder,
    ResolvedEntity,
    nl_to_cypher,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.test_nl2cypher_prompt_builder import FROZEN_SYSTEM_PROMPT


@pytest.fixture
def movies_mapping():
    return mapping_bundle_for("movies_pg")


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeAql:
    def __init__(self, responder):
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    def execute(self, aql: str, *, bind_vars: dict[str, Any]) -> _FakeCursor:
        self.calls.append({"aql": aql, "bind_vars": dict(bind_vars)})
        rows = self._responder(aql, bind_vars)
        return _FakeCursor(rows or [])


class _FakeDb:
    def __init__(self, responder):
        self.aql = _FakeAql(responder)


class TestExtractCandidates:
    def test_quoted_double(self, movies_mapping) -> None:
        resolver = EntityResolver(mapping=movies_mapping)
        cands = resolver.extract_candidates('Find movies similar to "The Matrix"')
        assert "The Matrix" in cands

    def test_quoted_single(self, movies_mapping) -> None:
        resolver = EntityResolver(mapping=movies_mapping)
        cands = resolver.extract_candidates("Find movies like 'Forest Gump'")
        assert "Forest Gump" in cands

    def test_title_case_phrase(self, movies_mapping) -> None:
        resolver = EntityResolver(mapping=movies_mapping)
        cands = resolver.extract_candidates("Which movies did Tom Hanks act in?")
        assert "Tom Hanks" in cands

    def test_skips_schema_keywords(self, movies_mapping) -> None:
        """Schema labels must not leak into candidates."""
        resolver = EntityResolver(mapping=movies_mapping)
        cands = resolver.extract_candidates("Find all Person nodes")
        assert all(c.lower() != "person" for c in cands), cands

    def test_empty_question_returns_empty(self, movies_mapping) -> None:
        resolver = EntityResolver(mapping=movies_mapping)
        assert resolver.extract_candidates("") == []

    def test_stopwords_excluded(self, movies_mapping) -> None:
        resolver = EntityResolver(mapping=movies_mapping)
        cands = resolver.extract_candidates("Which movies")
        assert "Which" not in cands

    def test_max_candidates_cap(self, movies_mapping) -> None:
        """The resolver must cap candidates even on noisy input."""
        resolver = EntityResolver(mapping=movies_mapping, max_candidates=2)
        noisy = "Alice Bob Charlie Dave Eve Frank George Henry"
        cands = resolver.extract_candidates(noisy)
        assert len(cands) <= 2


class TestResolveWithMockedDb:
    def test_typo_corrected(self, movies_mapping) -> None:
        """'Forest Gump' → 'Forrest Gump' via mocked contains-match."""
        def responder(aql: str, bind_vars: dict[str, Any]):
            field = bind_vars.get("field")
            collection = bind_vars.get("@c")
            mention = bind_vars.get("m", "").lower()
            if collection == "movies" and field == "title" and "gump" in mention:
                return [{"value": "Forrest Gump", "score": 0.85}]
            return []

        resolver = EntityResolver(
            db=_FakeDb(responder),
            mapping=movies_mapping,
        )
        hits = resolver.resolve('who acted in "Forest Gump"?')
        assert len(hits) == 1
        assert hits[0].mention == "Forest Gump"
        assert hits[0].label == "Movie"
        assert hits[0].property == "title"
        assert hits[0].value == "Forrest Gump"
        assert hits[0].score >= 0.5

    def test_no_match_returns_empty(self, movies_mapping) -> None:
        resolver = EntityResolver(
            db=_FakeDb(lambda a, b: []),
            mapping=movies_mapping,
        )
        hits = resolver.resolve("who acted in 'Nonexistent Title'?")
        assert hits == []

    def test_multiple_candidates_in_one_question(self, movies_mapping) -> None:
        """Both 'Tom Hanks' (Person.name) and 'Forest Gump' (Movie.title) resolve."""
        def responder(aql: str, bind_vars: dict[str, Any]):
            collection = bind_vars.get("@c")
            field = bind_vars.get("field")
            mention = bind_vars.get("m", "").lower()
            if collection == "persons" and field == "name" and "hanks" in mention:
                return [{"value": "Tom Hanks", "score": 1.0}]
            if collection == "movies" and field == "title" and "gump" in mention:
                return [{"value": "Forrest Gump", "score": 0.9}]
            return []

        resolver = EntityResolver(
            db=_FakeDb(responder),
            mapping=movies_mapping,
        )
        hits = resolver.resolve('Did Tom Hanks act in "Forest Gump"?')
        mentions = {h.mention for h in hits}
        assert "Tom Hanks" in mentions
        assert "Forest Gump" in mentions

    def test_best_score_wins_when_multiple_props_match(self, movies_mapping) -> None:
        """When two properties of the same label both match, the higher score wins."""
        def responder(aql: str, bind_vars: dict[str, Any]):
            field = bind_vars.get("field")
            if bind_vars.get("@c") == "movies" and field == "title":
                return [{"value": "The Matrix", "score": 1.0}]
            if bind_vars.get("@c") == "movies" and field == "label":
                return [{"value": "The Matrix (1999)", "score": 0.6}]
            return []

        resolver = EntityResolver(
            db=_FakeDb(responder),
            mapping=movies_mapping,
        )
        hits = resolver.resolve('Find movies similar to "The Matrix"')
        assert len(hits) == 1
        assert hits[0].value == "The Matrix"
        assert hits[0].property == "title"

    def test_below_threshold_filtered(self, movies_mapping) -> None:
        """Matches below ``min_score`` are dropped."""
        def responder(aql: str, bind_vars: dict[str, Any]):
            return [{"value": "Barely Related", "score": 0.3}]

        resolver = EntityResolver(
            db=_FakeDb(responder),
            mapping=movies_mapping,
            min_score=0.6,
        )
        assert resolver.resolve('who is "X"?') == []

    def test_cached_per_question(self, movies_mapping) -> None:
        """Repeat resolutions hit the per-instance cache, not the DB."""
        counter = {"calls": 0}

        def responder(aql: str, bind_vars: dict[str, Any]):
            counter["calls"] += 1
            if bind_vars.get("@c") == "movies" and bind_vars.get("field") == "title":
                return [{"value": "The Matrix", "score": 1.0}]
            return []

        resolver = EntityResolver(
            db=_FakeDb(responder),
            mapping=movies_mapping,
        )
        first = resolver.resolve('Find "The Matrix"')
        calls_after_first = counter["calls"]
        second = resolver.resolve('Find "The Matrix"')
        assert first == second
        assert counter["calls"] == calls_after_first, "second call should be cached"

    def test_query_failure_returns_empty(self, movies_mapping) -> None:
        """A broken DB must not propagate — resolver logs and returns []."""
        class _Boom:
            class aql:  # noqa: N801
                @staticmethod
                def execute(*a, **k):
                    raise RuntimeError("db down")

        resolver = EntityResolver(
            db=_Boom(),
            mapping=movies_mapping,
        )
        assert resolver.resolve('who is "X"?') == []


class TestOfflineFallback:
    def test_no_db_returns_empty(self, movies_mapping) -> None:
        assert EntityResolver(db=None, mapping=movies_mapping).resolve("q") == []

    def test_no_mapping_returns_empty(self) -> None:
        assert EntityResolver(db=object(), mapping=None).resolve("q") == []


class TestFormatPromptSection:
    def test_renders_bullets(self, movies_mapping) -> None:
        resolver = EntityResolver(mapping=movies_mapping)
        resolved = [
            ResolvedEntity(
                mention="Forest Gump",
                label="Movie",
                property="title",
                value="Forrest Gump",
                score=0.92,
            ),
        ]
        lines = resolver.format_prompt_section(resolved)
        assert lines == [
            '"Forest Gump" → Movie.title = "Forrest Gump" (similarity 0.92)',
        ]

    def test_prompt_builder_wraps_with_header(self, movies_mapping) -> None:
        """The ``## Resolved entities`` header is owned by PromptBuilder."""
        resolver = EntityResolver(mapping=movies_mapping)
        resolved = [
            ResolvedEntity(
                mention="Tom Hanks", label="Person", property="name",
                value="Tom Hanks", score=1.0,
            ),
        ]
        lines = resolver.format_prompt_section(resolved)
        rendered = PromptBuilder(
            schema_summary="SCHEMA",
            resolved_entities=lines,
        ).render_system()
        assert "## Resolved entities" in rendered
        assert "Tom Hanks" in rendered
        assert rendered.index("SCHEMA") < rendered.index("Resolved entities")


class TestNlToCypherIntegration:
    def test_use_entity_resolution_false_is_bit_identical(self, movies_mapping) -> None:
        """With the flag off, the system prompt matches the Wave 4-pre baseline."""
        from arango_cypher.nl2cypher import _build_schema_summary

        captured: dict[str, str] = {}

        class _Provider:
            def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
                captured["system"] = system
                return (
                    "```cypher\nMATCH (n:Person) RETURN n\n```",
                    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )

        nl_to_cypher(
            'who acted in "Forest Gump"?',
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=_Provider(),
        )
        expected = FROZEN_SYSTEM_PROMPT.replace(
            "{schema}", _build_schema_summary(movies_mapping),
        )
        assert captured["system"] == expected
        assert "Resolved entities" not in captured["system"]

    def test_resolver_hits_are_injected_into_prompt(self, movies_mapping) -> None:
        """A resolver with hits surfaces them in the rendered system prompt."""
        captured: dict[str, str] = {}

        class _Provider:
            def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
                captured["system"] = system
                return (
                    "```cypher\nMATCH (m:Movie {title: 'Forrest Gump'}) RETURN m\n```",
                    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )

        def responder(aql: str, bind_vars: dict[str, Any]):
            if bind_vars.get("@c") == "movies" and bind_vars.get("field") == "title":
                return [{"value": "Forrest Gump", "score": 0.92}]
            return []

        nl_to_cypher(
            'who acted in "Forest Gump"?',
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=True,
            db=_FakeDb(responder),
            llm_provider=_Provider(),
        )
        assert "Resolved entities" in captured["system"]
        assert "Forrest Gump" in captured["system"]
        assert "Forest Gump" in captured["system"]

    def test_no_db_no_resolution(self, movies_mapping) -> None:
        """Without a DB, the resolved-entities section is absent."""
        captured: dict[str, str] = {}

        class _Provider:
            def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
                captured["system"] = system
                return (
                    "```cypher\nMATCH (n) RETURN n\n```",
                    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )

        nl_to_cypher(
            'who acted in "Forest Gump"?',
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=True,
            db=None,
            llm_provider=_Provider(),
        )
        assert "Resolved entities" not in captured["system"]
