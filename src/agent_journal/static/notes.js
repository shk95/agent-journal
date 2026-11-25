"use strict";

const noteDrafts = new Map();
const noteWrites = new Map();
const noteDetails = new Map();
const noteMutations = new Set();
const draftPrefix = "agent-journal:draft:";
const segments = new Intl.Segmenter(undefined, { granularity: "grapheme" });
const notePriorities = new Map([
  ["highlighted", { label: "Highlighted", marker: "★ " }],
  ["normal", { label: "Normal", marker: "" }],
  ["low", { label: "Low priority", marker: "↓ " }],
]);
let noteEpoch = 0;
let noteCut = null;
let activeNoteEditor = null;

function displayTitle(note) {
  return (note.title || markdownHeading(note.body ?? "") || "Untitled note").normalize("NFC");
}

function styleNoteTitle(element, note) {
  const priority = notePriorities.get(note.priority);
  if (!priority) return;
  element.dataset.priority = note.priority;
  if (priority.marker) {
    const marker = node("span", priority.marker);
    marker.setAttribute("aria-hidden", "true");
    element.prepend(marker);
  }
}

function markdownHeading(body) {
  if (!/[\r\n]|^\s*#/.test(body)) return "";
  for (const block of renderMarkdown(body).children) {
    if (/^H[2-6]$/.test(block.tagName) && block.textContent.trim())
      return Array.from(block.textContent.trim().replaceAll("\n", " ")).slice(0, 240).join("");
  }
  return "";
}

function noteKey(note) {
  return note.project + ":" + note.id;
}

function readDrafts() {
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key?.startsWith(draftPrefix)) continue;
      const draft = JSON.parse(localStorage.getItem(key));
      if (draft?.id && draft.project) {
        const title = draft.title?.trim();
        if (title && markdownHeading(draft.body) !== title) {
          const heading = "# " + title.replace(/([\\*_\[\]#!<>&\x60])/g, "\\$1");
          draft.body = draft.body ? heading + "\n\n" + draft.body : heading;
        }
        delete draft.title;
        noteDrafts.set(noteKey(draft), draft);
      }
    }
  } catch {
    // A browser can revoke local storage or clear it during iteration.
  }
}

function checkpointDraft(draft) {
  try {
    const { checkpoint, ...record } = draft;
    localStorage.setItem(draftPrefix + noteKey(draft), JSON.stringify(record));
    draft.checkpoint = null;
  } catch {
    draft.checkpoint = null;
    draft.localError = "Local storage unavailable";
  }
}

function discardDraft(key) {
  noteDrafts.delete(key);
  try { localStorage.removeItem(draftPrefix + key); } catch { /* Storage may be disabled. */ }
}

function draftState(key) {
  const draft = noteDrafts.get(key);
  if (noteWrites.has(key)) return "Saving…";
  if (!draft) return "Saved";
  if (draft.conflict) return "Conflict · local draft preserved";
  if (draft.localError) return draft.localError;
  if (draft.error) return "Saved locally · " + draft.error;
  return draft.checkpoint ? "Editing" : "Saved locally";
}

function enqueueDraft(note, immediate = false) {
  const key = noteKey(note);
  const previous = noteDrafts.get(key);
  if (!(note.body.trim() || note.version || previous)) return;
  const now = Date.now();
  const draft = {
    id: note.id, project: note.project, body: note.body, thread: note.thread, turn: note.turn,
    version: previous ? previous.version : note.version,
    due: immediate ? now : now + 1000,
    checkpoint: now + 250,
    conflict: previous?.conflict ?? false,
  };
  noteDrafts.set(key, draft);
  if (immediate) checkpointDraft(draft);
  noteEpoch++;
  if (activeNoteEditor?.key === key) activeNoteEditor.update();
}

async function saveDraft(sent) {
  const { id, project, body, thread, turn } = sent;
  const payload = { id, project, body, thread, turn, format: "markdown" };
  let version = sent.version;
  if (version == null) {
    const result = await api("notes", {}, payload);
    version = result.version ?? 1;
    if (result.matches !== false) return version;
    if (version !== 1) throw new JournalError(409, "The newly created note has already changed.");
  }
  return (await api("edit-note", {}, { ...payload, version })).version;
}

function pumpDrafts() {
  const now = Date.now();
  for (const [key, draft] of noteDrafts) {
    if (draft.checkpoint && draft.checkpoint <= now) checkpointDraft(draft);
    if (noteWrites.size >= 2 || noteWrites.has(key) || draft.conflict || draft.due == null || draft.due > now)
      continue;
    if (!navigator.onLine || !$("login").hidden) continue;
    const sent = { ...draft };
    noteWrites.set(key, saveDraft(sent).then(version => {
      const current = noteDrafts.get(key);
      if (!current) return;
      const unchanged = current.body === sent.body;
      if (activeNoteEditor?.key === key) activeNoteEditor.acknowledge(version);
      if (unchanged) discardDraft(key);
      else {
        current.version = version;
        delete current.error;
        checkpointDraft(current);
      }
      noteDetails.clear();
      noteEpoch++;
      if (!$("detail").open) refresh();
    }).catch(error => {
      const current = noteDrafts.get(key);
      if (!current) return;
      current.error = error.message;
      current.conflict = error.status === 409 || error.status === 404;
      current.due = error.status === 400 ? null : Date.now() + 4000;
      checkpointDraft(current);
    }).finally(() => {
      noteWrites.delete(key);
      if (activeNoteEditor?.key === key) activeNoteEditor.update();
    }));
    if (activeNoteEditor?.key === key) activeNoteEditor.update();
  }
}

function mergeDrafts(project, turns, notes) {
  const result = new Map(notes.map(note => [note.id, note]));
  const targets = new Set(turns.map(row => JSON.stringify([row.thread, row.turn])));
  for (const draft of noteDrafts.values()) {
    if (draft.project !== project) continue;
    if (result.has(draft.id) || (draft.version == null &&
        (!draft.turn || targets.has(JSON.stringify([draft.thread, draft.turn]))))) {
      result.set(draft.id, { ...result.get(draft.id), ...draft, title: "", preview: draft.body.slice(0, 200) });
    }
  }
  return [...result.values()];
}

const inlineMarkdown = /\\([\\*_\[\]#!<>&\x60])|\x60([^\x60\n]+)\x60|!?\[([^\]\n]+)\]\(([^)\s]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|_([^_\n]+)_|\\?\n/gu;

function renderInline(container, text) {
  let offset = 0;
  for (const match of text.matchAll(inlineMarkdown)) {
    container.append(text.slice(offset, match.index));
    if (match[1] !== undefined) container.append(match[1]);
    else if (match[2] !== undefined) container.append(node("code", match[2]));
    else if (match[3] !== undefined) {
      try {
        const url = new URL(match[4], location.href);
        if (["http:", "https:", "mailto:"].includes(url.protocol)) {
          const link = node("a", match[3]);
          link.href = url.href;
          container.append(link);
        } else container.append(match[3]);
      } catch { container.append(match[3]); }
    } else if (match[5] ?? match[6]) container.append(node("strong", match[5] ?? match[6]));
    else if (match[7] ?? match[8]) container.append(node("em", match[7] ?? match[8]));
    else container.append(node("br"));
    offset = match.index + match[0].length;
  }
  container.append(text.slice(offset));
}

const atxHeading = /^ {0,3}(#{1,6})(?:[ \t]+(.*))?$/;
const setextHeading = /^ {0,3}(=+|-+)[ \t]*$/;
const openingFence = /^ {0,3}(\x60{3,}|~{3,})/;
const indentCode = /^(?: {4}|\t)/;

function renderMarkdown(text, depth = 0) {
  const out = node("div", undefined, "markdown");
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  const listLine = /^\s*(?:(\d+)[.)]|[-+*])\s+(.*)$/;
  while (i < lines.length) {
    const line = lines[i++];
    if (!line.trim()) continue;
    const fence = line.match(openingFence);
    if (fence) {
      const code = [];
      const closing = new RegExp("^ {0,3}" + fence[1][0] + "{" + fence[1].length + ",}[ \\t]*$");
      while (i < lines.length && !closing.test(lines[i])) code.push(lines[i++]);
      if (i < lines.length) i++;
      const pre = node("pre");
      pre.append(node("code", code.join("\n")));
      out.append(pre);
      continue;
    }
    const heading = line.match(atxHeading);
    const entry = line.match(listLine);
    if (heading) {
      const title = node("h" + Math.min(6, heading[1].length + 1));
      renderInline(title, (heading[2] ?? "").replace(/(?:^|[ \t]+)#+[ \t]*$/, "").trim());
      out.append(title);
    } else if (indentCode.test(line)) {
      const code = [line.replace(indentCode, "")];
      while (indentCode.test(lines[i] ?? "")) code.push(lines[i++].replace(indentCode, ""));
      const pre = node("pre");
      pre.append(node("code", code.join("\n")));
      out.append(pre);
    } else if (entry) {
      const list = node(entry[1] === undefined ? "ul" : "ol");
      if (entry[1] !== undefined) list.start = Number(entry[1]);
      let current = entry;
      do {
        const item = node("li");
        renderInline(item, current[2]);
        list.append(item);
        current = lines[i]?.match(listLine);
        if (current && (current[1] === undefined) === (entry[1] === undefined)) i++;
        else current = null;
      } while (current);
      out.append(list);
    } else if (depth < 12 && /^\s*>/.test(line)) {
      const quote = node("blockquote");
      const content = [line.replace(/^\s*> ?/, "")];
      while (/^\s*>/.test(lines[i] ?? "")) content.push(lines[i++].replace(/^\s*> ?/, ""));
      quote.append(renderMarkdown(content.join("\n"), depth + 1));
      out.append(quote);
    } else {
      const content = [line];
      while (lines[i]?.trim() && !setextHeading.test(lines[i]) && !atxHeading.test(lines[i]) &&
             !openingFence.test(lines[i]) && !/^\s*>/.test(lines[i]) && !listLine.test(lines[i]))
        content.push(lines[i++]);
      const underline = lines[i]?.match(setextHeading);
      if (underline) i++;
      const block = node(underline ? (underline[1][0] === "=" ? "h2" : "h3") : "p");
      renderInline(block, content.join(underline ? " " : "\n"));
      out.append(block);
    }
  }
  return out;
}

async function fullNote(project, note, signal) {
  if (currentPage?.data.legacy && typeof note.body === "string") return note;
  const key = noteKey({ project, id: note.id }) + ":" + note.version;
  if (noteDetails.has(key)) return noteDetails.get(key);
  const item = await api("note", { project, id: note.id }, undefined, signal);
  if (noteDetails.size >= 12) noteDetails.delete(noteDetails.keys().next().value);
  noteDetails.set(key, item);
  return item;
}

function noteContext(note) {
  return note.turn ? "Turn note" : "Project note";
}

function noteDetail(note, focusStatus = false) {
  const box = node("section");
  const controls = node("div", undefined, "note-actions");
  controls.setAttribute("role", "group");
  controls.setAttribute("aria-label", "Note actions");
  controls.append(action("Edit", () => noteEditor(note)));
  const status = notePriorities.has(note.priority)
    ? action("Status", () => chooseNoteStatus(note, controls, box))
    : null;
  if (status) controls.append(status);
  if (note.turn && note.version != null)
    controls.append(action("Cut", () => {
      noteCut = note;
      $("detail").close();
      $("connection").textContent = "Turn note cut · select a destination";
      noteEpoch++;
      refresh();
    }));
  if (note.version != null)
    controls.append(action("Delete", () => confirmNoteDelete(note, box)));
  const title = displayTitle(note);
  const markdown = renderMarkdown(note.body);
  const heading = markdown.firstElementChild;
  if (heading?.matches("h2, h3, h4, h5, h6") && heading.textContent.trim().normalize("NFC") === title)
    heading.remove();
  box.append(node("p", noteContext(note), "meta"), controls, markdown);
  modal(title, box);
  styleNoteTitle($("modalTitle"), note);
  if (focusStatus) status?.focus();
  $("detail").onkeydown = event => {
    if (event.target !== document.activeElement || event.ctrlKey || event.altKey || event.metaKey)
      return;
    if (event.key === "e") { event.preventDefault(); noteEditor(note); }
    if (event.key === "s" && status) { event.preventDefault(); status.click(); }
  };
}

function chooseNoteStatus(note, controls, box) {
  const key = noteKey(note);
  const feedback = node("span", "Status · " + notePriorities.get(note.priority).label, "meta");
  feedback.setAttribute("role", "status");
  const options = [...notePriorities].map(([priority, { label }]) => {
    const button = action(label, async () => {
      if (priority === note.priority) { noteDetail(note, true); return; }
      if (noteMutations.has(key)) return;
      if (noteDrafts.has(key)) {
        feedback.textContent = "Sync this note's local draft first.";
        return;
      }
      noteMutations.add(key);
      try {
        const saved = await api("set-note-priority", {}, {
          project: note.project, id: note.id, version: note.version, priority,
        });
        noteDetails.clear();
        if (noteCut && noteKey(noteCut) === key) noteCut = saved;
        noteEpoch++;
        if (box.isConnected) noteDetail(saved, true);
        refresh();
      } catch (error) {
        feedback.textContent = error.message;
      } finally {
        noteMutations.delete(key);
      }
    });
    button.setAttribute("aria-pressed", String(priority === note.priority));
    return button;
  });
  controls.setAttribute("aria-label", "Note status");
  controls.replaceChildren(...options, action("Cancel", () => noteDetail(note, true)), feedback);
  options.find(button => button.getAttribute("aria-pressed") === "true")?.focus();
  $("detail").onkeydown = event => {
    if (event.key === "Escape") { event.preventDefault(); noteDetail(note, true); }
  };
}

function confirmNoteDelete(note, box) {
  if (noteDrafts.has(noteKey(note))) {
    $("connection").textContent = "Sync this note's local draft first.";
    return;
  }
  box.replaceChildren(
    node("p", "Delete “" + displayTitle(note) + "”?"),
    action("Cancel", () => noteDetail(note)),
    action("Delete note", async () => {
      const key = noteKey(note);
      if (noteMutations.has(key)) return;
      noteMutations.add(key);
      try {
        await api("delete-note", {}, { project: note.project, id: note.id, version: note.version });
      } finally {
        noteMutations.delete(key);
      }
      noteDetails.clear();
      if (noteCut?.id === note.id) noteCut = null;
      if (box.isConnected) $("detail").close();
      noteEpoch++;
      await refresh();
    }),
  );
}

async function pasteNote(project, row) {
  if (!noteCut || noteCut.project !== project) return;
  const cut = noteCut;
  const key = noteKey(cut);
  if (noteMutations.has(key)) return;
  if (noteDrafts.has(key)) throw new Error("Sync this note's local draft first.");
  noteMutations.add(key);
  try {
    await api("move-note", {}, {
      project, id: cut.id, version: cut.version, thread: row.thread, turn: row.turn,
    });
  } finally {
    noteMutations.delete(key);
  }
  if (noteCut === cut) noteCut = null;
  noteDetails.clear();
  noteEpoch++;
  await refresh();
}

async function openNote(project, preview, edit = false) {
  const key = noteKey({ project, id: preview.id });
  const draft = noteDrafts.get(key);
  if (draft) {
    noteEditor(draft, true);
    return;
  }
  const loading = node("p", "Loading note…");
  modal(displayTitle(preview), loading);
  detailRequest = new AbortController();
  try {
    const note = { ...(await fullNote(project, preview, detailRequest.signal)), project };
    if (!$("detail").open || $("modalBody").firstChild !== loading) return;
    if (edit) noteEditor(note, true);
    else noteDetail(note);
  } catch (error) {
    if (loading.isConnected && error.name !== "AbortError") loading.textContent = error.message;
  }
}

function editNote(project, target = {}, note = null) {
  if (note) return openNote(project, note, true);
  noteEditor({ project, id: crypto.randomUUID(), body: "", version: null,
    thread: target.thread ?? null, turn: target.turn ?? null }, true);
}

function graphemeMotion(text, position, key) {
  if (key === "h") return segments.segment(text).containing(Math.max(0, position - 1))?.index ?? 0;
  if (key === "l") {
    const cluster = segments.segment(text).containing(position);
    return cluster ? cluster.index + cluster.segment.length : text.length;
  }
  const start = position ? text.lastIndexOf("\n", position - 1) + 1 : 0;
  const newline = text.indexOf("\n", position);
  const end = newline < 0 ? text.length : newline;
  if (key === "0") return start;
  if (key === "$") return end;
  if (key === "G") return text.length;
  if (key === "gg") return 0;
  if (key === "j" && end === text.length || key === "k" && start === 0) return position;
  const nextStart = key === "j" ? end + 1 : start > 1 ? text.lastIndexOf("\n", start - 2) + 1 : 0;
  const nextEnd = text.indexOf("\n", nextStart);
  const column = [...segments.segment(text.slice(start, position))].length;
  const next = text.slice(nextStart, nextEnd < 0 ? text.length : nextEnd);
  return nextStart + [...segments.segment(next)].slice(0, column)
    .reduce((total, part) => total + part.segment.length, 0);
}

function noteEditor(note, insert = false) {
  const work = { ...note };
  const key = noteKey(work);
  if (noteMutations.has(key)) return;
  const form = node("form", undefined, "note-editor");
  const area = node("textarea");
  const preview = node("div", undefined, "note-preview");
  const status = node("span", undefined, "meta");
  const conflict = node("div", undefined, "note-conflict");
  let mode = insert ? "Insert" : "Normal";
  let sequence = "";
  let command = null;
  let previewTimer;
  let changed = noteDrafts.has(key);
  let conflictShown = false;
  const history = [];

  area.value = work.body;
  area.maxLength = Math.max(40000, work.body.length);
  area.setAttribute("aria-label", "Note text");
  status.setAttribute("role", "status");
  const controls = node("div", undefined, "note-actions");
  controls.setAttribute("role", "group");
  controls.setAttribute("aria-label", "Editor actions");
  controls.append(
    action("Save", () => save(true)),
    action("Preview", () => form.classList.toggle("show-preview")),
    status,
  );
  const contents = node("label", "Markdown · # Title", "note-source");
  contents.append(area);
  form.append(controls, contents, preview, conflict);
  form.onsubmit = event => { event.preventDefault(); save(true); };

  function update() {
    status.textContent = mode + " · " + (changed ? draftState(key) : "Editing") +
      (command == null ? "" : " · :" + command);
    const waiting = noteDrafts.get(key)?.conflict === true;
    if (waiting && !conflictShown) {
      conflict.replaceChildren(action("Review server version", review));
      conflictShown = true;
    } else if (!waiting && conflictShown) {
      conflict.replaceChildren();
      conflictShown = false;
    }
  }
  function save(immediate) {
    enqueueDraft(work, immediate);
    if (immediate) pumpDrafts();
    update();
  }
  function updatePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => preview.replaceChildren(renderMarkdown(area.value)), 140);
  }
  function modified() {
    if (work.body === area.value) return;
    history.push(work.body);
    if (history.length > 50) history.shift();
    work.body = area.value;
    changed = true;
    updatePreview();
    save(false);
  }
  async function review() {
    try {
      const latest = await api("note", { project: work.project, id: work.id });
      conflict.replaceChildren(
        node("strong", "Server version · " + displayTitle(latest)),
        renderMarkdown(latest.body),
        action("Keep draft", () => {
          const draft = noteDrafts.get(key);
          draft.version = latest.version;
          draft.conflict = false;
          work.version = latest.version;
          save(true);
        }),
        action("Use server", () => {
          discardDraft(key);
          activeNoteEditor = null;
          changed = false;
          noteDetail(latest);
        }),
      );
    } catch (error) {
      conflict.append(node("p", error.message));
    }
  }
  area.addEventListener("input", event => { if (!event.isComposing) modified(); });
  area.addEventListener("compositionend", modified);
  area.addEventListener("paste", () => { mode = "Insert"; update(); });
  area.addEventListener("keydown", event => {
    if (event.isComposing) return;
    const pressed = event.key;
    if ((event.ctrlKey || event.metaKey) && pressed === "s") {
      event.preventDefault();
      save(true);
      return;
    }
    if (event.altKey || event.metaKey || event.ctrlKey) return;
    if (pressed === "Escape" && mode === "Insert") {
      event.preventDefault();
      mode = "Normal";
      update();
      return;
    }
    if (mode === "Insert" || ["Escape", "Tab", "Shift"].includes(pressed)) return;
    event.preventDefault();
    if (command !== null) {
      if (pressed === "Enter") {
        if (command.startsWith("w")) save(true);
        if (command.endsWith("q")) closeJournalDialog();
        command = null;
      } else if (/^[wq]$/.test(pressed)) command = (command + pressed).slice(0, 2);
      update();
      return;
    }
    if (pressed === ":") { command = ""; update(); return; }
    const { value, selectionStart: position } = area;
    const keypress = sequence + pressed;
    sequence = ["g", "d"].includes(pressed) && keypress.length === 1 ? pressed : "";
    if ("ia".includes(pressed) && pressed.length === 1) {
      if (pressed === "a") area.setSelectionRange(graphemeMotion(value, position, "l"), graphemeMotion(value, position, "l"));
      mode = "Insert";
    } else if (["h", "j", "k", "l", "0", "$", "gg", "G"].includes(keypress)) {
      const next = graphemeMotion(value, position, keypress);
      area.setSelectionRange(next, next);
    } else if (keypress === "x") {
      area.setRangeText("", position, graphemeMotion(value, position, "l"), "start");
      modified();
    } else if (keypress === "dd") {
      const start = graphemeMotion(value, position, "0");
      const end = graphemeMotion(value, position, "$");
      area.setRangeText("", start, Math.min(value.length, end + 1), "start");
      modified();
    } else if (keypress === "o") {
      const end = graphemeMotion(value, position, "$");
      area.setRangeText("\n", end, end, "end");
      mode = "Insert";
      modified();
    } else if (keypress === "u" && history.length) {
      area.value = work.body = history.pop();
      updatePreview();
      save(false);
    }
    update();
  });
  activeNoteEditor = {
    key, update,
    acknowledge(version) { work.version = version; },
    leaving() { if (changed && noteDrafts.has(key)) save(true); },
  };
  modal(work.version == null ? "Add " + noteContext(work).toLowerCase() : "Edit " + displayTitle(work), form);
  update();
  updatePreview();
  area.focus();
}

readDrafts();
