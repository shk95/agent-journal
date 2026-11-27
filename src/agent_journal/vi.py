"""Vim-style editing of a single journal note."""

import curses
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from functools import lru_cache

from wcwidth import grapheme_boundary_before, iter_graphemes

from .text_layout import cell_width, fit, lines, wrapped_spans

HELP = (
    "Movement  h j k l · w b e / W B E · 0 ^ $ · gg G · gj gk · f/F/t/T ; , · % { }",
    "Counts  3w · 2dd · d2w · 5G",
    "Operators  d c y > < + motion · dd cc yy >> << · iw aw iW aW · i/a + quotes or brackets",
    "Selection  v character · V line · Ctrl+V block · o swap ends · Esc normal",
    "Editing  i I a A o O · x X s S · D C · r R · J · ~ · p P · \"ayw / \"ap",
    "Search  / and ? · n N · :s/pattern/replacement/g · :%s/pattern/replacement/g",
    "History  u undo · Ctrl+R redo · . repeat · ma mark · 'a line · `a exact position",
    "Macros  qa record · q stop · @a play · @@ repeat macro",
    "Journal  :w save · :wq save and close · :q close · :q! discard local draft · :review review conflict · Ctrl+P preview",
)

MAX_COUNT = 10_000
OPERATORS = "dcy><"
MOTIONS = frozenset("hjkl0^$wbeWBEG;,%{}") | {"gg", "gj", "gk"}
ARGUMENTS = frozenset("fFtT'`mrq@")
PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">"}
ARROWS = {
    curses.KEY_LEFT: "h", curses.KEY_RIGHT: "l", curses.KEY_UP: "gk", curses.KEY_DOWN: "gj",
    curses.KEY_HOME: "0", curses.KEY_END: "$",
}
BACKSPACE = (curses.KEY_BACKSPACE, "\x7f", "\b")


@dataclass(frozen=True)
class Paste:
    text: str


@dataclass(frozen=True)
class Command:
    key: str
    argument: str = ""
    count: int = 0
    operator: str = ""
    register: str = '"'


def line_start(text, pos):
    return text.rfind("\n", 0, pos) + 1


def line_end(text, pos):
    end = text.find("\n", pos)
    return len(text) if end == -1 else end


def first_nonblank(text, pos):
    start = line_start(text, pos)
    line = text[start:line_end(text, pos)]
    return start + len(line) - len(line.lstrip())


def next_char(text, pos):
    return pos + len(next(iter_graphemes(text, start=pos), ""))


@lru_cache(maxsize=4)
def line_offsets(text):
    return [0, *(index + 1 for index, char in enumerate(text) if char == "\n")]


@lru_cache(maxsize=4)
def word_edges(text, big):
    starts, ends = [], []
    pos, previous = 0, 0
    for cluster in iter_graphemes(text):
        first = cluster[0]
        kind = 0 if first.isspace() else 1 if big or first.isalnum() or first == "_" else 2
        if kind:
            if kind != previous:
                starts.append(pos)
                ends.append(pos)
            else:
                ends[-1] = pos
        previous = kind
        pos += len(cluster)
    return starts, ends


def word_motion(text, pos, key, count, *, change=False, operating=False):
    starts, ends = word_edges(text, key.isupper())
    if key.lower() == "b":
        index = bisect_left(starts, pos) - count
        return starts[index] if index >= 0 else 0
    edges = ends if key.lower() == "e" or change else starts
    index = (bisect_left if change else bisect_right)(edges, pos) + count - 1
    if index < len(edges):
        return edges[index]
    return len(text) if operating and edges is starts else max(pos, grapheme_boundary_before(text, len(text)))


def count_at(keys, index):
    count = 0
    if index < len(keys) and keys[index] in "123456789":
        while index < len(keys) and keys[index] in "0123456789" and len(keys[index]) == 1:
            count = min(MAX_COUNT, count * 10 + int(keys[index]))
            index += 1
    return index, count


def parse(keys, visual):
    index, count = count_at(keys, 0)
    if index == len(keys):
        return None
    register = '"'
    if keys[index] == '"':
        if index + 1 == len(keys):
            return None
        register = keys[index + 1]
        if len(register) != 1 or register not in '"_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ':
            return Command("")
        index, following = count_at(keys, index + 2)
        count = min(MAX_COUNT, (count or 1) * following) if following else count
    if index == len(keys):
        return None
    key = keys[index]
    operator = ""
    if key in OPERATORS and not visual:
        operator = key
        index, following = count_at(keys, index + 1)
        count = min(MAX_COUNT, (count or 1) * following) if following else count
        if index == len(keys):
            return None
        key = keys[index]
        if key == operator:
            return Command("line", count=count, operator=operator, register=register)
    object_key = (operator or visual) and key in ("a", "i")
    if key == "g" or key in ARGUMENTS or object_key:
        if index + 1 == len(keys):
            return None
        argument = keys[index + 1]
        if key == "g":
            key, argument = key + argument, ""
        if object_key:
            key = "object-" + key
    else:
        argument = ""
    return Command(key, argument, count, operator, register)


def delimiter_ranges(text, left, right):
    stack, ranges = [], []
    if left == right:
        escaped = False
        for index, char in enumerate(text):
            if char == left and not escaped:
                if stack:
                    ranges.append((stack.pop(), index))
                else:
                    stack.append(index)
            escaped = char == "\\" and not escaped
        return ranges
    for index, char in enumerate(text):
        if char == left:
            stack.append(index)
        elif char == right and stack:
            ranges.append((stack.pop(), index))
    return ranges


def text_object(text, pos, inner, key, count):
    if key in ("w", "W"):
        starts, ends = word_edges(text, key == "W")
        index = bisect_right(starts, pos) - 1
        if index < 0 or pos >= next_char(text, ends[index]):
            index += 1
        if index == len(starts):
            return None
        start = starts[index]
        end = next_char(text, ends[min(index + count - 1, len(ends) - 1)])
        if not inner:
            trailing = end
            while trailing < len(text) and text[trailing] in " \t":
                trailing += 1
            if trailing == end:
                while start and text[start - 1] in " \t":
                    start -= 1
            end = trailing
        return start, end
    key = {"b": "(", "B": "{"}.get(key, key)
    quotes = key in ('"', "'", "`")
    left = next((left for left, right in PAIRS.items() if key in (left, right)), key)
    if not quotes and left not in PAIRS:
        return None
    offset = line_start(text, pos) if quotes else 0
    source = text[offset:line_end(text, pos)] if quotes else text
    pairs = [(a + offset, b + offset) for a, b in delimiter_ranges(source, left, PAIRS.get(left, left))]
    around = sorted(((a, b) for a, b in pairs if a <= pos <= b), reverse=True)
    if not around and quotes:
        around = [(a, b) for a, b in pairs if a >= pos]
    if count > len(around):
        return None
    start, end = around[count - 1]
    return start + int(inner), end + int(not inner)


def logical_column(text, pos):
    start = line_start(text, pos)
    return sum(1 for _ in iter_graphemes(text, start=start, end=pos))


def at_column(text, start, end, column):
    position = start
    for index, cluster in enumerate(iter_graphemes(text, start=start, end=end)):
        if index >= column:
            break
        position += len(cluster)
    return position


@lru_cache(maxsize=4)
def editor_rows(text, width):
    rows = []
    offset = 0
    for line in lines(text):
        rows.extend(
            (offset + start, fit(line[start:end], width), start != 0)
            for start, end in wrapped_spans(line, width)
        )
        offset += len(line) + 1
    return rows


def editor_row(rows, position):
    return bisect_right(rows, position, key=lambda row: row[0]) - 1


def screen_move(text, position, width, delta):
    rows = editor_rows(text, width)
    row = editor_row(rows, position)
    target = max(0, min(row + delta, len(rows) - 1))
    if target == row:
        return position
    column = cell_width(fit(text[rows[row][0]:position], width))
    start = rows[target][0]
    end = rows[target + 1][0] - 1 if target + 1 < len(rows) else len(text)
    used = 0
    for cluster in iter_graphemes(text, start=start):
        cells = 4 - used % 4 if cluster == "\t" else cell_width(cluster)
        if cluster == "\n" or used + cells > column or start + len(cluster) > end:
            break
        start += len(cluster)
        used += cells
    return start


class Editor:
    def __init__(self, note, mode="Normal", position=0):
        self.note = note
        self.mode = mode
        self.position = position
        self.top = 0
        self.width = 0
        self.preview = False
        self.command = ""
        self.pending = []
        self.message = ""
        self.help_offset = None
        self.anchor = None
        self.visual_keys = []
        self.history = []
        self.redo = []
        self.edit = None
        self.block_insert = None
        self.insert_repeat = None
        self.last_edit = []
        self.marks = {}
        self.registers = {}
        self.find = None
        self.search = None
        self.recording = None
        self.recorded = []
        self.macros = {}
        self.last_macro = None
        self.playing = 0
        self.steps_left = 0

    @property
    def text(self):
        return self.note["body"]

    def snapshot(self):
        return self.text, self.position, self.marks.copy()

    def restore(self, snapshot):
        self.note["body"], self.position, marks = snapshot
        self.marks = marks.copy()

    def begin(self, keys):
        if self.edit is None:
            self.edit = self.snapshot(), list(keys)

    def commit(self):
        if self.edit is not None:
            before, keys = self.edit
            if before[0] != self.text:
                self.history.append(before)
                del self.history[:-50]
                self.redo.clear()
                self.last_edit = keys
            self.edit = None

    def replace(self, start, end, value, position=None):
        result = self.text[:start] + value + self.text[end:]
        if len(result) > max(40_000, len(self.text)):
            self.message = "Note is too long"
            return
        self.note["body"] = result
        self.position = start + len(value) if position is None else position
        delta = len(value) - (end - start)
        for mark, offset in self.marks.items():
            if offset >= end:
                self.marks[mark] = offset + delta
            elif offset >= start:
                self.marks[mark] = start

    def finish_insert(self):
        if self.insert_repeat:
            offset, count = self.insert_repeat
            keys = self.edit[1][offset:]
            for _ in range(min(count - 1, MAX_COUNT // max(len(keys), 1))):
                for key in keys:
                    self.insert(key, record=False)
            self.insert_repeat = None
        if self.block_insert:
            original, start, targets = self.block_insert
            prefix, suffix = original[:start], original[start:]
            added = len(self.text) - len(original)
            value = self.text[start:start + added]
            if added > 0 and "\n" not in value and self.text.startswith(prefix) and self.text.endswith(suffix):
                if len(self.text) + added * len(targets) <= 40_000:
                    for target in reversed(targets):
                        self.replace(target + added, target + added, value, self.position)
                else:
                    self.message = "Note is too long"
            self.block_insert = None

    def seek(self, key, argument, count, repeat=False):
        text, pos = self.text, self.position
        forward = key in ("f", "t")
        start, end = line_start(text, pos), line_end(text, pos)
        matches = []
        at = start
        for grapheme in iter_graphemes(text, start=start, end=end):
            following = at + len(grapheme)
            if grapheme == argument and ((at > pos) if forward else (at < pos)):
                target = at
                if key == "t":
                    target = grapheme_boundary_before(text, at)
                elif key == "T":
                    target = following
                if not repeat or target != pos:
                    matches.append(target)
            at = following
        if len(matches) < count or not argument:
            return None
        return matches[count - 1] if forward else matches[-count]

    def motion(self, command):
        key, argument, count = command.key, command.argument, command.count or 1
        text, pos = self.text, self.position
        start, end = line_start(text, pos), line_end(text, pos)
        inclusive = linewise = False
        if key in ("h", "l"):
            for _ in range(count):
                following = max(start, grapheme_boundary_before(text, pos)) if key == "h" else min(end, next_char(text, pos))
                if following == pos:
                    break
                pos = following
        elif key in ("0", "^"):
            pos = start if key == "0" else first_nonblank(text, pos)
        elif key == "$":
            offsets = line_offsets(text)
            row = bisect_right(offsets, pos) - 1
            target = offsets[min(row + count - 1, len(offsets) - 1)]
            pos = line_end(text, target)
            if pos > target and not command.operator and self.mode not in ("Insert", "Replace"):
                pos = grapheme_boundary_before(text, pos)
        elif key in "wbeWBE":
            change = command.operator == "c" and key.lower() == "w" and pos < len(text) and not text[pos].isspace()
            pos = word_motion(text, pos, key, count, change=change, operating=bool(command.operator))
            if command.operator and key.lower() == "w" and count == 1 and self.position < end < pos:
                pos = end
            else:
                inclusive = key.lower() == "e" or change
        elif key in ("gg", "G", "j", "k", "gj", "gk", "line"):
            offsets = line_offsets(text)
            row = bisect_right(offsets, pos) - 1
            if key in ("gj", "gk") and self.width:
                pos = screen_move(text, pos, self.width, count if key == "gj" else -count)
            elif key in ("gg", "G"):
                target = (0 if key == "gg" else len(offsets) - 1) if not command.count else count - 1
                pos = first_nonblank(text, offsets[min(target, len(offsets) - 1)])
                linewise = True
            else:
                delta = count - 1 if key == "line" else count if key in ("j", "gj") else -count
                target = offsets[max(0, min(row + delta, len(offsets) - 1))]
                pos = at_column(text, target, line_end(text, target), logical_column(text, pos))
                linewise = key in ("j", "k", "line")
        elif key in ("f", "F", "t", "T", ";", ","):
            if key in (";", ","):
                if not self.find:
                    return None
                method, argument = self.find
                if key == ",":
                    method = method.swapcase()
            else:
                method = key
                self.find = key, argument
            pos = self.seek(method, argument, count, repeat=key in (";", ","))
            inclusive = True
        elif key in ("'", "`"):
            pos = self.marks.get(argument)
            linewise = key == "'"
            if pos is not None and linewise:
                pos = first_nonblank(text, pos)
        elif key == "%":
            char = text[pos:pos + 1]
            left = next((left for left, right in PAIRS.items() if char in (left, right)), "")
            if not left:
                return None
            pos = next((b if a == pos else a for a, b in delimiter_ranges(text, left, PAIRS[left])
                        if pos in (a, b)), None)
            inclusive = True
        elif key in ("{", "}"):
            edges = [match.start() + 1 for match in re.finditer(r"\n[ \t]*\n", text)]
            index = bisect_left(edges, pos) - count if key == "{" else bisect_right(edges, pos) + count - 1
            pos = edges[index] if 0 <= index < len(edges) else 0 if key == "{" else len(text)
        else:
            return None
        return (pos, inclusive, linewise) if pos is not None else None

    def line_range(self, first, last):
        start = line_start(self.text, min(first, last))
        end = line_end(self.text, max(first, last))
        return start, min(len(self.text), end + 1)

    def selection(self):
        if self.anchor is None:
            return "char", []
        text = self.text
        if self.mode == "Visual line":
            return "line", [self.line_range(self.anchor, self.position)]
        first, last = sorted((self.anchor, self.position))
        if self.mode == "Visual block":
            left, right = sorted((logical_column(text, self.anchor), logical_column(text, self.position)))
            starts = line_offsets(text)
            rows = starts[bisect_right(starts, first) - 1:bisect_right(starts, last)]
            ranges = [(at_column(text, start, line_end(text, start), left),
                       at_column(text, start, line_end(text, start), right + 1)) for start in rows]
            return "block", ranges
        return "char", [(first, next_char(text, last))]

    def store(self, name, kind, pieces, yank):
        if name == "_":
            return
        value = kind, pieces
        if name.isupper() and name.lower() in self.registers:
            old_kind, old = self.registers[name.lower()]
            if old_kind == kind == "block":
                combined = [a + b for a, b in zip(old, pieces)]
                combined.extend((old if len(old) > len(pieces) else pieces)[len(combined):])
                value = kind, combined
            else:
                value = kind, [old[0] + pieces[0]]
        self.registers['"'] = self.registers[name.lower()] = value
        if yank:
            self.registers["0"] = value

    def operate(self, operator, register, kind, ranges, count=1):
        if not ranges or not any(start != end for start, end in ranges) and kind != "line":
            if operator == "c" and kind != "block":
                self.mode = "Insert"
            return
        text = self.text
        first = ranges[0][0]
        if operator in "><":
            start, end = self.line_range(first, max(first, ranges[-1][1] - 1))
            changed = []
            for line in text[start:end].splitlines(keepends=True):
                if line.strip():
                    line = " " * (4 * count) + line if operator == ">" else re.sub(
                        rf"^(?: {{1,4}}|\t){{1,{count}}}", "", line
                    )
                changed.append(line)
            self.replace(start, end, "".join(changed), start)
        else:
            parts = [text[start:end] for start, end in ranges]
            if kind == "line" and not parts[0].endswith("\n"):
                parts[0] += "\n"
            self.store(register, kind, parts, operator == "y")
            if operator != "y":
                for start, end in reversed(ranges):
                    if kind == "line" and operator == "c":
                        value = "\n" if text[start:end].endswith("\n") else ""
                    else:
                        value = ""
                        if kind == "line" and end == len(text) and start and not text.endswith("\n"):
                            start -= 1
                    self.replace(start, end, value, start)
                if operator == "c":
                    self.mode = "Insert"
        self.position = min(first if kind == "block" else self.position, len(self.text))
        if kind == "block" and operator == "c":
            shifted, removed = [], ranges[0][1] - ranges[0][0]
            for start, end in ranges[1:]:
                shifted.append(start - removed)
                removed += end - start
            self.block_insert = self.text, self.position, shifted

    def paste_register(self, command):
        value = self.registers.get(command.register.lower())
        if not value:
            self.message = "Register is empty"
            return
        kind, pieces = value
        text, pos, count = self.text, self.position, command.count or 1
        _, selected = self.selection()
        proposed = len(text) - sum(end - start for start, end in selected) + sum(map(len, pieces)) * count
        if proposed > max(40_000, len(text)) + int(kind == "line"):
            self.message = "Note is too long"
            return
        if self.anchor is not None:
            selected_kind, ranges = self.selection()
            self.operate("d", "_", selected_kind, ranges)
            self.anchor = None
            self.mode = "Normal"
            text, pos = self.text, self.position
            before = True
        else:
            before = command.key == "P"
        if kind == "line":
            fragment = pieces[0] * count
            end = line_end(text, pos)
            at = line_start(text, pos) if before else min(len(text), end + 1)
            if not before and end == len(text) and text:
                self.replace(at, at, "\n" + fragment[:-1], at + 1)
            else:
                self.replace(at, at, fragment if text else fragment[:-1], at)
        elif kind == "block":
            column = logical_column(text, pos)
            if not before and pos < line_end(text, pos):
                column += 1
            row = bisect_right(line_offsets(text), pos) - 1
            result = text.split("\n")
            result.extend([""] * max(0, row + len(pieces) - len(result)))
            for index, piece in enumerate(pieces, row):
                line = result[index]
                point = at_column(line, 0, len(line), column)
                missing = max(0, column - sum(1 for _ in iter_graphemes(line)))
                proposed += missing + int(index >= len(line_offsets(text)))
                if proposed > max(40_000, len(text)):
                    self.restore(self.edit[0])
                    self.message = "Note is too long"
                    return
                result[index] = line[:point] + " " * missing + piece * count + line[point:]
            self.replace(0, len(text), "\n".join(result), pos)
        else:
            at = pos if before else min(line_end(text, pos), next_char(text, pos))
            fragment = pieces[0] * count
            self.replace(at, at, fragment, at + grapheme_boundary_before(fragment, len(fragment)))

    def find_search(self, key, count=1):
        if not self.search:
            return
        pattern, direction = self.search
        direction *= -1 if key == "N" else 1
        hits = [match.start() for match in pattern.finditer(self.text)]
        if not hits:
            self.message = "Pattern not found"
            return
        index = bisect_right(hits, self.position) + count - 1 if direction > 0 else bisect_left(hits, self.position) - count
        self.position = hits[index % len(hits)]

    def ex(self, prompt):
        save = {":w": "save", ":wq": "save-close", ":q": "close", ":q!": "discard-close", ":review": "review"}
        if prompt in save:
            return save[prompt]
        if prompt == ":help":
            self.help_offset = 0
            return None
        if prompt[0] in "/?":
            if len(prompt) == 1 and not self.search:
                return None
            try:
                pattern = re.compile(prompt[1:], re.MULTILINE) if len(prompt) > 1 else self.search[0]
            except re.error:
                self.message = "Invalid search pattern"
                return None
            self.search = pattern, 1 if prompt[0] == "/" else -1
            self.find_search("n")
            return None
        match = re.fullmatch(r":(%?)s([^\w\s\\])((?:\\.|(?!\2).)*)\2((?:\\.|(?!\2).)*)\2?([gi]*)", prompt)
        if not match:
            self.message = "" if prompt == ":" else "Unknown command · :help for keys"
            return None
        whole, separator, expression, replacement, flags = match.groups()
        start, end = (0, len(self.text)) if whole else (
            line_start(self.text, self.position), line_end(self.text, self.position))
        try:
            pattern = re.compile(expression.replace("\\" + separator, separator), re.IGNORECASE if "i" in flags else 0)
            replacement = replacement.replace("\\" + separator, separator)
            replacement = re.sub(r"\\&|&", lambda found: "&" if found[0] == "\\&" else r"\g<0>", replacement)
            output, matches = [], 0
            for line in self.text[start:end].split("\n"):
                result, found = pattern.subn(replacement, line, count=0 if "g" in flags else 1)
                output.append(result)
                matches += found
        except re.error:
            self.message = "Invalid substitution"
            return None
        if matches:
            self.begin(list(prompt) + ["\n"])
            self.replace(start, end, "\n".join(output), start)
            self.commit()
        self.message = f"{matches} substitutions" if matches else "Pattern not found"
        return None

    def play(self, keys, count):
        if not keys or self.playing >= 12:
            return
        if not self.playing:
            self.steps_left = MAX_COUNT
        self.playing += 1
        try:
            for _ in range(count):
                for key in keys:
                    if not self.steps_left:
                        self.message = "Playback limit reached"
                        return
                    self.steps_left -= 1
                    self.dispatch(key)
        finally:
            self.playing -= 1

    def run(self, command, keys):
        key, argument, count, operator, register = (
            command.key, command.argument, command.count or 1, command.operator, command.register)
        text, pos = self.text, self.position
        if operator:
            if key.startswith("object-"):
                found = text_object(text, pos, key == "object-i", argument, count)
                kind, ranges = "char", [found] if found is not None else []
            elif (move := self.motion(command)) is not None:
                target, inclusive, linewise = move
                if linewise or operator in "><":
                    kind, ranges = "line", [self.line_range(pos, target)]
                else:
                    start, end = sorted((pos, target))
                    kind, ranges = "char", [(start, next_char(text, end) if inclusive else end)]
            else:
                return
            self.begin(keys)
            self.operate(operator, register, kind, ranges, count if key == "line" else 1)
        elif key in ("v", "V", "\x16"):
            mode = {"v": "Visual", "V": "Visual line", "\x16": "Visual block"}[key]
            if self.mode == mode:
                self.mode, self.anchor = "Normal", None
            else:
                if self.anchor is None:
                    self.anchor = pos
                    self.visual_keys = []
                self.mode = mode
                self.visual_keys.extend(keys)
        elif self.anchor is not None and key in "dcy<>xs":
            kind, ranges = self.selection()
            self.begin(self.visual_keys + keys)
            self.mode, self.anchor = "Normal", None
            self.operate({"x": "d", "s": "c"}.get(key, key), register, kind, ranges, count)
        elif self.anchor is not None and key == "o":
            self.anchor, self.position = self.position, self.anchor
            self.visual_keys.extend(keys)
        elif self.anchor is not None and key.startswith("object-"):
            if (found := text_object(text, pos, key == "object-i", argument, count)) is not None:
                self.mode = "Visual"
                self.anchor, end = found
                self.position = max(self.anchor, grapheme_boundary_before(text, end))
                self.visual_keys.extend(keys)
        elif self.mode == "Visual block" and key in ("I", "A"):
            _, ranges = self.selection()
            self.begin(self.visual_keys + keys)
            points = [end if key == "A" else start for start, end in ranges]
            self.position, *targets = points
            self.block_insert = text, self.position, targets
            self.mode, self.anchor = "Insert", None
        elif self.anchor is not None and key in ("r", "u", "U", "~"):
            _, ranges = self.selection()
            self.begin(self.visual_keys + keys)
            self.mode, self.anchor = "Normal", None
            for start, end in reversed(ranges):
                value = text[start:end]
                if key == "r" and argument.isprintable():
                    value = "".join(char if char == "\n" else argument for char in iter_graphemes(value))
                else:
                    value = {"u": str.lower, "U": str.upper, "~": str.swapcase}.get(key, str)(value)
                self.replace(start, end, value, start)
        elif key in MOTIONS or key in ("f", "F", "t", "T", "'", "`"):
            if (move := self.motion(command)) is not None:
                self.position = move[0]
                if self.anchor is not None:
                    self.visual_keys.extend(keys)
        elif key in ("n", "N"):
            self.find_search(key, count)
        elif key == "m" and re.fullmatch("[a-z]", argument):
            self.marks[argument] = pos
        elif key in ("u", "\x12") and self.anchor is None:
            stack, other = (self.history, self.redo) if key == "u" else (self.redo, self.history)
            for _ in range(min(count, len(stack))):
                other.append(self.snapshot())
                self.restore(stack.pop())
        elif key == "." and self.anchor is None:
            self.play(self.last_edit, count)
        elif key == "q" and not self.playing and re.fullmatch("[a-z]", argument):
            self.recording, self.recorded = argument, []
        elif key == "@":
            name = self.last_macro if argument == "@" else argument
            self.last_macro = name
            self.play(self.macros.get(name, []), count)
        elif key in ("p", "P"):
            self.begin(self.visual_keys + keys if self.anchor is not None else keys)
            self.paste_register(command)
        elif key in ("a", "i", "A", "I", "o", "O", "R") and self.anchor is None:
            self.begin(keys)
            if key == "A":
                self.position = line_end(text, pos)
            elif key == "I":
                self.position = first_nonblank(text, pos)
            elif key == "a":
                self.position = min(next_char(text, pos), line_end(text, pos))
            elif key in ("o", "O"):
                at = line_end(text, pos) if key == "o" else line_start(text, pos)
                self.replace(at, at, "\n", at + int(key == "o"))
            self.mode = "Replace" if key == "R" else "Insert"
            if count > 1 and key in ("i", "I", "a", "A", "R"):
                self.insert_repeat = len(keys), count
        elif key in ("x", "X", "D", "C", "s", "S", "r", "J", "~") and self.anchor is None:
            self.begin(keys)
            start, end = pos, line_end(text, pos)
            if key == "J":
                last = end
                for _ in range(max(2, count) - 1):
                    if last == len(text):
                        break
                    last = line_end(text, last + 1)
                joined = text[start:end]
                for part in text[end + 1:last].split("\n"):
                    joined = joined.rstrip() + (" " if joined.strip() and part.strip() else "") + part.strip()
                if last > end:
                    self.replace(start, last, joined, end)
            elif key == "S":
                self.operate("c", register, "line", [self.line_range(pos, pos)])
            else:
                if key in ("x", "s", "r", "~"):
                    end = at_column(text, pos, end, count)
                elif key == "X":
                    end, edge = pos, line_start(text, pos)
                    for _ in range(count):
                        start = max(edge, grapheme_boundary_before(text, start))
                        if start == edge:
                            break
                if key == "r" and argument and argument.isprintable():
                    self.replace(start, end, argument * sum(1 for _ in iter_graphemes(text, start=start, end=end)), start)
                elif key == "~":
                    self.replace(start, end, text[start:end].swapcase())
                elif key != "r":
                    self.operate("c" if key in ("s", "C") else "d", register, "char", [(start, end)])
        if self.mode not in ("Insert", "Replace"):
            self.commit()

    def insert(self, key, *, record=True):
        self.begin(["i"])
        if record:
            self.edit[1].append(key)
        text, pos = self.text, self.position
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            self.position = grapheme_boundary_before(text, pos) if key == curses.KEY_LEFT else next_char(text, pos)
        elif key in ARROWS:
            if (move := self.motion(Command(ARROWS[key]))) is not None:
                self.position = move[0]
        elif key in BACKSPACE:
            self.replace(grapheme_boundary_before(text, pos), pos, "")
        elif key == curses.KEY_DC:
            self.replace(pos, next_char(text, pos), "")
        else:
            value = key.text.replace("\r\n", "\n").replace("\r", "\n") if isinstance(key, Paste) else "\n" if key == "\r" else key
            if isinstance(value, str) and (isinstance(key, Paste) or value.isprintable() or value in ("\n", "\t")):
                end = pos
                if self.mode == "Replace" and "\n" not in value:
                    end = at_column(text, pos, line_end(text, pos), sum(1 for _ in iter_graphemes(value)))
                self.replace(pos, end, value)

    def dispatch(self, key):
        if self.help_offset is not None:
            if key in ("\x1b", "\n", "\r"):
                self.help_offset = None
            elif key in ("j", curses.KEY_DOWN, "k", curses.KEY_UP):
                self.help_offset = max(0, self.help_offset + (1 if key in ("j", curses.KEY_DOWN) else -1))
            return None
        if key == "\x10":
            self.preview = not self.preview
            return None
        if key == "\x13":
            return "save"
        self.message = ""
        if not self.playing:
            if self.recording and key == "q" and self.mode == "Normal" and not self.pending and not self.command:
                self.macros[self.recording] = self.recorded
                self.recording = None
                return None
            if self.recording and len(self.recorded) < MAX_COUNT:
                self.recorded.append(key)
        if key == "\x1b":
            if self.edit is not None:
                self.finish_insert()
                self.edit[1].append(key)
                if self.position > line_start(self.text, self.position):
                    self.position = grapheme_boundary_before(self.text, self.position)
                self.commit()
            self.mode, self.anchor = "Normal", None
            self.command = ""
            self.pending.clear()
        elif self.command:
            if key in ("\n", "\r"):
                prompt, self.command = self.command, ""
                return self.ex(prompt)
            if key in BACKSPACE:
                self.command = self.command[:-1]
            elif isinstance(key, Paste):
                self.command += key.text.split("\n", 1)[0][:500]
            elif isinstance(key, str) and key.isprintable() and len(self.command) < 500:
                self.command += key
        elif isinstance(key, Paste) or self.mode in ("Insert", "Replace"):
            if self.mode not in ("Insert", "Replace"):
                self.begin(["i"])
                self.mode, self.anchor = "Insert", None
            self.insert(key)
        elif key in (":", "/", "?") and not self.pending:
            self.command = key
        elif key in ARROWS:
            self.run(Command(ARROWS[key]), [key])
        elif isinstance(key, str) and len(key) == 1:
            self.pending.append(key)
            parsed = parse(self.pending, self.anchor is not None)
            if parsed is not None:
                keys, self.pending = self.pending, []
                if parsed.key:
                    self.run(parsed, keys)
        return None

    def input(self, key):
        before = self.text
        result = self.dispatch(key)
        return result or ("change" if self.text != before else None)
