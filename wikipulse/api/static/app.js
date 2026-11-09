// WikiPulse live page. Plain JavaScript, no build step, nothing loaded from outside.
//
// - The WebSocket /ws/events sends a snapshot on connect, then every new or changed
//   event; the list updates in place (no refresh). It reconnects by itself.
// - Clicking an event loads GET /events/{id}; it reloads when that event changes.
// - The question box posts to /ask.
//
// Everything that comes from the data (Wikipedia titles, model text) is written with
// textContent, never as HTML, and only Wikipedia links become clickable.

"use strict";

const MAX_EVENTS = 400;
const FLASH_MS = 1500;
const NEW_BADGE_MS = 120000; // an event that arrived live is marked "new" for 2 minutes
const WIKIPEDIA = "https://en.wikipedia.org/";

const state = {
  events: new Map(), // event_id -> card from the API
  fresh: new Map(), // event id -> when it arrived live (not in a snapshot)
  selected: null,
  socket: null,
  retryMs: 1000,
};

const $ = (id) => document.getElementById(id);

// --- Small DOM helpers ---------------------------------------------------------------------
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

function link(url, text) {
  if (typeof url === "string" && url.startsWith(WIKIPEDIA)) {
    return el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text);
  }
  return document.createTextNode(text);
}

function time(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const month = d.toLocaleString("en", { month: "short", timeZone: "UTC" });
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${month} ${d.getUTCDate()}, ${hh}:${mm}`;
}

// Text with [n] markers -> text nodes and small links to source n.
function cited(text, sources, className) {
  const node = el("p", { class: className });
  const parts = text.split(/(\[\d+\])/);
  for (const part of parts) {
    const match = part.match(/^\[(\d+)\]$/);
    const source = match ? sources[Number(match[1]) - 1] : null;
    if (source && source.url && source.url.startsWith(WIKIPEDIA)) {
      const a = link(source.url, `[${match[1]}]`);
      a.className = "cite";
      a.title = source.title;
      node.append(a);
    } else if (source) {
      node.append(el("span", { class: "cite", title: source.title }, part)); // no link
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
        link(s.url, s.title),
        s.revision ? el("span", { class: "muted" }, `, revision ${s.revision}`) : null,
      ),
    ),
  );
}

// --- The event list ------------------------------------------------------------------------
function order(a, b) {
  const active = (b.status === "active") - (a.status === "active");
  if (active) return active;
  if (a.last_spike_at !== b.last_spike_at) return a.last_spike_at < b.last_spike_at ? 1 : -1;
  return b.number - a.number;
}

function visible(card) {
  if (card.status === "merged") return false;
  if (card.page_count < Number($("min-pages").value)) return false;
  if ($("active-only").checked && card.status !== "active") return false;
  return true;
}

function isNew(eventId) {
  const arrived = state.fresh.get(eventId);
  return arrived !== undefined && Date.now() - arrived < NEW_BADGE_MS;
}

function cardNode(card) {
  const pages = card.page_count === 1 ? "1 page" : `${card.page_count} pages`;
  const spikes = card.spike_count !== card.page_count ? `, ${card.spike_count} spikes` : "";
  const when = card.ended_at
    ? `${time(card.started_at)} to ${time(card.ended_at)}`
    : `since ${time(card.started_at)}`;
  const node = el(
    "li",
    { class: "card", tabindex: "0", "data-id": card.event_id },
    el(
      "h3",
      {},
      card.status === "active" ? el("span", { class: "badge active" }, "active") : null,
      isNew(card.event_id) ? el("span", { class: "badge new" }, "new") : null,
      card.title,
    ),
    card.description ? el("p", { class: "description" }, card.description) : null,
    el(
      "p",
      { class: "meta" },
      `#${card.number} · ${pages}${spikes} · score ${card.spike_score} · ${when}`,
      card.extracted ? ` · ${card.entity_count} entities` : " · not in the graph yet",
      card.has_briefing ? " · briefing" : "",
    ),
    el("p", { class: "pages" }, card.top_pages.join(" · ")),
  );
  if (card.event_id === state.selected) node.classList.add("selected");
  node.addEventListener("click", () => select(card.event_id));
  node.addEventListener("keydown", (e) => {
    if (e.key === "Enter") select(card.event_id);
  });
  return node;
}

function render(flashIds = []) {
  const cards = [...state.events.values()].filter(visible).sort(order);
  const list = $("event-list");
  list.replaceChildren(...cards.map(cardNode));
  $("event-count").textContent = `(${cards.length})`;
  $("events-empty").hidden = cards.length > 0;
  for (const id of flashIds) {
    const node = list.querySelector(`[data-id="${CSS.escape(id)}"]`);
    if (!node) continue;
    node.classList.add("flash");
    setTimeout(() => node.classList.remove("flash"), FLASH_MS);
  }
}

function trim() {
  if (state.events.size <= MAX_EVENTS) return;
  const oldest = [...state.events.values()].sort(order).slice(MAX_EVENTS);
  for (const card of oldest) state.events.delete(card.event_id);
}

function setAsOf(iso) {
  if (iso) $("as-of").textContent = `data up to ${time(iso)} UTC`;
}

// --- Live updates --------------------------------------------------------------------------
function onMessage(message) {
  if (message.type === "snapshot") {
    state.events = new Map(message.events.map((card) => [card.event_id, card]));
    setAsOf(message.as_of);
    render();
  } else if (message.type === "event") {
    const card = message.event;
    if (message.change === "new") state.fresh.set(card.event_id, Date.now());
    state.events.set(card.event_id, card);
    trim();
    setAsOf(message.as_of);
    render([card.event_id]);
    if (card.event_id === state.selected) loadDetail(card.event_id);
  }
}

function setConnection(name) {
  const node = $("connection");
  node.dataset.state = name;
  node.textContent = name;
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws/events`);
  state.socket = socket;
  socket.addEventListener("open", () => {
    setConnection("live");
    state.retryMs = 1000;
  });
  socket.addEventListener("message", (e) => {
    try {
      onMessage(JSON.parse(e.data));
    } catch (error) {
      console.error("Bad message", error);
    }
  });
  socket.addEventListener("close", () => {
    setConnection("reconnecting");
    setTimeout(connect, state.retryMs);
    state.retryMs = Math.min(state.retryMs * 2, 30000);
  });
}

// --- Event detail --------------------------------------------------------------------------
async function select(eventId) {
  state.selected = eventId;
  render();
  await loadDetail(eventId);
}

async function loadDetail(eventId) {
  const box = $("detail");
  try {
    const response = await fetch(`/events/${encodeURIComponent(eventId)}`);
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || response.statusText);
    if (state.selected === eventId) box.replaceChildren(...detailNodes(body));
  } catch (error) {
    box.replaceChildren(el("p", { class: "error" }, `Could not load the event: ${error.message}`));
  }
}

function detailNodes(detail) {
  const ev = detail.event;
  const nodes = [
    el("h3", {}, ev.title, el("span", { class: "muted" }, `  (${ev.event_id}, ${ev.status})`)),
  ];
  if (ev.description) nodes.push(el("p", {}, ev.description));
  if (detail.note) nodes.push(el("p", { class: "muted" }, detail.note));

  if (detail.briefing) {
    const b = detail.briefing;
    nodes.push(el("h3", {}, "Briefing"));
    nodes.push(cited(b.text, b.sources, "briefing-text"));
    if (b.verified === false) {
      nodes.push(el("p", { class: "muted" }, "Not checked against its sources."));
    }
    nodes.push(sourceList(b.sources));
  } else if (ev.extracted) {
    nodes.push(el("p", { class: "muted" }, "No briefing yet."));
  }

  nodes.push(el("h3", {}, `Wikipedia pages (${detail.pages.length})`));
  nodes.push(
    el(
      "ul",
      {},
      ...detail.pages.map((p) =>
        el(
          "li",
          {},
          link(p.url, p.title),
          el(
            "span",
            { class: "muted" },
            ` score ${p.spike_score}${p.spikes > 1 ? `, ${p.spikes} spikes` : ""}`,
          ),
        ),
      ),
    ),
  );

  if (detail.entities.length) {
    nodes.push(el("h3", {}, `Entities (${detail.entities.length}${detail.more_entities ? "+" : ""})`));
    nodes.push(
      el(
        "ul",
        {},
        ...detail.entities.map((e) =>
          el(
            "li",
            {},
            e.name,
            el("span", { class: "muted" }, ` ${e.type || ""} `),
            e.source ? link(e.source.url, "[source]") : null,
          ),
        ),
      ),
    );
  }
  if (detail.relations.length) {
    nodes.push(el("h3", {}, `Relations (${detail.relations.length}${detail.more_relations ? "+" : ""})`));
    nodes.push(
      el(
        "ul",
        {},
        ...detail.relations.map((r) =>
          el("li", {}, `${r.subject} ${r.relation} ${r.object} `, link(r.source.url, "[source]")),
        ),
      ),
    );
  }
  return nodes;
}

// --- Questions -----------------------------------------------------------------------------
async function ask(event) {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  const button = $("ask-button");
  const answer = $("answer");
  button.disabled = true;
  answer.replaceChildren(el("p", { class: "muted" }, "Thinking... (usually 5 to 60 seconds)"));
  try {
    const response = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(errorText(body) || response.statusText);
    const nodes = [cited(body.answer, body.sources, "answer-text")];
    if (body.sources.length) nodes.push(sourceList(body.sources));
    for (const note of body.notes) nodes.push(el("p", { class: "muted" }, note));
    if (body.seconds) nodes.push(el("p", { class: "muted" }, `${body.seconds.toFixed(1)} s`));
    answer.replaceChildren(...nodes);
  } catch (error) {
    answer.replaceChildren(el("p", { class: "error" }, error.message));
  } finally {
    button.disabled = false;
  }
}

// FastAPI's errors: {"detail": "text"} or {"detail": [{"msg": ...}, ...]} for validation.
function errorText(body) {
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) return body.detail.map((d) => d.msg).join("; ");
  return "";
}

// --- Start ---------------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  $("min-pages").addEventListener("change", () => render());
  $("active-only").addEventListener("change", () => render());
  $("ask-form").addEventListener("submit", ask);
  $("question").addEventListener("input", (e) => {
    $("question-count").textContent = `${e.target.value.length} / 500`;
  });
  connect();
});
