import { useEffect, useRef, useState } from "react";
import {
  connect,
  disconnect,
  getConnectDefaults,
  introspectSchema,
  introspectToMapping,
  type ConnectDefaults,
} from "../api/client";
import type { Action, ConnectionState } from "../api/store";

interface Props {
  connection: ConnectionState;
  introspecting: boolean;
  dispatch: (action: Action) => void;
}

export default function ConnectionDialog({ connection, introspecting, dispatch }: Props) {
  const [form, setForm] = useState({
    url: connection.url,
    database: connection.database,
    username: connection.username,
    password: "",
  });
  const [open, setOpen] = useState(false);
  const autoConnectAttempted = useRef(false);

  useEffect(() => {
    if (connection.status === "disconnected") {
      setForm((f) => ({
        ...f,
        url: connection.url,
        database: connection.database,
        username: connection.username,
      }));
    }
  }, [connection.status, connection.url, connection.database, connection.username]);

  useEffect(() => {
    getConnectDefaults()
      .then((defaults: ConnectDefaults) => {
        const newForm = {
          url: defaults.url || form.url,
          database: form.database && form.database !== "_system" ? form.database : (defaults.database || form.database),
          username: defaults.username || form.username,
          password: defaults.password || form.password,
        };
        setForm(newForm);

        if (!autoConnectAttempted.current && defaults.password && connection.status === "disconnected") {
          autoConnectAttempted.current = true;
          doConnect(newForm);
        }
      })
      .catch((err) => {
        // Surface defaults-fetch failures in the console rather than
        // silently dropping them. When this fails the form silently falls
        // back to hardcoded localhost defaults, which is exactly the
        // failure mode that's hardest to diagnose from a screenshot.
        console.warn("Failed to load /connect/defaults:", err);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function doConnect(f: typeof form) {
    dispatch({
      type: "CONNECT_START",
      url: f.url,
      database: f.database,
      username: f.username,
    });
    try {
      const resp = await connect({
        url: f.url,
        database: f.database,
        username: f.username,
        password: f.password,
      });
      dispatch({
        type: "CONNECT_SUCCESS",
        token: resp.token,
        databases: resp.databases,
        url: f.url,
        database: f.database,
        username: f.username,
        password: f.password,
      });
      setOpen(false);

      dispatch({ type: "INTROSPECT_START" });
      try {
        const schema = await introspectSchema(resp.token, 50, true);
        const mapping = introspectToMapping(schema);
        dispatch({
          type: "INTROSPECT_SUCCESS",
          mapping,
          warnings: schema.warnings ?? [],
        });
      } catch (introspectErr) {
        console.warn("Schema introspection failed:", introspectErr);
        dispatch({ type: "INTROSPECT_ERROR", error: introspectErr instanceof Error ? introspectErr.message : "Introspection failed" });
      }
    } catch (err) {
      dispatch({
        type: "CONNECT_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  async function handleConnect() {
    await doConnect(form);
  }

  async function handleSwitchDb(newDb: string) {
    if (newDb === connection.database) return;

    if (connection.token) {
      try { await disconnect(connection.token); } catch { /* best-effort */ }
    }

    const f = { ...form, database: newDb };
    setForm(f);
    await doConnect(f);
  }

  async function handleDisconnect() {
    if (connection.token) {
      try { await disconnect(connection.token); } catch { /* best-effort */ }
    }
    dispatch({ type: "DISCONNECT" });
  }

  // Force a fresh schema introspection. Bypasses both cache tiers
  // (in-process + persistent `_schemas` collection) so the user has
  // an escape hatch when the cached mapping is missing entities or
  // collection-name changes — common after schema migrations or when
  // the analyzer LLM previously returned an incomplete mapping.
  async function handleReintrospect() {
    if (!connection.token) return;
    dispatch({ type: "INTROSPECT_START" });
    try {
      const schema = await introspectSchema(connection.token, 50, true);
      const mapping = introspectToMapping(schema);
      dispatch({
        type: "INTROSPECT_SUCCESS",
        mapping,
        warnings: schema.warnings ?? [],
      });
    } catch (err) {
      dispatch({
        type: "INTROSPECT_ERROR",
        error: err instanceof Error ? err.message : "Introspection failed",
      });
    }
  }

  if (connection.status === "connected") {
    return (
      <div className="flex items-center gap-3 text-sm">
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-2 h-2 rounded-full bg-emerald-400" />
          <span className="text-gray-400 text-xs truncate max-w-[200px]" title={connection.url}>
            {connection.url.replace(/^https?:\/\//, "")}/
          </span>
          {connection.databases.length > 1 ? (
            <select
              value={connection.database}
              onChange={(e) => handleSwitchDb(e.target.value)}
              className="bg-gray-800 border border-gray-600 text-gray-200 text-sm rounded px-1.5 py-0.5 focus:border-indigo-500 focus:outline-none cursor-pointer"
            >
              {connection.databases.map((db) => (
                <option key={db} value={db}>{db}</option>
              ))}
            </select>
          ) : (
            <span className="text-gray-300">{connection.database}</span>
          )}
        </span>
        {introspecting && (
          <span className="flex items-center gap-1.5 text-xs text-amber-400 animate-pulse">
            <svg className="w-3 h-3 animate-spin" viewBox="0 0 16 16" fill="none">
              <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="2" strokeDasharray="28" strokeDashoffset="8" strokeLinecap="round" />
            </svg>
            Loading schema...
          </span>
        )}
        <button
          onClick={handleReintrospect}
          disabled={introspecting}
          title="Bypass the schema cache and re-introspect the database"
          className="px-2.5 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Refresh schema
        </button>
        <button
          onClick={handleDisconnect}
          className="px-2.5 py-1 text-xs rounded bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
        >
          Disconnect
        </button>
      </div>
    );
  }

  if (!open && connection.status === "connecting") {
    return (
      <span className="text-sm text-gray-400">Connecting...</span>
    );
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="px-3 py-1.5 text-sm rounded bg-indigo-600 hover:bg-indigo-500 text-white transition-colors"
      >
        Connect to ArangoDB
      </button>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-gray-800 rounded-lg shadow-2xl p-6 w-full max-w-md border border-gray-700">
        <h2 className="text-lg font-semibold mb-4 text-white">
          Connect to ArangoDB
        </h2>

        {connection.error && (
          <div className="mb-4 p-3 rounded bg-red-900/50 border border-red-700 text-red-300 text-sm">
            {connection.error}
          </div>
        )}

        <div className="space-y-3">
          <label>
            <span className="text-xs text-gray-400 block mb-1">URL</span>
            <input
              value={form.url}
              onChange={(e) => setForm({ ...form, url: e.target.value })}
              placeholder="http://localhost:8529 or https://cloud.arangodb.com"
              className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-600 text-sm text-white focus:border-indigo-500 focus:outline-none"
            />
          </label>
          <label>
            <span className="text-xs text-gray-400 block mb-1">Database</span>
            <input
              value={form.database}
              onChange={(e) => setForm({ ...form, database: e.target.value })}
              className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-600 text-sm text-white focus:border-indigo-500 focus:outline-none"
            />
          </label>
          <label>
            <span className="text-xs text-gray-400 block mb-1">Username</span>
            <input
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-600 text-sm text-white focus:border-indigo-500 focus:outline-none"
            />
          </label>
          <label>
            <span className="text-xs text-gray-400 block mb-1">Password</span>
            <input
              type="password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && handleConnect()}
              className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-600 text-sm text-white focus:border-indigo-500 focus:outline-none"
            />
          </label>
        </div>

        <div className="flex justify-end gap-3 mt-6">
          <button
            onClick={() => setOpen(false)}
            className="px-4 py-2 text-sm rounded bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleConnect}
            disabled={connection.status === "connecting"}
            className="px-4 py-2 text-sm rounded bg-indigo-600 hover:bg-indigo-500 text-white transition-colors disabled:opacity-50"
          >
            {connection.status === "connecting" ? "Connecting..." : "Connect"}
          </button>
        </div>
      </div>
    </div>
  );
}
