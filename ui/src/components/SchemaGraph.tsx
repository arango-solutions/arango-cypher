import { useCallback, useEffect, useMemo, useRef, useState } from "react";

interface Props {
  mapping: Record<string, unknown>;
}

interface PropInfo { name: string; field: string; type: string }
interface EntityInfo { name: string; collection: string; style: string; properties: PropInfo[] }
interface RelInfo { type: string; from: string; to: string; edgeCollection: string; style: string; properties: PropInfo[]; edgeCount: number }

/* ── Data extraction ────────────────────────────────────────────────── */

function extractMapping(mapping: Record<string, unknown>): { entities: EntityInfo[]; relationships: RelInfo[] } {
  const cs = (mapping.conceptualSchema ?? mapping.conceptual_schema ?? {}) as Record<string, unknown>;
  const pm = (mapping.physicalMapping ?? mapping.physical_mapping ?? {}) as Record<string, unknown>;
  const pmE = (pm.entities ?? {}) as Record<string, Record<string, unknown>>;
  const pmR = (pm.relationships ?? {}) as Record<string, Record<string, unknown>>;

  const csRich = cs.entities as Record<string, unknown>[] | undefined;
  const csTypes = cs.entityTypes as string[] | undefined;
  const csRRich = cs.relationships as Record<string, unknown>[] | undefined;
  const csRTypes = cs.relationshipTypes as string[] | undefined;

  function extractProps(obj: Record<string, unknown>): PropInfo[] {
    const raw = (obj.properties ?? {}) as Record<string, unknown>;
    return Object.entries(raw).slice(0, 8).map(([k, v]) => {
      if (v && typeof v === "object") { const o = v as Record<string, string>; return { name: k, field: o.field || k, type: o.type || "string" }; }
      return { name: k, field: k, type: "string" };
    });
  }

  const names: string[] = [];
  if (Array.isArray(csRich) && csRich.length > 0) csRich.forEach((e) => { const n = (e.name as string) || ""; if (n) names.push(n); });
  else if (Array.isArray(csTypes)) names.push(...csTypes.filter(Boolean));
  else names.push(...Object.keys(pmE));

  const entities: EntityInfo[] = names.map((n) => {
    const p = pmE[n] ?? {};
    return { name: n, collection: (p.collectionName as string) || n.toLowerCase() + "s", style: (p.style as string) || "COLLECTION", properties: extractProps(p) };
  });

  const relationships: RelInfo[] = [];
  if (Array.isArray(csRRich) && csRRich.length > 0) {
    for (const r of csRRich) {
      const t = (r.type as string) || ""; if (!t) continue;
      const p = pmR[t] ?? {};
      relationships.push({ type: t, from: (r.fromEntity as string) || "", to: (r.toEntity as string) || "", edgeCollection: (p.edgeCollectionName as string) || t.toLowerCase(), style: (p.style as string) || "DEDICATED_COLLECTION", properties: extractProps(p), edgeCount: (p.edgeCount as number) || 0 });
    }
  } else {
    const rn = Array.isArray(csRTypes) ? csRTypes : Object.keys(pmR);
    for (const t of rn) {
      if (!t) continue; const p = pmR[t] ?? {};
      const f = (p.domain as string) || (p.fromEntity as string) || (names[0] ?? "");
      const to = (p.range as string) || (p.toEntity as string) || (names[names.length - 1] ?? "");
      relationships.push({ type: t, from: f, to, edgeCollection: (p.edgeCollectionName as string) || t.toLowerCase(), style: (p.style as string) || "DEDICATED_COLLECTION", properties: extractProps(p), edgeCount: (p.edgeCount as number) || 0 });
    }
  }
  return { entities, relationships };
}

/* ── Layout constants ───────────────────────────────────────────────── */

const CARD_W = 200;
const CARD_HEADER = 36;
const ROW_H = 20;
const ROW_PAD = 6;
const MAPPING_GAP = 200;  // horizontal gap between ontology card and physical card
const PAIR_GAP_Y = 60;    // vertical gap between entity pairs

function fmtCount(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}k`;
  return String(n);
}

function cardH(propCount: number): number {
  if (propCount === 0) return CARD_HEADER;
  return CARD_HEADER + ROW_PAD + propCount * ROW_H + ROW_PAD;
}

const ONTO_COLORS = [
  { fill: "#1a1a3e", stroke: "#818cf8", text: "#c7d2fe" },
  { fill: "#1a2e1a", stroke: "#4ade80", text: "#bbf7d0" },
  { fill: "#2e1f0e", stroke: "#fbbf24", text: "#fde68a" },
  { fill: "#2e1515", stroke: "#f87171", text: "#fecaca" },
  { fill: "#251540", stroke: "#a78bfa", text: "#ddd6fe" },
  { fill: "#0e2a33", stroke: "#22d3ee", text: "#a5f3fc" },
];
const PHYS_COLORS: Record<string, { fill: string; stroke: string }> = {
  COLLECTION: { fill: "#0f2942", stroke: "#3b82f6" },
  LABEL: { fill: "#0f2942", stroke: "#3b82f6" },
  GENERIC_WITH_TYPE: { fill: "#162316", stroke: "#22c55e" },
  DEDICATED_COLLECTION: { fill: "#2a1a0a", stroke: "#f59e0b" },
};
const PROP_EDGE_COLORS = ["#6366f1", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#06b6d4"];

/* ── Layout computation ─────────────────────────────────────────────── */

interface CardPos { x: number; y: number; w: number; h: number }

interface Layout {
  ontoCards: Map<string, CardPos>;
  physCards: Map<string, CardPos>;
  relEdgeCards: Map<string, CardPos>;
  bounds: { x: number; y: number; w: number; h: number };
}

const LEFT_MARGIN = 160; // room for self-loop arrows on the left

function computeLayout(entities: EntityInfo[], relationships: RelInfo[]): Layout {
  const ontoCards = new Map<string, CardPos>();
  const physCards = new Map<string, CardPos>();
  const relEdgeCards = new Map<string, CardPos>();

  const ontoX = LEFT_MARGIN;
  const physX = ontoX + CARD_W + MAPPING_GAP;
  let curY = 60;

  // Group entities by physical collection for LPG deduplication
  const collectionEntities = new Map<string, EntityInfo[]>();
  for (const e of entities) {
    const group = collectionEntities.get(e.collection) || [];
    group.push(e);
    collectionEntities.set(e.collection, group);
  }

  for (const [collection, group] of collectionEntities) {
    const groupStartY = curY;
    for (const e of group) {
      const h = cardH(e.properties.length);
      ontoCards.set(e.name, { x: ontoX, y: curY, w: CARD_W, h });
      curY += h + PAIR_GAP_Y;
    }
    // Position the physical card vertically centred across its ontology group
    const firstPos = ontoCards.get(group[0].name)!;
    const lastPos = ontoCards.get(group[group.length - 1].name)!;
    const maxProps = Math.max(...group.map((e) => e.properties.length));
    const physH = cardH(maxProps);
    const centerY = (firstPos.y + lastPos.y + lastPos.h) / 2 - physH / 2;
    physCards.set(collection, { x: physX, y: Math.max(groupStartY, centerY), w: CARD_W, h: physH });
  }

  // Group relationships by their shared physical edge collection — one
  // `relations` collection for all GENERIC_WITH_TYPE / LABEL relationship types
  // (the GraphRAG shape), or one collection per type for DEDICATED_COLLECTION.
  // Mirrors the vertex-collection dedup above so a single edge-collection card
  // is drawn regardless of style (previously only DEDICATED_COLLECTION got one,
  // so type-discriminated edge mappings were silently omitted).
  const edgeCollRels = new Map<string, RelInfo[]>();
  for (const r of relationships) {
    if (!r.edgeCollection || physCards.has(r.edgeCollection)) continue;
    const group = edgeCollRels.get(r.edgeCollection) || [];
    group.push(r);
    edgeCollRels.set(r.edgeCollection, group);
  }
  for (const [collection, group] of edgeCollRels) {
    const maxProps = Math.max(0, ...group.map((r) => r.properties.length));
    const h = cardH(maxProps);
    relEdgeCards.set(collection, { x: physX, y: curY, w: CARD_W, h });
    curY += h + PAIR_GAP_Y;
  }

  const maxX = physX + CARD_W + 80;

  return { ontoCards, physCards, relEdgeCards, bounds: { x: 0, y: 0, w: maxX, h: curY + 20 } };
}

/* ── Rendering helpers ──────────────────────────────────────────────── */

function Card({ pos, label, subtitle, props, fill, stroke, textColor, tag }: {
  pos: CardPos; label: string; subtitle?: string; props: PropInfo[];
  fill: string; stroke: string; textColor: string; tag?: string;
}) {
  const { x, y, w, h } = pos;
  return (
    <g>
      <rect x={x + 2} y={y + 2} width={w} height={h} rx={8} fill="rgba(0,0,0,0.3)" />
      <rect x={x} y={y} width={w} height={h} rx={8} fill={fill} stroke={stroke} strokeWidth={2} />
      {props.length > 0 && <line x1={x + 8} y1={y + CARD_HEADER} x2={x + w - 8} y2={y + CARD_HEADER} stroke={stroke} strokeWidth={0.5} opacity={0.4} />}
      {tag && <text x={x + 10} y={y + 14} fill={stroke} fontSize={8} fontWeight="600" fontFamily="monospace">{tag}</text>}
      <text x={x + (tag ? 28 : w / 2)} y={y + (subtitle ? 16 : 22)} fill={textColor} fontSize={14} fontWeight="700" textAnchor={tag ? "start" : "middle"} fontFamily="system-ui, sans-serif">{label}</text>
      {subtitle && <text x={x + (tag ? 28 : w / 2)} y={y + 30} fill="#64748b" fontSize={9} textAnchor={tag ? "start" : "middle"} fontFamily="monospace">{subtitle}</text>}
      {props.map((p, j) => (
        <g key={p.name + j}>
          <text x={x + 12} y={y + CARD_HEADER + ROW_PAD + j * ROW_H + 14} fill="#d1d5db" fontSize={11} fontFamily="monospace">{tag ? p.field : p.name}</text>
          <text x={x + w - 12} y={y + CARD_HEADER + ROW_PAD + j * ROW_H + 14} fill="#64748b" fontSize={10} textAnchor="end" fontFamily="monospace">{p.type}</text>
        </g>
      ))}
    </g>
  );
}

function PropEdge({ x1, y1, x2, y2, color }: { x1: number; y1: number; x2: number; y2: number; color: string }) {
  const cpx = (x1 + x2) / 2;
  return (
    <path
      d={`M ${x1} ${y1} C ${cpx} ${y1} ${cpx} ${y2} ${x2} ${y2}`}
      fill="none" stroke={color} strokeWidth={1.2} opacity={0.6}
      markerEnd="url(#prop-arrow)"
    />
  );
}

function OntologyRel({ type, fromPos, toPos, isSelf, weight = 1.5, selected = false, onSelect }: { type: string; fromPos: CardPos; toPos: CardPos; isSelf: boolean; weight?: number; selected?: boolean; onSelect?: () => void }) {
  const stroke = selected ? "#818cf8" : "#94a3b8";
  const pillStroke = selected ? "#818cf8" : "#475569";
  const pillFill = selected ? "#1e1b4b" : "#0f172a";
  const w = selected ? weight + 1.5 : weight;
  const click = onSelect
    ? (e: React.MouseEvent) => { e.stopPropagation(); onSelect(); }
    : undefined;
  const groupProps = onSelect ? { onClick: click, style: { cursor: "pointer" as const } } : {};
  if (isSelf) {
    // Self-loop on the LEFT side of the card, away from property edges
    const cx = fromPos.x;
    const midY = fromPos.y + fromPos.h / 2;
    const loopW = 70;
    const loopH = Math.max(45, fromPos.h * 0.4);
    const path = `M ${cx} ${midY - 12} C ${cx - loopW} ${midY - loopH} ${cx - loopW} ${midY + loopH} ${cx} ${midY + 12}`;
    const pillW = type.length * 7.5 + 16;
    const lx = cx - loopW - pillW / 2 - 4;
    return (
      <g {...groupProps}>
        <path d={path} fill="none" stroke={stroke} strokeWidth={w} strokeDasharray="6 3" markerEnd="url(#onto-arrow)" />
        <rect x={lx} y={midY - 11} width={pillW} height={22} rx={11} fill={pillFill} stroke={pillStroke} strokeWidth={selected ? 1.5 : 1} />
        <text x={lx + pillW / 2} y={midY + 1} fill="#e2e8f0" fontSize={10} fontWeight="600" textAnchor="middle" dominantBaseline="middle">{type}</text>
      </g>
    );
  }
  // Non-self: draw on the LEFT side between stacked cards
  const x1 = fromPos.x;
  const y1 = fromPos.y + fromPos.h;
  const x2 = toPos.x;
  const y2 = toPos.y;
  const mx = x1 - 50;
  const my = (y1 + y2) / 2;
  const pillW = Math.max(56, type.length * 7 + 14);
  return (
    <g {...groupProps}>
      <path d={`M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`} fill="none" stroke={stroke} strokeWidth={w} markerEnd="url(#onto-arrow)" />
      <rect x={mx - pillW / 2} y={my - 10} width={pillW} height={20} rx={10} fill={pillFill} stroke={pillStroke} strokeWidth={selected ? 1.5 : 1} />
      <text x={mx} y={my + 1} fill="#e2e8f0" fontSize={10} fontWeight="600" textAnchor="middle" dominantBaseline="middle">{type}</text>
    </g>
  );
}

/* ── Main component ─────────────────────────────────────────────────── */

export default function SchemaGraph({ mapping }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 600, h: 500 });
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const drag = useRef<{ sx: number; sy: number; px: number; py: number } | null>(null);

  const updateSize = useCallback(() => {
    if (containerRef.current) setSize({ w: containerRef.current.clientWidth, h: containerRef.current.clientHeight });
  }, []);

  useEffect(() => { updateSize(); const ro = new ResizeObserver(updateSize); if (containerRef.current) ro.observe(containerRef.current); return () => ro.disconnect(); }, [updateSize]);

  const { entities, relationships } = useMemo(() => extractMapping(mapping), [mapping]);
  const layout = useMemo(() => computeLayout(entities, relationships), [entities, relationships]);

  // Phase 2 — abstraction: bundle relationship types by entity-type pair so a
  // graph with hundreds of predicates (the GraphRAG shape) collapses to a few
  // dozen arcs. `relationships` arrives frequency-ranked from the analyzer cap,
  // so member order within a bundle is already by edge volume.
  const bundles = useMemo(() => {
    const m = new Map<string, { from: string; to: string; types: string[]; volume: number; isSelf: boolean }>();
    for (const r of relationships) {
      const key = `${r.from}\u0001${r.to}`;
      let b = m.get(key);
      if (!b) {
        b = { from: r.from, to: r.to, types: [], volume: 0, isSelf: r.from === r.to };
        m.set(key, b);
      }
      b.types.push(r.type);
      b.volume += r.edgeCount || 0;
    }
    // Rank by edge volume (falls back to predicate count when no counts exist).
    return Array.from(m.values()).sort(
      (a, b) => b.volume - a.volume || b.types.length - a.types.length,
    );
  }, [relationships]);

  const maxVolume = useMemo(
    () => bundles.reduce((mx, b) => Math.max(mx, b.volume), 0),
    [bundles],
  );
  // Log-scaled stroke width so a 7.8M-edge predicate doesn't dwarf a 1k one.
  const arcWeight = (vol: number) => {
    if (maxVolume <= 0 || vol <= 0) return 1.5;
    return 1.2 + 4.3 * (Math.log10(1 + vol) / Math.log10(1 + maxVolume));
  };

  const [relQuery, setRelQuery] = useState("");
  const q = relQuery.trim().toLowerCase();
  const visibleBundles = useMemo(() => {
    if (!q) return bundles;
    return bundles.filter(
      (b) =>
        b.from.toLowerCase().includes(q) ||
        b.to.toLowerCase().includes(q) ||
        b.types.some((t) => t.toLowerCase().includes(q)),
    );
  }, [bundles, q]);
  const bundleLabel = (b: { types: string[]; volume: number }) => {
    const base = b.types.length === 1 ? b.types[0] : `${b.types.length} predicates`;
    return b.volume > 0 ? `${base} · ${fmtCount(b.volume)}` : base;
  };

  // Click-to-expand: select a bundle (by arc or panel row) to highlight its arc
  // and reveal its full predicate list.
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const keyOf = (b: { from: string; to: string }) => `${b.from}\u0001${b.to}`;

  const fitToView = useCallback(() => {
    if (size.w === 0 || size.h === 0) return;
    const b = layout.bounds;
    const z = Math.min(size.w / b.w, size.h / b.h, 2.5) * 0.88;
    setPan({ x: size.w / 2 - (b.x + b.w / 2) * z, y: size.h / 2 - (b.y + b.h / 2) * z });
    setZoom(z);
  }, [size, layout.bounds]);

  useEffect(() => { fitToView(); }, [fitToView]);

  const onWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const rect = containerRef.current?.getBoundingClientRect(); if (!rect) return;
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    setZoom((z) => { const nz = Math.max(0.1, Math.min(5, z * f)); setPan((p) => ({ x: mx - (mx - p.x) * (nz / z), y: my - (my - p.y) * (nz / z) })); return nz; });
  }, []);
  const onPointerDown = useCallback((e: React.PointerEvent) => { if (e.button !== 0) return; (e.target as Element).setPointerCapture?.(e.pointerId); drag.current = { sx: e.clientX, sy: e.clientY, px: pan.x, py: pan.y }; }, [pan]);
  const onPointerMove = useCallback((e: React.PointerEvent) => { if (!drag.current) return; setPan({ x: drag.current.px + (e.clientX - drag.current.sx), y: drag.current.py + (e.clientY - drag.current.sy) }); }, []);
  const onPointerUp = useCallback(() => { drag.current = null; }, []);

  if (entities.length === 0) {
    return <div ref={containerRef} className="flex items-center justify-center h-full text-gray-400 text-sm bg-gray-950">Load a mapping to see the schema graph.</div>;
  }

  return (
    <div ref={containerRef} className="h-full bg-gray-950 relative select-none" style={{ overflow: "hidden", cursor: drag.current ? "grabbing" : "grab", touchAction: "none" }}
      onWheel={onWheel} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}>
      <div className="absolute top-2 right-2 z-10 flex gap-1">
        <button onClick={() => setZoom((z) => Math.min(5, z * 1.3))} className="w-7 h-7 rounded bg-gray-800/80 text-gray-300 hover:bg-gray-700 text-sm font-bold flex items-center justify-center backdrop-blur">+</button>
        <button onClick={() => setZoom((z) => Math.max(0.1, z / 1.3))} className="w-7 h-7 rounded bg-gray-800/80 text-gray-300 hover:bg-gray-700 text-sm font-bold flex items-center justify-center backdrop-blur">&minus;</button>
        <button onClick={fitToView} className="h-7 px-2 rounded bg-gray-800/80 text-gray-300 hover:bg-gray-700 text-[10px] font-medium flex items-center justify-center backdrop-blur">Fit</button>
      </div>
      <div className="absolute bottom-2 left-2 z-10 text-[10px] text-gray-600">{Math.round(zoom * 100)}%</div>

      {/* Relationship browser — search the predicate vocabulary; arcs are
          bundled by entity-type pair, this exposes the individual predicates. */}
      {bundles.length > 0 && (
        <div className="absolute top-2 left-2 z-10 w-52 max-h-[78%] flex flex-col bg-gray-900/85 border border-gray-700 rounded-md backdrop-blur shadow-xl">
          <div className="p-1.5 border-b border-gray-800">
            <input
              value={relQuery}
              onChange={(e) => setRelQuery(e.target.value)}
              placeholder={`Search ${relationships.length} predicates…`}
              className="w-full bg-gray-800 text-gray-200 rounded px-2 py-1 text-[11px] border border-gray-700 focus:border-indigo-500 focus:outline-none placeholder-gray-600"
            />
          </div>
          <div className="overflow-y-auto py-1">
            <div className="px-2 py-0.5 text-[10px] text-gray-500">
              {visibleBundles.length} of {bundles.length} type pairs
            </div>
            {visibleBundles.slice(0, 50).map((b) => {
              const key = keyOf(b);
              const isSel = selectedKey === key;
              const matched = q ? b.types.filter((t) => t.toLowerCase().includes(q)) : b.types;
              // Selected row expands to the full predicate list; otherwise truncate.
              const shown = isSel ? matched : matched.slice(0, 6);
              return (
                <button
                  type="button"
                  key={`${b.from}->${b.to}`}
                  onClick={() => setSelectedKey((cur) => (cur === key ? null : key))}
                  className={`w-full text-left px-2 py-1 transition-colors ${
                    isSel ? "bg-indigo-600/20" : "hover:bg-gray-800/60"
                  }`}
                >
                  <div className="text-[11px] text-gray-300">
                    {b.from} <span className="text-gray-600">&rarr;</span> {b.to}
                    <span className="text-gray-500"> · {b.types.length}</span>
                    {b.volume > 0 && (
                      <span className="text-gray-600"> · {fmtCount(b.volume)} edges</span>
                    )}
                  </div>
                  <div
                    className={`text-[10px] text-gray-500 ${isSel ? "whitespace-normal break-words" : "truncate"}`}
                    title={b.types.join(", ")}
                  >
                    {shown.join(", ")}
                    {!isSel && matched.length > 6 ? "…" : ""}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Legend */}
      <div className="absolute bottom-2 right-2 z-10 flex gap-3 text-[9px] text-gray-500">
        <span className="flex items-center gap-1"><span className="w-6 h-0.5 inline-block" style={{ background: PROP_EDGE_COLORS[0] }} />property mapping</span>
        <span className="flex items-center gap-1"><span className="w-6 h-0.5 inline-block border-t border-dashed border-gray-500" />type mapping</span>
        <span className="flex items-center gap-1"><span className="w-6 inline-block bg-slate-400" style={{ height: 3 }} />thicker = more edges</span>
      </div>

      <svg width={size.w} height={size.h} className="block">
        <defs>
          <marker id="onto-arrow" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="#94a3b8" /></marker>
          <marker id="prop-arrow" markerWidth="6" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0, 6 2.5, 0 5" fill="#818cf8" /></marker>
          <marker id="map-arrow" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="#475569" /></marker>
        </defs>
        <g transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}>
          {/* Column headers */}
          <text x={LEFT_MARGIN + CARD_W / 2} y={30} fill="#64748b" fontSize={12} fontWeight="700" textAnchor="middle" fontFamily="system-ui, sans-serif">CONCEPTUAL SCHEMA</text>
          <text x={LEFT_MARGIN + CARD_W + MAPPING_GAP + CARD_W / 2} y={30} fill="#64748b" fontSize={12} fontWeight="700" textAnchor="middle" fontFamily="system-ui, sans-serif">PHYSICAL MODEL</text>
          <line x1={LEFT_MARGIN + CARD_W / 2 - 70} y1={38} x2={LEFT_MARGIN + CARD_W / 2 + 70} y2={38} stroke="#334155" strokeWidth={0.5} />
          <line x1={LEFT_MARGIN + CARD_W + MAPPING_GAP + CARD_W / 2 - 70} y1={38} x2={LEFT_MARGIN + CARD_W + MAPPING_GAP + CARD_W / 2 + 70} y2={38} stroke="#334155" strokeWidth={0.5} />

          {/* Ontology relationship edges — one arc per entity-type-pair bundle */}
          {visibleBundles.map((b) => {
            const fp = layout.ontoCards.get(b.from);
            const tp = layout.ontoCards.get(b.to);
            if (!fp || !tp) return null;
            const key = keyOf(b);
            return (
              <OntologyRel
                key={`relbundle-${b.from}->${b.to}`}
                type={bundleLabel(b)}
                fromPos={fp}
                toPos={tp}
                isSelf={b.isSelf}
                weight={arcWeight(b.volume)}
                selected={selectedKey === key}
                onSelect={() => setSelectedKey((cur) => (cur === key ? null : key))}
              />
            );
          })}

          {/* Entity ontology cards */}
          {entities.map((e, i) => {
            const pos = layout.ontoCards.get(e.name);
            if (!pos) return null;
            const c = ONTO_COLORS[i % ONTO_COLORS.length];
            return <Card key={`onto-${e.name}`} pos={pos} label={e.name} props={e.properties} fill={c.fill} stroke={c.stroke} textColor={c.text} />;
          })}

          {/* Physical collection cards (deduplicated for shared LPG collections) */}
          {(() => {
            const seen = new Set<string>();
            return entities.map((e) => {
              if (seen.has(e.collection)) return null;
              seen.add(e.collection);
              const pos = layout.physCards.get(e.collection);
              if (!pos) return null;
              const c = PHYS_COLORS[e.style] || PHYS_COLORS.COLLECTION;
              const allPropsForColl = entities.filter((x) => x.collection === e.collection).flatMap((x) => x.properties);
              const uniqueProps = allPropsForColl.filter((p, i, a) => a.findIndex((q) => q.field === p.field) === i);
              return <Card key={`phys-${e.collection}`} pos={pos} label={e.collection} subtitle={e.style.replace(/_/g, " ").toLowerCase()} props={uniqueProps} fill={c.fill} stroke={c.stroke} textColor="#e2e8f0" tag="D" />;
            });
          })()}

          {/* Edge collection cards (deduplicated for shared edge collections) */}
          {(() => {
            const seen = new Set<string>();
            return relationships.map((r) => {
              if (seen.has(r.edgeCollection)) return null;
              seen.add(r.edgeCollection);
              const pos = layout.relEdgeCards.get(r.edgeCollection);
              if (!pos) return null;
              const c = PHYS_COLORS[r.style] || PHYS_COLORS.DEDICATED_COLLECTION;
              const allProps = relationships
                .filter((x) => x.edgeCollection === r.edgeCollection)
                .flatMap((x) => x.properties);
              const uniqueProps = allProps.filter(
                (p, i, a) => a.findIndex((q) => q.field === p.field) === i,
              );
              const subtitle =
                r.style === "DEDICATED_COLLECTION"
                  ? "edge collection"
                  : r.style.replace(/_/g, " ").toLowerCase();
              return <Card key={`phys-edge-${r.edgeCollection}`} pos={pos} label={r.edgeCollection} subtitle={subtitle} props={uniqueProps} fill={c.fill} stroke={c.stroke} textColor="#e2e8f0" tag="E" />;
            });
          })()}

          {/* Type-level mapping edges (dashed header-to-header) */}
          {entities.map((e) => {
            const onto = layout.ontoCards.get(e.name);
            const phys = layout.physCards.get(e.collection);
            if (!onto || !phys) return null;
            const y = onto.y + CARD_HEADER / 2;
            return (
              <g key={`tmap-${e.name}`}>
                <line x1={onto.x + onto.w} y1={y} x2={phys.x} y2={y} stroke="#475569" strokeWidth={1.5} strokeDasharray="6 4" markerEnd="url(#map-arrow)" />
                <rect x={(onto.x + onto.w + phys.x) / 2 - 28} y={y - 9} width={56} height={18} rx={9} fill="#0f172a" stroke="#334155" strokeWidth={1} />
                <text x={(onto.x + onto.w + phys.x) / 2} y={y + 1} fill="#94a3b8" fontSize={8} fontWeight="500" textAnchor="middle" dominantBaseline="middle" fontFamily="monospace">{e.style}</text>
              </g>
            );
          })}

          {/* Bundle → edge collection mapping (one dashed link per bundle, not
              per predicate, so the shared edge collection stays legible) */}
          {visibleBundles.map((b) => {
            const r0 = relationships.find((r) => r.from === b.from && r.to === b.to);
            const edgeColl = r0?.edgeCollection;
            const ePos = edgeColl ? layout.relEdgeCards.get(edgeColl) : undefined;
            if (!ePos) return null;
            const fromOnto = layout.ontoCards.get(b.from);
            const toOnto = layout.ontoCards.get(b.to);
            if (!fromOnto) return null;

            let pillCX: number, pillCY: number;
            if (b.isSelf) {
              pillCX = fromOnto.x - 70;
              pillCY = fromOnto.y + fromOnto.h / 2;
            } else if (toOnto) {
              pillCX = fromOnto.x - 50;
              pillCY = (fromOnto.y + fromOnto.h + toOnto.y) / 2;
            } else {
              pillCX = fromOnto.x;
              pillCY = fromOnto.y + fromOnto.h;
            }

            const dstX = ePos.x;
            const dstY = ePos.y + CARD_HEADER / 2;
            const dropY = Math.max(pillCY + 20, ePos.y - 30);
            const midX = (pillCX + dstX) / 2;

            return (
              <path
                key={`tmap-bundle-${b.from}->${b.to}`}
                d={`M ${pillCX} ${pillCY + 11} L ${pillCX} ${dropY} Q ${pillCX} ${dstY} ${midX} ${dstY} L ${dstX} ${dstY}`}
                fill="none"
                stroke="#f59e0b"
                strokeWidth={1}
                strokeDasharray="6 4"
                markerEnd="url(#map-arrow)"
                opacity={0.35}
              />
            );
          })}

          {/* Property-level mapping edges */}
          {entities.map((e, ei) => {
            const onto = layout.ontoCards.get(e.name);
            const phys = layout.physCards.get(e.collection);
            if (!onto || !phys) return null;
            return e.properties.map((p, j) => {
              const srcY = onto.y + CARD_HEADER + ROW_PAD + j * ROW_H + 10;
              // Find the field row index in the deduplicated physical card
              const allColl = entities.filter((x) => x.collection === e.collection).flatMap((x) => x.properties);
              const uniqColl = allColl.filter((q, i, a) => a.findIndex((r) => r.field === q.field) === i);
              const physIdx = uniqColl.findIndex((q) => q.field === p.field);
              const dstY = phys.y + CARD_HEADER + ROW_PAD + (physIdx >= 0 ? physIdx : j) * ROW_H + 10;
              const color = PROP_EDGE_COLORS[(ei * 3 + j) % PROP_EDGE_COLORS.length];
              return <PropEdge key={`prop-${e.name}-${p.name}`} x1={onto.x + onto.w} y1={srcY} x2={phys.x} y2={dstY} color={color} />;
            });
          })}

          {/* Edge collection property labels are shown inside the edge collection card */}

          {/* _from / _to edges between edge collection and doc collections
              (deduplicated per edge collection — a shared `relations` collection
              connects the same doc collection(s) for all its types). */}
          {(() => {
            const seen = new Set<string>();
            return relationships.map((r) => {
            if (seen.has(r.edgeCollection)) return null;
            seen.add(r.edgeCollection);
            const ePos = layout.relEdgeCards.get(r.edgeCollection);
            const fromPhys = layout.physCards.get(entities.find((e) => e.name === r.from)?.collection || "");
            const toPhys = layout.physCards.get(entities.find((e) => e.name === r.to)?.collection || "");
            if (!ePos) return null;
            const segs: React.ReactElement[] = [];
            if (fromPhys) {
              segs.push(
                <g key={`from-${r.type}`}>
                  <path d={`M ${ePos.x + ePos.w / 4} ${ePos.y} Q ${ePos.x + ePos.w / 4} ${(ePos.y + fromPhys.y + fromPhys.h) / 2} ${fromPhys.x + fromPhys.w / 2} ${fromPhys.y + fromPhys.h}`}
                    fill="none" stroke="#475569" strokeWidth={1} strokeDasharray="3 2" markerEnd="url(#map-arrow)" />
                  <text x={(ePos.x + ePos.w / 4 + fromPhys.x + fromPhys.w / 2) / 2 + 10} y={(ePos.y + fromPhys.y + fromPhys.h) / 2} fill="#475569" fontSize={8} fontFamily="monospace">_from</text>
                </g>,
              );
            }
            if (toPhys) {
              segs.push(
                <g key={`to-${r.type}`}>
                  <path d={`M ${ePos.x + ePos.w * 3 / 4} ${ePos.y} Q ${ePos.x + ePos.w * 3 / 4} ${(ePos.y + toPhys.y + toPhys.h) / 2} ${toPhys.x + toPhys.w / 2} ${toPhys.y + toPhys.h}`}
                    fill="none" stroke="#475569" strokeWidth={1} strokeDasharray="3 2" markerEnd="url(#map-arrow)" />
                  <text x={(ePos.x + ePos.w * 3 / 4 + toPhys.x + toPhys.w / 2) / 2 + 10} y={(ePos.y + toPhys.y + toPhys.h) / 2} fill="#475569" fontSize={8} fontFamily="monospace">_to</text>
                </g>,
              );
            }
            return <g key={`refs-${r.edgeCollection}`}>{segs}</g>;
            });
          })()}
        </g>
      </svg>
    </div>
  );
}
