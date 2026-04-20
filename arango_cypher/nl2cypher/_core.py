"""NL→Cypher core pipeline: schema summarization, prompt building, rule-based fallback."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from arango_query_core.mapping import MappingBundle

from .providers import (
    LLMProvider,
    _get_default_provider,
)

if TYPE_CHECKING:
    from .entity_resolution import EntityResolver
    from .fewshot import FewShotIndex

logger = logging.getLogger(__name__)


@dataclass
class NL2CypherResult:
    """Result of a natural language to Cypher translation."""
    cypher: str
    explanation: str = ""
    confidence: float = 0.0
    method: str = "rule_based"
    schema_context: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    retries: int = 0
    cached_tokens: int = 0
    """Prompt tokens served from the provider's prefix cache (WP-25.4).

    Populated when the LLM provider surfaces a ``cached_tokens`` field
    on its usage payload (OpenAI's ``prompt_tokens_details.cached_tokens``,
    Anthropic's ``cache_read_input_tokens``, …).  ``0`` for rule-based
    results and for providers that don't expose cache telemetry.
    """


def _property_quality_hint(prop_meta: dict[str, Any] | None) -> str:
    """Render a compact data-quality hint suffix for a property.

    Returns a string like ``" [sentinels: 'NULL', 'N/A'; numeric-like]"`` when
    the physical mapping carries sentinel/numeric-like metadata, otherwise
    the empty string.
    """
    if not isinstance(prop_meta, dict):
        return ""
    parts: list[str] = []
    sentinels = prop_meta.get("sentinelValues") or prop_meta.get("sentinel_values")
    if isinstance(sentinels, list | tuple) and sentinels:
        quoted = ", ".join(f"'{s}'" for s in list(sentinels)[:3])
        parts.append(f"sentinels: {quoted}")
    if prop_meta.get("numericLike") or prop_meta.get("numeric_like"):
        parts.append("numeric-like string")
    if not parts:
        return ""
    return f" [{'; '.join(parts)}]"


def _pm_entity_props(label: str, pm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if isinstance(pm.get("entities"), dict):
        ent = pm["entities"].get(label, {})
        if isinstance(ent, dict):
            props = ent.get("properties", {})
            if isinstance(props, dict):
                return {k: v for k, v in props.items() if isinstance(v, dict)}
    return {}


def _pm_relationship_props(rtype: str, pm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if isinstance(pm.get("relationships"), dict):
        rel = pm["relationships"].get(rtype, {})
        if isinstance(rel, dict):
            props = rel.get("properties", {})
            if isinstance(props, dict):
                return {k: v for k, v in props.items() if isinstance(v, dict)}
    return {}


def _flagged_properties(
    labeled_props: dict[str, dict[str, dict[str, Any]]],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Flatten (label, propName, meta) for properties carrying quality hints."""
    flagged: list[tuple[str, str, dict[str, Any]]] = []
    for label, props in labeled_props.items():
        for pname, meta in props.items():
            if not isinstance(meta, dict):
                continue
            has_sentinel = bool(meta.get("sentinelValues") or meta.get("sentinel_values"))
            has_numeric = bool(meta.get("numericLike") or meta.get("numeric_like"))
            if has_sentinel or has_numeric:
                flagged.append((label, pname, meta))
    return flagged


_DATA_QUALITY_BLOCK_CYPHER = (
    "\nData-quality hints:\n"
    "  - When a property is marked 'sentinels: ...', the column uses string "
    "placeholder(s) for missing values (e.g. the literal text 'NULL'). These "
    "are NOT real nulls — exclude them in WHERE clauses, for example "
    "`WHERE t.COMPANY_SIZE <> 'NULL' AND t.COMPANY_SIZE IS NOT NULL`.\n"
    "  - When a property is marked 'numeric-like string', cast to number "
    "before ordering or comparing numerically, e.g. "
    "`toInteger(t.COMPANY_SIZE)` or `toFloat(t.AMOUNT)`.\n"
    "  - For 'top-N by numeric field X', combine both: filter out the "
    "sentinels, then ORDER BY the cast numeric value."
)


def _build_schema_summary(bundle: MappingBundle) -> str:
    """Build a conceptual-only schema description for LLM context.

    Per §1.2, the LLM prompt contains only conceptual labels, relationship
    types, properties (by conceptual name), and domain/range — never
    physical collection names, mapping styles, typeField/typeValue, physical
    field names, or other physical mapping details.  The LLM generates
    pure Cypher against the ontology; the transpiler handles the physical
    mapping.

    Data-quality hints (string sentinels, numeric-like string columns) are
    included in the property listing so the LLM can emit the right filters
    and casts.
    """
    cs = bundle.conceptual_schema
    pm = bundle.physical_mapping
    lines: list[str] = ["Graph schema (Cypher labels and relationship types):"]

    entities_emitted: list[str] = []

    def _format_entity(label: str, prop_names: list[str]) -> str:
        pm_props = _pm_entity_props(label, pm)
        parts: list[str] = []
        for name in prop_names[:8]:
            hint = _property_quality_hint(pm_props.get(name))
            parts.append(f"{name}{hint}")
        prop_str = ", ".join(parts) if parts else "no properties"
        return f"  Node :{label} ({prop_str})"

    cs_entities = cs.get("entities", [])
    cs_entity_types = cs.get("entityTypes", [])
    if isinstance(cs_entities, list) and cs_entities and isinstance(cs_entities[0], dict):
        for e in cs_entities:
            name = e.get("name", "")
            props = [p.get("name", "") for p in e.get("properties", []) if isinstance(p, dict)]
            if not props:
                props = _conceptual_props_for(name, cs, pm)
            lines.append(_format_entity(name, props))
            entities_emitted.append(name)
    elif isinstance(cs_entity_types, list) and cs_entity_types:
        for name in cs_entity_types:
            props = _conceptual_props_for(name, cs, pm)
            lines.append(_format_entity(name, props))
            entities_emitted.append(name)

    if not entities_emitted and isinstance(pm.get("entities"), dict):
        for name in pm["entities"]:
            props = _conceptual_props_for(name, cs, pm)
            lines.append(_format_entity(name, props))
            entities_emitted.append(name)

    cs_rels = cs.get("relationships", [])
    cs_rel_types = cs.get("relationshipTypes", [])
    if isinstance(cs_rels, list) and cs_rels and isinstance(cs_rels[0], dict):
        for r in cs_rels:
            rtype = r.get("type", "")
            from_e = r.get("fromEntity", "?")
            to_e = r.get("toEntity", "?")
            rprops = [
                p.get("name", "") for p in r.get("properties", [])
                if isinstance(p, dict) and p.get("name")
            ]
            pm_rprops = _pm_relationship_props(rtype, pm)
            formatted = [
                f"{n}{_property_quality_hint(pm_rprops.get(n))}" for n in rprops
            ]
            prop_str = f" [{', '.join(formatted)}]" if formatted else ""
            lines.append(f"  (:{from_e})-[:{rtype}{prop_str}]->(:{to_e})")
    elif isinstance(cs_rel_types, list) and cs_rel_types:
        for rtype in cs_rel_types:
            from_e, to_e = _conceptual_domain_range(rtype, cs, pm)
            lines.append(f"  (:{from_e})-[:{rtype}]->(:{to_e})")
    elif isinstance(pm.get("relationships"), dict):
        for rtype in pm["relationships"]:
            from_e, to_e = _conceptual_domain_range(rtype, cs, pm)
            lines.append(f"  (:{from_e})-[:{rtype}]->(:{to_e})")

    labeled_ent_props = {
        label: _pm_entity_props(label, pm) for label in entities_emitted
    }
    if _flagged_properties(labeled_ent_props):
        lines.append(_DATA_QUALITY_BLOCK_CYPHER)

    return "\n".join(lines)


def _conceptual_props_for(
    label: str, cs: dict[str, Any], pm: dict[str, Any],
) -> list[str]:
    """Return property names for a conceptual label (max 8).

    Prefers conceptual schema properties; falls back to physical mapping
    property *names* (which are conceptual property names, not field names).
    """
    for e in cs.get("entities", []):
        if isinstance(e, dict) and e.get("name") == label:
            props = [p.get("name", "") for p in e.get("properties", []) if isinstance(p, dict)]
            if props:
                return props[:8]

    if isinstance(pm.get("entities"), dict):
        pme = pm["entities"].get(label, {})
        if isinstance(pme, dict):
            return list(pme.get("properties", {}).keys())[:8]
    return []


def _conceptual_domain_range(
    rtype: str, cs: dict[str, Any], pm: dict[str, Any],
) -> tuple[str, str]:
    """Return (domain, range) for a relationship using conceptual metadata."""
    for r in cs.get("relationships", []):
        if isinstance(r, dict) and r.get("type") == rtype:
            return r.get("fromEntity", "?"), r.get("toEntity", "?")
    if isinstance(pm.get("relationships"), dict):
        pmr = pm["relationships"].get(rtype, {})
        if isinstance(pmr, dict):
            return pmr.get("domain", "?"), pmr.get("range", "?")
    return "?", "?"


_SYSTEM_PROMPT = """You are a Cypher query expert. Given a natural language question and a graph schema, generate a valid Cypher query.

Rules:
- Use only node labels and relationship types from the schema
- Use property names from the schema
- Return a single Cypher query (no explanation)
- Use standard Cypher syntax (MATCH, WHERE, RETURN, ORDER BY, LIMIT, etc.)
- For counts, use count()
- For aggregations, use collect(), sum(), avg(), min(), max()
- Wrap the query in ```cypher``` code block

{schema}"""


_RETRY_USER_SUFFIX = (
    "\n\nYour previous Cypher was invalid: {error}. Please fix it."
)


@dataclass
class PromptBuilder:
    """Composable prompt builder for the NL→Cypher pipeline.

    Renders the ``system`` and ``user`` messages used by :func:`nl_to_cypher`.
    Per PRD §1.2, only *conceptual* schema text ever reaches this builder —
    collection names, type discriminators, and AQL stay out of the prompt.

    Invariants
    ----------
    * Zero-shot case (empty ``few_shot_examples`` and ``resolved_entities``):
      ``render_system()`` is byte-identical to the pre-refactor rendering
      of the :data:`_SYSTEM_PROMPT` template against ``schema_summary``.
      This is pinned by :mod:`tests.test_nl2cypher_prompt_builder`.
    * ``retry_context`` never alters the system message; it is appended to
      the user message only, matching the historical retry wording.
    """

    schema_summary: str
    few_shot_examples: list[tuple[str, str]] = field(default_factory=list)
    resolved_entities: list[str] = field(default_factory=list)
    retry_context: str = ""

    def render_system(self) -> str:
        base = _SYSTEM_PROMPT.replace("{schema}", self.schema_summary)

        extensions: list[str] = []
        if self.few_shot_examples:
            extensions.append(self._render_few_shot_section())
        if self.resolved_entities:
            # TODO(WP-25.2): entity-resolution plug-in point — ER wave
            # will pre-resolve mentions in the question to canonical
            # conceptual entities before prompting.
            extensions.append(self._render_resolved_entities_section())

        if not extensions:
            return base
        return base + "\n\n" + "\n\n".join(extensions)

    def render_user(self, question: str) -> str:
        if not self.retry_context:
            return question
        return question + _RETRY_USER_SUFFIX.format(error=self.retry_context)

    def _render_few_shot_section(self) -> str:
        lines = ["## Examples"]
        for nl, cypher in self.few_shot_examples:
            lines.append(f"Q: {nl}")
            lines.append("```cypher")
            lines.append(cypher.strip())
            lines.append("```")
        return "\n".join(lines)

    def _render_resolved_entities_section(self) -> str:
        lines = ["## Resolved entities"]
        for entry in self.resolved_entities:
            lines.append(f"- {entry}")
        return "\n".join(lines)


def _extract_cypher_from_response(text: str) -> str:
    """Extract Cypher query from LLM response (handles code blocks)."""
    m = re.search(r"```(?:cypher)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    cypher_lines = []
    for line in lines:
        upper = line.upper()
        if any(kw in upper for kw in ("MATCH", "RETURN", "WHERE", "WITH", "OPTIONAL", "ORDER", "LIMIT", "UNWIND", "CREATE", "SET", "DELETE")):
            cypher_lines.append(line)
        elif cypher_lines:
            cypher_lines.append(line)
    return "\n".join(cypher_lines) if cypher_lines else text.strip()


def _validate_cypher(cypher: str) -> tuple[bool, str]:
    """Syntactic check using the ANTLR parser.

    Returns ``(ok, error_message)``.  On success *error_message* is empty.
    Falls back to a keyword heuristic when the parser is unavailable.
    """
    if not cypher or not cypher.strip():
        return False, "empty Cypher string"
    try:
        from arango_cypher.parser import parse_cypher
        parse_cypher(cypher)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _validate_via_explain(
    cypher: str,
    *,
    mapping: MappingBundle | None,
    db: Any | None,
) -> tuple[bool, str]:
    """Execution-grounded validation via :func:`explain_aql` (WP-25.3).

    Returns ``(True, "")`` in two cases: the validation step is skipped
    (no *db* or *mapping*), or EXPLAIN succeeded.  When either the
    Cypher→AQL transpile step or EXPLAIN itself fails, returns
    ``(False, short_error_message)`` suitable for LLM feedback.

    Translation errors are surfaced separately from EXPLAIN errors so
    the LLM can tell whether it broke the Cypher shape (transpile) or
    merely hallucinated a collection / property name (EXPLAIN).
    """
    if db is None or mapping is None:
        return True, ""
    try:
        from arango_cypher.api import translate
        from arango_query_core.exec import explain_aql
    except Exception as exc:
        logger.info("execution-grounded validation unavailable: %s", exc)
        return True, ""
    try:
        tq = translate(cypher, mapping=mapping)
    except Exception as exc:
        msg = str(exc) or exc.__class__.__name__
        msg = msg.splitlines()[0] if "\n" in msg else msg
        return False, f"Cypher did not transpile to AQL: {msg[:300]}"
    try:
        ok, err = explain_aql(db, tq.aql, tq.bind_vars or {})
    except Exception as exc:
        logger.info("EXPLAIN step raised unexpectedly: %s", exc)
        return True, ""
    return ok, err


def _fix_labels(cypher: str, ctx: _SchemaCtx) -> str:
    """Rewrite Cypher labels that don't exist in the mapping to the closest match.

    Handles common LLM hallucinations like ``Actor`` → ``Person`` by using the
    same fuzzy matching the rule-based engine uses, plus role-synonym lookup.
    """
    def _replace_label(m: re.Match) -> str:
        prefix = m.group(1)
        label = m.group(2)
        if label.lower() in ctx.entities:
            return prefix + ctx.entities[label.lower()]["name"]
        if label.lower() in {r["type"].lower() for r in ctx.relationships.values()}:
            return prefix + label
        role = label.lower().rstrip("s")
        rel = ctx.role_to_rel.get(role)
        if rel:
            from_e = rel.get("fromEntity", "")
            if from_e and from_e != "Any":
                return prefix + from_e
        ent = _match_entity(label, ctx.entities)
        if ent:
            return prefix + ent["name"]
        return prefix + label

    return re.sub(r"((?:\(|\[)[a-zA-Z0-9_]*:)([A-Z]\w*)", _replace_label, cypher)


def _call_llm_with_retry(
    question: str,
    schema_summary: str,
    provider: LLMProvider,
    max_retries: int = 2,
    ctx: _SchemaCtx | None = None,
    few_shot_examples: list[tuple[str, str]] | None = None,
    resolved_entities: list[str] | None = None,
    mapping: MappingBundle | None = None,
    db: Any | None = None,
) -> NL2CypherResult | None:
    """Call the LLM provider with parse + execution-grounded validation and retry.

    After each LLM call the generated Cypher is parsed via the ANTLR
    grammar.  On ANTLR failure the parse error is fed back.  On ANTLR
    success, if both *mapping* and *db* are available (WP-25.3), the
    Cypher is translated to AQL and planned via ``POST /_api/explain``;
    EXPLAIN errors (missing collections, unknown properties, bad
    traversals) feed back into the retry prompt the same way parse
    errors do.  With no *db* the EXPLAIN step is skipped and the
    behaviour is identical to the pre-WP-25.3 pipeline.

    The prompt is assembled by a :class:`PromptBuilder` shared across
    attempts; only ``retry_context`` mutates between iterations so the
    (cacheable) system prefix stays byte-stable.

    The ``max_retries`` budget is shared across both failure kinds —
    a query that fails ANTLR on attempt 1 and EXPLAIN on attempt 2 with
    ``max_retries=2`` gets one more try before falling through.
    """
    builder = PromptBuilder(
        schema_summary=schema_summary,
        few_shot_examples=list(few_shot_examples) if few_shot_examples else [],
        resolved_entities=list(resolved_entities) if resolved_entities else [],
    )
    best_cypher = ""
    best_content = ""
    total_usage: dict[str, int] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }
    for attempt in range(1 + max_retries):
        try:
            system = builder.render_system()
            user = builder.render_user(question)

            result = provider.generate(system, user)
            if isinstance(result, tuple):
                content, usage = result
                for k in total_usage:
                    total_usage[k] += int(usage.get(k, 0) or 0)
            else:
                content = result

            cypher = _extract_cypher_from_response(content)

            if ctx:
                cypher = _fix_labels(cypher, ctx)

            if not best_cypher:
                best_cypher = cypher
                best_content = content

            ok, err_msg = _validate_cypher(cypher)
            if ok:
                explain_ok, explain_err = _validate_via_explain(
                    cypher, mapping=mapping, db=db,
                )
                if explain_ok:
                    return NL2CypherResult(
                        cypher=cypher,
                        explanation=content,
                        confidence=0.8,
                        method="llm",
                        schema_context=schema_summary,
                        prompt_tokens=total_usage["prompt_tokens"],
                        completion_tokens=total_usage["completion_tokens"],
                        total_tokens=total_usage["total_tokens"],
                        retries=attempt,
                        cached_tokens=total_usage["cached_tokens"],
                    )
                best_cypher = cypher
                best_content = content
                builder.retry_context = (
                    f"Translated AQL failed EXPLAIN: {explain_err}. "
                    f"The Cypher was: {cypher}. Please revise your Cypher."
                )
                logger.info(
                    "LLM attempt %d/%d: EXPLAIN failed: %s",
                    attempt + 1, 1 + max_retries, explain_err[:120],
                )
                continue

            best_cypher = cypher
            best_content = content
            builder.retry_context = err_msg or "generated text did not parse as Cypher"
            logger.info(
                "LLM attempt %d/%d: validation failed for: %s",
                attempt + 1, 1 + max_retries, cypher[:120],
            )
        except Exception as e:
            logger.warning("LLM call failed (attempt %d): %s", attempt + 1, e)
            builder.retry_context = str(e)

    if best_cypher:
        return NL2CypherResult(
            cypher=best_cypher,
            explanation=f"WARNING: Cypher failed validation after {1 + max_retries} attempts. "
                        f"Last error: {builder.retry_context}\n\n{best_content}",
            confidence=0.3,
            method="llm",
            schema_context=schema_summary,
            prompt_tokens=total_usage["prompt_tokens"],
            completion_tokens=total_usage["completion_tokens"],
            total_tokens=total_usage["total_tokens"],
            retries=max_retries,
            cached_tokens=total_usage["cached_tokens"],
        )
    return None


def _build_schema_context(bundle: MappingBundle) -> _SchemaCtx:
    """Extract entities, relationships, and derived lookup tables from the mapping."""
    cs = bundle.conceptual_schema
    pm = bundle.physical_mapping

    entities: dict[str, dict] = {}
    cs_entities = cs.get("entities", [])
    cs_entity_types = cs.get("entityTypes", [])
    if isinstance(cs_entities, list) and cs_entities and isinstance(cs_entities[0], dict):
        entities = {e["name"].lower(): e for e in cs_entities if "name" in e}
    elif isinstance(cs_entity_types, list):
        for name in cs_entity_types:
            entities[name.lower()] = {"name": name, "properties": []}
    if not entities and isinstance(pm.get("entities"), dict):
        for name in pm["entities"]:
            entities[name.lower()] = {"name": name, "properties": []}

    relationships: dict[str, dict] = {}
    cs_rels = cs.get("relationships", [])
    cs_rel_types = cs.get("relationshipTypes", [])
    if isinstance(cs_rels, list) and cs_rels and isinstance(cs_rels[0], dict):
        relationships = {r["type"].lower(): r for r in cs_rels if "type" in r}
    elif isinstance(cs_rel_types, list):
        for rtype in cs_rel_types:
            pm_rel = pm.get("relationships", {}).get(rtype, {}) if isinstance(pm.get("relationships"), dict) else {}
            relationships[rtype.lower()] = {
                "type": rtype,
                "fromEntity": pm_rel.get("domain", "Any"),
                "toEntity": pm_rel.get("range", "Any"),
                "properties": [],
            }
    if not relationships and isinstance(pm.get("relationships"), dict):
        for rtype, pm_rel in pm["relationships"].items():
            relationships[rtype.lower()] = {
                "type": rtype,
                "fromEntity": pm_rel.get("domain", "Any"),
                "toEntity": pm_rel.get("range", "Any"),
                "properties": [],
            }

    _ROLE_SYNONYMS: dict[str, list[str]] = {
        "acted_in": ["actor", "actress", "cast", "star", "performer"],
        "directed": ["director"],
        "produced": ["producer"],
        "wrote": ["writer", "author", "screenwriter"],
        "reviewed": ["reviewer", "critic"],
        "follows": ["follower"],
        "knows": ["friend", "acquaintance", "contact"],
    }
    role_to_rel: dict[str, dict] = {}
    for rkey, rdef in relationships.items():
        for _synonyms in _ROLE_SYNONYMS.values():
            normalized_rkey = rkey.replace("_", "")
            for syn_key, syn_list in _ROLE_SYNONYMS.items():
                if syn_key.replace("_", "") == normalized_rkey or syn_key == rkey:
                    for s in syn_list:
                        role_to_rel[s] = rdef
                    break

    return _SchemaCtx(entities=entities, relationships=relationships,
                      role_to_rel=role_to_rel, pm=pm)


@dataclass
class _SchemaCtx:
    entities: dict[str, dict]
    relationships: dict[str, dict]
    role_to_rel: dict[str, dict]
    pm: dict[str, Any]


def _extract_filter_value(text: str) -> str:
    """Extract a meaningful filter value from text, stripping articles and entity nouns."""
    text = re.sub(r"^(?:the|a|an|some|all|any)\s+", "", text.strip())
    text = re.sub(r"\s+(?:movie|movies|film|films|person|persons|people)s?$", "", text, flags=re.I)
    return text.strip()


def _find_rel_for_verb(verb: str, relationships: dict[str, dict]) -> dict | None:
    """Map a verb from the question to a relationship using verb stems and synonyms."""
    verb = verb.lower().rstrip("s").rstrip("ed")
    _VERB_TO_REL: dict[str, str] = {
        "act": "acted_in", "star": "acted_in", "appear": "acted_in",
        "direct": "directed", "helm": "directed",
        "produc": "produced", "made": "produced",
        "writ": "wrote", "wrot": "wrote", "pen": "wrote",
        "review": "reviewed", "rat": "reviewed", "critiqu": "reviewed",
        "follow": "follows",
        "know": "knows",
    }
    for stem, rel_key in _VERB_TO_REL.items():
        if verb.startswith(stem) or stem.startswith(verb):
            if rel_key in relationships:
                return relationships[rel_key]
    rel = _match_relationship(verb, relationships)
    if rel:
        return rel
    return None


def _rule_based_translate(question: str, bundle: MappingBundle) -> NL2CypherResult:
    """Rule-based fallback: pattern-match common natural language queries."""
    ctx = _build_schema_context(bundle)
    entities = ctx.entities
    relationships = ctx.relationships
    role_to_rel = ctx.role_to_rel

    q = question.lower().strip().rstrip("?").rstrip(".")
    wants_single = bool(re.search(r"\b(?:a\s+(?:random|single|sample)|one)\b", q))

    _VERB_PHRASES = (
        r"(?:were|was|are|is)\s+in"
        r"|act(?:ed)?\s+in|star(?:red)?\s+in|appear(?:ed)?\s+in"
        r"|direct(?:ed)?|produc(?:ed)?|writ(?:ten|e|ten)?|wrote"
        r"|review(?:ed)?|follow(?:ed|s)?|know[sn]?"
    )
    m = re.match(
        r"(?:which|what|find|list|show)\s+(\w+)\s+"
        r"(?:(?:that|who|which)\s+)?"
        rf"(?:{_VERB_PHRASES})"
        r"\s*(.*)",
        q,
    )
    if m:
        role_word = m.group(1).rstrip("s")
        filter_text = _extract_filter_value(m.group(2)) if m.group(2).strip() else ""
        verb_match = re.search(
            r"((?:were|was|are|is)\s+in|acted?\s*in|starred?\s*in|appeared?\s*in"
            r"|directed?|produced?|wrote?|written|reviewed?|followed?|known?)", q,
        )
        rel = role_to_rel.get(role_word)
        if not rel and verb_match:
            verb_text = verb_match.group(1).split()[0]
            rel = _find_rel_for_verb(verb_text, relationships)
        if not rel:
            rel = _find_rel_for_verb(role_word, relationships)
        if rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                cypher = f"MATCH (a:{from_e})-[:{rel['type']}]->(b:{to_e})"
                if filter_text:
                    cypher += f"\nWHERE toLower(b.title) CONTAINS '{filter_text}' OR toLower(b.name) CONTAINS '{filter_text}'"
                cypher += "\nRETURN a"
                return NL2CypherResult(
                    cypher=cypher, explanation=f"Find {from_e} via {rel['type']}", confidence=0.6, method="rule_based",
                )

    m = re.match(
        r"(?:which|what)\s+(\w+)\s+"
        r"(?:did|has|have|had|does|do)\s+"
        r"(.+?)\s+"
        rf"(?:{_VERB_PHRASES})"
        r"\s*(.*)",
        q,
    )
    if m:
        entity_hint = m.group(1).rstrip("s")
        person_name = m.group(2).strip()
        entity = _match_entity(entity_hint, entities)
        verb_match = re.search(
            r"((?:were|was)\s+in|act(?:ed)?\s*in|star(?:red)?\s*in"
            r"|direct(?:ed)?|produc(?:ed)?|writ(?:ten|e)?|review(?:ed)?|follow(?:ed)?)", q,
        )
        rel = _find_rel_for_verb(verb_match.group(1).split()[0], relationships) if verb_match else None
        if entity and rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                target_label = entity["name"]
                source_label = from_e if target_label != from_e else to_e
                cypher = (
                    f"MATCH (a:{source_label})-[:{rel['type']}]->(b:{target_label})\n"
                    f"WHERE toLower(a.name) CONTAINS '{person_name}'\n"
                    f"RETURN b"
                )
                return NL2CypherResult(
                    cypher=cypher, explanation=f"Find {target_label} via {rel['type']}", confidence=0.6, method="rule_based",
                )

    m = re.match(r"who\s+(\w+)\s+(?:in\s+)?(.+)", q)
    if m:
        verb = m.group(1)
        filter_text = _extract_filter_value(m.group(2))
        rel = _find_rel_for_verb(verb, relationships)
        if rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                cypher = (
                    f"MATCH (a:{from_e})-[:{rel['type']}]->(b:{to_e})\n"
                    f"WHERE toLower(b.title) CONTAINS '{filter_text}' OR toLower(b.name) CONTAINS '{filter_text}'\n"
                    f"RETURN a"
                )
                return NL2CypherResult(
                    cypher=cypher, explanation=f"Find who {verb} {filter_text}", confidence=0.5, method="rule_based",
                )

    q = re.sub(
        r"^(?:(?:can\s+you\s+)?(?:please\s+)?(?:give\s+me|show\s+me|get\s+me|tell\s+me|i\s+(?:want|need))\s+)",
        "get ", q,
    )

    explicit_limit: int | None = None
    limit_m = re.match(r"^(\w+\s+)(\d+)\s+", q)
    if limit_m:
        explicit_limit = int(limit_m.group(2))
        q = limit_m.group(1) + q[limit_m.end():]

    q = re.sub(r"^(\w+\s+)(?:(?:a|an)\s+(?:random|single|sample)\s+|the\s+|an?\s+)", r"\1", q)
    q = re.sub(r"^(\w+\s+)(?:some|any)\s+", r"\1all ", q)
    q = re.sub(r"^(\w+\s+)(?:random|sample)\s+", r"\1", q)

    m = re.match(r"(?:find|list|show|get|return|fetch|display|retrieve|select|which|what)\s+(?:all\s+)?(\w+)s?\b(.*)", q)
    if m:
        entity_hint = m.group(1)
        rest = m.group(2).strip()

        role_word = entity_hint.rstrip("s")
        rel = role_to_rel.get(role_word)
        if rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                filter_text = _extract_filter_value(rest) if rest else ""
                cypher = f"MATCH (a:{from_e})-[:{rel['type']}]->(b:{to_e})"
                if filter_text:
                    cypher += f"\nWHERE toLower(b.title) CONTAINS '{filter_text}' OR toLower(b.name) CONTAINS '{filter_text}'"
                cypher += "\nRETURN a"
                return NL2CypherResult(
                    cypher=cypher, explanation=f"Find {from_e} via {rel['type']}", confidence=0.5, method="rule_based",
                )

        entity = _match_entity(entity_hint, entities)
        if entity:
            name = entity["name"]
            props = [p["name"] for p in entity.get("properties", [])[:5]]
            ret = ", ".join(f"n.{p}" for p in props) if props else "n"
            if explicit_limit:
                limit = f"\nLIMIT {explicit_limit}"
            elif wants_single:
                limit = "\nLIMIT 1"
            else:
                limit = ""

            if rest:
                where = _parse_simple_filter(rest, "n")
                if where:
                    return NL2CypherResult(
                        cypher=f"MATCH (n:{name})\nWHERE {where}\nRETURN {ret}{limit}",
                        explanation=f"Find {name} nodes with filter",
                        confidence=0.5, method="rule_based",
                    )

            return NL2CypherResult(
                cypher=f"MATCH (n:{name})\nRETURN {ret}{limit}",
                explanation=f"{'Get one' if wants_single else 'List all'} {name} node{'s' if not wants_single else ''}",
                confidence=0.6, method="rule_based",
            )

    m = re.match(r"(?:how many|count)\s+(\w+)s?\b", q)
    if m:
        entity = _match_entity(m.group(1), entities)
        if entity:
            return NL2CypherResult(
                cypher=f"MATCH (n:{entity['name']})\nRETURN count(n)",
                explanation=f"Count {entity['name']} nodes",
                confidence=0.7, method="rule_based",
            )

    for rtype, rdef in relationships.items():
        if rtype.replace("_", " ") in q or rtype in q:
            from_e = rdef.get("fromEntity", "Any")
            to_e = rdef.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                return NL2CypherResult(
                    cypher=f"MATCH (a:{from_e})-[:{rdef['type']}]->(b:{to_e})\nRETURN a, b",
                    explanation=f"Pattern matched relationship type {rdef['type']}",
                    confidence=0.3, method="rule_based",
                )

    return NL2CypherResult(
        cypher="",
        explanation="Could not generate a query from the input. Try rephrasing or use an LLM backend.",
        confidence=0.0,
        method="rule_based",
    )


_IRREGULAR_PLURALS: dict[str, str] = {
    "people": "person", "persons": "person",
    "men": "man", "women": "woman",
    "children": "child", "mice": "mouse",
    "data": "datum", "indices": "index",
}


def _match_entity(hint: str, entities: dict[str, dict]) -> dict | None:
    """Fuzzy-match an entity name from user input."""
    hint = hint.lower()
    hint_singular = _IRREGULAR_PLURALS.get(hint, hint)
    stems = {hint, hint_singular, hint.rstrip("s"), hint.rstrip("es"), re.sub(r"ies$", "y", hint)}
    for key, val in entities.items():
        key_stems = {key, key.rstrip("s"), key.rstrip("es"), re.sub(r"ies$", "y", key)}
        if stems & key_stems:
            return val
    for key, val in entities.items():
        if hint in key or key in hint:
            return val
    for key, val in entities.items():
        key_base = re.sub(r"[aeiouy]+$", "", key)
        for s in stems:
            s_base = re.sub(r"[aeiouy]+$", "", s)
            if len(s_base) >= 3 and (s_base.startswith(key_base) or key_base.startswith(s_base)):
                return val
    return None


def _match_relationship(hint: str, relationships: dict[str, dict]) -> dict | None:
    """Fuzzy-match a relationship type."""
    hint = hint.lower()
    for key, val in relationships.items():
        normalized = key.replace("_", "").lower()
        if hint in normalized or normalized in hint:
            return val
    verb_map = {
        "acted": "acted_in",
        "directed": "directed",
        "produced": "produced",
        "wrote": "wrote",
        "reviewed": "reviewed",
        "follows": "follows",
        "knows": "knows",
        "purchased": "purchased",
        "bought": "purchased",
        "ordered": "purchased",
        "sold": "sold_by",
        "reports": "reports_to",
        "supplies": "supplied_by",
    }
    mapped = verb_map.get(hint)
    if mapped:
        for key, val in relationships.items():
            if key == mapped:
                return val
    return None


def _parse_simple_filter(text: str, var: str) -> str | None:
    """Parse simple 'where/with/in X' filters."""
    m = re.match(r"(?:where|with|in|from)\s+(?:the\s+)?(?:name|title)\s+(?:is\s+)?['\"]?(.+?)['\"]?$", text)
    if m:
        val = m.group(1).strip("'\"")
        return f"{var}.name = '{val}'"

    m = re.match(r"(?:where|in|from)\s+(?:country\s+)?(?:is\s+)?['\"]?(\w+)['\"]?$", text)
    if m:
        return f"{var}.country = '{m.group(1)}'"

    return None


# ---------------------------------------------------------------------------
# Default few-shot index (lazy, process-wide)
# ---------------------------------------------------------------------------

_DEFAULT_FEWSHOT_INDEX: FewShotIndex | None = None
_DEFAULT_FEWSHOT_RESOLVED = False
_DEFAULT_FEWSHOT_INVALIDATION_REGISTERED = False


def _invalidate_default_fewshot_index() -> None:
    """Drop the cached process-wide FewShotIndex.

    Wired into :mod:`arango_cypher.nl_corrections` via
    :func:`register_invalidation_listener` so every saved / deleted NL
    correction forces a rebuild on the next ``nl_to_cypher`` call. Also
    exposed for tests and external callers that want to force a refresh
    after mutating the corpus files directly.
    """
    global _DEFAULT_FEWSHOT_INDEX, _DEFAULT_FEWSHOT_RESOLVED
    _DEFAULT_FEWSHOT_INDEX = None
    _DEFAULT_FEWSHOT_RESOLVED = False


def _ensure_nl_corrections_listener() -> None:
    """Register the FewShotIndex invalidation hook exactly once per process.

    Deferred to first-call time (rather than module import) so importing
    ``arango_cypher.nl2cypher`` does not transitively import SQLite or
    touch the nl_corrections database file. Safe to call repeatedly.
    """
    global _DEFAULT_FEWSHOT_INVALIDATION_REGISTERED
    if _DEFAULT_FEWSHOT_INVALIDATION_REGISTERED:
        return
    try:
        from arango_cypher import nl_corrections as _nlc

        _nlc.register_invalidation_listener(_invalidate_default_fewshot_index)
        _DEFAULT_FEWSHOT_INVALIDATION_REGISTERED = True
    except Exception as exc:
        logger.info("nl_corrections listener registration failed: %s", exc)


def _get_default_fewshot_index() -> FewShotIndex | None:
    """Lazily build the default FewShotIndex from shipped corpora + user
    corrections.

    Seed examples from ``arango_cypher/nl2cypher/corpora/*.yml`` are
    loaded first; approved ``(question, cypher)`` pairs from the
    :mod:`arango_cypher.nl_corrections` store are appended afterward,
    so a user's correction wins ties against a seed example with the
    same BM25 score.

    Returns ``None`` if ``rank_bm25`` is unavailable or both the corpora
    and the corrections store are empty. Caller falls back to a
    zero-shot prompt in that case.
    """
    global _DEFAULT_FEWSHOT_INDEX, _DEFAULT_FEWSHOT_RESOLVED

    _ensure_nl_corrections_listener()

    if _DEFAULT_FEWSHOT_RESOLVED:
        return _DEFAULT_FEWSHOT_INDEX
    _DEFAULT_FEWSHOT_RESOLVED = True
    try:
        from pathlib import Path

        from .fewshot import BM25Retriever, FewShotIndex, _NoopRetriever

        corpora_dir = Path(__file__).parent / "corpora"
        paths = sorted(corpora_dir.glob("*.yml"))

        seed_index = FewShotIndex.from_corpus_files(paths) if paths else None
        seed_examples: list[tuple[str, str]] = (
            list(seed_index.examples) if seed_index is not None else []
        )

        correction_examples: list[tuple[str, str]] = []
        try:
            from arango_cypher import nl_corrections as _nlc

            correction_examples = _nlc.all_examples()
        except Exception as exc:
            logger.info("nl_corrections load failed (ignored): %s", exc)

        combined = seed_examples + correction_examples
        if not combined:
            _DEFAULT_FEWSHOT_INDEX = None
            return None

        try:
            retriever = BM25Retriever(combined)
        except ImportError as exc:
            logger.info("rank_bm25 not installed; FewShotIndex degrades to no-op: %s", exc)
            retriever = _NoopRetriever()
        _DEFAULT_FEWSHOT_INDEX = FewShotIndex(retriever, examples=combined)
    except Exception as exc:
        logger.info("FewShotIndex initialization failed: %s", exc)
        _DEFAULT_FEWSHOT_INDEX = None
    return _DEFAULT_FEWSHOT_INDEX


def nl_to_cypher(
    question: str,
    *,
    mapping: MappingBundle | dict[str, Any] | None = None,
    use_llm: bool = True,
    llm_provider: LLMProvider | None = None,
    max_retries: int = 2,
    use_fewshot: bool = True,
    fewshot_index: FewShotIndex | None = None,
    use_entity_resolution: bool = True,
    entity_resolver: EntityResolver | None = None,
    db: Any | None = None,
) -> NL2CypherResult:
    """Translate a natural language question to Cypher.

    Args:
        question: Plain English question about the graph.
        mapping: Schema mapping (MappingBundle or export dict).
        use_llm: If True, attempt LLM translation first.
        llm_provider: A custom LLM provider (implements ``LLMProvider``).
            If None and ``use_llm`` is True, falls back to an OpenAI-compatible
            provider configured via ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
            ``OPENAI_MODEL`` environment variables.
        max_retries: Number of retry attempts if LLM output fails validation.
        use_fewshot: If True (default), retrieve top-K similar examples
            from the shipped corpora and inject them into the prompt.
        fewshot_index: Optional caller-supplied FewShotIndex to override
            the default one built from the shipped corpora.
        use_entity_resolution: If True (default) and a ``db`` handle or
            ``entity_resolver`` is provided, pre-resolve mentions in the
            question against the database's string properties so the LLM
            sees database-correct literals (e.g. ``"Forrest Gump"``
            instead of the user's ``"Forest Gump"``).  When no DB and no
            resolver are supplied the flag is a no-op and the prompt is
            byte-identical to the pre-WP-25.2 baseline.
        entity_resolver: Optional caller-supplied :class:`EntityResolver`.
            Overrides ``db``-based auto-construction when provided.
        db: Optional python-arango ``StandardDatabase``.  When supplied
            (and ``use_entity_resolution=True``) an :class:`EntityResolver`
            is constructed lazily against this connection.
    """
    if mapping is None:
        return NL2CypherResult(
            cypher="",
            explanation="No schema mapping provided. Cannot generate Cypher without knowing the graph structure.",
            confidence=0.0,
        )

    if isinstance(mapping, dict):
        bundle = MappingBundle(
            conceptual_schema=mapping.get("conceptualSchema") or mapping.get("conceptual_schema") or {},
            physical_mapping=mapping.get("physicalMapping") or mapping.get("physical_mapping") or {},
            metadata=mapping.get("metadata", {}),
        )
    else:
        bundle = mapping

    schema_summary = _build_schema_summary(bundle)
    ctx = _build_schema_context(bundle)

    if use_llm:
        provider = llm_provider or _get_default_provider()
        if provider is not None:
            few_shot: list[tuple[str, str]] = []
            if use_fewshot:
                index = fewshot_index if fewshot_index is not None else _get_default_fewshot_index()
                if index is not None:
                    try:
                        few_shot = index.retrieve(question, k=3)
                    except Exception as exc:
                        logger.info("FewShotIndex.retrieve failed: %s", exc)
                        few_shot = []

            resolved_lines: list[str] = []
            if use_entity_resolution:
                resolver = entity_resolver
                if resolver is None and db is not None:
                    try:
                        from .entity_resolution import EntityResolver
                        resolver = EntityResolver(db=db, mapping=bundle)
                    except Exception as exc:
                        logger.info("EntityResolver init failed: %s", exc)
                        resolver = None
                if resolver is not None:
                    try:
                        hits = resolver.resolve(question)
                        if hits:
                            resolved_lines = resolver.format_prompt_section(hits)
                    except Exception as exc:
                        logger.info("EntityResolver.resolve failed: %s", exc)
                        resolved_lines = []

            result = _call_llm_with_retry(
                question, schema_summary, provider, max_retries=max_retries,
                ctx=ctx, few_shot_examples=few_shot,
                resolved_entities=resolved_lines,
                mapping=bundle, db=db,
            )
            if result and result.cypher:
                return result

    return _rule_based_translate(question, bundle)


# ---------------------------------------------------------------------------
# Representative NL query suggestions (used to seed the UI "Ask" history)
# ---------------------------------------------------------------------------


_SUGGEST_PROMPT_TEMPLATE = """You generate short, natural-language example questions that a user might ask about a property graph.

The user has just connected to a database with the following schema:

{schema}

Rules:
- Produce between 6 and 10 distinct questions.
- Each question must be answerable against this schema (use only labels, relationship types, and properties shown above).
- Mix question shapes: simple lookups, one-hop traversals, two-hop traversals, filters on properties, aggregations (counts, averages), ordering / top-k, and at least one question that uses a property filter.
- Keep each question under ~120 characters, phrased the way a human would type it (no SQL, no Cypher, no code fences).
- Do NOT prefix questions with numbers, bullets, or labels.
- Output ONLY the questions, one per line, nothing else.
"""


def _llm_suggest_nl_queries(
    bundle: MappingBundle,
    provider: LLMProvider,
    count: int = 8,
) -> list[str]:
    """Ask the LLM to propose representative NL queries for the schema.

    Uses the public ``LLMProvider.generate`` protocol so any provider
    works — including :class:`~arango_cypher.nl2cypher.AnthropicProvider`,
    which does not subclass ``_BaseChatProvider``.
    """
    schema_summary = _build_schema_summary(bundle)
    system = _SUGGEST_PROMPT_TEMPLATE.format(schema=schema_summary)
    user = (
        f"Generate {count} example natural-language questions for this graph. "
        "Return only the questions, one per line."
    )
    try:
        result = provider.generate(system, user)
        content = result[0] if isinstance(result, tuple) else result
    except Exception as exc:
        logger.info("LLM suggest_nl_queries failed: %s", exc)
        return []

    lines: list[str] = []
    for raw in content.splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", s)
        s = s.strip().strip('"').strip("'")
        if len(s) < 4:
            continue
        if s.lower().startswith(("match ", "with ", "return ", "for ", "//", "#")):
            continue
        lines.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for q in lines:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out[:count]


def _rule_based_suggest_nl_queries(
    bundle: MappingBundle, count: int = 8,
) -> list[str]:
    """Generate representative NL questions from the schema without an LLM."""
    cs = bundle.conceptual_schema
    pm = bundle.physical_mapping

    entities: list[dict[str, Any]] = []
    cs_ents = cs.get("entities", [])
    if isinstance(cs_ents, list) and cs_ents and isinstance(cs_ents[0], dict):
        for e in cs_ents:
            name = e.get("name") or ""
            props = [
                p.get("name", "") for p in e.get("properties", [])
                if isinstance(p, dict) and p.get("name")
            ]
            if name:
                entities.append({"name": name, "properties": props[:8]})
    if not entities and isinstance(pm.get("entities"), dict):
        for name, spec in pm["entities"].items():
            props = list((spec or {}).get("properties", {}).keys())[:8]
            entities.append({"name": name, "properties": props})

    rels: list[dict[str, Any]] = []
    cs_rels = cs.get("relationships", [])
    if isinstance(cs_rels, list) and cs_rels and isinstance(cs_rels[0], dict):
        for r in cs_rels:
            rels.append({
                "type": r.get("type", ""),
                "from": r.get("fromEntity", ""),
                "to": r.get("toEntity", ""),
            })
    if not rels and isinstance(pm.get("relationships"), dict):
        for rtype, spec in pm["relationships"].items():
            spec = spec or {}
            rels.append({
                "type": rtype,
                "from": spec.get("domain") or "",
                "to": spec.get("range") or "",
            })

    def _humanize(token: str) -> str:
        if not token:
            return token
        stripped = token.replace("_", "").replace("-", "")
        if stripped.isupper():
            s = token.replace("_", " ").replace("-", " ")
        else:
            s = re.sub(r"(?<!^)(?=[A-Z])", " ", token)
            s = s.replace("_", " ").replace("-", " ")
        return re.sub(r"\s+", " ", s).strip().lower()

    def _plural(word: str) -> str:
        if not word:
            return word
        if word.endswith("s"):
            return word
        if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
            return word[:-1] + "ies"
        return word + "s"

    def _verbalize_rel(rtype: str) -> str:
        return _humanize(rtype).replace(" ", " ").lower()

    suggestions: list[str] = []

    for e in entities[:4]:
        label = _plural(_humanize(e["name"]))
        suggestions.append(f"Show 10 {label}")
        suggestions.append(f"How many {label} are there?")
        if e["properties"]:
            prop = _humanize(e["properties"][0])
            suggestions.append(f"List {label} ordered by {prop}")

    for r in rels[:4]:
        if not (r["type"] and r["from"] and r["to"]):
            continue
        verb = _verbalize_rel(r["type"])
        src = _humanize(r["from"])
        dst_plural = _plural(_humanize(r["to"]))
        suggestions.append(
            f"For each {src}, show the {dst_plural} they {verb}"
        )
        suggestions.append(
            f"Count {dst_plural} per {src}"
        )

    if len(rels) >= 2:
        r1, r2 = rels[0], rels[1]
        if r1.get("to") and r2.get("from") and r1["to"] == r2["from"]:
            suggestions.append(
                f"Find {_plural(_humanize(r2['to']))} connected to {_plural(_humanize(r1['from']))} "
                f"through {_humanize(r1['to'])}"
            )

    seen: set[str] = set()
    out: list[str] = []
    for q in suggestions:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= count:
            break
    return out


def suggest_nl_queries(
    mapping: MappingBundle | dict[str, Any] | None,
    *,
    count: int = 8,
    use_llm: bool = True,
    llm_provider: LLMProvider | None = None,
) -> list[str]:
    """Return a representative set of natural-language questions for the schema.

    Used to seed the UI "Ask" history after the user connects to a database
    and schema introspection completes. Falls back to rule-based generation
    when no LLM provider is configured or the LLM call fails.
    """
    if mapping is None:
        return []

    if isinstance(mapping, dict):
        bundle = MappingBundle(
            conceptual_schema=mapping.get("conceptualSchema") or mapping.get("conceptual_schema") or {},
            physical_mapping=mapping.get("physicalMapping") or mapping.get("physical_mapping") or {},
            metadata=mapping.get("metadata", {}),
        )
    else:
        bundle = mapping

    if use_llm:
        provider = llm_provider or _get_default_provider()
        if provider is not None:
            llm_out = _llm_suggest_nl_queries(bundle, provider, count=count)
            if llm_out:
                return llm_out

    return _rule_based_suggest_nl_queries(bundle, count=count)
