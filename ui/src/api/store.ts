import { useCallback, useReducer } from "react";

export interface ConnectionState {
  status: "disconnected" | "connecting" | "connected";
  token: string | null;
  host: string;
  port: number;
  database: string;
  username: string;
  databases: string[];
  error: string | null;
}

export type ResultTab = "table" | "json" | "explain" | "profile";

export interface AppState {
  connection: ConnectionState;
  cypher: string;
  mapping: Record<string, unknown>;
  aql: string;
  bindVars: Record<string, unknown>;
  results: unknown[] | null;
  explainPlan: unknown | null;
  profileData: { statistics: Record<string, unknown>; profile: unknown } | null;
  activeResultTab: ResultTab;
  error: string | null;
  translating: boolean;
  executing: boolean;
  explaining: boolean;
  profiling: boolean;
}

const STORAGE_KEY = "cypher-workbench";

function loadSavedState(): Partial<AppState> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const saved = JSON.parse(raw);
    return {
      cypher: saved.cypher ?? "",
      mapping: saved.mapping ?? {},
    };
  } catch {
    return {};
  }
}

function saveState(state: AppState) {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ cypher: state.cypher, mapping: state.mapping }),
    );
  } catch {
    // localStorage may be unavailable
  }
}

export const initialState: AppState = {
  connection: {
    status: "disconnected",
    token: null,
    host: "localhost",
    port: 8529,
    database: "_system",
    username: "root",
    databases: [],
    error: null,
  },
  cypher: "",
  mapping: {},
  aql: "",
  bindVars: {},
  results: null,
  explainPlan: null,
  profileData: null,
  activeResultTab: "table",
  error: null,
  translating: false,
  executing: false,
  explaining: false,
  profiling: false,
  ...loadSavedState(),
};

export type Action =
  | { type: "SET_CYPHER"; cypher: string }
  | { type: "SET_MAPPING"; mapping: Record<string, unknown> }
  | { type: "SET_MAPPING_JSON"; json: string }
  | { type: "CONNECT_START" }
  | {
      type: "CONNECT_SUCCESS";
      token: string;
      databases: string[];
      host: string;
      port: number;
      database: string;
      username: string;
    }
  | { type: "CONNECT_ERROR"; error: string }
  | { type: "DISCONNECT" }
  | { type: "TRANSLATE_START" }
  | {
      type: "TRANSLATE_SUCCESS";
      aql: string;
      bindVars: Record<string, unknown>;
    }
  | { type: "TRANSLATE_ERROR"; error: string }
  | { type: "EXECUTE_START" }
  | { type: "EXECUTE_SUCCESS"; results: unknown[] }
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
  | { type: "CLEAR_ERROR" };

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "SET_CYPHER":
      return { ...state, cypher: action.cypher };
    case "SET_MAPPING":
      return { ...state, mapping: action.mapping };
    case "SET_MAPPING_JSON":
      try {
        return { ...state, mapping: JSON.parse(action.json) };
      } catch {
        return state;
      }
    case "CONNECT_START":
      return {
        ...state,
        connection: { ...state.connection, status: "connecting", error: null },
      };
    case "CONNECT_SUCCESS":
      return {
        ...state,
        connection: {
          status: "connected",
          token: action.token,
          host: action.host,
          port: action.port,
          database: action.database,
          username: action.username,
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
        connection: { ...initialState.connection },
        results: null,
        explainPlan: null,
        profileData: null,
      };
    case "TRANSLATE_START":
      return { ...state, translating: true, error: null };
    case "TRANSLATE_SUCCESS":
      return {
        ...state,
        translating: false,
        aql: action.aql,
        bindVars: action.bindVars,
        error: null,
      };
    case "TRANSLATE_ERROR":
      return { ...state, translating: false, error: action.error };
    case "EXECUTE_START":
      return { ...state, executing: true, error: null };
    case "EXECUTE_SUCCESS":
      return {
        ...state,
        executing: false,
        results: action.results,
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
    default:
      return state;
  }
}

export function useAppState() {
  const [state, dispatch] = useReducer(reducer, initialState);

  const persistAndDispatch = useCallback(
    (action: Action) => {
      dispatch(action);
      if (action.type === "SET_CYPHER" || action.type === "SET_MAPPING") {
        const next = reducer(state, action);
        saveState(next);
      }
    },
    [state],
  );

  return [state, persistAndDispatch] as const;
}
