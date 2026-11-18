// The demo's graph database (spec 13a): the 5 November 2026 knowledge graph, loaded into
// Relate (an embedded graph database that speaks Cypher, compiled to WebAssembly).
//
// No DOM and no Worker API here, so the same code runs in the page's Web Worker
// (graph-worker.js) and in Node for the tests (tests/js/graph_check.mjs).
//
// data/graph.json (scripts/export_graph.py) carries everything: the statements that
// rebuild the graph (`load`, `cleanup`), the retrieval queries of wikipulse/qa/graph.py
// (`queries`), the graph's clock and the prepared questions with Neo4j's rows.

import { bindTimes, validateCypher } from "./cypher-check.js";

export const VISITOR_MAX_ROWS = 100; // rows of a query typed into the page
export const NAMED_MAX_ROWS = 200; // rows of a shipped query, whatever its $limit
export const FULL_ROWS = 10000; // "no limit": the full result behind a LIMIT (tie check)
// Datetime parameters of the shipped queries (EVENTS_BETWEEN, TRENDING). JSON carries
// them as ISO text, so runNamed() wraps each use in datetime(), as for $now.
export const NAMED_TIME_PARAMETERS = ["start", "end", "since"];
const HIDDEN_PROPERTIES = new Set(["embedding", "embedding_hash", "embedding_model"]);

export class GraphDb {
  // `RelateDb` is the class from relate.js (after its init()); `data` is graph.json.
  constructor(RelateDb, data) {
    this.db = new RelateDb();
    this.data = data;
  }

  // One statement; its envelope {columns, rows, error, ...}. Engine errors don't throw.
  execute(query, params = null) {
    return JSON.parse(this.db.execute(query, params === null ? null : JSON.stringify(params)));
  }

  // Rebuild the graph: schema, temporary id indexes, nodes and relationships in batches,
  // then drop the temporary ids. Returns {ms, nodes, relationships}.
  load() {
    const started = performance.now();
    const { load, cleanup, batch } = this.data;
    for (const step of load) {
      if (step.rows === null) {
        this.check(this.execute(step.cypher), step.cypher);
        continue;
      }
      for (let i = 0; i < step.rows.length; i += batch) {
        this.check(this.execute(step.cypher, { rows: step.rows.slice(i, i + batch) }), step.cypher);
      }
    }
    for (const statement of cleanup) this.check(this.execute(statement), statement);
    const nodes = this.execute("MATCH (n) RETURN count(n) AS n").rows[0].n;
    const relationships = this.execute("MATCH ()-[r]->() RETURN count(r) AS n").rows[0].n;
    return { ms: performance.now() - started, nodes, relationships };
  }

  check(result, statement) {
    if (result.error) throw new Error(`loading the graph failed: ${result.error} (${statement.slice(0, 80)})`);
  }

  // A shipped query by name (graph.json `queries`), with JSON parameters; datetime
  // parameters ($start, $end, $since) are given as ISO text.
  runNamed(name, params) {
    const query = this.data.queries[name];
    if (!query) return { error: `no query named ${name}` };
    return this.result(this.execute(bindTimes(query, NAMED_TIME_PARAMETERS), params), NAMED_MAX_ROWS);
  }

  // A prepared question's Cypher, run live, with its comparison to Neo4j's stored rows.
  runPrepared(id) {
    const question = this.data.prepared.find((q) => q.id === id);
    if (!question) return { error: `no prepared question ${id}` };
    const live = this.runCypher(question.cypher, this.data.max_rows);
    if (live.refused || live.error) return live;
    return { ...live, comparison: this.comparePrepared(question, live) };
  }

  // {verdict, same, note}; "tie" only when the question's tie evidence proves it.
  comparePrepared(question, live) {
    if (live.columns.join("|") !== question.neo4j.columns.join("|")) {
      return { verdict: "different", same: false, note: "Other columns." };
    }
    const comparison = compareRows(live.rows, question.neo4j.rows);
    if (comparison.same || !question.tie || !this.provesTie(question, live.rows)) return comparison;
    const keys = question.tie.order_by.join(", ") || "nothing (no ORDER BY)";
    return {
      verdict: "tie",
      same: true,
      orderBy: question.tie.order_by,
      note:
        `The same rows down to the LIMIT. At the cut-off several rows tie on the sort key (${keys}), ` +
        "and each engine keeps a different few of them; without the LIMIT both return the same rows.",
    };
  }

  // Runs the question's query again returning its ORDER BY keys, with its own LIMIT and
  // with FULL_ROWS, and checks the difference with tieAtLimit() against Neo4j's evidence.
  provesTie(question, liveRows) {
    const { tie } = question;
    const keys = tie.order_by.length;
    const keyed = this.runCypher(tie.cypher, this.data.max_rows);
    const full = this.runCypher(tie.full_cypher, FULL_ROWS);
    if (keyed.refused || keyed.error || full.refused || full.error || full.capped) return false;
    if (!sameMultiset(dropKeys(keyed.rows, keys), liveRows)) return false;
    if (!sameMultiset(dropKeys(tie.neo4j.rows, keys), question.neo4j.rows)) return false;
    return tieAtLimit(keyed.rows, tie.neo4j.rows, full.rows, tie.neo4j_full_keys, keys);
  }

  // Cypher from outside (typed by a visitor, or an LLM-written prepared question): it
  // runs only if WikiPulse's read-only check accepts it, with a row cap; $now,
  // $today_start and $week_start are the graph's clock.
  runCypher(text, maxRows = VISITOR_MAX_ROWS) {
    let query;
    try {
      query = validateCypher(text, maxRows);
    } catch (error) {
      if (error.name === "UnsafeCypherError") return { refused: error.message };
      throw error;
    }
    const result = this.result(this.execute(bindTimes(query), this.data.clock), maxRows);
    return { ...result, query };
  }

  // The envelope as {columns, rows (canonical, in column order), ms, error}.
  result(envelope, maxRows) {
    if (envelope.error) return { error: envelope.error };
    const columns = envelope.columns;
    const types = envelope.columnTypes || [];
    const rows = envelope.rows.slice(0, maxRows).map((row) =>
      columns.map((column, i) => canonical(row[column], types[i] === "PATH")),
    );
    return {
      columns,
      rows,
      ms: envelope.elapsed_ms,
      capped: envelope.rows.length > maxRows,
    };
  }
}

// --- One comparable form for both engines --------------------------------------------------
// The same rules as canonical() in scripts/export_graph.py, which wrote Neo4j's rows:
// nodes {labels, properties}, relationships {type, properties}, paths {nodes,
// relationships}, datetimes as UTC ISO text with six fraction digits, map keys sorted.
const ISO_TIME = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})(\[[^\]]+\])?$/;

export function canonicalTime(text) {
  const found = text.match(ISO_TIME);
  if (!found) return text;
  let [, base, fraction = "", offset] = found;
  const micros = (fraction + "000000").slice(0, 6);
  if (!["Z", "+00:00", "-00:00"].includes(offset)) {
    const moment = new Date(`${base}${offset}`); // whole seconds: no precision lost
    base = moment.toISOString().slice(0, 19);
  }
  return `${base}.${micros}Z`;
}

function isKind(value, kind) {
  return value !== null && typeof value === "object" && !Array.isArray(value) && value.kind === kind;
}

function looksLikePath(list) {
  if (list.length < 3 || list.length % 2 === 0) return false;
  return list.every((item, i) => isKind(item, i % 2 === 0 ? "node" : "relationship"));
}

function properties(props) {
  const out = {};
  for (const key of Object.keys(props).sort()) {
    if (!HIDDEN_PROPERTIES.has(key)) out[key] = canonical(props[key]);
  }
  return out;
}

export function canonical(value, isPath = false) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return canonicalTime(value);
  if (typeof value !== "object") return value;
  if (Array.isArray(value)) {
    if (isPath || looksLikePath(value)) {
      return {
        nodes: value.filter((_, i) => i % 2 === 0).map((n) => canonical(n)),
        relationships: value.filter((_, i) => i % 2 === 1).map((r) => canonical(r)),
      };
    }
    return value.map((item) => canonical(item));
  }
  if (isKind(value, "node")) return { labels: [...value.labels].sort(), properties: properties(value.properties) };
  if (isKind(value, "relationship")) return { type: value.type, properties: properties(value.properties) };
  const out = {};
  for (const key of Object.keys(value).sort()) out[key] = canonical(value[key]);
  return out;
}

// --- Comparing results -----------------------------------------------------------------------
const text = (value) => JSON.stringify(value);

// Every list sorted, recursively: for lists whose order the query leaves open, e.g.
// collect() without a full ORDER BY before it.
function unordered(value) {
  if (Array.isArray(value)) return value.map(unordered).sort((a, b) => (text(a) < text(b) ? -1 : text(a) > text(b) ? 1 : 0));
  if (value !== null && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = unordered(value[key]);
    return out;
  }
  return value;
}

function sameMultiset(a, b) {
  if (a.length !== b.length) return false;
  const left = a.map(text).sort();
  const right = b.map(text).sort();
  return left.every((row, i) => row === right[i]);
}

// How the live rows compare with the stored ones: {verdict, same, note}. Rows that
// differ only in which tied rows a LIMIT kept are "different" here; GraphDb.comparePrepared()
// calls that a tie only when tieAtLimit() proves it.
export function compareRows(live, stored) {
  if (text(live) === text(stored)) return { verdict: "identical", same: true, note: "The same rows in the same order." };
  if (sameMultiset(live, stored)) {
    return { verdict: "same rows", same: true, note: "The same rows in another order: the query doesn't fully order them." };
  }
  if (sameMultiset(live.map(unordered), stored.map(unordered))) {
    return {
      verdict: "same rows",
      same: true,
      note: "The same rows; lists inside them are in another order (the query collects them without a full order).",
    };
  }
  return { verdict: "different", same: false, note: "The rows differ." };
}

// --- Ties at a LIMIT -------------------------------------------------------------------------
// The same check as tie_at_limit() in scripts/export_graph.py. All rows come from a query
// that also returns its ORDER BY keys as its last `keys` columns: `live` and `stored` with
// the query's own LIMIT (Relate's and Neo4j's), `fullLive` with a much larger one;
// `fullStoredKeys` are the keys of Neo4j's full result, in its order. A tie needs: the
// LIMIT cut the result and both full results have the same keys; both limited results
// are rows of the full result with the same key sequence; the rows above the last row's
// key are the same in both and as many, with the same keys, as the full result ranks
// above it; and more rows share that key in the full result than the LIMIT kept.
function counts(values) {
  const out = new Map();
  for (const value of values) out.set(value, (out.get(value) || 0) + 1);
  return out;
}

function sameCounts(a, b) {
  return a.size === b.size && [...a].every(([key, n]) => b.get(key) === n);
}

function within(rows, full) {
  const have = counts(full.map((row) => text(unordered(row))));
  return [...counts(rows.map((row) => text(unordered(row))))].every(([key, n]) => (have.get(key) || 0) >= n);
}

export function dropKeys(rows, keys) {
  return rows.map((row) => row.slice(0, row.length - keys));
}

export function tieAtLimit(live, stored, fullLive, fullStoredKeys, keys) {
  const count = live.length;
  if (!count || stored.length !== count || fullLive.length !== fullStoredKeys.length) return false;
  if (fullLive.length <= count) return false;
  const key = (row) => text(row.slice(row.length - keys));
  const fullKeys = fullStoredKeys.map((k) => text(k));
  if (!sameCounts(counts(fullLive.map(key)), counts(fullKeys))) return false;
  if (!within(live, fullLive) || !within(stored, fullLive)) return false;
  if (text(live.map(key)) !== text(stored.map(key))) return false;
  const boundary = key(live[live.length - 1]);
  const aboveLive = live.filter((row) => key(row) !== boundary);
  const aboveStored = stored.filter((row) => key(row) !== boundary);
  const rankedAbove = fullKeys.indexOf(boundary);
  if (aboveLive.length !== rankedAbove || !sameMultiset(aboveLive.map(unordered), aboveStored.map(unordered))) {
    return false;
  }
  if (!sameCounts(counts(aboveLive.map(key)), counts(fullKeys.slice(0, rankedAbove)))) return false;
  return fullKeys.filter((k) => k === boundary).length > count - rankedAbove;
}
