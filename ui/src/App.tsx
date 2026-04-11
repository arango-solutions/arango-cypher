import { useCallback, useRef, useState } from "react";
import ConnectionDialog from "./components/ConnectionDialog";
import CypherEditor from "./components/CypherEditor";
import AqlEditor from "./components/AqlEditor";
import ResultsPanel from "./components/ResultsPanel";
import MappingPanel from "./components/MappingPanel";
import { useAppState } from "./api/store";
import {
  translateCypher,
  executeCypher,
  explainCypher,
  profileCypher,
} from "./api/client";

export default function App() {
  const [state, dispatch] = useAppState();
  const [showMapping, setShowMapping] = useState(true);
  const mappingRef = useRef(state.mapping);
  mappingRef.current = state.mapping;
  const cypherRef = useRef(state.cypher);
  cypherRef.current = state.cypher;

  function makeRequest() {
    return {
      cypher: cypherRef.current,
      mapping: mappingRef.current,
      extensions_enabled: true,
    };
  }

  const handleTranslate = useCallback(async () => {
    if (!cypherRef.current.trim()) return;
    dispatch({ type: "TRANSLATE_START" });
    try {
      const resp = await translateCypher(makeRequest());
      dispatch({
        type: "TRANSLATE_SUCCESS",
        aql: resp.aql,
        bindVars: resp.bind_vars,
      });
    } catch (err) {
      dispatch({
        type: "TRANSLATE_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch]);

  const handleExecute = useCallback(async () => {
    if (!cypherRef.current.trim() || !state.connection.token) return;
    dispatch({ type: "EXECUTE_START" });
    try {
      const resp = await executeCypher(makeRequest(), state.connection.token);
      dispatch({ type: "TRANSLATE_SUCCESS", aql: resp.aql, bindVars: resp.bind_vars });
      dispatch({ type: "EXECUTE_SUCCESS", results: resp.results });
    } catch (err) {
      dispatch({
        type: "EXECUTE_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch, state.connection.token]);

  const handleExplain = useCallback(async () => {
    if (!cypherRef.current.trim() || !state.connection.token) return;
    dispatch({ type: "EXPLAIN_START" });
    try {
      const resp = await explainCypher(makeRequest(), state.connection.token);
      dispatch({ type: "TRANSLATE_SUCCESS", aql: resp.aql, bindVars: resp.bind_vars });
      dispatch({ type: "EXPLAIN_SUCCESS", plan: resp.plan });
    } catch (err) {
      dispatch({
        type: "EXPLAIN_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch, state.connection.token]);

  const handleProfile = useCallback(async () => {
    if (!cypherRef.current.trim() || !state.connection.token) return;
    dispatch({ type: "PROFILE_START" });
    try {
      const resp = await profileCypher(makeRequest(), state.connection.token);
      dispatch({ type: "TRANSLATE_SUCCESS", aql: resp.aql, bindVars: resp.bind_vars });
      dispatch({
        type: "PROFILE_SUCCESS",
        results: resp.results,
        statistics: resp.statistics,
        profile: resp.profile,
      });
    } catch (err) {
      dispatch({
        type: "PROFILE_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch, state.connection.token]);

  const isConnected = state.connection.status === "connected";
  const isLoading =
    state.translating || state.executing || state.explaining || state.profiling;

  return (
    <div className="h-screen flex flex-col bg-gray-950 text-gray-100">
      {/* Connection bar */}
      <header className="flex items-center justify-between px-4 py-2 bg-gray-900 border-b border-gray-800">
        <div className="flex items-center gap-3">
          <h1 className="text-sm font-semibold text-white tracking-tight">
            Cypher Workbench
          </h1>
          <span className="text-gray-600 text-xs">|</span>
          <ConnectionDialog
            connection={state.connection}
            dispatch={dispatch}
          />
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowMapping(!showMapping)}
            className={`px-2.5 py-1 text-xs rounded transition-colors ${
              showMapping
                ? "bg-indigo-600/20 text-indigo-400 border border-indigo-600/30"
                : "bg-gray-800 text-gray-400 hover:text-gray-200"
            }`}
          >
            Mapping
          </button>
        </div>
      </header>

      {/* Error banner */}
      {state.error && (
        <div className="px-4 py-2 bg-red-900/30 border-b border-red-800 flex items-center justify-between">
          <span className="text-sm text-red-300">{state.error}</span>
          <button
            onClick={() => dispatch({ type: "CLEAR_ERROR" })}
            className="text-red-400 hover:text-red-200 text-xs ml-4"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 min-h-0 flex">
        {/* Mapping sidebar */}
        {showMapping && (
          <div className="w-80 border-r border-gray-800 flex-shrink-0">
            <MappingPanel
              mapping={state.mapping}
              onChange={(m) => dispatch({ type: "SET_MAPPING", mapping: m })}
            />
          </div>
        )}

        {/* Editors and results */}
        <div className="flex-1 min-w-0 flex flex-col">
          {/* Editor toolbar */}
          <div className="flex items-center gap-2 px-3 py-2 bg-gray-900/50 border-b border-gray-800">
            <button
              onClick={handleTranslate}
              disabled={isLoading || !state.cypher.trim()}
              className="px-3 py-1.5 text-xs font-medium rounded bg-indigo-600 hover:bg-indigo-500 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              title="Ctrl/Cmd+Enter"
            >
              {state.translating ? "Translating..." : "Translate"}
            </button>
            <button
              onClick={handleExecute}
              disabled={isLoading || !isConnected || !state.cypher.trim()}
              className="px-3 py-1.5 text-xs font-medium rounded bg-emerald-600 hover:bg-emerald-500 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              title="Shift+Enter"
            >
              {state.executing ? "Running..." : "Run"}
            </button>
            <div className="w-px h-5 bg-gray-700" />
            <button
              onClick={handleExplain}
              disabled={isLoading || !isConnected || !state.cypher.trim()}
              className="px-3 py-1.5 text-xs font-medium rounded bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              title="Ctrl/Cmd+Shift+E"
            >
              {state.explaining ? "Explaining..." : "Explain"}
            </button>
            <button
              onClick={handleProfile}
              disabled={isLoading || !isConnected || !state.cypher.trim()}
              className="px-3 py-1.5 text-xs font-medium rounded bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              title="Ctrl/Cmd+Shift+P"
            >
              {state.profiling ? "Profiling..." : "Profile"}
            </button>

            {isLoading && (
              <div className="ml-2 w-4 h-4 border-2 border-indigo-400 border-t-transparent rounded-full animate-spin" />
            )}

            <div className="flex-1" />

            <span className="text-xs text-gray-600">
              {isConnected ? (
                <span className="text-gray-500">
                  Shift+Enter to run
                </span>
              ) : (
                <span className="text-amber-600">
                  Connect to run / explain / profile
                </span>
              )}
            </span>
          </div>

          {/* Side-by-side editors */}
          <div className="flex-1 min-h-0 flex">
            {/* Cypher editor */}
            <div className="flex-1 min-w-0 flex flex-col border-r border-gray-800">
              <div className="px-3 py-1.5 bg-gray-900/30 border-b border-gray-800">
                <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
                  Cypher
                </span>
              </div>
              <div className="flex-1 min-h-0">
                <CypherEditor
                  value={state.cypher}
                  onChange={(v) =>
                    dispatch({ type: "SET_CYPHER", cypher: v })
                  }
                  onTranslate={handleTranslate}
                  onExecute={handleExecute}
                  onExplain={handleExplain}
                  onProfile={handleProfile}
                />
              </div>
            </div>

            {/* AQL editor */}
            <div className="flex-1 min-w-0 flex flex-col">
              <div className="px-3 py-1.5 bg-gray-900/30 border-b border-gray-800">
                <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
                  AQL
                </span>
                {state.aql && (
                  <span className="text-xs text-gray-600 ml-2">read-only</span>
                )}
              </div>
              <div className="flex-1 min-h-0">
                <AqlEditor
                  value={state.aql}
                  bindVars={state.bindVars}
                  error={null}
                />
              </div>
            </div>
          </div>

          {/* Results panel */}
          <div className="h-64 border-t border-gray-800 flex-shrink-0">
            <ResultsPanel
              results={state.results}
              explainPlan={state.explainPlan}
              profileData={state.profileData}
              activeTab={state.activeResultTab}
              dispatch={dispatch}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
