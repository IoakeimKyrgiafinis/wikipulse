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
// Python's regular expressions are Unicode-aware: \w, \d, \s and \b match letters, digits
// and spaces of every script, and IGNORECASE lets "ı" match "i". JavaScript's \w, \d and
// \b are ASCII-only, so the patterns below spell out Python's classes (WORD_CHAR, DIGIT,
// SPACE, keyword()) and read digits with digitsValue(), like Python's int(). Only
// characters added to Unicode after Python's own Unicode version (15.0 for Python 3.12)
// can still be read differently.
//
// Also here: bindTimes() (the $now, $today_start, $week_start parameters, which JSON
// can't carry as datetimes) and luceneTerms() (the search box's full-text query).

export const MAX_QUERY_CHARS = 3000;
export const MAX_HOPS = 6;
// GQL's INSERT, NODETACH and NEXT included: Relate runs them, and INSERT writes.
export const FORBIDDEN_WORDS = [
  "CREATE", "INSERT", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "FOREACH", "LOAD",
  "CALL", "USE", "ALTER", "GRANT", "DENY", "REVOKE", "SHOW", "TERMINATE", "START", "STOP",
  "ENABLE", "RENAME", "NODETACH", "NEXT",
];
export const READ_STARTS = ["MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN"];

// Python's classes, for patterns with the u flag: \w is a letter, a number or "_" from
// any script; \d a decimal digit of any script; \s these characters (str.isspace()).
const WORD_CHAR = String.raw`\p{L}\p{N}_`;
const DIGIT = String.raw`\p{Nd}`;
const SPACE = String.raw`\t\n\v\f\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000`;
const S = `[${SPACE}]`;
const D = `[${DIGIT}]`;
// Python's \bWORD\b with IGNORECASE: also "İ" and "ı" match "i", "ſ" "s", and the Kelvin
// sign "K" "k".
const CASE_EXTRAS = { I: "\u0130\u0131", S: "\u017f", K: "\u212a" };
const keyword = (word) => [...word].map((c) => `[${c}${c.toLowerCase()}${CASE_EXTRAS[c] || ""}]`).join("");
const bounded = (body) => `(?<![${WORD_CHAR}])(?:${body})(?![${WORD_CHAR}])`;
const pattern = (source, flags = "") => () => new RegExp(source, `u${flags}`);

const COMMENT_OR_TEXT = () =>
  /\/\/[^\n]*|\/\*[\s\S]*?\*\/|'(?:\\[\s\S]|[^'\\])*'|"(?:\\[\s\S]|[^"\\])*"|`[^`]*`/g;
const WORD = pattern(`(?<![.${WORD_CHAR}$])([A-Za-z]+)(?![${WORD_CHAR}])`, "g");
const REL_PATTERN = pattern(String.raw`-${S}*\[([^\]]*)\]`, "g");
const HOPS = pattern(String.raw`\*${S}*(${D}*)${S}*(\.\.${S}*(${D}*))?`);
const QUANTIFIER = pattern(String.raw`[\-)>]${S}*(\{${S}*(${D}*)${S}*(,${S}*(${D}*))?${S}*\}|\+|\*)`, "g");
const RELATIONSHIP = pattern(String.raw`-${S}*-|-${S}*\[|\]${S}*-|-${S}*>|<${S}*-`);
const LIMIT = pattern(bounded(keyword("LIMIT")), "g");
const UNION = pattern(bounded(`${keyword("UNION")}(${S}+${keyword("ALL")})?`), "g");
const CLAUSES = ["MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN", "WHERE", "ORDER", "SKIP", "OFFSET", "UNION"];
const CLAUSE = pattern(bounded(CLAUSES.map(keyword).join("|")));
const FENCE = pattern(`^\`\`\`[a-zA-Z]*${S}*|${S}*\`\`\`$`, "g");
const LEADING_SPACE = pattern(`^${S}+`);
const TRAILING_SPACE = pattern(`${S}+$`);
const DECIMAL = pattern(`^${D}+$`);
const IS_DIGIT = pattern(`^${D}$`);

// Python's str.rstrip() and str.strip() (JavaScript's trim() knows other spaces).
const stripEnd = (text) => text.replace(TRAILING_SPACE(), "");
const strip = (text) => stripEnd(text).replace(LEADING_SPACE(), "");
// Python's len(): code points, not UTF-16 units.
const codePoints = (text) => [...text].length;

// Python's int() on decimal digits of any script ("７" is 7, "٣" is 3). Unicode encodes
// each script's digits as a run 0 to 9 in order, and every unbroken stretch of decimal
// digits is made of whole runs, so a digit's value is its distance from the start of
// its stretch, modulo 10.
function digitValue(char) {
  let start = char.codePointAt(0);
  while (start > 0 && IS_DIGIT().test(String.fromCodePoint(start - 1))) start -= 1;
  return (char.codePointAt(0) - start) % 10;
}

export function digitsValue(text) {
  return [...text].reduce((total, char) => total * 10 + digitValue(char), 0);
}
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
  return !RELATIONSHIP().test(skeleton.slice(start, found.index + 1));
}

function checkQuantifiers(skeleton) {
  for (const found of skeleton.matchAll(QUANTIFIER())) {
    if (isArithmetic(skeleton, found)) continue;
    const [, symbol, lower, comma, upper] = found;
    const limit = symbol === "+" || symbol === "*" ? null : comma !== undefined ? upper : lower;
    if (!limit) {
      throw new UnsafeCypherError(
        `quantified path pattern '${strip(found[0])}' needs an upper bound ` +
          `(at most ${MAX_HOPS}, e.g. {1,3})`,
      );
    }
    if (digitsValue(limit) > MAX_HOPS) {
      throw new UnsafeCypherError(`quantified path pattern longer than ${MAX_HOPS} hops`);
    }
  }
}

function checkHops(skeleton) {
  for (const [, pattern] of skeleton.matchAll(REL_PATTERN())) {
    const hops = pattern.match(HOPS());
    if (hops === null) continue;
    const [, lower, dots, upper] = hops;
    const limit = dots === undefined ? lower : upper;
    if (!limit) {
      throw new UnsafeCypherError(
        `variable-length pattern [${strip(pattern)}] needs an upper bound ` +
          `(at most ${MAX_HOPS}, e.g. *1..3)`,
      );
    }
    if (digitsValue(limit) > MAX_HOPS) {
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
    if (!CLAUSE().test(masked.slice(end))) {
      const value = strip(masked.slice(end));
      if (DECIMAL().test(value) && digitsValue(value) <= maxRows) return text;
      const trailing = text.slice(stripEnd(text).length);
      return text.slice(0, last.index) + `LIMIT ${maxRows}` + trailing;
    }
  }
  const body = stripEnd(text);
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
  query = strip(stripSemicolons(strip(strip(query).replace(FENCE(), ""))));
  if (!query) throw new UnsafeCypherError("empty query");
  if (codePoints(query) > MAX_QUERY_CHARS) {
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
  const terms = (text.match(new RegExp(`[${WORD_CHAR}]+`, "gu")) || []).map((t) => t.toLowerCase());
  return [...new Set(terms.filter((t) => codePoints(t) > 1))].join(" OR ");
}
