"use strict";
const $ = (id) => document.getElementById(id);
const labels = {
  inProgress: "In progress",
  completed: "Completed",
  interrupted: "Interrupted",
  failed: "Failed",
};
function statusLabel(row) {
  if (row.status === "inProgress" && row.observation === "unavailable")
    return "Observer offline";
  if (row.status === "inProgress" && row.observation === "stopped")
    return "Recording stopped";
  return labels[row.status];
}
const axes = ["Journey", "Turn notes", "Project notes"];
const pageKeys = { "[": "earlier", "]": "later", "<": "first", ">": "last" };
const visible = new Set(axes);
const pageSize = 60;
let pageQuery = {};
let legacyNewerPages = [];
let activeAxis = 0;
let selectedJourneySession = null;
let journeyFilter = null;
let sessionControls = null;
let activeRequest = null;
let modalTrigger = null;
let detailRequest = null;
let expandedTurnPane = null;
const readCache = new Map();
const detailCache = new Map();
let last = "",
  version = 0;
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function action(text, fn, cls) {
  const el = node("button", text, cls);
  el.type = "button";
  el.onclick = () =>
    Promise.resolve().then(fn).catch(
      (err) => ($("connection").textContent = err.message),
    );
  return el;
}
class JournalError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}
async function api(path, params = {}, body, signal) {
  const res = await fetch(
    "/api/" + path + "?" + new URLSearchParams(params),
    {
      signal,
      ...(body ? {
          method: "POST",
          headers: { "Content-Type": "application/json; charset=utf-8" },
          body: JSON.stringify(body),
        } : {}),
    },
  );
  if (res.status === 401) {
    $("login").hidden = false;
    $("view").hidden = true;
    throw new JournalError(401, "Enter your access token");
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new JournalError(res.status, data.error || "Journal HTTP " + res.status);
  }
  return res.json();
}
function modal(title, child, transcriptView = false) {
  const dialog = $("detail");
  detailRequest?.abort();
  detailRequest = null;
  delete $("modalTitle").dataset.priority;
  dialog.classList.toggle("transcript-detail", transcriptView);
  $("modalTitle").textContent = title;
  $("modalBody").replaceChildren(child);
  dialog.onkeydown = null;
  if (!dialog.open) {
    modalTrigger = document.activeElement;
    dialog.showModal();
  }
}
function finishNoteEditing() {
  const editor = activeNoteEditor;
  activeNoteEditor = null;
  editor?.leaving();
}
function closeJournalDialog() {
  closeTurnPane();
  detailRequest?.abort();
  finishNoteEditing();
  $("detail").close();
}
function openTurnPane(title, pane, trigger) {
  if (expandedTurnPane) return;
  const dialog = $("turnPaneDetail");
  expandedTurnPane = {
    pane, parent: pane.parentElement, trigger, top: pane.scrollTop, left: pane.scrollLeft,
  };
  $("turnPaneTitle").textContent = title;
  dialog.append(pane);
  dialog.showModal();
  pane.focus({ preventScroll: true });
  pane.scrollTo(0, 0);
}
function closeTurnPane() {
  if (!expandedTurnPane) return;
  const { pane, parent, trigger, top, left } = expandedTurnPane;
  expandedTurnPane = null;
  gPending = false;
  $("turnPaneDetail").close();
  parent.append(pane);
  trigger.focus({ preventScroll: true });
  pane.scrollTo(left, top);
}
async function transcript(project, r) {
  const panes = node("div", undefined, "turn-detail-panes");
  function pane(title) {
    const section = node("section", undefined, "turn-detail-pane");
    section.setAttribute("aria-label", title);
    const scroll = node("div", undefined, "turn-detail-scroll");
    scroll.tabIndex = 0;
    scroll.setAttribute("aria-label", title);
    scroll.addEventListener("focus", () => { gPending = false; });
    const heading = node("div", undefined, "turn-detail-heading");
    const expand = action("View details", () => openTurnPane(title, scroll, expand), "small");
    heading.append(node("h2", title), expand);
    section.append(heading, scroll);
    panes.append(section);
    return scroll;
  }
  const summary = pane("Turn explanation");
  const box = pane("Transcript");
  summary.append(node("p", r.summary || "No explanation available."));
  box.append(node("p", "Loading transcript…"));
  modal("Turn details", panes, true);
  summary.focus({ preventScroll: true });
  const key = JSON.stringify([project, r.thread, r.turn, r.revision]);
  try {
    let detail = r.revision == null ? null : detailCache.get(key);
    if (!detail) {
      detailRequest = new AbortController();
      detail = await api("transcript", {
        project, thread: r.thread, turn: r.turn, with_summary: "true",
      }, undefined, detailRequest.signal);
      if (r.revision != null) {
        if (detailCache.size >= 6) detailCache.delete(detailCache.keys().next().value);
        detailCache.set(key, detail);
      }
    }
    if (!box.isConnected || !$("detail").open) return;
    const items = Array.isArray(detail) ? detail : detail.items;
    if (detail.summary_detail) summary.replaceChildren(renderMarkdown(detail.summary_detail));
    else if (detail.summary) summary.replaceChildren(node("p", detail.summary));
    box.replaceChildren();
    if (!items.length) box.append(node("p", "No transcript items."));
    function renderItems(start) {
      if (!box.isConnected || !$("detail").open) return;
      for (const item of items.slice(start, start + 8)) {
        box.append(node("h2", item.type), node("pre", item.text ??
          (item.aggregatedOutput ? (item.command ?? "") + "\n" + item.aggregatedOutput :
            JSON.stringify(item, null, 2))));
      }
      if (start + 8 < items.length) requestAnimationFrame(() => renderItems(start + 8));
    }
    renderItems(0);
  } catch (error) {
    if (box.isConnected && error.name !== "AbortError") box.textContent = error.message;
  }
}
function projectLink(k) {
  const a = node("a", k);
  a.href = "#" + encodeURIComponent(k);
  return a;
}
function noteView(project, note) {
  const el = node("div", undefined, "note");
  const title = displayTitle(note);
  const open = action(title, () => openNote(project, note), "note-title");
  styleNoteTitle(open, note);
  open.dataset.axis = note.turn ? "1" : "2";
  open.dataset.identity = note.id;
  el.append(open);
  const preview = note.preview ?? note.body;
  if (preview?.trim().normalize("NFC") !== title)
    el.append(node("div", preview, "note-excerpt"));
  el.append(action("Edit", () => editNote(project, {}, note), "small"));
  return el;
}
function overview(entries) {
  const out = node("div");
  for (const p of entries) {
    const rows = p.turns;
    const section = node("article", undefined, "project"),
      left = node("div"),
      right = node("div");
    left.append(projectLink(p.project));
    const live = rows.filter((r) => statusLabel(r) === labels.inProgress);
    left.append(
      node(
        "div",
        p.active
          ? live.length
            ? live.length + " in progress"
            : "Idle"
          : "Recording disabled",
        "meta",
      ),
    );
    const display = live.length ? live : rows.slice(-1);
    for (const r of display) {
      const item = node("p", r.prompt + " → " + r.summary);
      item.append(node("small", statusLabel(r), "meta"));
      right.append(item);
    }
    if (!display.length)
      right.append(node("small", "No recorded turns yet"));
    section.append(left, right);
    out.append(section);
  }
  if (!entries.length)
    out.append(
      node(
        "p",
        "Ask Codex to register a GitHub project with Agent Journal.",
        "empty",
      ),
    );
  return out;
}

function storageLink(project) {
  const link = node("a", project || "Storage");
  link.href = project ? "#stats?" + new URLSearchParams({ project }) : "#stats";
  return link;
}
function isStorageRoute() {
  return location.hash === "#stats" || location.hash.startsWith("#stats?");
}
function formatBytes(size) {
  for (const [scale, unit] of [[1e12, "TB"], [1e9, "GB"], [1e6, "MB"], [1e3, "KB"]]) {
    if (size >= scale) return (size / scale).toFixed(1) + " " + unit;
  }
  return size.toLocaleString() + " B";
}
function storageTable(title, headers, rows, empty) {
  const section = node("section", undefined, "storage-section");
  const heading = node("h2", title);
  section.append(heading);
  if (!rows.length) {
    section.append(node("p", empty, "empty"));
    return section;
  }
  const scroll = node("div", undefined, "storage-scroll");
  const table = node("table", undefined, "storage-table");
  table.setAttribute("aria-label", title);
  const header = table.createTHead().insertRow();
  for (const label of headers) {
    const cell = node("th", label);
    cell.scope = "col";
    header.append(cell);
  }
  const body = table.createTBody();
  for (const values of rows) {
    const row = body.insertRow();
    for (const value of values) row.insertCell().append(value);
  }
  scroll.append(table);
  section.append(scroll);
  return section;
}
function storageView(data, project) {
  const out = node("div");
  const totals = node("dl", undefined, "storage-totals");
  const { recent, projects, largest_turns } = data;
  const metrics = [
    [`Turns started in the last ${recent.days} days`, recent.turn_count.toLocaleString()],
    ["Data in those turns", formatBytes(recent.turn_bytes)],
  ];
  if (!project) {
    metrics.unshift(
      ["Database file", formatBytes(data.database_bytes)],
      ["WAL file", formatBytes(data.wal_bytes)],
    );
  }
  for (const [label, value] of metrics) {
    const metric = node("div");
    metric.append(node("dt", label), node("dd", value));
    totals.append(metric);
  }
  const date = (started) => started === null ? "–" : new Date(started * 1000).toLocaleString();
  out.append(
    totals,
    storageTable(
      project ? "Project usage" : "Storage by project",
      ["Project", "Turns", "Notes", "Record data", "Latest turn"],
      projects.map((row) => [
        project ? projectLink(row.project) : storageLink(row.project),
        row.turn_count.toLocaleString(),
        row.note_count.toLocaleString(),
        formatBytes(row.turn_bytes + row.note_bytes),
        date(row.latest_turn_started),
      ]),
      "No registered projects.",
    ),
    storageTable(
      "Largest turns",
      [...(project ? [] : ["Project"]), "Started", "Record data", "Transcript"],
      largest_turns.map((row) => [
        ...(project ? [] : [storageLink(row.project)]),
        date(row.started),
        formatBytes(row.body_bytes),
        action(row.summary || "Open transcript", () => transcript(row.project, row)),
      ]),
      "No recorded turns yet.",
    ),
  );
  return out;
}

const sessionColors = [
  "#26754e",
  "#425da9",
  "#a14728",
  "#7a4c98",
  "#0e737a",
  "#a13660",
  "#816024",
  "#496277",
];
function sessionColor(thread) {
  let index = 0;
  for (const char of thread)
    index = (index * 31 + char.codePointAt(0)) % sessionColors.length;
  return sessionColors[index];
}

function graphMark(className, lane, color) {
  const mark = node("span", undefined, className);
  mark.style.setProperty("--lane", lane);
  mark.style.setProperty("--session-color", color);
  return mark;
}

function graphGutter(row) {
  const graph = node("div", undefined, "journey-graph");
  graph.setAttribute("aria-hidden", "true");
  const { lane, tracks, forks } = row.graph_lanes;
  const color = sessionColor(row.thread);
  const left = Math.min(lane, ...forks);
  const right = Math.max(lane, ...forks);
  if (left < right) {
    const fork = graphMark("graph-fork", left, color);
    fork.style.setProperty("--fork-span", right - left);
    graph.append(fork);
  }
  for (const [track, thread, above, below] of tracks) {
    const ink = sessionColor(thread);
    if (above || below) {
      const side = above && below ? "through" : above ? "incoming" : "outgoing";
      graph.append(graphMark("graph-track " + side, track, ink));
    }
    if (above && below && track !== lane && left < track && track < right)
      graph.append(graphMark("graph-crossing", track, ink));
  }
  graph.append(graphMark("graph-point", lane, color));
  return graph;
}

function board(project, rows, notes) {
  const cols = axes.filter((x) => visible.has(x)),
    grid = node("div", undefined, "board");
  if (!cols.length) return node("p", "Select a column to show.", "empty");
  const journeyVisible = visible.has("Journey");
  const turnNotesVisible = visible.has("Turn notes");
  const paired = journeyVisible && turnNotesVisible;
  const hasGraph = journeyVisible && rows.length > 0;
  const recordsLayout = (paired ? " paired" : "") + (hasGraph ? " with-graph" : "");
  if (journeyVisible) {
    const laneCount = Math.max(
      1,
      ...rows.flatMap((r) => r.graph_lanes.tracks.map(([lane]) => lane + 1)),
    );
    grid.style.setProperty("--lane-count", laneCount);
  }
  function pane(names) {
    const journeyPane = names.includes("Journey");
    const section = node("section", undefined, journeyPane && hasGraph ? "board-pane graph-pane" : "board-pane");
    const head = node("div", undefined, "pane-head" + (journeyPane ? recordsLayout : ""));
    const scroll = node("div", undefined, "board-scroll");
    scroll.tabIndex = 0;
    scroll.dataset.pane = names.join("+");
    scroll.setAttribute("role", "region");
    scroll.setAttribute("aria-label", names.join(" and "));
    for (const name of names) {
      const label = node("div", undefined, name === "Journey" && hasGraph ? "head journey-head" : "head");
      label.append(node("h2", name));
      if (name === "Project notes")
        label.append(action("＋", () => editNote(project), "small"));
      head.append(label);
    }
    section.append(head, scroll);
    grid.append(section);
    return scroll;
  }
  const recordAxes = cols.filter(name => name !== "Project notes");
  const records = recordAxes.length ? pane(recordAxes) : null;
  const projectPane = visible.has("Project notes") ? pane(["Project notes"]) : null;
  if (records && projectPane) {
    grid.classList.add("with-project");
    grid.style.setProperty("--record-pane-width", paired ? "3fr" : journeyVisible ? "2fr" : "1fr");
  }
  const seenSessions = new Set();
  const sessionCards = new Map();
  function highlightSession(event) {
    if (journeyFilter) return;
    const thread = event.target.closest(".board-card")?.dataset.session;
    if (!thread || thread === selectedJourneySession) return;
    for (const card of sessionCards.get(selectedJourneySession) ?? [])
      card.classList.remove("selected-session");
    for (const card of sessionCards.get(thread) ?? [])
      card.classList.add("selected-session");
    selectedJourneySession = thread;
  }
  if (journeyVisible) {
    records.addEventListener("focusin", highlightSession);
    records.addEventListener("click", highlightSession);
  }
  const turnNotes = new Map();
  const projectNotes = [];
  for (const note of mergeDrafts(project, rows, notes)) {
    if (!note.turn) {
      projectNotes.push(note);
      continue;
    }
    const key = JSON.stringify([note.thread, note.turn]);
    if (!turnNotes.has(key)) turnNotes.set(key, []);
    turnNotes.get(key).push(note);
  }
  rows.forEach((r, i) => {
    if (!records) return;
    const card = node("article", undefined, "board-card" + recordsLayout);
    card.dataset.turn = r.turn;
    if (journeyVisible) {
      card.dataset.session = r.thread;
      card.style.setProperty("--session-color", sessionColor(r.thread));
      card.classList.toggle("selected-session", r.thread === (journeyFilter?.thread ?? selectedJourneySession));
      if (!sessionCards.has(r.thread)) sessionCards.set(r.thread, []);
      sessionCards.get(r.thread).push(card);
      const cell = node("div", undefined, "cell journey-cell"),
        button = action("", () => transcript(project, r), "rowbtn");
      button.dataset.axis = "0";
      button.dataset.identity = JSON.stringify([r.thread, r.turn]);
      const meta = node("div", undefined, "meta");
      const time = new Date(r.started * 1000).toLocaleString("en-US", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
      const session = node("span", r.thread.slice(-6), "session-id");
      session.title = "Session " + r.thread;
      meta.append(
        node("span", time),
        node("span", statusLabel(r), statusLabel(r) === labels.inProgress ? "live" : ""),
      );
      if (r.parent && !seenSessions.has(r.thread)) {
        const parentVisible = rows.slice(0, i).some(
          ({ thread, turn }) => thread === r.parent && turn === r.parent_turn,
        );
        const detail =
          r.parent_turn && !parentVisible ? " · Parent turn outside this page" : "";
        const fork = node(
          "span",
          "Fork of " + r.parent.slice(-6) + detail,
          "fork-context",
        );
        fork.title = "Parent session " + r.parent;
        meta.append(fork);
      }
      seenSessions.add(r.thread);
      const title = node("span", undefined, "journey-title");
      title.append(session, node("span", r.prompt, "req"));
      button.append(
        meta,
        title,
        node("span", r.summary, "result"),
      );
      cell.append(graphGutter(r), button);
      if (!turnNotesVisible && noteCut?.project === project)
        cell.append(action("Paste note", () => pasteNote(project, r), "small journey-paste"));
      card.append(cell);
    }
    if (turnNotesVisible) {
      const cell = node("div", undefined, "cell turn-notes-cell");
      if (!journeyVisible) cell.append(node("small", r.prompt));
      const attached = turnNotes.get(JSON.stringify([r.thread, r.turn])) ?? [];
      for (const note of attached) {
        const entry = noteView(project, note);
        if (!paired) entry.classList.add("note-card");
        cell.append(entry);
      }
      const addNote = action(
        "+ Note",
        () => editNote(project, { thread: r.thread, turn: r.turn }),
        "small",
      );
      if (!attached.length) {
        addNote.dataset.axis = "1";
        addNote.dataset.identity = JSON.stringify([r.thread, r.turn]);
      }
      cell.append(addNote);
      if (noteCut?.project === project)
        cell.append(action("Paste note", () => pasteNote(project, r), "small"));
      card.append(cell);
    }
    records.append(card);
  });
  if (records && !rows.length) records.append(node("p",
    journeyFilter?.mode === "only" ? "No turns for this session." : "Waiting for the first recorded turn.",
    "cell empty"));
  if (projectPane) {
    for (const note of projectNotes) {
      const card = node("article", undefined, "board-card cell");
      card.append(noteView(project, note));
      projectPane.append(card);
    }
    if (!projectNotes.length) projectPane.append(node("p", "No project notes yet.", "cell empty"));
  }
  return grid;
}

const boardPrefix = "agent-journal:board:";
let currentPage = null;
let browserJump = null;
let gPending = false;

function route() {
  const storage = isStorageRoute();
  const project = storage
    ? new URLSearchParams(location.hash.slice("#stats".length)).get("project") || ""
    : decodeURIComponent(location.hash.slice(1));
  const query = storage ? {} : { ...pageQuery };
  return { storage, project, query, legacyNewerPages, key: JSON.stringify([storage, project, query]) };
}

function cachedRead(key) {
  if (readCache.has(key)) return readCache.get(key);
  try {
    const cached = JSON.parse(localStorage.getItem(boardPrefix + key));
    if (cached) readCache.set(key, cached);
    return cached;
  } catch {
    return null;
  }
}

function rememberRead(key, data) {
  const entry = { saved: Date.now(), data };
  readCache.delete(key);
  readCache.set(key, entry);
  while (readCache.size > 12) readCache.delete(readCache.keys().next().value);
  try {
    localStorage.setItem(boardPrefix + key, JSON.stringify(entry));
    const keys = Object.keys(localStorage).filter(name => name.startsWith(boardPrefix));
    if (keys.length > 12) {
      const dates = keys.map(name => [name, JSON.parse(localStorage.getItem(name))?.saved ?? 0]);
      dates.sort((a, b) => a[1] - b[1]);
      for (const [name] of dates.slice(0, dates.length - 12)) localStorage.removeItem(name);
    }
  } catch { /* The current view remains usable when the browser refuses a cache write. */ }
}

async function readRoute(target, cached, signal) {
  const { storage, project, query } = target;
  if (storage) return api("stats", project ? { project } : {}, undefined, signal);
  if (!project) return api("overview", {}, undefined, signal);
  const params = cached?.revision ? { project, ...query, revision: cached.revision } : { project, ...query };
  try {
    const response = await api("board", params, undefined, signal);
    return response.unchanged ? cached : response;
  } catch (error) {
    if (error.status !== 404) throw error;
  }
  const journey = before => api("journey", { project, limit: pageSize + 1,
    before: before ?? "", thread: query.thread ?? "", lanes: true }, undefined, signal);
  const cachedEdge = query.edge && cached?.legacy && "legacy_before" in cached;
  let before = cachedEdge ? cached.legacy_before : query.before ?? null;
  let newerPages = (cachedEdge ? cached.legacy_newer_pages : target.legacyNewerPages).slice();
  let [batch, notes] = await Promise.all([
    journey(before),
    api("notes", { project }, undefined, signal),
  ]);
  if (query.edge === "first" && !cachedEdge) {
    // Older servers expose only backward cursors. Keep the pages traversed to get here.
    while (batch.length > pageSize) {
      const row = batch.at(-pageSize);
      const cursor = JSON.stringify([row.started, row.turn, row.thread]);
      if (cursor === before) throw new Error("The server did not return an older Journey page");
      newerPages.push(before);
      before = cursor;
      batch = await journey(before);
    }
  }
  return {
    legacy: true,
    legacy_before: before,
    legacy_newer_pages: newerPages,
    turns: batch.slice(-pageSize),
    turn_notes: notes.filter(note => note.turn),
    project_notes: notes.filter(note => !note.turn),
    earlier: batch.length > pageSize,
    later: before !== null,
  };
}

function applySessionFilter(thread, mode) {
  if (!currentPage) return;
  journeyFilter = thread ? { thread, mode } : null;
  const scope = mode === "only" ? thread : undefined;
  if (scope === pageQuery.thread) {
    renderView(currentPage, currentPage.data);
    return;
  }
  const query = { ...pageQuery };
  for (const key of ["thread", "before", "after", "edge"]) delete query[key];
  if (scope) query.thread = scope;
  pageQuery = query;
  legacyNewerPages = [];
  browserJump = null;
  refresh();
}

function makeSessionControls(project) {
  const sessions = new Set();
  const details = node("details", undefined, "session-filter");
  const summary = node("summary", "Session filter");
  const panel = node("div", undefined, "session-controls");
  const label = node("label", "Session");
  const select = node("select");
  select.append(node("option", "Choose a session"));
  select.options[0].value = "";
  label.append(select);
  const highlight = action("Highlight", () => applySessionFilter(select.value, "highlight"));
  const only = action("Show only", () => applySessionFilter(select.value, "only"));
  const clear = action("Clear filter", () => applySessionFilter("", null));
  const status = node("span", undefined, "meta");
  status.setAttribute("role", "status");
  panel.append(label, highlight, only, clear, status);
  details.append(summary, panel);

  function updateButtons() {
    highlight.disabled = only.disabled = !select.value;
    clear.disabled = !journeyFilter;
    highlight.setAttribute("aria-pressed", String(journeyFilter?.mode === "highlight" && journeyFilter.thread === select.value));
    only.setAttribute("aria-pressed", String(journeyFilter?.mode === "only" && journeyFilter.thread === select.value));
  }
  function addSessions(threads) {
    const size = sessions.size;
    for (const thread of threads) sessions.add(thread);
    if (sessions.size === size) return;
    const selected = select.value || journeyFilter?.thread;
    const suffixes = new Map();
    for (const thread of sessions) {
      const suffix = thread.slice(-6);
      suffixes.set(suffix, (suffixes.get(suffix) ?? 0) + 1);
    }
    select.length = 1;
    for (const thread of [...sessions].sort()) {
      const suffix = thread.slice(-6);
      const option = node("option", suffixes.get(suffix) > 1 ? thread : suffix);
      option.value = thread;
      select.append(option);
    }
    select.value = selected ?? "";
  }
  select.addEventListener("change", updateButtons);
  details.addEventListener("toggle", async () => {
    if (!details.open) return;
    if (!select.value && selectedJourneySession && !journeyFilter) select.value = selectedJourneySession;
    updateButtons();
    try {
      addSessions(await api("sessions", { project }));
      updateButtons();
    } catch {
      if (!sessions.size) status.textContent = "Session list unavailable";
    }
  });
  return {
    project, details,
    update(data) {
      addSessions(data.turns.map(row => row.thread));
      summary.textContent = journeyFilter
        ? `Session filter · ${journeyFilter.thread.slice(-6)} · ${journeyFilter.mode === "only" ? "Show only" : "Highlight"}`
        : "Session filter";
      status.textContent = journeyFilter?.mode === "highlight" &&
        !data.turns.some(row => row.thread === journeyFilter.thread)
        ? "This session has no turns on the current page." : "";
      updateButtons();
    },
  };
}

function renderView(target, data) {
  const { storage, project } = target;
  const sig = JSON.stringify([target.key, [...visible], journeyFilter, noteEpoch, data.revision ?? data]);
  if (last === sig) return;
  last = sig;
  const sameProjectPage = currentPage?.project === project &&
    ["project_before", "project_after", "project_edge"].every(key => currentPage.query[key] === target.query[key]);
  const oldScroll = new Map([...$("content").querySelectorAll("[data-pane]")]
    .filter(pane => currentPage?.key === target.key || (sameProjectPage && pane.dataset.pane === "Project notes"))
    .map(pane => [pane.dataset.pane, pane.scrollTop]));
  if (storage || currentPage?.project !== project) selectedJourneySession = null;
  currentPage = storage || !project ? null : { ...target, data };
  const selected = document.activeElement?.dataset?.identity;
  const selectedAxis = document.activeElement?.dataset?.axis;
  $("view").hidden = false;
  $("crumb").replaceChildren();
  $("axes").replaceChildren();
  $("title").textContent = project || (storage ? "Storage" : "Projects");
  const showSessionControls = visible.has("Journey") || (journeyFilter && visible.has("Turn notes"));
  $("sessionFilters").hidden = storage || !project || !showSessionControls;
  if (!$("sessionFilters").hidden) {
    if (sessionControls?.project !== project) {
      sessionControls = makeSessionControls(project);
      $("sessionFilters").replaceChildren(sessionControls.details);
    }
    sessionControls.update(data);
  }
  if (project || storage) {
    const link = node("a", "All projects");
    link.href = "#";
    $("crumb").append(link);
  }
  if (!storage || project) $("crumb").append(storageLink());
  if (storage) {
    $("axes").append(action("Refresh", refresh));
    $("content").replaceChildren(storageView(data, project));
    return;
  }
  if (!project) {
    $("content").replaceChildren(overview(data));
    return;
  }
  for (const axis of axes) {
    const label = node("label"), cb = node("input");
    cb.type = "checkbox";
    cb.checked = visible.has(axis);
    cb.onchange = () => {
      cb.checked ? visible.add(axis) : visible.delete(axis);
      renderView(target, data);
      [...$("axes").querySelectorAll("input")][axes.indexOf(axis)]?.focus();
    };
    label.append(cb, axis);
    $("axes").append(label);
  }
  const nav = node("div", undefined, "pagination");
  if (data.earlier) nav.append(
    action("Oldest turns", () => paginate(0, "first")),
    action("Earlier turns", () => paginate(0, "earlier")),
  );
  if (data.later) nav.append(action("Newer turns", () => paginate(0, "later")));
  if (target.query.before || target.query.after || target.query.edge === "first") {
    nav.append(action("Latest turns", () => paginate(0, "last")));
  }
  if (visible.has(axes[2])) {
    if (data.project_earlier) nav.append(
      action("Oldest project notes", () => paginate(2, "first")),
      action("Earlier project notes", () => paginate(2, "earlier")),
    );
    if (data.project_later) nav.append(
      action("Newer project notes", () => paginate(2, "later")),
      action("Latest project notes", () => paginate(2, "last")),
    );
  }
  if (noteCut?.project === project) nav.append(action("Cancel cut", () => {
    noteCut = null;
    noteEpoch++;
    renderView(target, data);
  }));
  $("content").replaceChildren(nav, board(project, data.turns, [...data.turn_notes, ...data.project_notes]));
  for (const pane of $("content").querySelectorAll("[data-pane]")) {
    if (oldScroll.has(pane.dataset.pane)) pane.scrollTop = oldScroll.get(pane.dataset.pane);
  }
  if (browserJump?.key === target.key) {
    const items = axisItems(browserJump.axis);
    items[browserJump.first ? 0 : items.length - 1]?.focus();
    browserJump = null;
  } else if (selected != null && selectedAxis != null) {
    axisItems(Number(selectedAxis)).find(item => item.dataset.identity === selected)?.focus({ preventScroll: true });
  }
}

async function refresh() {
  const target = route();
  const cached = cachedRead(target.key);
  if (cached && $("login").hidden && !$("detail").open) {
    renderView(target, cached.data);
    if (Date.now() - cached.saved > 10000) $("connection").textContent = "Cached data · refreshing";
  }
  if (activeRequest?.key === target.key) return activeRequest.promise;
  activeRequest?.controller.abort();
  const request = { key: target.key, controller: new AbortController(), id: ++version };
  activeRequest = request;
  request.promise = (async () => {
    try {
      const data = await readRoute(target, cached?.data, request.controller.signal);
      if (request.id !== version) return;
      if (cached?.data === data) cached.saved = Date.now();
      else rememberRead(target.key, data);
      $("login").hidden = true;
      $("connection").textContent = "Connected";
      if (!$("detail").open) renderView(target, data);
    } catch (error) {
      if (error.name === "AbortError" || request.id !== version) return;
      $("connection").textContent =
        cached && error.status !== 401 ? "Cached data · " + error.message : error.message;
    } finally {
      if (activeRequest === request) activeRequest = null;
    }
  })();
  return request.promise;
}

function paginate(axis, direction) {
  if (!currentPage) return;
  const projectNotes = axis === 2;
  const { data } = currentPage;
  const entries = projectNotes ? data.project_notes : data.turns;
  const prefix = projectNotes ? "project_" : "";
  if ((direction === "earlier" || direction === "later") && !data[prefix + direction])
    return;
  const query = { ...pageQuery };
  for (const name of ["before", "after", "edge"]) delete query[prefix + name];
  if (!projectNotes) legacyNewerPages = [];
  if (direction === "earlier" || direction === "later") {
    const earlier = direction === "earlier";
    const row = entries[earlier ? 0 : entries.length - 1];
    if (!row) return;
    const cursor = JSON.stringify(projectNotes ? [row.created, row.id] : [row.started, row.turn, row.thread]);
    if (data.legacy && !projectNotes) {
      legacyNewerPages = (data.legacy_newer_pages ?? []).slice();
      if (earlier) {
        legacyNewerPages.push(data.legacy_before ?? null);
        query.before = cursor;
      } else {
        const before = legacyNewerPages.pop();
        if (before) query.before = before;
      }
    } else query[prefix + (earlier ? "before" : "after")] = cursor;
  } else if (direction === "first" || axis === 1) query[prefix + "edge"] = direction;
  pageQuery = query;
  browserJump = { key: route().key, axis, first: ["first", "later"].includes(direction) };
  if (browserJump.key === currentPage.key) {
    const items = axisItems(axis);
    items[browserJump.first ? 0 : items.length - 1]?.focus();
    browserJump = null;
  } else refresh();
}

function axisItems(axis) {
  return [...$("content").querySelectorAll(`[data-axis="${axis}"]`)];
}

function moveBetweenAxes(element, direction) {
  const candidates = axes.flatMap((axis, index) =>
    visible.has(axis) && (index - activeAxis) * direction > 0 ? [index] : []);
  const nextAxis = direction < 0 ? candidates.at(-1) : candidates[0];
  if (nextAxis === undefined) return;

  const card = element.closest(".board-card.paired");
  const pane = [...$("content").querySelectorAll("[data-pane]")].find(candidate =>
    candidate.dataset.pane.split("+").includes(axes[nextAxis]));
  activeAxis = nextAxis;
  if (card && nextAxis < 2 && pane) {
    const matching = card.querySelector(`[data-axis="${nextAxis}"]`);
    if (matching) {
      matching.focus();
      return;
    }
  }
  if (pane) {
    const top = pane.getBoundingClientRect().top;
    const items = axisItems(nextAxis);
    (items.find(item => item.getBoundingClientRect().bottom > top) ?? pane).focus();
  }
}

function browseKeys(event) {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || event.isComposing)
    return;
  if (event.target.matches("input, select, textarea, [contenteditable]")) return;
  if ($("detail").open) {
    if (activeNoteEditor) return;
    if (["j", "k", "d", "u", "g", "G"].includes(event.key)) {
      event.preventDefault();
      const dialog = $("turnPaneDetail").open ? $("turnPaneDetail") : $("detail");
      const scroll = event.target.closest(".turn-detail-scroll")
        ?? event.target.closest(".turn-detail-pane")?.querySelector(".turn-detail-scroll")
        ?? dialog.querySelector(".turn-detail-scroll")
        ?? dialog;
      if (event.key === "g") {
        if (gPending) scroll.scrollTop = 0;
        gPending = !gPending;
      } else {
        gPending = false;
        if (event.key === "G") scroll.scrollTop = scroll.scrollHeight;
        else scroll.scrollTop += ({ j: 28, k: -28, d: scroll.clientHeight / 2, u: -scroll.clientHeight / 2 })[event.key];
      }
    }
    return;
  }
  if (!currentPage || !event.target.closest("main")) return;
  if (event.target.dataset.axis != null) activeAxis = Number(event.target.dataset.axis);
  else if (event.target.dataset.pane && !event.target.dataset.pane.split("+").includes(axes[activeAxis]))
    activeAxis = axes.indexOf(event.target.dataset.pane.split("+")[0]);
  if (/^[123]$/.test(event.key)) {
    event.preventDefault();
    activeAxis = Number(event.key) - 1;
    visible.clear();
    visible.add(axes[activeAxis]);
    const { data, ...target } = currentPage;
    renderView(target, data);
    axisItems(activeAxis)[0]?.focus();
    return;
  }
  if (!["h", "l", "j", "k", "d", "u", "g", "G"].includes(event.key) && !Object.hasOwn(pageKeys, event.key))
    return;
  event.preventDefault();
  if (event.key === "h" || event.key === "l") {
    gPending = false;
    moveBetweenAxes(event.target, event.key === "h" ? -1 : 1);
    return;
  }
  if (event.key === "g") {
    if (gPending) axisItems(activeAxis)[0]?.focus();
    gPending = !gPending;
    return;
  }
  gPending = false;
  if (event.key === "G") {
    axisItems(activeAxis).at(-1)?.focus();
    return;
  }
  if (Object.hasOwn(pageKeys, event.key)) {
    paginate(activeAxis, pageKeys[event.key]);
    return;
  }
  const items = axisItems(activeAxis);
  const index = items.indexOf(document.activeElement);
  const pane = items[0]?.closest("[data-pane]");
  const half = Math.max(1, Math.floor((pane?.clientHeight ?? window.innerHeight) / 130));
  const step = ({j: 1, k: -1, d: half, u: -half})[event.key];
  items[Math.max(0, Math.min(items.length - 1, index + step))]?.focus();
}

$("home").onclick = () => { location.hash = ""; };
$("close").onclick = closeJournalDialog;
$("closeTurnPane").onclick = closeTurnPane;
$("turnPaneDetail").addEventListener("cancel", event => {
  event.preventDefault();
  closeTurnPane();
});
$("detail").addEventListener("cancel", () => {
  detailRequest?.abort();
  finishNoteEditing();
});
$("detail").addEventListener("close", () => {
  if ($("detail").open) return;
  closeTurnPane();
  detailRequest?.abort();
  detailRequest = null;
  finishNoteEditing();
  modalTrigger?.focus({ preventScroll: true });
  modalTrigger = null;
  $("modalBody").replaceChildren();
  refresh();
});
$("auth").onsubmit = async event => {
  event.preventDefault();
  try {
    await api("login", {}, { token: $("token").value });
    $("token").value = "";
    last = "";
    refresh();
  } catch (error) {
    $("connection").textContent = error.message;
  }
};
window.addEventListener("hashchange", () => {
  pageQuery = {};
  legacyNewerPages = [];
  journeyFilter = null;
  sessionControls = null;
  browserJump = null;
  last = "";
  if ($("detail").open) closeJournalDialog();
  refresh();
});
window.addEventListener("online", () => { pumpDrafts(); refresh(); });
window.addEventListener("pagehide", () => {
  for (const draft of noteDrafts.values()) {
    if (draft.checkpoint) checkpointDraft(draft);
  }
});
document.addEventListener("keydown", browseKeys);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
async function startJournal() {
  if (location.hash.startsWith("#login?")) {
    const params = new URLSearchParams(location.hash.slice("#login?".length));
    const project = params.get("project");
    history.replaceState(null, "", location.pathname + location.search +
      (project ? "#" + encodeURIComponent(project) : ""));
    const code = params.get("code");
    if (code) {
      try {
        await api("login", {}, { code });
      } catch {
        // The normal refresh can use an existing session or show the sign-in form.
      }
    }
  }
  await refresh();
}
startJournal();
setInterval(pumpDrafts, 250);
setInterval(() => {
  if (!activeRequest && !document.hidden && !$("detail").open && $("login").hidden && !isStorageRoute())
    refresh();
}, 3000);
