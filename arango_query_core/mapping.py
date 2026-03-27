from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .errors import CoreError

JsonObj = dict[str, Any]


@dataclass(frozen=True)
class MappingSource:
    kind: Literal["explicit", "heuristic", "schema_analyzer_export"]
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


class MappingResolver:
    """
    Minimal adapter over a MappingBundle.

    v0.1: this is intentionally light. As soon as we integrate `arangodb-schema-analyzer`,
    we can optionally wrap its PhysicalMapping helper methods too.
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
            raise CoreError(f"No entity mapping for: {label_or_entity}", code="MAPPING_NOT_FOUND")
        return mapping

    def resolve_relationship(self, rel_type: str) -> JsonObj:
        pm = self.bundle.physical_mapping
        rels = pm.get("relationships") if isinstance(pm.get("relationships"), dict) else {}
        if not isinstance(rels, dict):
            rels = {}
        mapping = rels.get(rel_type)
        if not isinstance(mapping, dict):
            raise CoreError(f"No relationship mapping for: {rel_type}", code="MAPPING_NOT_FOUND")
        return mapping

