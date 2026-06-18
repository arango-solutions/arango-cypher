import { useEffect, useMemo, useRef, useState } from "react";
import type { NamedGraph } from "../api/client";

interface Props {
  graphs: NamedGraph[];
  loading: boolean;
  // The currently-bound graph name, or null for "All collections".
  selection: string | null;
  onSelect: (graphName: string | null) => void;
  error?: string | null;
}

// Optional named-graph scope selector (PRD §17). A database may hold
// collections from many unrelated sources (GraphRAG imports, analytics
// result collections, app-control collections, the schema cache); scoping
// the session to a named graph restricts introspection — and therefore
// translation and execution — to that graph's collections. "All collections"
// (selection = null) is the default and is byte-identical to prior behaviour.
export default function GraphSelector({
  graphs,
  loading,
  selection,
  onSelect,
  error,
}: Props) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  useEffect(() => {
    if (open) {
      setFilter("");
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return graphs;
    return graphs.filter((g) => g.name.toLowerCase().includes(q));
  }, [graphs, filter]);

  // Hide the selector entirely when the database has no named graphs —
  // there is nothing to scope to and the default (all collections) applies.
  if (!loading && graphs.length === 0 && !error) {
    return null;
  }

  const active = selection != null;
  const label = active ? selection : "All collections";
  const pillClass = active
    ? "bg-sky-900/30 text-sky-300 border-sky-700 hover:bg-sky-900/40"
    : "bg-gray-800 text-gray-400 border-gray-700 hover:text-gray-200";
  const tooltip = active
    ? `Schema scoped to named graph "${selection}"`
    : "No graph scope — every collection in the database is considered";

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-1.5 px-2 py-0.5 text-xs rounded border transition-colors ${pillClass}`}
        title={tooltip}
      >
        <span className="text-[10px] text-gray-500 uppercase tracking-wide">Graph</span>
        <span className="font-medium max-w-[160px] truncate">{label}</span>
        <span className="text-gray-500 text-[10px]">&#9662;</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 z-50 w-72 bg-gray-900 border border-gray-700 rounded shadow-2xl">
          {graphs.length > 6 && (
            <div className="p-2 border-b border-gray-800">
              <input
                ref={inputRef}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder={loading ? "Loading graphs…" : `Search ${graphs.length} graphs…`}
                className="w-full bg-gray-800 text-gray-200 text-xs rounded px-2 py-1 border border-gray-700 focus:border-indigo-500 focus:outline-none placeholder-gray-600"
              />
            </div>
          )}
          <div className="max-h-64 overflow-y-auto">
            <button
              onClick={() => {
                onSelect(null);
                setOpen(false);
              }}
              className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
                !active ? "bg-indigo-600/20 text-indigo-300" : "text-gray-300 hover:bg-gray-800"
              }`}
            >
              <div className="font-medium">All collections</div>
              <div className="text-[10px] text-gray-500">
                No graph scope — every collection is considered
              </div>
            </button>
            <div className="border-t border-gray-800" />
            {loading && graphs.length === 0 && (
              <div className="px-3 py-2 text-xs text-gray-500">Loading…</div>
            )}
            {error && (
              <div className="px-3 py-2 text-[11px] text-rose-300/90 leading-snug border-l-2 border-rose-700/60 bg-rose-950/20">
                <div className="font-medium text-rose-200">Graph lookup failed</div>
                <div className="text-rose-300/80 break-words mt-0.5">{error}</div>
              </div>
            )}
            {!loading && graphs.length > 0 && filtered.length === 0 && (
              <div className="px-3 py-2 text-xs text-gray-500">No matches</div>
            )}
            {filtered.map((g) => {
              const isSelected = selection === g.name;
              return (
                <button
                  key={g.name}
                  onClick={() => {
                    onSelect(g.name);
                    setOpen(false);
                  }}
                  className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
                    isSelected
                      ? "bg-sky-900/30 text-sky-300"
                      : "text-gray-300 hover:bg-gray-800"
                  }`}
                  title={`${g.vertexCollections.join(", ")}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium truncate">{g.name}</span>
                    <span className="text-[10px] text-gray-500 shrink-0 tabular-nums">
                      {g.collectionCount} {g.collectionCount === 1 ? "collection" : "collections"}
                    </span>
                  </div>
                  {g.edgeDefinitions.length > 0 && (
                    <div className="text-[10px] text-gray-500 truncate">
                      {g.edgeDefinitions.length}{" "}
                      {g.edgeDefinitions.length === 1 ? "edge definition" : "edge definitions"}
                    </div>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
