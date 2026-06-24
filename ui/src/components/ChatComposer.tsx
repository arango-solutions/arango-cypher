import { useEffect, useLayoutEffect, useRef, useState } from "react";

export interface ChatComposerProps {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  busy: boolean;
  placeholder?: string;
  suggestions?: string[];
  onPickSuggestion?: (s: string) => void;
  contextSlot?: React.ReactNode;
  statusSlot?: React.ReactNode;
}

function SendIcon() {
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
      <line x1="22" y1="2" x2="11" y2="13" />
      <polygon points="22 2 15 22 11 13 2 9 22 2" />
    </svg>
  );
}

const MAX_TEXTAREA_PX = 160;

/**
 * Agent-style chat composer (Query Workbench Shell, §3.1).
 *
 * Enter sends (runs the full pipeline); Shift+Enter inserts a newline. The
 * textarea auto-grows up to a cap. A status slot under the box surfaces pipeline
 * progress / result summaries; a context slot holds connection chips (tenant,
 * graph scope).
 */
export default function ChatComposer({
  value,
  onChange,
  onSend,
  busy,
  placeholder = "Ask a question about your data…",
  suggestions = [],
  onPickSuggestion,
  contextSlot,
  statusSlot,
}: ChatComposerProps) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const [suggestOpen, setSuggestOpen] = useState(false);

  // Auto-grow the textarea to fit content, capped so it never dominates.
  useLayoutEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, MAX_TEXTAREA_PX)}px`;
  }, [value]);

  useEffect(() => {
    if (!suggestOpen) return;
    const onPointer = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setSuggestOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointer);
    return () => document.removeEventListener("mousedown", onPointer);
  }, [suggestOpen]);

  const canSend = !busy && value.trim().length > 0;

  const send = () => {
    if (!canSend) return;
    setSuggestOpen(false);
    onSend();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="px-3 py-2 bg-gray-900/30 border-b border-gray-800">
      {contextSlot && (
        <div className="flex items-center flex-wrap gap-1.5 mb-1.5">{contextSlot}</div>
      )}
      <div className="relative" ref={rootRef}>
        <div className="flex items-end gap-2 rounded-lg border border-gray-700 bg-gray-800 focus-within:border-indigo-500 transition-colors px-2.5 py-1.5">
          <textarea
            ref={taRef}
            rows={1}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={onKeyDown}
            onFocus={() => {
              if (suggestions.length > 0) setSuggestOpen(true);
            }}
            placeholder={placeholder}
            aria-label="Ask a question about your data"
            className="flex-1 resize-none bg-transparent text-gray-200 text-sm leading-relaxed placeholder-gray-600 focus:outline-none py-0.5 max-h-40 overflow-y-auto"
          />
          <button
            type="button"
            onClick={send}
            disabled={!canSend}
            aria-label="Send"
            title="Send (Enter)"
            className="shrink-0 flex items-center justify-center w-8 h-8 rounded-md bg-violet-600 hover:bg-violet-500 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {busy ? (
              <span className="w-4 h-4 border-2 border-white/70 border-t-transparent rounded-full animate-spin" />
            ) : (
              <SendIcon />
            )}
          </button>
        </div>

        {suggestOpen && suggestions.length > 0 && (
          <div className="absolute left-0 right-0 top-full mt-0.5 z-50 bg-gray-800 border border-gray-700 rounded shadow-xl max-h-48 overflow-y-auto">
            {suggestions.map((q, i) => (
              <button
                key={i}
                type="button"
                className="w-full text-left px-2.5 py-1.5 text-xs text-gray-300 hover:bg-gray-700 hover:text-white truncate transition-colors"
                title={q}
                onClick={() => {
                  onPickSuggestion?.(q);
                  setSuggestOpen(false);
                }}
              >
                {q}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="flex items-center gap-2 mt-1 min-h-[16px] px-0.5">
        <span className="text-[10px] text-gray-600 shrink-0">
          Enter to send · Shift+Enter for newline
        </span>
        {statusSlot && <div className="flex-1 min-w-0 text-right">{statusSlot}</div>}
      </div>
    </div>
  );
}
