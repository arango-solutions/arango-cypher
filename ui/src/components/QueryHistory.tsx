import { useMemo, useState } from "react";
import type { HistoryEntry } from "../api/store";

interface Props {
  history: HistoryEntry[];
  // The currently connected (url, database). When supplied, the panel
  // defaults to "current connection only" so the user doesn't have to
  // scroll past queries they ran against other databases. The toggle
  // is reachable in the header; ``null`` (no connection) disables
  // filtering entirely.
  currentConnection: { url: string; database: string } | null;
  onSelect: (entry: HistoryEntry) => void;
  onClear: () => void;
  onClose: () => void;
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();

  if (sameDay) {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function entryMatchesConnection(
  entry: HistoryEntry,
  conn: { url: string; database: string } | null,
): boolean {
  if (!conn) return true;
  // Entries written by older UI bundles lack connection metadata.
  // Treat them as matching everything so the user can still see them
  // — better to show too much than to silently hide history that
  // pre-dates the snapshot feature.
  if (!entry.connectionUrl && !entry.connectionDatabase) return true;
  return (
    entry.connectionUrl === conn.url && entry.connectionDatabase === conn.database
  );
}

// Compact "27 rows" / "27 rows · cached" / "no snapshot" badge text.
function snapshotBadge(entry: HistoryEntry): { label: string; tone: "ok" | "muted" | "warn" } {
  const hasRows = entry.results !== undefined;
  const rowCount = entry.rowCount;
  if (hasRows && rowCount !== undefined) {
    if (entry.truncated) {
      return {
        label: `${rowCount.toLocaleString()} rows (first ${entry.results!.length.toLocaleString()} cached)`,
        tone: "warn",
      };
    }
    return { label: `${rowCount.toLocaleString()} rows cached`, tone: "ok" };
  }
  if (!hasRows && rowCount !== undefined) {
    return {
      label: `${rowCount.toLocaleString()} rows (snapshot dropped to fit storage)`,
      tone: "warn",
    };
  }
  return { label: "no snapshot — click re-runs the query", tone: "muted" };
}

export default function QueryHistory({
  history,
  currentConnection,
  onSelect,
  onClear,
  onClose,
}: Props) {
  const [search, setSearch] = useState("");
  // Default the connection filter on iff we actually have a current
  // connection — that's the most common case and matches what users
  // intuitively want ("show me what I ran against *this* DB").
  const [currentOnly, setCurrentOnly] = useState<boolean>(currentConnection !== null);

  const filtered = useMemo(() => {
    let list = history;
    if (currentOnly) {
      list = list.filter((h) => entryMatchesConnection(h, currentConnection));
    }
    if (!search.trim()) return list;
    const lower = search.toLowerCase();
    return list.filter(
      (h) =>
        h.cypher.toLowerCase().includes(lower) ||
        h.aqlPreview.toLowerCase().includes(lower),
    );
  }, [history, search, currentOnly, currentConnection]);

  // Show the toggle only when there's something to toggle. If every
  // entry is from the current connection (or no connection metadata
  // exists), the toggle is noise.
  const hasOffConnectionEntries = useMemo(() => {
    if (!currentConnection) return false;
    return history.some(
      (h) =>
        (h.connectionUrl || h.connectionDatabase) &&
        !entryMatchesConnection(h, currentConnection),
    );
  }, [history, currentConnection]);

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative w-full max-w-md bg-gray-900 border-l border-gray-800 flex flex-col shadow-2xl">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
          <h2 className="text-sm font-semibold text-white">Query History</h2>
          <div className="flex items-center gap-2">
            {history.length > 0 && (
              <button
                onClick={onClear}
                className="px-2 py-1 text-xs rounded bg-gray-800 hover:bg-red-900/50 text-gray-400 hover:text-red-300 transition-colors"
              >
                Clear All
              </button>
            )}
            <button
              onClick={onClose}
              className="px-2 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 text-gray-400 hover:text-gray-200 transition-colors"
            >
              Close
            </button>
          </div>
        </div>

        <div className="px-4 py-2 border-b border-gray-800 space-y-2">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search queries..."
            className="w-full px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-gray-200 placeholder-gray-500 focus:border-indigo-500 focus:outline-none"
          />
          {hasOffConnectionEntries && currentConnection && (
            <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer">
              <input
                type="checkbox"
                checked={currentOnly}
                onChange={(e) => setCurrentOnly(e.target.checked)}
                className="rounded border-gray-600 bg-gray-800 accent-indigo-500"
              />
              <span>
                Show only{" "}
                <span className="font-mono text-gray-300">
                  {currentConnection.database}
                </span>{" "}
                queries
              </span>
            </label>
          )}
        </div>

        <div className="flex-1 overflow-y-auto">
          {filtered.length === 0 ? (
            <div className="flex items-center justify-center h-32">
              <p className="text-gray-600 text-sm">
                {history.length === 0
                  ? "No queries yet."
                  : currentOnly
                    ? "No queries against this database yet."
                    : "No matches found."}
              </p>
            </div>
          ) : (
            <div className="divide-y divide-gray-800/50">
              {filtered.map((entry, i) => {
                const badge = snapshotBadge(entry);
                const toneClass =
                  badge.tone === "ok"
                    ? "text-emerald-400/80"
                    : badge.tone === "warn"
                      ? "text-amber-400/80"
                      : "text-gray-600";
                return (
                  <button
                    key={`${entry.timestamp}-${i}`}
                    onClick={() => {
                      onSelect(entry);
                      onClose();
                    }}
                    className="w-full text-left px-4 py-3 hover:bg-gray-800/60 transition-colors group"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <pre className="text-xs text-gray-200 font-mono whitespace-pre-wrap break-all line-clamp-3 flex-1">
                        {entry.cypher}
                      </pre>
                      <span className="text-xs text-gray-600 flex-shrink-0 mt-0.5">
                        {formatTime(entry.timestamp)}
                      </span>
                    </div>
                    {entry.aqlPreview && (
                      <p className="text-xs text-gray-500 mt-1 truncate font-mono">
                        → {entry.aqlPreview}
                      </p>
                    )}
                    <div className="mt-1.5 flex items-center gap-2 text-[10px]">
                      <span className={toneClass}>{badge.label}</span>
                      {entry.execMs != null && (
                        <span className="text-sky-400/70 tabular-nums">
                          · {entry.execMs}ms
                        </span>
                      )}
                      {entry.connectionDatabase && (
                        <span
                          className="ml-auto text-gray-500 font-mono"
                          title={entry.connectionUrl || ""}
                        >
                          {entry.connectionDatabase}
                        </span>
                      )}
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="px-4 py-2 border-t border-gray-800 text-xs text-gray-600">
          {filtered.length === history.length
            ? `${history.length} ${history.length === 1 ? "entry" : "entries"}`
            : `${filtered.length} of ${history.length} ${history.length === 1 ? "entry" : "entries"}`}
        </div>
      </div>
    </div>
  );
}
