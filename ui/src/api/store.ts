import { useCallback, useReducer } from "react";
import type { SchemaWarning } from "./client";

export interface ConnectionState {
  status: "disconnected" | "connecting" | "connected";
  token: string | null;
  url: string;
  database: string;
  username: string;
  password: string;
  databases: string[];
  error: string | null;
}

export type ResultTab = "table" | "json" | "graph" | "explain" | "profile";

// History entries may carry a snapshot of the result rows the query
// produced, so the user can re-open a prior result *without* re-running
// the query against the live database. Snapshots are bounded
// (``MAX_ROWS_PER_ENTRY``) and the whole history payload is bounded
// (``MAX_PERSIST_BYTES``) so we never blow the localStorage quota.
//
// ``rowCount`` is the *true* number of rows the query returned, even if
// ``results`` was row-truncated or dropped entirely to fit storage. The
// UI uses (rowCount, results) to render badges:
//   results === undefined && rowCount === undefined  →  no snapshot (legacy entry / translate-only)
//   results !== undefined && truncated  →  "N rows (showing first K)"
//   results === undefined && rowCount !== undefined  →  "N rows (snapshot dropped to fit storage)"
//   results !== undefined && !truncated  →  "N rows"
export interface HistoryEntry {
  cypher: string;
  timestamp: number;
  aqlPreview: string;
  // Snapshot fields — all optional so entries written by older UI
  // bundles continue to deserialize without migration.
  aql?: string;
  bindVars?: Record<string, unknown>;
  results?: unknown[];
  rowCount?: number;
  truncated?: boolean;
  execMs?: number | null;
  connectionUrl?: string;
  connectionDatabase?: string;
}

export interface AppState {
  connection: ConnectionState;
  cypher: string;
  mapping: Record<string, unknown>;
  params: Record<string, unknown>;
  aql: string;
  bindVars: Record<string, unknown>;
  results: unknown[] | null;
  warnings: Array<{ message: string }>;
  explainPlan: unknown | null;
  profileData: { statistics: Record<string, unknown>; profile: unknown } | null;
  activeResultTab: ResultTab;
  error: string | null;
  introspecting: boolean;
  // True while a *full* schema analysis is running (the force/"Refresh schema"
  // path), as opposed to a cheap read-only catalog read. Drives a distinct,
  // honest spinner label ("Analyzing… can take a minute") so a multi-second
  // wait doesn't look frozen.
  schemaAnalyzing: boolean;
  // True when the catalog has no analyzed mapping for this database yet
  // (introspect returned status="pending"). Surfaced as an actionable banner —
  // not a generic error — telling the user to reconnect, wait for the sidecar,
  // or analyze now.
  schemaPending: boolean;
  translating: boolean;
  executing: boolean;
  explaining: boolean;
  profiling: boolean;
  history: HistoryEntry[];
  translateMs: number | null;
  execMs: number | null;
  activeStatement: number;
  // Backend-supplied schema warnings (ANALYZER_NOT_INSTALLED etc.). The
  // banner reads from this; the dismissal-suppression list lives in
  // localStorage keyed by (url, database, code) so the same warning can
  // re-appear on a different connection without leaking dismissals.
  schemaWarnings: SchemaWarning[];
  // WP-30: tracks the provenance of the Cypher currently sitting in
  // the editor. ``"nl_pipeline"`` after a successful NL→Cypher, and
  // ``"user"`` after any user edit / paste / sample load. The
  // translate-error banner exposes a one-click regenerate action only
  // when this is ``"nl_pipeline"`` — hand-written Cypher that fails
  // Translate is the user's query and must not be silently replaced.
  editorCypherSource: "nl_pipeline" | "user" | null;
  // WP-30: the NL question that produced the editor's current Cypher,
  // when ``editorCypherSource === "nl_pipeline"``. Used as the
  // question argument on regenerate-with-hint. Null when no NL
  // produced the current editor contents (either hand-written or no
  // NL issued in this session).
  lastNlQuestion: string | null;
}

const STORAGE_KEY = "cypher-workbench";

const MAX_HISTORY = 50;

// Per-entry row cap. Snapshots beyond this are row-truncated and flagged
// ``truncated: true``. 1000 rows keeps a useful sample for replay while
// holding a single entry under ~1 MB for typical document shapes.
export const MAX_ROWS_PER_ENTRY = 1000;

// Total persistence cap. localStorage in most browsers caps the origin
// at ~5 MB; we leave headroom for the user's other localStorage keys
// (``nl_history``, ``auto_translate``, tenant context, etc.).
export const MAX_PERSIST_BYTES = 4_500_000;

// Cap a single snapshot's rows. Pure function so the reducer can call
// it deterministically. Mutates nothing — returns a (possibly fresh)
// HistoryEntry plus a flag indicating whether truncation happened.
export function truncateEntrySnapshot(entry: HistoryEntry): HistoryEntry {
  if (!entry.results) return entry;
  const rowCount = entry.rowCount ?? entry.results.length;
  if (entry.results.length <= MAX_ROWS_PER_ENTRY) {
    return { ...entry, rowCount, truncated: entry.truncated ?? false };
  }
  return {
    ...entry,
    results: entry.results.slice(0, MAX_ROWS_PER_ENTRY),
    rowCount,
    truncated: true,
  };
}

function buildPersistPayload(state: AppState, history: HistoryEntry[]): string {
  return JSON.stringify({
    cypher: state.cypher,
    mapping: state.mapping,
    params: state.params,
    history,
  });
}

// Progressively drop result snapshots from the oldest entries until the
// serialized payload fits ``maxBytes``. We keep cypher + aqlPreview +
// rowCount on every entry so the panel can still tell the user there
// *was* a result, just not its rows. Returns a fresh array (caller
// shouldn't observe mutation).
export function trimHistoryForPersistence(
  state: AppState,
  history: HistoryEntry[],
  maxBytes: number,
): HistoryEntry[] {
  const working = history.slice(0, MAX_HISTORY).map((h) => ({ ...h }));
  if (buildPersistPayload(state, working).length <= maxBytes) return working;
  // Walk oldest → newest, stripping ``results`` (and ``bindVars`` —
  // they can be large for queries with $documents bind vars) until the
  // payload fits. ``rowCount`` is preserved so the UI can still render
  // an honest "snapshot dropped to fit storage" indicator.
  for (let i = working.length - 1; i >= 0; i--) {
    if (working[i].results) {
      delete working[i].results;
      delete working[i].bindVars;
      if (buildPersistPayload(state, working).length <= maxBytes) return working;
    }
  }
  return working;
}

function loadSavedState(): Partial<AppState> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const saved = JSON.parse(raw);
    return {
      cypher: saved.cypher ?? "",
      mapping: saved.mapping ?? {},
      params: saved.params ?? {},
      history: Array.isArray(saved.history) ? saved.history.slice(0, MAX_HISTORY) : [],
    };
  } catch {
    return {};
  }
}

function saveState(state: AppState) {
  // Two-pass write: try the full payload first; if it exceeds our soft
  // cap *or* the browser throws QuotaExceededError, retry with the
  // result snapshots progressively stripped. The cypher / mapping /
  // params slice always persists — those are tiny and the user expects
  // them to survive a reload.
  const history = state.history.slice(0, MAX_HISTORY);
  const fullPayload = buildPersistPayload(state, history);
  try {
    if (fullPayload.length <= MAX_PERSIST_BYTES) {
      localStorage.setItem(STORAGE_KEY, fullPayload);
      return;
    }
    const trimmed = trimHistoryForPersistence(state, history, MAX_PERSIST_BYTES);
    localStorage.setItem(STORAGE_KEY, buildPersistPayload(state, trimmed));
  } catch {
    // QuotaExceededError or localStorage disabled. Try the most
    // aggressive trim: every snapshot dropped. If even that fails the
    // browser is fundamentally refusing writes — there's nothing useful
    // we can do.
    try {
      const minimal = history.map((h) => {
        const { results: _r, bindVars: _b, ...rest } = h;
        void _r; void _b;
        return rest;
      });
      localStorage.setItem(STORAGE_KEY, buildPersistPayload(state, minimal));
    } catch {
      // localStorage unavailable — silently degrade
    }
  }
}

export const initialState: AppState = {
  connection: {
    status: "disconnected",
    token: null,
    url: "http://localhost:8529",
    database: "_system",
    username: "root",
    password: "",
    databases: [],
    error: null,
  },
  cypher: "MATCH (p1:Person)-[:KNOWS]->(p2:Person)\nRETURN p1, p2",
  mapping: {},
  params: {},
  aql: "",
  bindVars: {},
  results: null,
  warnings: [],
  explainPlan: null,
  profileData: null,
  activeResultTab: "table",
  error: null,
  introspecting: false,
  schemaAnalyzing: false,
  schemaPending: false,
  translating: false,
  executing: false,
  explaining: false,
  profiling: false,
  history: [],
  translateMs: null,
  execMs: null,
  activeStatement: 0,
  schemaWarnings: [],
  editorCypherSource: null,
  lastNlQuestion: null,
  ...loadSavedState(),
};

export type Action =
  // WP-30: ``source`` lets callers declare whether the write came
  // from the NL pipeline or user input. Omitting ``source`` defaults
  // to ``"user"`` so existing dispatchers (editor onChange, sample
  // loads, history replay, paste) correctly flip the provenance flag
  // without every call site needing to be updated.
  | { type: "SET_CYPHER"; cypher: string; source?: "nl_pipeline" | "user" }
  // WP-30: compound action emitted after a successful NL→Cypher
  // translation. Sets the editor contents, flags the provenance as
  // ``"nl_pipeline"``, and records the NL question so the translate-
  // error regenerate-with-hint action can reuse it as the question
  // argument. Prefer this over ``SET_CYPHER + source: "nl_pipeline"``
  // because it keeps the cypher + question bookkeeping atomic.
  | { type: "NL_SUCCESS"; cypher: string; question: string }
  | { type: "SET_MAPPING"; mapping: Record<string, unknown> }
  | { type: "SET_MAPPING_JSON"; json: string }
  | {
      type: "CONNECT_START";
      url: string;
      database: string;
      username: string;
    }
  | {
      type: "CONNECT_SUCCESS";
      token: string;
      databases: string[];
      url: string;
      database: string;
      username: string;
      password: string;
    }
  | { type: "CONNECT_ERROR"; error: string }
  | { type: "DISCONNECT" }
  | { type: "INTROSPECT_START"; analyzing?: boolean }
  | {
      type: "INTROSPECT_SUCCESS";
      mapping: Record<string, unknown>;
      warnings?: SchemaWarning[];
    }
  | { type: "INTROSPECT_ERROR"; error: string }
  | { type: "INTROSPECT_PENDING" }
  | { type: "SCHEMA_WARNINGS_REPLACE"; warnings: SchemaWarning[] }
  | { type: "SCHEMA_WARNINGS_CLEAR" }
  | { type: "TRANSLATE_START" }
  | {
      type: "TRANSLATE_SUCCESS";
      aql: string;
      bindVars: Record<string, unknown>;
      warnings?: Array<{ message: string }>;
      translateMs?: number | null;
    }
  | { type: "TRANSLATE_ERROR"; error: string }
  | { type: "EXECUTE_START" }
  | { type: "EXECUTE_SUCCESS"; results: unknown[]; warnings?: Array<{ message: string }>; execMs?: number | null }
  | { type: "EXECUTE_ERROR"; error: string }
  | { type: "EXPLAIN_START" }
  | { type: "EXPLAIN_SUCCESS"; plan: unknown }
  | { type: "EXPLAIN_ERROR"; error: string }
  | { type: "PROFILE_START" }
  | {
      type: "PROFILE_SUCCESS";
      results: unknown[];
      statistics: Record<string, unknown>;
      profile: unknown;
    }
  | { type: "PROFILE_ERROR"; error: string }
  | { type: "SET_RESULT_TAB"; tab: ResultTab }
  | { type: "CLEAR_ERROR" }
  | { type: "SET_PARAMS"; params: Record<string, unknown> }
  | { type: "ADD_HISTORY"; entry: HistoryEntry }
  | { type: "CLEAR_HISTORY" }
  // Restore a prior history entry into the live workbench state — the
  // editor's Cypher, the AQL pane, bind vars, and (if the entry carries
  // a snapshot) the Results pane. Used when the user clicks an entry in
  // the QueryHistory panel; far cheaper than re-running the query.
  | { type: "RESTORE_FROM_HISTORY"; entry: HistoryEntry }
  | { type: "SET_ACTIVE_STATEMENT"; index: number };

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "SET_CYPHER":
      return {
        ...state,
        cypher: action.cypher,
        editorCypherSource: action.source ?? "user",
      };
    case "NL_SUCCESS":
      return {
        ...state,
        cypher: action.cypher,
        editorCypherSource: "nl_pipeline",
        lastNlQuestion: action.question,
      };
    case "SET_MAPPING":
      return { ...state, mapping: action.mapping };
    case "SET_MAPPING_JSON":
      try {
        return { ...state, mapping: JSON.parse(action.json) };
      } catch {
        return state;
      }
    case "CONNECT_START":
      // Track the attempted url/database/username on the connection state
      // immediately. If the attempt fails, CONNECT_ERROR keeps these fields
      // (it spreads ...state.connection), so the form-reset useEffect in
      // ConnectionDialog will re-seed the form with what the user actually
      // tried — not the hardcoded localhost default. Without this the
      // dialog silently snaps back to localhost:8529 after every failed
      // auto-connect, which hides the real (e.g. cloud) URL the user
      // would otherwise edit and retry.
      return {
        ...state,
        connection: {
          ...state.connection,
          status: "connecting",
          url: action.url,
          database: action.database,
          username: action.username,
          // Drop the previous token the moment a (re)connect starts. On a
          // database switch the old token is about to be (or already is)
          // server-side invalidated; keeping it here causes the token+database
          // keyed effects (tenant discovery, graph catalog) to re-fire against
          // the NEW database with the DEAD token → spurious 401s, and a late
          // 401 from one of those in-flight calls could even DISCONNECT the
          // freshly-issued session. Nulling it makes those effects bail
          // (`if (!token) return`) until CONNECT_SUCCESS installs the new token.
          token: null,
          error: null,
        },
      };
    case "CONNECT_SUCCESS":
      return {
        ...state,
        connection: {
          status: "connected",
          token: action.token,
          url: action.url,
          database: action.database,
          username: action.username,
          password: action.password,
          databases: action.databases,
          error: null,
        },
      };
    case "CONNECT_ERROR":
      return {
        ...state,
        connection: {
          ...state.connection,
          status: "disconnected",
          error: action.error,
        },
      };
    case "DISCONNECT":
      return {
        ...state,
        connection: {
          ...state.connection,
          status: "disconnected",
          token: null,
          databases: [],
          error: null,
        },
        results: null,
        explainPlan: null,
        profileData: null,
        schemaWarnings: [],
        schemaPending: false,
        schemaAnalyzing: false,
        editorCypherSource: null,
        lastNlQuestion: null,
      };
    case "INTROSPECT_START":
      return {
        ...state,
        introspecting: true,
        schemaAnalyzing: action.analyzing ?? false,
        schemaPending: false,
      };
    case "INTROSPECT_SUCCESS":
      return {
        ...state,
        introspecting: false,
        schemaAnalyzing: false,
        schemaPending: false,
        mapping: action.mapping,
        schemaWarnings: action.warnings ?? [],
      };
    case "INTROSPECT_ERROR":
      return {
        ...state,
        introspecting: false,
        schemaAnalyzing: false,
        schemaPending: false,
        error: action.error,
      };
    case "INTROSPECT_PENDING":
      return {
        ...state,
        introspecting: false,
        schemaAnalyzing: false,
        schemaPending: true,
      };
    case "SCHEMA_WARNINGS_REPLACE":
      return { ...state, schemaWarnings: action.warnings };
    case "SCHEMA_WARNINGS_CLEAR":
      return { ...state, schemaWarnings: [] };
    case "TRANSLATE_START":
      return { ...state, translating: true, error: null, translateMs: null };
    case "TRANSLATE_SUCCESS":
      return {
        ...state,
        translating: false,
        aql: action.aql,
        bindVars: action.bindVars,
        warnings: action.warnings ?? state.warnings,
        // Preserve a previously measured transpile time when the
        // dispatching caller didn't supply a fresh one (e.g. Run /
        // Explain / Profile after a manual Translate). Without this
        // guard the transpile-time badge gets clobbered to null the
        // moment the user executes, hiding it behind the exec-time
        // badge.
        translateMs:
          action.translateMs !== undefined ? action.translateMs : state.translateMs,
        error: null,
      };
    case "TRANSLATE_ERROR":
      return { ...state, translating: false, error: action.error };
    case "EXECUTE_START":
      return { ...state, executing: true, error: null, execMs: null };
    case "EXECUTE_SUCCESS":
      return {
        ...state,
        executing: false,
        results: action.results,
        warnings: action.warnings ?? state.warnings,
        execMs: action.execMs ?? null,
        activeResultTab: "table",
        error: null,
      };
    case "EXECUTE_ERROR":
      return { ...state, executing: false, error: action.error };
    case "EXPLAIN_START":
      return { ...state, explaining: true, error: null };
    case "EXPLAIN_SUCCESS":
      return {
        ...state,
        explaining: false,
        explainPlan: action.plan,
        activeResultTab: "explain",
        error: null,
      };
    case "EXPLAIN_ERROR":
      return { ...state, explaining: false, error: action.error };
    case "PROFILE_START":
      return { ...state, profiling: true, error: null };
    case "PROFILE_SUCCESS":
      return {
        ...state,
        profiling: false,
        results: action.results,
        profileData: {
          statistics: action.statistics,
          profile: action.profile,
        },
        activeResultTab: "profile",
        error: null,
      };
    case "PROFILE_ERROR":
      return { ...state, profiling: false, error: action.error };
    case "SET_RESULT_TAB":
      return { ...state, activeResultTab: action.tab };
    case "CLEAR_ERROR":
      return { ...state, error: null };
    case "SET_PARAMS":
      return { ...state, params: action.params };
    case "ADD_HISTORY": {
      // Deduplicate by cypher (case-sensitive — whitespace was already
      // trimmed by the caller). When the new entry lacks a result
      // snapshot but the prior entry had one, *carry the snapshot
      // forward* — this is what makes the translate-after-execute
      // sequence non-destructive. Without this, hitting "Translate"
      // after a successful "Run" would silently wipe the cached rows.
      const existingIdx = state.history.findIndex(
        (h) => h.cypher === action.entry.cypher,
      );
      let merged = truncateEntrySnapshot(action.entry);
      if (existingIdx >= 0) {
        const prior = state.history[existingIdx];
        if (merged.results === undefined && prior.results !== undefined) {
          merged = {
            ...merged,
            results: prior.results,
            rowCount: prior.rowCount ?? prior.results.length,
            truncated: prior.truncated,
            execMs: merged.execMs ?? prior.execMs,
            bindVars: merged.bindVars ?? prior.bindVars,
            aql: merged.aql ?? prior.aql,
          };
        }
      }
      const updated =
        existingIdx >= 0
          ? [
              merged,
              ...state.history.slice(0, existingIdx),
              ...state.history.slice(existingIdx + 1),
            ]
          : [merged, ...state.history];
      return { ...state, history: updated.slice(0, MAX_HISTORY) };
    }
    case "CLEAR_HISTORY":
      return { ...state, history: [] };
    case "RESTORE_FROM_HISTORY": {
      // History replay is a deliberate user action — flip provenance
      // back to "user" so the WP-30 regenerate-from-NL button stays
      // hidden (we don't know if the original Cypher came from NL, and
      // ``lastNlQuestion`` is not stored on history entries).
      const entry = action.entry;
      const hasSnapshot = entry.results !== undefined;
      return {
        ...state,
        cypher: entry.cypher,
        aql: entry.aql ?? "",
        bindVars: entry.bindVars ?? {},
        results: hasSnapshot ? entry.results ?? null : null,
        // Only flip the active tab to "table" when we actually have
        // rows to show. Otherwise keep whatever the user was on so the
        // restore doesn't yank them away from an Explain plan they
        // were inspecting.
        activeResultTab: hasSnapshot ? "table" : state.activeResultTab,
        execMs: entry.execMs ?? null,
        // Restore clears any pending error and explain/profile state —
        // those weren't snapshotted and would be stale relative to the
        // restored Cypher.
        error: null,
        explainPlan: null,
        profileData: null,
        editorCypherSource: "user",
      };
    }
    case "SET_ACTIVE_STATEMENT":
      return { ...state, activeStatement: action.index };
    default:
      return state;
  }
}

// WP-30: re-export the pure reducer for unit tests. Kept as a
// named ``__reducerForTest`` alias so production imports explicitly
// opt in — the public entry point remains ``useAppState``. This
// lets ``store.test.ts`` exercise every action's state transition
// without a React tree or a mocked dispatcher.
export { reducer as __reducerForTest };

export function useAppState() {
  const [state, dispatch] = useReducer(reducer, initialState);

  const PERSIST_ACTIONS = new Set([
    "SET_CYPHER", "NL_SUCCESS", "SET_MAPPING", "SET_PARAMS",
    "ADD_HISTORY", "CLEAR_HISTORY", "RESTORE_FROM_HISTORY",
  ]);

  const persistAndDispatch = useCallback(
    (action: Action) => {
      dispatch(action);
      if (PERSIST_ACTIONS.has(action.type)) {
        const next = reducer(state, action);
        saveState(next);
      }
    },
    [state],
  );

  return [state, persistAndDispatch] as const;
}
