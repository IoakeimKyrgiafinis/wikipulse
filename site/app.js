// WikiPulse demo: replays one recorded day in the browser. Plain JavaScript, no build
// step, nothing loaded from outside (data/day.json comes from the same site).
//
// The data is the day as WikiPulse stored it (scripts/export_demo.py): every event with
// its pages, each page with its spikes (start and end in event time), and, for the
// events the graph extraction processed, a headline, entities, relations and sometimes a
// briefing. The replay clock decides what is visible: an event appears when its first
// spike starts, a page joins when its first spike starts, and an event is "active" until
// it ends. The same rules as the live page, computed from the recording instead of pushed
// over a WebSocket.
//
// Everything from the data (Wikipedia titles, model text) is written with textContent,
// never as HTML, and only Wikipedia and Wikidata links become clickable.

"use strict";

const DATA_URL = "data/day.json";
const START_AT = 16 * 3600; // the replay starts at 16:00 UTC, when the graph data begins
const MAX_CARDS = 200; // most cards drawn at once
const FIRST_CARDS = 6; // latest events shown before "Show more"
const MORE_CARDS = 20; // cards each "Show more" adds
const NEW_S = 20 * 60; // an event is "new" for 20 minutes of event time
const FLASH_MS = 1500;
const MAX_GRAPH_NODES = 14;
const WIKIPEDIA = "https://en.wikipedia.org/";
const WIKIDATA = "https://www.wikidata.org/wiki/";
const SVG = "http://www.w3.org/2000/svg";

const state = {
  day: null,
  events: [], // prepared events (see prepare())
  t: START_AT, // seconds since the start of the day
  playing: false,
  speed: 300,
  lastFrame: null,
  selected: null,
  signatures: new Map(), // event number -> what the card showed last time
  detailSignature: "",
  extraCards: 0, // cards shown beyond the first ones ("Show more")
};

const $ = (id) => document.getElementById(id);

// --- Small helpers -------------------------------------------------------------------------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function link(url, text) {
  const safe = typeof url === "string" && (url.startsWith(WIKIPEDIA) || url.startsWith(WIKIDATA));
  if (safe) return el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text);
  return document.createTextNode(text);
}

function revisionUrl(revision) {
  return `${WIKIPEDIA}w/index.php?oldid=${encodeURIComponent(revision)}`;
}

function sourceLink(title, revision, text = "[source]") {
  if (!title) return null;
  const url = revision ? revisionUrl(revision) : `${WIKIPEDIA}wiki/${encodeURIComponent(title.replaceAll(" ", "_"))}`;
  const a = link(url, text);
  if (a.nodeType === Node.ELEMENT_NODE) a.title = revision ? `${title}, revision ${revision}` : title;
  return a;
}

const pad = (n) => String(n).padStart(2, "0");

function clockText(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s > 86400) return `${clockText(s - 86400)} the next day`;
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}`;
}

// ISO text -> seconds since the start of the day (Infinity for "never").
function secondsOf(iso) {
  if (!iso) return Infinity;
  return (Date.parse(iso) - Date.parse(state.day.start)) / 1000;
}

// Text with [n] markers -> text nodes and small links to source n.
function cited(text, sources, className) {
  const node = el("p", { class: className });
  for (const part of text.split(/(\[\d+\])/)) {
    const match = part.match(/^\[(\d+)\]$/);
    const source = match ? sources[Number(match[1]) - 1] : null;
    if (source && source.url) {
      const a = link(source.url, part);
      if (a.nodeType === Node.ELEMENT_NODE) {
        a.className = "cite";
        a.title = source.title;
      }
      node.append(a);
    } else if (source) {
      node.append(el("span", { class: "cite", title: source.title }, part));
    } else {
      node.append(part);
    }
  }
  return node;
}

function sourceList(sources) {
  return el(
    "ol",
    { class: "sources" },
    ...sources.map((s) =>
      el(
        "li",
        {},
        s.url ? link(s.url, s.title) : s.title,
        s.revision ? el("span", { class: "muted" }, `, revision ${s.revision}`) : null,
      ),
    ),
  );
}

// --- The replay model ----------------------------------------------------------------------
// Times become seconds since the start of the day, once, so each frame is cheap.
function prepare(day) {
  return day.events.map((ev) => {
    const pages = ev.pages.map((page) => {
      const spikes = page.spikes.map((s) => ({ ...s, s: secondsOf(s.start), e: secondsOf(s.end) }));
      return { ...page, spikes, join: Math.min(...spikes.map((s) => s.s)) };
    });
    pages.sort((a, b) => a.join - b.join);
    return { ...ev, pages, start: secondsOf(ev.started_at), end: secondsOf(ev.ended_at) };
  });
}

// What an event looks like at time t (null: not detected yet).
function viewAt(ev, t) {
  if (t < ev.start) return null;
  const pages = ev.pages.filter((p) => p.join <= t);
  const spikes = pages.flatMap((p) => p.spikes.filter((s) => s.s <= t));
  const active = t < ev.end;
  return {
    ev,
    pages,
    spikeCount: spikes.length,
    lastSpike: Math.max(...spikes.map((s) => s.s)),
    active,
    score: Math.max(...spikes.map((s) => s.score)),
  };
}

function order(a, b) {
  if (a.active !== b.active) return a.active ? -1 : 1;
  if (a.lastSpike !== b.lastSpike) return b.lastSpike - a.lastSpike;
  return b.ev.number - a.ev.number;
}

function visible(view) {
  if (view.pages.length < Number($("min-pages").value)) return false;
  if ($("graph-only").checked && !view.ev.headline) return false;
  if ($("active-only").checked && !view.active) return false;
  return true;
}

// --- The event list ------------------------------------------------------------------------
function cardNode(view, t) {
  const ev = view.ev;
  const pages = view.pages.length === 1 ? "1 page" : `${view.pages.length} pages`;
  const spikes = view.spikeCount !== view.pages.length ? `, ${view.spikeCount} spikes` : "";
  const when = view.active
    ? `since ${clockText(ev.start)}`
    : `${clockText(ev.start)} to ${clockText(ev.end)}`;
  const node = el(
    "li",
    { class: "card", tabindex: "0", "data-id": String(ev.number) },
    el(
      "h3",
      {},
      view.active ? el("span", { class: "badge active" }, "active") : null,
      t - ev.start < NEW_S ? el("span", { class: "badge new" }, "new") : null,
      ev.headline || ev.title,
    ),
    ev.description ? el("p", { class: "description" }, ev.description) : null,
    el(
      "p",
      { class: "meta" },
      `#${ev.number} · ${pages}${spikes} · score ${view.score.toFixed(1)} · ${when}`,
      ev.headline ? ` · ${ev.entities.length} entities` : " · not in the graph",
      ev.briefing ? " · briefing" : "",
    ),
    el("p", { class: "pages" }, view.pages.slice(0, 5).map((p) => p.title).join(" · ")),
  );
  if (ev.number === state.selected) node.classList.add("selected");
  node.addEventListener("click", () => select(ev.number));
  node.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      select(ev.number);
    }
  });
  return node;
}

function signature(view) {
  return `${view.pages.length}/${view.spikeCount}/${view.active}`;
}

// Redraw the list if anything visible changed; flash cards that are new or grew, unless
// the change came from moving the slider (flash = true only while playing).
function renderList(force = false, flash = false) {
  const t = state.t;
  const views = [];
  for (const ev of state.events) {
    const view = viewAt(ev, t);
    if (view) views.push(view);
  }
  const changed = [];
  const next = new Map();
  for (const view of views) {
    const sig = signature(view);
    next.set(view.ev.number, sig);
    if (state.signatures.get(view.ev.number) !== sig) changed.push(view.ev.number);
  }
  const removed = state.signatures.size !== next.size;
  state.signatures = next;
  updateProgress(views);
  if (!force && !changed.length && !removed) return;

  const shown = views.filter(visible).sort(order);
  const list = $("event-list");
  list.replaceChildren(...shown.slice(0, MAX_CARDS).map((v) => cardNode(v, t)));
  $("event-count").textContent = `(${shown.length})`;
  $("events-empty").hidden = shown.length > 0;
  const more = $("events-more");
  more.hidden = shown.length <= MAX_CARDS;
  more.textContent = `Showing the ${MAX_CARDS} most recent of ${shown.length}; the filters narrow the list.`;
  if (flash) {
    for (const number of changed) {
      const node = list.querySelector(`[data-id="${number}"]`);
      if (!node) continue;
      node.classList.add("flash");
      setTimeout(() => node.classList.remove("flash"), FLASH_MS);
    }
  }
  if (state.selected !== null) renderDetail();
  fitList();
}

// The 6 latest events show; older cards hide behind "Show more", so the list doesn't
// stretch the page.
function fitList() {
  const cards = [...$("event-list").children];
  const shown = Math.min(cards.length, FIRST_CARDS + state.extraCards);
  cards.forEach((card, i) => {
    card.hidden = i >= shown && card.dataset.id !== String(state.selected);
  });
  const hiddenCount = cards.filter((card) => card.hidden).length;
  $("events-fold").hidden = hiddenCount === 0 && state.extraCards === 0;
  $("events-show-more").hidden = hiddenCount === 0;
  $("events-show-more").textContent = `Show more (${hiddenCount} older)`;
  $("events-show-fewer").hidden = state.extraCards === 0;
}

function showMoreCards(more) {
  state.extraCards = more ? state.extraCards + MORE_CARDS : 0;
  fitList();
  if (!more) $("events-heading").scrollIntoView({ block: "nearest" });
}

function updateProgress(views) {
  const multi = views.filter((v) => v.pages.length > 1).length;
  $("progress").textContent = `${views.length} events so far, ${multi} with 2+ pages`;
}

// --- Event detail --------------------------------------------------------------------------
function select(number) {
  state.selected = number;
  state.detailSignature = "";
  for (const node of document.querySelectorAll(".card.selected")) node.classList.remove("selected");
  const card = $("event-list").querySelector(`[data-id="${number}"]`);
  if (card) card.classList.add("selected");
  renderDetail();
  if (window.matchMedia("(max-width: 800px)").matches) {
    $("detail-heading").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function renderDetail() {
  const ev = state.events.find((e) => e.number === state.selected);
  if (!ev) return;
  const view = viewAt(ev, state.t);
  const sig = view ? signature(view) : "later";
  if (sig === state.detailSignature) return;
  state.detailSignature = sig;
  $("detail").replaceChildren(...detailNodes(ev, view));
}

function detailNodes(ev, view) {
  const status = !view ? "not detected yet" : view.active ? "active" : "ended";
  const nodes = [
    el("h3", {}, ev.headline || ev.title, el("span", { class: "muted" }, `  (#${ev.number}, ${status})`)),
  ];
  if (ev.description) nodes.push(el("p", {}, ev.description));
  nodes.push(
    el(
      "p",
      { class: "muted" },
      Number.isFinite(ev.end)
        ? `Detected at ${clockText(ev.start)} UTC, ended at ${clockText(ev.end)}.`
        : `Detected at ${clockText(ev.start)} UTC, still active when the recording ends.`,
    ),
  );
  if (!ev.headline) {
    nodes.push(
      el(
        "p",
        { class: "muted" },
        "Not in the knowledge graph: on this day the graph extraction ran for the events " +
          "that started between 16:00 and 23:00 UTC.",
      ),
    );
  }

  if (ev.briefing) nodes.push(...briefingNodes(ev.briefing));

  const joined = view ? view.pages : [];
  const later = ev.pages.length - joined.length;
  nodes.push(el("h3", {}, `Wikipedia pages (${joined.length}${later ? ` of ${ev.pages.length}` : ""})`));
  nodes.push(el("ul", {}, ...joined.map((p) => pageItem(p))));
  if (later) {
    nodes.push(el("p", { class: "later" }, `${later} more page${later > 1 ? "s join" : " joins"} later in the day.`));
  }

  if (ev.headline) {
    nodes.push(
      el(
        "p",
        { class: "muted" },
        "Headline, entities and relations were extracted from the event's final form, after the " +
          "day; live, they appear a few minutes after detection and update as pages join.",
      ),
    );
  }
  if (ev.relations.length) {
    nodes.push(el("h3", {}, `Relations (${ev.relations.length})`));
    nodes.push(graphNode(ev));
    nodes.push(
      el(
        "ul",
        {},
        ...ev.relations.map((r) =>
          el("li", {}, `${r.subject} `, el("em", {}, r.relation), ` ${r.object} `, sourceLink(r.source, r.revision)),
        ),
      ),
    );
  }
  if (ev.entities.length) {
    nodes.push(el("h3", {}, `Entities (${ev.entities.length})`));
    nodes.push(
      el(
        "ul",
        {},
        ...ev.entities.map((e) =>
          el(
            "li",
            {},
            e.name,
            el("span", { class: "muted" }, ` ${e.type || ""} `),
            e.wikidata_id ? link(WIKIDATA + encodeURIComponent(e.wikidata_id), e.wikidata_id) : null,
            " ",
            sourceLink(e.source, e.revision),
          ),
        ),
      ),
    );
  }
  return nodes;
}

function pageItem(page) {
  const edits = page.spikes.reduce((sum, s) => sum + s.edits, 0);
  const editors = Math.max(...page.spikes.map((s) => s.editor_count));
  const bits = [
    `joined ${clockText(page.join)}`,
    `${edits} edits by ${editors}+ people`,
    page.spikes.length > 1 ? `${page.spikes.length} spikes` : null,
  ].filter(Boolean);
  return el(
    "li",
    {},
    link(page.url, page.title),
    page.description ? el("span", { class: "muted" }, `: ${page.description}`) : null,
    el("span", { class: "muted" }, ` (${bits.join(", ")})`),
    page.revision ? " " : null,
    page.revision ? sourceLink(page.title, page.revision, "[revision read]") : null,
  );
}

function briefingNodes(briefing) {
  const nodes = [el("h3", {}, "Briefing (analyst agent)")];
  nodes.push(cited(briefing.text, briefing.sources, "briefing-text"));
  const checked = briefing.verified ? "every sentence checked against its source" : "not checked";
  nodes.push(el("p", { class: "muted" }, `Written by ${briefing.model || "an LLM"}; ${checked}.`));
  nodes.push(sourceList(briefing.sources));
  return nodes;
}

// A small drawing of the relations: entities on a circle, one line per relation. Only
// the best-connected entities when there are many; the full list follows below it.
function graphNode(ev) {
  const degree = new Map();
  for (const r of ev.relations) {
    for (const name of [r.subject, r.object]) degree.set(name, (degree.get(name) || 0) + 1);
  }
  const names = [...degree.keys()].sort((a, b) => degree.get(b) - degree.get(a)).slice(0, MAX_GRAPH_NODES);
  const types = new Map(ev.entities.map((e) => [e.name, e.type || "other"]));
  const width = 560;
  const height = 290;
  const cx = width / 2;
  const cy = height / 2;
  const radius = names.length > 2 ? 105 : 70;
  const at = new Map(
    names.map((name, i) => {
      const angle = (2 * Math.PI * i) / names.length - Math.PI / 2;
      return [name, { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle), angle }];
    }),
  );
  const drawing = svg("svg", {
    class: "graph",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `Graph of ${ev.relations.length} relations between ${degree.size} entities`,
  });
  for (const r of ev.relations) {
    const a = at.get(r.subject);
    const b = at.get(r.object);
    if (!a || !b) continue;
    const line = svg("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y });
    const title = svg("title");
    title.textContent = `${r.subject} ${r.relation} ${r.object}`;
    line.append(title);
    drawing.append(line);
  }
  for (const name of names) {
    const p = at.get(name);
    const type = ["person", "organization", "place", "event"].includes(types.get(name)) ? types.get(name) : "other";
    const dot = svg("circle", { cx: p.x, cy: p.y, r: 6, class: type });
    const title = svg("title");
    title.textContent = `${name} (${types.get(name) || "entity"})`;
    dot.append(title);
    drawing.append(dot);
    const right = Math.cos(p.angle) >= -0.01;
    const label = svg("text", {
      x: p.x + (right ? 9 : -9),
      y: p.y + 4,
      "text-anchor": Math.abs(Math.cos(p.angle)) < 0.2 ? "middle" : right ? "start" : "end",
      dy: Math.abs(Math.cos(p.angle)) < 0.2 ? (Math.sin(p.angle) < 0 ? -8 : 12) : 0,
    });
    label.textContent = name.length > 26 ? `${name.slice(0, 25)}…` : name;
    drawing.append(label);
  }
  const legend = el(
    "p",
    { class: "legend muted" },
    ...["person", "organization", "place", "event", "other"].map((t) => el("span", { class: t }, `${t} `)),
    names.length < degree.size ? `(${names.length} of ${degree.size} entities drawn)` : null,
  );
  return el("div", {}, drawing, legend);
}

// --- The clock -----------------------------------------------------------------------------
function setTime(seconds, flash = false) {
  state.t = Math.max(0, Math.min(seconds, 86400));
  $("clock").textContent = `Nov ${new Date(state.day.start).getUTCDate()}, ${clockText(state.t)} UTC`;
  $("scrubber").value = String(Math.floor(state.t));
  renderList(false, flash);
}

function setPlaying(playing) {
  state.playing = playing;
  state.lastFrame = null;
  const button = $("play");
  button.textContent = playing ? "Pause" : state.t >= 86400 ? "Replay" : "Play";
  button.setAttribute("aria-pressed", String(playing));
  if (playing) requestAnimationFrame(frame);
}

function frame(now) {
  if (!state.playing) return;
  if (state.lastFrame !== null) {
    const elapsed = Math.min(now - state.lastFrame, 250) / 1000; // a hidden tab doesn't jump
    setTime(state.t + elapsed * state.speed, true);
    if (state.t >= 86400) {
      setPlaying(false);
      return;
    }
  }
  state.lastFrame = now;
  requestAnimationFrame(frame);
}

// --- Start ---------------------------------------------------------------------------------
function describeData(day) {
  const c = day.counts;
  $("day-label").textContent = new Date(day.start).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
  $("about-data").textContent =
    `This export: ${c.events} events from ${c.spikes} spikes (grouping run ${day.run_id}); ` +
    `${c.events_in_graph} of them in the knowledge graph with ${c.entities} entity mentions and ` +
    `${c.relations} relations (${day.extraction_model || "local LLM"}), ${c.briefings} with a ` +
    "briefing. Only counts of edits and editors are included, never who edited.";
}

async function start() {
  for (const id of ["min-pages", "graph-only", "active-only"]) {
    $(id).addEventListener("change", () => renderList(true));
  }
  try {
    const response = await fetch(DATA_URL);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    state.day = await response.json();
  } catch (error) {
    $("loading").textContent = `Could not load the recorded day: ${error.message}`;
    $("loading").className = "error";
    return;
  }
  $("loading").hidden = true;
  state.events = prepare(state.day);
  describeData(state.day);

  $("play").addEventListener("click", () => {
    if (!state.playing && state.t >= 86400) setTime(0);
    setPlaying(!state.playing);
  });
  $("speed").addEventListener("change", (e) => {
    state.speed = Number(e.target.value);
  });
  $("scrubber").addEventListener("input", (e) => setTime(Number(e.target.value)));
  $("events-show-more").addEventListener("click", () => showMoreCards(true));
  $("events-show-fewer").addEventListener("click", () => showMoreCards(false));
  document.addEventListener("keydown", (e) => {
    const typing = ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(e.target.tagName);
    if (e.key === " " && !typing && !e.target.classList.contains("card")) {
      e.preventDefault();
      $("play").click();
    }
  });

  setTime(START_AT);
  renderList(true);
  const params = new URLSearchParams(location.search);
  if (params.get("autoplay") !== "0") setPlaying(true);
}

document.addEventListener("DOMContentLoaded", start);
