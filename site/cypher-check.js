// The read-only check for Cypher typed into the demo (spec 13a): a line-by-line port of
// wikipulse/qa/cypher.py's validate_cypher(), so the browser refuses exactly what
// WikiPulse refuses. Both implementations pass the same cases,
// tests/fixtures/cypher_cases.json (tests/qa/test_cypher_cases.py runs the Python one,
// tests/test_demo_graph_js.py this one, with Node).
//
// The checks work on the query with its strings, comments and `quoted` names blanked
// out, so `WHERE n.name = 'Set'` is fine while a real SET clause is not. They refuse
// write and admin clauses, CALL, several statements, and paths without an upper bound
// of at most MAX_HOPS; and they force a final LIMIT on every branch of a UNION.
//
// Also here: bindTimes() (the $now, $today_start, $week_start parameters, which JSON
// can't carry as datetimes) and luceneTerms() (the search box's full-text query).

export const MAX_QUERY_CHARS = 3000;
export const MAX_HOPS = 6;
export const FORBIDDEN_WORDS = [
  "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "FOREACH", "LOAD",
  "CALL", "USE", "ALTER", "GRANT", "DENY", "REVOKE", "SHOW", "TERMINATE", "START", "STOP",
  "ENABLE", "RENAME",
];
export const READ_STARTS = ["MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN"];

// Python's \w on text is a letter, a number or "_" from any script.
const COMMENT_OR_TEXT = () =>
  /\/\/[^\n]*|\/\*[\s\S]*?\*\/|'(?:\\[\s\S]|[^'\\])*'|"(?:\\[\s\S]|[^"\\])*"|`[^`]*`/g;
const WORD = () => /(?<![.\p{L}\p{N}_$])([A-Za-z]+)(?![\p{L}\p{N}_])/gu;
const REL_PATTERN = () => /-\s*\[([^\]]*)\]/g;
const HOPS = /\*\s*(\d*)\s*(\.\.\s*(\d*))?/;
const QUANTIFIER = () => /[-)>]\s*(\{\s*(\d*)\s*(,\s*(\d*))?\s*\}|\+|\*)/g;
const RELATIONSHIP = /-\s*-|-\s*\[|\]\s*-|-\s*>|<\s*-/;
const LIMIT = () => /\bLIMIT\b/gi;
const UNION = () => /\bUNION(\s+ALL)?\b/gi;
const CLAUSE = /\b(MATCH|OPTIONAL|WITH|UNWIND|RETURN|WHERE|ORDER|SKIP|OFFSET|UNION)\b/i;
const FENCE = /^```[a-zA-Z]*\s*|\s*```$/g;
// Characters Neo4j's parser also takes as the "-", "<" and ">" of a relationship.
const ARROW_CHARS = new Map([
  ..."\u00ad\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d".split("").map((c) => [c, "-"]),
  ..."\u27e8\u3008\ufe64\uff1c".split("").map((c) => [c, "<"]),
  ..."\u27e9\u3009\ufe65\uff1e".split("").map((c) => [c, ">"]),
]);

export class UnsafeCypherError extends Error {
  constructor(message) {
    super(message);
    this.name = "UnsafeCypherError";
  }
}

// The query with comments removed and strings and `quoted` names emptied.
export function blankOut(query) {
  return query.replace(COMMENT_OR_TEXT(), (text) =>
    text.startsWith("//") || text.startsWith("/*") ? " " : text[0] + text[text.length - 1],
  );
}

// Like blankOut, but every character keeps its place.
export function mask(query) {
  return query.replace(COMMENT_OR_TEXT(), (text) => {
    if (text.startsWith("//") || text.startsWith("/*")) return text.replace(/[^\n]/g, " ");
    return text[0] + " ".repeat(text.length - 2) + text[text.length - 1];
  });
}

function depths(masked) {
  const out = [];
  let depth = 0;
  for (const char of masked.split("")) {
    if (")]}".includes(char)) depth -= 1;
    out.push(depth);
    if ("([{".includes(char)) depth += 1;
  }
  return out;
}

function groupStart(skeleton, close) {
  let depth = 0;
  for (let index = close; index >= 0; index -= 1) {
    if (skeleton[index] === ")") depth += 1;
    else if (skeleton[index] === "(") {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return null;
}

// True when a "+", "*" or "{" after ")" belongs to an expression, not a path.
function isArithmetic(skeleton, found) {
  if (skeleton[found.index] !== ")") return false;
  const start = groupStart(skeleton, found.index);
  if (start === null) return false;
  return !RELATIONSHIP.test(skeleton.slice(start, found.index + 1));
}

function checkQuantifiers(skeleton) {
  for (const found of skeleton.matchAll(QUANTIFIER())) {
    if (isArithmetic(skeleton, found)) continue;
    const [, symbol, lower, comma, upper] = found;
    const limit = symbol === "+" || symbol === "*" ? null : comma !== undefined ? upper : lower;
    if (!limit) {
      throw new UnsafeCypherError(
        `quantified path pattern '${found[0].trim()}' needs an upper bound ` +
          `(at most ${MAX_HOPS}, e.g. {1,3})`,
      );
    }
    if (Number(limit) > MAX_HOPS) {
      throw new UnsafeCypherError(`quantified path pattern longer than ${MAX_HOPS} hops`);
    }
  }
}

function checkHops(skeleton) {
  for (const [, pattern] of skeleton.matchAll(REL_PATTERN())) {
    const hops = pattern.match(HOPS);
    if (hops === null) continue;
    const [, lower, dots, upper] = hops;
    const limit = dots === undefined ? lower : upper;
    if (!limit) {
      throw new UnsafeCypherError(
        `variable-length pattern [${pattern.trim()}] needs an upper bound ` +
          `(at most ${MAX_HOPS}, e.g. *1..3)`,
      );
    }
    if (Number(limit) > MAX_HOPS) {
      throw new UnsafeCypherError(`variable-length pattern longer than ${MAX_HOPS} hops`);
    }
  }
  checkQuantifiers(skeleton);
}

function branches(masked) {
  const levels = depths(masked);
  const starts = [0];
  const ends = [];
  for (const union of masked.matchAll(UNION())) {
    if (levels[union.index] === 0) {
      ends.push(union.index);
      starts.push(union.index + union[0].length);
    }
  }
  ends.push(masked.length);
  return starts.map((start, i) => [start, ends[i]]);
}

function limitBranch(text, masked, maxRows) {
  const levels = depths(masked);
  const limits = [...masked.matchAll(LIMIT())].filter((m) => levels[m.index] === 0);
  const last = limits.length ? limits[limits.length - 1] : null;
  if (last !== null) {
    const end = last.index + last[0].length;
    if (!CLAUSE.test(masked.slice(end))) {
      const value = masked.slice(end).trim();
      if (/^\d+$/.test(value) && Number(value) <= maxRows) return text;
      const trailing = text.slice(text.trimEnd().length);
      return text.slice(0, last.index) + `LIMIT ${maxRows}` + trailing;
    }
  }
  const body = text.trimEnd();
  return body + `\nLIMIT ${maxRows}` + text.slice(body.length);
}

function withLimit(query, maxRows) {
  const masked = mask(query);
  for (const [start, end] of branches(masked).reverse()) {
    const limited = limitBranch(query.slice(start, end), masked.slice(start, end), maxRows);
    query = query.slice(0, start) + limited + query.slice(end);
  }
  return query;
}

function stripSemicolons(text) {
  return text.replace(/;+$/, "");
}

// The query ready to run, or UnsafeCypherError saying why it can't run.
export function validateCypher(query, maxRows = 25) {
  query = stripSemicolons(query.trim().replace(FENCE, "").trim()).trim();
  if (!query) throw new UnsafeCypherError("empty query");
  if (query.length > MAX_QUERY_CHARS) {
    throw new UnsafeCypherError(`query longer than ${MAX_QUERY_CHARS} characters`);
  }
  const skeleton = blankOut(query);
  if (skeleton.includes(";")) throw new UnsafeCypherError("only one statement is allowed");
  const words = [...skeleton.matchAll(WORD())].map((m) => m[1].toUpperCase());
  if (!words.length || !READ_STARTS.includes(words[0])) {
    throw new UnsafeCypherError(`a read query starts with one of ${READ_STARTS.join(", ")}`);
  }
  const forbidden = [...new Set(words.filter((w) => FORBIDDEN_WORDS.includes(w)))].sort();
  if (forbidden.length) {
    throw new UnsafeCypherError(`not a read-only query (uses ${forbidden.join(", ")})`);
  }
  if (!words.includes("RETURN")) throw new UnsafeCypherError("the query returns nothing (no RETURN)");
  checkHops(skeleton.split("").map((c) => ARROW_CHARS.get(c) ?? c).join(""));
  return withLimit(query, maxRows);
}

// --- Time parameters -----------------------------------------------------------------------
// WikiPulse passes $now, $today_start and $week_start as datetimes. JSON parameters can
// only carry their ISO text, so each use outside strings and comments becomes
// datetime($name): `ev.started_at >= $today_start` compares datetimes, as in Neo4j.
export const TIME_PARAMETERS = ["now", "today_start", "week_start"];

export function bindTimes(query, names = TIME_PARAMETERS) {
  const masked = mask(query);
  const found = /\$([A-Za-z_][A-Za-z0-9_]*)/g;
  let out = "";
  let from = 0;
  for (const match of masked.matchAll(found)) {
    if (!names.includes(match[1])) continue;
    out += query.slice(from, match.index) + `datetime(${match[0]})`;
    from = match.index + match[0].length;
  }
  return out + query.slice(from);
}

// --- Search ----------------------------------------------------------------------------------
// The search box's words as a full-text query: like wikipulse.qa.graph.lucene_terms()
// (words of two or more letters, lower case, each once, joined by OR).
export function luceneTerms(text) {
  const terms = (text.match(/[\p{L}\p{N}_]+/gu) || []).map((t) => t.toLowerCase());
  return [...new Set(terms.filter((t) => t.length > 1))].join(" OR ");
}
