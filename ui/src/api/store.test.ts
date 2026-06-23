/**
 * WP-30 reducer state-machine tests.
 *
 * Scope: pin the ``editorCypherSource`` provenance field and
 * ``lastNlQuestion`` bookkeeping that the translate-error banner
 * uses to gate the "Regenerate from NL with error hint" action.
 *
 * These are pure-reducer tests — no React, no DOM. Vitest runs them
 * under ``npm run test`` (see package.json). Importing the private
 * ``reducer`` via its ``initialState + action → next state`` shape
 * keeps the test surface tight to the public action contract.
 */
import { describe, expect, it } from "vitest";

import {
  type Action,
  type AppState,
  type HistoryEntry,
  MAX_ROWS_PER_ENTRY,
  initialState,
  trimHistoryForPersistence,
  truncateEntrySnapshot,
} from "./store";

// Re-derive the reducer from ``useAppState``'s public observation:
// ``useReducer`` calls the reducer on every dispatch, so we can
// re-run it via a tiny driver. We import the reducer by re-exporting
// it from the module under test; vite's esbuild supports this fine.
// If the module ever stops exporting it, the build will fail loudly.
import { __reducerForTest as reducer } from "./store";

function apply(state: AppState, ...actions: Action[]): AppState {
  return actions.reduce(reducer, state);
}

describe("reducer: editorCypherSource state machine (WP-30)", () => {
  it("starts as null on a fresh state", () => {
    expect(initialState.editorCypherSource).toBeNull();
    expect(initialState.lastNlQuestion).toBeNull();
  });

  it("SET_CYPHER without source defaults to 'user'", () => {
    const next = apply(initialState, {
      type: "SET_CYPHER",
      cypher: "MATCH (n) RETURN n",
    });
    expect(next.editorCypherSource).toBe("user");
    expect(next.cypher).toBe("MATCH (n) RETURN n");
  });

  it("SET_CYPHER with source='user' is idempotent on provenance", () => {
    const next = apply(initialState, {
      type: "SET_CYPHER",
      cypher: "x",
      source: "user",
    });
    expect(next.editorCypherSource).toBe("user");
  });

  it("NL_SUCCESS flips provenance to 'nl_pipeline' and records the question", () => {
    const next = apply(initialState, {
      type: "NL_SUCCESS",
      cypher: "MATCH (p:Person) RETURN p",
      question: "find people",
    });
    expect(next.editorCypherSource).toBe("nl_pipeline");
    expect(next.lastNlQuestion).toBe("find people");
    expect(next.cypher).toBe("MATCH (p:Person) RETURN p");
  });

  it("user edit after NL_SUCCESS flips provenance back to 'user'", () => {
    const next = apply(
      initialState,
      {
        type: "NL_SUCCESS",
        cypher: "MATCH (p:Person) RETURN p",
        question: "find people",
      },
      { type: "SET_CYPHER", cypher: "MATCH (p:Person) RETURN p LIMIT 10" },
    );
    expect(next.editorCypherSource).toBe("user");
    // The NL question is preserved across user edits — the regenerate
    // button is gated on ``editorCypherSource`` alone, so retaining
    // ``lastNlQuestion`` after a user edit is harmless and avoids
    // losing it if the user edits-then-regenerates.
    expect(next.lastNlQuestion).toBe("find people");
  });

  it("repeated NL_SUCCESS overwrites the question with the latest one", () => {
    const next = apply(
      initialState,
      {
        type: "NL_SUCCESS",
        cypher: "MATCH (p:Person) RETURN p",
        question: "find people",
      },
      {
        type: "NL_SUCCESS",
        cypher: "MATCH (m:Movie) RETURN m",
        question: "find movies",
      },
    );
    expect(next.lastNlQuestion).toBe("find movies");
    expect(next.cypher).toBe("MATCH (m:Movie) RETURN m");
    expect(next.editorCypherSource).toBe("nl_pipeline");
  });

  it("DISCONNECT resets both provenance and last question", () => {
    const withNl = apply(initialState, {
      type: "NL_SUCCESS",
      cypher: "x",
      question: "q",
    });
    expect(withNl.editorCypherSource).toBe("nl_pipeline");

    const next = apply(withNl, { type: "DISCONNECT" });
    expect(next.editorCypherSource).toBeNull();
    expect(next.lastNlQuestion).toBeNull();
  });

  it("SET_CYPHER explicit source='nl_pipeline' flips provenance without needing NL_SUCCESS", () => {
    // Not the recommended path (use NL_SUCCESS) but the union allows
    // it and the reducer must honour it so the type is not a lie.
    const next = apply(initialState, {
      type: "SET_CYPHER",
      cypher: "x",
      source: "nl_pipeline",
    });
    expect(next.editorCypherSource).toBe("nl_pipeline");
    // lastNlQuestion is NOT set by SET_CYPHER — callers that want
    // the banner's regenerate button should use NL_SUCCESS instead.
    expect(next.lastNlQuestion).toBeNull();
  });
});

describe("reducer: WP-30 regenerate-button gating invariants", () => {
  it("fresh state has the regenerate button hidden (source=null)", () => {
    const canRegenerate =
      initialState.editorCypherSource === "nl_pipeline" &&
      initialState.lastNlQuestion !== null;
    expect(canRegenerate).toBe(false);
  });

  it("after NL_SUCCESS, regenerate is available", () => {
    const s = apply(initialState, {
      type: "NL_SUCCESS",
      cypher: "c",
      question: "q",
    });
    const canRegenerate =
      s.editorCypherSource === "nl_pipeline" && s.lastNlQuestion !== null;
    expect(canRegenerate).toBe(true);
  });

  it("after NL_SUCCESS + user edit, regenerate is hidden (source=user)", () => {
    const s = apply(
      initialState,
      { type: "NL_SUCCESS", cypher: "c", question: "q" },
      { type: "SET_CYPHER", cypher: "c2" },
    );
    const canRegenerate =
      (s.editorCypherSource as string) === "nl_pipeline" &&
      s.lastNlQuestion !== null;
    expect(canRegenerate).toBe(false);
  });

  it("DISCONNECT removes the affordance even mid-session", () => {
    const s = apply(
      initialState,
      { type: "NL_SUCCESS", cypher: "c", question: "q" },
      { type: "DISCONNECT" },
    );
    const canRegenerate =
      s.editorCypherSource === "nl_pipeline" && s.lastNlQuestion !== null;
    expect(canRegenerate).toBe(false);
  });
});

describe("reducer: history result snapshots (HRS)", () => {
  function entry(
    cypher: string,
    overrides: Partial<HistoryEntry> = {},
  ): HistoryEntry {
    return {
      cypher,
      timestamp: 1_700_000_000_000,
      aqlPreview: "FOR n IN ...",
      ...overrides,
    };
  }

  it("ADD_HISTORY stores a snapshot when one is supplied", () => {
    const e = entry("MATCH (p:Person) RETURN p", {
      aql: "FOR p IN Person RETURN p",
      bindVars: {},
      results: [{ name: "Alice" }, { name: "Bob" }],
      rowCount: 2,
      execMs: 12,
      connectionUrl: "https://prod.example",
      connectionDatabase: "addtech",
    });
    const next = apply(initialState, { type: "ADD_HISTORY", entry: e });
    expect(next.history).toHaveLength(1);
    expect(next.history[0].results).toEqual([{ name: "Alice" }, { name: "Bob" }]);
    expect(next.history[0].rowCount).toBe(2);
    expect(next.history[0].connectionDatabase).toBe("addtech");
  });

  it("ADD_HISTORY carries snapshot forward when re-adding the same Cypher without one", () => {
    // Simulates the translate-after-execute path: Run cached rows,
    // then the user hits Translate again (which dispatches ADD_HISTORY
    // without a ``results`` field). Without merge logic the second
    // call would wipe the cache.
    const withResults = entry("MATCH (p:Person) RETURN p", {
      aql: "FOR p IN Person RETURN p",
      results: [{ name: "Alice" }],
      rowCount: 1,
    });
    const withoutResults = entry("MATCH (p:Person) RETURN p", {
      aql: "FOR p IN Person RETURN p",
      timestamp: 1_700_000_001_000,
    });
    const next = apply(
      initialState,
      { type: "ADD_HISTORY", entry: withResults },
      { type: "ADD_HISTORY", entry: withoutResults },
    );
    expect(next.history).toHaveLength(1);
    expect(next.history[0].results).toEqual([{ name: "Alice" }]);
    expect(next.history[0].rowCount).toBe(1);
    expect(next.history[0].timestamp).toBe(1_700_000_001_000);
  });

  it("ADD_HISTORY replaces a stale snapshot with a fresh one", () => {
    const old = entry("MATCH (p) RETURN p", { results: [{ a: 1 }], rowCount: 1 });
    const fresh = entry("MATCH (p) RETURN p", {
      results: [{ a: 1 }, { a: 2 }],
      rowCount: 2,
      timestamp: 1_700_000_002_000,
    });
    const next = apply(
      initialState,
      { type: "ADD_HISTORY", entry: old },
      { type: "ADD_HISTORY", entry: fresh },
    );
    expect(next.history).toHaveLength(1);
    expect(next.history[0].results).toEqual([{ a: 1 }, { a: 2 }]);
    expect(next.history[0].rowCount).toBe(2);
  });

  it("truncateEntrySnapshot caps results at MAX_ROWS_PER_ENTRY and flags truncation", () => {
    const big = Array.from({ length: MAX_ROWS_PER_ENTRY + 50 }, (_, i) => ({ i }));
    const t = truncateEntrySnapshot(entry("c", { results: big }));
    expect(t.results).toHaveLength(MAX_ROWS_PER_ENTRY);
    expect(t.rowCount).toBe(MAX_ROWS_PER_ENTRY + 50);
    expect(t.truncated).toBe(true);
  });

  it("truncateEntrySnapshot leaves small snapshots intact (truncated=false)", () => {
    const small = [{ a: 1 }];
    const t = truncateEntrySnapshot(entry("c", { results: small }));
    expect(t.results).toBe(small);
    expect(t.rowCount).toBe(1);
    expect(t.truncated).toBe(false);
  });

  it("ADD_HISTORY auto-truncates oversized snapshots via truncateEntrySnapshot", () => {
    const big = Array.from({ length: MAX_ROWS_PER_ENTRY + 100 }, (_, i) => ({ i }));
    const next = apply(initialState, {
      type: "ADD_HISTORY",
      entry: entry("c", { results: big }),
    });
    expect(next.history[0].results).toHaveLength(MAX_ROWS_PER_ENTRY);
    expect(next.history[0].rowCount).toBe(MAX_ROWS_PER_ENTRY + 100);
    expect(next.history[0].truncated).toBe(true);
  });

  it("RESTORE_FROM_HISTORY repopulates cypher, aql, bindVars, and results", () => {
    const e = entry("MATCH (p:Person) RETURN p", {
      aql: "FOR p IN Person RETURN p",
      bindVars: { tenantId: "t1" },
      results: [{ name: "Alice" }],
      rowCount: 1,
      execMs: 42,
    });
    const next = apply(initialState, { type: "RESTORE_FROM_HISTORY", entry: e });
    expect(next.cypher).toBe("MATCH (p:Person) RETURN p");
    expect(next.aql).toBe("FOR p IN Person RETURN p");
    expect(next.bindVars).toEqual({ tenantId: "t1" });
    expect(next.results).toEqual([{ name: "Alice" }]);
    expect(next.activeResultTab).toBe("table");
    expect(next.execMs).toBe(42);
    // Restore flips provenance to "user" — we don't carry the NL
    // origin across history entries, so the WP-30 regenerate button
    // must stay hidden.
    expect(next.editorCypherSource).toBe("user");
  });

  it("RESTORE_FROM_HISTORY without a snapshot keeps the active tab and nulls results", () => {
    // Simulates restoring a translate-only entry (no Run yet) or one
    // whose snapshot was dropped to fit storage. The caller is on the
    // Explain tab; restore must not yank them away.
    const explainTabState: AppState = {
      ...initialState,
      activeResultTab: "explain",
      results: [{ stale: true }],
    };
    const e = entry("MATCH (p) RETURN p", {
      aql: "FOR p IN Person RETURN p",
      rowCount: 100,
    });
    const next = apply(explainTabState, { type: "RESTORE_FROM_HISTORY", entry: e });
    expect(next.cypher).toBe("MATCH (p) RETURN p");
    expect(next.results).toBeNull();
    expect(next.activeResultTab).toBe("explain");
  });

  it("RESTORE_FROM_HISTORY clears stale explain/profile/error state", () => {
    const dirtyState: AppState = {
      ...initialState,
      explainPlan: { foo: "bar" },
      profileData: { statistics: {}, profile: null },
      error: "stale error",
    };
    const e = entry("MATCH (p) RETURN p");
    const next = apply(dirtyState, { type: "RESTORE_FROM_HISTORY", entry: e });
    expect(next.explainPlan).toBeNull();
    expect(next.profileData).toBeNull();
    expect(next.error).toBeNull();
  });

  it("CLEAR_HISTORY wipes everything including snapshots", () => {
    const next = apply(
      initialState,
      {
        type: "ADD_HISTORY",
        entry: entry("c", { results: [{ a: 1 }], rowCount: 1 }),
      },
      { type: "CLEAR_HISTORY" },
    );
    expect(next.history).toHaveLength(0);
  });
});

describe("trimHistoryForPersistence: storage-quota defense (HRS)", () => {
  function bigEntry(idx: number, rowSize: number): HistoryEntry {
    // Pad each row with a long string so the serialized payload is
    // measurable in bytes, not just rows.
    const rows = Array.from({ length: rowSize }, (_, i) => ({
      idx,
      i,
      pad: "x".repeat(200),
    }));
    return {
      cypher: `MATCH (n${idx}) RETURN n${idx}`,
      timestamp: 1_700_000_000_000 + idx,
      aqlPreview: `FOR n IN c${idx}`,
      aql: `FOR n IN c${idx} RETURN n`,
      results: rows,
      rowCount: rowSize,
    };
  }

  it("returns the history unchanged when payload already fits", () => {
    const history = [bigEntry(0, 10)];
    const trimmed = trimHistoryForPersistence(initialState, history, 5_000_000);
    expect(trimmed).toHaveLength(1);
    expect(trimmed[0].results).toBeDefined();
  });

  it("drops the oldest snapshot first when payload exceeds the cap", () => {
    // 3 entries × 100 rows × ~200 bytes ≈ 60 KB+ — comfortably above
    // a 5 KB cap. The trimmer should drop snapshots starting from the
    // oldest (last in array, since newest is index 0).
    const history = [bigEntry(2, 100), bigEntry(1, 100), bigEntry(0, 100)];
    const trimmed = trimHistoryForPersistence(initialState, history, 5_000);
    expect(trimmed).toHaveLength(3);
    // Oldest had its snapshot dropped.
    expect(trimmed[2].results).toBeUndefined();
    expect(trimmed[2].rowCount).toBe(100);
    // Newest retains its snapshot for as long as the cap allows.
    // (Could be that ALL three were dropped if even one didn't fit;
    // assert the ordering invariant rather than which specific ones.)
    if (trimmed[0].results === undefined) {
      // All dropped — middle must also be dropped.
      expect(trimmed[1].results).toBeUndefined();
    }
  });

  it("preserves cypher + aqlPreview + rowCount on every entry even when snapshots are dropped", () => {
    const history = [bigEntry(2, 200), bigEntry(1, 200), bigEntry(0, 200)];
    const trimmed = trimHistoryForPersistence(initialState, history, 1_000);
    for (const e of trimmed) {
      expect(e.cypher).toBeTruthy();
      expect(e.aqlPreview).toBeTruthy();
      expect(e.rowCount).toBe(200);
    }
  });
});

describe("reducer: TRANSLATE_ERROR preserves provenance (WP-30)", () => {
  it("TRANSLATE_ERROR after NL_SUCCESS keeps source=nl_pipeline", () => {
    const s = apply(
      initialState,
      { type: "NL_SUCCESS", cypher: "c", question: "q" },
      { type: "TRANSLATE_START" },
      { type: "TRANSLATE_ERROR", error: "parse error at position 17" },
    );
    expect(s.editorCypherSource).toBe("nl_pipeline");
    expect(s.lastNlQuestion).toBe("q");
    expect(s.error).toBe("parse error at position 17");
    // The banner conditions on (error && source === "nl_pipeline"),
    // so this is the exact state where the regenerate button must
    // appear.
  });

  it("TRANSLATE_ERROR after user edit keeps source=user (no regenerate)", () => {
    const s = apply(
      initialState,
      { type: "NL_SUCCESS", cypher: "c", question: "q" },
      { type: "SET_CYPHER", cypher: "user typed this" },
      { type: "TRANSLATE_START" },
      { type: "TRANSLATE_ERROR", error: "parse error" },
    );
    expect(s.editorCypherSource).toBe("user");
  });
});

describe("reducer: CONNECT_START token lifecycle (db-switch 401 fix)", () => {
  function connected(): AppState {
    return apply(initialState, {
      type: "CONNECT_SUCCESS",
      token: "old-token",
      databases: ["a", "b"],
      url: "https://cluster",
      database: "a",
      username: "root",
      password: "pw",
    });
  }

  it("CONNECT_START drops the previous token so db-keyed effects bail", () => {
    // Switching to database "b" while connected to "a": the old token is
    // about to be invalidated. If the reducer kept it, the tenant/graph
    // effects (keyed on token+database) would re-fire against "b" with the
    // dead token and 401. Nulling the token makes them `if (!token) return`.
    const s = apply(connected(), {
      type: "CONNECT_START",
      url: "https://cluster",
      database: "b",
      username: "root",
    });
    expect(s.connection.status).toBe("connecting");
    expect(s.connection.token).toBeNull();
    expect(s.connection.database).toBe("b");
  });

  it("CONNECT_SUCCESS installs the fresh token after a switch", () => {
    const s = apply(
      connected(),
      { type: "CONNECT_START", url: "https://cluster", database: "b", username: "root" },
      {
        type: "CONNECT_SUCCESS",
        token: "new-token",
        databases: ["a", "b"],
        url: "https://cluster",
        database: "b",
        username: "root",
        password: "pw",
      },
    );
    expect(s.connection.status).toBe("connected");
    expect(s.connection.token).toBe("new-token");
    expect(s.connection.database).toBe("b");
  });

  it("CONNECT_ERROR after a switch leaves no stale token behind", () => {
    const s = apply(
      connected(),
      { type: "CONNECT_START", url: "https://cluster", database: "b", username: "root" },
      { type: "CONNECT_ERROR", error: "boom" },
    );
    expect(s.connection.status).toBe("disconnected");
    expect(s.connection.token).toBeNull();
    expect(s.connection.error).toBe("boom");
  });
});

describe("reducer: schema catalog pending/analyzing state", () => {
  it("fresh state is neither pending nor analyzing", () => {
    expect(initialState.schemaPending).toBe(false);
    expect(initialState.schemaAnalyzing).toBe(false);
  });

  it("INTROSPECT_START defaults analyzing=false (catalog read)", () => {
    const s = apply(initialState, { type: "INTROSPECT_START" });
    expect(s.introspecting).toBe(true);
    expect(s.schemaAnalyzing).toBe(false);
    expect(s.schemaPending).toBe(false);
  });

  it("INTROSPECT_START with analyzing=true flags a full analysis", () => {
    const s = apply(initialState, { type: "INTROSPECT_START", analyzing: true });
    expect(s.introspecting).toBe(true);
    expect(s.schemaAnalyzing).toBe(true);
  });

  it("INTROSPECT_PENDING surfaces a pending banner, not an error", () => {
    const s = apply(
      initialState,
      { type: "INTROSPECT_START" },
      { type: "INTROSPECT_PENDING" },
    );
    expect(s.introspecting).toBe(false);
    expect(s.schemaPending).toBe(true);
    expect(s.error).toBeNull();
  });

  it("INTROSPECT_SUCCESS clears pending + analyzing", () => {
    const s = apply(
      initialState,
      { type: "INTROSPECT_PENDING" },
      { type: "INTROSPECT_START", analyzing: true },
      { type: "INTROSPECT_SUCCESS", mapping: { entities: {} } },
    );
    expect(s.schemaPending).toBe(false);
    expect(s.schemaAnalyzing).toBe(false);
    expect(s.mapping).toEqual({ entities: {} });
  });

  it("INTROSPECT_START clears a prior pending flag (Check again / Analyze now)", () => {
    const s = apply(
      initialState,
      { type: "INTROSPECT_PENDING" },
      { type: "INTROSPECT_START" },
    );
    expect(s.schemaPending).toBe(false);
    expect(s.introspecting).toBe(true);
  });

  it("DISCONNECT clears pending so the banner doesn't linger", () => {
    const s = apply(
      initialState,
      { type: "INTROSPECT_PENDING" },
      { type: "DISCONNECT" },
    );
    expect(s.schemaPending).toBe(false);
  });
});
