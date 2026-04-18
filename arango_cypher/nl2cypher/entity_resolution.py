"""Pre-flight entity resolution for the NL→Cypher pipeline (WP-25.2).

Before invoking the LLM, extract candidate entity mentions from the user
question (quoted strings, Title-Case phrases, capitalized proper nouns)
and try to resolve each one against the connected database's string
properties (``name``, ``title``, ``label``, …).  The resolved mentions
are rendered into :class:`PromptBuilder.resolved_entities` as a
``## Resolved entities`` section that sits between the schema and the
question — the LLM then writes its WHERE clauses against the *actual*
database strings ("Forrest Gump") instead of the user's wording
("Forest Gump").

Per PRD §1.2 the LLM still only sees conceptual schema details: the
resolved value is a property **value** (``"Forrest Gump"``), never a
collection name, type discriminator, or physical mapping detail.

The module degrades gracefully when no ``db`` handle is configured —
:meth:`EntityResolver.resolve` returns ``[]`` and the prompt reverts
to its pre-WP-25.2 shape.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from arango_query_core.mapping import MappingBundle, MappingResolver

logger = logging.getLogger(__name__)


_STRING_PROPERTY_CANDIDATES: tuple[str, ...] = (
    "name",
    "title",
    "label",
    "fullName",
    "full_name",
    "displayName",
    "display_name",
    "companyName",
    "company_name",
    "productName",
    "product_name",
)
"""Property names that are likely to hold human-readable strings worth resolving.

The resolver tries each of these, in order, as a first-pass heuristic
before falling back to "any string-typed property declared on the entity".
Keeping this small and intentional avoids flooding the DB with look-ups
on boolean/numeric columns.
"""


_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "is", "are", "was", "were",
    "in", "on", "at", "of", "to", "from", "with", "by", "for", "as",
    "who", "what", "when", "where", "why", "how", "which", "that",
    "all", "any", "some", "each", "every", "both", "find", "list",
    "show", "get", "return", "count", "how many", "give me", "tell me",
    "i", "me", "my", "we", "our", "you", "your",
})


_SENTENCE_INITIAL_FILLERS: frozenset[str] = frozenset({
    "did", "does", "do", "has", "have", "had", "is", "are", "was", "were",
    "can", "could", "should", "would", "will", "shall", "may", "might",
    "who", "what", "when", "where", "why", "how", "which", "that",
    "find", "list", "show", "get", "return", "count", "give", "tell",
    "please", "let", "i", "we",
})
"""Lowercased function words that, when they appear sentence-initial and get
auto-capitalized by convention, should NOT be treated as entity mentions.

So ``"Did Tom Hanks act in X?"`` resolves to ``["Tom Hanks", "X"]``, not
``["Did Tom Hanks", "X"]``.
"""


@dataclass
class ResolvedEntity:
    """One resolved (mention → database value) mapping."""

    mention: str
    label: str
    property: str
    value: str
    score: float = 0.0


class EntityResolver:
    """Extracts entity mentions from a question and resolves them against the DB.

    The resolver is safe to use offline: when ``db`` is ``None`` every call
    to :meth:`resolve` returns ``[]`` so the pipeline falls back to its
    pre-WP-25.2 zero-shot prompt.  Unit tests therefore never need a live
    database — they mock the ``db`` handle or pass ``None``.
    """

    def __init__(
        self,
        *,
        db: Any | None = None,
        mapping: MappingBundle | None = None,
        max_candidates: int = 5,
        min_score: float = 0.5,
        fuzzy_threshold: float = 0.7,
    ) -> None:
        """Initialize a resolver.

        Args:
            db: A python-arango ``StandardDatabase`` (or compatible mock).
                When ``None``, :meth:`resolve` returns ``[]``.
            mapping: The :class:`~arango_query_core.mapping.MappingBundle`
                for the dataset.  Required for any actual resolution.
            max_candidates: Cap on candidate mentions per question.
            min_score: Final-score floor — resolutions below this are
                dropped.  Default 0.5 keeps low-confidence guesses out.
            fuzzy_threshold: Minimum normalized similarity (1 −
                edit-distance / max-length) at which the AQL
                ``LEVENSHTEIN_DISTANCE`` branch contributes to the
                final score.  0.7 ≈ "≤ 30 % of characters differ"; this
                catches single-character typos in 4+ char names
                ("Forest"/"Forrest") and 1–2 char drops in 7+ char
                names ("Tom Hank"/"Tom Hanks") without lighting up on
                wholly unrelated strings.  The fuzzy contribution is
                also down-weighted by ``0.9`` so an exact (1.00) or
                substring (0.85) hit always wins when both fire.
        """
        self.db = db
        self.mapping = mapping
        self.max_candidates = max_candidates
        self.min_score = min_score
        self.fuzzy_threshold = fuzzy_threshold
        self._cache: dict[tuple[int, str], list[ResolvedEntity]] = {}
        self._schema_labels: set[str] = self._collect_schema_labels(mapping)
        self._resolver: MappingResolver | None = (
            MappingResolver(mapping) if mapping is not None else None
        )

    def _collect_schema_labels(self, mapping: MappingBundle | None) -> set[str]:
        """Return the lowercased set of conceptual labels + relationship types.

        Used by :meth:`extract_candidates` to reject schema-keyword
        false positives like "Person" in "Find all Person nodes".
        """
        if mapping is None:
            return set()
        labels: set[str] = set()
        cs = mapping.conceptual_schema or {}
        pm = mapping.physical_mapping or {}

        ents = cs.get("entities", [])
        if isinstance(ents, list):
            for e in ents:
                if isinstance(e, dict) and e.get("name"):
                    labels.add(str(e["name"]).lower())
        etypes = cs.get("entityTypes", [])
        if isinstance(etypes, list):
            labels.update(str(x).lower() for x in etypes if x)
        rels = cs.get("relationships", [])
        if isinstance(rels, list):
            for r in rels:
                if isinstance(r, dict) and r.get("type"):
                    labels.add(str(r["type"]).lower())
        rtypes = cs.get("relationshipTypes", [])
        if isinstance(rtypes, list):
            labels.update(str(x).lower() for x in rtypes if x)

        if isinstance(pm.get("entities"), dict):
            labels.update(k.lower() for k in pm["entities"])
        if isinstance(pm.get("relationships"), dict):
            labels.update(k.lower() for k in pm["relationships"])
        return labels

    def extract_candidates(self, question: str) -> list[str]:
        """Return a conservative list of candidate entity mentions.

        Extraction order (de-duplicated, max ``max_candidates``):

        1. Quoted substrings (single or double quotes).
        2. Consecutive Title-Case phrases of ≥2 tokens
           (``Tom Hanks``, ``The Matrix``).
        3. Single capitalized tokens that are not schema labels, not
           sentence-initial fillers, and not stop-words.

        The goal is high precision: better to miss a candidate than to
        drown the LLM in noise.  Schema keywords (``Person``, ``Movie``,
        ``ACTED_IN``) are explicitly rejected so `"Find all Person nodes"`
        doesn't resolve `"Person"` against itself.
        """
        if not question:
            return []

        seen: set[str] = set()
        out: list[str] = []

        def _valid(cand: str) -> bool:
            key = cand.lower()
            if not cand or key in seen:
                return False
            if key in self._schema_labels or key in _STOPWORDS:
                return False
            return len(cand) >= 2

        def _add(cand: str) -> None:
            cand = cand.strip().strip(".,;:!?")
            if not _valid(cand):
                return
            seen.add(cand.lower())
            out.append(cand)

        def _strip_sentence_initial_filler(phrase: str, offset: int) -> str:
            """Drop a leading filler token when the phrase is sentence-initial.

            ``"Did Tom Hanks"`` at offset 0 or after a sentence break becomes
            ``"Tom Hanks"``; ``"Tom Hanks"`` mid-sentence passes through.
            """
            tokens = phrase.split()
            if not tokens:
                return phrase
            first_low = tokens[0].lower()
            is_initial = (
                offset == 0
                or (offset >= 2 and question[offset - 2] in ".!?\n")
                or (offset >= 1 and question[offset - 1] == "\n")
            )
            if is_initial and first_low in _SENTENCE_INITIAL_FILLERS and len(tokens) > 1:
                return " ".join(tokens[1:])
            return phrase

        for m in re.finditer(r'"([^"]+)"|\'([^\']+)\'', question):
            _add(m.group(1) or m.group(2) or "")

        for m in re.finditer(
            r"\b([A-Z][a-zA-Z0-9]*(?:\s+(?:of|the|and|de|la|von|van)\s+[A-Z][a-zA-Z0-9]*"
            r"|\s+[A-Z][a-zA-Z0-9]*)+)\b",
            question,
        ):
            _add(_strip_sentence_initial_filler(m.group(1), m.start()))

        multi_word_tokens_lower: set[str] = set()
        for cand in out:
            for tok in cand.split():
                if len(tok) >= 2:
                    multi_word_tokens_lower.add(tok.lower())

        for m in re.finditer(r"\b([A-Z][a-zA-Z0-9_-]{2,})\b", question):
            tok = m.group(1)
            low = tok.lower()
            if low in multi_word_tokens_lower:
                continue
            start = m.start()
            is_initial = start == 0 or (start >= 1 and question[start - 1] in ".!?\n ")
            if is_initial and low in _SENTENCE_INITIAL_FILLERS:
                continue
            _add(tok)

        return out[: self.max_candidates]

    def _resolve_properties_for(self, label: str) -> list[str]:
        """Return string-property names to query for *label*, best first."""
        if self.mapping is None:
            return list(_STRING_PROPERTY_CANDIDATES)
        pm = self.mapping.physical_mapping or {}
        entity_map = pm.get("entities", {}) if isinstance(pm.get("entities"), dict) else {}
        decl = entity_map.get(label, {}) if isinstance(entity_map, dict) else {}
        props_meta = decl.get("properties", {}) if isinstance(decl, dict) else {}

        declared_names: list[str] = []
        if isinstance(props_meta, dict):
            for name, meta in props_meta.items():
                if isinstance(meta, dict):
                    ptype = str(meta.get("type", "string")).lower()
                    if ptype in ("string", "str", "text"):
                        declared_names.append(name)
                elif isinstance(meta, str):
                    if meta.lower() in ("string", "str", "text"):
                        declared_names.append(name)

        ordered: list[str] = []
        seen: set[str] = set()
        for cand in _STRING_PROPERTY_CANDIDATES:
            if cand in declared_names and cand not in seen:
                ordered.append(cand)
                seen.add(cand)
        for name in declared_names:
            if name not in seen:
                ordered.append(name)
                seen.add(name)
        return ordered or list(_STRING_PROPERTY_CANDIDATES)

    def _entity_labels(self) -> list[str]:
        """All conceptual entity labels present in the mapping."""
        if self.mapping is None:
            return []
        cs = self.mapping.conceptual_schema or {}
        pm = self.mapping.physical_mapping or {}
        labels: list[str] = []
        seen: set[str] = set()

        ents = cs.get("entities", [])
        if isinstance(ents, list):
            for e in ents:
                if isinstance(e, dict) and e.get("name"):
                    name = str(e["name"])
                    if name not in seen:
                        labels.append(name)
                        seen.add(name)
        if not labels:
            etypes = cs.get("entityTypes", [])
            if isinstance(etypes, list):
                for name in etypes:
                    if isinstance(name, str) and name not in seen:
                        labels.append(name)
                        seen.add(name)
        if not labels and isinstance(pm.get("entities"), dict):
            for name in pm["entities"]:
                if name not in seen:
                    labels.append(name)
                    seen.add(name)
        return labels

    def resolve(self, question: str) -> list[ResolvedEntity]:
        """Resolve each extracted candidate against the database.

        Returns an empty list when no DB is configured.  Otherwise, for
        each candidate, iterates over entity labels and their string
        properties, keeps the best match above ``min_score``, and caches
        results for subsequent calls with the same question.
        """
        if self.db is None or self.mapping is None:
            return []

        cache_key = (id(self.mapping), question)
        if cache_key in self._cache:
            return self._cache[cache_key]

        candidates = self.extract_candidates(question)
        if not candidates:
            self._cache[cache_key] = []
            return []

        labels = self._entity_labels()
        resolved: list[ResolvedEntity] = []
        for mention in candidates:
            best = self._resolve_single(mention, labels)
            if best is not None and best.score >= self.min_score:
                resolved.append(best)

        self._cache[cache_key] = resolved
        return resolved

    def _resolve_single(
        self,
        mention: str,
        labels: list[str],
    ) -> ResolvedEntity | None:
        """Probe each ``label × property`` pair and return the best hit, if any."""
        best: ResolvedEntity | None = None
        for label in labels:
            for prop in self._resolve_properties_for(label):
                hit = self._query_label_property(label, prop, mention)
                if hit is None:
                    continue
                value, score = hit
                if best is None or score > best.score:
                    best = ResolvedEntity(
                        mention=mention,
                        label=label,
                        property=prop,
                        value=value,
                        score=score,
                    )
                if best is not None and best.score >= 0.99:
                    return best
        return best

    def _query_label_property(
        self,
        label: str,
        prop: str,
        mention: str,
    ) -> tuple[str, float] | None:
        """Execute a single resolution query: ``label.prop`` ≈ ``mention``.

        Resolution goes through :class:`MappingResolver` so both
        ``COLLECTION`` (collection name == label) and ``LABEL`` (multiple
        labels share a physical collection, distinguished by a
        ``typeField``/``typeValue`` discriminator) styles are handled
        without the caller needing to know which style is in play.

        Combines four scoring strategies in pure AQL:

        * **exact** (1.00) — case-insensitive equality.
        * **contains** (0.85) — DB value contains the mention as a substring.
        * **reverse** (0.70) — mention contains the DB value as a substring.
        * **fuzzy**   (≤ 0.90) — ``LEVENSHTEIN_DISTANCE`` normalized by
          the longer of the two strings, gated by
          :attr:`fuzzy_threshold` and down-weighted by ``0.9`` so an
          exact / substring hit always wins when both fire.  This is
          the path that catches typos like "Forest Gump" → "Forrest
          Gump" against a live DB.

        The final ``score`` is ``MAX(exact, contains, reverse, fuzzy)``.
        We keep the initial implementation pure-AQL to avoid a hard
        dependency on ArangoSearch view creation; the resolver can be
        upgraded to SEARCH/BM25/NGRAM later without changing this
        public contract.
        """
        if self._resolver is None or self.db is None:
            return None

        try:
            emap = self._resolver.resolve_entity(label)
        except Exception:
            return None

        collection = emap.get("collectionName") or label
        if not collection:
            return None

        type_field = emap.get("typeField")
        type_value = emap.get("typeValue")
        props_meta = emap.get("properties", {}) if isinstance(emap.get("properties"), dict) else {}
        meta = props_meta.get(prop, {}) if isinstance(props_meta, dict) else {}
        field_name = str(meta["field"]) if isinstance(meta, dict) and meta.get("field") else prop

        type_filter = (
            "  FILTER d[@type_field] == @type_value\n"
            if type_field and type_value else ""
        )

        aql = (
            f"FOR d IN @@c\n"
            f"{type_filter}"
            f"  FILTER HAS(d, @field) AND IS_STRING(d[@field])\n"
            f"  LET lv = LOWER(d[@field])\n"
            f"  LET lm = LOWER(@m)\n"
            f"  LET exact = lv == lm ? 1.0 : 0.0\n"
            f"  LET contains = CONTAINS(lv, lm) ? 0.85 : 0.0\n"
            f"  LET reverse = CONTAINS(lm, lv) ? 0.7 : 0.0\n"
            f"  LET maxlen = LENGTH(lv) > LENGTH(lm) ? LENGTH(lv) : LENGTH(lm)\n"
            f"  LET fuzzy_raw = maxlen > 0 "
            f"? 1.0 - (LEVENSHTEIN_DISTANCE(lv, lm) * 1.0 / maxlen) : 0.0\n"
            f"  LET fuzzy = fuzzy_raw >= @fuzzy_threshold ? fuzzy_raw * 0.9 : 0.0\n"
            f"  LET score = MAX([exact, contains, reverse, fuzzy])\n"
            f"  FILTER score > 0\n"
            f"  SORT score DESC\n"
            f"  LIMIT 1\n"
            f"  RETURN {{value: d[@field], score: score}}"
        )
        bind_vars: dict[str, Any] = {
            "@c": collection,
            "field": field_name,
            "m": mention,
            "fuzzy_threshold": self.fuzzy_threshold,
        }
        if type_field and type_value:
            bind_vars["type_field"] = type_field
            bind_vars["type_value"] = type_value

        try:
            cursor = self.db.aql.execute(aql, bind_vars=bind_vars)
            rows = list(cursor)
        except Exception as exc:
            logger.info(
                "EntityResolver query failed for %s.%s ~= %r: %s",
                label, field_name, mention, exc,
            )
            return None

        if not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict):
            return None
        value = row.get("value")
        score = row.get("score")
        if not isinstance(value, str):
            return None
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            return None
        return value, score_f

    def format_prompt_section(self, resolved: list[ResolvedEntity]) -> list[str]:
        """Render resolved entities as individual bullet strings.

        The returned list is fed directly into
        :class:`~arango_cypher.nl2cypher.PromptBuilder.resolved_entities`,
        which wraps them in the ``## Resolved entities`` header.  Keeping
        the section header owned by :class:`PromptBuilder` ensures the
        schema-first ordering required by WP-25.4 prompt caching.
        """
        out: list[str] = []
        for r in resolved:
            out.append(
                f'"{r.mention}" → {r.label}.{r.property} = "{r.value}" '
                f"(similarity {r.score:.2f})"
            )
        return out


@dataclass
class _NullResolver:
    """A zero-cost resolver used when no DB is configured.

    Kept as a concrete class (instead of ``None``) so call sites can
    unconditionally invoke ``.resolve(q)`` without branching.  The
    NL→Cypher core code still honours the ``use_entity_resolution`` flag
    for bit-identity — this is just a convenience.
    """

    entities: list[ResolvedEntity] = field(default_factory=list)

    def resolve(self, question: str) -> list[ResolvedEntity]:
        return []

    def format_prompt_section(self, resolved: list[ResolvedEntity]) -> list[str]:
        return []
