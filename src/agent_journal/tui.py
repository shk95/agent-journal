import curses
import json
import os
import shlex
import subprocess
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from .graph import layout

STATE = {
    "inProgress": "In progress",
    "completed": "Completed",
    "interrupted": "Interrupted",
    "failed": "Failed",
}
PAGE_SIZE = 60
OBSERVATION = {"unavailable": "Observer offline", "stopped": "Recording stopped"}
CLICK = curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED
WHEEL_UP = curses.BUTTON4_PRESSED
WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0)
AXES = ("Journey", "Turn notes", "Project notes")
AXIS_KEYS = tuple(map(ord, "123"))
MOVE = {ord("j"): 1, ord("k"): -1, curses.KEY_DOWN: 1, curses.KEY_UP: -1}


@dataclass
class Board:
    axes: frozenset[int] = frozenset(range(3))
    active: int = 0
    turn: tuple[str, str] | None = None
    note_ids: dict[int, str] = field(default_factory=dict)
    top: int = 0
    project_top: int | None = None
    before: str = ""

    def focus(self, axis):
        return replace(
            self, axes=frozenset({axis}), active=axis, note_ids=self.note_ids.copy()
        )

    def toggle(self, axis):
        if axis not in self.axes:
            self.axes |= {axis}
        elif len(self.axes) > 1:
            self.axes -= {axis}
            if self.active == axis:
                self.active = min(self.axes)


@dataclass
class Item:
    title: str
    content: str
    note: dict | None = None
    offset: int = 0
    width: int = 0
    lines: list[str] = field(default_factory=list)


def fit(text, n):
    out = []
    for c in text.replace("\n", " "):
        if unicodedata.category(c).startswith("C"):
            continue
        n -= 2 if unicodedata.east_asian_width(c) in ("F", "W") else 1
        if n < 0:
            break
        out.append(c)
    return "".join(out)


def write(win, y, x, text, width, style=0):
    if width <= 0:
        return
    try:
        win.addstr(y, x, fit(str(text), width), style)
    except curses.error:
        pass


def terminal_palette():
    if not curses.has_colors():
        return []
    try:
        curses.use_default_colors()
    except curses.error:
        return []
    if os.environ.get("NO_COLOR"):
        return []
    colors = (
        curses.COLOR_GREEN,
        curses.COLOR_MAGENTA,
        curses.COLOR_CYAN,
        curses.COLOR_BLUE,
        curses.COLOR_RED,
    )
    palette = []
    for pair, color in enumerate(colors[: curses.COLOR_PAIRS - 1], 1):
        if color < curses.COLORS:
            try:
                curses.init_pair(pair, color, -1)
            except curses.error:
                return []
            palette.append(curses.color_pair(pair))
    return palette


def lane_style(lane, palette):
    return palette[lane % len(palette)] if palette else 0


def draw_session_graph(win, y, graph, palette, lanes, step, overflow):
    if not lanes:
        return
    point, tracks, forks = graph["lane"], graph["tracks"], graph["forks"]
    left, right = min([point, *forks]), max([point, *forks])
    if left < right and left < lanes:
        span = (min(right, lanes - 1) - left) * step + 1
        write(win, y, 1 + left * step, "─" * span, span, lane_style(point, palette))
    for lane, _, above, below in tracks:
        if lane >= lanes:
            continue
        x = 1 + lane * step
        style = lane_style(lane, palette)
        if lane == point:
            write(win, y, x, "●", 1, style | curses.A_BOLD)
        elif lane in forks:
            corner = "┌" if lane == left else "┐" if lane == right else "┬"
            write(win, y, x, corner, 1, style)
        elif above and below:
            # A vertical rail breaks a crossing branch without creating a junction.
            write(win, y, x, "│", 1, style)
        if below:
            write(win, y + 1, x, "│", 1, style)
    if overflow and any(lane >= lanes for lane, _, _, _ in tracks):
        write(win, y, lanes + 1, "…", 1, curses.A_BOLD)


def draw_buttons(win, y, width, options, hits, active=()):
    x = 0
    for label, key in options:
        label = f"[{label}]"
        end = x + len(label)
        if end > width:
            break
        style = curses.A_REVERSE if key in active else curses.A_BOLD
        write(win, y, x, label, len(label), style)
        hits.update(((y, col), key) for col in range(x, end))
        x = end + 1


def mouse_event():
    try:
        _, x, y, _, state = curses.getmouse()
    except curses.error:
        return None
    if state & CLICK:
        return x, y, 0
    if state & WHEEL_UP:
        return x, y, curses.KEY_UP
    if state & WHEEL_DOWN:
        return x, y, curses.KEY_DOWN
    return None


def entry(win, title):
    h, w = win.getmaxyx()
    win.move(h - 2, 0)
    win.clrtoeol()
    write(win, h - 2, 0, title, w - 1, curses.A_BOLD)
    win.move(h - 1, 0)
    win.clrtoeol()
    win.refresh()
    curses.echo()
    _, mouse = curses.mousemask(0)
    win.timeout(-1)
    try:
        return win.getstr(h - 1, 0, 2048).decode().strip()
    finally:
        curses.noecho()
        if mouse:
            curses.mousemask(mouse)
        win.timeout(300)


def edit_body(win, body):
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    with tempfile.TemporaryDirectory(prefix="journal-note-") as folder:
        path = Path(folder) / "note.txt"
        path.write_text(body, encoding="utf-8")
        curses.def_prog_mode()
        curses.endwin()
        try:
            opened = subprocess.run([*editor, str(path)], check=False)
            return (
                path.read_text(encoding="utf-8").strip()
                if opened.returncode == 0
                else ""
            )
        finally:
            curses.reset_prog_mode()
            win.clear()
            win.refresh()


def turn_key(row):
    return row["thread"], row["turn"]


def note_in(items, identifier, *, latest=False):
    return next(
        (note for note in items if note["id"] == identifier),
        items[-1 if latest else 0] if items else None,
    )


def focus_axis(view, axis, turns, turn_notes):
    view.active = axis
    if axis == 1 and not turn_notes.get(view.turn):
        keys = [turn_key(turn) for turn in turns]
        anchor = keys.index(view.turn) if view.turn in keys else len(keys) - 1
        candidates = (i for i, key in enumerate(keys) if key in turn_notes)
        index = min(candidates, key=lambda i: abs(i - anchor), default=None)
        if index is not None:
            view.turn = keys[index]


def axis_path(view, width):
    axes = sorted(view.axes)
    names = " + ".join(f"{axis + 1} {AXES[axis]}" for axis in axes)
    if len(names) + 7 > width:
        names = " + ".join(str(axis + 1) for axis in axes)
    return "Axes · " + names


def draw_board(
    win, view, focused, turns, graphs, turn_notes, project_notes, palette, message
):
    h, w = win.getmaxyx()
    hits, turn_rows, note_rows = {}, {}, {}
    if focused:
        draw_buttons(win, 1, w - 1, [("Back", 27)], hits)
        write(win, 1, 7, axis_path(view, w - 8), w - 8, curses.A_BOLD)
    buttons = 2 if focused else 1
    start = buttons + 2
    labels = (
        ("1 Journey", "2 Turn notes", "3 Project notes")
        if w >= 47
        else ("1 Journey", "2 Turn", "3 Project")
        if w >= 34
        else ("1", "2", "3")
    )
    draw_buttons(
        win,
        buttons,
        w - 1,
        zip(labels, AXIS_KEYS),
        hits,
        active=[AXIS_KEYS[axis] for axis in view.axes] if focused else (),
    )
    weights = [2 * (0 in view.axes), int(1 in view.axes), int(2 in view.axes)]
    total = sum(weights)
    split = w * weights[0] // total
    second = w * sum(weights[:2]) // total
    for axis, (x, end) in enumerate(zip((0, split, second), (split, second, w))):
        if axis in view.axes:
            style = curses.A_REVERSE if view.active == axis else curses.A_BOLD
            write(win, start - 1, x, AXES[axis], end - x - 1, style)

    height = max(1, (h - start - 2) // 2)
    keys = [turn_key(turn) for turn in turns]
    idx = keys.index(view.turn) if view.turn in keys else max(0, len(keys) - 1)
    view.turn = keys[idx] if keys else None
    view.top = max(0, idx - height + 1, min(view.top, idx, len(keys) - height))
    if 2 in view.axes:
        picked = note_in(project_notes, view.note_ids.get(2), latest=True)
        limit = max(0, len(project_notes) - height)
        view.project_top = min(
            view.project_top if view.project_top is not None else limit, limit
        )
        if picked and view.active == 2:
            index = project_notes.index(picked)
            view.project_top = max(0, index - height + 1, min(view.project_top, index))
        for i, note in enumerate(
            project_notes[view.project_top : view.project_top + height]
        ):
            y = start + 2 * i
            selected = view.active == 2 and note == picked
            write(
                win,
                y,
                second,
                ("› " if selected else "  ") + note["body"],
                w - second - 1,
                curses.A_REVERSE if selected else 0,
            )
            note_rows[2, y] = note

    lane_count = max(
        (lane + 1 for item in graphs for lane, _, _, _ in item["tracks"]), default=0
    )
    room = max(0, min(split - 8, split // 2))
    step = 2 if 2 * lane_count - 1 <= room else 1
    overflow = 0 < room < lane_count
    lanes = min(lane_count, room - overflow)
    text_x = 2 + max(0, (lanes - 1) * step + 1) + overflow
    for i in range(view.top, min(view.top + height, len(turns))):
        turn = turns[i]
        y = start + 2 * (i - view.top)
        selected = i == idx
        pointer = ">" if selected else " "
        style = curses.A_REVERSE if selected and view.active == 0 else 0
        if 0 in view.axes:
            write(win, y, 0, pointer, 1, curses.A_BOLD)
            draw_session_graph(win, y, graphs[i], palette, lanes, step, overflow)
            write(
                win,
                y,
                text_x,
                f"{turn['thread'][-5:]}  {turn['prompt']}",
                split - text_x - 1,
                style,
            )
            status = OBSERVATION.get(turn.get("observation"), STATE[turn["status"]])
            write(
                win,
                y + 1,
                text_x,
                f"{status} · {turn['summary']}",
                split - text_x - 1,
            )
        if 1 in view.axes:
            attached = turn_notes.get(keys[i], [])
            note = note_in(attached, view.note_ids.get(1))
            if note:
                prefix = (
                    f"[{attached.index(note) + 1}/{len(attached)}] "
                    if len(attached) > 1
                    else ""
                )
                note_selected = selected and view.active == 1
                write(
                    win,
                    y,
                    split,
                    ("› " if note_selected else "  ") + prefix + note["body"],
                    second - split - 1,
                    curses.A_REVERSE if note_selected else 0,
                )
                note_rows[1, y] = note
            if 0 not in view.axes:
                write(
                    win, y + 1, split, f"{pointer} {turn['prompt']}", second - split - 1
                )
        turn_rows[y] = turn_rows[y + 1] = keys[i]
    if not turns and view.axes & {0, 1}:
        write(win, start, 0, "No recorded turns yet", max(split, second) - 1)
    if not project_notes and 2 in view.axes:
        write(win, start, second, "No project notes yet", w - second - 1)
    action_labels = (
        ("Transcript", "+ Turn note", "+ Project note", "Older", "Latest")
        if w >= 61
        else ("Open", "+ Turn", "+ Project", "<", ">")
        if w >= 38
        else ("Open", "+T", "+P", "<", ">")
    )
    available = ""
    if 0 in view.axes:
        available += "t"
    if view.axes & {0, 1}:
        available += "mbg"
    if 2 in view.axes:
        available += "n"
    draw_buttons(
        win,
        h - 2,
        w - 1,
        (
            (label, ord(key))
            for label, key in zip(action_labels, "tmnbg")
            if key in available
        ),
        hits,
    )
    help_text = (
        "1–3 toggle axes   Tab focus   j/k move   Enter open   e edit   Esc back"
        if focused
        else "1–3 focus on an axis   Enter open   m/n add notes   q quit"
    )
    write(win, h - 1, 0, message or help_text, w - 1)
    return hits, turn_rows, note_rows, start, split, second


def show_item(win, view, item, message):
    h, w = win.getmaxyx()
    hits = {}
    write(win, 1, 2, axis_path(view, w - 3), w - 3, curses.A_BOLD)
    draw_buttons(win, 2, w - 1, [("Close", 27)], hits)
    write(win, 2, 8, item.title, w - 9, curses.A_REVERSE)
    width = w - 3
    if item.width != width:
        item.width = width
        item.lines = []
        for line in item.content.split("\n"):
            line = "".join(
                char
                for char in line.expandtabs(4)
                if not unicodedata.category(char).startswith("C")
            )
            if not line:
                item.lines.append("")
                continue
            start = used = 0
            for i, char in enumerate(line):
                cells = 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
                if used + cells > width:
                    item.lines.append(line[start:i])
                    start, used = i, 0
                used += cells
            item.lines.append(line[start:])
    height = h - 4
    item.offset = min(item.offset, max(0, len(item.lines) - height))
    for i, line in enumerate(item.lines[item.offset : item.offset + height], 3):
        write(win, i, 2, line, width)
    actions = "j/k scroll · Esc close"
    if item.note:
        actions += " · e edit"
    write(win, h - 1, 0, message or actions, w - 1)
    win.refresh()
    key = win.getch()
    if key == curses.KEY_MOUSE:
        event = mouse_event()
        if event is None:
            return 0
        x, y, key = event
        if not key:
            return hits.get((y, x), 0)
        if not 3 <= y < h - 1:
            return 0
    if key in MOVE:
        item.offset = max(0, min(item.offset + MOVE[key], len(item.lines) - height))
    return key


def transcript_item(api, project, turn):
    items = api.request(
        "transcript", project=project, thread=turn["thread"], turn=turn["turn"]
    )
    sections = []
    for item in items:
        content = item.get("text") or json.dumps(item, ensure_ascii=False, indent=2)
        sections.append(f"[{item['type']}]\n{content}")
    return Item(
        "Transcript · " + turn["prompt"], "\n".join(sections) or "No transcript items."
    )


def note_item(note, turns):
    title = "Project note"
    if note["turn"]:
        prompt = next(
            (r["prompt"] for r in turns if turn_key(r) == turn_key(note)), note["turn"]
        )
        title = "Turn note · " + prompt
    return Item(title, note["body"], note)


def update_note(win, api, project, item):
    body = edit_body(win, item.note["body"])
    if body:
        saved = api.request(
            "edit-note",
            {
                "project": project,
                "id": item.note["id"],
                "version": item.note["version"],
                "body": body,
            },
        )
        item.content = body
        item.width = 0
        item.note = item.note | {"body": body, "version": saved["version"]}


def show(win, api, project, mouse):
    palette = terminal_palette()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if mouse:
        curses.mousemask(CLICK | (WHEEL_UP | WHEEL_DOWN if WHEEL_DOWN else 0))
    win.timeout(300)
    root = view = Board()
    item = None
    turns, graphs, turn_notes, project_notes = [], [], {}, []
    loaded_before, last, message, has_older = None, 0, "", False
    while True:
        if item is None and (
            loaded_before != view.before or time.monotonic() - last >= 3
        ):
            try:
                batch = api.request(
                    "journey",
                    project=project,
                    limit=PAGE_SIZE + 1,
                    before=view.before,
                    lanes="true",
                )
                notes = api.request("notes", project=project)
                has_older = len(batch) > PAGE_SIZE
                turns = batch[-PAGE_SIZE:]
                graphs = (
                    list(layout(batch))[-PAGE_SIZE:]
                    if batch and "graph_lanes" not in batch[0]
                    else [row["graph_lanes"] for row in turns]
                )
                turn_notes, project_notes = {}, []
                for note in notes:
                    if note["turn"]:
                        turn_notes.setdefault(turn_key(note), []).append(note)
                    else:
                        project_notes.append(note)
                message = ""
            except (OSError, RuntimeError) as e:
                message = str(e)
            loaded_before, last = view.before, time.monotonic()

        h, w = win.getmaxyx()
        win.erase()
        write(win, 0, 0, project, w - 1, curses.A_BOLD)
        if h < 8 or w < 25:
            if h > 1:
                write(win, 1, 0, "Enlarge the pane · Esc back · q quit", w - 1)
            win.refresh()
            key = win.getch()
            if key == ord("q"):
                return
            if key == 27:
                if item is not None:
                    item = None
                else:
                    view = root
            continue

        if item is not None:
            key = show_item(win, view, item, message)
            if key in (27, 10, 13):
                item = None
                message = ""
            elif key == ord("q"):
                return
            elif key == ord("e") and item.note:
                try:
                    update_note(win, api, project, item)
                    message, last = "", 0
                except (OSError, RuntimeError) as e:
                    message = str(e)
            continue

        hits, turn_rows, note_rows, start, split, second = draw_board(
            win,
            view,
            view is not root,
            turns,
            graphs,
            turn_notes,
            project_notes,
            palette,
            message,
        )
        win.refresh()
        key = win.getch()
        clicked_note = None
        if key == curses.KEY_MOUSE:
            event = mouse_event()
            if event is None:
                continue
            x, y, key = event
            if not (0 <= x < w - 1 and 0 <= y < h):
                continue
            axis = 0 if x < split else 1 if x < second else 2
            if not key:
                key = hits.get((y, x), 0)
                if not key and start - 1 <= y < h - 2:
                    clicked_note = note_rows.get((axis, y))
                    view.active = axis
                    if axis < 2 and y in turn_rows:
                        view.turn = turn_rows[y]
                    if clicked_note:
                        view.note_ids[axis] = clicked_note["id"]
                        key = 10
            elif start <= y < h - 2:
                view.active = axis
                height = max(1, (h - start - 2) // 2)
                if axis == 0:
                    view.top = max(0, min(view.top + MOVE[key], len(turns) - height))
                elif axis == 2:
                    view.project_top = max(
                        0,
                        min(view.project_top + MOVE[key], len(project_notes) - height),
                    )
            else:
                continue

        if key == ord("q"):
            return
        if key == 27:
            view = root
            message = ""
            continue
        if key in AXIS_KEYS:
            axis = AXIS_KEYS.index(key)
            if view is root:
                view = root.focus(axis)
                focus_axis(view, axis, turns, turn_notes)
            else:
                view.toggle(axis)
            continue
        if key == 9:
            axes = sorted(view.axes)
            axis = axes[(axes.index(view.active) + 1) % len(axes)]
            focus_axis(view, axis, turns, turn_notes)
            continue

        selected_turn_notes = turn_notes.get(view.turn, [])
        keys = [turn_key(turn) for turn in turns]
        if key in MOVE:
            step = MOVE[key]
            if view.active == 1:
                items = [note for key in keys for note in turn_notes.get(key, [])]
                selected = note_in(selected_turn_notes, view.note_ids.get(1))
            elif view.active == 2:
                items = project_notes
                selected = note_in(items, view.note_ids.get(2), latest=True)
            else:
                items, selected = [], None
            if items:
                index = (
                    items.index(selected) + step
                    if selected is not None
                    else (0 if step > 0 else len(items) - 1)
                )
                note = items[max(0, min(len(items) - 1, index))]
                view.note_ids[view.active] = note["id"]
                if view.active == 1:
                    view.turn = turn_key(note)
            elif view.active != 2 and keys:
                view.turn = keys[
                    max(0, min(len(keys) - 1, keys.index(view.turn) + step))
                ]
            continue
        if key == ord("b") and has_older and turns:
            first = turns[0]
            view.before = json.dumps([first["started"], first["turn"], first["thread"]])
            view.top, view.turn, last = 0, None, 0
            continue
        if key == ord("g"):
            view.before, view.top, view.turn, last = "", 0, None, 0
            continue
        if key not in (10, 13, ord("t"), ord("e"), ord("m"), ord("n")):
            continue

        try:
            if key in (ord("m"), ord("n")):
                target = {}
                if key == ord("m"):
                    if view.turn is None:
                        continue
                    target = {"thread": view.turn[0], "turn": view.turn[1]}
                body = entry(win, "Note · Enter saves · Empty line cancels")
                if body:
                    identifier = uuid.uuid4().hex
                    api.request(
                        "notes",
                        {"project": project, "id": identifier, "body": body} | target,
                    )
                    view.note_ids[1 if target else 2] = identifier
                    last = 0
                continue
            if key == ord("t") or (key in (10, 13) and view.active == 0):
                if view.turn and 0 in view.axes:
                    item = transcript_item(api, project, turns[keys.index(view.turn)])
                    axis = 0
            else:
                axis = view.active
                if key == ord("e") and axis == 0:
                    axis = 1 if 1 in view.axes and selected_turn_notes else 2
                if axis not in view.axes:
                    continue
                items = selected_turn_notes if axis == 1 else project_notes
                note = clicked_note or note_in(
                    items, view.note_ids.get(axis), latest=axis == 2
                )
                if note:
                    item = note_item(note, turns)
            if item is not None:
                if view is root:
                    view = root.focus(axis)
                message = ""
                if key == ord("e"):
                    update_note(win, api, project, item)
                    last = 0
        except (OSError, RuntimeError) as e:
            message = str(e)


def run(api, project, *, mouse=True):
    if not (os.isatty(0) and os.isatty(1)):
        raise ValueError("Open the terminal journal in an interactive terminal")
    try:
        curses.wrapper(show, api, project, mouse)
    except curses.error as exc:
        raise RuntimeError(f"Could not use the terminal: {exc}") from exc
