import { useEffect, useRef } from "react";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { bracketMatching, foldGutter, foldKeymap } from "@codemirror/language";
import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { oneDark } from "./theme";
import { cypher } from "../lang/cypher";

interface Props {
  value: string;
  onChange: (value: string) => void;
  onTranslate: () => void;
  onExecute: () => void;
  onExplain: () => void;
  onProfile: () => void;
}

export default function CypherEditor({
  value,
  onChange,
  onTranslate,
  onExecute,
  onExplain,
  onProfile,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const callbacksRef = useRef({ onChange, onTranslate, onExecute, onExplain, onProfile });
  callbacksRef.current = { onChange, onTranslate, onExecute, onExplain, onProfile };

  useEffect(() => {
    if (!containerRef.current) return;

    const workbenchKeymap = keymap.of([
      {
        key: "Mod-Enter",
        run: () => { callbacksRef.current.onTranslate(); return true; },
      },
      {
        key: "Shift-Enter",
        run: () => { callbacksRef.current.onExecute(); return true; },
      },
      {
        key: "Mod-Shift-e",
        run: () => { callbacksRef.current.onExplain(); return true; },
      },
      {
        key: "Mod-Shift-p",
        run: () => { callbacksRef.current.onProfile(); return true; },
      },
    ]);

    const state = EditorState.create({
      doc: value,
      extensions: [
        workbenchKeymap,
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightActiveLine(),
        history(),
        foldGutter(),
        bracketMatching(),
        closeBrackets(),
        highlightSelectionMatches(),
        cypher(),
        oneDark,
        keymap.of([
          ...closeBracketsKeymap,
          ...defaultKeymap,
          ...searchKeymap,
          ...historyKeymap,
          ...foldKeymap,
        ]),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            callbacksRef.current.onChange(update.state.doc.toString());
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

    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync external value changes (e.g. loading from history)
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

  return <div ref={containerRef} className="h-full" />;
}
