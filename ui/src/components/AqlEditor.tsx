import { useEffect, useRef, useState } from "react";
import { EditorState } from "@codemirror/state";
import { EditorView, lineNumbers, highlightActiveLine, highlightActiveLineGutter } from "@codemirror/view";
import { bracketMatching } from "@codemirror/language";
import { highlightSelectionMatches } from "@codemirror/search";
import { oneDark } from "./theme";
import { aql } from "../lang/aql";

interface Props {
  value: string;
  bindVars: Record<string, unknown>;
  error: string | null;
}

export default function AqlEditor({ value, bindVars, error }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const [showBindVars, setShowBindVars] = useState(false);

  useEffect(() => {
    if (!containerRef.current) return;

    const state = EditorState.create({
      doc: value,
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightActiveLine(),
        bracketMatching(),
        highlightSelectionMatches(),
        aql(),
        oneDark,
        EditorState.readOnly.of(true),
        EditorView.theme({
          "&": { height: "100%" },
          ".cm-scroller": { overflow: "auto" },
        }),
      ],
    });

    const view = new EditorView({ state, parent: containerRef.current });
    viewRef.current = view;

    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, []);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (current !== value) {
      view.dispatch({
        changes: { from: 0, to: current.length, insert: value },
      });
    }
  }, [value]);

  const hasBindVars = Object.keys(bindVars).length > 0;

  return (
    <div className="h-full flex flex-col">
      {error ? (
        <div className="flex-1 flex items-center justify-center p-4">
          <div className="p-4 rounded bg-red-900/30 border border-red-800 text-red-300 text-sm max-w-full overflow-auto">
            {error}
          </div>
        </div>
      ) : (
        <div className="flex-1 min-h-0" ref={containerRef} />
      )}

      {hasBindVars && (
        <div className="border-t border-gray-700">
          <button
            onClick={() => setShowBindVars(!showBindVars)}
            className="w-full px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 flex items-center gap-1.5 transition-colors"
          >
            <span className={`transition-transform ${showBindVars ? "rotate-90" : ""}`}>
              &#9654;
            </span>
            Bind Variables ({Object.keys(bindVars).length})
          </button>
          {showBindVars && (
            <pre className="px-3 pb-2 text-xs text-gray-300 overflow-auto max-h-32 font-mono">
              {JSON.stringify(bindVars, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
