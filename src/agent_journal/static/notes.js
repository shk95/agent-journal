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
      if (activeNoteEditor?.key === key) activeNoteEditor.acknowledge(version);
      if (current?.body === sent.body) discardDraft(key);
      else if (current) {
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

const inlineMarkdown = /\\([\\*_\[\]#!<>&\x60])|\x60([^\x60\n]+)\x60|!?\[([^\]\n]+)\]\(([^)\s]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|_([^_\n]+)_/gu;

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
    else container.append(node("em", match[7] ?? match[8]));
    offset = match.index + match[0].length;
  }
  container.append(text.slice(offset));
}

function wrapText(container, text, render = (line, value) => { line.textContent = value; }) {
  const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
  const content = document.createDocumentFragment();
  for (const [index, value] of lines.entries()) {
    if (index) content.append("\n");
    const line = node("span", undefined, "wrap-line");
    render(line, value, index < lines.length - 1);
    content.append(line);
  }
  container.append(content);
  return container;
}

function renderWrappedInline(container, text) {
  return wrapText(container, text, (line, value, followed) => {
    const prose = value.trim();
    renderInline(line, followed ? prose.replace(/\\$/, "") : prose);
  });
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
      pre.append(wrapText(node("code"), code.join("\n")));
      out.append(pre);
      continue;
    }
    const heading = line.match(atxHeading);
    const entry = line.match(listLine);
    if (heading) {
      const title = node("h" + Math.min(6, heading[1].length + 1));
      renderWrappedInline(title, (heading[2] ?? "").replace(/(?:^|[ \t]+)#+[ \t]*$/, "").trim());
      out.append(title);
    } else if (indentCode.test(line)) {
      const code = [line.replace(indentCode, "")];
      while (indentCode.test(lines[i] ?? "")) code.push(lines[i++].replace(indentCode, ""));
      const pre = node("pre");
      pre.append(wrapText(node("code"), code.join("\n")));
      out.append(pre);
    } else if (entry) {
      const list = node(entry[1] === undefined ? "ul" : "ol");
      if (entry[1] !== undefined) list.start = Number(entry[1]);
      let current = entry;
      do {
        const item = node("li");
        renderWrappedInline(item, current[2]);
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
      renderWrappedInline(block, content.join(underline ? " " : "\n"));
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
  const cut = note.turn && note.version != null
    ? action("Cut", () => {
      noteCut = note;
      $("detail").close();
      $("connection").textContent = "Turn note cut · select a destination";
      noteEpoch++;
      refresh();
    }) : null;
  const remove = note.version != null ? action("Delete", () => confirmNoteDelete(note, box)) : null;
  if (cut) controls.append(cut);
  if (remove) controls.append(remove);
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
    if (event.key === "x" && cut) { event.preventDefault(); cut.click(); }
    if (event.key === "D" && remove) { event.preventDefault(); remove.click(); }
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
    if (/^[123]$/.test(event.key)) { event.preventDefault(); options[Number(event.key) - 1].click(); }
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

function editorWrapping(area, container) {
  const gutter = node("div", undefined, "wrap-gutter");
  const markers = node("div");
  const selection = node("div", undefined, "vi-block-selection");
  const blocks = node("div");
  const measure = node("div", undefined, "wrap-measure");
  const source = document.createTextNode("");
  const endMarker = document.createTextNode("\u200b");
  const range = document.createRange();
  gutter.setAttribute("aria-hidden", "true");
  selection.setAttribute("aria-hidden", "true");
  measure.setAttribute("aria-hidden", "true");
  gutter.append(markers);
  selection.append(blocks);
  measure.append(source, endMarker);
  container.append(area, gutter, selection, measure);
  let frame;
  let ranges = [];

  function scroll() {
    markers.style.transform = `translateY(${-area.scrollTop}px)`;
    blocks.style.transform = markers.style.transform;
  }

  function populateMeasure(text = area.value) {
    const width = `${area.clientWidth}px`;
    if (measure.style.width !== width) measure.style.width = width;
    if (source.data !== text) source.data = text;
  }

  function caretRect(position) {
    const text = source.data;
    if (position === text.length) {
      range.setStart(endMarker, 0);
      range.collapse(true);
      return range.getBoundingClientRect();
    }
    const part = VI_SEGMENTS.segment(text).containing(position);
    range.setStart(source, part.index);
    range.setEnd(source, part.index + part.segment.length);
    const { left, top, height } = range.getBoundingClientRect();
    return { left, top, height };
  }

  function update() {
    frame = null;
    if (!area.clientWidth) return;
    populateMeasure();
    const lineHeight = parseFloat(getComputedStyle(measure).lineHeight);
    const top = measure.getBoundingClientRect().top + parseFloat(getComputedStyle(measure).paddingTop);
    const content = document.createDocumentFragment();
    let start = 0;
    for (const line of source.data.split("\n")) {
      if (line) {
        range.setStart(source, start);
        range.setEnd(source, start + line.length);
        const rows = new Set([...range.getClientRects()].map(rect => Math.round((rect.top - top) / lineHeight)));
        rows.delete(Math.min(...rows));
        for (const row of rows) {
          const mark = node("span", "↳");
          mark.style.top = `${row * lineHeight}px`;
          content.append(mark);
        }
      }
      start += line.length + 1;
    }
    markers.replaceChildren(content);
    const origin = measure.getBoundingClientRect();
    const highlight = document.createDocumentFragment();
    for (const [start, end] of ranges) {
      if (start === end) continue;
      range.setStart(source, start);
      range.setEnd(source, end);
      for (const rect of range.getClientRects()) {
        const block = node("span");
        block.style.left = `${rect.left - origin.left}px`;
        block.style.top = `${Math.round((rect.top - top) / lineHeight) * lineHeight + top - origin.top}px`;
        block.style.width = `${rect.width}px`;
        block.style.height = `${lineHeight}px`;
        highlight.append(block);
      }
    }
    blocks.replaceChildren(highlight);
    scroll();
  }

  function schedule() {
    if (frame == null) frame = requestAnimationFrame(update);
  }

  const observer = new ResizeObserver(schedule);
  observer.observe(area);
  area.addEventListener("scroll", scroll);
  return {
    schedule,
    select(block) {
      if (block.length || ranges.length) {
        ranges = block;
        schedule();
      }
    },
    move(text, position, rows) {
      if (!text) return position;
      populateMeasure(text);
      const initial = caretRect(position);
      const height = parseFloat(getComputedStyle(measure).lineHeight);
      const top = Math.max(caretRect(0).top, Math.min(caretRect(text.length).top, initial.top + rows * height));
      if (Math.abs(top - initial.top) < 1) return position;
      let low = 0, high = text.length;
      while (low < high) {
        const mid = Math.floor((low + high) / 2);
        if (caretRect(mid).top < top - 1) low = mid + 1;
        else high = mid;
      }
      let cursor = VI_SEGMENTS.segment(text).containing(low)?.index ?? low;
      let target = cursor, distance = Infinity;
      while (true) {
        const rect = caretRect(cursor);
        if (rect.top > top + 1) break;
        if (rect.top >= top - 1 && Math.abs(rect.left - initial.left) < distance) {
          target = cursor;
          distance = Math.abs(rect.left - initial.left);
        }
        if (cursor === text.length) break;
        cursor = viNext(text, cursor);
      }
      return target;
    },
    reveal(position) {
      populateMeasure();
      const rect = caretRect(position);
      if (!rect.height) return;
      const top = rect.top - measure.getBoundingClientRect().top;
      if (top < area.scrollTop) area.scrollTop = top;
      else if (top + rect.height > area.scrollTop + area.clientHeight)
        area.scrollTop = top + rect.height - area.clientHeight;
    },
    close() {
      observer.disconnect();
      if (frame != null) cancelAnimationFrame(frame);
    },
  };
}

function noteEditor(note, insert = false) {
  const work = { ...note };
  const key = noteKey(work);
  if (noteMutations.has(key)) return;
  const form = node("form", undefined, "note-editor");
  const area = node("textarea");
  const sourceEditor = node("div", undefined, "note-source-editor");
  const wrapping = editorWrapping(area, sourceEditor);
  const editor = new ViEditor(work.body, insert ? "Insert" : "Normal", wrapping.move);
  const preview = node("div", undefined, "note-preview");
  const status = node("span", undefined, "meta");
  const conflict = node("div", undefined, "note-conflict");
  const commandLine = node("label", undefined, "vi-command");
  const commandPrefix = node("span");
  const commandInput = node("input");
  commandInput.type = "text";
  commandInput.maxLength = 499;
  commandInput.setAttribute("aria-label", "Editor command");
  commandInput.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  commandLine.hidden = true;
  commandLine.append(commandPrefix, commandInput);
  const help = node("section", undefined, "vi-help");
  help.hidden = true;
  const shortcuts = node("ul");
  shortcuts.append(...VI_HELP.map(line => node("li", line)));
  help.append(node("h2", "Editor keys"), shortcuts);
  let previewTimer;
  let changed = noteDrafts.has(key);
  let conflictShown = false;

  area.value = work.body;
  area.wrap = "soft";
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
  const hint = node("p", `${VI_NORMAL_SHORTCUT} normal / cancel · :help keys · :q close · :q! discard · :w save · Ctrl+P preview · ↳ wrapped line`, "meta editor-hint");
  controls.append(hint);
  const contents = node("label", "Markdown · # Title", "note-source");
  contents.append(sourceEditor);
  form.append(controls, contents, preview, commandLine, help, conflict);
  form.onsubmit = event => {
    event.preventDefault();
    if (editor.command) {
      for (const key of commandInput.value) editor.input(key);
      execute(editor.input("Enter"));
    } else save(true);
  };

  function update() {
    status.textContent = editor.mode + " · " + (changed ? draftState(key) : "Editing") +
      (editor.recording ? " · Recording @" + editor.recording : "") +
      (editor.pending.length ? " · " + editor.pending.join("") : "") +
      (editor.message ? " · " + editor.message : "");
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
  function paintEditor() {
    if (area.value !== editor.text) area.value = editor.text;
    const [kind, ranges] = editor.selection();
    const [start, end] = kind !== "block" && ranges.length ? ranges[0] : [editor.position, editor.position];
    if (area.selectionStart !== start || area.selectionEnd !== end) {
      area.setSelectionRange(start, end, editor.position === start ? "backward" : "forward");
    }
    wrapping.select(kind === "block" ? ranges : []);
    wrapping.reveal(editor.position);
    if (work.body !== editor.text) {
      work.body = editor.text;
      changed = true;
      wrapping.schedule();
      updatePreview();
      save(false);
    }
    if (editor.command && commandLine.hidden) {
      commandPrefix.textContent = editor.command[0];
      commandInput.value = "";
      commandLine.hidden = false;
      commandInput.focus();
    } else if (!editor.command && !commandLine.hidden) {
      commandLine.hidden = true;
      area.focus({ preventScroll: true });
    }
    if (editor.help && help.hidden) {
      help.hidden = false;
      help.scrollIntoView({ block: "nearest" });
    } else if (!editor.help) help.hidden = true;
    update();
  }

  function execute(result) {
    paintEditor();
    if (result === "discard-close") {
      discardDraft(key);
      noteEpoch++;
      closeJournalDialog();
      return;
    }
    if (result === "save" || result === "save-close") save(true);
    if (result === "close" || result === "save-close") closeJournalDialog();
    if (result === "review" && noteDrafts.get(key)?.conflict) review();
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
          changed = false;
          finishNoteEditing();
          noteDetail(latest);
        }),
      );
    } catch (error) {
      conflict.append(node("p", error.message));
    }
  }
  let previousPosition = 0;
  let composing = false;
  area.addEventListener("beforeinput", () => {
    if (!composing) previousPosition = area.selectionStart;
  });
  area.addEventListener("compositionstart", () => {
    composing = true;
    previousPosition = area.selectionStart;
  });
  area.addEventListener("input", event => {
    if (event.isComposing) wrapping.schedule();
    else {
      editor.nativeEdit(area.value, area.selectionStart, previousPosition);
      paintEditor();
    }
  });
  area.addEventListener("compositionend", () => {
    composing = false;
    editor.nativeEdit(area.value, area.selectionStart, previousPosition);
    paintEditor();
  });
  area.addEventListener("paste", () => {
    if (!editor.inserting) {
      if (editor.anchor !== null) editor.input("Escape");
      editor.position = area.selectionStart;
      editor.input("i");
    }
  });
  area.addEventListener("pointerup", () => {
    if (editor.anchor !== null) editor.input("Escape");
    editor.position = area.selectionStart;
    wrapping.select([]);
    update();
  });
  form.addEventListener("keydown", event => {
    if (event.key !== ";" || !event.ctrlKey || event.altKey || event.metaKey ||
        event.isComposing || composing) return;
    event.preventDefault();
    if (editor.inserting || (editor.anchor === null && document.activeElement === area && !editor.command)) {
      editor.position = area.selectionStart;
    }
    execute(editor.input("Escape"));
    area.focus({ preventScroll: true });
  });
  area.addEventListener("keydown", event => {
    if (event.isComposing || composing) return;
    const pressed = event.key;
    if ((event.ctrlKey || event.metaKey) && pressed === "s") {
      event.preventDefault();
      save(true);
      return;
    }
    if (event.ctrlKey && pressed === "p") {
      event.preventDefault();
      form.classList.toggle("show-preview");
      return;
    }
    if (event.ctrlKey && pressed === "r" && !editor.inserting) {
      event.preventDefault();
      execute(editor.input("\x12"));
      return;
    }
    if (event.ctrlKey && pressed === "v" && !editor.inserting) {
      event.preventDefault();
      if (editor.anchor === null) editor.position = area.selectionStart;
      execute(editor.input("\x16"));
      return;
    }
    if (event.altKey || event.metaKey || event.ctrlKey || ["Escape", "Tab", "Shift"].includes(pressed)) return;
    if (editor.inserting && (!Object.hasOwn(VI_ARROWS, pressed) || event.shiftKey)) return;
    event.preventDefault();
    if (editor.anchor === null) editor.position = area.selectionStart;
    execute(editor.input(pressed));
  });
  activeNoteEditor = {
    key, update,
    acknowledge(version) { work.version = version; },
    leaving() {
      wrapping.close();
      clearTimeout(previewTimer);
      if (changed && noteDrafts.has(key)) save(true);
    },
  };
  modal(work.version == null ? "Add " + noteContext(work).toLowerCase() : "Edit " + displayTitle(work), form);
  update();
  updatePreview();
  area.focus();
}

readDrafts();
