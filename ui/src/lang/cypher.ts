import {
  LanguageSupport,
  StreamLanguage,
  type StreamParser,
} from "@codemirror/language";

const cypherKeywords = new Set([
  "MATCH", "OPTIONAL", "WHERE", "RETURN", "WITH", "ORDER", "BY", "SKIP",
  "LIMIT", "CREATE", "MERGE", "SET", "DELETE", "DETACH", "REMOVE", "CALL",
  "YIELD", "UNION", "ALL", "UNWIND", "AS", "DISTINCT", "CASE", "WHEN",
  "THEN", "ELSE", "END", "AND", "OR", "NOT", "XOR", "IN", "IS", "NULL",
  "TRUE", "FALSE", "STARTS", "ENDS", "CONTAINS", "EXISTS",
]);

const cypherFunctions = new Set([
  "count", "sum", "avg", "min", "max", "collect", "size", "length",
  "toLower", "toUpper", "toString", "toInteger", "toFloat", "toBoolean",
  "coalesce", "head", "tail", "last", "type", "id", "labels", "keys",
  "properties", "nodes", "relationships", "range", "abs", "ceil", "floor",
  "round", "sign", "rand", "replace", "substring", "trim", "ltrim", "rtrim",
  "split", "reverse", "left", "right",
]);

const parser: StreamParser<{ inString: string | null; inComment: boolean }> = {
  startState() {
    return { inString: null, inComment: false };
  },

  token(stream, state) {
    if (state.inComment) {
      if (stream.match("*/")) {
        state.inComment = false;
        return "blockComment";
      }
      stream.next();
      return "blockComment";
    }

    if (state.inString) {
      const quote = state.inString;
      while (!stream.eol()) {
        const ch = stream.next();
        if (ch === "\\") {
          stream.next();
        } else if (ch === quote) {
          state.inString = null;
          return "string";
        }
      }
      return "string";
    }

    if (stream.match("//")) {
      stream.skipToEnd();
      return "lineComment";
    }

    if (stream.match("/*")) {
      state.inComment = true;
      return "blockComment";
    }

    if (stream.match(/^"/) || stream.match(/^'/)) {
      state.inString = stream.current();
      return "string";
    }

    // Parameters
    if (stream.match(/^\$[a-zA-Z_]\w*/)) {
      return "variableName.special";
    }

    // Numbers
    if (stream.match(/^-?\d+(\.\d+)?([eE][+-]?\d+)?/)) {
      return "number";
    }

    // Labels and relationship types after colon
    if (stream.match(/^:[A-Z_]\w*/i)) {
      return "typeName";
    }

    // arango.* namespace
    if (stream.match(/^arango\.\w+/)) {
      return "function";
    }

    // Identifiers and keywords
    if (stream.match(/^[a-zA-Z_]\w*/)) {
      const word = stream.current();
      const upper = word.toUpperCase();
      if (cypherKeywords.has(upper)) return "keyword";
      if (cypherFunctions.has(word.toLowerCase())) return "function";
      return "variableName";
    }

    // Operators
    if (stream.match(/^[<>=!]+/) || stream.match(/^[-+*/%^]/)) {
      return "operator";
    }

    // Brackets
    if (stream.match(/^[()[\]{}]/)) {
      return "bracket";
    }

    // Punctuation
    if (stream.match(/^[,;.]/)) {
      return "punctuation";
    }

    stream.next();
    return null;
  },
};

export function cypher() {
  return new LanguageSupport(StreamLanguage.define(parser));
}
