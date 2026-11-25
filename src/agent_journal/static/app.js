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
const visible = new Set(axes);
const pageSize = 60;
let before = "";
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
  el.onclick = () =>
    Promise.resolve(fn()).catch(
      (err) => ($("connection").textContent = err.message),
    );
  return el;
}
async function api(path, params = {}, body) {
  const res = await fetch(
    "/api/" + path + "?" + new URLSearchParams(params),
    body
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {},
  );
  if (res.status === 401) {
    $("login").hidden = false;
    $("view").hidden = true;
    throw new Error("Enter your access token");
  }
  if (!res.ok) throw new Error((await res.json()).error);
  return res.json();
}
function modal(title, child) {
  $("modalTitle").textContent = title;
  $("modalBody").replaceChildren(child);
  $("detail").showModal();
}
function editNote(project, target = {}, note = null) {
  const form = node("form"),
    area = node("textarea"),
    submit = node("button", "Save"),
    err = node("p");
  area.required = true;
  area.value = note?.body ?? "";
  area.setAttribute("aria-label", "Note text");
  const id = note?.id ?? crypto.randomUUID();
  form.append(area, submit, err);
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    submit.disabled = true;
    try {
      const payload = { project, id, body: area.value };
      if (note) await api("edit-note", {}, { ...payload, version: note.version });
      else await api("notes", {}, { ...payload, ...target });
      $("detail").close();
      last = "";
      await refresh();
    } catch (e) {
      err.textContent = e.message;
    } finally {
      submit.disabled = false;
    }
  };
  modal(note ? "Edit note" : (target.turn ? "Add turn note" : "Add project note"), form);
  area.focus();
}
async function transcript(project, r) {
  const box = node("div");
  for (const item of await api("transcript", {
    project,
    thread: r.thread,
    turn: r.turn,
  })) {
    box.append(
      node("h2", item.type),
      node("pre", item.text ?? JSON.stringify(item, null, 2)),
    );
  }
  modal("Transcript", box);
}
function projectLink(k) {
  const a = node("a", k);
  a.href = "#" + encodeURIComponent(k);
  return a;
}
function noteView(project, note) {
  const el = node("div", undefined, "note");
  el.append(node("div", note.body), action("Edit", () => editNote(project, {}, note), "small"));
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
  if (journeyVisible) {
    const laneCount = Math.max(
      1,
      ...rows.flatMap((r) => r.graph_lanes.tracks.map(([lane]) => lane + 1)),
    );
    grid.style.setProperty("--lane-count", laneCount);
  }
  grid.style.gridTemplateColumns = cols
    .map((axis) =>
      axis === "Journey"
        ? "minmax(calc(190px + var(--graph-width)),2fr)"
        : "minmax(135px,1fr)",
    )
    .join(" ");
  function put(el, name, row) {
    el.style.gridColumn = String(cols.indexOf(name) + 1);
    el.style.gridRow = String(row);
    grid.append(el);
  }
  for (const a of cols) {
    const head = node(
      "div",
      undefined,
      a === "Journey" ? "head journey-head" : "head",
    );
    head.append(node("h2", a));
    if (a === "Project notes")
      head.append(action("＋", () => editNote(project), "small"));
    put(head, a, 1);
  }
  const seenSessions = new Set();
  rows.forEach((r, i) => {
    if (journeyVisible) {
      const cell = node("div", undefined, "cell journey-cell"),
        button = action("", () => transcript(project, r), "rowbtn");
      const meta = node("div", undefined, "meta");
      const time = new Date(r.started * 1000).toLocaleString("en-US", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
      const session = node("span", r.thread.slice(-6), "session-id");
      session.style.color = sessionColor(r.thread);
      session.title = "Session " + r.thread;
      meta.append(
        node("span", time),
        session,
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
      button.append(
        meta,
        node("span", r.prompt, "req"),
        node("span", r.summary, "result"),
      );
      cell.append(graphGutter(r), button);
      put(cell, "Journey", i + 2);
    }
    if (visible.has("Turn notes")) {
      const cell = node("div", undefined, "cell");
      if (!journeyVisible) cell.append(node("small", r.prompt));
      for (const n of notes.filter(
        (n) => n.turn === r.turn && n.thread === r.thread,
      ))
        cell.append(noteView(project, n));
      cell.append(
        action(
          "+ Note",
          () => editNote(project, { thread: r.thread, turn: r.turn }),
          "small",
        ),
      );
      put(cell, "Turn notes", i + 2);
    }
  });
  if (!rows.length && cols.some((x) => x !== "Project notes"))
    put(
      node("p", "Waiting for the first recorded turn.", "cell empty"),
      cols[0],
      2,
    );
  if (visible.has("Project notes")) {
    const panel = node("div", undefined, "free");
    for (const n of notes.filter((n) => !n.turn))
      panel.append(noteView(project, n));
    put(panel, "Project notes", 2);
    panel.style.gridRow = "2 / span " + Math.max(1, rows.length);
  }
  return grid;
}
async function refresh() {
  const current = ++version;
  try {
    const storage = isStorageRoute();
    const key = storage
      ? new URLSearchParams(location.hash.slice("#stats".length)).get("project") || ""
      : decodeURIComponent(location.hash.slice(1));
    let data;
    if (storage) {
      data = await api("stats", key ? { project: key } : {});
    } else if (key) {
      data = await Promise.all([
        api("journey", { project: key, limit: pageSize + 1, before, lanes: true }),
        api("notes", { project: key }),
      ]);
    } else {
      data = await api("overview");
    }
    if (current !== version) return;
    $("login").hidden = true;
    $("view").hidden = false;
    $("connection").textContent = "Connected";
    const sig = JSON.stringify([storage, key, before, [...visible], data]);
    if (sig === last) return;
    last = sig;
    $("crumb").replaceChildren();
    $("axes").replaceChildren();
    $("title").textContent = key || (storage ? "Storage" : "Projects");
    if (key || storage) {
      const a = node("a", "All projects");
      a.href = "#";
      $("crumb").append(a);
    }
    if (!storage || key) $("crumb").append(storageLink());
    if (storage) {
      $("axes").append(action("Refresh", refresh));
      $("content").replaceChildren(storageView(data, key));
      return;
    }
    if (key) {
      for (const axis of axes) {
        const label = node("label"),
          cb = node("input");
        cb.type = "checkbox";
        cb.checked = visible.has(axis);
        cb.onchange = () => {
          cb.checked ? visible.add(axis) : visible.delete(axis);
          refresh();
        };
        label.append(cb, axis);
        $("axes").append(label);
      }
    }
    if (!key) {
      $("content").replaceChildren(overview(data));
      return;
    }
    const [turns, notes] = data;
    const rows = turns.slice(-pageSize);
    const nav = node("div", undefined, "pagination");
    if (turns.length > pageSize) {
      nav.append(action("Earlier turns", () => {
        const { started, turn, thread } = rows[0];
        before = JSON.stringify([started, turn, thread]);
        refresh();
      }));
    }
    if (before) nav.append(action("Latest turns", () => { before = ""; refresh(); }));
    $("content").replaceChildren(nav, board(key, rows, notes));

  } catch (e) {
    if (current === version) $("connection").textContent = e.message;
  }
}
$("home").onclick = () => {
  location.hash = "";
};
$("close").onclick = () => $("detail").close();
$("auth").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("login", {}, { token: $("token").value });
    $("token").value = "";
    last = "";
    refresh();
  } catch (err) {
    $("connection").textContent = err.message;
  }
};
window.addEventListener("hashchange", () => { before = ""; refresh(); });
refresh();
setInterval(() => {
  if (!document.hidden && !$("detail").open && $("login").hidden && !isStorageRoute())
    refresh();
}, 3000);
