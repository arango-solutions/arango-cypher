import { useEffect, useState } from "react";
import {
  connect,
  disconnect,
  getConnectDefaults,
  type ConnectDefaults,
} from "../api/client";
import type { Action, ConnectionState } from "../api/store";

interface Props {
  connection: ConnectionState;
  dispatch: (action: Action) => void;
}

export default function ConnectionDialog({ connection, dispatch }: Props) {
  const [form, setForm] = useState({
    host: connection.host,
    port: String(connection.port),
    database: connection.database,
    username: connection.username,
    password: "",
  });
  const [open, setOpen] = useState(false);

  useEffect(() => {
    getConnectDefaults()
      .then((defaults: ConnectDefaults) => {
        setForm((f) => ({
          ...f,
          host: defaults.host || f.host,
          port: String(defaults.port || f.port),
          database: defaults.database || f.database,
          username: defaults.username || f.username,
        }));
      })
      .catch(() => {});
  }, []);

  async function handleConnect() {
    dispatch({ type: "CONNECT_START" });
    try {
      const resp = await connect({
        host: form.host,
        port: Number(form.port),
        database: form.database,
        username: form.username,
        password: form.password,
      });
      dispatch({
        type: "CONNECT_SUCCESS",
        token: resp.token,
        databases: resp.databases,
        host: form.host,
        port: Number(form.port),
        database: form.database,
        username: form.username,
      });
      setOpen(false);
    } catch (err) {
      dispatch({
        type: "CONNECT_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  async function handleDisconnect() {
    if (connection.token) {
      try {
        await disconnect(connection.token);
      } catch {
        // best-effort
      }
    }
    dispatch({ type: "DISCONNECT" });
  }

  if (connection.status === "connected") {
    return (
      <div className="flex items-center gap-3 text-sm">
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-2 h-2 rounded-full bg-emerald-400" />
          <span className="text-gray-300">
            {connection.host}:{connection.port}/{connection.database}
          </span>
        </span>
        <button
          onClick={handleDisconnect}
          className="px-2.5 py-1 text-xs rounded bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
        >
          Disconnect
        </button>
      </div>
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
          <div className="flex gap-3">
            <label className="flex-1">
              <span className="text-xs text-gray-400 block mb-1">Host</span>
              <input
                value={form.host}
                onChange={(e) => setForm({ ...form, host: e.target.value })}
                className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-600 text-sm text-white focus:border-indigo-500 focus:outline-none"
              />
            </label>
            <label className="w-24">
              <span className="text-xs text-gray-400 block mb-1">Port</span>
              <input
                value={form.port}
                onChange={(e) => setForm({ ...form, port: e.target.value })}
                className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-600 text-sm text-white focus:border-indigo-500 focus:outline-none"
              />
            </label>
          </div>
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
