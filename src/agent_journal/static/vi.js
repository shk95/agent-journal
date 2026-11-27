"use strict";

// The terminal implementation is in vi.py. Exercise both with the same key sequences.
const VI_NORMAL_SHORTCUT = "Ctrl+;";
const VI_HELP = [
  "Movement  h j k l · w b e / W B E · 0 ^ $ · gg G · gj gk · f/F/t/T ; , · % { }",
  "Counts  3w · 2dd · d2w · 5G",
  "Operators  d c y > < + motion · dd cc yy >> << · iw aw iW aW · i/a + quotes or brackets",
  `Selection  v character · V line · Ctrl+V block · o swap ends · ${VI_NORMAL_SHORTCUT} normal / cancel`,
  "Editing  i I a A o O · x X s S · D C · r R · J · ~ · p P · \"ayw / \"ap",
  "Search  / and ? · n N · :s/pattern/replacement/g · :%s/pattern/replacement/g",
  "History  u undo · Ctrl+R redo · . repeat · ma mark · 'a line · \x60a exact position",
  "Macros  qa record · q stop · @a play · @@ repeat macro",
  "Journal  :w save · :wq save and close · :q close · :q! discard local draft · :review review conflict · Ctrl+P preview",
];

const VI_SEGMENTS = new Intl.Segmenter(undefined, { granularity: "grapheme" });
const VI_LIMIT = 10000;
const VI_OPERATORS = "dcy><";
const VI_MOTIONS = new Set([...("hjkl0^$wbeWBEG;,%{}"), "gg", "gj", "gk", "f", "F", "t", "T", "'", "\x60"]);
const VI_PAIRS = { "(": ")", "[": "]", "{": "}", "<": ">" };
const VI_ARROWS = { ArrowLeft: "h", ArrowRight: "l", ArrowUp: "gk", ArrowDown: "gj", Home: "0", End: "$" };

function viBound(values, pos, right) {
  let low = 0, high = values.length;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    if (values[mid] < pos || (right && values[mid] === pos)) low = mid + 1;
    else high = mid;
  }
  return low;
}

function viStart(text, pos) {
  return pos ? text.lastIndexOf("\n", pos - 1) + 1 : 0;
}

function viEnd(text, pos) {
  const end = text.indexOf("\n", pos);
  return end < 0 ? text.length : end;
}

function viNonblank(text, pos) {
  const start = viStart(text, pos);
  const line = text.slice(start, viEnd(text, pos));
  return start + line.length - line.trimStart().length;
}

function viNext(text, pos) {
  const part = VI_SEGMENTS.segment(text).containing(pos);
  return part ? part.index + part.segment.length : text.length;
}

function viPrevious(text, pos) {
  return pos ? VI_SEGMENTS.segment(text).containing(pos - 1)?.index ?? 0 : 0;
}

function viLines(text) {
  return [0, ...Array.from(text.matchAll(/\n/g), item => item.index + 1)];
}

function viColumn(text, pos) {
  return Array.from(VI_SEGMENTS.segment(text.slice(viStart(text, pos), pos))).length;
}

function viAtColumn(text, start, end, column) {
  let result = start;
  for (const { segment } of VI_SEGMENTS.segment(text.slice(start, end))) {
    if (column-- <= 0) break;
    result += segment.length;
  }
  return result;
}

function viSwapCase(text) {
  return Array.from(text, char => char === char.toLowerCase() ? char.toUpperCase() : char.toLowerCase()).join("");
}

function viWordEdges(text, big) {
  const starts = [], ends = [];
  let previous = 0;
  for (const { index, segment } of VI_SEGMENTS.segment(text)) {
    const kind = /^\s/u.test(segment) ? 0 : big || /^[\p{L}\p{N}_]/u.test(segment) ? 1 : 2;
    if (kind) {
      if (kind !== previous) {
        starts.push(index);
        ends.push(index);
      } else ends[ends.length - 1] = index;
    }
    previous = kind;
  }
  return [starts, ends];
}

function viParse(keys, visual) {
  let index = 0, count = readCount(), register = '"';
  function readCount() {
    if (!/^[1-9]$/.test(keys[index] ?? "")) return 0;
    let number = 0;
    while (/^[0-9]$/.test(keys[index] ?? "")) number = Math.min(VI_LIMIT, number * 10 + Number(keys[index++]));
    return number;
  }
  function extendCount() {
    const following = readCount();
    if (following) count = Math.min(VI_LIMIT, (count || 1) * following);
  }
  if (keys[index] === '"') {
    register = keys[++index];
    if (register === undefined) return null;
    if (!/^["_0-9a-z]$/i.test(register)) return {};
    index++;
    extendCount();
  }
  let key = keys[index], operator = "", argument = "";
  if (key === undefined) return null;
  if (VI_OPERATORS.includes(key) && !visual) {
    operator = key;
    index++;
    extendCount();
    key = keys[index];
    if (key === undefined) return null;
    if (key === operator) return { key: "line", count, operator, register };
  }
  const object = (operator || visual) && (key === "a" || key === "i");
  if (key === "g" || "fFtT'\x60mrq@".includes(key) || object) {
    argument = keys[index + 1];
    if (argument === undefined) return null;
    if (key === "g") {
      key += argument;
      argument = "";
    }
    if (object) key = "object-" + key;
  }
  return { key, argument, count, operator, register };
}

function viPairs(text, left, right) {
  const stack = [], pairs = [];
  let escaped = false;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (left === right) {
      if (char === left && !escaped) {
        if (stack.length) pairs.push([stack.pop(), index]);
        else stack.push(index);
      }
      escaped = char === "\\" && !escaped;
    } else if (char === left) stack.push(index);
    else if (char === right && stack.length) pairs.push([stack.pop(), index]);
  }
  return pairs;
}

function viObject(text, pos, inner, key, count) {
  if (key === "w" || key === "W") {
    const [starts, ends] = viWordEdges(text, key === "W");
    let index = viBound(starts, pos, true) - 1;
    if (index < 0 || pos >= viNext(text, ends[index])) index++;
    if (index >= starts.length) return null;
    let start = starts[index];
    let end = viNext(text, ends[Math.min(index + count - 1, ends.length - 1)]);
    if (!inner) {
      const formerEnd = end;
      while (end < text.length && /[ \t]/.test(text[end])) end++;
      if (formerEnd === end) while (start && /[ \t]/.test(text[start - 1])) start--;
    }
    return [start, end];
  }
  key = { b: "(", B: "{" }[key] ?? key;
  const quotes = ['"', "'", "\x60"].includes(key);
  const left = Object.keys(VI_PAIRS).find(open => open === key || VI_PAIRS[open] === key) ?? key;
  if (!quotes && !Object.hasOwn(VI_PAIRS, left)) return null;
  const offset = quotes ? viStart(text, pos) : 0;
  const source = quotes ? text.slice(offset, viEnd(text, pos)) : text;
  const pairs = viPairs(source, left, VI_PAIRS[left] ?? left).map(([a, b]) => [a + offset, b + offset]);
  let around = pairs.filter(([a, b]) => a <= pos && pos <= b).sort((a, b) => b[0] - a[0]);
  if (!around.length && quotes) around = pairs.filter(([a]) => a >= pos);
  if (count > around.length) return null;
  const [start, end] = around[count - 1];
  return [start + Number(inner), end + Number(!inner)];
}

class ViEditor {
  constructor(text, mode = "Normal", screenMove = null) {
    this.text = text;
    this.mode = mode;
    this.position = 0;
    this.screenMove = screenMove;
    this.command = "";
    this.pending = [];
    this.message = "";
    this.help = false;
    this.anchor = null;
    this.visualKeys = [];
    this.history = [];
    this.redo = [];
    this.edit = null;
    this.blockInsert = null;
    this.insertRepeat = null;
    this.lastEdit = [];
    this.marks = new Map();
    this.registers = new Map();
    this.find = null;
    this.search = null;
    this.recording = null;
    this.recorded = [];
    this.macros = new Map();
    this.lastMacro = null;
    this.playing = 0;
  }

  get inserting() { return this.mode === "Insert" || this.mode === "Replace"; }

  snapshot() { return [this.text, this.position, new Map(this.marks)]; }

  restore([text, position, marks]) {
    this.text = text;
    this.position = position;
    this.marks = new Map(marks);
  }

  begin(keys) {
    if (!this.edit) this.edit = [this.snapshot(), [...keys]];
  }

  commit() {
    if (!this.edit) return;
    const [snapshot, keys] = this.edit;
    if (snapshot[0] !== this.text) {
      this.history.push(snapshot);
      if (this.history.length > 50) this.history.shift();
      this.redo.length = 0;
      this.lastEdit = keys;
    }
    this.edit = null;
  }

  replace(start, end, value, position = start + value.length) {
    const result = this.text.slice(0, start) + value + this.text.slice(end);
    if (result.length > Math.max(40000, this.text.length) &&
        [...result].length > Math.max(40000, [...this.text].length)) {
      this.message = "Note is too long";
      return;
    }
    this.text = result;
    this.position = Math.max(0, Math.min(result.length, position));
    const delta = value.length - (end - start);
    for (const [mark, offset] of this.marks) {
      if (offset >= end) this.marks.set(mark, offset + delta);
      else if (offset >= start) this.marks.set(mark, start);
    }
  }

  finishInsert() {
    if (this.insertRepeat) {
      const [offset, count] = this.insertRepeat;
      const keys = this.edit[1].slice(offset);
      const repeats = Math.min(count - 1, Math.floor(VI_LIMIT / Math.max(keys.length, 1)));
      for (let n = 0; n < repeats; n++) for (const key of keys) this.insert(key, false);
      this.insertRepeat = null;
    }
    if (this.blockInsert) {
      const [original, start, targets] = this.blockInsert;
      const added = this.text.length - original.length;
      const value = this.text.slice(start, start + added);
      if (added > 0 && !value.includes("\n") && this.text.startsWith(original.slice(0, start)) &&
          this.text.endsWith(original.slice(start))) {
        if ([...this.text].length + [...value].length * targets.length <= 40000) {
          for (const target of [...targets].reverse()) this.replace(target + added, target + added, value, this.position);
        } else this.message = "Note is too long";
      }
      this.blockInsert = null;
    }
  }

  seek(key, argument, count, repeat) {
    const { text, position } = this;
    const forward = key === "f" || key === "t";
    const start = viStart(text, position), end = viEnd(text, position);
    const hits = [];
    for (const { index, segment } of VI_SEGMENTS.segment(text.slice(start, end))) {
      const at = start + index;
      if (segment !== argument || (forward ? at <= position : at >= position)) continue;
      const target = key === "t" ? viPrevious(text, at) : key === "T" ? at + segment.length : at;
      if (!repeat || target !== position) hits.push(target);
    }
    return hits.length < count ? null : forward ? hits[count - 1] : hits.at(-count);
  }

  motion(command) {
    const { key, operator } = command;
    let { argument } = command;
    let { text, position: pos } = this;
    const count = command.count || 1;
    const start = viStart(text, pos), end = viEnd(text, pos);
    let inclusive = false, linewise = false;
    if (key === "h" || key === "l") {
      for (let step = 0; step < count; step++) {
        const following = key === "h" ? Math.max(start, viPrevious(text, pos)) : Math.min(end, viNext(text, pos));
        if (following === pos) break;
        pos = following;
      }
    } else if (key === "0" || key === "^") pos = key === "0" ? start : viNonblank(text, pos);
    else if (key === "$") {
      const offsets = viLines(text), row = viBound(offsets, pos, true) - 1;
      const target = offsets[Math.min(row + count - 1, offsets.length - 1)];
      pos = viEnd(text, target);
      if (pos > target && !operator && !this.inserting) pos = viPrevious(text, pos);
    } else if ("wbeWBE".includes(key)) {
      const [starts, ends] = viWordEdges(text, key === key.toUpperCase());
      const word = key.toLowerCase();
      const change = operator === "c" && word === "w" && pos < text.length && !/\s/u.test(text[pos]);
      if (word === "b") pos = starts[viBound(starts, pos, false) - count] ?? 0;
      else {
        const edges = word === "e" || change ? ends : starts;
        pos = edges[viBound(edges, pos, !change) + count - 1] ??
          (operator && edges === starts ? text.length : Math.max(pos, viPrevious(text, text.length)));
      }
      if (operator && word === "w" && count === 1 && this.position < end && end < pos) pos = end;
      else inclusive = word === "e" || Boolean(change);
    } else if (["gg", "G", "j", "k", "gj", "gk", "line"].includes(key)) {
      const offsets = viLines(text), row = viBound(offsets, pos, true) - 1;
      if (key === "gg" || key === "G") {
        const target = command.count ? count - 1 : key === "gg" ? 0 : offsets.length - 1;
        pos = viNonblank(text, offsets[Math.min(target, offsets.length - 1)]);
        linewise = true;
      } else if (this.screenMove && (key === "gj" || key === "gk")) {
        pos = this.screenMove(text, pos, key === "gj" ? count : -count);
      } else {
        const delta = key === "line" ? count - 1 : key === "j" || key === "gj" ? count : -count;
        const target = offsets[Math.max(0, Math.min(row + delta, offsets.length - 1))];
        pos = viAtColumn(text, target, viEnd(text, target), viColumn(text, pos));
        linewise = key === "j" || key === "k" || key === "line";
      }
    } else if ("fFtT;,".includes(key)) {
      let method = key;
      const repeat = key === ";" || key === ",";
      if (repeat) {
        if (!this.find) return null;
        [method, argument] = this.find;
        if (key === ",") method = method === method.toLowerCase() ? method.toUpperCase() : method.toLowerCase();
      } else this.find = [key, argument];
      pos = this.seek(method, argument, count, repeat);
      inclusive = true;
    } else if (key === "'" || key === "\x60") {
      pos = this.marks.get(argument) ?? null;
      linewise = key === "'";
      if (pos !== null && linewise) pos = viNonblank(text, pos);
    } else if (key === "%") {
      const left = Object.keys(VI_PAIRS).find(open => open === text[pos] || VI_PAIRS[open] === text[pos]);
      const pair = left && viPairs(text, left, VI_PAIRS[left]).find(pair => pair.includes(pos));
      if (!pair) return null;
      pos = pair[0] === pos ? pair[1] : pair[0];
      inclusive = true;
    } else if (key === "{" || key === "}") {
      const edges = Array.from(text.matchAll(/\n[ \t]*\n/g), match => match.index + 1);
      const index = key === "{" ? viBound(edges, pos, false) - count : viBound(edges, pos, true) + count - 1;
      pos = edges[index] ?? (key === "{" ? 0 : text.length);
    } else return null;
    return pos === null ? null : [pos, inclusive, linewise];
  }

  lineRange(first, last) {
    return [viStart(this.text, Math.min(first, last)), Math.min(this.text.length, viEnd(this.text, Math.max(first, last)) + 1)];
  }

  selection() {
    if (this.anchor === null) return ["char", []];
    const { anchor, position, text } = this;
    if (this.mode === "Visual line") return ["line", [this.lineRange(anchor, position)]];
    const first = Math.min(anchor, position), last = Math.max(anchor, position);
    if (this.mode === "Visual block") {
      const a = viColumn(text, anchor), b = viColumn(text, position);
      const left = Math.min(a, b), right = Math.max(a, b);
      const offsets = viLines(text);
      const rows = offsets.slice(viBound(offsets, first, true) - 1, viBound(offsets, last, true));
      return ["block", rows.map(start => {
        const end = viEnd(text, start);
        return [viAtColumn(text, start, end, left), viAtColumn(text, start, end, right + 1)];
      })];
    }
    return ["char", [[first, viNext(text, last)]]];
  }

  store(name, kind, pieces, yank) {
    if (name === "_") return;
    const named = name.toLowerCase();
    const previous = name !== named ? this.registers.get(named) : null;
    if (previous) {
      const [oldKind, old] = previous;
      if (oldKind === "block" && kind === "block") {
        pieces = Array.from({ length: Math.max(old.length, pieces.length) },
          (_, i) => (old[i] ?? "") + (pieces[i] ?? ""));
      } else pieces = [old[0] + pieces[0]];
    }
    const target = [kind, pieces];
    this.registers.set('"', target);
    this.registers.set(named, target);
    if (yank) this.registers.set("0", target);
  }

  operate(operator, register, kind, ranges, count = 1) {
    if (!ranges.length || (!ranges.some(([start, end]) => start !== end) && kind !== "line")) {
      if (operator === "c" && kind !== "block") this.mode = "Insert";
      return;
    }
    const text = this.text, first = ranges[0][0];
    if (operator === ">" || operator === "<") {
      const [start, end] = this.lineRange(first, Math.max(first, ranges.at(-1)[1] - 1));
      const changed = text.slice(start, end).replace(/[^\n]+/g, line => {
        if (!line.trim()) return line;
        if (operator === ">") return " ".repeat(4 * count) + line;
        return line.replace(new RegExp("^(?: {1,4}|\\t){1," + count + "}"), "");
      });
      this.replace(start, end, changed, start);
    } else {
      const parts = ranges.map(([start, end]) => text.slice(start, end));
      if (kind === "line" && !parts[0].endsWith("\n")) parts[0] += "\n";
      this.store(register, kind, parts, operator === "y");
      if (operator !== "y") {
        for (let i = ranges.length - 1; i >= 0; i--) {
          let [start, end] = ranges[i];
          const value = kind === "line" && operator === "c" && text.slice(start, end).endsWith("\n") ? "\n" : "";
          if (kind === "line" && operator !== "c" && end === text.length && start && !text.endsWith("\n")) start--;
          this.replace(start, end, value, start);
        }
        if (operator === "c") this.mode = "Insert";
      }
    }
    this.position = Math.min(kind === "block" ? first : this.position, this.text.length);
    if (kind === "block" && operator === "c") {
      let removed = ranges[0][1] - ranges[0][0];
      const targets = ranges.slice(1).map(([start, end]) => {
        const target = start - removed;
        removed += end - start;
        return target;
      });
      this.blockInsert = [this.text, this.position, targets];
    }
  }

  pasteRegister(command) {
    const value = this.registers.get(command.register.toLowerCase());
    if (!value) { this.message = "Register is empty"; return; }
    const [kind, pieces] = value, count = command.count || 1;
    const [, selected] = this.selection();
    let proposed = [...this.text].length - selected.reduce((removed, [start, end]) =>
      removed + [...this.text.slice(start, end)].length, 0) + pieces.reduce((length, piece) => length + [...piece].length, 0) * count;
    const limit = Math.max(40000, [...this.text].length);
    if (proposed > limit + Number(kind === "line")) { this.message = "Note is too long"; return; }
    let before = command.key === "P";
    if (this.anchor !== null) {
      const [selectedKind, ranges] = this.selection();
      this.operate("d", "_", selectedKind, ranges);
      this.anchor = null;
      this.mode = "Normal";
      before = true;
    }
    const { text, position: pos } = this;
    if (kind === "line") {
      const fragment = pieces[0].repeat(count), end = viEnd(text, pos);
      const at = before ? viStart(text, pos) : Math.min(text.length, end + 1);
      if (!before && end === text.length && text) this.replace(at, at, "\n" + fragment.slice(0, -1), at + 1);
      else this.replace(at, at, text ? fragment : fragment.slice(0, -1), at);
    } else if (kind === "block") {
      let column = viColumn(text, pos);
      if (!before && pos < viEnd(text, pos)) column++;
      const row = viBound(viLines(text), pos, true) - 1;
      const result = text.split("\n");
      proposed += Math.max(0, row + pieces.length - result.length);
      while (result.length < row + pieces.length) result.push("");
      for (const [index, piece] of pieces.entries()) {
        const line = result[row + index];
        const point = viAtColumn(line, 0, line.length, column);
        const missing = Math.max(0, column - Array.from(VI_SEGMENTS.segment(line)).length);
        proposed += missing;
        if (proposed > limit) {
          this.restore(this.edit[0]);
          this.message = "Note is too long";
          return;
        }
        result[row + index] = line.slice(0, point) + " ".repeat(missing) + piece.repeat(count) + line.slice(point);
      }
      this.replace(0, text.length, result.join("\n"), pos);
    } else {
      const at = before ? pos : Math.min(viEnd(text, pos), viNext(text, pos));
      const fragment = pieces[0].repeat(count);
      this.replace(at, at, fragment, at + viPrevious(fragment, fragment.length));
    }
  }

  findSearch(key, count = 1) {
    if (!this.search) return;
    const [pattern, original] = this.search;
    const direction = key === "N" ? -original : original;
    const hits = Array.from(this.text.matchAll(pattern), match => match.index);
    if (!hits.length) { this.message = "Pattern not found"; return; }
    const index = direction > 0 ? viBound(hits, this.position, true) + count - 1
      : viBound(hits, this.position, false) - count;
    this.position = hits[(index % hits.length + hits.length) % hits.length];
  }

  ex(prompt) {
    const save = { ":w": "save", ":wq": "save-close", ":q": "close", ":q!": "discard-close", ":review": "review" };
    if (Object.hasOwn(save, prompt)) return save[prompt];
    if (prompt === ":help") { this.help = true; return; }
    if (prompt[0] === "/" || prompt[0] === "?") {
      if (prompt.length === 1 && !this.search) return;
      try {
        const pattern = prompt.length > 1 ? new RegExp(prompt.slice(1), "gmu") : this.search[0];
        this.search = [pattern, prompt[0] === "/" ? 1 : -1];
        this.findSearch("n");
      } catch { this.message = "Invalid search pattern"; }
      return;
    }
    const match = /^:(%?)s([^\w\s\\])((?:\\.|(?!\2).)*)\2((?:\\.|(?!\2).)*)\2?([gi]*)$/u.exec(prompt);
    if (!match) { this.message = prompt === ":" ? "" : "Unknown command · :help for keys"; return; }
    const [, whole, separator, expression, replacement, flags] = match;
    const [start, end] = whole ? [0, this.text.length] : [viStart(this.text, this.position), viEnd(this.text, this.position)];
    let matches = 0;
    const unescape = value => value.split("\\" + separator).join(separator);
    try {
      const pattern = new RegExp(unescape(expression), (flags.includes("g") ? "g" : "") + (flags.includes("i") ? "i" : "") + "u");
      const parts = this.text.slice(start, end).split("\n");
      const output = parts.map(line => line.replace(pattern, (...found) => {
        matches++;
        return unescape(replacement).replace(/\\&|&|\\([0-9])/g, (token, group) =>
          token === "\\&" ? "&" : token === "&" ? found[0] : typeof found[Number(group)] === "string" ? found[Number(group)] : "");
      }));
      if (matches) {
        this.begin([...prompt, "Enter"]);
        this.replace(start, end, output.join("\n"), start);
        this.commit();
      }
      this.message = matches ? matches + " substitutions" : "Pattern not found";
    } catch { this.message = "Invalid substitution"; }
  }

  play(keys, count) {
    if (!keys?.length || this.playing >= 12) return;
    if (!this.playing) this.stepsLeft = VI_LIMIT;
    this.playing++;
    try {
      for (let i = 0; i < count; i++) {
        for (const key of keys) {
          if (!this.stepsLeft--) { this.stepsLeft = 0; this.message = "Playback limit reached"; return; }
          this.input(key);
        }
      }
    } finally { this.playing--; }
  }

  run(command, keys) {
    const { key, operator, register, argument } = command;
    const count = command.count || 1;
    const { text, position: pos } = this;
    if (operator) {
      let kind = "char", ranges;
      if (key.startsWith("object-")) {
        const found = viObject(text, pos, key === "object-i", argument, count);
        ranges = found ? [found] : [];
      } else {
        const move = this.motion(command);
        if (!move) return;
        const [target, inclusive, linewise] = move;
        if (linewise || operator === ">" || operator === "<") {
          kind = "line";
          ranges = [this.lineRange(pos, target)];
        } else {
          const start = Math.min(pos, target), end = Math.max(pos, target);
          ranges = [[start, inclusive ? viNext(text, end) : end]];
        }
      }
      this.begin(keys);
      this.operate(operator, register, kind, ranges, key === "line" ? count : 1);
    } else if (["v", "V", "\x16"].includes(key)) {
      const mode = { v: "Visual", V: "Visual line", "\x16": "Visual block" }[key];
      if (this.mode === mode) {
        this.mode = "Normal";
        this.anchor = null;
      } else {
        if (this.anchor === null) {
          this.anchor = pos;
          this.visualKeys = [];
        }
        this.mode = mode;
        this.visualKeys.push(...keys);
      }
    } else if (this.anchor !== null && "dcy<>xs".includes(key)) {
      const [kind, ranges] = this.selection();
      this.begin([...this.visualKeys, ...keys]);
      this.mode = "Normal";
      this.anchor = null;
      this.operate({ x: "d", s: "c" }[key] ?? key, register, kind, ranges, count);
    } else if (this.anchor !== null && key === "o") {
      [this.anchor, this.position] = [this.position, this.anchor];
      this.visualKeys.push(...keys);
    } else if (this.anchor !== null && key.startsWith("object-")) {
      const found = viObject(text, pos, key === "object-i", argument, count);
      if (found) {
        this.mode = "Visual";
        this.anchor = found[0];
        this.position = Math.max(this.anchor, viPrevious(text, found[1]));
        this.visualKeys.push(...keys);
      }
    } else if (this.mode === "Visual block" && (key === "I" || key === "A")) {
      const [, ranges] = this.selection();
      this.begin([...this.visualKeys, ...keys]);
      const [point, ...targets] = ranges.map(([start, end]) => key === "A" ? end : start);
      this.position = point;
      this.blockInsert = [text, point, targets];
      this.mode = "Insert";
      this.anchor = null;
    } else if (this.anchor !== null && ["r", "u", "U", "~"].includes(key)) {
      const [, ranges] = this.selection();
      this.begin([...this.visualKeys, ...keys]);
      this.mode = "Normal";
      this.anchor = null;
      for (const [start, end] of [...ranges].reverse()) {
        let value = text.slice(start, end);
        if (key === "r" && argument && !/[\x00-\x1f]/.test(argument))
          value = Array.from(VI_SEGMENTS.segment(value), part => part.segment === "\n" ? "\n" : argument).join("");
        else if (key === "u") value = value.toLowerCase();
        else if (key === "U") value = value.toUpperCase();
        else if (key === "~") value = viSwapCase(value);
        this.replace(start, end, value, start);
      }
    } else if (VI_MOTIONS.has(key)) {
      const move = this.motion(command);
      if (move) {
        this.position = move[0];
        if (this.anchor !== null) this.visualKeys.push(...keys);
      }
    } else if (key === "n" || key === "N") this.findSearch(key, count);
    else if (key === "m" && /^[a-z]$/.test(argument)) this.marks.set(argument, pos);
    else if (this.anchor === null && (key === "u" || key === "\x12")) {
      const [stack, other] = key === "u" ? [this.history, this.redo] : [this.redo, this.history];
      for (let n = 0; n < count && stack.length; n++) { other.push(this.snapshot()); this.restore(stack.pop()); }
    } else if (key === "." && this.anchor === null) this.play(this.lastEdit, count);
    else if (key === "q" && !this.playing && /^[a-z]$/.test(argument)) {
      this.recording = argument;
      this.recorded = [];
    } else if (key === "@") {
      const name = argument === "@" ? this.lastMacro : argument;
      this.lastMacro = name;
      this.play(this.macros.get(name), count);
    } else if (key === "p" || key === "P") {
      this.begin(this.anchor !== null ? [...this.visualKeys, ...keys] : keys);
      this.pasteRegister(command);
    } else if (["a", "i", "A", "I", "o", "O", "R"].includes(key) && this.anchor === null) {
      this.begin(keys);
      if (key === "A") this.position = viEnd(text, pos);
      else if (key === "I") this.position = viNonblank(text, pos);
      else if (key === "a") this.position = Math.min(viNext(text, pos), viEnd(text, pos));
      else if (key === "o" || key === "O") {
        const at = key === "o" ? viEnd(text, pos) : viStart(text, pos);
        this.replace(at, at, "\n", at + Number(key === "o"));
      }
      this.mode = key === "R" ? "Replace" : "Insert";
      if (count > 1 && ["i", "I", "a", "A", "R"].includes(key)) this.insertRepeat = [keys.length, count];
    } else if (["x", "X", "D", "C", "s", "S", "r", "J", "~"].includes(key) && this.anchor === null) {
      this.begin(keys);
      let start = pos, end = viEnd(text, pos);
      if (key === "J") {
        let last = end;
        for (let n = 1; n < Math.max(2, count) && last !== text.length; n++) last = viEnd(text, last + 1);
        let joined = text.slice(start, end);
        for (const piece of text.slice(end + 1, last).split("\n")) {
          joined = joined.trimEnd() + (joined.trim() && piece.trim() ? " " : "") + piece.trim();
        }
        if (last > end) this.replace(start, last, joined, end);
      } else if (key === "S") this.operate("c", register, "line", [this.lineRange(pos, pos)]);
      else {
        if ("xsr~".includes(key)) end = viAtColumn(text, pos, end, count);
        else if (key === "X") {
          end = pos;
          const edge = viStart(text, pos);
          for (let n = 0; n < count && start > edge; n++) start = Math.max(edge, viPrevious(text, start));
        }
        if (key === "r" && argument && !/[\x00-\x1f]/.test(argument))
          this.replace(start, end, argument.repeat(Array.from(VI_SEGMENTS.segment(text.slice(start, end))).length), start);
        else if (key === "~") this.replace(start, end, viSwapCase(text.slice(start, end)));
        else if (key !== "r") this.operate(key === "s" || key === "C" ? "c" : "d", register, "char", [[start, end]]);
      }
    }
    if (!this.inserting) this.commit();
  }

  insert(key, record = true) {
    this.begin(["i"]);
    if (record) this.edit[1].push(key);
    const { text, position: pos } = this;
    if (typeof key === "object") {
      const start = Math.max(0, Math.min(text.length, pos + key.offset));
      const end = Math.min(text.length, start + key.remove);
      this.replace(start, end, key.text, start + key.caret);
    } else if (key === "ArrowLeft" || key === "ArrowRight") {
      this.position = key === "ArrowLeft" ? viPrevious(text, pos) : viNext(text, pos);
    } else if (Object.hasOwn(VI_ARROWS, key)) {
      const move = this.motion({ key: VI_ARROWS[key] });
      if (move) this.position = move[0];
    } else if (key === "Backspace") this.replace(viPrevious(text, pos), pos, "");
    else if (key === "Delete") this.replace(pos, viNext(text, pos), "");
    else if ([...key].length === 1 || key === "Enter" || key === "Tab") {
      const value = key === "Enter" ? "\n" : key === "Tab" ? "\t" : key;
      const end = this.mode === "Replace" && value !== "\n" ? Math.min(viEnd(text, pos), viNext(text, pos)) : pos;
      this.replace(pos, end, value);
    }
  }

  nativeEdit(value, caret, from) {
    const prev = this.text;
    if (prev === value) { this.position = caret; return; }
    let start = 0;
    const prefixLimit = Math.min(from, caret, prev.length, value.length);
    while (start < prefixLimit && prev[start] === value[start]) start++;
    if (/[\uDC00-\uDFFF]/.test(prev[start] ?? "") || /[\uDC00-\uDFFF]/.test(value[start] ?? "")) start--;
    let end = prev.length, tail = value.length;
    while (end > start && tail > Math.max(start, caret) && prev[end - 1] === value[tail - 1]) { end--; tail--; }
    if (/[\uDC00-\uDFFF]/.test(prev[end] ?? "") || /[\uDC00-\uDFFF]/.test(value[tail] ?? "")) { end++; tail++; }
    const text = value.slice(start, tail);
    if (this.mode === "Replace" && start === end && text && !text.includes("\n")) {
      end = viAtColumn(prev, start, viEnd(prev, start), Array.from(VI_SEGMENTS.segment(text)).length);
    }
    this.position = from;
    const patch = { offset: start - from, remove: end - start, text, caret: caret - start };
    if (this.recording && this.recorded.length < VI_LIMIT) this.recorded.push(patch);
    this.insert(patch);
    if (!this.inserting) {
      this.edit[1].push("Escape");
      this.commit();
    }
  }

  input(key) {
    if (this.help) {
      if (key === "Escape" || key === "Enter") this.help = false;
      return;
    }
    this.message = "";
    if (!this.playing) {
      if (this.recording && key === "q" && this.mode === "Normal" && !this.pending.length && !this.command) {
        this.macros.set(this.recording, this.recorded);
        this.recording = null;
        return;
      }
      if (this.recording && this.recorded.length < VI_LIMIT) this.recorded.push(key);
    }
    if (key === "Escape") {
      if (this.edit) {
        this.finishInsert();
        this.edit[1].push(key);
        if (this.position > viStart(this.text, this.position)) this.position = viPrevious(this.text, this.position);
        this.commit();
      }
      this.mode = "Normal";
      this.anchor = null;
      this.command = "";
      this.pending.length = 0;
    } else if (this.command) {
      if (key === "Enter") {
        const prompt = this.command;
        this.command = "";
        return this.ex(prompt);
      }
      if (key === "Backspace") this.command = this.command.slice(0, -1);
      else if ([...key].length === 1 && this.command.length < 500) this.command += key;
    } else if (this.inserting || typeof key === "object") this.insert(key);
    else if ([":", "/", "?"].includes(key) && !this.pending.length) this.command = key;
    else if (Object.hasOwn(VI_ARROWS, key)) this.run({ key: VI_ARROWS[key] }, [key]);
    else if ([...key].length === 1) {
      this.pending.push(key);
      const parsed = viParse(this.pending, this.anchor !== null);
      if (parsed !== null) {
        const keys = this.pending;
        this.pending = [];
        if (parsed.key) this.run(parsed, keys);
      }
    }
  }
}

if (typeof module !== "undefined") module.exports = ViEditor;
