// "Explore the graph" (spec 13a): the 5 November 2026 knowledge graph, queried live in
// the visitor's browser. Nothing here starts until the visitor presses the button, so
// the replay above (app.js) never loads the database.
//
// The database (Relate, compiled to WebAssembly) runs in a Web Worker
// (graph-worker.js). This page talks to it with messages, one query at a time, and
// gives each query QUERY_LIMIT_MS: WebAssembly can't cancel a statement, so a query
// that takes longer is stopped by terminating the worker, and a fresh worker reloads
// the graph (about a second).
//
// Everything from the data is written with textContent, never as HTML; only Wikipedia
// and Wikidata links become clickable.

import { luceneTerms } from "./cypher-check.js";

const QUERY_LIMIT_MS = 5000;
const LOAD_LIMIT_MS = 60000;
const MAX_CELL_CHARS = 400;
const WIKIPEDIA = "https://en.wikipedia.org/";
const WIKIDATA = "https://www.wikidata.org/wiki/";
const EXAMPLES = {
  relations:
    "MATCH (n:Entity {name: 'Ueno Bank'})-[r:RELATED_TO]-(m:Entity)\n" +
    "RETURN startNode(r).name AS subject, r.relation AS relation, endNode(r).name AS object,\n" +
    "       r.source_title AS source, r.source_revision AS revision",
  busiest:
    "MATCH (n:Entity)-[:INVOLVED_IN]->(ev:Event)\n" +
    "RETURN n.name AS entity, n.type AS type, count(ev) AS events\n" +
    "ORDER BY events DESC, entity LIMIT 10",
  recent:
    "MATCH (ev:Event) WHERE ev.started_at >= $now - duration({hours: 1})\n" +
    "RETURN ev.title AS event, ev.started_at AS started, ev.page_count AS pages\n" +
    "ORDER BY started DESC",
  slow: "MATCH (a:Entity), (b:Entity), (c:Entity)\nRETURN count(*) AS combinations",
  write: "MATCH (n:Entity {name: 'Ueno Bank'})\nSET n.name = 'Nobody'\nRETURN n.name",
};

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function link(url, text) {
  const safe = typeof url === "string" && (url.startsWith(WIKIPEDIA) || url.startsWith(WIKIDATA));
  if (safe) return el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text);
  return document.createTextNode(text);
}

function sourceLink(title, revision) {
  if (!title) return null;
  const url = revision
    ? `${WIKIPEDIA}w/index.php?oldid=${encodeURIComponent(revision)}`
    : `${WIKIPEDIA}wiki/${encodeURIComponent(title.replaceAll(" ", "_"))}`;
  const a = link(url, "[source]");
  a.title = revision ? `${title}, revision ${revision}` : title;
  return a;
}

function button(text, onClick, attrs = {}) {
  const node = el("button", { type: "button", ...attrs }, text);
  node.addEventListener("click", onClick);
  return node;
}

// Accent- and case-insensitive form of a name ("Peña" -> "pena").
const fold = (text) => text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();

// An entity's aliases other than its name.
const otherNames = (entity) => (entity.aliases || []).filter((a) => a !== entity.name);

// --- Talking to the worker -------------------------------------------------------------------
class GraphClient {
  constructor(onStatus) {
    this.onStatus = onStatus;
    this.worker = null;
    this.pending = new Map();
    this.nextId = 1;
    this.queue = Promise.resolve();
    this.meta = null;
    this.broken = null; // why the database is gone for good (a reload failed), or null
  }

  async start() {
    this.worker = new Worker("graph-worker.js", { type: "module" });
    this.worker.addEventListener("message", (event) => this.settle(event.data.id, event.data));
    this.worker.addEventListener("error", (event) => {
      event.preventDefault();
      for (const id of [...this.pending.keys()]) {
        this.settle(id, { ok: false, error: event.message || "the database worker failed to start" });
      }
    });
    const reply = await this.send({ op: "load" }, LOAD_LIMIT_MS);
    if (!reply.ok) throw new Error(reply.timedOut ? "loading took too long" : reply.error);
    this.meta = reply.result;
    return this.meta;
  }

  settle(id, reply) {
    const resolve = this.pending.get(id);
    if (!resolve) return;
    this.pending.delete(id);
    resolve(reply);
  }

  send(message, limitMs) {
    const id = this.nextId++;
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.worker.terminate();
        this.settle(id, { ok: false, timedOut: true });
      }, limitMs);
      this.pending.set(id, (reply) => {
        clearTimeout(timer);
        resolve(reply);
      });
      this.worker.postMessage({ ...message, id });
    });
  }

  // One query at a time; a query that runs out of time (or crashes the WebAssembly
  // instance) is answered with an error, and the graph is reloaded in a new worker.
  run(message) {
    const job = this.queue.then(async () => {
      if (this.broken) return { ok: false, error: this.broken, elapsed: 0 };
      const started = performance.now();
      const reply = await this.send(message, QUERY_LIMIT_MS);
      const elapsed = performance.now() - started;
      if (reply.timedOut || reply.trap) {
        this.onStatus(
          reply.timedOut
            ? `A query ran longer than ${QUERY_LIMIT_MS / 1000} s and was stopped. Reloading the graph...`
            : "The database crashed on a query. Reloading the graph...",
          "busy",
        );
        this.worker.terminate();
        try {
          await this.start();
        } catch (error) {
          this.broken = `the graph database could not restart (${error.message}); reload the page to try again`;
          this.onStatus(`${this.broken[0].toUpperCase()}${this.broken.slice(1)}.`, "error");
          return { ...reply, elapsed, restartFailed: true };
        }
        this.onStatus(readyText(this.meta, "Graph reloaded"), "ready");
      }
      return { ...reply, elapsed };
    });
    this.queue = job.catch(() => {});
    return job;
  }

  async named(name, params) {
    const reply = await this.run({ op: "named", name, params });
    if (!reply.ok) throw new Error(reply.timedOut ? "the query took too long" : reply.error);
    if (reply.result.error) throw new Error(reply.result.error);
    return reply.result;
  }
}

function readyText(meta, prefix = "Ready") {
  const seconds = (meta.total_ms / 1000).toFixed(1);
  return (
    `${prefix} in ${seconds} s: ${meta.nodes.toLocaleString("en")} nodes and ` +
    `${meta.relationships.toLocaleString("en")} relationships, running in your browser.`
  );
}

// Rows (arrays in column order) as objects by column name.
const objects = (result) =>
  result.rows.map((row) => Object.fromEntries(result.columns.map((c, i) => [c, row[i]])));

// --- Result tables ---------------------------------------------------------------------------
function cellText(value) {
  if (value === null || value === undefined) return "null";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > MAX_CELL_CHARS ? `${text.slice(0, MAX_CELL_CHARS - 1)}…` : text;
}

function table(columns, rows, caption) {
  if (!rows.length) return el("p", { class: "muted" }, "No rows.");
  return el(
    "table",
    { class: "rows" },
    caption ? el("caption", {}, caption) : null,
    el("thead", {}, el("tr", {}, ...columns.map((c) => el("th", { scope: "col" }, c)))),
    el(
      "tbody",
      {},
      ...rows.map((row) => el("tr", {}, ...row.map((v, i) => el("td", { "data-col": columns[i] }, cellText(v))))),
    ),
  );
}

// --- The section -----------------------------------------------------------------------------
let client = null;

function setStatus(text, kind = "") {
  const status = $("graph-status");
  status.textContent = text;
  status.className = `graph-status ${kind}`;
}

async function openExplorer() {
  const start = $("graph-start");
  start.disabled = true;
  setStatus("Starting the graph database: downloading Relate (WebAssembly) and the graph...", "busy");
  client = new GraphClient(setStatus);
  try {
    const meta = await client.start();
    $("graph-status").dataset.loadMs = String(Math.round(meta.total_ms));
    setStatus(readyText(meta), "ready");
  } catch (error) {
    setStatus(`The graph database could not start: ${error.message}`, "error");
    start.disabled = false;
    return;
  }
  start.hidden = true;
  $("graph-tools").hidden = false;
  renderPrepared(client.meta.prepared);
  $("graph-search").focus();
}

// 1. Search -----------------------------------------------------------------------------------
async function search(event) {
  event.preventDefault();
  const text = $("graph-search").value;
  const box = $("graph-search-results");
  const terms = luceneTerms(text);
  if (!terms) {
    box.replaceChildren(el("p", { class: "muted" }, "Type a name, or a word of one."));
    return;
  }
  box.replaceChildren(el("p", { class: "muted" }, "Searching..."));
  try {
    const found = objects(await client.named("SEARCH_ENTITIES", { query: terms, limit: 20 }));
    if (!found.length) {
      box.replaceChildren(el("p", { class: "muted" }, `No entity matches “${text}”.`));
      return;
    }
    box.replaceChildren(
      el(
        "ul",
        { class: "hits" },
        ...found.map((e) =>
          el(
            "li",
            {},
            button(e.name, () => showEntity(e.key)),
            el("span", { class: "muted" }, ` ${e.type || "entity"}`),
            otherNames(e).length ? el("span", { class: "muted" }, ` · also ${otherNames(e).slice(0, 3).join(", ")}`) : null,
          ),
        ),
      ),
    );
  } catch (error) {
    box.replaceChildren(el("p", { class: "error" }, `Search failed: ${error.message}`));
  }
}

// 2. Entity view ------------------------------------------------------------------------------
async function showEntity(key) {
  const box = $("graph-entity");
  box.replaceChildren(el("p", { class: "muted" }, "Reading the graph..."));
  try {
    const [entity] = objects(await client.named("ENTITIES_BY_KEY", { keys: [key] }));
    const events = objects(await client.named("ENTITY_EVENTS", { key, limit: 10 }));
    const relations = objects(await client.named("ENTITY_RELATIONS", { key, limit: 15 }));
    const neighbours = new Map();
    for (const ev of events) {
      for (const other of objects(await client.named("EVENT_ENTITIES", { key: ev.event_key, limit: 25 }))) {
        if (other.entity_key !== key) neighbours.set(other.entity_key, other);
      }
    }
    box.replaceChildren(...entityNodes(entity, events, relations, [...neighbours.values()]));
    $("graph-entity-heading").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    box.replaceChildren(el("p", { class: "error" }, `Could not read the entity: ${error.message}`));
  }
}

function entityNodes(entity, events, relations, neighbours) {
  const nodes = [
    el(
      "h4",
      {},
      entity.name,
      el("span", { class: "muted" }, ` ${entity.type || "entity"} `),
      entity.wikidata_id ? link(WIKIDATA + encodeURIComponent(entity.wikidata_id), entity.wikidata_id) : null,
    ),
  ];
  if (otherNames(entity).length) nodes.push(el("p", { class: "muted" }, `Also called: ${otherNames(entity).join(", ")}`));
  nodes.push(
    el(
      "p",
      { class: "actions" },
      button("Connect from here", () => setPathEnd("path-from", entity.name)),
      button("Connect to here", () => setPathEnd("path-to", entity.name)),
    ),
  );
  nodes.push(el("h5", {}, `Events (${events.length})`));
  nodes.push(
    el(
      "ul",
      {},
      ...events.map((ev) =>
        el("li", {}, ev.event_title || ev.event_key, el("span", { class: "muted" }, ` as ${ev.type || "involved"} `), sourceLink(ev.source_title, ev.source_revision)),
      ),
    ),
  );
  nodes.push(el("h5", {}, `Relations (${relations.length})`));
  if (!relations.length) nodes.push(el("p", { class: "muted" }, "None extracted."));
  nodes.push(
    el(
      "ul",
      {},
      ...relations.map((r) =>
        el("li", {}, `${r.subject} `, el("em", {}, r.relation), ` ${r.object} `, sourceLink(r.source_title, r.source_revision)),
      ),
    ),
  );
  const groups = new Map();
  for (const n of neighbours) {
    const type = ["person", "organization", "place"].includes(n.type) ? n.type : "other";
    if (!groups.has(type)) groups.set(type, []);
    groups.get(type).push(n);
  }
  nodes.push(el("h5", {}, `In the same events (${neighbours.length})`));
  for (const type of ["person", "organization", "place", "other"]) {
    const members = groups.get(type);
    if (!members) continue;
    const label = { person: "People", organization: "Organisations", place: "Places", other: "Other" }[type];
    nodes.push(
      el(
        "p",
        { class: "neighbours" },
        el("strong", {}, `${label}: `),
        ...members
          .sort((a, b) => a.entity_name.localeCompare(b.entity_name))
          .map((n) => button(n.entity_name, () => showEntity(n.entity_key), { class: "chip" })),
      ),
    );
  }
  return nodes;
}

// 3. Connection finder ------------------------------------------------------------------------
// "Connect from/to here": fill one end, then search straight away if the other end names a
// different entity; otherwise empty it and put the cursor there for the visitor to type.
function setPathEnd(id, name) {
  const otherId = id === "path-from" ? "path-to" : "path-from";
  $(id).value = name;
  $("path-heading").scrollIntoView({ behavior: "smooth", block: "start" });
  const other = $(otherId);
  if (other.value.trim() && fold(other.value) !== fold(name)) {
    $("path-form").requestSubmit();
    return;
  }
  other.value = "";
  other.placeholder = "Type a name, then Find connections";
  other.focus({ preventScroll: true });
}

async function resolveEntity(name) {
  const terms = luceneTerms(name);
  if (!terms) return null;
  const found = objects(await client.named("SEARCH_ENTITIES", { query: terms, limit: 20 }));
  const wanted = fold(name);
  const exact = found.find((e) => fold(e.name) === wanted || e.aliases.some((a) => fold(a) === wanted));
  return exact || found[0] || null;
}

async function findPath(event) {
  event.preventDefault();
  const box = $("path-results");
  box.replaceChildren(el("p", { class: "muted" }, "Looking for connections..."));
  try {
    const from = await resolveEntity($("path-from").value);
    const to = await resolveEntity($("path-to").value);
    if (!from || !to) {
      box.replaceChildren(el("p", { class: "error" }, `No entity found for “${!from ? $("path-from").value : $("path-to").value}”.`));
      return;
    }
    const found = objects(await client.named("PATHS", { a: from.key, b: to.key, limit: 3 }));
    const intro = el("p", { class: "muted" }, `From ${from.name} to ${to.name}, at most ${client.meta.max_hops} steps:`);
    if (!found.length) {
      box.replaceChildren(intro, el("p", {}, `No connection within ${client.meta.max_hops} steps.`));
      return;
    }
    box.replaceChildren(
      intro,
      el(
        "ol",
        { class: "paths" },
        ...found.map((path) =>
          el(
            "li",
            {},
            el("p", { class: "path-names" }, path.names.join(" → ")),
            el(
              "ul",
              {},
              ...path.steps.map((s) =>
                el("li", {}, `${s.start} `, el("em", {}, s.label), ` ${s.end} `, sourceLink(s.source_title, s.source_revision)),
              ),
            ),
          ),
        ),
      ),
    );
  } catch (error) {
    box.replaceChildren(el("p", { class: "error" }, `Could not search: ${error.message}`));
  }
}

// 4. Prepared questions -----------------------------------------------------------------------
function verdictOf(correctness) {
  const scores = Object.values(correctness);
  if (scores.length && scores.every((s) => s >= 2)) return ["correct", "good"];
  if (scores.length && scores.every((s) => s <= 0)) return ["wrong", "bad"];
  return ["partly", ""];
}

function renderPrepared(prepared) {
  const list = $("prepared-list");
  list.replaceChildren(
    ...prepared.map((q) => {
      const [verdict, badge] = verdictOf(q.correctness);
      const judged = Object.entries(q.correctness)
        .map(([judge, s]) => `${judge.replace("ollama:", "")} ${s}/2`)
        .join(", ");
      const live = el("div", { class: "live", id: `live-${q.id}`, "aria-live": "polite" });
      return el(
        "details",
        { "data-id": q.id },
        el("summary", {}, el("span", { class: `badge ${badge}` }, verdict), q.question),
        el("p", { class: "muted" }, "The Cypher the LLM wrote for it:"),
        el("pre", {}, el("code", {}, q.cypher)),
        el("p", { class: "actions" }, button("Run it", () => runPrepared(q), { class: "run" })),
        live,
        el("h5", {}, "WikiPulse's answer"),
        el("p", { class: "answer-text" }, q.answer),
        q.sources.length
          ? el("ol", { class: "sources" }, ...q.sources.map((s) => el("li", {}, s.url ? link(s.url, s.title) : s.title)))
          : null,
        el("p", { class: "muted" }, `Answered by ${(q.model || "no model").replace("ollama:", "")}. Judges: ${judged}.`),
      );
    }),
  );
}

async function runPrepared(q) {
  const box = $(`live-${q.id}`);
  box.replaceChildren(el("p", { class: "muted" }, "Running..."));
  // The same check and row cap as "Try your own Cypher". The comparison with Neo4j's
  // rows (GraphDb.runPrepared) is a developer test (tests/js/graph_check.mjs), not shown.
  const reply = await client.run({ op: "cypher", text: q.cypher, maxRows: client.meta.max_rows });
  const outcome = cypherOutcome(reply);
  box.dataset.outcome = outcome.ok ? "rows" : "error";
  if (!outcome.ok) {
    box.replaceChildren(outcome.node);
    return;
  }
  const { columns, rows } = reply.result;
  box.replaceChildren(
    el("p", { class: "muted" }, `${rows.length} row${rows.length === 1 ? "" : "s"} in ${Math.round(reply.elapsed)} ms, live in your browser:`),
    table(columns, rows),
  );
}

// 5. Your own Cypher --------------------------------------------------------------------------
function cypherOutcome(reply) {
  if (reply.restartFailed) {
    const why = reply.timedOut ? `Stopped after ${QUERY_LIMIT_MS / 1000} s` : "The database crashed";
    return { ok: false, node: el("p", { class: "error" }, `${why}, and the graph database could not restart: reload the page.`) };
  }
  if (reply.timedOut) {
    return {
      ok: false,
      node: el("p", { class: "error" }, `Stopped after ${QUERY_LIMIT_MS / 1000} s: WebAssembly can't cancel a query, so the database was restarted and the graph reloaded.`),
    };
  }
  if (!reply.ok) return { ok: false, node: el("p", { class: "error" }, `The database failed: ${reply.error}`) };
  if (reply.result.refused) {
    return { ok: false, node: el("p", { class: "error" }, `Refused by WikiPulse's read-only check: ${reply.result.refused}.`) };
  }
  if (reply.result.error) return { ok: false, node: el("p", { class: "error" }, `Relate: ${reply.result.error}`) };
  return { ok: true };
}

async function runOwn(event) {
  event.preventDefault();
  const box = $("cypher-results");
  box.replaceChildren(el("p", { class: "muted" }, "Running..."));
  const reply = await client.run({ op: "cypher", text: $("cypher-text").value });
  const outcome = cypherOutcome(reply);
  box.dataset.outcome = reply.timedOut ? "timeout" : reply.result?.refused ? "refused" : outcome.ok ? "rows" : "error";
  if (!outcome.ok) {
    box.replaceChildren(outcome.node);
    return;
  }
  const { columns, rows, capped, query } = reply.result;
  const changed = query !== $("cypher-text").value.trim().replace(/;+$/, "");
  box.replaceChildren(
    el(
      "p",
      { class: "muted" },
      `${rows.length} row${rows.length === 1 ? "" : "s"} in ${Math.round(reply.elapsed)} ms` +
        (capped ? `, cut at ${rows.length}` : "") +
        (changed ? "; the read-only check set the final LIMIT, so it ran as:" : "."),
    ),
    ...(changed ? [el("pre", {}, el("code", {}, query))] : []),
    table(columns, rows),
  );
}

// --- Start ----------------------------------------------------------------------------------
function setUp() {
  $("graph-start").addEventListener("click", openExplorer);
  $("graph-search-form").addEventListener("submit", search);
  $("path-form").addEventListener("submit", findPath);
  $("cypher-form").addEventListener("submit", runOwn);
  for (const node of document.querySelectorAll("[data-example]")) {
    node.addEventListener("click", () => {
      $("cypher-text").value = EXAMPLES[node.dataset.example];
      $("cypher-text").focus();
    });
  }
  $("cypher-text").value = EXAMPLES.relations;
  if (new URLSearchParams(location.search).get("graph") === "1") openExplorer();
}

setUp();
