import { useEffect, useMemo, useRef, useState } from "react";
import type { TenantRecord, TenantContext } from "../api/client";

// Tenants may carry an optional `docs` count (the denormalised
// discovery path reports how many documents each tenant has) so the
// picker can show which tenants actually hold data.
type TenantOption = TenantRecord & { docs?: number };

interface Props {
  tenants: TenantOption[];
  loading: boolean;
  selection: TenantContext | null;
  onSelect: (ctx: TenantContext | null) => void;
  // Diagnostic surface so a missing tenant catalog isn't silent.
  // `detected` is true when the backend found the resolved
  // collection in the connected database. `resolvedCollection` is
  // the actual ArangoDB collection name we tried to query.
  // `source` reports whether the collection name came from the
  // client-derived mapping or the literal-name heuristic.
  // `error` carries the last fetch error.
  detected?: boolean;
  resolvedCollection?: string | null;
  source?: "client" | "heuristic" | null;
  error?: string | null;
}

// Use the document `_key` as the canonical scope identifier.
//
// Why `_key` rather than a schema-specific column like
// TENANT_HEX_ID, NAME, or SUBDOMAIN:
//   * `_key` is guaranteed unique within the collection by ArangoDB
//     and is automatically indexed — no risk of ambiguous matches
//     and no full-collection scan in the generated AQL.
//   * It exists for every tenant document, in every multi-tenant
//     schema, regardless of whether the operator chose to also
//     model TENANT_HEX_ID / SUBDOMAIN. Earlier versions of this
//     selector defaulted to TENANT_HEX_ID, which produced queries
//     like `MATCH (t:Tenant {TENANT_HEX_ID: '40D89CC8'})` —
//     correct only for schemas that happen to have that field.
//   * In Cypher we get the `{_key: '...'}` shorthand for free; in
//     AQL it transpiles to `t._key == '...'` — the cheapest
//     possible tenant filter.
// NAME is still surfaced as the human-readable label.
function toContext(t: TenantOption): TenantContext {
  return {
    property: "_key",
    value: t.key,
    display: t.name || t.subdomain || t.key,
  };
}

export default function TenantSelector({
  tenants,
  loading,
  selection,
  onSelect,
  detected,
  resolvedCollection,
  source,
  error,
}: Props) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  useEffect(() => {
    if (open) {
      setFilter("");
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return tenants;
    return tenants.filter((t) => {
      const hay = `${t.name ?? ""} ${t.subdomain ?? ""} ${t.key}`.toLowerCase();
      return hay.includes(q);
    });
  }, [tenants, filter]);

  const empty = !loading && tenants.length === 0;
  const fallbackLabel = empty
    ? error
      ? "Tenant lookup failed"
      : detected === false
        ? "No tenant collection"
        : "No tenants"
    : "All tenants";
  const label = selection?.display || selection?.value || fallbackLabel;
  const active = selection != null;
  // Pill colour: amber when an active scope is set, red-ish when the
  // schema declares a Tenant entity but the backend couldn't load
  // any tenants (so the user knows the selector is non-functional),
  // muted gray otherwise.
  const variant = active
    ? "scoped"
    : empty
      ? "warn"
      : "idle";
  const pillClass =
    variant === "scoped"
      ? "bg-amber-900/30 text-amber-300 border-amber-700 hover:bg-amber-900/40"
      : variant === "warn"
        ? "bg-rose-900/20 text-rose-300 border-rose-800/70 hover:bg-rose-900/30"
        : "bg-gray-800 text-gray-400 border-gray-700 hover:text-gray-200";
  const tooltip = active
    ? `Queries scoped to tenant ${label}`
    : empty
      ? error
        ? `Tenant lookup failed: ${error}`
        : detected === false
          ? `No collection \`${resolvedCollection ?? "Tenant"}\` found in this database (resolved via ${source ?? "heuristic"})`
          : `Collection \`${resolvedCollection ?? "Tenant"}\` is empty`
      : "No tenant scope — queries run across all tenants";

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-1.5 px-2 py-0.5 text-xs rounded border transition-colors ${pillClass}`}
        title={tooltip}
      >
        <span className="text-[10px] text-gray-500 uppercase tracking-wide">Tenant</span>
        <span className="font-medium max-w-[160px] truncate">{label}</span>
        <span className="text-gray-500 text-[10px]">&#9662;</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 z-50 w-72 bg-gray-900 border border-gray-700 rounded shadow-2xl">
          <div className="p-2 border-b border-gray-800">
            <input
              ref={inputRef}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder={
                loading ? "Loading tenants…" : `Search ${tenants.length} tenants…`
              }
              className="w-full bg-gray-800 text-gray-200 text-xs rounded px-2 py-1 border border-gray-700 focus:border-indigo-500 focus:outline-none placeholder-gray-600"
            />
          </div>
          <div className="max-h-64 overflow-y-auto">
            <button
              onClick={() => {
                onSelect(null);
                setOpen(false);
              }}
              className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
                !active ? "bg-indigo-600/20 text-indigo-300" : "text-gray-300 hover:bg-gray-800"
              }`}
            >
              <div className="font-medium">All tenants</div>
              <div className="text-[10px] text-gray-500">
                No tenant scope — queries run across all tenants
              </div>
            </button>
            <div className="border-t border-gray-800" />
            {loading && tenants.length === 0 && (
              <div className="px-3 py-2 text-xs text-gray-500">Loading…</div>
            )}
            {!loading && tenants.length === 0 && (
              <div className="px-3 py-2 text-[11px] text-rose-300/90 leading-snug border-l-2 border-rose-700/60 bg-rose-950/20">
                {error ? (
                  <>
                    <div className="font-medium text-rose-200">Tenant lookup failed</div>
                    <div className="text-rose-300/80 break-words mt-0.5">{error}</div>
                  </>
                ) : detected === false ? (
                  <>
                    <div className="font-medium text-rose-200">No tenant collection</div>
                    <div className="text-rose-300/80 mt-0.5">
                      Looked for <code className="text-rose-200">{resolvedCollection ?? "Tenant"}</code>{" "}
                      via <span className="italic">{source ?? "heuristic"}</span> — not found in this database.
                    </div>
                  </>
                ) : (
                  <>
                    <div className="font-medium text-rose-200">No tenants</div>
                    <div className="text-rose-300/80 mt-0.5">
                      Collection <code className="text-rose-200">{resolvedCollection ?? "Tenant"}</code>{" "}
                      is empty.
                    </div>
                  </>
                )}
              </div>
            )}
            {!loading && tenants.length > 0 && filtered.length === 0 && (
              <div className="px-3 py-2 text-xs text-gray-500">No matches</div>
            )}
            {filtered.map((t) => {
              const ctx = toContext(t);
              const isSelected =
                selection != null &&
                selection.property === ctx.property &&
                selection.value === ctx.value;
              return (
                <button
                  key={t.key}
                  onClick={() => {
                    onSelect(ctx);
                    setOpen(false);
                  }}
                  className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
                    isSelected
                      ? "bg-amber-900/30 text-amber-300"
                      : "text-gray-300 hover:bg-gray-800"
                  }`}
                  title={t.id || (t.hex_id ? `TENANT_HEX_ID: ${t.hex_id}` : undefined)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium truncate">{t.name || t.key}</span>
                    {typeof t.docs === "number" && (
                      <span className="text-[10px] text-gray-500 shrink-0 tabular-nums">
                        {t.docs.toLocaleString()} docs
                      </span>
                    )}
                  </div>
                  {(t.subdomain || (t.name && t.key !== t.name)) && (
                    <div className="text-[10px] text-gray-500 truncate">
                      {t.subdomain || t.key}
                    </div>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
