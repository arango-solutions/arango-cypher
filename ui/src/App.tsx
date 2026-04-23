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
  saveCorrection,
  listCorrections,
  deleteCorrection,
  suggestNlQueries,
  listTenants,
  isAuthError,
  type CorrectionRecord,
  type TenantContext,
  type TenantRecord,
} from "./api/client";

const NL_SAMPLES_SEEN_KEY = "nl_samples_seen";
const TENANT_CTX_KEY = "tenant_context";

function loadSeenNlSamples(): Record<string, number> {
  try {
    const raw = localStorage.getItem(NL_SAMPLES_SEEN_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function markSeenNlSamples(key: string) {
  try {
    const seen = loadSeenNlSamples();
    seen[key] = Date.now();
    localStorage.setItem(NL_SAMPLES_SEEN_KEY, JSON.stringify(seen));
  } catch {
    // ignore
  }
}

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
  const [nlMode, setNlMode] = useState<"cypher" | "aql">("cypher");
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

  function addToHistory(aql: string) {
    const cypher = cypherRef.current.trim();
    if (!cypher) return;
    dispatch({
      type: "ADD_HISTORY",
      entry: {
        cypher,
        timestamp: Date.now(),
        aqlPreview: aql.slice(0, 120),
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
      handleMaybeAuthError(err);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch, autoRun, handleMaybeAuthError]);

  const handleExecute = useCallback(async () => {
    if (!state.connection.token) return;
    dispatch({ type: "EXECUTE_START" });
    try {
      if (directAqlRef.current && state.aql) {
        const resp = await executeAql(state.aql, state.bindVars, state.connection.token);
        dispatch({ type: "EXECUTE_SUCCESS", results: resp.results, warnings: resp.warnings, execMs: resp.exec_ms });
        addToHistory(resp.aql);
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
        addToHistory(resp.aql);
      }
    } catch (err) {
      dispatch({
        type: "EXECUTE_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
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
      addToHistory(resp.aql);
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

  const appendNlHistory = useCallback((queries: string[]) => {
    if (!queries.length) return;
    setNlHistory((prev) => {
      const existing = new Set(prev);
      const additions = queries.filter((q) => q && !existing.has(q));
      if (additions.length === 0) return prev;
      const next = [...prev, ...additions].slice(0, 100);
      localStorage.setItem("nl_history", JSON.stringify(next));
      return next;
    });
  }, []);

  // Seed the Ask-history with a representative set of NL queries the first
  // time we connect to a given database and finish schema introspection.
  const mappingEntityCount = useMemo(() => {
    const pm = (state.mapping as Record<string, unknown>)?.physical_mapping as
      | Record<string, unknown>
      | undefined;
    const ents = pm?.entities as Record<string, unknown> | undefined;
    return ents ? Object.keys(ents).length : 0;
  }, [state.mapping]);

  // Heuristic: this schema is multi-tenant if the conceptual or
  // physical mapping declares a `Tenant` entity. When true we show the
  // tenant selector; when false we hide it entirely so single-tenant
  // workspaces get no extra chrome. See
  // `arango_cypher.nl2cypher.tenant_guardrail.has_tenant_entity`
  // for the mirrored backend check.
  const hasTenantEntity = useMemo(() => {
    const m = state.mapping as Record<string, unknown>;
    const cs =
      (m?.conceptual_schema as Record<string, unknown> | undefined) ??
      (m?.conceptualSchema as Record<string, unknown> | undefined);
    const ents = cs?.entities;
    if (Array.isArray(ents)) {
      for (const e of ents) {
        if (e && typeof e === "object" && (e as { name?: unknown }).name === "Tenant") {
          return true;
        }
      }
    }
    const pm =
      (m?.physical_mapping as Record<string, unknown> | undefined) ??
      (m?.physicalMapping as Record<string, unknown> | undefined);
    const pEnts = pm?.entities as Record<string, unknown> | undefined;
    return !!(pEnts && Object.prototype.hasOwnProperty.call(pEnts, "Tenant"));
  }, [state.mapping]);

  const [tenantCatalog, setTenantCatalog] = useState<TenantRecord[]>([]);
  const [tenantsDetected, setTenantsDetected] = useState(false);
  const [tenantsLoading, setTenantsLoading] = useState(false);
  const [tenantContext, setTenantContext] = useState<TenantContext | null>(null);
  // Diagnostic state — what collection the backend tried to query and
  // whether it found it via mapping vs heuristic. Surfaced in the
  // selector tooltip / empty state so a missing tenant list isn't
  // silent.
  const [tenantResolution, setTenantResolution] = useState<{
    collection: string | null;
    source: "client" | "heuristic" | null;
    error: string | null;
  }>({ collection: null, source: null, error: null });

  // Fetch the tenant catalog when we connect to a schema that has a
  // Tenant entity. Skipped otherwise — we want /tenants to be a no-op
  // for single-tenant workspaces, not an always-on network call.
  useEffect(() => {
    const token = state.connection.token;
    if (!token) {
      setTenantCatalog([]);
      setTenantsDetected(false);
      setTenantContext(null);
      setTenantResolution({ collection: null, source: null, error: null });
      return;
    }
    if (state.introspecting) return;
    if (!hasTenantEntity) {
      setTenantCatalog([]);
      setTenantsDetected(false);
      setTenantContext(null);
      setTenantResolution({ collection: null, source: null, error: null });
      return;
    }
    let cancelled = false;
    setTenantsLoading(true);
    (async () => {
      try {
        // Pass the introspected mapping so the server can resolve the
        // *actual* tenant collection name (e.g. `Tenants` vs literal
        // `Tenant`) from physical_mapping. Without this, real-world
        // schemas where the collection name doesn't match the
        // conceptual entity name silently produce an empty catalog.
        const mapping =
          (state.mapping as Record<string, unknown> | null | undefined) || null;
        const resp = await listTenants(token, mapping);
        if (cancelled) return;
        setTenantsDetected(resp.detected);
        setTenantCatalog(resp.tenants || []);
        setTenantResolution({
          collection: resp.collection ?? null,
          source: resp.source ?? null,
          error: null,
        });
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
        }
        if (isAuthError(err)) {
          dispatch({ type: "DISCONNECT" });
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
    hasTenantEntity,
    state.mapping,
  ]);

  const handleTenantSelect = useCallback(
    (ctx: TenantContext | null) => {
      setTenantContext(ctx);
      saveTenantContext(state.connection.url, state.connection.database, ctx);
    },
    [state.connection.url, state.connection.database],
  );

  const tenantContextRef = useRef<TenantContext | null>(null);
  tenantContextRef.current = tenantContext;

  useEffect(() => {
    if (state.connection.status !== "connected") return;
    if (state.introspecting) return;
    if (mappingEntityCount === 0) return;

    const key = `${state.connection.url}||${state.connection.database}`;
    const seen = loadSeenNlSamples();
    if (seen[key]) return;

    let cancelled = false;
    (async () => {
      try {
        const resp = await suggestNlQueries(state.mapping, 8);
        if (cancelled) return;
        if (resp.queries && resp.queries.length > 0) {
          appendNlHistory(resp.queries);
        }
        markSeenNlSamples(key);
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
    state.introspecting,
    mappingEntityCount,
    state.mapping,
    appendNlHistory,
  ]);

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
          dispatch({ type: "SET_CYPHER", cypher: resp.cypher });
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
            Cypher Workbench
          </h1>
          <span className="text-gray-600 text-xs">|</span>
          <ConnectionDialog
            connection={state.connection}
            introspecting={state.introspecting}
            dispatch={dispatch}
          />
        </div>
        <div className="flex items-center gap-2">
          {isConnected && hasTenantEntity && (
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
                  <div className="flex flex-col items-center gap-2">
                    <svg className="w-6 h-6 text-indigo-400 animate-spin" viewBox="0 0 24 24" fill="none">
                      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.5" strokeDasharray="42" strokeDashoffset="12" strokeLinecap="round" />
                    </svg>
                    <span className="text-xs text-gray-300 font-medium">Extracting schema...</span>
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
                onFocus={() => { if (nlHistory.length > 0) setNlHistoryOpen(true); }}
                placeholder="Describe what you want in plain English..."
                className="w-full bg-gray-800 text-gray-200 text-xs rounded px-2.5 py-1.5 border border-gray-700 focus:border-indigo-500 focus:outline-none placeholder-gray-600"
              />
              {nlHistoryOpen && nlHistory.length > 0 && (
                <div className="absolute left-0 right-0 top-full mt-0.5 z-50 bg-gray-800 border border-gray-700 rounded shadow-xl max-h-48 overflow-y-auto">
                  {nlHistory.map((q, i) => (
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
          onSelect={(cypher) => dispatch({ type: "SET_CYPHER", cypher })}
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
