import { useCallback, useEffect, useState } from "react";
import { forceReacquireSchema, type SchemaWarning } from "../api/client";
import type { Action } from "../api/store";

interface Props {
  warnings: SchemaWarning[];
  url: string;
  database: string;
  token: string | null;
  dispatch: (action: Action) => void;
}

const DISMISSED_KEY = "schema_warning_dismissed";

// Persist per-(connection, warning.code) dismissals so the same warning
// stays hidden on the same database but reappears on a different one.
// localStorage layout: { "<url>::<db>::<code>": <epoch_ms_dismissed> }.
function loadDismissed(): Record<string, number> {
  try {
    const raw = localStorage.getItem(DISMISSED_KEY);
    return raw ? (JSON.parse(raw) as Record<string, number>) : {};
  } catch {
    return {};
  }
}

function saveDismissed(map: Record<string, number>) {
  try {
    localStorage.setItem(DISMISSED_KEY, JSON.stringify(map));
  } catch {
    // localStorage may be unavailable
  }
}

function dismissalKey(url: string, database: string, code: string): string {
  return `${url}::${database}::${code}`;
}

export default function SchemaWarningBanner({
  warnings,
  url,
  database,
  token,
  dispatch,
}: Props) {
  const [dismissed, setDismissed] = useState<Record<string, number>>(() =>
    loadDismissed(),
  );
  const [reacquiring, setReacquiring] = useState(false);
  const [reacquireError, setReacquireError] = useState<string | null>(null);

  // Re-load dismissals when the connection changes — different database
  // means a different set of dismissal keys is now relevant.
  useEffect(() => {
    setDismissed(loadDismissed());
    setReacquireError(null);
  }, [url, database]);

  const visible = warnings.filter(
    (w) => !dismissed[dismissalKey(url, database, w.code)],
  );

  const handleDismiss = useCallback(
    (code: string) => {
      const next = {
        ...dismissed,
        [dismissalKey(url, database, code)]: Date.now(),
      };
      setDismissed(next);
      saveDismissed(next);
    },
    [dismissed, url, database],
  );

  const handleForceReacquire = useCallback(async () => {
    if (!token || reacquiring) return;
    setReacquiring(true);
    setReacquireError(null);
    try {
      const result = await forceReacquireSchema(token);
      dispatch({
        type: "SCHEMA_WARNINGS_REPLACE",
        warnings: result.warnings ?? [],
      });
    } catch (err) {
      setReacquireError(err instanceof Error ? err.message : String(err));
    } finally {
      setReacquiring(false);
    }
  }, [token, reacquiring, dispatch]);

  if (visible.length === 0) return null;

  // Surface the analyzer-missing warning first; it's the most actionable
  // and the only one that has a built-in remediation button.
  const analyzerMissing = visible.find(
    (w) => w.code === "ANALYZER_NOT_INSTALLED",
  );
  const others = visible.filter((w) => w !== analyzerMissing);

  return (
    <div className="bg-amber-900/20 border-b border-amber-800/30">
      {analyzerMissing && (
        <div className="px-4 py-2 flex items-start gap-2.5">
          <span className="text-amber-500 text-sm mt-0.5 shrink-0">
            &#9888;
          </span>
          <div className="flex-1 min-w-0">
            <div className="text-xs text-amber-300 font-medium">
              Schema mapping is degraded — analyzer is not installed
            </div>
            <div className="text-xs text-amber-400/90 mt-0.5">
              {analyzerMissing.message}
              {analyzerMissing.install_hint && (
                <>
                  {" "}
                  Install hint:{" "}
                  <code className="bg-amber-950/40 px-1 py-0.5 rounded text-amber-200">
                    {analyzerMissing.install_hint}
                  </code>
                </>
              )}
            </div>
            {reacquireError && (
              <div className="text-xs text-rose-400 mt-1">
                Reacquire failed: {reacquireError}
              </div>
            )}
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <button
              type="button"
              onClick={handleForceReacquire}
              disabled={!token || reacquiring}
              title="Drop the cached mapping and rebuild from scratch via the analyzer (HTTP 503 if still missing)."
              className="px-2 py-0.5 text-[11px] rounded bg-amber-700/30 hover:bg-amber-700/50 border border-amber-600/40 text-amber-200 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {reacquiring ? "Reacquiring…" : "Force reacquire"}
            </button>
            <button
              type="button"
              onClick={() => handleDismiss(analyzerMissing.code)}
              title="Hide this warning for the current database."
              className="px-1.5 py-0.5 text-[11px] rounded text-amber-400/70 hover:text-amber-200 hover:bg-amber-800/30 transition-colors"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}
      {others.map((w) => (
        <div
          key={w.code}
          className="px-4 py-1.5 flex items-start gap-2.5 border-t border-amber-800/20 first:border-t-0"
        >
          <span className="text-amber-500 text-xs mt-0.5 shrink-0">
            &#9888;
          </span>
          <div className="flex-1 min-w-0">
            <span className="text-xs text-amber-300 font-medium">{w.code}</span>
            <span className="text-xs text-amber-400/90 ml-2">{w.message}</span>
          </div>
          <button
            type="button"
            onClick={() => handleDismiss(w.code)}
            className="px-1.5 py-0.5 text-[11px] rounded text-amber-400/70 hover:text-amber-200 hover:bg-amber-800/30 transition-colors shrink-0"
          >
            Dismiss
          </button>
        </div>
      ))}
    </div>
  );
}
