import { useCallback, useMemo, useState, type ReactNode } from "react";
import type { Action, ResultTab } from "../api/store";
import CytoscapeGraph from "./CytoscapeGraph";
import type { CyNode, CyEdge } from "./CytoscapeGraph";

interface Props {
  results: unknown[] | null;
  explainPlan: unknown | null;
  profileData: { statistics: Record<string, unknown>; profile: unknown } | null;
  activeTab: ResultTab;
  dispatch: (action: Action) => void;
  execMs?: number | null;
}

const ALWAYS_TABS: { id: ResultTab; label: string }[] = [
  { id: "table", label: "Table" },
  { id: "json", label: "JSON" },
  { id: "graph", label: "Graph" },
];

// String tokens commonly used as "null sentinel" placeholders in dirty data.
// When we see the literal text "NULL" (etc.) as a value we render it quoted
// and tinted so it is visually distinct from real `null`.
const SENTINEL_STRINGS = new Set([
  "NULL", "NONE", "NIL", "N/A", "NA", "UNKNOWN",
  "TBD", "TBA", "#N/A", "(NULL)",
]);

function isSentinelString(v: unknown): v is string {
  return typeof v === "string" && SENTINEL_STRINGS.has(v.trim().toUpperCase());
}

function renderCellValue(val: unknown): ReactNode {
  if (val === null || val === undefined) {
    return <span className="text-gray-600 italic">null</span>;
  }
  if (isSentinelString(val)) {
    return (
      <span
        className="text-amber-400/80"
        title="String sentinel value, not a real null. Filter it out in your query to exclude these rows."
      >
        &ldquo;{val}&rdquo;
      </span>
    );
  }
  if (typeof val === "object") {
    return <span className="text-xs">{JSON.stringify(val)}</span>;
  }
  return String(val);
}

function TableView({ data }: { data: unknown[] }) {
  if (data.length === 0) {
    return (
      <div className="p-4 text-gray-500 text-sm">No results returned.</div>
    );
  }

  const allKeys = new Set<string>();
  for (const row of data) {
    if (row && typeof row === "object" && !Array.isArray(row)) {
      Object.keys(row).forEach((k) => allKeys.add(k));
    }
  }
  const columns = Array.from(allKeys);

  if (columns.length === 0) {
    return (
      <div className="p-4 overflow-auto">
        {data.map((item, i) => (
          <div key={i} className="text-sm text-gray-300 mb-1 font-mono">
            {JSON.stringify(item)}
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="overflow-auto h-full">
      <table className="w-full text-sm text-left">
        <thead className="sticky top-0 bg-gray-800 text-gray-400 text-xs uppercase">
          <tr>
            <th className="px-3 py-2 font-medium text-gray-500 w-10">#</th>
            {columns.map((col) => (
              <th key={col} className="px-3 py-2 font-medium">
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row, i) => (
            <tr
              key={i}
              className="border-t border-gray-800 hover:bg-gray-800/50"
            >
              <td className="px-3 py-1.5 text-gray-500 font-mono text-xs">
                {i + 1}
              </td>
              {columns.map((col) => {
                const val =
                  row && typeof row === "object" && !Array.isArray(row)
                    ? (row as Record<string, unknown>)[col]
                    : undefined;
                return (
                  <td key={col} className="px-3 py-1.5 text-gray-300 font-mono">
                    {renderCellValue(val)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JsonView({ data }: { data: unknown }) {
  return (
    <pre className="p-4 text-sm text-gray-300 font-mono overflow-auto h-full whitespace-pre-wrap">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}

function PlanNode({ node, depth = 0 }: { node: Record<string, unknown>; depth?: number }) {
  const type = (node.type as string) || "unknown";
  const cost = node.estimatedCost as number | undefined;
  const nrItems = node.estimatedNrItems as number | undefined;

  const details: string[] = [];
  if (cost !== undefined) details.push(`cost: ${cost.toFixed(2)}`);
  if (nrItems !== undefined) details.push(`rows: ${nrItems}`);

  if (node.indexes && Array.isArray(node.indexes)) {
    for (const idx of node.indexes as Record<string, unknown>[]) {
      details.push(`index: ${idx.type}/${idx.collection}`);
    }
  }
  if (node.filter) {
    details.push("has filter");
  }

  const dependencies = (node.dependencies as Record<string, unknown>[]) || [];

  return (
    <div style={{ marginLeft: depth * 20 }}>
      <div className="flex items-center gap-2 py-1">
        <span className="text-indigo-400 font-medium text-sm">{type}</span>
        {details.length > 0 && (
          <span className="text-gray-500 text-xs">
            ({details.join(", ")})
          </span>
        )}
      </div>
      {dependencies.map((dep, i) => (
        <PlanNode key={i} node={dep} depth={depth + 1} />
      ))}
    </div>
  );
}

function ExplainView({ plan }: { plan: unknown }) {
  if (!plan || typeof plan !== "object") {
    return (
      <div className="p-4 text-gray-500 text-sm">
        No execution plan available. Click "Explain" to generate one.
      </div>
    );
  }

  const planObj = plan as Record<string, unknown>;
  const nodes = (planObj.nodes as Record<string, unknown>[]) || [];
  const rules = (planObj.rules as string[]) || [];
  const collections = (planObj.collections as Record<string, unknown>[]) || [];

  if (nodes.length > 0) {
    return (
      <div className="p-4 overflow-auto h-full">
        <div className="mb-4">
          <h3 className="text-xs font-semibold text-gray-400 uppercase mb-2">
            Execution Plan
          </h3>
          {nodes.map((node, i) => (
            <PlanNode key={i} node={node} />
          ))}
        </div>

        {rules.length > 0 && (
          <div className="mb-4">
            <h3 className="text-xs font-semibold text-gray-400 uppercase mb-2">
              Optimizer Rules Applied
            </h3>
            <div className="flex flex-wrap gap-1.5">
              {rules.map((rule) => (
                <span
                  key={rule}
                  className="px-2 py-0.5 rounded bg-gray-700 text-xs text-gray-300"
                >
                  {rule}
                </span>
              ))}
            </div>
          </div>
        )}

        {collections.length > 0 && (
          <div>
            <h3 className="text-xs font-semibold text-gray-400 uppercase mb-2">
              Collections
            </h3>
            <div className="flex flex-wrap gap-1.5">
              {collections.map((c, i) => (
                <span
                  key={i}
                  className="px-2 py-0.5 rounded bg-gray-700 text-xs text-gray-300"
                >
                  {(c as Record<string, string>).name}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    );
  }

  return <JsonView data={plan} />;
}

interface ProfileWarning {
  severity: "warning" | "info";
  message: string;
}

function analyzeProfile(profile: unknown): ProfileWarning[] {
  const warnings: ProfileWarning[] = [];
  if (!profile || typeof profile !== "object") return warnings;

  const nodes: Record<string, unknown>[] = [];

  function collectNodes(obj: unknown) {
    if (!obj || typeof obj !== "object") return;
    if (Array.isArray(obj)) {
      for (const item of obj) collectNodes(item);
      return;
    }
    const o = obj as Record<string, unknown>;
    if (o.type) nodes.push(o);
    if (o.nodes && Array.isArray(o.nodes)) collectNodes(o.nodes);
    if (o.dependencies && Array.isArray(o.dependencies)) collectNodes(o.dependencies);
  }
  collectNodes(profile);

  for (const node of nodes) {
    const type = String(node.type || "");

    if (type === "EnumerateCollectionNode") {
      const coll = (node.collection as string) || "unknown";
      const items = node.items as number | undefined;
      const itemsStr = items != null ? ` (${items.toLocaleString()} docs)` : "";
      warnings.push({
        severity: "warning",
        message: `Full collection scan on "${coll}"${itemsStr} — consider adding an index`,
      });
    }

    if (type === "IndexNode") {
      const indexes = node.indexes as Record<string, unknown>[] | undefined;
      if (Array.isArray(indexes)) {
        for (const idx of indexes) {
          const selectivity = idx.selectivityEstimate as number | undefined;
          if (selectivity != null && selectivity < 0.01) {
            warnings.push({
              severity: "info",
              message: `Low selectivity index on "${idx.collection}" (${(selectivity * 100).toFixed(2)}%)`,
            });
          }
        }
      }
    }

    const cost = node.estimatedCost as number | undefined;
    if (cost != null && cost > 100000) {
      warnings.push({
        severity: "warning",
        message: `High estimated cost (${cost.toLocaleString()}) for ${type}`,
      });
    }
  }

  return warnings;
}

function ProfileWarningsBanner({ warnings }: { warnings: ProfileWarning[] }) {
  if (warnings.length === 0) return null;
  return (
    <div className="px-3 py-2 bg-amber-900/20 border-b border-amber-800/30 space-y-1">
      {warnings.map((w, i) => (
        <div key={i} className="flex items-start gap-2">
          <span className={`text-xs mt-0.5 shrink-0 ${w.severity === "warning" ? "text-amber-500" : "text-blue-400"}`}>
            {w.severity === "warning" ? "\u26A0" : "\u2139"}
          </span>
          <span className={`text-xs ${w.severity === "warning" ? "text-amber-400" : "text-blue-300"}`}>
            {w.message}
          </span>
        </div>
      ))}
    </div>
  );
}

function ProfileView({
  data,
}: {
  data: { statistics: Record<string, unknown>; profile: unknown };
}) {
  const { statistics, profile } = data;
  const profileWarnings = useMemo(() => analyzeProfile(profile), [profile]);

  return (
    <div className="overflow-auto h-full">
      <ProfileWarningsBanner warnings={profileWarnings} />
      <div className="p-4 space-y-4">
        <div>
          <h3 className="text-xs font-semibold text-gray-400 uppercase mb-2">
            Execution Statistics
          </h3>
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
            {Object.entries(statistics).map(([key, val]) => (
              <div
                key={key}
                className="p-2 rounded bg-gray-800 border border-gray-700"
              >
                <div className="text-xs text-gray-400">{key}</div>
                <div className="text-sm text-white font-mono">
                  {typeof val === "number" ? val.toLocaleString() : String(val)}
                </div>
              </div>
            ))}
          </div>
        </div>

        {profile != null && (
          <div>
            <h3 className="text-xs font-semibold text-gray-400 uppercase mb-2">
              Profile Details
            </h3>
            <pre className="text-xs text-gray-300 font-mono whitespace-pre-wrap">
              {JSON.stringify(profile, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}

const NODE_COLORS = [
  "#6366f1", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
  "#06b6d4", "#ec4899", "#84cc16",
];

function extractGraphData(data: unknown[]): {
  nodes: CyNode[];
  edges: CyEdge[];
  collColors: Map<string, string>;
} {
  const nodeMap = new Map<string, CyNode>();
  const edges: CyEdge[] = [];
  const collColors = new Map<string, string>();
  let colorIdx = 0;

  const processDoc = (doc: Record<string, unknown>) => {
    const id = doc._id as string | undefined;
    if (!id || nodeMap.has(id)) return;
    const coll = id.split("/")[0] || "unknown";
    if (!collColors.has(coll)) {
      collColors.set(coll, NODE_COLORS[colorIdx++ % NODE_COLORS.length]);
    }
    const label =
      (doc.name as string) ||
      (doc.title as string) ||
      (doc.productName as string) ||
      (doc.companyName as string) ||
      (doc.firstName as string) ||
      (doc._key as string) ||
      id.split("/")[1] ||
      id;
    nodeMap.set(id, { id, label, color: collColors.get(coll)!, data: { ...doc } });
  };

  const processEdge = (doc: Record<string, unknown>) => {
    const from = doc._from as string | undefined;
    const to = doc._to as string | undefined;
    if (!from || !to) return;
    const eColl = doc._id ? (doc._id as string).split("/")[0] : "";
    const relType = (doc.relation as string) || (doc.type as string) || eColl || "";
    edges.push({ source: from, target: to, label: relType, data: { ...doc } });
  };

  for (const row of data) {
    if (!row || typeof row !== "object") continue;
    const obj = row as Record<string, unknown>;

    for (const val of Object.values(obj)) {
      if (val && typeof val === "object" && !Array.isArray(val)) {
        const d = val as Record<string, unknown>;
        if (d._from && d._to) processEdge(d);
        else if (d._id) processDoc(d);
      }
    }
    if (obj._from && obj._to) processEdge(obj);
    else if (obj._id) processDoc(obj);
  }

  return { nodes: Array.from(nodeMap.values()), edges, collColors };
}

function NodeInspector({
  node,
  onClose,
}: {
  node: CyNode;
  onClose: () => void;
}) {
  return (
    <div className="w-60 border-l border-gray-800 overflow-auto p-3 bg-gray-900/50 shrink-0">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="w-3 h-3 rounded-full shrink-0"
            style={{ backgroundColor: node.color || "#6366f1" }}
          />
          <span className="text-sm font-semibold text-white truncate">
            {node.label}
          </span>
        </div>
        <button
          onClick={onClose}
          className="text-[10px] text-gray-500 hover:text-gray-300 transition-colors shrink-0 ml-2"
        >
          Close
        </button>
      </div>
      <div className="text-[10px] text-gray-500 mb-2 font-mono">{node.id}</div>
      <div className="space-y-1">
        {Object.entries(node.data)
          .filter(([k]) => !k.startsWith("_") && k !== "id" && k !== "label" && k !== "color")
          .map(([k, v]) => (
            <div key={k} className="flex items-start gap-2">
              <span className="text-xs text-gray-400 shrink-0 font-mono">
                {k}:
              </span>
              <span className="text-xs text-gray-300 font-mono break-all">
                {renderCellValue(v)}
              </span>
            </div>
          ))}
      </div>
    </div>
  );
}

function GraphView({ data }: { data: unknown[] }) {
  const [selected, setSelected] = useState<CyNode | null>(null);

  const { nodes, edges, collColors } = useMemo(
    () => extractGraphData(data),
    [data],
  );
  const collections = useMemo(
    () => Array.from(collColors.entries()),
    [collColors],
  );

  if (nodes.length === 0 && edges.length === 0) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-gray-500 text-sm">
          No graph data found. Results need{" "}
          <code className="text-gray-400">_id</code> /{" "}
          <code className="text-gray-400">_from</code> /{" "}
          <code className="text-gray-400">_to</code> fields to visualize.
        </p>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-4 px-3 py-1 shrink-0">
        <span className="text-xs text-gray-400">
          {nodes.length} nodes, {edges.length} edges
        </span>
        <div className="flex gap-3">
          {collections.map(([name, color]) => (
            <span
              key={name}
              className="flex items-center gap-1 text-xs text-gray-400"
            >
              <span
                className="w-2.5 h-2.5 rounded-full inline-block"
                style={{ backgroundColor: color }}
              />
              {name}
            </span>
          ))}
        </div>
        {selected && (
          <button
            onClick={() => setSelected(null)}
            className="ml-auto text-[10px] text-gray-500 hover:text-gray-300 transition-colors"
          >
            Clear selection
          </button>
        )}
      </div>
      <div className="flex-1 min-h-0 flex">
        <div className={selected ? "flex-1 min-w-0" : "w-full"}>
          <CytoscapeGraph
            nodes={nodes}
            edges={edges}
            onNodeClick={setSelected}
            onBackgroundClick={() => setSelected(null)}
          />
        </div>
        {selected && (
          <NodeInspector node={selected} onClose={() => setSelected(null)} />
        )}
      </div>
    </div>
  );
}

function downloadBlob(content: string, filename: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function toCsv(data: unknown[]): string {
  if (data.length === 0) return "";
  const allKeys = new Set<string>();
  for (const row of data) {
    if (row && typeof row === "object" && !Array.isArray(row)) {
      Object.keys(row).forEach((k) => allKeys.add(k));
    }
  }
  const columns = Array.from(allKeys);
  if (columns.length === 0) {
    return data.map((item) => JSON.stringify(item)).join("\n");
  }
  const escape = (v: unknown) => {
    const s = v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
    return s.includes(",") || s.includes('"') || s.includes("\n") ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const header = columns.map(escape).join(",");
  const rows = data.map((row) => {
    const obj = (row && typeof row === "object" && !Array.isArray(row)) ? row as Record<string, unknown> : {};
    return columns.map((col) => escape(obj[col])).join(",");
  });
  return [header, ...rows].join("\n");
}

export default function ResultsPanel({
  results,
  explainPlan,
  profileData,
  activeTab,
  dispatch,
  execMs,
}: Props) {
  const hasData =
    results !== null || explainPlan !== null || profileData !== null;

  const handleExportJson = useCallback(() => {
    if (!results) return;
    downloadBlob(JSON.stringify(results, null, 2), "results.json", "application/json");
  }, [results]);

  const handleExportCsv = useCallback(() => {
    if (!results) return;
    downloadBlob(toCsv(results), "results.csv", "text/csv");
  }, [results]);

  const tabs = [
    ...ALWAYS_TABS,
    ...(explainPlan ? [{ id: "explain" as ResultTab, label: "Explain" }] : []),
    ...(profileData ? [{ id: "profile" as ResultTab, label: "Profile" }] : []),
  ];

  return (
    <div className="h-full flex flex-col">
      <div className="flex border-b border-gray-700 bg-gray-900/50">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => dispatch({ type: "SET_RESULT_TAB", tab: tab.id })}
            className={`px-4 py-2 text-xs font-medium transition-colors ${
              activeTab === tab.id
                ? "text-indigo-400 border-b-2 border-indigo-400"
                : "text-gray-500 hover:text-gray-300"
            }`}
          >
            {tab.label}
          </button>
        ))}
        {results && results.length > 0 && (
          <div className="ml-auto flex items-center gap-1.5 px-2">
            <button
              onClick={handleExportCsv}
              className="px-2 py-1 text-[10px] rounded bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors"
              title="Download as CSV"
            >
              CSV
            </button>
            <button
              onClick={handleExportJson}
              className="px-2 py-1 text-[10px] rounded bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors"
              title="Download as JSON"
            >
              JSON
            </button>
            <span className="text-xs text-gray-500 ml-1">
              {results.length} row{results.length !== 1 ? "s" : ""}
              {execMs != null && (
                <span className="text-sky-400/70 ml-1.5 tabular-nums">
                  {execMs}ms
                </span>
              )}
            </span>
          </div>
        )}
      </div>

      <div className="flex-1 min-h-0 overflow-auto">
        {!hasData ? (
          <div className="flex items-center justify-center h-full">
            <p className="text-gray-600 text-sm">
              Run a query to see results here.
            </p>
          </div>
        ) : activeTab === "table" && results ? (
          <TableView data={results} />
        ) : activeTab === "json" && results ? (
          <JsonView data={results} />
        ) : activeTab === "graph" && results ? (
          <GraphView data={results} />
        ) : activeTab === "explain" ? (
          <ExplainView plan={explainPlan} />
        ) : activeTab === "profile" && profileData ? (
          <ProfileView data={profileData} />
        ) : (
          <div className="flex items-center justify-center h-full">
            <p className="text-gray-600 text-sm">
              No data for this view yet.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
