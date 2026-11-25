import curses
from collections import deque
from dataclasses import dataclass, field as dataclass_field

from wcwidth import grapheme_boundary_before, iter_graphemes

from .text_layout import cell_width, fit


@dataclass(frozen=True)
class Paste:
    text: str


class Keys:
    def __init__(self, window):
        self.window = window
        self.pending = deque()
        self.paste = None

    def read(self):
        if self.pending:
            return self.pending.popleft()
        try:
            key = self.window.get_wch()
        except curses.error:
            return None
        if key == "\x1b":
            ending = []
            self.window.timeout(12)
            try:
                for expected in "[201~" if self.paste is not None else "[200~":
                    observed = self.window.get_wch()
                    ending.append(observed)
                    if observed != expected:
                        break
                else:
                    if self.paste is None:
                        self.paste = []
                        return None
                    text = "".join(self.paste)
                    self.paste = None
                    return Paste(text)
            except curses.error:
                pass
            finally:
                self.window.timeout(40)
            if self.paste is None:
                self.pending.extend(ending)
            else:
                self.paste.extend(
                    value for value in [key, *ending] if isinstance(value, str)
                )
                return None
        elif self.paste is not None:
            if isinstance(key, str) and len(self.paste) < 80_000:
                self.paste.append(key)
            return None
        return key


def line_start(text, pos):
    return text.rfind("\n", 0, pos) + 1


def line_end(text, pos):
    end = text.find("\n", pos)
    return len(text) if end == -1 else end


@dataclass
class Editor:
    note: dict
    mode: str = "Normal"
    position: int = 0
    top: int = 0
    preview: bool = False
    command: str = ""
    pending: str = ""
    history: list = dataclass_field(default_factory=list)

    def update(self, start, end, value):
        source = self.note["body"]
        result = source[:start] + value + source[end:]
        if len(result) > max(40_000, len(source)):
            return False
        if result == source:
            return False
        self.history.append((source, self.position))
        del self.history[:-50]
        self.note["body"] = result
        self.position = start + len(value)
        return True

    def move(self, key, text):
        self.position = min(self.position, len(text))
        if key in ("h", curses.KEY_LEFT):
            if self.position:
                self.position = grapheme_boundary_before(text, self.position)
        elif key in ("l", curses.KEY_RIGHT):
            self.position += len(next(iter_graphemes(text, start=self.position), ""))
        elif key in ("0", curses.KEY_HOME):
            self.position = line_start(text, self.position)
        elif key in ("$", curses.KEY_END):
            self.position = line_end(text, self.position)
        else:
            start = line_start(text, self.position)
            column = sum(
                1 for _ in iter_graphemes(text, start=start, end=self.position)
            )
            if key in ("j", curses.KEY_DOWN):
                target = line_end(text, self.position) + 1
                if target > len(text):
                    return
            else:
                if start == 0:
                    return
                target = line_start(text, start - 1)
            self.position = target
            stop = line_end(text, target)
            for i, cluster in enumerate(iter_graphemes(text, start=target, end=stop)):
                if i >= column:
                    break
                self.position += len(cluster)

    def input(self, key):
        text, pos = self.note["body"], self.position
        if key == "\x10":
            self.preview = not self.preview
            return None
        if key == "\x13":
            return "save"
        if key == "\x1b":
            if self.mode == "Insert":
                self.mode = "Normal"
            elif self.command:
                self.command = ""
            else:
                return "close"
            return None
        if isinstance(key, Paste):
            self.mode = "Insert"
            pasted = key.text.replace("\r\n", "\n").replace("\r", "\n")
            return "change" if self.update(pos, pos, pasted) else None
        if self.mode == "Insert":
            if key in ("\x7f", "\b", curses.KEY_BACKSPACE) and pos:
                return (
                    "change"
                    if self.update(grapheme_boundary_before(text, pos), pos, "")
                    else None
                )
            if key == curses.KEY_DC:
                end = pos + len(next(iter_graphemes(text, start=pos), ""))
                return "change" if self.update(pos, end, "") else None
            if key in (
                curses.KEY_LEFT,
                curses.KEY_RIGHT,
                curses.KEY_UP,
                curses.KEY_DOWN,
                curses.KEY_HOME,
                curses.KEY_END,
            ):
                self.move(key, text)
            elif isinstance(key, str) and (key.isprintable() or key in ("\n", "\r", "\t")):
                return (
                    "change"
                    if self.update(pos, pos, "\n" if key == "\r" else key)
                    else None
                )
            return None
        if self.command:
            if key in ("\n", "\r"):
                command, self.command = self.command, ""
                return {":w": "save", ":wq": "save-close", ":q": "close"}.get(command)
            if isinstance(key, str) and key.isalpha() and len(self.command) < 4:
                self.command += key
            return None
        previous, self.pending = self.pending, ""
        if key in (
            "h",
            "j",
            "k",
            "l",
            "0",
            "$",
            curses.KEY_LEFT,
            curses.KEY_RIGHT,
            curses.KEY_UP,
            curses.KEY_DOWN,
            curses.KEY_HOME,
            curses.KEY_END,
        ):
            self.move(key, text)
        elif key == "g":
            if previous == "g":
                self.position = 0
            else:
                self.pending = key
        elif key == "G":
            self.position = len(text)
        elif key == ":":
            self.command = ":"
        elif key in ("i", "a"):
            if key == "a":
                self.move("l", text)
            self.mode = "Insert"
        elif key in ("o", "O"):
            self.mode = "Insert"
            end = line_end(text, pos) if key == "o" else line_start(text, pos)
            if self.update(end, end, "\n"):
                self.position = end + (key == "o")
                return "change"
        elif key == "x":
            end = pos + len(next(iter_graphemes(text, start=pos), ""))
            return "change" if self.update(pos, end, "") else None
        elif key == "d":
            if previous == "d":
                start = line_start(text, pos)
                end = min(len(text), line_end(text, pos) + 1)
                return "change" if self.update(start, end, "") else None
            self.pending = key
        elif key == "u" and self.history:
            self.note["body"], self.position = self.history.pop()
            return "change"
        return None


def editor_line(line, position, width):
    before = list(iter_graphemes(line, end=position))
    used = sum(
        cell_width(cluster) or (4 if cluster == "\t" else 0) for cluster in before
    )
    start = 0
    for cluster in before:
        if used < width:
            break
        used -= cell_width(cluster) or (4 if cluster == "\t" else 0)
        start += len(cluster)
    prefix = fit(line[start:position], width)
    cursor = sum(cell_width(cluster) for cluster in iter_graphemes(prefix))
    return fit(line[start:], width), min(cursor, width - 1)
