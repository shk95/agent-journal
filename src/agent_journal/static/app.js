"use strict";
const $ = (id) => document.getElementById(id);
const labels = {
  inProgress: "In progress",
  completed: "Completed",
  interrupted: "Interrupted",
  failed: "Failed",
};
const axes = ["Journey", "Turn notes", "Project notes"];
const visible = new Set(axes);
const colors = ["#24794e", "#735995", "#a76135", "#397e93", "#8e5764"];
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
function addNote(project, target = {}) {
  const form = node("form"),
    area = node("textarea"),
    submit = node("button", "Save"),
    err = node("p");
  area.required = true;
  area.setAttribute("aria-label", "Note text");
  const id = crypto.randomUUID();
  form.append(area, submit, err);
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    submit.disabled = true;
    try {
      await api("notes", {}, { project, id, body: area.value, ...target });
      $("detail").close();
      last = "";
      await refresh();
    } catch (e) {
      err.textContent = e.message;
    } finally {
      submit.disabled = false;
    }
  };
  modal(target.turn ? "Add turn note" : "Add project note", form);
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
function overview(entries) {
  const out = node("div");
  for (const [p, rows] of entries) {
    const section = node("article", undefined, "project"),
      left = node("div"),
      right = node("div");
    left.append(projectLink(p.project));
    const live = rows.filter((r) => r.status === "inProgress");
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
    for (const r of display)
      right.append(node("p", r.prompt + " → " + r.summary));
    if (!display.length)
      right.append(node("small", "No recorded turns yet"));
    section.append(left, right);
    out.append(section);
  }
  if (!entries.length)
    out.append(
      node(
        "p",
        "Run agent-journal register in a project to see it here.",
        "empty",
      ),
    );
  return out;
}
function board(project, rows, notes) {
  const cols = axes.filter((x) => visible.has(x)),
    grid = node("div", undefined, "board");
  if (!cols.length) return node("p", "Select a column to show.", "empty");
  grid.style.gridTemplateColumns = cols
    .map((x) => (x === "Journey" ? "minmax(220px,2fr)" : "minmax(135px,1fr)"))
    .join(" ");
  function put(el, name, row) {
    el.style.gridColumn = String(cols.indexOf(name) + 1);
    el.style.gridRow = String(row);
    grid.append(el);
  }
  for (const a of cols) {
    const head = node("div", undefined, "head");
    head.append(node("h2", a));
    if (a === "Project notes")
      head.append(action("＋", () => addNote(project), "small"));
    put(head, a, 1);
  }
  const sessions = [...new Set(rows.map((r) => r.thread))];
  const lastRow = new Map(),
    firstRow = new Map();
  rows.forEach((r, i) => {
    if (!firstRow.has(r.thread)) firstRow.set(r.thread, i);
    lastRow.set(r.thread, i);
  });
  rows.forEach((r, i) => {
    if (visible.has("Journey")) {
      const cell = node("div", undefined, "cell"),
        button = action("", () => transcript(project, r), "rowbtn");
      const mark = node("span", undefined, "graph");
      mark.setAttribute("aria-hidden", "true");
      for (const s of sessions) {
        if (firstRow.get(s) > i || lastRow.get(s) < i) continue;
        const glyph =
          s === r.thread
            ? r.parent && firstRow.get(s) === i
              ? "⑂"
              : "●"
            : "│";
        const lane = node("span", glyph);
        lane.style.color = colors[sessions.indexOf(s) % colors.length];
        mark.append(lane);
      }
      const meta = node("div", undefined, "meta");
      const time = new Date(r.started * 1000).toLocaleString("en-US", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
      meta.append(
        mark,
        node("span", time),
        node("span", r.thread.slice(-6)),
        node("span", labels[r.status], r.status === "inProgress" ? "live" : ""),
      );
      if (r.parent && firstRow.get(r.thread) === i)
        meta.append(node("span", "↳ " + r.parent.slice(-6)));
      button.append(
        meta,
        node("span", r.prompt, "req"),
        node("span", r.summary, "result"),
      );
      cell.append(button);
      put(cell, "Journey", i + 2);
    }
    if (visible.has("Turn notes")) {
      const cell = node("div", undefined, "cell");
      if (!visible.has("Journey")) cell.append(node("small", r.prompt));
      for (const n of notes.filter(
        (n) => n.turn === r.turn && n.thread === r.thread,
      ))
        cell.append(node("p", n.body, "note"));
      cell.append(
        action(
          "+ Note",
          () => addNote(project, { thread: r.thread, turn: r.turn }),
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
      panel.append(node("p", n.body, "note"));
    put(panel, "Project notes", 2);
    panel.style.gridRow = "2 / span " + Math.max(1, rows.length);
  }
  return grid;
}
async function refresh() {
  const current = ++version;
  try {
    const key = decodeURIComponent(location.hash.slice(1)),
      projects = await api("projects");
    let data;
    if (key)
      data = await Promise.all([
        api("journey", { project: key }),
        api("notes", { project: key }),
      ]);
    else
      data = await Promise.all(
        projects.map(async (p) => [
          p,
          await api("journey", { project: p.project }),
        ]),
      );
    if (current !== version) return;
    $("login").hidden = true;
    $("view").hidden = false;
    $("connection").textContent = "Connected";
    const sig = JSON.stringify([key, [...visible], data]);
    if (sig === last) return;
    last = sig;
    $("crumb").replaceChildren();
    $("axes").replaceChildren();
    $("title").textContent = key || "Projects";
    if (key) {
      const a = node("a", "All projects");
      a.href = "#";
      $("crumb").append(a);
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
    $("content").replaceChildren(key ? board(key, ...data) : overview(data));
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
window.addEventListener("hashchange", refresh);
refresh();
setInterval(() => {
  if (!document.hidden && !$("detail").open && $("login").hidden) refresh();
}, 3000);
