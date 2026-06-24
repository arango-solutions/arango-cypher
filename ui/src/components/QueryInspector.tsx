import { useEffect, useRef, useState } from "react";

export interface QueryInspectorProps {
  open: boolean;
  onToggle: () => void;

  // Power actions (relocated from the old editor toolbar).
  onTranslate: () => void;
  onRun: () => void;
  onExplain: () => void;
  onProfile: () => void;
  translating: boolean;
  executing: boolean;
  explaining: boolean;
  profiling: boolean;
  busy: boolean;
  isConnected: boolean;
  cypherEmpty: boolean;

  // Per-pane visibility is controlled by App so result-affordance chips can
  // focus a specific pane. At least one must remain open (enforced in App).
  cypherOpen: boolean;
  aqlOpen: boolean;
  onToggleCypher: () => void;
  onToggleAql: () => void;

  // Editor content (kept in App so all editor refs/state stay put).
  cypherPane: React.ReactNode;
  aqlPane: React.ReactNode;
}

const LS = {
  height: "qi_height",
  split: "qi_split",
};

const MIN_HEIGHT = 160;
const MAX_HEIGHT = 720;
const DEFAULT_HEIGHT = 320;
const MIN_RATIO = 0.2;
const MAX_RATIO = 0.8;

function loadNum(key: string, fallback: number): number {
  const raw = localStorage.getItem(key);
  const n = raw == null ? NaN : Number(raw);
  return Number.isFinite(n) ? n : fallback;
}

function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v));
}

/**
 * Collapsible "Query Inspector" drawer (Query Workbench Shell, L1).
 *
 * Closed by default. Holds the source|target editors (passed as slots) plus the
 * power actions. The Cypher|AQL divider is drag-movable, each side is
 * individually collapsible, and the drawer height is drag-resizable. All layout
 * choices persist to localStorage.
 */
export default function QueryInspector(props: QueryInspectorProps) {
  const [height, setHeight] = useState(() => loadNum(LS.height, DEFAULT_HEIGHT));
  const [ratio, setRatio] = useState(() => loadNum(LS.split, 0.5));
  const { cypherOpen, aqlOpen, onToggleCypher, onToggleAql } = props;

  const bodyRef = useRef<HTMLDivElement>(null);
  const heightDrag = useRef<{ startY: number; startH: number } | null>(null);
  const splitDrag = useRef<boolean>(false);

  useEffect(() => localStorage.setItem(LS.height, String(height)), [height]);
  useEffect(() => localStorage.setItem(LS.split, String(ratio)), [ratio]);

  // Drawer height drag (handle on the top edge).
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!heightDrag.current) return;
      const dy = heightDrag.current.startY - e.clientY;
      setHeight(clamp(heightDrag.current.startH + dy, MIN_HEIGHT, MAX_HEIGHT));
    };
    const onUp = () => {
      heightDrag.current = null;
      splitDrag.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    const onSplit = (e: MouseEvent) => {
      if (!splitDrag.current || !bodyRef.current) return;
      const rect = bodyRef.current.getBoundingClientRect();
      const r = (e.clientX - rect.left) / rect.width;
      setRatio(clamp(r, MIN_RATIO, MAX_RATIO));
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mousemove", onSplit);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mousemove", onSplit);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  if (!props.open) {
    return (
      <button
        type="button"
        onClick={props.onToggle}
        title="Show the Cypher / AQL inspector"
        className="w-full flex items-center gap-2 px-3 py-1.5 bg-gray-900/40 hover:bg-gray-800 border-t border-gray-800 text-gray-500 hover:text-gray-300 transition-colors"
      >
        <span className="text-xs leading-none">&#9650;</span>
        <span className="text-[11px] font-medium uppercase tracking-wide">
          Query Inspector
        </span>
        <span className="text-[10px] text-gray-600">Cypher · AQL</span>
      </button>
    );
  }

  const bothOpen = cypherOpen && aqlOpen;

  const actionBtn =
    "px-3 py-1 text-xs font-medium rounded transition-colors disabled:opacity-40 disabled:cursor-not-allowed";

  return (
    <div
      className="flex flex-col border-t border-gray-800 bg-gray-950 shrink-0"
      style={{ height }}
    >
      {/* Top resize handle */}
      <div
        className="h-1.5 cursor-row-resize hover:bg-indigo-500/30 active:bg-indigo-500/40 transition-colors"
        onMouseDown={(e) => {
          e.preventDefault();
          heightDrag.current = { startY: e.clientY, startH: height };
          document.body.style.cursor = "row-resize";
          document.body.style.userSelect = "none";
        }}
      />

      {/* Action toolbar */}
      <div className="flex items-center gap-2 px-3 py-1.5 bg-gray-900/50 border-b border-gray-800">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mr-1">
          Inspector
        </span>
        <button
          onClick={props.onTranslate}
          disabled={props.busy || props.cypherEmpty}
          className={`${actionBtn} bg-indigo-600 hover:bg-indigo-500 text-white`}
          title="Transpile Cypher → AQL (Ctrl/Cmd+Enter)"
        >
          {props.translating ? "Translating…" : "Translate"}
        </button>
        <button
          onClick={props.onRun}
          disabled={props.busy || !props.isConnected || props.cypherEmpty}
          className={`${actionBtn} bg-emerald-600 hover:bg-emerald-500 text-white`}
          title="Run (Shift+Enter)"
        >
          {props.executing ? "Running…" : "Run"}
        </button>
        <div className="w-px h-5 bg-gray-700" />
        <button
          onClick={props.onExplain}
          disabled={props.busy || !props.isConnected || props.cypherEmpty}
          className={`${actionBtn} bg-gray-700 hover:bg-gray-600 text-gray-200`}
          title="Explain (Ctrl/Cmd+Shift+E)"
        >
          {props.explaining ? "Explaining…" : "Explain"}
        </button>
        <button
          onClick={props.onProfile}
          disabled={props.busy || !props.isConnected || props.cypherEmpty}
          className={`${actionBtn} bg-gray-700 hover:bg-gray-600 text-gray-200`}
          title="Profile (Ctrl/Cmd+Shift+P)"
        >
          {props.profiling ? "Profiling…" : "Profile"}
        </button>
        {props.busy && (
          <div className="ml-1 w-4 h-4 border-2 border-indigo-400 border-t-transparent rounded-full animate-spin" />
        )}

        <div className="flex-1" />

        {/* Per-pane visibility */}
        <button
          onClick={onToggleCypher}
          className={`px-2 py-0.5 text-[10px] rounded transition-colors ${
            cypherOpen
              ? "bg-indigo-600/20 text-indigo-300 border border-indigo-600/30"
              : "bg-gray-800 text-gray-500 hover:text-gray-300"
          }`}
          title="Toggle the Cypher pane"
        >
          Cypher
        </button>
        <button
          onClick={onToggleAql}
          className={`px-2 py-0.5 text-[10px] rounded transition-colors ${
            aqlOpen
              ? "bg-indigo-600/20 text-indigo-300 border border-indigo-600/30"
              : "bg-gray-800 text-gray-500 hover:text-gray-300"
          }`}
          title="Toggle the AQL pane"
        >
          AQL
        </button>
        <button
          onClick={props.onToggle}
          aria-label="Hide inspector"
          title="Hide inspector"
          className="ml-1 px-1.5 py-0.5 text-xs rounded text-gray-500 hover:text-gray-200 hover:bg-gray-800 transition-colors"
        >
          &#10005;
        </button>
      </div>

      {/* Editor split body */}
      <div ref={bodyRef} className="flex-1 min-h-0 flex">
        {cypherOpen && (
          <div
            className="flex flex-col min-h-0 min-w-0 overflow-hidden"
            style={bothOpen ? { width: `${ratio * 100}%` } : { flex: 1 }}
          >
            {props.cypherPane}
          </div>
        )}

        {bothOpen && (
          <div
            className="w-1.5 shrink-0 cursor-col-resize bg-gray-800 hover:bg-indigo-500/30 active:bg-indigo-500/40 transition-colors"
            onMouseDown={(e) => {
              e.preventDefault();
              splitDrag.current = true;
              document.body.style.cursor = "col-resize";
              document.body.style.userSelect = "none";
            }}
          />
        )}

        {aqlOpen && (
          <div className="flex flex-col min-h-0 min-w-0 overflow-hidden flex-1">
            {props.aqlPane}
          </div>
        )}
      </div>
    </div>
  );
}
