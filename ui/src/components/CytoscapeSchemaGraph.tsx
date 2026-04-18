import { useEffect, useRef, useCallback, useState, useMemo } from "react";
import cytoscape from "cytoscape";
import type { Core, EventObject, NodeSingular } from "cytoscape";

interface PropInfo {
  name: string;
  field: string;
  type: string;
}
interface EntityInfo {
  name: string;
  collection: string;
  style: string;
  properties: PropInfo[];
}
interface RelInfo {
  type: string;
  from: string;
  to: string;
  edgeCollection: string;
  style: string;
  properties: PropInfo[];
}

interface Props {
  mapping: Record<string, unknown>;
  onMappingChange?: (mapping: Record<string, unknown>) => void;
}

interface SelectedItem {
  kind: "entity" | "relationship";
  name: string;
  collection: string;
  style: string;
  properties: PropInfo[];
  from?: string;
  to?: string;
}

interface ContextMenu {
  x: number;
  y: number;
  items: { label: string; action: () => void }[];
}

interface DialogState {
  kind: "add-entity" | "edit-entity" | "add-relationship" | null;
  entityName?: string;
  entityCollection?: string;
  entityStyle?: string;
  relType?: string;
  relFrom?: string;
  relTo?: string;
  relCollection?: string;
}

const CONCEPT_COLORS = [
  "#818cf8", "#4ade80", "#fbbf24", "#f87171", "#a78bfa", "#22d3ee",
];
const PHYS_COLOR = "#64748b";
const MAP_EDGE_COLOR = "#475569";

function extractMapping(mapping: Record<string, unknown>): {
  entities: EntityInfo[];
  relationships: RelInfo[];
} {
  const cs = (mapping.conceptualSchema ??
    mapping.conceptual_schema ??
    {}) as Record<string, unknown>;
  const pm = (mapping.physicalMapping ??
    mapping.physical_mapping ??
    {}) as Record<string, unknown>;
  const pmE = (pm.entities ?? {}) as Record<string, Record<string, unknown>>;
  const pmR = (pm.relationships ?? {}) as Record<
    string,
    Record<string, unknown>
  >;

  const csRich = cs.entities as Record<string, unknown>[] | undefined;
  const csTypes = cs.entityTypes as string[] | undefined;
  const csRRich = cs.relationships as Record<string, unknown>[] | undefined;
  const csRTypes = cs.relationshipTypes as string[] | undefined;

  function extractProps(obj: Record<string, unknown>): PropInfo[] {
    const raw = (obj.properties ?? {}) as Record<string, unknown>;
    return Object.entries(raw)
      .slice(0, 8)
      .map(([k, v]) => {
        if (v && typeof v === "object") {
          const o = v as Record<string, string>;
          return { name: k, field: o.field || k, type: o.type || "string" };
        }
        return { name: k, field: k, type: "string" };
      });
  }

  const names: string[] = [];
  if (Array.isArray(csRich) && csRich.length > 0)
    csRich.forEach((e) => {
      const n = (e.name as string) || "";
      if (n) names.push(n);
    });
  else if (Array.isArray(csTypes)) names.push(...csTypes.filter(Boolean));
  else names.push(...Object.keys(pmE));

  const entities: EntityInfo[] = names.map((n) => {
    const p = pmE[n] ?? {};
    return {
      name: n,
      collection: (p.collectionName as string) || n.toLowerCase() + "s",
      style: (p.style as string) || "COLLECTION",
      properties: extractProps(p),
    };
  });

  const relationships: RelInfo[] = [];
  if (Array.isArray(csRRich) && csRRich.length > 0) {
    for (const r of csRRich) {
      const t = (r.type as string) || "";
      if (!t) continue;
      const p = pmR[t] ?? {};
      relationships.push({
        type: t,
        from: (r.fromEntity as string) || "",
        to: (r.toEntity as string) || "",
        edgeCollection:
          (p.edgeCollectionName as string) || t.toLowerCase(),
        style: (p.style as string) || "DEDICATED_COLLECTION",
        properties: extractProps(p),
      });
    }
  } else {
    const rn = Array.isArray(csRTypes) ? csRTypes : Object.keys(pmR);
    for (const t of rn) {
      if (!t) continue;
      const p = pmR[t] ?? {};
      const f =
        (p.domain as string) ||
        (p.fromEntity as string) ||
        (names[0] ?? "");
      const to =
        (p.range as string) ||
        (p.toEntity as string) ||
        (names[names.length - 1] ?? "");
      relationships.push({
        type: t,
        from: f,
        to,
        edgeCollection:
          (p.edgeCollectionName as string) || t.toLowerCase(),
        style: (p.style as string) || "DEDICATED_COLLECTION",
        properties: extractProps(p),
      });
    }
  }
  return { entities, relationships };
}

function deepClone<T>(obj: T): T {
  return JSON.parse(JSON.stringify(obj));
}

function addEntityToMapping(
  mapping: Record<string, unknown>,
  name: string,
  collection: string,
  style: string,
): Record<string, unknown> {
  const m = deepClone(mapping);
  const csKey = m.conceptual_schema ? "conceptual_schema" : "conceptualSchema";
  const pmKey = m.physical_mapping ? "physical_mapping" : "physicalMapping";

  const cs = ((m[csKey] as Record<string, unknown>) ?? {});
  const pm = ((m[pmKey] as Record<string, unknown>) ?? {});

  if (!m[csKey]) m[csKey] = cs;
  if (!m[pmKey]) m[pmKey] = pm;

  const csEntities = (cs.entities ?? []) as Record<string, unknown>[];
  csEntities.push({ name, labels: [name], properties: [] });
  cs.entities = csEntities;

  const pmEntities = ((pm.entities ?? {}) as Record<string, Record<string, unknown>>);
  pmEntities[name] = { style, collectionName: collection, properties: {} };
  pm.entities = pmEntities;

  return m;
}

function deleteEntityFromMapping(
  mapping: Record<string, unknown>,
  name: string,
): Record<string, unknown> {
  const m = deepClone(mapping);
  const csKey = m.conceptual_schema ? "conceptual_schema" : "conceptualSchema";
  const pmKey = m.physical_mapping ? "physical_mapping" : "physicalMapping";

  const cs = ((m[csKey] as Record<string, unknown>) ?? {});
  const pm = ((m[pmKey] as Record<string, unknown>) ?? {});

  if (Array.isArray(cs.entities)) {
    cs.entities = (cs.entities as Record<string, unknown>[]).filter(
      (e) => e.name !== name,
    );
  }
  if (Array.isArray(cs.relationships)) {
    cs.relationships = (cs.relationships as Record<string, unknown>[]).filter(
      (r) => r.fromEntity !== name && r.toEntity !== name,
    );
  }

  const pmEntities = (pm.entities ?? {}) as Record<string, unknown>;
  delete pmEntities[name];

  const pmRels = (pm.relationships ?? {}) as Record<string, Record<string, unknown>>;
  for (const [k, v] of Object.entries(pmRels)) {
    if (v.domain === name || v.range === name) delete pmRels[k];
  }

  return m;
}

function addRelationshipToMapping(
  mapping: Record<string, unknown>,
  type: string,
  from: string,
  to: string,
  edgeCollection: string,
): Record<string, unknown> {
  const m = deepClone(mapping);
  const csKey = m.conceptual_schema ? "conceptual_schema" : "conceptualSchema";
  const pmKey = m.physical_mapping ? "physical_mapping" : "physicalMapping";

  const cs = ((m[csKey] as Record<string, unknown>) ?? {});
  const pm = ((m[pmKey] as Record<string, unknown>) ?? {});

  if (!m[csKey]) m[csKey] = cs;
  if (!m[pmKey]) m[pmKey] = pm;

  const csRels = (cs.relationships ?? []) as Record<string, unknown>[];
  csRels.push({ type, fromEntity: from, toEntity: to, properties: [] });
  cs.relationships = csRels;

  const pmRels = ((pm.relationships ?? {}) as Record<string, Record<string, unknown>>);
  pmRels[type] = {
    style: "DEDICATED_COLLECTION",
    edgeCollectionName: edgeCollection,
    domain: from,
    range: to,
    properties: {},
  };
  pm.relationships = pmRels;

  return m;
}

function updateEntityInMapping(
  mapping: Record<string, unknown>,
  oldName: string,
  newName: string,
  collection: string,
  style: string,
): Record<string, unknown> {
  const m = deepClone(mapping);
  const csKey = m.conceptual_schema ? "conceptual_schema" : "conceptualSchema";
  const pmKey = m.physical_mapping ? "physical_mapping" : "physicalMapping";

  const cs = ((m[csKey] as Record<string, unknown>) ?? {});
  const pm = ((m[pmKey] as Record<string, unknown>) ?? {});

  if (Array.isArray(cs.entities)) {
    for (const e of cs.entities as Record<string, unknown>[]) {
      if (e.name === oldName) {
        e.name = newName;
        e.labels = [newName];
      }
    }
  }
  if (Array.isArray(cs.relationships)) {
    for (const r of cs.relationships as Record<string, unknown>[]) {
      if (r.fromEntity === oldName) r.fromEntity = newName;
      if (r.toEntity === oldName) r.toEntity = newName;
    }
  }

  const pmEntities = (pm.entities ?? {}) as Record<string, Record<string, unknown>>;
  if (pmEntities[oldName]) {
    const ent = pmEntities[oldName];
    ent.collectionName = collection;
    ent.style = style;
    if (oldName !== newName) {
      pmEntities[newName] = ent;
      delete pmEntities[oldName];
    }
  }

  const pmRels = (pm.relationships ?? {}) as Record<string, Record<string, unknown>>;
  for (const v of Object.values(pmRels)) {
    if (v.domain === oldName) v.domain = newName;
    if (v.range === oldName) v.range = newName;
  }

  return m;
}

function EditDialog({
  dialog,
  entityNames,
  onClose,
  onSubmit,
}: {
  dialog: DialogState;
  entityNames: string[];
  onClose: () => void;
  onSubmit: (d: DialogState) => void;
}) {
  const [name, setName] = useState(dialog.entityName ?? "");
  const [collection, setCollection] = useState(dialog.entityCollection ?? "");
  const [style, setStyle] = useState(dialog.entityStyle ?? "COLLECTION");
  const [relType, setRelType] = useState(dialog.relType ?? "");
  const [relFrom, setRelFrom] = useState(dialog.relFrom ?? entityNames[0] ?? "");
  const [relTo, setRelTo] = useState(dialog.relTo ?? entityNames[0] ?? "");
  const [relColl, setRelColl] = useState(dialog.relCollection ?? "");

  if (!dialog.kind) return null;

  const isRelDialog = dialog.kind === "add-relationship";
  const title = dialog.kind === "add-entity"
    ? "Add Entity"
    : dialog.kind === "edit-entity"
    ? "Edit Entity"
    : "Add Relationship";

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (isRelDialog) {
      if (!relType.trim()) return;
      onSubmit({
        ...dialog,
        relType: relType.trim(),
        relFrom,
        relTo,
        relCollection: relColl.trim() || relType.trim().toLowerCase(),
      });
    } else {
      if (!name.trim()) return;
      onSubmit({
        ...dialog,
        entityName: name.trim(),
        entityCollection: collection.trim() || name.trim().toLowerCase() + "s",
        entityStyle: style,
      });
    }
  };

  const inputClass =
    "w-full bg-gray-800 text-gray-200 text-xs rounded px-2.5 py-1.5 border border-gray-700 focus:border-indigo-500 focus:outline-none";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <form
        onSubmit={handleSubmit}
        className="bg-gray-900 border border-gray-700 rounded-lg shadow-2xl w-80 p-4 space-y-3"
      >
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-white">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-200 text-lg leading-none"
          >
            &times;
          </button>
        </div>

        {isRelDialog ? (
          <>
            <div>
              <label className="text-[10px] text-gray-500 uppercase block mb-1">Type</label>
              <input value={relType} onChange={(e) => setRelType(e.target.value)} className={inputClass} placeholder="KNOWS" autoFocus />
            </div>
            <div>
              <label className="text-[10px] text-gray-500 uppercase block mb-1">From</label>
              <select value={relFrom} onChange={(e) => setRelFrom(e.target.value)} className={inputClass}>
                {entityNames.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </div>
            <div>
              <label className="text-[10px] text-gray-500 uppercase block mb-1">To</label>
              <select value={relTo} onChange={(e) => setRelTo(e.target.value)} className={inputClass}>
                {entityNames.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </div>
            <div>
              <label className="text-[10px] text-gray-500 uppercase block mb-1">Edge Collection</label>
              <input value={relColl} onChange={(e) => setRelColl(e.target.value)} className={inputClass} placeholder="auto-derived" />
            </div>
          </>
        ) : (
          <>
            <div>
              <label className="text-[10px] text-gray-500 uppercase block mb-1">Name</label>
              <input value={name} onChange={(e) => setName(e.target.value)} className={inputClass} placeholder="Person" autoFocus />
            </div>
            <div>
              <label className="text-[10px] text-gray-500 uppercase block mb-1">Collection</label>
              <input value={collection} onChange={(e) => setCollection(e.target.value)} className={inputClass} placeholder="auto-derived" />
            </div>
            <div>
              <label className="text-[10px] text-gray-500 uppercase block mb-1">Style</label>
              <select value={style} onChange={(e) => setStyle(e.target.value)} className={inputClass}>
                <option value="COLLECTION">Collection</option>
                <option value="TYPE_DISCRIMINATOR">Type Discriminator</option>
              </select>
            </div>
          </>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} className="px-3 py-1.5 text-xs rounded bg-gray-700 text-gray-300 hover:bg-gray-600 transition-colors">
            Cancel
          </button>
          <button type="submit" className="px-3 py-1.5 text-xs rounded bg-indigo-600 text-white hover:bg-indigo-500 transition-colors font-medium">
            {dialog.kind === "edit-entity" ? "Save" : "Add"}
          </button>
        </div>
      </form>
    </div>
  );
}

export default function CytoscapeSchemaGraph({ mapping, onMappingChange }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const [selected, setSelected] = useState<SelectedItem | null>(null);
  const [contextMenu, setContextMenu] = useState<ContextMenu | null>(null);
  const [dialog, setDialog] = useState<DialogState>({ kind: null });

  const onMappingChangeRef = useRef(onMappingChange);
  onMappingChangeRef.current = onMappingChange;

  const { entities, relationships } = useMemo(
    () => extractMapping(mapping),
    [mapping],
  );

  const entityNames = useMemo(() => entities.map((e) => e.name), [entities]);

  const applyMapping = useCallback((newMapping: Record<string, unknown>) => {
    onMappingChangeRef.current?.(newMapping);
  }, []);

  const buildElements = useCallback(() => {
    const elems: cytoscape.ElementDefinition[] = [];
    const physCollections = new Set<string>();

    entities.forEach((e, i) => {
      physCollections.add(e.collection);
      elems.push({
        data: {
          id: `concept-${e.name}`,
          label: e.name,
          subtitle: "class",
          color: CONCEPT_COLORS[i % CONCEPT_COLORS.length],
          kind: "entity",
          layer: "concept",
          entityName: e.name,
          collection: e.collection,
          style: e.style,
          propCount: e.properties.length,
        },
      });
    });

    relationships.forEach((r) => {
      if (r.edgeCollection) physCollections.add(r.edgeCollection);
    });

    const physArr = Array.from(physCollections);
    physArr.forEach((coll) => {
      const mappedEntities = entities.filter((e) => e.collection === coll);
      const mappedRels = relationships.filter((r) => r.edgeCollection === coll);
      const styleLabel = mappedEntities.length > 0
        ? mappedEntities[0].style.replace(/_/g, " ").toLowerCase()
        : mappedRels.length > 0
        ? mappedRels[0].style.replace(/_/g, " ").toLowerCase()
        : "collection";
      elems.push({
        data: {
          id: `phys-${coll}`,
          label: coll,
          subtitle: styleLabel,
          color: PHYS_COLOR,
          kind: "physical",
          layer: "physical",
          entityName: "",
          collection: coll,
          style: "",
          propCount: 0,
        },
      });
    });

    entities.forEach((e) => {
      elems.push({
        data: {
          id: `map-${e.name}`,
          source: `concept-${e.name}`,
          target: `phys-${e.collection}`,
          label: "",
          kind: "mapping",
        },
      });
    });

    relationships.forEach((r) => {
      elems.push({
        data: {
          id: `rel-${r.type}`,
          source: `concept-${r.from}`,
          target: `concept-${r.to}`,
          label: r.type,
          edgeCollection: r.edgeCollection,
          relStyle: r.style,
          kind: "relationship",
          relType: r.type,
          relFrom: r.from,
          relTo: r.to,
        },
      });

      if (r.edgeCollection) {
        elems.push({
          data: {
            id: `relmap-${r.type}`,
            source: `rel-${r.type}`,
            target: `phys-${r.edgeCollection}`,
            label: "",
            kind: "mapping",
          },
        });
      }
    });

    return elems;
  }, [entities, relationships]);

  useEffect(() => {
    const onClick = () => setContextMenu(null);
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  useEffect(() => {
    if (!containerRef.current) return;

    const elements = buildElements();

    const conceptNodes = elements.filter(
      (e) => e.data.layer === "concept"
    );
    const physNodes = elements.filter(
      (e) => e.data.layer === "physical"
    );
    const containerW = containerRef.current.clientWidth || 800;
    const leftX = containerW * 0.25;
    const rightX = containerW * 0.75;
    const conceptSpacing = Math.max(80, 400 / Math.max(conceptNodes.length, 1));
    const physSpacing = Math.max(80, 400 / Math.max(physNodes.length, 1));
    const conceptStartY = 60;
    const physStartY = 60;

    conceptNodes.forEach((n, i) => {
      n.position = { x: leftX, y: conceptStartY + i * conceptSpacing };
    });
    physNodes.forEach((n, i) => {
      n.position = { x: rightX, y: physStartY + i * physSpacing };
    });

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      layout: { name: "preset" },
      style: [
        {
          selector: "node[layer='concept']",
          style: {
            shape: "round-rectangle",
            width: 130,
            height: 36,
            "background-color": "#1e1b4b",
            "border-width": 2,
            "border-color": "data(color)",
            label: "data(label)",
            "text-valign": "center",
            "text-halign": "center",
            "font-size": "12px",
            "font-weight": "bold",
            color: "data(color)",
            "text-wrap": "wrap",
          } as unknown as cytoscape.Css.Node,
        },
        {
          selector: "node[layer='physical']",
          style: {
            shape: "rectangle",
            width: 130,
            height: 32,
            "background-color": "#1e293b",
            "border-width": 1.5,
            "border-color": PHYS_COLOR,
            "border-style": "dashed" as cytoscape.Css.LineStyle,
            label: "data(label)",
            "text-valign": "center",
            "text-halign": "center",
            "font-size": "11px",
            "font-weight": "normal",
            "font-style": "italic" as cytoscape.Css.FontStyle,
            color: "#94a3b8",
            "text-wrap": "wrap",
          } as unknown as cytoscape.Css.Node,
        },
        {
          selector: "node.selected",
          style: {
            "border-width": 3,
            "border-color": "#e5e7eb",
          } as cytoscape.Css.Node,
        },
        {
          selector: "edge[kind='relationship']",
          style: {
            width: 2,
            "line-color": "#818cf8",
            "target-arrow-color": "#818cf8",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            label: "data(label)",
            "font-size": "9px",
            "font-weight": 600,
            color: "#c7d2fe",
            "text-background-color": "#0f172a",
            "text-background-opacity": 0.9,
            "text-background-padding": "3px",
            "text-rotation": "autorotate",
          } as cytoscape.Css.Edge,
        },
        {
          selector: "edge[kind='mapping']",
          style: {
            width: 1,
            "line-color": MAP_EDGE_COLOR,
            "line-style": "dashed" as cytoscape.Css.LineStyle,
            "line-dash-pattern": [6, 4],
            "target-arrow-shape": "none",
            "curve-style": "bezier",
            opacity: 0.6,
          } as unknown as cytoscape.Css.Edge,
        },
        {
          selector: "edge:active",
          style: {
            "overlay-opacity": 0.1,
            "overlay-color": "#818cf8",
          } as cytoscape.Css.Edge,
        },
      ],
      minZoom: 0.1,
      maxZoom: 5,
      wheelSensitivity: 0.3,
    });

    cy.on("tap", "node", (evt: EventObject) => {
      const node = evt.target as NodeSingular;
      cy.nodes().removeClass("selected");
      node.addClass("selected");
      const d = node.data();
      if (d.layer === "physical") {
        const mappedEnts = entities.filter((e) => e.collection === d.collection);
        const mappedRels = relationships.filter((r) => r.edgeCollection === d.collection);
        setSelected({
          kind: "entity",
          name: d.label,
          collection: d.collection,
          style: mappedEnts.length > 0 ? mappedEnts.map((e) => e.name).join(", ") : mappedRels.map((r) => r.type).join(", "),
          properties: [],
        });
        return;
      }
      const ent = entities.find((e) => e.name === d.entityName);
      if (ent) {
        setSelected({
          kind: "entity",
          name: ent.name,
          collection: ent.collection,
          style: ent.style,
          properties: ent.properties,
        });
      }
    });

    cy.on("tap", "edge", (evt: EventObject) => {
      const edge = evt.target;
      const d = edge.data();
      cy.nodes().removeClass("selected");
      const rel = relationships.find((r) => r.type === d.relType);
      if (rel) {
        setSelected({
          kind: "relationship",
          name: rel.type,
          collection: rel.edgeCollection,
          style: rel.style,
          properties: rel.properties,
          from: rel.from,
          to: rel.to,
        });
      }
    });

    cy.on("tap", (evt: EventObject) => {
      if (evt.target === cy) {
        cy.nodes().removeClass("selected");
        setSelected(null);
      }
    });

    cy.on("cxttap", "node", (evt: EventObject) => {
      evt.originalEvent.preventDefault();
      const node = evt.target as NodeSingular;
      const d = node.data();
      if (d.layer === "physical") return;
      const rp = node.renderedPosition();
      const container = containerRef.current;
      if (!container) return;
      const rect = container.getBoundingClientRect();
      setContextMenu({
        x: rect.left + rp.x,
        y: rect.top + rp.y,
        items: [
          {
            label: "Edit Entity",
            action: () => {
              const ent = entities.find((e) => e.name === d.entityName);
              setDialog({
                kind: "edit-entity",
                entityName: d.entityName,
                entityCollection: ent?.collection ?? d.collection,
                entityStyle: ent?.style ?? d.style,
              });
            },
          },
          {
            label: "Add Relationship From Here",
            action: () => {
              setDialog({
                kind: "add-relationship",
                relFrom: d.entityName,
                relTo: entities[0]?.name ?? "",
              });
            },
          },
          {
            label: "Delete Entity",
            action: () => {
              applyMapping(deleteEntityFromMapping(mapping, d.entityName));
              setSelected(null);
            },
          },
        ],
      });
    });

    cy.on("cxttap", (evt: EventObject) => {
      if (evt.target === cy) {
        evt.originalEvent.preventDefault();
        const rp = evt.renderedPosition;
        const container = containerRef.current;
        if (!container) return;
        const rect = container.getBoundingClientRect();
        setContextMenu({
          x: rect.left + (rp?.x ?? 0),
          y: rect.top + (rp?.y ?? 0),
          items: [
            {
              label: "Add Entity",
              action: () => setDialog({ kind: "add-entity" }),
            },
            {
              label: "Add Relationship",
              action: () => setDialog({ kind: "add-relationship" }),
            },
          ],
        });
      }
    });

    cy.on("dbltap", "node[layer='concept']", (evt: EventObject) => {
      const d = (evt.target as NodeSingular).data();
      const ent = entities.find((e) => e.name === d.entityName);
      setDialog({
        kind: "edit-entity",
        entityName: d.entityName,
        entityCollection: ent?.collection ?? d.collection,
        entityStyle: ent?.style ?? d.style,
      });
    });

    cyRef.current = cy;

    return () => {
      cy.destroy();
      cyRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !containerRef.current) return;

    cy.elements().remove();
    const elements = buildElements();

    const conceptNodes = elements.filter((e) => e.data.layer === "concept");
    const physNodes = elements.filter((e) => e.data.layer === "physical");
    const containerW = containerRef.current.clientWidth || 800;
    const leftX = containerW * 0.25;
    const rightX = containerW * 0.75;
    const conceptSpacing = Math.max(80, 400 / Math.max(conceptNodes.length, 1));
    const physSpacing = Math.max(80, 400 / Math.max(physNodes.length, 1));

    conceptNodes.forEach((n, i) => {
      n.position = { x: leftX, y: 60 + i * conceptSpacing };
    });
    physNodes.forEach((n, i) => {
      n.position = { x: rightX, y: 60 + i * physSpacing };
    });

    cy.add(elements);
    cy.fit(undefined, 40);
  }, [buildElements]);

  const handleFit = useCallback(() => {
    cyRef.current?.fit(undefined, 50);
  }, []);

  const handleZoomIn = useCallback(() => {
    const cy = cyRef.current;
    if (cy) cy.zoom({ level: cy.zoom() * 1.3, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }, []);

  const handleZoomOut = useCallback(() => {
    const cy = cyRef.current;
    if (cy) cy.zoom({ level: cy.zoom() / 1.3, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }, []);

  const handleDialogSubmit = useCallback(
    (d: DialogState) => {
      if (d.kind === "add-entity" && d.entityName) {
        applyMapping(
          addEntityToMapping(mapping, d.entityName, d.entityCollection || d.entityName.toLowerCase() + "s", d.entityStyle || "COLLECTION"),
        );
      } else if (d.kind === "edit-entity" && d.entityName && dialog.entityName) {
        applyMapping(
          updateEntityInMapping(mapping, dialog.entityName, d.entityName, d.entityCollection || d.entityName.toLowerCase() + "s", d.entityStyle || "COLLECTION"),
        );
      } else if (d.kind === "add-relationship" && d.relType && d.relFrom && d.relTo) {
        applyMapping(
          addRelationshipToMapping(mapping, d.relType, d.relFrom, d.relTo, d.relCollection || d.relType.toLowerCase()),
        );
      }
      setDialog({ kind: null });
    },
    [mapping, dialog.entityName, applyMapping],
  );

  if (entities.length === 0 && !onMappingChange) {
    return (
      <div className="flex items-center justify-center h-full text-gray-400 text-sm bg-gray-950">
        Load a mapping to see the schema graph.
      </div>
    );
  }

  return (
    <div className="h-full bg-gray-950 relative flex">
      <div className={`relative ${selected ? "flex-1" : "w-full"}`}>
        <div ref={containerRef} className="w-full h-full" />
        <div className="absolute top-2 left-0 right-0 flex justify-around pointer-events-none z-10">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-indigo-400/70">
            Conceptual Schema
          </span>
          <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400/70">
            Physical Collections
          </span>
        </div>
        <div className="absolute top-2 right-2 z-10 flex gap-1">
          {onMappingChange && (
            <button
              onClick={() => setDialog({ kind: "add-entity" })}
              className="h-7 px-2 rounded bg-gray-800/80 text-gray-300 hover:bg-gray-700 text-[10px] font-medium flex items-center justify-center backdrop-blur"
              title="Add entity"
            >
              + Entity
            </button>
          )}
          <button
            onClick={handleZoomIn}
            className="w-7 h-7 rounded bg-gray-800/80 text-gray-300 hover:bg-gray-700 text-sm font-bold flex items-center justify-center backdrop-blur"
          >
            +
          </button>
          <button
            onClick={handleZoomOut}
            className="w-7 h-7 rounded bg-gray-800/80 text-gray-300 hover:bg-gray-700 text-sm font-bold flex items-center justify-center backdrop-blur"
          >
            &minus;
          </button>
          <button
            onClick={handleFit}
            className="h-7 px-2 rounded bg-gray-800/80 text-gray-300 hover:bg-gray-700 text-[10px] font-medium flex items-center justify-center backdrop-blur"
          >
            Fit
          </button>
        </div>
        <div className="absolute bottom-2 left-2 z-10 text-[10px] text-gray-600">
          {entities.length} classes, {relationships.length} relationships &mdash; {new Set(entities.map((e) => e.collection).concat(relationships.map((r) => r.edgeCollection).filter(Boolean))).size} collections
          {onMappingChange && (
            <span className="ml-2 text-gray-700">(right-click to edit)</span>
          )}
        </div>
      </div>

      {contextMenu && (
        <div
          className="fixed z-50 bg-gray-800 border border-gray-700 rounded shadow-xl py-1 min-w-[160px]"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          {contextMenu.items.map((item, i) => (
            <button
              key={i}
              onClick={(e) => {
                e.stopPropagation();
                setContextMenu(null);
                item.action();
              }}
              className="w-full text-left px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700 hover:text-white transition-colors"
            >
              {item.label}
            </button>
          ))}
        </div>
      )}

      {dialog.kind && (
        <EditDialog
          dialog={dialog}
          entityNames={entityNames}
          onClose={() => setDialog({ kind: null })}
          onSubmit={handleDialogSubmit}
        />
      )}

      {selected && (
        <div className="w-60 border-l border-gray-800 overflow-auto p-3 bg-gray-900/50 shrink-0">
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-semibold text-indigo-400 uppercase">
              {selected.kind}
            </span>
            <button
              onClick={() => setSelected(null)}
              className="text-[10px] text-gray-500 hover:text-gray-300 transition-colors"
            >
              Close
            </button>
          </div>
          <div className="text-sm font-semibold text-white mb-1">
            {selected.name}
          </div>
          <div className="text-[10px] text-gray-500 font-mono mb-1">
            {selected.collection}
          </div>
          <div className="text-[10px] text-gray-600 mb-3">
            {selected.style.replace(/_/g, " ").toLowerCase()}
          </div>
          {selected.from && (
            <div className="text-[10px] text-gray-500 mb-1">
              {selected.from} &rarr; {selected.to}
            </div>
          )}
          {selected.properties.length > 0 && (
            <>
              <div className="text-[10px] text-gray-400 uppercase font-semibold mb-1 mt-2">
                Properties
              </div>
              <div className="space-y-0.5">
                {selected.properties.map((p) => (
                  <div key={p.name} className="flex items-center gap-2">
                    <span className="text-xs text-gray-300 font-mono">
                      {p.name}
                    </span>
                    <span className="text-[10px] text-gray-600 font-mono">
                      {p.type}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
          {onMappingChange && selected.kind === "entity" && (
            <div className="mt-3 flex gap-1.5">
              <button
                onClick={() =>
                  setDialog({
                    kind: "edit-entity",
                    entityName: selected.name,
                    entityCollection: selected.collection,
                    entityStyle: selected.style,
                  })
                }
                className="px-2 py-1 text-[10px] rounded bg-gray-700 text-gray-300 hover:bg-gray-600 transition-colors"
              >
                Edit
              </button>
              <button
                onClick={() => {
                  applyMapping(deleteEntityFromMapping(mapping, selected.name));
                  setSelected(null);
                }}
                className="px-2 py-1 text-[10px] rounded bg-red-900/40 text-red-300 hover:bg-red-900/60 transition-colors"
              >
                Delete
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
