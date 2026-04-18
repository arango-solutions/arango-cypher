"""Few-shot example retrieval for the NL→Cypher prompt.

At prompt-construction time, pick the top-K (NL question → Cypher answer)
pairs from a curated seed corpus that are most similar to the user's
question, and inject them into :class:`PromptBuilder.few_shot_examples`.
This lifts the LLM out of zero-shot mode without leaking physical
schema details: the corpus contains only conceptual-schema Cypher.

``rank_bm25`` is imported lazily inside :class:`BM25Retriever` so this
module stays import-safe when the dependency is not installed. Callers
should use :meth:`FewShotIndex.from_corpus_files`, which transparently
downgrades to a no-op retriever if BM25 is unavailable.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Retriever(Protocol):
    """Protocol for retrievers that return (question, cypher) pairs."""

    def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]:
        ...


def _tokenize(text: str) -> list[str]:
    """Whitespace + lowercase tokenizer used for BM25 scoring."""
    return re.findall(r"[a-zA-Z0-9_]+", text.lower())


class BM25Retriever:
    """BM25-based retriever over a fixed list of ``(question, cypher)`` examples.

    ``rank_bm25`` is imported lazily so the module can be imported even
    when the dependency is not installed.  Attempting to *construct* a
    ``BM25Retriever`` without ``rank_bm25`` raises :class:`ImportError`
    with a helpful message.
    """

    def __init__(self, examples: list[tuple[str, str]]) -> None:
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise ImportError(
                "BM25Retriever requires the 'rank_bm25' package. "
                "Install it with `pip install rank_bm25>=0.2.2` or "
                "`pip install 'arango-cypher-py[dev]'`."
            ) from exc

        self._examples: list[tuple[str, str]] = list(examples)
        self._tokenized: list[list[str]] = [_tokenize(q) for q, _ in self._examples]
        safe_corpus = [toks or ["<empty>"] for toks in self._tokenized]
        self._bm25 = BM25Okapi(safe_corpus) if self._examples else None

    def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]:
        if not self._examples or self._bm25 is None or k <= 0:
            return []
        tokens = _tokenize(question)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            range(len(self._examples)),
            key=lambda i: (-float(scores[i]), i),
        )
        out: list[tuple[str, str]] = []
        for idx in ranked[:k]:
            if float(scores[idx]) <= 0.0:
                continue
            out.append(self._examples[idx])
        return out


class _NoopRetriever:
    """Fallback retriever used when BM25 / corpora are unavailable."""

    def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]:
        return []


class FewShotIndex:
    """A retrieval index wrapping a :class:`Retriever` implementation.

    Use :meth:`from_corpus_files` to build an index from one or more
    YAML corpus files. If ``rank_bm25`` is unavailable or all corpora
    are empty, the resulting index silently degrades to a no-op
    retriever — :meth:`retrieve` returns an empty list so the caller
    falls back to a zero-shot prompt.
    """

    def __init__(
        self,
        retriever: Retriever,
        examples: list[tuple[str, str]] | None = None,
    ) -> None:
        self._retriever = retriever
        if examples is not None:
            self._examples: list[tuple[str, str]] = list(examples)
        else:
            self._examples = list(getattr(retriever, "_examples", []))

    @property
    def examples(self) -> list[tuple[str, str]]:
        """Read-only view of all examples loaded into the index."""
        return list(self._examples)

    @classmethod
    def from_corpus_files(cls, paths: list[Path]) -> FewShotIndex:
        """Load YAML corpora and build a BM25-backed index.

        The corpus file shape is::

            version: 1
            examples:
              - question: "Find a person by name"
                cypher: 'MATCH (p:Person {name: "Tom Hanks"}) RETURN p'
        """
        examples: list[tuple[str, str]] = []
        try:
            import yaml
        except ImportError:
            logger.info("PyYAML not available; FewShotIndex falls back to no-op retriever.")
            return cls(_NoopRetriever())

        for path in paths:
            try:
                data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
            except Exception as exc:
                logger.info("Failed to load corpus %s: %s", path, exc)
                continue
            raw = data.get("examples") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                continue
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                q = entry.get("question")
                c = entry.get("cypher")
                if isinstance(q, str) and isinstance(c, str) and q.strip() and c.strip():
                    examples.append((q.strip(), c.strip()))

        if not examples:
            return cls(_NoopRetriever(), examples=[])

        try:
            retriever: Retriever = BM25Retriever(examples)
        except ImportError as exc:
            logger.info("rank_bm25 not installed; FewShotIndex degrades to no-op: %s", exc)
            retriever = _NoopRetriever()
        return cls(retriever, examples=examples)

    def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]:
        return self._retriever.retrieve(question, k=k)

    def format_prompt_section(self, question: str, k: int = 3) -> str:
        """Render the ``## Examples`` section for *question*.

        Returns the empty string when no examples match, matching the
        contract expected by :class:`PromptBuilder` — which suppresses
        the section entirely on an empty list.  When matches exist, the
        output is byte-identical to the section
        :class:`~arango_cypher.nl2cypher.PromptBuilder` would render for
        the same examples.
        """
        matches = self.retrieve(question, k=k)
        if not matches:
            return ""
        lines = ["## Examples"]
        for nl, cypher in matches:
            lines.append(f"Q: {nl}")
            lines.append("```cypher")
            lines.append(cypher.strip())
            lines.append("```")
        return "\n".join(lines)
