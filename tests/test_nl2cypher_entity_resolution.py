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

    def execute(
        self,
        aql: str,
        *,
        bind_vars: dict[str, Any],
        max_runtime: float | None = None,
        **_: Any,
    ) -> _FakeCursor:
        self.calls.append(
            {"aql": aql, "bind_vars": dict(bind_vars), "max_runtime": max_runtime}
        )
        rows = self._responder(aql, bind_vars)
        return _FakeCursor(rows or [])


class _FakeCollection:
    def __init__(self, count: int, indexes: list[dict[str, Any]]) -> None:
        self._count = count
        self._indexes = indexes

    def count(self) -> int:
        return self._count

    def indexes(self) -> list[dict[str, Any]]:
        return self._indexes


class _FakeDb:
    def __init__(self, responder, collections: dict[str, _FakeCollection] | None = None):
        self.aql = _FakeAql(responder)
        self._collections = collections or {}

    def collection(self, name: str) -> _FakeCollection:
        if name not in self._collections:
            raise KeyError(name)
        return self._collections[name]


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

    def test_parenthesized_symbol_extracted(self, movies_mapping) -> None:
        """WP-S2b: a ticker in parentheses is captured as a candidate."""
        resolver = EntityResolver(mapping=movies_mapping)
        cands = resolver.extract_candidates(
            "companies that Cincinnati Financial (CINF) has a stake in"
        )
        assert "CINF" in cands
        assert "Cincinnati Financial" in cands

    def test_parenthesized_symbol_probed_first(self, movies_mapping) -> None:
        """The symbol should precede the long name so an exact match wins."""
        resolver = EntityResolver(mapping=movies_mapping)
        cands = resolver.extract_candidates("Cincinnati Financial (CINF) stake")
        assert cands.index("CINF") < cands.index("Cincinnati Financial")

    def test_dotted_symbol_extracted(self, movies_mapping) -> None:
        resolver = EntityResolver(mapping=movies_mapping)
        cands = resolver.extract_candidates("Berkshire Hathaway (BRK.B) holdings")
        assert "BRK.B" in cands

    def test_year_in_parens_not_a_symbol(self, movies_mapping) -> None:
        # "(2020)" is not an uppercase-letter-led symbol.
        resolver = EntityResolver(mapping=movies_mapping)
        cands = resolver.extract_candidates("movies from (2020) onward")
        assert "2020" not in cands


class TestIdentifierPropertyCandidates:
    """WP-S2b: identifier/symbol fields are part of the probe property set."""

    def test_symbol_fields_present(self) -> None:
        from arango_cypher.nl2cypher.entity_resolution import (
            _STRING_PROPERTY_CANDIDATES,
        )

        for field_name in ("ticker", "symbol", "code", "id"):
            assert field_name in _STRING_PROPERTY_CANDIDATES


class TestArangoSearchAdvisory:
    """WP-S3: fuzzy probes on un-indexed fields emit an IndexAdvisory."""

    @staticmethod
    def _gump_responder(aql: str, bind_vars: dict[str, Any]):
        if bind_vars.get("@c") == "movies" and "gump" in bind_vars.get("m", "").lower():
            return [{"value": "Forrest Gump", "score": 0.85}]
        return []

    def test_advisory_emitted_without_fuzzy_index(self, movies_mapping) -> None:
        from arango_cypher.nl2cypher import IndexAdvisory

        # No collections registered → indexes() unavailable → "no fuzzy coverage".
        resolver = EntityResolver(db=_FakeDb(self._gump_responder), mapping=movies_mapping)
        resolver.resolve('who acted in "Forest Gump"?')
        assert resolver.advisories, "expected an ArangoSearch advisory"
        assert all(isinstance(a, IndexAdvisory) for a in resolver.advisories)
        # The resolver probes every label×property pair; the movies.title probe
        # (the slow Levenshtein scan) must be among the advisories.
        by_key = {(a.collection, a.field): a for a in resolver.advisories}
        assert ("movies", "title") in by_key
        spec = by_key[("movies", "title")].suggested_inverted_index()
        assert spec["type"] == "inverted"
        assert spec["fields"][0]["name"] == "title"

    def test_no_advisory_when_field_has_inverted_index(self, movies_mapping) -> None:
        collections = {
            "movies": _FakeCollection(
                count=10,
                indexes=[
                    {"type": "inverted", "fields": [{"name": "title", "analyzer": "text_en"}]}
                ],
            )
        }
        resolver = EntityResolver(
            db=_FakeDb(self._gump_responder, collections=collections),
            mapping=movies_mapping,
        )
        resolver.resolve('who acted in "Forest Gump"?')
        assert all(a.field != "title" for a in resolver.advisories)

    def test_advisory_deduped(self, movies_mapping) -> None:
        resolver = EntityResolver(db=_FakeDb(self._gump_responder), mapping=movies_mapping)
        resolver.resolve('who acted in "Forest Gump"?')
        resolver._cache.clear()  # force re-probe
        resolver.resolve('who acted in "Forest Gump"?')
        keys = [(a.collection, a.field) for a in resolver.advisories]
        assert len(keys) == len(set(keys)), keys

    def test_advisory_as_dict_shape(self, movies_mapping) -> None:
        resolver = EntityResolver(db=_FakeDb(self._gump_responder), mapping=movies_mapping)
        resolver.resolve('who acted in "Forest Gump"?')
        d = resolver.advisories[0].as_dict()
        assert set(d) >= {"collection", "field", "reason", "suggestedIndex"}


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


class TestFuzzyScoring:
    """Wave 4h: ``LEVENSHTEIN_DISTANCE``-based fuzzy match in the AQL."""

    def test_fuzzy_threshold_is_bound_into_query(self, movies_mapping) -> None:
        """The configured threshold must reach the query as a bind var."""
        captured: list[dict[str, Any]] = []

        def responder(aql: str, bind_vars: dict[str, Any]):
            captured.append(dict(bind_vars))
            return [{"value": "Forrest Gump", "score": 0.82}]

        resolver = EntityResolver(
            db=_FakeDb(responder),
            mapping=movies_mapping,
            fuzzy_threshold=0.65,
        )
        resolver.resolve('who acted in "Forest Gump"?')
        assert captured, "expected at least one AQL execution"
        assert captured[0].get("fuzzy_threshold") == 0.65

    def test_aql_includes_levenshtein_branch(self, movies_mapping) -> None:
        """Defensive: the emitted AQL must reference LEVENSHTEIN_DISTANCE.

        We don't pin the full AQL string (too brittle), but we do pin
        the *presence* of the fuzzy branch so a future refactor that
        accidentally drops it breaks loudly.
        """
        captured: list[str] = []

        def responder(aql: str, bind_vars: dict[str, Any]):
            captured.append(aql)
            return []

        resolver = EntityResolver(
            db=_FakeDb(responder),
            mapping=movies_mapping,
        )
        resolver.resolve('find "Anything"')
        assert captured
        aql = captured[0]
        assert "LEVENSHTEIN_DISTANCE" in aql
        assert "fuzzy_threshold" in aql
        assert "MAX([exact, contains, reverse, fuzzy])" in aql

    def test_default_threshold_is_documented(self) -> None:
        """The default fuzzy_threshold should match the docstring (0.7)."""
        resolver = EntityResolver(db=None, mapping=None)
        assert resolver.fuzzy_threshold == 0.7

    def test_fuzzy_score_below_threshold_dropped_by_filter(
        self,
        movies_mapping,
    ) -> None:
        """When the DB returns score=0 (because fuzzy was below threshold),
        the resolver drops the candidate via min_score, not by inspecting AQL.
        """

        def responder(aql: str, bind_vars: dict[str, Any]):
            return []

        resolver = EntityResolver(
            db=_FakeDb(responder),
            mapping=movies_mapping,
        )
        assert resolver.resolve('find "Totally Unrelated"') == []


class TestOfflineFallback:
    def test_no_db_returns_empty(self, movies_mapping) -> None:
        assert EntityResolver(db=None, mapping=movies_mapping).resolve("q") == []

    def test_no_mapping_returns_empty(self) -> None:
        assert EntityResolver(db=object(), mapping=None).resolve("q") == []


class TestProbeHardening:
    """Per-probe safety limits that keep resolution from hanging on big data."""

    def test_default_probe_timeout_passed_to_execute(self, movies_mapping) -> None:
        db = _FakeDb(lambda a, b: [])
        resolver = EntityResolver(db=db, mapping=movies_mapping)
        resolver.resolve('find "The Matrix"')
        assert db.aql.calls, "expected at least one probe"
        assert all(c["max_runtime"] == 5.0 for c in db.aql.calls)

    def test_probe_timeout_zero_disables_max_runtime(self, movies_mapping) -> None:
        db = _FakeDb(lambda a, b: [])
        resolver = EntityResolver(db=db, mapping=movies_mapping, probe_timeout=0)
        resolver.resolve('find "The Matrix"')
        assert db.aql.calls
        assert all(c["max_runtime"] is None for c in db.aql.calls)

    def test_explicit_probe_timeout_overrides_default(self, movies_mapping) -> None:
        db = _FakeDb(lambda a, b: [])
        resolver = EntityResolver(db=db, mapping=movies_mapping, probe_timeout=2.5)
        resolver.resolve('find "The Matrix"')
        assert all(c["max_runtime"] == 2.5 for c in db.aql.calls)

    def test_value_length_filter_present_by_default(self, movies_mapping) -> None:
        captured: list[str] = []
        db = _FakeDb(lambda aql, b: captured.append(aql) or [])
        resolver = EntityResolver(db=db, mapping=movies_mapping)
        resolver.resolve('find "The Matrix"')
        assert captured
        assert "LENGTH(d[@field]) <= @max_value_length" in captured[0]
        assert db.aql.calls[0]["bind_vars"]["max_value_length"] == 512

    def test_value_length_zero_disables_filter(self, movies_mapping) -> None:
        captured: list[str] = []
        db = _FakeDb(lambda aql, b: captured.append(aql) or [])
        resolver = EntityResolver(db=db, mapping=movies_mapping, max_value_length=0)
        resolver.resolve('find "The Matrix"')
        assert captured
        assert "max_value_length" not in captured[0]
        assert "max_value_length" not in db.aql.calls[0]["bind_vars"]

    def test_env_overrides_apply(self, movies_mapping, monkeypatch) -> None:
        monkeypatch.setenv("ARANGO_CYPHER_ER_PROBE_TIMEOUT_S", "1.5")
        monkeypatch.setenv("ARANGO_CYPHER_ER_MAX_VALUE_LENGTH", "64")
        monkeypatch.setenv("ARANGO_CYPHER_ER_MAX_SCAN_COLLECTION_SIZE", "9000")
        resolver = EntityResolver(db=object(), mapping=movies_mapping)
        assert resolver.probe_timeout == 1.5
        assert resolver.max_value_length == 64
        assert resolver.max_scan_collection_size == 9000

    def test_invalid_env_falls_back_to_default(self, movies_mapping, monkeypatch) -> None:
        monkeypatch.setenv("ARANGO_CYPHER_ER_PROBE_TIMEOUT_S", "not-a-number")
        resolver = EntityResolver(db=object(), mapping=movies_mapping)
        assert resolver.probe_timeout == 5.0

    def test_size_gate_disabled_by_default_probes_large_collection(
        self, movies_mapping
    ) -> None:
        seen: list[str] = []

        def responder(aql, bind_vars):
            seen.append(bind_vars.get("@c"))
            return []

        db = _FakeDb(
            responder,
            collections={"movies": _FakeCollection(count=10_000_000, indexes=[])},
        )
        resolver = EntityResolver(db=db, mapping=movies_mapping)
        resolver.resolve('find "The Matrix"')
        assert "movies" in seen, "gate off → large collection still probed"

    def test_size_gate_skips_oversized_unindexed_collection(
        self, movies_mapping
    ) -> None:
        seen: list[str] = []

        def responder(aql, bind_vars):
            seen.append(bind_vars.get("@c"))
            return []

        db = _FakeDb(
            responder,
            collections={"movies": _FakeCollection(count=10_000, indexes=[])},
        )
        resolver = EntityResolver(
            db=db, mapping=movies_mapping, max_scan_collection_size=1000
        )
        resolver.resolve('find "The Matrix"')
        assert "movies" not in seen, "oversized unindexed collection must be skipped"

    def test_size_gate_allows_indexed_collection(self, movies_mapping) -> None:
        seen: list[str] = []

        def responder(aql, bind_vars):
            seen.append(bind_vars.get("@c"))
            return [{"value": "The Matrix", "score": 1.0}] if bind_vars.get("@c") == "movies" else []

        db = _FakeDb(
            responder,
            collections={
                "movies": _FakeCollection(
                    count=10_000,
                    indexes=[{"type": "persistent", "fields": ["title"]}],
                )
            },
        )
        resolver = EntityResolver(
            db=db, mapping=movies_mapping, max_scan_collection_size=1000
        )
        hits = resolver.resolve('find "The Matrix"')
        assert "movies" in seen, "indexed collection must still be probed"
        assert any(h.value == "The Matrix" for h in hits)

    def test_size_gate_does_not_skip_when_count_unknown(self, movies_mapping) -> None:
        seen: list[str] = []

        def responder(aql, bind_vars):
            seen.append(bind_vars.get("@c"))
            return []

        # No collections registered → count() raises → count unknown → no skip.
        db = _FakeDb(responder)
        resolver = EntityResolver(
            db=db, mapping=movies_mapping, max_scan_collection_size=1000
        )
        resolver.resolve('find "The Matrix"')
        assert "movies" in seen, "unknown count must not skip the probe"

    def test_total_budget_default_value(self, movies_mapping) -> None:
        resolver = EntityResolver(db=object(), mapping=movies_mapping)
        assert resolver.total_budget == 8.0

    def test_total_budget_env_override(self, movies_mapping, monkeypatch) -> None:
        monkeypatch.setenv("ARANGO_CYPHER_ER_TOTAL_BUDGET_S", "3")
        resolver = EntityResolver(db=object(), mapping=movies_mapping)
        assert resolver.total_budget == 3.0

    def test_tiny_budget_short_circuits_before_any_probe(self, movies_mapping) -> None:
        db = _FakeDb(lambda a, b: [])
        # A sub-nanosecond budget is always exhausted before the first probe.
        resolver = EntityResolver(db=db, mapping=movies_mapping, total_budget=1e-9)
        result = resolver.resolve('find "The Matrix"')
        assert result == []
        assert db.aql.calls == [], "budget must stop probing before any DB call"

    def test_zero_budget_disables_ceiling(self, movies_mapping) -> None:
        db = _FakeDb(lambda a, b: [])
        resolver = EntityResolver(db=db, mapping=movies_mapping, total_budget=0)
        resolver.resolve('find "The Matrix"')
        assert db.aql.calls, "budget=0 disables the ceiling → probes run"


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
                mention="Tom Hanks",
                label="Person",
                property="name",
                value="Tom Hanks",
                score=1.0,
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
            "{schema}",
            _build_schema_summary(movies_mapping),
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
