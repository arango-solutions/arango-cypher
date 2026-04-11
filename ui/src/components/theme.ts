import { EditorView } from "@codemirror/view";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";

const darkColors = {
  bg: "#111827",
  fg: "#e5e7eb",
  keyword: "#c084fc",
  string: "#86efac",
  number: "#fbbf24",
  function: "#60a5fa",
  variable: "#e5e7eb",
  variableSpecial: "#f472b6",
  type: "#2dd4bf",
  comment: "#6b7280",
  operator: "#94a3b8",
  bracket: "#d1d5db",
  punctuation: "#9ca3af",
};

const highlightStyle = HighlightStyle.define([
  { tag: t.keyword, color: darkColors.keyword, fontWeight: "bold" },
  { tag: t.string, color: darkColors.string },
  { tag: t.number, color: darkColors.number },
  { tag: [t.function(t.variableName), t.function(t.name)], color: darkColors.function },
  { tag: t.variableName, color: darkColors.variable },
  { tag: t.special(t.variableName), color: darkColors.variableSpecial, fontWeight: "bold" },
  { tag: t.typeName, color: darkColors.type, fontWeight: "bold" },
  { tag: [t.lineComment, t.blockComment], color: darkColors.comment, fontStyle: "italic" },
  { tag: t.operator, color: darkColors.operator },
  { tag: t.bracket, color: darkColors.bracket },
  { tag: t.punctuation, color: darkColors.punctuation },
]);

const baseTheme = EditorView.theme(
  {
    "&": {
      color: darkColors.fg,
      backgroundColor: darkColors.bg,
    },
    ".cm-content": {
      caretColor: "#e5e7eb",
    },
    ".cm-cursor, .cm-dropCursor": {
      borderLeftColor: "#e5e7eb",
    },
    "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
      {
        backgroundColor: "rgba(99, 102, 241, 0.25)",
      },
    ".cm-panels": {
      backgroundColor: "#1f2937",
      color: "#e5e7eb",
    },
    ".cm-panels.cm-panels-top": {
      borderBottom: "1px solid #374151",
    },
    ".cm-panels.cm-panels-bottom": {
      borderTop: "1px solid #374151",
    },
    ".cm-searchMatch": {
      backgroundColor: "rgba(250, 204, 21, 0.2)",
      outline: "1px solid rgba(250, 204, 21, 0.4)",
    },
    ".cm-searchMatch.cm-searchMatch-selected": {
      backgroundColor: "rgba(250, 204, 21, 0.4)",
    },
    ".cm-activeLine": {
      backgroundColor: "rgba(99, 102, 241, 0.06)",
    },
    ".cm-selectionMatch": {
      backgroundColor: "rgba(99, 102, 241, 0.15)",
    },
    ".cm-matchingBracket, .cm-nonmatchingBracket": {
      backgroundColor: "rgba(99, 102, 241, 0.3)",
      outline: "1px solid rgba(99, 102, 241, 0.5)",
    },
    ".cm-gutters": {
      backgroundColor: "#111827",
      color: "#6b7280",
      border: "none",
      borderRight: "1px solid #374151",
    },
    ".cm-activeLineGutter": {
      backgroundColor: "rgba(99, 102, 241, 0.1)",
    },
    ".cm-foldPlaceholder": {
      backgroundColor: "transparent",
      border: "none",
      color: "#6b7280",
    },
    ".cm-tooltip": {
      border: "1px solid #374151",
      backgroundColor: "#1f2937",
      color: "#e5e7eb",
    },
    ".cm-tooltip .cm-tooltip-arrow:before": {
      borderTopColor: "transparent",
      borderBottomColor: "transparent",
    },
    ".cm-tooltip .cm-tooltip-arrow:after": {
      borderTopColor: "#1f2937",
      borderBottomColor: "#1f2937",
    },
    ".cm-tooltip-autocomplete": {
      "& > ul > li[aria-selected]": {
        backgroundColor: "rgba(99, 102, 241, 0.2)",
        color: "#e5e7eb",
      },
    },
  },
  { dark: true },
);

export const oneDark = [baseTheme, syntaxHighlighting(highlightStyle)];
