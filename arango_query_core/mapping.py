from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from .errors import CoreError

JsonObj = dict[str, Any]


# ArangoDB collection-name grammar (per the official docs):
#   * starts with a letter or underscore
#   * 1 to 256 chars total
#   * subsequent chars: letter, digit, underscore, or hyphen
#
# Used as a defence-in-depth guard wherever a collection name must be
# embedded directly into an AQL string (e.g. ``FOR d IN `<name>``` or
# ``RETURN LENGTH(`<name>`)``). Backticks alone prevent most forms because
# the AQL grammar requires balanced backticks around an identifier — but a
# stray backtick or newline in the input would still escape the quote, so we
# regex-validate first and refuse to interpolate anything that doesn't match.
#
# Lifted from ``arango_cypher/service.py:_COLLECTION_NAME_RE`` (introduced
# at the ``/tenants?collection=`` boundary in the 2026-04-24 service-side
# hardening sprint) so non-service callers (notably
# ``arango_cypher/schema_acquire.py:compute_statistics``) get the same
# guarantee without re-importing service-internal symbols.
COLLECTION_NAME_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,255}$")


def is_valid_collection_name(name: str) -> bool:
    """Return ``True`` iff *name* matches the ArangoDB collection-name grammar.

    A pure-string predicate (no DB call) used as a precondition before
    embedding *name* into an AQL string. Callers that need the exception
    form should raise their own typed error on a ``False`` return.
    """
    return isinstance(name, str) and bool(COLLECTION_NAME_RE.match(name))


@dataclass(frozen=True)
class RelationshipStats:
    """Cardinality statistics for a single relationship type."""
    edge_count: int = 0
    source_count: int = 0
    target_count: int = 0
    avg_out_degree: float = 0.0
    avg_in_degree: float = 0.0
    cardinality_pattern: str = "N:M"
    selectivity: float = 1.0


@dataclass(frozen=True)
class IndexInfo:
    """Metadata for a single index on a collection."""
    type: str
    fields: tuple[str, ...]
    unique: bool = False
    sparse: bool = False
    name: str = ""
    vci: bool = False
    deduplicate: bool = False


@dataclass(frozen=True)
class PropertyInfo:
    """Metadata for a single property/attribute on an entity or relationship.

    ``sentinel_values`` lists string values that appear in the sampled data
    and match known "null-sentinel" tokens (``NULL``, ``N/A``, ``UNKNOWN``,
    ...). ``numeric_like`` is True when the non-sentinel string values parse
    as numbers (i.e. the field stores numeric data as strings).
    ``sample_values`` holds a few representative non-sentinel values for
    prompt / UI context.
    """
    field: str
    type: str = "string"
    indexed: bool = False
    required: bool = False
    description: str = ""
    sentinel_values: tuple[str, ...] = ()
    numeric_like: bool = False
    sample_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class MappingSource:
    kind: Literal["explicit", "heuristic", "schema_analyzer_export", "owl_turtle"]
    fingerprint: str | None = None
    generated_at_iso: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class MappingBundle:
    conceptual_schema: JsonObj
    physical_mapping: JsonObj
    metadata: JsonObj
    owl_turtle: str | None = None
    source: MappingSource | None = None


def mapping_from_wire_dict(
    d: JsonObj,
    *,
    source: MappingSource | None = None,
) -> MappingBundle:
    """Build a :class:`MappingBundle` from a wire-format dict.

    Accepts either snake_case (``conceptual_schema`` / ``physical_mapping``)
    or camelCase (``conceptualSchema`` / ``physicalMapping``) keys — the
    FastAPI endpoints post camelCase from the UI while the Python API
    uses snake_case, and this helper is the single spelling-normalising
    entry point so every consumer (HTTP service, CLI, tool-calling
    harness) produces an identically-keyed bundle.

    The ``metadata`` field passes through unchanged; ``owl_turtle`` is
    intentionally *not* read here (the existing wire contract never
    carried it — see `docs/python_prd.md` §14 open item).

    Pair this helper with :func:`mapping_hash` (below): round-tripping a
    wire dict through ``mapping_from_wire_dict`` and hashing the result
    produces the same fingerprint regardless of input spelling.
    """
    return MappingBundle(
        conceptual_schema=d.get("conceptual_schema") or d.get("conceptualSchema") or {},
        physical_mapping=d.get("physical_mapping") or d.get("physicalMapping") or {},
        metadata=d.get("metadata", {}),
        source=source,
    )


def mapping_hash(mapping: MappingBundle | JsonObj | Any) -> str:
    """Deterministic 16-hex-char fingerprint of a mapping bundle.

    Accepts either a :class:`MappingBundle` instance or a wire-format
    dict with snake_case or camelCase keys (see
    :func:`mapping_from_wire_dict` for the accepted key shapes). The
    fingerprint is stable across key spellings: the same logical
    mapping submitted as camelCase by the UI and as snake_case by the
    Python API produces the same hash, so downstream lookup tables
    (``corrections`` / ``nl_corrections``) key identically regardless
    of caller.

    The output is a 16-character prefix of the SHA-256 of the
    JSON-canonicalised ``{cs, pm}`` pair — enough entropy to avoid
    collisions within any realistic corrections-store size while
    staying short enough to inline into log lines and index-column
    values.
    """
    cs: Any
    pm: Any
    if isinstance(mapping, MappingBundle):
        cs = mapping.conceptual_schema
        pm = mapping.physical_mapping
    elif hasattr(mapping, "conceptual_schema"):
        cs = mapping.conceptual_schema
        pm = mapping.physical_mapping
    elif isinstance(mapping, dict):
        cs = mapping.get("conceptual_schema")
        if cs is None:
            cs = mapping.get("conceptualSchema", {})
        pm = mapping.get("physical_mapping")
        if pm is None:
            pm = mapping.get("physicalMapping", {})
    else:
        cs, pm = {}, {}
    raw = {"cs": cs, "pm": pm}
    blob = json.dumps(raw, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


class MappingResolver:
    """
    Adapter over a MappingBundle that resolves entities, relationships,
    and their properties from the conceptual-to-physical mapping.
    """

    def __init__(self, bundle: MappingBundle):
        self.bundle = bundle

    def resolve_entity(self, label_or_entity: str) -> JsonObj:
        pm = self.bundle.physical_mapping
        entities = pm.get("entities") if isinstance(pm.get("entities"), dict) else {}
        if not isinstance(entities, dict):
            entities = {}
        mapping = entities.get(label_or_entity)
        if not isinstance(mapping, dict):
            available = sorted(entities.keys()) if entities else []
            hint = f". Available entities: {', '.join(available)}" if available else " (mapping has no entities — did schema introspection succeed?)"
            raise CoreError(f"No entity mapping for: {label_or_entity}{hint}", code="MAPPING_NOT_FOUND")
        return mapping

    def resolve_relationship(self, rel_type: str) -> JsonObj:
        pm = self.bundle.physical_mapping
        rels = pm.get("relationships") if isinstance(pm.get("relationships"), dict) else {}
        if not isinstance(rels, dict):
            rels = {}
        mapping = rels.get(rel_type)
        if not isinstance(mapping, dict):
            available = sorted(rels.keys()) if rels else []
            hint = f". Available relationships: {', '.join(available)}" if available else " (mapping has no relationships — did schema introspection succeed?)"
            raise CoreError(f"No relationship mapping for: {rel_type}{hint}", code="MAPPING_NOT_FOUND")
        return mapping

    def resolve_properties(self, label_or_type: str) -> dict[str, PropertyInfo]:
        """Return property metadata for an entity label or relationship type.

        Looks in the entity mapping first, then relationships. Returns an empty
        dict if the label/type exists but has no properties defined.
        """
        pm = self.bundle.physical_mapping
        mapping: JsonObj | None = None

        entities = pm.get("entities", {})
        if isinstance(entities, dict) and label_or_type in entities:
            mapping = entities[label_or_type]

        if mapping is None:
            rels = pm.get("relationships", {})
            if isinstance(rels, dict) and label_or_type in rels:
                mapping = rels[label_or_type]

        if mapping is None:
            return {}

        props_raw = mapping.get("properties", {})
        if not isinstance(props_raw, dict):
            return {}

        result: dict[str, PropertyInfo] = {}
        for name, meta in props_raw.items():
            if isinstance(meta, dict):
                sentinels = meta.get("sentinelValues") or meta.get("sentinel_values") or ()
                samples = meta.get("sampleValues") or meta.get("sample_values") or ()
                result[name] = PropertyInfo(
                    field=meta.get("field", name),
                    type=meta.get("type", "string"),
                    indexed=bool(meta.get("indexed", False)),
                    required=bool(meta.get("required", False)),
                    description=meta.get("description", ""),
                    sentinel_values=tuple(str(s) for s in sentinels) if sentinels else (),
                    numeric_like=bool(meta.get("numericLike") or meta.get("numeric_like")),
                    sample_values=tuple(str(s) for s in samples) if samples else (),
                )
            elif isinstance(meta, str):
                result[name] = PropertyInfo(field=name, type=meta)
            else:
                result[name] = PropertyInfo(field=name)
        return result

    def edge_constrains_target(self, rel_type: str, target_label: str, direction: str = "OUTBOUND") -> bool:
        """Return True if the relationship's domain/range guarantees the target type.

        When an edge has declared domain and range, and the target label matches
        the expected endpoint, IS_SAME_COLLECTION filtering is unnecessary
        because the edge already constrains the vertices.

        This applies to both DEDICATED_COLLECTION (edge collection is exclusive
        to one relationship type) and GENERIC_WITH_TYPE (the type discriminator
        filter, always emitted by the translator, restricts traversal to edges
        of this type — so domain/range constraints are equally valid).

        Domain/range supports both single strings and arrays of strings (union
        types).  When the domain or range is a union, the filter can only be
        skipped if every class in the union maps to the **same** physical
        collection — otherwise the edge could point to multiple collections
        and ``IS_SAME_COLLECTION`` is still needed.

        Domain/range is resolved from (in order):
        1. Explicit ``domain``/``range`` fields on the physical relationship mapping
        2. ``fromEntity``/``toEntity`` in the conceptual schema's relationships array
        3. Inferred when the conceptual schema defines a single entity type
        """
        try:
            rmap = self.resolve_relationship(rel_type)
        except CoreError:
            return False

        style = rmap.get("style")
        if style not in ("DEDICATED_COLLECTION", "GENERIC_WITH_TYPE"):
            return False

        domain, range_ = self._resolve_domain_range(rel_type, rmap)
        if not domain or not range_:
            return False

        if direction == "OUTBOUND":
            return self._endpoint_constrains(range_, target_label)
        elif direction == "INBOUND":
            return self._endpoint_constrains(domain, target_label)
        else:
            return (
                self._endpoint_constrains(domain, target_label)
                and self._endpoint_constrains(range_, target_label)
            )

    def _endpoint_constrains(self, endpoint: str | list[str], target_label: str) -> bool:
        """Check if an endpoint (single class or union) guarantees the target type.

        For a single class, the target must match.  For a union, the target must
        be in the set **and** every class in the union must map to the same
        physical collection (otherwise multiple collections are reachable and
        IS_SAME_COLLECTION is still needed).
        """
        if isinstance(endpoint, str):
            return endpoint == target_label

        if not isinstance(endpoint, list) or not endpoint:
            return False

        if target_label not in endpoint:
            return False

        if len(endpoint) == 1:
            return True

        collections: set[str] = set()
        for label in endpoint:
            try:
                emap = self.resolve_entity(label)
                collections.add(emap.get("collectionName", ""))
            except CoreError:
                return False

        return len(collections) == 1

    def _resolve_domain_range(
        self, rel_type: str, rmap: JsonObj,
    ) -> tuple[str | list[str] | None, str | list[str] | None]:
        """Resolve domain/range for a relationship from physical mapping or conceptual schema.

        Returns string for single-class endpoints, list for union endpoints, or
        None when unresolvable.
        """
        domain = rmap.get("domain")
        range_ = rmap.get("range")
        if domain and range_:
            return domain, range_

        cs = self.bundle.conceptual_schema
        rels = cs.get("relationships", [])
        if isinstance(rels, list):
            for r in rels:
                if isinstance(r, dict) and r.get("type") == rel_type:
                    from_e = r.get("fromEntity")
                    to_e = r.get("toEntity")
                    if self._is_valid_endpoint(from_e) and self._is_valid_endpoint(to_e):
                        return self._normalize_endpoint(from_e), self._normalize_endpoint(to_e)
                    break

        entity_labels = self.all_entity_labels()
        if len(entity_labels) == 1:
            return entity_labels[0], entity_labels[0]

        return None, None

    @staticmethod
    def _is_valid_endpoint(val: Any) -> bool:
        if isinstance(val, str):
            return bool(val) and val != "Any"
        if isinstance(val, list):
            return bool(val) and all(isinstance(v, str) and v and v != "Any" for v in val)
        return False

    @staticmethod
    def _normalize_endpoint(val: str | list[str]) -> str | list[str]:
        """Return a string for single-class, list for multi-class."""
        if isinstance(val, list) and len(val) == 1:
            return val[0]
        return val

    def resolve_indexes(self, label_or_type: str) -> list[IndexInfo]:
        """Return index metadata for an entity label or relationship type."""
        pm = self.bundle.physical_mapping
        mapping: JsonObj | None = None

        entities = pm.get("entities", {})
        if isinstance(entities, dict) and label_or_type in entities:
            mapping = entities[label_or_type]

        if mapping is None:
            rels = pm.get("relationships", {})
            if isinstance(rels, dict) and label_or_type in rels:
                mapping = rels[label_or_type]

        if mapping is None:
            return []

        indexes_raw = mapping.get("indexes", [])
        if not isinstance(indexes_raw, list):
            return []

        result: list[IndexInfo] = []
        for idx in indexes_raw:
            if not isinstance(idx, dict):
                continue
            fields = idx.get("fields", [])
            if isinstance(fields, list):
                fields = tuple(str(f) for f in fields)
            else:
                continue
            result.append(IndexInfo(
                type=str(idx.get("type", "persistent")),
                fields=fields,
                unique=bool(idx.get("unique", False)),
                sparse=bool(idx.get("sparse", False)),
                name=str(idx.get("name", "")),
                vci=bool(idx.get("vci", False)),
                deduplicate=bool(idx.get("deduplicate", False)),
            ))
        return result

    def has_vci(self, rel_type: str) -> bool:
        """Check if any index on the relationship's edge collection has VCI enabled."""
        return any(idx.vci for idx in self.resolve_indexes(rel_type))

    def all_entity_labels(self) -> list[str]:
        """Return all entity type labels defined in the mapping."""
        entities = self.bundle.physical_mapping.get("entities", {})
        return list(entities.keys()) if isinstance(entities, dict) else []

    def all_relationship_types(self) -> list[str]:
        """Return all relationship type names defined in the mapping."""
        rels = self.bundle.physical_mapping.get("relationships", {})
        return list(rels.keys()) if isinstance(rels, dict) else []

    def all_edge_collections(self) -> list[str]:
        """Return distinct edge collection names from the physical mapping."""
        rels = self.bundle.physical_mapping.get("relationships", {})
        if not isinstance(rels, dict):
            return []
        seen: set[str] = set()
        result: list[str] = []
        for rmap in rels.values():
            ec = rmap.get("edgeCollectionName") or rmap.get("collectionName", "")
            if isinstance(ec, str) and ec and ec not in seen:
                seen.add(ec)
                result.append(ec)
        return result

    def schema_summary(self) -> JsonObj:
        """Return a structured summary of the full mapping for the UI graph view.

        Includes cardinality statistics when available in the bundle metadata.
        """
        stats = self._get_stats()
        entity_stats = stats.get("entities", {})
        rel_stats_map = stats.get("relationships", {})

        entities: list[JsonObj] = []
        for label in self.all_entity_labels():
            emap = self.resolve_entity(label)
            props = self.resolve_properties(label)
            ent: JsonObj = {
                "label": label,
                "collection": emap.get("collectionName", ""),
                "style": emap.get("style", ""),
                "properties": {
                    name: {
                        "field": p.field,
                        "type": p.type,
                        "indexed": p.indexed,
                        "required": p.required,
                        "description": p.description,
                        **(
                            {"sentinelValues": list(p.sentinel_values)}
                            if p.sentinel_values else {}
                        ),
                        **({"numericLike": True} if p.numeric_like else {}),
                        **(
                            {"sampleValues": list(p.sample_values)}
                            if p.sample_values else {}
                        ),
                    }
                    for name, p in props.items()
                },
            }
            if emap.get("typeField"):
                ent["typeField"] = emap["typeField"]
                ent["typeValue"] = emap.get("typeValue", "")
            est = entity_stats.get(label, {})
            if isinstance(est, dict) and "estimated_count" in est:
                ent["estimatedCount"] = est["estimated_count"]
            entities.append(ent)

        relationships: list[JsonObj] = []
        for rtype in self.all_relationship_types():
            rmap = self.resolve_relationship(rtype)
            props = self.resolve_properties(rtype)
            domain, range_ = self._resolve_domain_range(rtype, rmap)
            rel_entry: JsonObj = {
                "type": rtype,
                "edgeCollection": rmap.get("edgeCollectionName", ""),
                "style": rmap.get("style", ""),
                "domain": domain,
                "range": range_,
                "embeddedPath": rmap.get("embeddedPath"),
                "embeddedArray": rmap.get("embeddedArray"),
                "properties": {
                    name: {
                        "field": p.field,
                        "type": p.type,
                        "indexed": p.indexed,
                        **(
                            {"sentinelValues": list(p.sentinel_values)}
                            if p.sentinel_values else {}
                        ),
                        **({"numericLike": True} if p.numeric_like else {}),
                        **(
                            {"sampleValues": list(p.sample_values)}
                            if p.sample_values else {}
                        ),
                    }
                    for name, p in props.items()
                },
            }
            if rmap.get("typeField"):
                rel_entry["typeField"] = rmap["typeField"]
                rel_entry["typeValue"] = rmap.get("typeValue", "")
            rs = rel_stats_map.get(rtype, {})
            if isinstance(rs, dict) and rs.get("edge_count"):
                rel_entry["statistics"] = {
                    "edgeCount": rs.get("edge_count", 0),
                    "avgOutDegree": rs.get("avg_out_degree", 0),
                    "avgInDegree": rs.get("avg_in_degree", 0),
                    "cardinalityPattern": rs.get("cardinality_pattern", "N:M"),
                    "selectivity": rs.get("selectivity", 1.0),
                }
            relationships.append(rel_entry)

        return {"entities": entities, "relationships": relationships}

    # ------------------------------------------------------------------
    # Cardinality statistics helpers
    # ------------------------------------------------------------------

    def _get_stats(self) -> JsonObj:
        return self.bundle.metadata.get("statistics", {})

    def estimated_count(self, label: str) -> int | None:
        """Return estimated document count for an entity label, or None if unknown."""
        stats = self._get_stats()
        ent_stats = stats.get("entities", {})
        entry = ent_stats.get(label, {})
        if isinstance(entry, dict) and "estimated_count" in entry:
            return int(entry["estimated_count"])
        return None

    def collection_count(self, collection_name: str) -> int | None:
        """Return document count for a physical collection, or None if unknown."""
        stats = self._get_stats()
        col_stats = stats.get("collections", {})
        entry = col_stats.get(collection_name, {})
        if isinstance(entry, dict) and "count" in entry:
            return int(entry["count"])
        return None

    def relationship_stats(self, rel_type: str) -> RelationshipStats | None:
        """Return cardinality statistics for a relationship type, or None if unknown."""
        stats = self._get_stats()
        rel_stats = stats.get("relationships", {})
        entry = rel_stats.get(rel_type, {})
        if not isinstance(entry, dict) or "edge_count" not in entry:
            return None
        return RelationshipStats(
            edge_count=int(entry.get("edge_count", 0)),
            source_count=int(entry.get("source_count", 0)),
            target_count=int(entry.get("target_count", 0)),
            avg_out_degree=float(entry.get("avg_out_degree", 0.0)),
            avg_in_degree=float(entry.get("avg_in_degree", 0.0)),
            cardinality_pattern=str(entry.get("cardinality_pattern", "N:M")),
            selectivity=float(entry.get("selectivity", 1.0)),
        )

    def preferred_traversal_direction(self, rel_type: str) -> str | None:
        """Suggest OUTBOUND or INBOUND based on fan-out/fan-in asymmetry.

        Returns the direction that produces fewer intermediate results,
        or None if statistics are unavailable or roughly symmetric.
        """
        rs = self.relationship_stats(rel_type)
        if rs is None or rs.edge_count == 0:
            return None
        if rs.avg_out_degree == 0 and rs.avg_in_degree == 0:
            return None

        ratio = (rs.avg_out_degree / rs.avg_in_degree) if rs.avg_in_degree > 0 else float("inf")
        if ratio > 5.0:
            return "INBOUND"
        if ratio < 0.2:
            return "OUTBOUND"
        return None

