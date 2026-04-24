"""Tests for WP-25.1 dynamic few-shot retrieval.

Pins the contract between :func:`nl_to_cypher` and the BM25-backed
:class:`FewShotIndex`: when few-shot is enabled the prompt gains an
``## Examples`` section populated from the shipped corpora, and when
it is disabled the system string is byte-identical to the Wave 4-pre
zero-shot baseline.

These tests run fully offline — no LLM is ever invoked.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from arango_cypher.nl2cypher import BM25Retriever, FewShotIndex, nl_to_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.test_nl2cypher_prompt_builder import FROZEN_SYSTEM_PROMPT

CORPORA_DIR = Path(__file__).resolve().parent.parent / "arango_cypher" / "nl2cypher" / "corpora"


@pytest.fixture
def movies_mapping():
    return mapping_bundle_for("movies_pg")


def _rank_bm25_available() -> bool:
    return importlib.util.find_spec("rank_bm25") is not None


@pytest.mark.skipif(
    not _rank_bm25_available(),
    reason="rank_bm25 is required for this test",
)
def test_bm25_retriever_finds_similar_question() -> None:
    """With a tiny curated corpus, BM25 must surface the clearly-matching example."""
    examples = [
        (
            "Who directed The Matrix?",
            'MATCH (p:Person)-[:DIRECTED]->(m:Movie {title: "The Matrix"}) RETURN p.name',
        ),
        (
            "List all customers in Germany",
            "MATCH (c:Customer) WHERE c.country = 'Germany' RETURN c.companyName",
        ),
        ("Show users older than 25", "MATCH (u:User) WHERE u.age > 25 RETURN u.name"),
    ]
    retriever = BM25Retriever(examples)
    top = retriever.retrieve("Who directed the film The Matrix", k=1)
    assert len(top) == 1
    assert top[0][0] == "Who directed The Matrix?"


def test_bm25_retriever_raises_without_rank_bm25_installed() -> None:
    """Without ``rank_bm25`` installed, constructing BM25Retriever must fail loudly.

    ``rank_bm25`` is typically installed in the dev environment, so this
    test skips there; it only asserts behavior on a bare install.  We
    never uninstall anything — per the task contract.
    """
    if _rank_bm25_available():
        pytest.skip("rank_bm25 is installed; uninstall to exercise the ImportError path")
    with pytest.raises(ImportError, match="rank_bm25"):
        BM25Retriever([("q", "MATCH (n) RETURN n")])


def test_few_shot_index_from_corpus_files() -> None:
    """All three shipped corpora load; ≥44 examples; a known entry is present."""
    paths = sorted(CORPORA_DIR.glob("*.yml"))
    assert len(paths) == 3, f"expected 3 corpus files, got {len(paths)}: {paths}"
    index = FewShotIndex.from_corpus_files(paths)
    assert len(index.examples) >= 44

    cyphers = {cy for _q, cy in index.examples}
    assert any("DIRECTED" in cy and "The Matrix" in cy for cy in cyphers)
    assert any("Customer" in cy and "Germany" in cy for cy in cyphers)
    assert any("User" in cy and "Alice" in cy for cy in cyphers)


@pytest.mark.skipif(
    not _rank_bm25_available(),
    reason="rank_bm25 is required for this test",
)
def test_prompt_section_format() -> None:
    """Golden: for a question that exactly matches a corpus entry, the
    rendered section is byte-identical to the expected block.

    Using the shipped corpora and a question whose top-1 is unambiguous
    keeps this robust to BM25 tie-breaking (we only pin k=1).
    """
    paths = sorted(CORPORA_DIR.glob("*.yml"))
    index = FewShotIndex.from_corpus_files(paths)
    section = index.format_prompt_section("Who directed The Matrix?", k=1)
    expected = (
        "## Examples\n"
        "Q: Who directed The Matrix?\n"
        "```cypher\n"
        'MATCH (p:Person)-[:DIRECTED]->(m:Movie {title: "The Matrix"}) RETURN p.name\n'
        "```"
    )
    assert section == expected


def test_empty_corpus_graceful() -> None:
    """An empty retriever yields the empty string, never raises."""

    class _Empty:
        def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]:
            return []

    index = FewShotIndex(_Empty())
    assert index.format_prompt_section("anything") == ""
    assert index.format_prompt_section("anything", k=10) == ""


def test_nl_to_cypher_use_fewshot_false_is_bit_identical(movies_mapping) -> None:
    """With ``use_fewshot=False``, the system prompt matches the Wave 4-pre baseline.

    This guarantees that teams wanting to pin behavior (e.g. for
    prompt-caching A/B) can disable retrieval cleanly without the
    ``## Examples`` block sneaking back in.
    """
    from arango_cypher.nl2cypher import _build_schema_summary

    captured: dict[str, str] = {}

    class CapturingProvider:
        def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
            captured["system"] = system
            return (
                "```cypher\nMATCH (n:Person) RETURN n\n```",
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )

    result = nl_to_cypher(
        "List all people",
        mapping=movies_mapping,
        use_fewshot=False,
        llm_provider=CapturingProvider(),
    )
    assert result.method == "llm"

    schema_summary = _build_schema_summary(movies_mapping)
    expected = FROZEN_SYSTEM_PROMPT.replace("{schema}", schema_summary)
    assert captured["system"] == expected
    assert "## Examples" not in captured["system"]
