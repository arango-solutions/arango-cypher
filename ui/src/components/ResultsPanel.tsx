import type { Action, ResultTab } from "../api/store";

interface Props {
  results: unknown[] | null;
  explainPlan: unknown | null;
  profileData: { statistics: Record<string, unknown>; profile: unknown } | null;
  activeTab: ResultTab;
  dispatch: (action: Action) => void;
}

const TABS: { id: ResultTab; label: string }[] = [
  { id: "table", label: "Table" },
  { id: "json", label: "JSON" },
  { id: "explain", label: "Explain" },
  { id: "profile", label: "Profile" },
];

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
                    {val === null || val === undefined ? (
                      <span className="text-gray-600 italic">null</span>
                    ) : typeof val === "object" ? (
                      <span className="text-xs">
                        {JSON.stringify(val)}
                      </span>
                    ) : (
                      String(val)
                    )}
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

function ProfileView({
  data,
}: {
  data: { statistics: Record<string, unknown>; profile: unknown };
}) {
  const { statistics, profile } = data;

  return (
    <div className="p-4 overflow-auto h-full space-y-4">
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
  );
}

export default function ResultsPanel({
  results,
  explainPlan,
  profileData,
  activeTab,
  dispatch,
}: Props) {
  const hasData =
    results !== null || explainPlan !== null || profileData !== null;

  return (
    <div className="h-full flex flex-col">
      <div className="flex border-b border-gray-700 bg-gray-900/50">
        {TABS.map((tab) => (
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
