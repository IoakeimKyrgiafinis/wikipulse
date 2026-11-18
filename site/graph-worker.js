// The demo's graph database runs here, in a Web Worker (spec 13a), started by explore.js
// only when the visitor opens "Explore the graph".
//
// Why a worker: WebAssembly has no statement timeout and no way to cancel a running
// query. In a worker, a runaway query blocks only the worker; the page stays responsive
// and, after its time limit, terminates the worker and starts a fresh one.
//
// Messages in:  {id, op: "load"} | {id, op: "named", name, params} | {id, op: "cypher", text, maxRows}
//               | {id, op: "prepared", question}
// Messages out: {id, ok: true, result} | {id, ok: false, error, trap}
// Everything is fetched from this site: relate.js and relate_bg.wasm (vendored by
// scripts/vendor_relate.py) and data/graph.json.

import init, { RelateDb } from "./vendor/relate/relate.js";
import { isWasmTrap } from "./vendor/relate/trap.js";
import { GraphDb } from "./graph-db.js";

const GRAPH_URL = "data/graph.json";
let graph = null;

async function load() {
  const started = performance.now();
  await init();
  const response = await fetch(GRAPH_URL);
  if (!response.ok) throw new Error(`${GRAPH_URL}: ${response.status} ${response.statusText}`);
  const data = await response.json();
  const fetched = performance.now();
  graph = new GraphDb(RelateDb, data);
  const loaded = graph.load();
  return {
    download_ms: fetched - started,
    load_ms: loaded.ms,
    total_ms: performance.now() - started,
    nodes: loaded.nodes,
    relationships: loaded.relationships,
    snapshot: data.snapshot,
    run: data.run,
    clock: data.clock,
    max_hops: data.max_hops,
    max_rows: data.max_rows,
    queries: Object.keys(data.queries),
    prepared: data.prepared,
  };
}

function handle(message) {
  if (message.op === "named") return graph.runNamed(message.name, message.params);
  if (message.op === "cypher") return graph.runCypher(message.text, message.maxRows);
  if (message.op === "prepared") return graph.runPrepared(message.question);
  throw new Error(`unknown operation ${message.op}`);
}

self.addEventListener("message", async (event) => {
  const message = event.data;
  try {
    const result = message.op === "load" ? await load() : handle(message);
    self.postMessage({ id: message.id, ok: true, result });
  } catch (error) {
    self.postMessage({ id: message.id, ok: false, error: String(error?.message || error), trap: isWasmTrap(error) });
  }
});
