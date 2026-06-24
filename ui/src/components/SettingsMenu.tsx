import { useEffect, useRef, useState } from "react";

export interface SettingsMenuProps {
  showMapping: boolean;
  onToggleMapping: () => void;
  showOutline: boolean;
  onToggleOutline: () => void;
  onOpenSamples: () => void;
  onOpenHistory: () => void;
  historyCount: number;
  autoTranslate: boolean;
  onToggleAutoTranslate: () => void;
  autoRun: boolean;
  onToggleAutoRun: () => void;
  autoRunDisabled: boolean;
  autoOpenOnError: boolean;
  onToggleAutoOpenOnError: () => void;
  nlMode: "cypher" | "aql";
  onNlModeChange: (mode: "cypher" | "aql") => void;
}

function GearIcon() {
  return (
    <svg
      className="w-4 h-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

function ToggleRow({
  label,
  description,
  active,
  onClick,
  disabled,
}: {
  label: string;
  description?: string;
  active: boolean;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={active}
      onClick={onClick}
      disabled={disabled}
      className="w-full flex items-center justify-between gap-3 px-3 py-2 text-left hover:bg-gray-800/60 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
    >
      <span className="min-w-0">
        <span className="block text-xs text-gray-200">{label}</span>
        {description && (
          <span className="block text-[10px] text-gray-500">{description}</span>
        )}
      </span>
      <span
        className={`relative inline-flex h-4 w-7 shrink-0 items-center rounded-full transition-colors ${
          active ? "bg-indigo-600" : "bg-gray-700"
        }`}
      >
        <span
          className={`inline-block h-3 w-3 transform rounded-full bg-white transition-transform ${
            active ? "translate-x-3.5" : "translate-x-0.5"
          }`}
        />
      </span>
    </button>
  );
}

function ActionRow({
  label,
  badge,
  onClick,
}: {
  label: string;
  badge?: string | number;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full flex items-center justify-between gap-3 px-3 py-2 text-left hover:bg-gray-800/60 transition-colors"
    >
      <span className="text-xs text-gray-200">{label}</span>
      {badge !== undefined && badge !== "" && (
        <span className="text-[10px] text-gray-500 tabular-nums">{badge}</span>
      )}
    </button>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-3 pt-2.5 pb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-600">
      {children}
    </div>
  );
}

/**
 * Gear/settings popover (Query Workbench Shell, L2).
 *
 * Holds the workspace-panel triggers (Mapping, Outline, Samples, History) and
 * global behavior preferences (auto-translate, auto-run, NL output mode) so the
 * header and editor toolbar stay free of configuration clutter.
 */
export default function SettingsMenu(props: SettingsMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const runItem = (fn: () => void) => () => {
    fn();
    setOpen(false);
  };

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Settings"
        title="Settings"
        className={`flex items-center justify-center w-8 h-8 rounded transition-colors ${
          open
            ? "bg-indigo-600/20 text-indigo-400 border border-indigo-600/30"
            : "bg-gray-800 text-gray-400 hover:text-gray-200"
        }`}
      >
        <GearIcon />
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full mt-1 z-50 w-64 rounded-lg border border-gray-700 bg-gray-900 shadow-xl overflow-hidden"
        >
          <SectionLabel>Panels</SectionLabel>
          <ToggleRow
            label="Schema mapping"
            description="Entity/relationship mapping panel"
            active={props.showMapping}
            onClick={props.onToggleMapping}
          />
          <ToggleRow
            label="Clause outline"
            description="Jump to Cypher clauses"
            active={props.showOutline}
            onClick={props.onToggleOutline}
          />
          <ActionRow label="Sample queries" onClick={runItem(props.onOpenSamples)} />
          <ActionRow
            label="Query history"
            badge={props.historyCount > 0 ? props.historyCount : ""}
            onClick={runItem(props.onOpenHistory)}
          />

          <div className="my-1 border-t border-gray-800" />

          <SectionLabel>Behavior</SectionLabel>
          <ToggleRow
            label="Auto-translate"
            description="Translate after NL generates Cypher"
            active={props.autoTranslate}
            onClick={props.onToggleAutoTranslate}
          />
          <ToggleRow
            label="Auto-run"
            description={
              props.autoRunDisabled
                ? "Connect to enable"
                : "Run after a successful translate"
            }
            active={props.autoRun}
            onClick={props.onToggleAutoRun}
            disabled={props.autoRunDisabled}
          />
          <ToggleRow
            label="Open inspector on error"
            description="Reveal editors when a query fails"
            active={props.autoOpenOnError}
            onClick={props.onToggleAutoOpenOnError}
          />

          <div className="px-3 py-2">
            <div className="text-[10px] font-medium text-gray-500 mb-1">
              NL output mode
            </div>
            <div className="flex items-center rounded border border-gray-700 overflow-hidden">
              <button
                type="button"
                onClick={() => props.onNlModeChange("cypher")}
                className={`flex-1 px-2 py-1 text-[10px] font-medium transition-colors ${
                  props.nlMode === "cypher"
                    ? "bg-indigo-600 text-white"
                    : "bg-gray-800 text-gray-400 hover:text-gray-200"
                }`}
                title="NL → Cypher → AQL (two-stage)"
              >
                Cypher
              </button>
              <button
                type="button"
                onClick={() => props.onNlModeChange("aql")}
                className={`flex-1 px-2 py-1 text-[10px] font-medium transition-colors ${
                  props.nlMode === "aql"
                    ? "bg-amber-600 text-white"
                    : "bg-gray-800 text-gray-400 hover:text-gray-200"
                }`}
                title="NL → AQL (direct, requires LLM)"
              >
                AQL
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
