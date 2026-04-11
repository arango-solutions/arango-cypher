import {
  LanguageSupport,
  StreamLanguage,
  type StreamParser,
} from "@codemirror/language";

const aqlKeywords = new Set([
  "FOR", "IN", "FILTER", "RETURN", "LET", "SORT", "LIMIT", "COLLECT",
  "INTO", "WITH", "COUNT", "INSERT", "UPDATE", "REPLACE", "REMOVE",
  "UPSERT", "GRAPH", "OUTBOUND", "INBOUND", "ANY", "ALL", "NONE",
  "SHORTEST_PATH", "K_SHORTEST_PATHS", "PRUNE", "SEARCH", "OPTIONS",
  "AGGREGATE", "LIKE", "NOT", "AND", "OR", "TRUE", "FALSE", "NULL",
  "DISTINCT", "ASC", "DESC", "WINDOW",
]);

const aqlFunctions = new Set([
  "LENGTH", "COUNT", "SUM", "MIN", "MAX", "AVG", "MEDIAN", "STDDEV",
  "VARIANCE", "UNIQUE", "SORTED_UNIQUE", "FIRST", "LAST", "NTH",
  "PUSH", "POP", "APPEND", "UNSHIFT", "SHIFT", "SLICE", "REVERSE",
  "FLATTEN", "MERGE", "UNSET", "KEEP", "ATTRIBUTES", "VALUES", "ZIP",
  "HAS", "DOCUMENT", "PARSE_IDENTIFIER", "IS_NULL", "IS_STRING",
  "IS_NUMBER", "IS_BOOL", "IS_ARRAY", "IS_OBJECT", "TO_NUMBER",
  "TO_STRING", "TO_BOOL", "TO_ARRAY", "CONCAT", "CONCAT_SEPARATOR",
  "LIKE", "CONTAINS", "UPPER", "LOWER", "TRIM", "LTRIM", "RTRIM",
  "SPLIT", "SUBSTITUTE", "SUBSTRING", "LEFT", "RIGHT", "REGEX_TEST",
  "DATE_NOW", "DATE_ISO8601", "DATE_TIMESTAMP", "RAND", "RANGE",
  "UNION", "UNION_DISTINCT", "INTERSECTION", "MINUS", "BM25", "TFIDF",
  "ANALYZER", "TOKENS", "PHRASE", "STARTS_WITH", "EXISTS", "BOOST",
  "GEO_POINT", "GEO_DISTANCE", "GEO_CONTAINS", "GEO_INTERSECTS",
  "FULLTEXT", "NEAR", "WITHIN", "WITHIN_RECTANGLE", "COSINE_SIMILARITY",
  "L2_DISTANCE", "TO_ARRAY", "VALUE",
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

    // Bind parameters: @@collection or @param
    if (stream.match(/^@@[a-zA-Z_]\w*/)) {
      return "variableName.special";
    }
    if (stream.match(/^@[a-zA-Z_]\w*/)) {
      return "variableName.special";
    }

    // Numbers
    if (stream.match(/^-?\d+(\.\d+)?([eE][+-]?\d+)?/)) {
      return "number";
    }

    // Identifiers and keywords
    if (stream.match(/^[a-zA-Z_]\w*/)) {
      const word = stream.current();
      const upper = word.toUpperCase();
      if (aqlKeywords.has(upper)) return "keyword";
      if (aqlFunctions.has(upper)) return "function";
      return "variableName";
    }

    // Operators
    if (stream.match(/^[<>=!]+/) || stream.match(/^[-+*/%]/)) {
      return "operator";
    }

    // Brackets
    if (stream.match(/^[()[\]{}]/)) {
      return "bracket";
    }

    // Punctuation
    if (stream.match(/^[,;.:]/)) {
      return "punctuation";
    }

    stream.next();
    return null;
  },
};

export function aql() {
  return new LanguageSupport(StreamLanguage.define(parser));
}
