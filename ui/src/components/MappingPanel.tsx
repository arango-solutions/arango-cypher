import { useEffect, useRef, useState } from "react";
import { EditorState } from "@codemirror/state";
import { EditorView, lineNumbers, keymap } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { json } from "@codemirror/lang-json";
import { bracketMatching } from "@codemirror/language";
import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { oneDark } from "./theme";

interface Props {
  mapping: Record<string, unknown>;
  onChange: (mapping: Record<string, unknown>) => void;
}

const SAMPLE_MAPPING = {
  conceptual_schema: {
    entityTypes: ["Person"],
    relationshipTypes: ["KNOWS"],
  },
  physical_mapping: {
    entities: {
      Person: { style: "COLLECTION", collectionName: "persons" },
    },
    relationships: {
      KNOWS: {
        style: "DEDICATED_COLLECTION",
        edgeCollectionName: "knows",
      },
    },
  },
};

export default function MappingPanel({ mapping, onChange }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  const initial =
    Object.keys(mapping).length > 0
      ? JSON.stringify(mapping, null, 2)
      : JSON.stringify(SAMPLE_MAPPING, null, 2);

  useEffect(() => {
    if (!containerRef.current) return;

    const state = EditorState.create({
      doc: initial,
      extensions: [
        lineNumbers(),
        history(),
        bracketMatching(),
        closeBrackets(),
        json(),
        oneDark,
        keymap.of([
          ...closeBracketsKeymap,
          ...defaultKeymap,
          ...historyKeymap,
        ]),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            const text = update.state.doc.toString();
            try {
              const parsed = JSON.parse(text);
              setParseError(null);
              onChangeRef.current(parsed);
            } catch (e) {
              setParseError(
                e instanceof Error ? e.message : "Invalid JSON",
              );
            }
          }
        }),
        EditorView.theme({
          "&": { height: "100%" },
          ".cm-scroller": { overflow: "auto" },
        }),
      ],
    });

    const view = new EditorView({ state, parent: containerRef.current });
    viewRef.current = view;

    // Parse initial value
    try {
      const parsed = JSON.parse(initial);
      onChangeRef.current(parsed);
    } catch {
      // keep current
    }

    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-700 bg-gray-900/50">
        <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
          Mapping (JSON)
        </span>
        {parseError && (
          <span className="text-xs text-red-400 truncate ml-2 max-w-xs">
            {parseError}
          </span>
        )}
      </div>
      <div className="flex-1 min-h-0" ref={containerRef} />
    </div>
  );
}
