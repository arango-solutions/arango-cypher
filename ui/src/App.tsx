import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EditorView } from "@codemirror/view";
import ConnectionDialog from "./components/ConnectionDialog";
import CypherEditor from "./components/CypherEditor";
import AqlEditor from "./components/AqlEditor";
import ResultsPanel from "./components/ResultsPanel";
import MappingPanel from "./components/MappingPanel";
import ParameterPanel from "./components/ParameterPanel";
import QueryHistory from "./components/QueryHistory";
import SampleQueries from "./components/SampleQueries";
import ClauseOutline from "./components/ClauseOutline";
import TenantSelector from "./components/TenantSelector";
import GraphSelector from "./components/GraphSelector";
import SchemaWarningBanner from "./components/SchemaWarningBanner";
import { useAppState } from "./api/store";
import { buildCorrespondenceMap, buildReverseMap } from "./utils/correspondenceMap";
import {
  translateCypher,
  executeCypher,
  executeAql,
  explainCypher,
  profileCypher,
  nl2Cypher,
  nl2Aql,
  createIndex,
  saveCorrection,
  listCorrections,
  deleteCorrection,
  suggestNlQueries,
  discoverTenants,
  bindTenant,
  listGraphs,
  bindGraph,
  introspectSchema,
  introspectSchemaUntilReady,
  introspectToMapping,
  isAuthError,
  isTranspileFallbackError,
  type CorrectionRecord,
  type TenantContext,
  type DiscoveredTenant,
  type NamedGraph,
  type IndexAdvisory,
} from "./api/client";

const TENANT_CTX_KEY = "tenant_context";
const GRAPH_SCOPE_KEY = "graph_scope";

function tenantCtxStoreKey(url: string, database: string): string {
  return `${TENANT_CTX_KEY}::${url}::${database}`;
}

function loadTenantContext(url: string, database: string): TenantContext | null {
  try {
    const raw = localStorage.getItem(tenantCtxStoreKey(url, database));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.property === "string" && typeof parsed.value === "string") {
      return parsed as TenantContext;
    }
    return null;
  } catch {
    return null;
  }
}

function saveTenantContext(url: string, database: string, ctx: TenantContext | null) {
  try {
    const key = tenantCtxStoreKey(url, database);
    if (ctx == null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(ctx));
  } catch {
    // ignore
  }
}

function graphScopeStoreKey(url: string, database: string): string {
  return `${GRAPH_SCOPE_KEY}::${url}::${database}`;
}

function loadGraphScope(url: string, database: string): string | null {
  try {
    return localStorage.getItem(graphScopeStoreKey(url, database));
  } catch {
    return null;
  }
}

function saveGraphScope(url: string, database: string, graphName: string | null) {
  try {
    const key = graphScopeStoreKey(url, database);
    if (graphName == null) localStorage.removeItem(key);
    else localStorage.setItem(key, graphName);
  } catch {
    // ignore
  }
}

function splitCypherStatements(input: string): string[] {
  const stmts: string[] = [];
  let buf = "";
  let inStr: string | null = null;
  let inBlock = false;

  for (let i = 0; i < input.length; i++) {
    const ch = input[i];

    if (inStr) {
      buf += ch;
      if (ch === "\\" && i + 1 < input.length) { buf += input[++i]; continue; }
      if (ch === inStr) inStr = null;
      continue;
    }
    if (inBlock) {
      buf += ch;
      if (ch === "*" && input[i + 1] === "/") { buf += input[++i]; inBlock = false; }
      continue;
    }
    if (ch === "'" || ch === '"') { inStr = ch; buf += ch; continue; }
    if (ch === "/" && input[i + 1] === "*") { buf += ch + input[++i]; inBlock = true; continue; }
    if (ch === "/" && input[i + 1] === "/") {
      while (i < input.length && input[i] !== "\n") buf += input[i++];
      continue;
    }
    if (ch === ";") {
      const trimmed = buf.trim();
      if (trimmed) stmts.push(trimmed);
      buf = "";
      continue;
    }
    buf += ch;
  }
  const trimmed = buf.trim();
  if (trimmed) stmts.push(trimmed);
  return stmts.length > 0 ? stmts : [""];
}

export default function App() {
  const [state, dispatch] = useAppState();
  const [showMapping, setShowMapping] = useState(true);
  const [mappingWidth, setMappingWidth] = useState(320);
  const [showHistory, setShowHistory] = useState(false);
  const [showSamples, setShowSamples] = useState(false);
  const [showOutline, setShowOutline] = useState(false);
  const cypherViewRef = useRef<EditorView | null>(null);
  const [cypherHighlightLines, setCypherHighlightLines] = useState<number[]>([]);
  const [aqlHighlightLines, setAqlHighlightLines] = useState<number[]>([]);

  const correspondenceMap = useMemo(
    () => buildCorrespondenceMap(state.cypher, state.aql),
    [state.cypher, state.aql],
  );
  const reverseCorrespondenceMap = useMemo(
    () => buildReverseMap(correspondenceMap),
    [correspondenceMap],
  );

  const handleCypherHoverLine = useCallback(
    (line: number | null) => {
      if (line == null) {
        setAqlHighlightLines([]);
        return;
      }
      const aqlLines = correspondenceMap.get(line - 1);
      setAqlHighlightLines(aqlLines ? aqlLines.map((l) => l + 1) : []);
    },
    [correspondenceMap],
  );

  const handleAqlHoverLine = useCallback(
    (line: number | null) => {
      if (line == null) {
        setCypherHighlightLines([]);
        return;
      }
      const cypherLines = reverseCorrespondenceMap.get(line - 1);
      setCypherHighlightLines(cypherLines ? cypherLines.map((l) => l + 1) : []);
    },
    [reverseCorrespondenceMap],
  );

  const dragRef = useRef<{ startX: number; startW: number } | null>(null);

  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!dragRef.current) return;
      const delta = e.clientX - dragRef.current.startX;
      setMappingWidth(Math.max(240, Math.min(800, dragRef.current.startW + delta)));
    };
    const onMouseUp = () => { dragRef.current = null; document.body.style.cursor = ""; document.body.style.userSelect = ""; };
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => { window.removeEventListener("mousemove", onMouseMove); window.removeEventListener("mouseup", onMouseUp); };
  }, []);
  const [nlInput, setNlInput] = useState("");
  const [nlLoading, setNlLoading] = useState(false);
  const [nlInfo, setNlInfo] = useState("");
  // WP-29: structured NL failure banner. Populated when the backend
  // returns ``method === "validation_failed"`` (retry budget
  // exhausted) or ``"tenant_guardrail_blocked"``. We render a red
  // banner with the full ``explanation`` instead of writing the
  // server's (empty) ``cypher`` into the editor — the pre-WP-29
  // behaviour silently dropped an invalid query into the editor.
  const [nlError, setNlError] = useState("");
  // WP-S3c: inverted/ArangoSearch index advisories from the NL entity resolver
  // (fuzzy probes that fell back to a full scan). Rendered as a one-click
  // "Create index" affordance below the NL input. Keyed by `collection.field`
  // to track per-advisory create status (idle/creating/created/error).
  const [nlAdvisories, setNlAdvisories] = useState<IndexAdvisory[]>([]);
  const [advisoryStatus, setAdvisoryStatus] = useState<
    Record<string, { state: "creating" | "created" | "error"; message?: string }>
  >({});
  const [nlMode, setNlMode] = useState<"cypher" | "aql">("cypher");
  // Holds the Cypher that failed to transpile with a recoverable
  // (UNSUPPORTED / NOT_IMPLEMENTED) error, enabling the "Generate AQL
  // with AI" fallback button in the error banner. Null when the current
  // error isn't a recoverable transpile failure.
  const [aqlFallbackCypher, setAqlFallbackCypher] = useState<string | null>(null);
  const directAqlRef = useRef(false); // true when AQL came from NL→AQL direct path
  const [aqlModified, setAqlModified] = useState(false);
  const editedAqlRef = useRef("");
  const [learnSaving, setLearnSaving] = useState(false);
  const [learnInfo, setLearnInfo] = useState("");
  const [showCorrections, setShowCorrections] = useState(false);
  const [corrections, setCorrections] = useState<CorrectionRecord[]>([]);
  const [nlHistory, setNlHistory] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem("nl_history") || "[]"); } catch { return []; }
  });
  // Auto-generated sample questions for the *current* (database, graph) scope.
  // Kept separate from the user's persisted history so they regenerate (and
  // replace, never accumulate) whenever the connected DB or named-graph scope
  // changes, instead of going stale against a previously-connected database.
  const [nlSamples, setNlSamples] = useState<string[]>([]);
  const lastSampleKeyRef = useRef<string | null>(null);
  const [nlHistoryOpen, setNlHistoryOpen] = useState(false);
  const [autoTranslate, setAutoTranslate] = useState<boolean>(() => {
    try { return localStorage.getItem("auto_translate") === "1"; } catch { return false; }
  });
  const [autoRun, setAutoRun] = useState<boolean>(() => {
    try { return localStorage.getItem("auto_run") === "1"; } catch { return false; }
  });
  const [pendingAutoTranslate, setPendingAutoTranslate] = useState(false);
  const [pendingAutoRun, setPendingAutoRun] = useState(false);

  const toggleAutoTranslate = useCallback(() => {
    setAutoTranslate((prev) => {
      const next = !prev;
      try { localStorage.setItem("auto_translate", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  }, []);

  const toggleAutoRun = useCallback(() => {
    setAutoRun((prev) => {
      const next = !prev;
      try { localStorage.setItem("auto_run", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  }, []);
  const nlHistoryRef = useRef<HTMLDivElement>(null);
  const mappingRef = useRef(state.mapping);
  mappingRef.current = state.mapping;
  const cypherRef = useRef(state.cypher);
  cypherRef.current = state.cypher;
  const paramsRef = useRef(state.params);
  paramsRef.current = state.params;

  const activeStmtRef = useRef(state.activeStatement);
  activeStmtRef.current = state.activeStatement;

  function getActiveStatement(): string {
    const stmts = splitCypherStatements(cypherRef.current);
    const idx = Math.min(activeStmtRef.current, stmts.length - 1);
    return stmts[idx] || "";
  }

  function makeRequest() {
    const p = paramsRef.current;
    return {
      cypher: getActiveStatement(),
      mapping: mappingRef.current,
      params: Object.keys(p).length > 0 ? p : undefined,
      extensions_enabled: true,
    };
  }

  // ``addToHistory`` accepts an optional snapshot bundle so callers
  // that have just executed the query (Run / Profile / direct-AQL) can
  // persist the rows. Translate-only and Explain-only callers pass no
  // snapshot — the reducer's ADD_HISTORY case is responsible for
  // carrying any *existing* snapshot forward so those calls don't wipe
  // cached rows.
  function addToHistory(
    aql: string,
    snapshot?: {
      results?: unknown[];
      bindVars?: Record<string, unknown>;
      execMs?: number | null;
    },
  ) {
    const cypher = cypherRef.current.trim();
    if (!cypher) return;
    const conn = state.connection;
    dispatch({
      type: "ADD_HISTORY",
      entry: {
        cypher,
        timestamp: Date.now(),
        aqlPreview: aql.slice(0, 120),
        aql,
        bindVars: snapshot?.bindVars,
        results: snapshot?.results,
        rowCount: snapshot?.results?.length,
        execMs: snapshot?.execMs ?? null,
        connectionUrl: conn.url,
        connectionDatabase: conn.database,
      },
    });
  }

  // Any 401 means the session token the backend issued us has
  // expired (or was revoked). The token is useless from here on —
  // drop it so the header flips back to "Connect to ArangoDB" and
  // the user can re-authenticate. The caller still surfaces the
  // friendly "Please re-authenticate" message from ApiError in its
  // own XXX_ERROR dispatch.
  const handleMaybeAuthError = useCallback(
    (err: unknown) => {
      if (isAuthError(err)) {
        dispatch({ type: "DISCONNECT" });
      }
    },
    [dispatch],
  );

  const handleTranslate = useCallback(async () => {
    if (!cypherRef.current.trim()) return;
    directAqlRef.current = false;
    setAqlFallbackCypher(null);
    dispatch({ type: "TRANSLATE_START" });
    try {
      const resp = await translateCypher(makeRequest());
      dispatch({
        type: "TRANSLATE_SUCCESS",
        aql: resp.aql,
        bindVars: resp.bind_vars,
        warnings: resp.warnings,
        translateMs: resp.elapsed_ms,
      });
      addToHistory(resp.aql);
      if (autoRun) setPendingAutoRun(true);
    } catch (err) {
      dispatch({
        type: "TRANSLATE_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
      setAqlFallbackCypher(isTranspileFallbackError(err) ? cypherRef.current : null);
      handleMaybeAuthError(err);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch, autoRun, handleMaybeAuthError]);

  const handleExecute = useCallback(async () => {
    if (!state.connection.token) return;
    setAqlFallbackCypher(null);
    dispatch({ type: "EXECUTE_START" });
    try {
      if (directAqlRef.current && state.aql) {
        const resp = await executeAql(state.aql, state.bindVars, state.connection.token);
        dispatch({ type: "EXECUTE_SUCCESS", results: resp.results, warnings: resp.warnings, execMs: resp.exec_ms });
        addToHistory(resp.aql, {
          results: resp.results,
          bindVars: state.bindVars,
          execMs: resp.exec_ms,
        });
      } else {
        if (!cypherRef.current.trim()) return;
        const resp = await executeCypher(makeRequest(), state.connection.token);
        dispatch({
          type: "TRANSLATE_SUCCESS",
          aql: resp.aql,
          bindVars: resp.bind_vars,
          warnings: resp.warnings,
          translateMs: resp.translate_ms,
        });
        dispatch({ type: "EXECUTE_SUCCESS", results: resp.results, warnings: resp.warnings, execMs: resp.exec_ms });
        addToHistory(resp.aql, {
          results: resp.results,
          bindVars: resp.bind_vars,
          execMs: resp.exec_ms,
        });
      }
    } catch (err) {
      dispatch({
        type: "EXECUTE_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
      // Only the Cypher->AQL path can raise a transpile error; a direct-AQL
      // run that fails is an execution error, not a transpile one.
      setAqlFallbackCypher(
        !directAqlRef.current && isTranspileFallbackError(err) ? cypherRef.current : null,
      );
      handleMaybeAuthError(err);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch, state.connection.token, state.aql, state.bindVars, handleMaybeAuthError]);

  const handleExplain = useCallback(async () => {
    if (!cypherRef.current.trim() || !state.connection.token) return;
    dispatch({ type: "EXPLAIN_START" });
    try {
      const resp = await explainCypher(makeRequest(), state.connection.token);
      dispatch({
        type: "TRANSLATE_SUCCESS",
        aql: resp.aql,
        bindVars: resp.bind_vars,
        translateMs: resp.translate_ms,
      });
      dispatch({ type: "EXPLAIN_SUCCESS", plan: resp.plan });
      addToHistory(resp.aql);
    } catch (err) {
      dispatch({
        type: "EXPLAIN_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
      handleMaybeAuthError(err);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch, state.connection.token, handleMaybeAuthError]);

  const handleProfile = useCallback(async () => {
    if (!cypherRef.current.trim() || !state.connection.token) return;
    dispatch({ type: "PROFILE_START" });
    try {
      const resp = await profileCypher(makeRequest(), state.connection.token);
      dispatch({
        type: "TRANSLATE_SUCCESS",
        aql: resp.aql,
        bindVars: resp.bind_vars,
        translateMs: resp.translate_ms,
      });
      dispatch({
        type: "PROFILE_SUCCESS",
        results: resp.results,
        statistics: resp.statistics,
        profile: resp.profile,
      });
      // Profile carries execution rows but the response shape lacks a
      // dedicated ``exec_ms`` — the profile statistics block has its
      // own timing fields. Persist the rows + bind vars so a history
      // restore replays the Results pane; statistics/profile are not
      // snapshotted (the analytical view is regenerated only when the
      // user explicitly profiles again).
      addToHistory(resp.aql, {
        results: resp.results,
        bindVars: resp.bind_vars,
        execMs: null,
      });
    } catch (err) {
      dispatch({
        type: "PROFILE_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
      handleMaybeAuthError(err);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch, state.connection.token, handleMaybeAuthError]);

  const addNlHistory = useCallback((query: string) => {
    setNlHistory((prev) => {
      const filtered = prev.filter((q) => q !== query);
      const next = [query, ...filtered].slice(0, 50);
      localStorage.setItem("nl_history", JSON.stringify(next));
      return next;
    });
  }, []);

  // Combined Ask-input suggestions: the user's own typed history first, then
  // the freshly-generated samples for the current (database, graph) scope,
  // de-duplicated. Samples live in their own state so a DB/graph switch
  // replaces them rather than leaving stale entries behind.
  const nlSuggestions = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const q of nlHistory) {
      if (q && !seen.has(q)) { seen.add(q); out.push(q); }
    }
    for (const q of nlSamples) {
      if (q && !seen.has(q)) { seen.add(q); out.push(q); }
    }
    return out;
  }, [nlHistory, nlSamples]);

  // Seed the Ask-history with a representative set of NL queries the first
  // time we connect to a given database and finish schema introspection.
  const mappingEntityCount = useMemo(() => {
    const pm = (state.mapping as Record<string, unknown>)?.physical_mapping as
      | Record<string, unknown>
      | undefined;
    const ents = pm?.entities as Record<string, unknown> | undefined;
    return ents ? Object.keys(ents).length : 0;
  }, [state.mapping]);

  // Whether the *analysed* schema is multi-tenant. Unlike the old
  // mapping-only heuristic (which just looked for a literal `Tenant`
  // entity), this is decided by the backend's tenant-scope manifest
  // after introspection — so it also catches the common denormalised
  // shape where tenancy lives only as a field (e.g. `Alert.tenantId`)
  // with no `Tenant` collection at all. Drives whether the tenant
  // picker is shown.
  const [tenantMultiTenant, setTenantMultiTenant] = useState(false);
  const [tenantCatalog, setTenantCatalog] = useState<DiscoveredTenant[]>([]);
  const [tenantsDetected, setTenantsDetected] = useState(false);
  const [tenantsLoading, setTenantsLoading] = useState(false);
  const [tenantContext, setTenantContext] = useState<TenantContext | null>(null);
  // Diagnostic state — how tenants were discovered (Tenant collection
  // vs denormalised field), the field/collection involved, and the last
  // error. Surfaced in the selector tooltip / empty state so a missing
  // tenant list is never silent.
  const [tenantResolution, setTenantResolution] = useState<{
    collection: string | null;
    source: "client" | "heuristic" | null;
    error: string | null;
  }>({ collection: null, source: null, error: null });

  // Named-graph scoping (PRD §17). The catalog of named graphs in the
  // connected database, the currently-bound scope (null = all
  // collections), and the last fetch error. Unlike tenancy, named graphs
  // can be listed immediately after connect (a single `db.graphs()` call),
  // so this is fetched as soon as we have a token.
  const [graphCatalog, setGraphCatalog] = useState<NamedGraph[]>([]);
  const [graphsLoading, setGraphsLoading] = useState(false);
  const [graphScope, setGraphScope] = useState<string | null>(null);
  const [graphError, setGraphError] = useState<string | null>(null);

  // Discover selectable tenants once we're connected and schema
  // analysis has finished. This is deliberately a *post-analysis*
  // step: whether the schema is multi-tenant — and what the tenant
  // ids are — can only be known after introspection builds the
  // tenant-scope manifest. The backend handles both shapes (a
  // `Tenant` collection or denormalised field values) and tells us
  // via `multiTenant` whether to show the picker at all.
  useEffect(() => {
    const token = state.connection.token;
    if (!token) {
      setTenantMultiTenant(false);
      setTenantCatalog([]);
      setTenantsDetected(false);
      setTenantContext(null);
      setTenantResolution({ collection: null, source: null, error: null });
      return;
    }
    if (state.introspecting) return;
    let cancelled = false;
    setTenantsLoading(true);
    (async () => {
      try {
        // Pass the introspected mapping so the server can build the
        // tenant-scope manifest and resolve the actual physical
        // collection names / denormalised tenant field.
        const mapping =
          (state.mapping as Record<string, unknown> | null | undefined) || null;
        const resp = await discoverTenants(token, mapping);
        if (cancelled) return;
        setTenantMultiTenant(resp.multiTenant);
        // `detected` for the selector = "we have a usable tenant
        // catalog". For multi-tenant schemas with no rows yet (empty
        // probed collections) we still flag detected so the empty
        // state reads "no tenants" rather than "no tenant collection".
        setTenantsDetected(resp.multiTenant);
        setTenantCatalog(resp.tenants || []);
        setTenantResolution({
          collection: resp.collections?.[0] ?? resp.tenantField ?? null,
          source: null,
          error: null,
        });
        if (!resp.multiTenant) {
          setTenantContext(null);
          setTenantsLoading(false);
          return;
        }
        // Rehydrate a previously-saved selection for this (url, database).
        const saved = loadTenantContext(
          state.connection.url,
          state.connection.database,
        );
        if (saved) {
          // Only rehydrate if the saved value still resolves to a
          // tenant in the catalog. Selections persisted by older UI
          // bundles may be keyed on TENANT_HEX_ID / NAME; we
          // transparently migrate them to the canonical `_key` form
          // so the user doesn't lose their selection across a
          // bundle upgrade.
          const list = resp.tenants || [];
          let resolved =
            saved.property === "_key"
              ? list.find((t) => t.key === saved.value)
              : saved.property === "TENANT_HEX_ID"
                ? list.find((t) => t.hex_id === saved.value)
                : saved.property === "NAME"
                  ? list.find((t) => t.name === saved.value)
                  : undefined;
          if (resolved) {
            const migrated = {
              property: "_key",
              value: resolved.key,
              display: resolved.name || resolved.subdomain || resolved.key,
            };
            setTenantContext(migrated);
            if (saved.property !== "_key" || saved.value !== resolved.key) {
              saveTenantContext(state.connection.url, state.connection.database, migrated);
            }
            // Re-bind the freshly-issued session to the rehydrated
            // tenant. The session token changes on every (re)connect,
            // so a saved selection is meaningless until we push it back
            // to the server — otherwise the first query after a reload
            // runs unbound and trips Layer 5.
            try {
              await bindTenant(token, resolved.key);
            } catch (bindErr) {
              console.warn("Tenant rebind on rehydrate failed:", bindErr);
            }
          } else {
            setTenantContext(null);
            saveTenantContext(state.connection.url, state.connection.database, null);
          }
        } else {
          setTenantContext(null);
        }
      } catch (err) {
        // Surface HTTP status when available (ApiError carries it).
        // The most common failure mode in practice is a stale backend
        // that doesn't know about /tenants at all (404) or doesn't
        // accept the new query param shape (405). Showing the status
        // in the pill turns "Tenant lookup failed" from a dead-end
        // into something the operator can act on.
        const status =
          err && typeof err === "object" && "status" in err
            ? ` (HTTP ${(err as { status: number }).status})`
            : "";
        const base = err instanceof Error ? err.message : String(err);
        const msg = `${base}${status}`;
        console.warn("Tenant catalog fetch failed:", msg);
        if (!cancelled) {
          setTenantCatalog([]);
          setTenantsDetected(false);
          setTenantResolution({ collection: null, source: null, error: msg });
          // Only react to a 401 if this effect run is still current. During a
          // database switch the previous run is cancelled (token/database
          // changed); a late 401 from its in-flight request must NOT disconnect
          // the freshly-established session.
          if (isAuthError(err)) {
            dispatch({ type: "DISCONNECT" });
          }
        }
      } finally {
        if (!cancelled) setTenantsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // `dispatch` comes from `useAppState`; including it in the deps
    // here would cause this expensive tenant-catalog fetch to re-run
    // on every reducer update (the memoized dispatch identity changes
    // with `state`). It's safe to omit — React guarantees the
    // reducer's underlying `dispatch` is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    state.connection.token,
    state.connection.url,
    state.connection.database,
    state.introspecting,
    state.mapping,
  ]);

  const handleTenantSelect = useCallback(
    (ctx: TenantContext | null) => {
      setTenantContext(ctx);
      saveTenantContext(state.connection.url, state.connection.database, ctx);
      // Re-bind the live session to the chosen tenant (or clear it for
      // "All tenants"). This is what actually scopes execution at
      // Layers 4–6 — the local `tenantContext` only drives the
      // NL-to-Cypher guardrail and the pill's display.
      const token = state.connection.token;
      if (!token) return;
      (async () => {
        try {
          await bindTenant(token, ctx ? ctx.value : null);
        } catch (err) {
          console.warn("Tenant bind failed:", err);
          if (isAuthError(err)) dispatch({ type: "DISCONNECT" });
        }
      })();
    },
    // `dispatch` is stable (useReducer); omitted intentionally so this
    // callback identity only changes with the connection target.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [state.connection.url, state.connection.database, state.connection.token],
  );

  // Re-introspect the live session so the freshly-bound named-graph scope
  // is reflected in the mapping. Uses the cache (force=false): each scope
  // has its own cache slot, so this is cheap on rehydrate. Shared by the
  // graph picker and the rehydrate path.
  const reintrospectScoped = useCallback(
    async (token: string, opts: { force?: boolean } = {}) => {
      const force = opts.force ?? false;
      dispatch({ type: "INTROSPECT_START", analyzing: force });
      try {
        // Catalog model: a read serves the analyzed mapping the sidecar keeps
        // warm (read-only, fast). When the catalog has nothing for this database
        // yet, introspect reports status="pending"; we surface that as an
        // actionable banner rather than spinning forever. "Analyze now" (force)
        // bypasses the catalog and runs the full analyzer synchronously.
        const schema = force
          ? await introspectSchema(token, 50, true)
          : await introspectSchemaUntilReady(token);
        if (schema.status === "pending") {
          dispatch({ type: "INTROSPECT_PENDING" });
        } else {
          const mapping = introspectToMapping(schema);
          dispatch({
            type: "INTROSPECT_SUCCESS",
            mapping,
            warnings: schema.warnings ?? [],
          });
        }
      } catch (err) {
        dispatch({
          type: "INTROSPECT_ERROR",
          error: err instanceof Error ? err.message : "Introspection failed",
        });
      }
    },
    // dispatch is stable (useReducer).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Fetch the named-graph catalog once connected, and rehydrate a saved
  // scope for this (url, database). Named graphs are cheap to enumerate
  // (a single `db.graphs()` call), so unlike tenant discovery this does
  // not wait for introspection.
  useEffect(() => {
    const token = state.connection.token;
    if (!token) {
      setGraphCatalog([]);
      setGraphScope(null);
      setGraphError(null);
      return;
    }
    let cancelled = false;
    setGraphsLoading(true);
    const { url, database } = state.connection;
    (async () => {
      try {
        const resp = await listGraphs(token);
        if (cancelled) return;
        const graphs = resp.graphs || [];
        setGraphCatalog(graphs);
        setGraphError(null);
        const saved = loadGraphScope(url, database);
        if (saved && graphs.some((g) => g.name === saved)) {
          setGraphScope(saved);
          // The session token is fresh on every (re)connect, so a saved
          // scope must be pushed back to the server, and the initial
          // (connect-time) introspection ran unscoped — re-introspect.
          try {
            await bindGraph(token, saved);
            if (!cancelled) await reintrospectScoped(token);
          } catch (bindErr) {
            console.warn("Graph rebind on rehydrate failed:", bindErr);
          }
        } else {
          setGraphScope(null);
          if (saved) saveGraphScope(url, database, null);
        }
      } catch (err) {
        const status =
          err && typeof err === "object" && "status" in err
            ? ` (HTTP ${(err as { status: number }).status})`
            : "";
        const msg = `${err instanceof Error ? err.message : String(err)}${status}`;
        console.warn("Graph catalog fetch failed:", msg);
        if (!cancelled) {
          setGraphCatalog([]);
          setGraphError(msg);
          // Stale-run guard: a 401 from a cancelled run (e.g. mid database
          // switch) must not tear down the new session. See tenant effect.
          if (isAuthError(err)) dispatch({ type: "DISCONNECT" });
        }
      } finally {
        if (!cancelled) setGraphsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // dispatch is stable; reintrospectScoped identity is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.connection.token, state.connection.url, state.connection.database]);

  const handleGraphSelect = useCallback(
    (graphName: string | null) => {
      setGraphScope(graphName);
      saveGraphScope(state.connection.url, state.connection.database, graphName);
      const token = state.connection.token;
      if (!token) return;
      (async () => {
        try {
          await bindGraph(token, graphName);
          // Re-introspect so the scoped mapping drives translation /
          // NL / execution from here on.
          await reintrospectScoped(token);
        } catch (err) {
          console.warn("Graph bind failed:", err);
          setGraphError(err instanceof Error ? err.message : String(err));
          if (isAuthError(err)) dispatch({ type: "DISCONNECT" });
        }
      })();
    },
    // dispatch / reintrospectScoped are stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [state.connection.url, state.connection.database, state.connection.token, reintrospectScoped],
  );

  const tenantContextRef = useRef<TenantContext | null>(null);
  tenantContextRef.current = tenantContext;

  useEffect(() => {
    if (state.connection.status !== "connected") return;
    if (state.introspecting) return;
    if (mappingEntityCount === 0) return;

    // Regenerate samples whenever the (database, named-graph) scope changes.
    // Including graphScope in the key means selecting/clearing a named graph
    // re-derives samples against the scoped mapping; a per-session ref avoids
    // re-calling the LLM on unrelated re-renders of the same scope.
    const key = `${state.connection.url}||${state.connection.database}||${graphScope ?? ""}`;
    if (lastSampleKeyRef.current === key) return;

    let cancelled = false;
    (async () => {
      try {
        const resp = await suggestNlQueries(state.mapping, 8);
        if (cancelled) return;
        lastSampleKeyRef.current = key;
        setNlSamples(resp.queries ?? []);
      } catch (err) {
        console.warn("NL sample generation failed:", err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    state.connection.status,
    state.connection.url,
    state.connection.database,
    graphScope,
    state.introspecting,
    mappingEntityCount,
    state.mapping,
  ]);

  // When the connection drops, forget the generated samples so a reconnect
  // (or a switch to a different database) starts from a clean slate.
  useEffect(() => {
    if (state.connection.status !== "connected") {
      lastSampleKeyRef.current = null;
      setNlSamples([]);
    }
  }, [state.connection.status]);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (nlHistoryRef.current && !nlHistoryRef.current.contains(e.target as Node)) {
        setNlHistoryOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const handleNL = useCallback(async () => {
    if (!nlInput.trim()) return;
    addNlHistory(nlInput.trim());
    setNlLoading(true);
    setNlInfo("");
    setNlError("");
    setNlAdvisories([]);
    setAdvisoryStatus({});
    try {
      const tenantCtx = tenantContextRef.current;
      if (nlMode === "aql") {
        const resp = await nl2Aql(nlInput, mappingRef.current, tenantCtx);
        if (resp.aql) {
          directAqlRef.current = true;
          dispatch({
            type: "TRANSLATE_SUCCESS",
            aql: resp.aql,
            bindVars: resp.bind_vars || {},
            warnings: [],
            translateMs: resp.elapsed_ms ?? null,
          });
          dispatch({ type: "SET_CYPHER", cypher: `/* NL→AQL: ${nlInput.trim()} */` });
          const ms = resp.elapsed_ms != null ? ` ${resp.elapsed_ms}ms` : "";
          const tokens = resp.total_tokens ? ` ${resp.total_tokens}tok` : "";
          const info = `${resp.method} (${Math.round(resp.confidence * 100)}%)${ms}${tokens}`;
          setNlInfo(info);
          if (autoRun) setPendingAutoRun(true);
        } else {
          setNlInfo(resp.explanation || "Could not generate AQL");
        }
      } else {
        const resp = await nl2Cypher(nlInput, mappingRef.current, {
          sessionToken: state.connection.token ?? undefined,
          tenantContext: tenantCtx,
        });
        // WP-S3c: surface any inverted-index advisories the entity resolver
        // recorded (fuzzy probes that hit a full scan) regardless of whether
        // a cypher came back — the missing index is worth flagging either way.
        setNlAdvisories(resp.advisories ?? []);
        // WP-29: structured fail-closed methods produce an empty
        // ``cypher`` by design. Surface them as a red banner and
        // never write the server payload into the Cypher editor.
        // Tenant-guardrail follows the same shape but is emitted by
        // the tenant-scope postcondition rather than the retry loop.
        const isFailClosed =
          resp.method === "validation_failed" ||
          resp.method === "tenant_guardrail_blocked";
        if (resp.cypher) {
          directAqlRef.current = false;
          // WP-30: dispatch NL_SUCCESS (not SET_CYPHER) so the
          // reducer flags the editor's provenance as "nl_pipeline"
          // and records the NL question. The translate-error banner
          // reads both fields to decide whether to offer a one-click
          // regenerate-with-hint action.
          dispatch({
            type: "NL_SUCCESS",
            cypher: resp.cypher,
            question: nlInput.trim(),
          });
          const ms = resp.elapsed_ms != null ? ` ${resp.elapsed_ms}ms` : "";
          const tokens = resp.total_tokens ? ` ${resp.total_tokens}tok` : "";
          const info = `${resp.method} (${Math.round(resp.confidence * 100)}%)${ms}${tokens}`;
          setNlInfo(info);
          if (autoTranslate || autoRun) setPendingAutoTranslate(true);
        } else if (isFailClosed) {
          setNlError(resp.explanation || "NL → Cypher failed validation");
        } else {
          setNlInfo(resp.explanation || "Could not generate Cypher");
        }
      }
    } catch (err) {
      setNlInfo(err instanceof Error ? err.message : "NL translation failed");
      handleMaybeAuthError(err);
    } finally {
      setNlLoading(false);
    }
  }, [nlInput, nlMode, dispatch, addNlHistory, autoTranslate, autoRun, state.connection.token, handleMaybeAuthError]);

  // WP-S3c: one-click creation of the inverted index an advisory recommends.
  // Authenticated (mutates the connected DB). Idempotent server-side. Tracks
  // per-advisory status so the button can show creating/created/error inline.
  const handleCreateIndex = useCallback(
    async (advisory: IndexAdvisory) => {
      const token = state.connection.token;
      if (!token) return;
      const key = `${advisory.collection}.${advisory.field}`;
      setAdvisoryStatus((s) => ({ ...s, [key]: { state: "creating" } }));
      try {
        const resp = await createIndex(token, advisory);
        setAdvisoryStatus((s) => ({
          ...s,
          [key]: {
            state: "created",
            message: resp.created
              ? "Index created."
              : resp.message || "Index already exists.",
          },
        }));
      } catch (err) {
        setAdvisoryStatus((s) => ({
          ...s,
          [key]: {
            state: "error",
            message: err instanceof Error ? err.message : "Index creation failed",
          },
        }));
        handleMaybeAuthError(err);
      }
    },
    [state.connection.token, handleMaybeAuthError],
  );

  // WP-30: regenerate the Cypher from the last NL question, feeding
  // the current translate error back into the LLM as retry context.
  // Gated on ``editorCypherSource === "nl_pipeline"`` in the banner —
  // hand-written Cypher that fails Translate is the user's query,
  // not an NL-pipeline artifact, so regenerate must not appear.
  const handleRegenerateFromNl = useCallback(async () => {
    if (!state.lastNlQuestion) return;
    const hint = state.error || "";
    setNlLoading(true);
    setNlError("");
    try {
      const tenantCtx = tenantContextRef.current;
      const resp = await nl2Cypher(state.lastNlQuestion, mappingRef.current, {
        sessionToken: state.connection.token ?? undefined,
        tenantContext: tenantCtx,
        retryContext: hint,
      });
      const isFailClosed =
        resp.method === "validation_failed" ||
        resp.method === "tenant_guardrail_blocked";
      if (resp.cypher) {
        directAqlRef.current = false;
        dispatch({
          type: "NL_SUCCESS",
          cypher: resp.cypher,
          question: state.lastNlQuestion,
        });
        dispatch({ type: "CLEAR_ERROR" });
        const ms = resp.elapsed_ms != null ? ` ${resp.elapsed_ms}ms` : "";
        const tokens = resp.total_tokens ? ` ${resp.total_tokens}tok` : "";
        setNlInfo(
          `regenerated ${resp.method} (${Math.round(resp.confidence * 100)}%)${ms}${tokens}`,
        );
        if (autoTranslate || autoRun) setPendingAutoTranslate(true);
      } else if (isFailClosed) {
        setNlError(resp.explanation || "NL regenerate failed validation");
      } else {
        setNlInfo(resp.explanation || "Could not regenerate Cypher");
      }
    } catch (err) {
      setNlInfo(err instanceof Error ? err.message : "Regenerate failed");
      handleMaybeAuthError(err);
    } finally {
      setNlLoading(false);
    }
  }, [
    state.lastNlQuestion,
    state.error,
    state.connection.token,
    dispatch,
    autoTranslate,
    autoRun,
    handleMaybeAuthError,
  ]);

  // C (transparent fallback): when a Cypher query can't be transpiled
  // deterministically (UNSUPPORTED / NOT_IMPLEMENTED), offer a one-click
  // recovery that asks the LLM to translate the *failing Cypher* to AQL via
  // /nl2aql. If the Cypher came from the NL pipeline we also pass the original
  // question as context. The result runs through /execute-aql (Layer 4/5 still
  // apply), so this stays within the tenant-safety boundary.
  const handleFallbackToAql = useCallback(async () => {
    const failingCypher = aqlFallbackCypher;
    if (!failingCypher) return;
    setNlLoading(true);
    setNlError("");
    setNlInfo("");
    try {
      const tenantCtx = tenantContextRef.current;
      const question =
        state.editorCypherSource === "nl_pipeline" ? state.lastNlQuestion || "" : "";
      const resp = await nl2Aql(question, mappingRef.current, tenantCtx, failingCypher);
      if (resp.aql) {
        directAqlRef.current = true;
        dispatch({
          type: "TRANSLATE_SUCCESS",
          aql: resp.aql,
          bindVars: resp.bind_vars || {},
          warnings: [],
          translateMs: resp.elapsed_ms,
        });
        dispatch({ type: "CLEAR_ERROR" });
        setAqlFallbackCypher(null);
        const ms = resp.elapsed_ms != null ? ` ${resp.elapsed_ms}ms` : "";
        const tokens = resp.total_tokens ? ` ${resp.total_tokens}tok` : "";
        setNlInfo(`AI-generated AQL from Cypher (${resp.method})${ms}${tokens}`);
        if (autoRun) setPendingAutoRun(true);
      } else {
        setNlError(resp.explanation || "AI could not translate this Cypher to AQL.");
      }
    } catch (err) {
      setNlError(err instanceof Error ? err.message : "AI Cypher→AQL fallback failed");
      handleMaybeAuthError(err);
    } finally {
      setNlLoading(false);
    }
  }, [
    aqlFallbackCypher,
    state.editorCypherSource,
    state.lastNlQuestion,
    dispatch,
    autoRun,
    handleMaybeAuthError,
  ]);

  // Chain auto-translate after NL→Cypher when enabled.
  useEffect(() => {
    if (!pendingAutoTranslate) return;
    if (state.translating || state.executing) return;
    if (!state.cypher.trim()) return;
    setPendingAutoTranslate(false);
    handleTranslate();
  }, [pendingAutoTranslate, state.cypher, state.translating, state.executing, handleTranslate]);

  // Chain auto-run after a successful translate (manual or auto).
  useEffect(() => {
    if (!pendingAutoRun) return;
    if (state.translating || state.executing) return;
    if (!state.aql.trim()) return;
    if (!state.connection.token) return;
    setPendingAutoRun(false);
    handleExecute();
  }, [
    pendingAutoRun,
    state.aql,
    state.translating,
    state.executing,
    state.connection.token,
    handleExecute,
  ]);

  const handleAqlModified = useCallback((modified: boolean, editedAql: string) => {
    setAqlModified(modified);
    editedAqlRef.current = editedAql;
    if (!modified) setLearnInfo("");
  }, []);

  const handleLearn = useCallback(async () => {
    if (!aqlModified || !editedAqlRef.current.trim()) return;
    setLearnSaving(true);
    setLearnInfo("");
    try {
      await saveCorrection({
        cypher: cypherRef.current,
        mapping: mappingRef.current,
        database: state.connection.database || "",
        original_aql: state.aql,
        corrected_aql: editedAqlRef.current,
        bind_vars: state.bindVars,
      });
      setLearnInfo("Saved");
      setAqlModified(false);
    } catch (err) {
      setLearnInfo(err instanceof Error ? err.message : "Save failed");
    } finally {
      setLearnSaving(false);
    }
  }, [aqlModified, state.aql, state.bindVars, state.connection.database]);

  const loadCorrections = useCallback(async () => {
    try {
      const resp = await listCorrections();
      setCorrections(resp.corrections);
    } catch { /* ignore */ }
  }, []);

  const handleDeleteCorrection = useCallback(async (id: number) => {
    try {
      await deleteCorrection(id);
      setCorrections((prev) => prev.filter((c) => c.id !== id));
    } catch { /* ignore */ }
  }, []);

  const handleJumpToLine = useCallback((line: number) => {
    const view = cypherViewRef.current;
    if (!view) return;
    const lineInfo = view.state.doc.line(Math.min(line, view.state.doc.lines));
    view.dispatch({
      selection: { anchor: lineInfo.from },
      scrollIntoView: true,
    });
    view.focus();
  }, []);

  const isConnected = state.connection.status === "connected";
  const isLoading =
    state.translating || state.executing || state.explaining || state.profiling;

  return (
    <div className="h-screen flex flex-col bg-gray-950 text-gray-100">
      <SchemaWarningBanner
        warnings={state.schemaWarnings}
        url={state.connection.url}
        database={state.connection.database}
        token={state.connection.token}
        dispatch={dispatch}
      />
      {/* Connection bar */}
      <header className="flex items-center justify-between px-4 py-2 bg-gray-900 border-b border-gray-800">
        <div className="flex items-center gap-3">
          <h1 className="text-sm font-semibold text-white tracking-tight">
            Arango Cypher
          </h1>
          <span className="text-gray-600 text-xs">|</span>
          <ConnectionDialog
            connection={state.connection}
            introspecting={state.introspecting}
            analyzing={state.schemaAnalyzing}
            dispatch={dispatch}
          />
        </div>
        <div className="flex items-center gap-2">
          {isConnected && (
            <GraphSelector
              graphs={graphCatalog}
              loading={graphsLoading}
              selection={graphScope}
              onSelect={handleGraphSelect}
              error={graphError}
            />
          )}
          {isConnected && tenantMultiTenant && (
            <TenantSelector
              tenants={tenantCatalog}
              loading={tenantsLoading}
              selection={tenantContext}
              onSelect={handleTenantSelect}
              detected={tenantsDetected}
              resolvedCollection={tenantResolution.collection}
              source={tenantResolution.source}
              error={tenantResolution.error}
            />
          )}
          <button
            onClick={() => setShowSamples(true)}
            className="px-2.5 py-1 text-xs rounded bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors"
          >
            Samples
          </button>
          <button
            onClick={() => setShowHistory(true)}
            className="px-2.5 py-1 text-xs rounded bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors"
          >
            History
            {state.history.length > 0 && (
              <span className="ml-1.5 text-gray-500">
                ({state.history.length})
              </span>
            )}
          </button>
          <button
            onClick={() => setShowOutline(!showOutline)}
            className={`px-2.5 py-1 text-xs rounded transition-colors ${
              showOutline
                ? "bg-indigo-600/20 text-indigo-400 border border-indigo-600/30"
                : "bg-gray-800 text-gray-400 hover:text-gray-200"
            }`}
          >
            Outline
          </button>
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
        <div className="px-4 py-2 bg-red-900/30 border-b border-red-800 flex items-center justify-between gap-3">
          <span className="text-sm text-red-300 flex-1 break-words">{state.error}</span>
          <div className="flex items-center gap-2 shrink-0">
            {/*
              WP-30: one-click regenerate only when the editor's
              Cypher came from the NL pipeline and we still have the
              original question. Hand-written Cypher never offers
              this — a user edit flips editorCypherSource to "user".
              The last NL question may be null if the user cleared
              history or reconnected; we grey out in that case so the
              affordance stays discoverable but non-confusing.
            */}
            {state.editorCypherSource === "nl_pipeline" && (
              <button
                onClick={handleRegenerateFromNl}
                disabled={
                  nlLoading ||
                  !state.lastNlQuestion ||
                  !isConnected
                }
                title={
                  state.lastNlQuestion
                    ? "Re-invoke NL→Cypher with this error as a retry hint"
                    : "Regenerate unavailable — original question not available in this session"
                }
                className="px-2 py-1 text-xs font-medium rounded bg-violet-600 hover:bg-violet-500 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {nlLoading ? "..." : "Regenerate from NL with error hint"}
              </button>
            )}
            {aqlFallbackCypher && (
              <button
                onClick={handleFallbackToAql}
                disabled={nlLoading || !isConnected}
                title="This Cypher uses a feature the transpiler can't translate. Ask the LLM to generate equivalent AQL instead."
                className="px-2 py-1 text-xs font-medium rounded bg-amber-600 hover:bg-amber-500 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {nlLoading ? "..." : "Generate AQL with AI"}
              </button>
            )}
            <button
              onClick={() => {
                setAqlFallbackCypher(null);
                dispatch({ type: "CLEAR_ERROR" });
              }}
              className="text-red-400 hover:text-red-200 text-xs"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}

      {/* Schema-pending banner — the catalog has no analyzed mapping for this
          database yet (sidecar hasn't synced it, or the session expired). Give
          the user a clear status and concrete next steps instead of an endless
          spinner. */}
      {state.schemaPending && isConnected && (
        <div className="px-4 py-2 bg-amber-900/30 border-b border-amber-700/60 flex items-center justify-between gap-3">
          <span className="text-sm text-amber-200 flex-1 break-words">
            <strong className="font-semibold">Schema not ready.</strong>{" "}
            The analyzed schema for{" "}
            <span className="font-mono">{state.connection.database}</span>{" "}
            isn&apos;t in the catalog yet. The background sidecar refreshes it on a
            schedule — wait a moment and check again, analyze it now (slow), or
            reconnect if your session may have expired.
          </span>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => {
                const t = state.connection.token;
                if (t) reintrospectScoped(t);
              }}
              disabled={state.introspecting}
              title="Re-read the catalog (fast). Use this after the sidecar has synced."
              className="px-2 py-1 text-xs font-medium rounded bg-amber-700 hover:bg-amber-600 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Check again
            </button>
            <button
              onClick={() => {
                const t = state.connection.token;
                if (t) reintrospectScoped(t, { force: true });
              }}
              disabled={state.introspecting}
              title="Bypass the catalog and run the full schema analysis now. This can take a minute on a large database."
              className="px-2 py-1 text-xs font-medium rounded bg-gray-700 hover:bg-gray-600 text-gray-100 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Analyze now
            </button>
          </div>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 min-h-0 flex">
        {/* Mapping sidebar */}
        {showMapping ? (
          <>
            <div className="border-r border-gray-800 flex-shrink-0 relative" style={{ width: mappingWidth }}>
              <MappingPanel
                mapping={state.mapping}
                onChange={(m) => dispatch({ type: "SET_MAPPING", mapping: m })}
                onClose={() => setShowMapping(false)}
              />
              {state.introspecting && (
                <div className="absolute inset-0 bg-gray-950/70 flex items-center justify-center z-20 backdrop-blur-sm">
                  <div className="flex flex-col items-center gap-2 px-4 text-center">
                    <svg className="w-6 h-6 text-indigo-400 animate-spin" viewBox="0 0 24 24" fill="none">
                      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.5" strokeDasharray="42" strokeDashoffset="12" strokeLinecap="round" />
                    </svg>
                    <span className="text-xs text-gray-300 font-medium">
                      {state.schemaAnalyzing ? "Analyzing schema…" : "Loading schema…"}
                    </span>
                    <span className="text-[10px] text-gray-500">
                      {state.schemaAnalyzing
                        ? "Running full analysis — this can take up to a minute"
                        : "Reading the schema catalog"}
                    </span>
                  </div>
                </div>
              )}
            </div>
            <div
              className="w-1.5 flex-shrink-0 cursor-col-resize hover:bg-indigo-500/30 active:bg-indigo-500/40 transition-colors"
              onMouseDown={(e) => {
                e.preventDefault();
                dragRef.current = { startX: e.clientX, startW: mappingWidth };
                document.body.style.cursor = "col-resize";
                document.body.style.userSelect = "none";
              }}
            />
          </>
        ) : (
          <button
            onClick={() => setShowMapping(true)}
            title="Show schema mapping pane"
            aria-label="Show schema mapping pane"
            className="w-6 flex-shrink-0 flex flex-col items-center justify-center gap-2 bg-gray-900/40 hover:bg-gray-800 border-r border-gray-800 group transition-colors"
          >
            <span className="text-gray-500 group-hover:text-indigo-400 text-xs leading-none transition-colors">
              &#9654;
            </span>
            <span
              className="text-[10px] text-gray-600 group-hover:text-gray-400 uppercase tracking-wider transition-colors"
              style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
            >
              Mapping
            </span>
          </button>
        )}

        {/* Editors and results */}
        <div className="flex-1 min-w-0 flex flex-col">
          {/* NL input bar */}
          <div className="flex items-center gap-2 px-3 py-1.5 bg-gray-900/30 border-b border-gray-800">
            <span className="text-xs text-gray-500 shrink-0">Ask:</span>
            {tenantContext && (
              <span
                className="flex items-center gap-1 px-1.5 py-0.5 rounded bg-amber-900/30 border border-amber-700/60 text-amber-300 text-[10px] shrink-0"
                title={`Queries scoped to Tenant.${tenantContext.property} = ${tenantContext.value}`}
              >
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-400" />
                {tenantContext.display || tenantContext.value}
              </span>
            )}
            <div className="flex-1 relative" ref={nlHistoryRef}>
              <input
                type="text"
                value={nlInput}
                onChange={(e) => setNlInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { handleNL(); setNlHistoryOpen(false); } }}
                onFocus={() => { if (nlSuggestions.length > 0) setNlHistoryOpen(true); }}
                placeholder="Describe what you want in plain English..."
                className="w-full bg-gray-800 text-gray-200 text-xs rounded px-2.5 py-1.5 border border-gray-700 focus:border-indigo-500 focus:outline-none placeholder-gray-600"
              />
              {nlHistoryOpen && nlSuggestions.length > 0 && (
                <div className="absolute left-0 right-0 top-full mt-0.5 z-50 bg-gray-800 border border-gray-700 rounded shadow-xl max-h-48 overflow-y-auto">
                  {nlSuggestions.map((q, i) => (
                    <button
                      key={i}
                      className="w-full text-left px-2.5 py-1.5 text-xs text-gray-300 hover:bg-gray-700 hover:text-white truncate transition-colors"
                      title={q}
                      onClick={() => { setNlInput(q); setNlHistoryOpen(false); }}
                    >
                      {q}
                    </button>
                  ))}
                </div>
              )}
            </div>
            {/* NL output mode toggle: Cypher (two-stage) vs AQL (direct) */}
            <div className="flex items-center rounded border border-gray-700 overflow-hidden shrink-0">
              <button
                onClick={() => setNlMode("cypher")}
                className={`px-2 py-1 text-[10px] font-medium transition-colors ${nlMode === "cypher" ? "bg-indigo-600 text-white" : "bg-gray-800 text-gray-400 hover:text-gray-200"}`}
                title="NL → Cypher → AQL (two-stage)"
              >
                Cypher
              </button>
              <button
                onClick={() => setNlMode("aql")}
                className={`px-2 py-1 text-[10px] font-medium transition-colors ${nlMode === "aql" ? "bg-amber-600 text-white" : "bg-gray-800 text-gray-400 hover:text-gray-200"}`}
                title="NL → AQL (direct, requires LLM)"
              >
                AQL
              </button>
            </div>
            <button
              onClick={handleNL}
              disabled={nlLoading || !nlInput.trim()}
              className="px-3 py-1.5 text-xs font-medium rounded bg-violet-600 hover:bg-violet-500 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
            >
              {nlLoading ? "..." : "Generate"}
            </button>
            {nlInfo && !nlError && (
              <span className="text-[10px] text-emerald-500/70 shrink-0 max-w-[280px] truncate tabular-nums" title={nlInfo}>
                {nlInfo}
              </span>
            )}
          </div>

          {nlError && (
            <div
              role="alert"
              className="mx-2 mb-2 px-3 py-2 rounded border border-red-700/60 bg-red-950/40 text-red-200 text-xs whitespace-pre-wrap flex items-start gap-2"
            >
              <span className="font-semibold shrink-0">NL → Cypher failed:</span>
              <span className="flex-1 break-words">{nlError}</span>
              <button
                onClick={() => setNlError("")}
                className="text-red-300 hover:text-red-100 text-[10px] uppercase tracking-wide shrink-0"
                title="Dismiss"
              >
                dismiss
              </button>
            </div>
          )}

          {/* WP-S3c: inverted-index advisories from the NL entity resolver.
              A fuzzy name match fell back to a full collection scan; offer
              one-click creation of an inverted (ArangoSearch) index. */}
          {nlAdvisories.length > 0 && (
            <div className="mx-2 mb-2 px-3 py-2 rounded border border-amber-700/50 bg-amber-950/30 text-amber-200 text-xs space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="text-amber-400" aria-hidden>&#9888;</span>
                <span className="font-semibold">Slow fuzzy match detected</span>
                <button
                  onClick={() => setNlAdvisories([])}
                  className="ml-auto text-amber-300/80 hover:text-amber-100 text-[10px] uppercase tracking-wide"
                  title="Dismiss"
                >
                  dismiss
                </button>
              </div>
              {nlAdvisories.map((adv) => {
                const key = `${adv.collection}.${adv.field}`;
                const status = advisoryStatus[key];
                return (
                  <div key={key} className="flex items-center gap-2 flex-wrap">
                    <span className="flex-1 min-w-[180px] break-words text-amber-300/90">
                      Fuzzy matching on <code className="text-amber-200">{adv.collection}.{adv.field}</code> runs a
                      full collection scan. An inverted index speeds it up.
                    </span>
                    {status?.state === "created" ? (
                      <span className="text-emerald-400 text-[11px] shrink-0">
                        &#10003; {status.message}
                      </span>
                    ) : (
                      <button
                        onClick={() => handleCreateIndex(adv)}
                        disabled={!state.connection.token || status?.state === "creating"}
                        className="px-2.5 py-1 text-[11px] font-medium rounded bg-amber-600 hover:bg-amber-500 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
                        title={`Create an inverted index on ${adv.collection}.${adv.field}`}
                      >
                        {status?.state === "creating" ? "Creating..." : "Create index"}
                      </button>
                    )}
                    {status?.state === "error" && (
                      <span className="text-red-300 text-[11px] w-full">{status.message}</span>
                    )}
                  </div>
                );
              })}
            </div>
          )}

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
              disabled={isLoading || !isConnected || (!state.cypher.trim() && !state.aql)}
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

            <div className="w-px h-5 bg-gray-700 ml-2" />

            <label
              className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-200 cursor-pointer select-none"
              title="Automatically Translate after generating Cypher from natural language"
            >
              <input
                type="checkbox"
                checked={autoTranslate}
                onChange={toggleAutoTranslate}
                className="w-3.5 h-3.5 rounded border-gray-600 bg-gray-800 text-indigo-500 focus:ring-1 focus:ring-indigo-500 focus:ring-offset-0 cursor-pointer"
              />
              Auto-translate
            </label>
            <label
              className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-200 cursor-pointer select-none"
              title="Automatically Run after a successful Translate (requires connection)"
            >
              <input
                type="checkbox"
                checked={autoRun}
                onChange={toggleAutoRun}
                disabled={!isConnected}
                className="w-3.5 h-3.5 rounded border-gray-600 bg-gray-800 text-emerald-500 focus:ring-1 focus:ring-emerald-500 focus:ring-offset-0 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              />
              Auto-run
            </label>

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
              <div className="px-3 py-1.5 bg-gray-900/30 border-b border-gray-800 flex items-center gap-2">
                <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
                  Cypher
                </span>
                {(() => {
                  const stmts = splitCypherStatements(state.cypher);
                  if (stmts.length <= 1) return null;
                  const idx = Math.min(state.activeStatement, stmts.length - 1);
                  return (
                    <div className="flex items-center gap-1 ml-2">
                      <button
                        onClick={() => dispatch({ type: "SET_ACTIVE_STATEMENT", index: Math.max(0, idx - 1) })}
                        disabled={idx === 0}
                        className="w-5 h-5 rounded text-[10px] bg-gray-800 text-gray-400 hover:text-gray-200 disabled:opacity-30 flex items-center justify-center transition-colors"
                      >
                        &#9664;
                      </button>
                      <span className="text-[10px] text-gray-500 tabular-nums whitespace-nowrap">
                        {idx + 1} / {stmts.length}
                      </span>
                      <button
                        onClick={() => dispatch({ type: "SET_ACTIVE_STATEMENT", index: Math.min(stmts.length - 1, idx + 1) })}
                        disabled={idx === stmts.length - 1}
                        className="w-5 h-5 rounded text-[10px] bg-gray-800 text-gray-400 hover:text-gray-200 disabled:opacity-30 flex items-center justify-center transition-colors"
                      >
                        &#9654;
                      </button>
                    </div>
                  );
                })()}
              </div>
              <div className="flex-1 min-h-0 flex">
                <div className="flex-1 min-w-0">
                  <CypherEditor
                    value={state.cypher}
                    mapping={state.mapping}
                    onChange={(v) =>
                      dispatch({ type: "SET_CYPHER", cypher: v })
                    }
                    onTranslate={handleTranslate}
                    onExecute={handleExecute}
                    onExplain={handleExplain}
                    onProfile={handleProfile}
                    viewRef={cypherViewRef}
                    highlightLines={cypherHighlightLines}
                    onHoverLine={handleCypherHoverLine}
                  />
                </div>
                {showOutline && (
                  <div className="w-48 border-l border-gray-800 overflow-y-auto bg-gray-900/30 shrink-0">
                    <div className="px-3 py-1.5 border-b border-gray-800">
                      <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">
                        Clause Outline
                      </span>
                    </div>
                    <ClauseOutline
                      cypher={state.cypher}
                      onJumpToLine={handleJumpToLine}
                    />
                  </div>
                )}
              </div>
              <ParameterPanel
                cypher={state.cypher}
                params={state.params}
                onChange={(p) => dispatch({ type: "SET_PARAMS", params: p })}
              />
            </div>

            {/* AQL editor */}
            <div className="flex-1 min-w-0 flex flex-col">
              <div className="px-3 py-1.5 bg-gray-900/30 border-b border-gray-800 flex items-center gap-2">
                <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
                  AQL
                </span>
                {state.aql && !aqlModified && (
                  <span className="text-xs text-gray-600">editable</span>
                )}
                {aqlModified && (
                  <span className="text-xs text-amber-400 font-medium">modified</span>
                )}
                {aqlModified && (
                  <button
                    onClick={handleLearn}
                    disabled={learnSaving}
                    className="px-2 py-0.5 text-[10px] font-medium rounded bg-emerald-600 hover:bg-emerald-500 text-white transition-colors disabled:opacity-40"
                  >
                    {learnSaving ? "Saving..." : "Learn"}
                  </button>
                )}
                {learnInfo && (
                  <span className="text-[10px] text-emerald-400">{learnInfo}</span>
                )}
                <div className="flex-1" />
                <button
                  onClick={() => { setShowCorrections(!showCorrections); if (!showCorrections) loadCorrections(); }}
                  className="text-[10px] text-gray-500 hover:text-gray-300 transition-colors"
                  title="View learned corrections"
                >
                  {showCorrections ? "Hide" : "Learned"} ({corrections.length})
                </button>
                {state.translateMs != null && (
                  <span className="text-[10px] text-emerald-500/70 tabular-nums">
                    Cypher→AQL {state.translateMs}ms
                  </span>
                )}
                {state.execMs != null && (
                  <span className="text-[10px] text-sky-400/70 tabular-nums">
                    AQL exec {state.execMs}ms
                  </span>
                )}
              </div>
              {showCorrections && (
                <div className="max-h-40 overflow-y-auto bg-gray-900/50 border-b border-gray-800">
                  {corrections.length === 0 ? (
                    <div className="px-3 py-2 text-xs text-gray-500">No learned corrections yet</div>
                  ) : (
                    corrections.map((c) => (
                      <div key={c.id} className="flex items-start gap-2 px-3 py-1.5 border-b border-gray-800/50 hover:bg-gray-800/30">
                        <div className="flex-1 min-w-0">
                          <div className="text-[10px] text-gray-400 truncate" title={c.cypher}>
                            {c.cypher.slice(0, 80)}
                          </div>
                          <div className="text-[10px] text-gray-600">
                            {c.database || "any"} · {new Date(c.created_at).toLocaleDateString()}
                          </div>
                        </div>
                        <button
                          onClick={() => handleDeleteCorrection(c.id)}
                          className="text-[10px] text-red-500/60 hover:text-red-400 shrink-0"
                          title="Delete this correction"
                        >
                          ✕
                        </button>
                      </div>
                    ))
                  )}
                </div>
              )}
              {state.warnings.length > 0 && (
                <div className="px-3 py-1.5 bg-amber-900/20 border-b border-amber-800/30 space-y-0.5">
                  {state.warnings.map((w, i) => (
                    <div key={i} className="flex items-start gap-2">
                      <span className="text-amber-500 text-xs mt-0.5 shrink-0">&#9888;</span>
                      <span className="text-xs text-amber-400">{w.message}</span>
                    </div>
                  ))}
                </div>
              )}
              <div className="flex-1 min-h-0">
                <AqlEditor
                  value={state.aql}
                  bindVars={state.bindVars}
                  error={null}
                  onModified={handleAqlModified}
                  mapping={state.mapping}
                  highlightLines={aqlHighlightLines}
                  onHoverLine={handleAqlHoverLine}
                />
              </div>
            </div>
          </div>

          {/* Results panel */}
          <div className="h-64 border-t border-gray-800 flex-shrink-0">
            <ResultsPanel
              results={state.results}
              warnings={state.warnings}
              explainPlan={state.explainPlan}
              profileData={state.profileData}
              activeTab={state.activeResultTab}
              dispatch={dispatch}
              execMs={state.execMs}
            />
          </div>
        </div>
      </div>

      {showHistory && (
        <QueryHistory
          history={state.history}
          currentConnection={{
            url: state.connection.url,
            database: state.connection.database,
          }}
          onSelect={(entry) => dispatch({ type: "RESTORE_FROM_HISTORY", entry })}
          onClear={() => dispatch({ type: "CLEAR_HISTORY" })}
          onClose={() => setShowHistory(false)}
        />
      )}

      {showSamples && (
        <SampleQueries
          onSelect={(cypher) => dispatch({ type: "SET_CYPHER", cypher })}
          onClose={() => setShowSamples(false)}
        />
      )}
    </div>
  );
}
