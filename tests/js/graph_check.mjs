// Runs the demo's graph database the way the page does, in Node, with the vendored
// Relate WebAssembly build (scripts/vendor_relate.py) and site/data/graph.json:
//
//   node tests/js/graph_check.mjs <site folder>
//
// It loads the graph with site/graph-db.js, then checks what the page offers: the
// search finds "pena", Santiago Peña and Ueno Bank are connected, every prepared
// question's live rows match Neo4j's stored rows (or differ only in an explained way),
// a write is refused, and the row cap holds. Prints one JSON report and exits 1 on any
// failure. Called by tests/test_demo_graph_js.py and by the Pages workflow.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { importFrom, stage } from "./stage.mjs";

const siteDir = process.argv[2] || "site";
const dir = stage(siteDir, { vendor: true });
const relate = await importFrom(dir, "vendor/relate/relate.js");
const { GraphDb, compareRows, tieAtLimit, VISITOR_MAX_ROWS } = await importFrom(dir, "graph-db.js");
const { luceneTerms } = await importFrom(dir, "cypher-check.js");

const failures = [];
const expect = (ok, what, detail = {}) => {
  if (!ok) failures.push({ what, ...detail });
};

let started = performance.now();
await relate.default({ module_or_path: readFileSync(join(dir, "vendor/relate/relate_bg.wasm")) });
const initMs = performance.now() - started;
const data = JSON.parse(readFileSync(join(siteDir, "data/graph.json"), "utf-8"));
const graph = new GraphDb(relate.RelateDb, data);
const loaded = graph.load();
expect(loaded.nodes === data.counts.nodes, "node count", { got: loaded.nodes });
expect(loaded.relationships === data.counts.relationships, "relationship count", { got: loaded.relationships });

// 1. Search, accent-insensitive.
const search = graph.runNamed("SEARCH_ENTITIES", { query: luceneTerms("pena"), limit: 20 });
expect(!search.error && search.rows.length > 0 && search.rows[0][1] === "Santiago Peña", "search pena", { search });
const keyOf = (name) => {
  const found = graph.runNamed("SEARCH_ENTITIES", { query: luceneTerms(name), limit: 20 });
  const row = found.rows.find((r) => r[1] === name);
  return row ? row[0] : null;
};

// 2. The entity view's queries for Santiago Peña.
const pena = keyOf("Santiago Peña");
const ueno = keyOf("Ueno Bank");
expect(pena && ueno, "keys of Santiago Peña and Ueno Bank", { pena, ueno });
const events = graph.runNamed("ENTITY_EVENTS", { key: pena, limit: 10 });
const relations = graph.runNamed("ENTITY_RELATIONS", { key: pena, limit: 15 });
expect(events.rows?.length > 0 && relations.rows?.length > 0, "entity view", { events, relations });

// 3. The connection finder.
started = performance.now();
const paths = graph.runNamed("PATHS", { a: pena, b: ueno, limit: 3 });
const pathMs = performance.now() - started;
expect(!paths.error && paths.rows.length > 0, "a path from Santiago Peña to Ueno Bank", { paths });

// 4. Prepared questions: live rows against Neo4j's, as the page compares them (a tie
// only when the full results prove it).
const verdicts = {};
for (const question of data.prepared) {
  const live = graph.runPrepared(question.id);
  if (live.error || live.refused) {
    expect(false, `prepared ${question.id} ran`, { live });
    continue;
  }
  verdicts[question.id] = live.comparison.verdict;
  expect(live.comparison.same, `prepared ${question.id} matches Neo4j`, {
    comparison: live.comparison,
    live: live.rows.slice(0, 3),
  });
}

// 5. Read-only: a write is refused before it reaches the database.
const write = graph.runCypher("MATCH (n:Entity {name: 'Ueno Bank'}) SET n.name = 'Nobody' RETURN n.name");
expect(Boolean(write.refused), "a write is refused", { write });
// GQL's INSERT and NEXT: Relate would run both (tests/test_demo_graph_relate.py).
for (const gql of [
  "MATCH (n:Entity {name: 'Ueno Bank'}) INSERT (:Entity {name: 'Injected by visitor'}) RETURN n.name",
  "MATCH (n:Entity) RETURN n.name AS a NEXT INSERT (:Entity {name: 'Injected by visitor'}) RETURN 1 AS b",
]) {
  const refused = graph.runCypher(gql);
  expect(Boolean(refused.refused), "a GQL write is refused", { gql, refused });
}
const still = graph.runCypher("MATCH (n:Entity {name: 'Ueno Bank'}) RETURN count(n) AS n");
expect(still.rows?.[0]?.[0] === 1, "the graph is unchanged", { still });
const injected = graph.runCypher("MATCH (n:Entity {name: 'Injected by visitor'}) RETURN count(n) AS n");
expect(injected.rows?.[0]?.[0] === 0, "nothing was inserted", { injected });

// 6. The row cap.
const many = graph.runCypher("MATCH (n) RETURN n.name AS name");
expect(many.rows?.length === VISITOR_MAX_ROWS && many.query.endsWith(`LIMIT ${VISITOR_MAX_ROWS}`), "row cap", {
  rows: many.rows?.length,
});

// 7. Shipped queries with datetime parameters take them as ISO text.
const between = graph.runNamed("EVENTS_BETWEEN", { start: "2026-11-05T18:00:00Z", end: "2026-11-05T20:00:00Z", limit: 15 });
expect(!between.error && between.rows.length > 0, "EVENTS_BETWEEN with ISO text", { between });
const trending = graph.runNamed("TRENDING", { since: "2026-11-05T22:00:00Z", limit: 10 });
expect(!trending.error && trending.rows.length > 0, "TRENDING with ISO text", { trending });

// 8. The comparison rules (the same cases as tests/test_demo_graph.py).
expect(compareRows([["x", "a"], ["x", "b"]], [["x", "c"], ["x", "d"]]).verdict === "different", "a constant column is no tie");
const FULL = [["a", 9, 9], ["b", 5, 5], ["c", 5, 5], ["d", 5, 5], ["e", 1, 1]];
const FULL_KEYS = [[9], [5], [5], [5], [1]];
const tieCases = [
  [true, FULL.slice(0, 3), [FULL[0], FULL[3], FULL[1]], FULL, FULL_KEYS, 1],
  [false, FULL.slice(0, 3), [FULL[1], FULL[2], FULL[3]], FULL, FULL_KEYS, 1],
  [false, FULL.slice(0, 3), [FULL[0], FULL[1], ["z", 5, 5]], FULL, FULL_KEYS, 1],
  [false, FULL.slice(0, 3), [FULL[0], FULL[3], FULL[1]], FULL, [[9], [5], [5], [1], [1]], 1],
  [false, FULL.slice(0, 3), [FULL[0], FULL[3], FULL[1]], FULL.slice(0, 3), FULL_KEYS.slice(0, 3), 1],
  [false, FULL.slice(0, 4), [FULL[0], FULL[3], FULL[1], FULL[2]], FULL, FULL_KEYS, 1],
  [false, [["x", "a", 3], ["x", "b", 2]], [["x", "c", 1], ["x", "d", 0]],
    [["x", "a", 3], ["x", "b", 2], ["x", "c", 1], ["x", "d", 0]], [[3], [2], [1], [0]], 1],
];
tieCases.forEach(([wanted, ...args], index) => expect(tieAtLimit(...args) === wanted, `tie rule ${index}`));

const report = {
  init_ms: Math.round(initMs),
  load_ms: Math.round(loaded.ms),
  path_ms: Math.round(pathMs),
  nodes: loaded.nodes,
  relationships: loaded.relationships,
  verdicts,
  failures,
};
console.log(JSON.stringify(report));
process.exit(failures.length ? 1 : 0);
