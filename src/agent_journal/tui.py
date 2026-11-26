import curses
import json
import locale
import os
import select
import signal
import sys
import termios
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from itertools import islice

from .client_state import ClientState, background
from .common import open_web, state_dir
from .graph import layout
from .settings import DEFAULT_EXPLANATION_QUALITY, load_settings
from .text_layout import cell_width, fit, markdown_preview, note_title, wrap
from .tui_editor import Editor, Keys, Paste, editor_line, line_start

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
PAGE_KEYS = {"[": "earlier", "]": "later", "<": "first", ">": "last"}
NOTE_PRIORITIES = {
    "highlighted": ("Highlighted", "★ ", curses.A_BOLD),
    "normal": ("Normal", "", 0),
    "low": ("Low priority", "↓ ", curses.A_DIM),
}


@dataclass
class Board:
    axes: frozenset[int] = frozenset(range(3))
    active: int = 0
    turn: tuple[str, str] | None = None
    note_ids: dict[int, str] = field(default_factory=dict)
    top: int = 0
    note_top: int = 0
    project_top: int | None = None
    query: dict = field(default_factory=dict)
    legacy_newer_pages: tuple[str | None, ...] = ()

    def focus(self, axis):
        return replace(
            self, axes=frozenset({axis}), active=axis,
            note_ids=self.note_ids.copy(), query=self.query.copy(),
        )

    def toggle(self, axis):
        if axis not in self.axes:
            self.axes |= {axis}
        elif len(self.axes) > 1:
            self.axes -= {axis}
            if self.active == axis:
                self.active = min(self.axes)


@dataclass(kw_only=True)
class TextPane:
    content: str = ""
    offset: int = 0
    width: int = 0
    height: int = 0
    lines: list[str] = field(default_factory=list)
    wrapping: object = None
    bottom: bool = False

    def set_content(self, content):
        if self.content != content:
            self.content, self.width = content, 0


@dataclass
class Item(TextPane):
    title: str
    note: dict | None = None
    context: str = ""
    editor: Editor | None = None
    loading: bool = False
    message: str = ""
    confirm: str = ""
    choosing_priority: bool = False
    preview: str = ""
    preview_due: float = 0
    server_note: dict | None = None
    summary: TextPane | None = None
    summary_focused: bool = True
    pane_rows: tuple[int, int] = (0, 0)
    expanded: "Item | None" = None


def write(win, y, x, text, width, style=0):
    if width <= 0:
        return
    try:
        win.addstr(y, x, fit(str(text), width), style)
    except curses.error:
        pass


def terminal_palette():
    if not curses.has_colors():
        return [], 0
    try:
        curses.use_default_colors()
    except curses.error:
        return [], 0
    if os.environ.get("NO_COLOR"):
        return [], 0
    colors = (
        curses.COLOR_GREEN,
        curses.COLOR_MAGENTA,
        curses.COLOR_CYAN,
        curses.COLOR_BLUE,
        curses.COLOR_RED,
    )
    terminal_background = os.environ.get("COLORFGBG", "").split(";")[-1]
    muted_background = 238 if terminal_background in ("0", "8") else 251
    card_style = 0
    card_pair = 2 * len(colors) + 1
    if curses.COLORS >= 256 and card_pair < curses.COLOR_PAIRS:
        try:
            curses.init_pair(card_pair, -1, muted_background)
            card_style = curses.color_pair(card_pair)
        except curses.error:
            pass
    palette = []
    for pair, color in enumerate(colors[: curses.COLOR_PAIRS - 1], 1):
        if color < curses.COLORS:
            try:
                curses.init_pair(pair, color, -1)
            except curses.error:
                return [], 0
            normal = curses.color_pair(pair)
            highlighted = normal | curses.A_UNDERLINE
            highlight_pair = pair + len(colors)
            if card_style:
                try:
                    curses.init_pair(highlight_pair, color, muted_background)
                    highlighted = curses.color_pair(highlight_pair)
                except curses.error:
                    pass
            palette.append((normal, highlighted))
    return palette, card_style


def lane_style(lane, palette, *, highlighted=False):
    if not palette:
        return curses.A_UNDERLINE if highlighted else 0
    normal, emphasis = palette[lane % len(palette)]
    return emphasis if highlighted else normal


def draw_session_graph(win, y, graph, palette, lanes, step, overflow, *, end, junction):
    if not lanes:
        return
    point, tracks, forks = graph["lane"], graph["tracks"], graph["forks"]
    left, right = min([point, *forks]), max([point, *forks])
    if left < right and left < lanes:
        span = (min(right, lanes - 1) - left) * step + 1
        write(win, y, 2 + left * step, "─" * span, span, lane_style(point, palette))
    for lane, _, above, below in tracks:
        if lane >= lanes:
            continue
        x = 2 + lane * step
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
            for line in range(y + 1, end):
                write(win, line, x, "│", 1, style)
            write(win, end, x, junction, 1, style)
    if overflow and any(lane >= lanes for lane, _, _, _ in tracks):
        write(win, y, lanes + 2, "…", 1, curses.A_BOLD)


def draw_buttons(win, y, width, options, hits, active=(), *, toolbar=False, shortcuts=False):
    aliases = {27: "Esc", curses.KEY_EXIT: "Esc", 19: ":w", 16: "^P"}
    labels = [
        (f"[{label}]", key, " " + aliases.get(key, chr(key)) if shortcuts else "")
        for label, key in options
    ]
    gap = 2 if toolbar else 1
    if shortcuts and not toolbar and sum(
        len(label) + len(hint) + gap for label, _, hint in labels
    ) - gap > width:
        labels = [(label, key, "") for label, key, _ in labels]
    x = int(toolbar)
    for label, key, hint in labels:
        size = len(label) + len(hint)
        if x + size > width:
            if not toolbar:
                break
            y, x = y + 1, 1
        style = curses.A_REVERSE if toolbar or key in active else curses.A_BOLD
        write(win, y, x, label, len(label), style)
        if hint:
            write(win, y, x + len(label), hint, len(hint), curses.A_DIM)
        end = x + size
        hits.update(((y, col), key) for col in range(x, end))
        x = end + gap
    return y + 1


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


def turn_key(row):
    return row["thread"], row["turn"]


def note_in(items, identifier, *, latest=False):
    return next(
        (note for note in items if note["id"] == identifier),
        items[-1 if latest else 0] if items else None,
    )


def turn_note_entries(turns, turn_notes):
    return [
        (key, note)
        for key in map(turn_key, turns)
        for note in turn_notes.get(key) or (None,)
    ]


def selected_turn_note(view, turn_notes):
    return view.turn, note_in(turn_notes.get(view.turn, []), view.note_ids.get(1))


def select_turn_note(view, entry):
    view.turn, note = entry
    if note:
        view.note_ids[1] = note["id"]
    else:
        view.note_ids.pop(1, None)


def axis_path(view, width):
    axes = sorted(view.axes)
    names = " + ".join(f"{axis + 1} {AXES[axis]}" for axis in axes)
    if len(names) + 7 > width:
        names = " + ".join(str(axis + 1) for axis in axes)
    return "Axes · " + names


@dataclass
class BoardHits:
    buttons: dict = field(default_factory=dict)
    columns: dict = field(default_factory=dict)
    turns: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)
    visible: dict = field(default_factory=dict)
    heights: dict = field(default_factory=dict)
    start: int = 0
    end: int = 0


def card_window(heights, top, selected, capacity):
    if not heights:
        return 0, range(0)
    # Keep a full last screen where possible and bring the selection into view.
    last_top, used = len(heights) - 1, heights[-1]
    while last_top and used + heights[last_top - 1] <= capacity:
        last_top -= 1
        used += heights[last_top]
    top = min(selected, last_top, max(0, top if top is not None else last_top))
    used = sum(heights[top : selected + 1])
    while used > capacity and top < selected:
        used -= heights[top]
        top += 1
    end, used = top, 0
    while end < len(heights) and used + heights[end] <= capacity:
        used += heights[end]
        end += 1
    return top, range(top, end)


def pane_rule(win, y, spans, joints):
    for (left, right), (start, end) in zip(spans, zip(joints, joints[1:])):
        write(win, y, left, start + "─" * (right - left - 1) + end, right - left + 1)


def pane_sides(win, y, spans):
    write(win, y, spans[0][0], "│", 1)
    for _, right in spans:
        write(win, y, right, "│", 1)


def draw_cards(win, view, maps, axes, spans, heights, top, selected, paint):
    capacity = maps.end - maps.start
    top, visible = card_window(heights, top, selected, capacity)
    maps.heights.update(dict.fromkeys(axes, heights))
    paired = len(axes) == 2
    pane_rule(win, maps.start - 1, spans, "┌┬┐" if paired else "┌┐")
    for axis, (left, right) in zip(axes, spans):
        style = curses.A_REVERSE if axis == view.active else curses.A_BOLD
        markers = ("↑" if top else "") + ("↓" if visible.stop < len(heights) else "")
        label = AXES[axis] if right - left >= len(AXES[axis]) + len(markers) + 4 else str(axis + 1)
        write(win, maps.start - 1, left + 2, f" {label} ", right - left - 2, style)
        if markers and right - left > len(label) + len(markers) + 4:
            write(win, maps.start - 1, right - len(markers) - 1, markers, len(markers))
    y = maps.start
    for i in visible:
        end = y + heights[i] - 1
        if i == visible.stop - 1 and maps.end - end <= 3:
            end = maps.end - 1
        for row in range(y, end):
            pane_sides(win, row, spans)
        last = end == maps.end - 1
        pane_rule(
            win, end, spans,
            ("└┴┘" if paired else "└┘") if last else ("├┼┤" if paired else "├┤"),
        )
        paint(i, y, end, i == selected)
        y = end + 1
    for row in range(y, maps.end - 1):
        pane_sides(win, row, spans)
    if y < maps.end:
        pane_rule(win, maps.end - 1, spans, "└┴┘" if paired else "└┘")
    return top, max(1, len(visible))


def draw_board(
    win, view, focused, turns, graphs, turn_notes, project_notes, palette, message,
    *, page, card_background=0,
):
    h, w = win.getmaxyx()
    maps = BoardHits()
    hits = maps.buttons
    if focused:
        back_hint = w >= 34
        draw_buttons(win, 1, w - 1, [("Back", 27)], hits, shortcuts=back_hint)
        path_x = len("[Back] Esc " if back_hint else "[Back] ")
        write(win, 1, path_x, axis_path(view, w - path_x - 1), w - path_x - 1, curses.A_BOLD)
    buttons = 2 if focused else 1
    maps.start, maps.end = buttons + 2, h - 2
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
    boundaries = (0, w * weights[0] // total, w * sum(weights[:2]) // total, w)
    paired = {0, 1} <= view.axes
    for axis in sorted(view.axes):
        left, end = boundaries[axis : axis + 2]
        right = end - (1 if axis == max(view.axes) else 2)
        if paired and axis == 0:
            right = end
        maps.columns[axis] = left, right

    available_rows = maps.end - maps.start
    card_height = min(3, available_rows)
    keys = [turn_key(turn) for turn in turns]
    idx = keys.index(view.turn) if view.turn in keys else max(0, len(keys) - 1)
    view.turn = keys[idx] if keys else None
    lane_count = max(
        (lane + 1 for item in graphs for lane, _, _, _ in item["tracks"]), default=0
    )
    journey_right = maps.columns.get(0, (0, 0))[1]
    room = max(0, min(journey_right - 9, journey_right // 2))
    step = 2 if 2 * lane_count - 1 <= room else 1
    overflow = 0 < room < lane_count
    lanes = min(lane_count, room - overflow)
    graph_right = 2 + max(0, (lanes - 1) * step + 1) + overflow
    journey_left = graph_right + 2
    text_x = journey_left + 2

    def paint_turn(i, y, end, selected):
        turn = turns[i]
        graph = graphs[i]
        same_session = turn["thread"] == view.turn[0]
        background = card_background if same_session else 0
        if background:
            for left, right in spans:
                width = right - left - 1
                for row in range(y, end):
                    write(win, row, left + 1, " " * width, width, background)
        pointer = ">" if selected else " "
        style = curses.A_REVERSE if selected and view.active == 0 else background
        write(win, y, 1, pointer, 1, curses.A_BOLD)
        junction = "┴" if end == maps.end - 1 else "│"
        draw_session_graph(
            win, y, graph, palette, lanes, step, overflow, end=end, junction=junction,
        )
        width = journey_right - text_x
        session = turn["thread"][-6:]
        if width >= 16:
            session = "● " + session
        session = fit(session, max(1, width - 3))
        label_style = lane_style(graph["lane"], palette, highlighted=same_session)
        if same_session:
            label_style |= curses.A_BOLD
        if label_style & curses.A_UNDERLINE:
            label_style = (label_style | style) if not background else background | curses.A_BOLD
        label_width = cell_width(session)
        write(win, y, text_x, session, min(width, label_width), label_style)
        gap = 1 if width < 16 else 2
        write(win, y, text_x + label_width, " " * gap, min(gap, width - label_width), style)
        prompt_x = text_x + label_width + gap
        write(win, y, prompt_x, turn["prompt"], journey_right - prompt_x, style)
        if end - y > 1:
            status = OBSERVATION.get(turn.get("observation"), STATE[turn["status"]])
            write(
                win, y + 1, text_x, f"{status} · {turn['summary']}", journey_right - text_x, background,
            )
        for row in range(y, end):
            maps.turns[0, row] = keys[i]
        if not paired:
            return
        attached = turn_notes.get(keys[i], [])
        if not attached:
            paint_note(1, y, None, selected, background=background)
        picked = note_in(attached, view.note_ids.get(1))
        start = max(0, attached.index(picked) - (end - y) + 1) if picked else 0
        shown = attached[start : start + end - y]
        maps.visible[1] = maps.visible.get(1, 0) + max(1, len(shown))
        for offset, note in enumerate(shown, start):
            row = y + offset - start
            prefix = f"[{offset + 1}/{len(attached)}] " if len(attached) > 1 else ""
            paint_note(1, row, note, selected and note == picked, prefix, background=background)
        for row in range(y, end):
            maps.turns[1, row] = keys[i]

    def paint_note(axis, y, note, selected, prefix="", *, background=0):
        left, right = maps.columns[axis]
        active = selected and view.active == axis
        if note:
            _, marker, style = NOTE_PRIORITIES.get(note.get("priority"), NOTE_PRIORITIES["normal"])
            title = note_title(note)
            maps.notes[axis, y] = note
        else:
            marker, style, title = "", curses.A_DIM, "No turn notes"
        write(
            win, y, left + 1, ("› " if active else "  ") + prefix + marker + title,
            right - left - 1, curses.A_REVERSE if active else background | style,
        )

    if 0 in view.axes:
        axes = (0, 1) if paired else (0,)
        graph_span = [(0, graph_right)]
        pane_rule(win, maps.start - 1, graph_span, "┌┐")
        for y in range(maps.start, maps.end - 1):
            pane_sides(win, y, graph_span)
        pane_rule(win, maps.end - 1, graph_span, "└┘")
        spans = [(journey_left, journey_right)]
        if paired:
            spans.append(maps.columns[1])
        heights = [
            min(available_rows, max(2, len(turn_notes.get(key, [])) if paired else 0) + 1)
            for key in keys
        ]
        view.top, count = draw_cards(win, view, maps, axes, spans, heights, view.top, idx, paint_turn)
        maps.visible[0] = count
        if not turns:
            write(win, maps.start, text_x, "No recorded turns yet", journey_right - text_x)

    prompts = {turn_key(turn): turn["prompt"] for turn in turns}
    for axis, top_name in (
        (1, "note_top"),
        (2, "project_top"),
    ):
        if axis not in view.axes or (paired and axis == 1):
            continue
        if axis == 1:
            entries = turn_note_entries(turns, turn_notes)
            picked = selected_turn_note(view, turn_notes)
        else:
            entries = [(None, note) for note in project_notes]
            picked = (None, note_in(project_notes, view.note_ids.get(axis), latest=True))
        left, right = maps.columns[axis]

        def paint(i, y, end, selected):
            key, note = entries[i]
            paint_note(axis, y, note, selected)
            context = prompts[key] if key else note.get("preview", note.get("body", ""))
            if end - y > 1 and (not note or context.strip() != note_title(note)):
                write(win, y + 1, left + 3, context, right - left - 3, curses.A_DIM)
            for row in range(y, end):
                if note:
                    maps.notes[axis, row] = note
                if key:
                    maps.turns[axis, row] = key

        top, count = draw_cards(
            win, view, maps, (axis,), [(left, right)], [card_height] * len(entries),
            getattr(view, top_name), entries.index(picked) if entries else 0, paint,
        )
        setattr(view, top_name, top)
        maps.visible[axis] = count
        if not entries:
            write(win, maps.start, left + 2, "No " + AXES[axis].lower() + " yet", right - left - 2)
    action_labels = (
        ("Transcript", "+ Turn note", "+ Project note", "Older", "Newer", "Oldest", "Latest", "Paste", "Web")
        if w >= 61
        else ("Open", "+ Turn", "+ Project", "Prev", "Next", "First", "Last", "Paste", "Web")
        if w >= 38
        else ("Open", "+T", "+P", "Prev", "Next", "First", "Last", "Paste", "Web")
    )
    available = ""
    if 0 in view.axes:
        available += "t"
    if view.axes & {0, 1}:
        available += "mp"
    if 2 in view.axes:
        available += "n"
    prefix = "project_" if view.active == 2 else ""
    if page.get(prefix + "earlier"):
        available += "["
    if page.get(prefix + "later"):
        available += "]"
    available += "<>w"
    draw_buttons(
        win,
        h - 2,
        w - 1,
        (
            (label, ord(key))
            for label, key in zip(action_labels, "tmn[]<>pw")
            if key in available
        ),
        hits,
        shortcuts=True,
    )
    help_text = (
        "h/l axes · j/k move · gg/G page ends · [/] pages · </> oldest/latest · Esc back"
        if focused
        else "1–3 open an axis · h/l axes · j/k move · Enter open · m/n add notes · q quit"
    )
    write(win, h - 1, 0, message or help_text, w - 1)
    return maps


def transcript_text(api, project, turn):
    detail = api.request(
        "transcript", project=project, thread=turn["thread"], turn=turn["turn"],
        with_summary="true",
    )
    if isinstance(detail, list):
        detail = {"items": detail}
    sections = []
    for item in detail["items"]:
        if item["type"] == "commandExecution" and item.get("aggregatedOutput"):
            text = (item.get("command") or "") + "\n" + item["aggregatedOutput"]
        else:
            text = item.get("text") or json.dumps(item, ensure_ascii=False, indent=2)
        sections.append(f"[{item['type']}]\n{text}")
    summary = detail.get("summary_detail")
    return (
        markdown_preview(summary) if summary else (detail.get("summary") or turn.get("summary") or "No explanation available."),
        "\n".join(sections) or "No transcript items.",
    )


def panel(window, level):
    h, w = window.getmaxyx()
    top = level * 3
    if h - top < 10:
        window.erase()
        return window
    frame = window.derwin(h - top, w, top, 0)
    frame.erase()
    spans = [(0, w - 1)]
    pane_rule(window, top, spans, "┌┐")
    for y in range(top + 1, h - 1):
        pane_sides(window, y, spans)
    pane_rule(window, h - 1, spans, "└┘")
    return frame.derwin(h - top - 2, w - 2, 1, 1)


def detail_lines(item, width, height):
    item.height = height
    if item.width != width:
        item.width = width
        item.lines = []
        item.wrapping = wrap(item.content, width)
    if item.wrapping is not None:
        count = min(180, max(0, item.offset + height + 4 - len(item.lines)))
        if item.bottom:
            count = max(count, 180)
        if count:
            extra = list(islice(item.wrapping, count))
            item.lines.extend(extra)
            if len(extra) < count:
                item.wrapping = None
    if item.bottom:
        item.offset = max(0, len(item.lines) - height)
        item.bottom = item.wrapping is not None
    elif item.wrapping is None:
        item.offset = min(item.offset, max(0, len(item.lines) - height))
    return item.lines[item.offset : item.offset + height]


def draw_item_actions(window, options, hits):
    h, w = window.getmaxyx()
    separator = draw_buttons(
        window, 2, w - 1, options, hits, toolbar=True, shortcuts=h >= 11 or w >= 60
    )
    write(window, separator, 0, "─" * w, w, curses.A_DIM)
    return separator + 1


def draw_editor(window, item, status, hits):
    editor = item.editor
    h, w = window.getmaxyx()
    label_y = draw_item_actions(window, [("Close", curses.KEY_EXIT), ("Save", 19), ("Preview", 16)], hits)
    start = label_y + 1
    height = h - start - 1
    cursor = None
    split = editor.preview and w >= 86
    source_width = w // 2 - 2 if split else w - 1
    if editor.preview:
        left = source_width + 3 if split else 0
        preview_width = w - left - 1
        write(window, label_y, left, "Markdown preview", preview_width, curses.A_DIM)
        for y, line in enumerate(islice(wrap(item.preview, preview_width), height), start):
            write(window, y, left, line, preview_width)
    if not editor.preview or split:
        write(window, label_y, 0, "Markdown · # Title", source_width, curses.A_DIM)
        text = editor.note["body"]
        source = text.split("\n")
        row = text.count("\n", 0, editor.position)
        editor.top = max(0, row - height + 1, min(editor.top, row))
        for i in range(editor.top, min(len(source), editor.top + height)):
            y = start + i - editor.top
            if i == row:
                col = editor.position - line_start(text, editor.position)
                line, x = editor_line(source[i], col, source_width)
                write(window, y, 0, line, source_width)
                cursor = y, x
            else:
                write(window, y, 0, source[i], source_width)
    footer = (
        item.message or editor.command
        or f"{editor.mode} · {status} · :w save · ^P preview"
    )
    write(window, h - 1, 0, footer, w - 1)
    return cursor


def draw_item(window, view, item, status, *, expanded=False):
    h, w = window.getmaxyx()
    hits = {}
    write(window, 0, 0, axis_path(view, w - 1), w - 1, curses.A_DIM)
    heading = item.context or item.title
    if item.confirm:
        heading = f"Delete note · {item.title}"
    elif item.choosing_priority:
        heading = "Status · " + NOTE_PRIORITIES[item.note["priority"]][0]
    write(window, 1, 0, heading, w - 1, curses.A_BOLD)
    if item.server_note:
        label_y = draw_item_actions(window, [("Keep draft", ord("l")), ("Use server", ord("s"))], hits)
        write(window, label_y, 0, "Server · " + note_title(item.server_note), w - 1, curses.A_BOLD)
        start = label_y + 1
        for y, line in enumerate(islice(wrap(item.server_note["body"], w - 1), h - start - 1), start):
            write(window, y, 0, line, w - 1)
        write(window, h - 1, 0, "Local draft preserved · l keep draft · s use server · Esc back", w - 1)
        return hits, None
    if item.editor:
        return hits, draw_editor(window, item, status, hits)
    if item.choosing_priority:
        options = [(label, ord(str(i))) for i, (label, _, _) in enumerate(NOTE_PRIORITIES.values(), 1)]
        draw_item_actions(window, [*options, ("Cancel", 27)], hits)
        write(window, h - 1, 0, "1/2/3 set status · Esc cancel", w - 1)
        return hits, None
    controls = [("Back" if expanded else "Close", curses.KEY_EXIT)]
    if item.confirm:
        controls = [("Cancel", 27), ("Delete", ord("y"))]
    elif item.note and not item.loading:
        controls.append(("Edit", ord("e")))
        if "priority" in item.note:
            controls.append(("Status", ord("s")))
        if item.note["turn"]:
            controls.append(("Cut", ord("x")))
        controls.append(("Delete", ord("D")))
    start = draw_item_actions(window, controls, hits)
    if item.summary is not None:
        height = h - start - 1
        minimum = 3 if height >= 6 else 2 if height >= 4 else 1
        split = start + max(minimum, min(height - minimum, height * 2 // 5))
        item.pane_rows = start, split
        for y, end, label, pane, key in (
            (start, split, "Turn explanation", item.summary, "1"),
            (split, h - 1, "Transcript", item, "2"),
        ):
            focused = (pane is item.summary) == item.summary_focused
            style = curses.A_REVERSE if focused else curses.A_BOLD
            bordered = end - y >= 3
            bounds = [(0, w - 1)]
            if bordered:
                pane_rule(window, y, bounds, "┌┐")
                pane_rule(window, end - 1, bounds, "└┘")
                for row in range(y + 1, end - 1):
                    pane_sides(window, row, bounds)
            button = f"[View details] {key}" if w >= 36 else f"[Open] {key}"
            right = w - 2
            button_x = right - len(button)
            write(window, y, 2, f" {label} ", button_x - 3, style)
            write(window, y, button_x, button, len(button), curses.A_BOLD)
            hits.update(((y, column), ord(key)) for column in range(button_x, right))
            text_x = 2 if bordered else 0
            lines = detail_lines(pane, w - text_x - 2, max(0, end - y - 1 - int(bordered)))
            for row, line in enumerate(lines, y + 1):
                write(window, row, text_x, line, w - text_x - 2)
        footer = item.message or "Tab pane · j/k scroll · Enter details · Esc close"
        write(window, h - 1, 0, footer, w - 1)
        return hits, None
    if item.note:
        _, marker, style = NOTE_PRIORITIES.get(item.note.get("priority"), NOTE_PRIORITIES["normal"])
        write(window, start, 0, marker + item.title, w - 1, style or curses.A_BOLD)
        start += 1
    for y, line in enumerate(detail_lines(item, w - 1, h - start - 1), start):
        write(window, y, 0, line, w - 1)
    footer = item.message or ("j/k scroll · d/u page · gg/G ends · Esc close")
    if item.confirm:
        footer = item.confirm
    write(window, h - 1, 0, footer, w - 1)
    return hits, None


def sort_notes(notes, drafts, turns):
    project_notes = OrderedDict((note["id"], note) for note in notes[0])
    turn_notes = {turn_key(row): OrderedDict() for row in turns}
    for note in notes[1]:
        slot = turn_notes.get(turn_key(note))
        if slot is not None:
            slot[note["id"]] = note
    for draft in drafts.values():
        slot = turn_notes.get(turn_key(draft)) if draft["turn"] else project_notes
        if slot is not None and (draft["id"] in slot or draft["version"] is None):
            slot[draft["id"]] = slot.get(draft["id"], {}) | draft
    return list(project_notes.values()), {key: list(value.values()) for key, value in turn_notes.items()}


def page_cursor(row, fields):
    return json.dumps([row[key] for key in fields], separators=(",", ":"))


class Terminal:
    def __init__(self, window, api, project):
        self.window, self.api, self.project = window, api, project
        self.state = ClientState(api, project)
        self.root = self.view = Board()
        self.item = self.board_job = self.detail_job = self.preview_job = self.mutation = None
        self.detail_request = None
        self.web_job = None
        self.settings_root = state_dir()
        self.explanation_quality = DEFAULT_EXPLANATION_QUALITY
        self.settings_refresh = 0
        self.cut = None
        self.pages = OrderedDict()
        self.details = OrderedDict()
        self.refresh_after = {}
        self.message = ""
        self.prefix = ""
        self.offline = False
        self.saved_revision = 0
        self.board_generation = 0
        self.last_read = {}
        self.jump_when_ready = None
        self.palette, self.card_background = terminal_palette()
        self.keys = Keys(window)

    def page(self, query):
        key = json.dumps(query, sort_keys=True)
        if key not in self.pages:
            self.pages[key] = self.state.cached_board(query)
            while len(self.pages) > 12:
                self.pages.popitem(last=False)
        self.pages.move_to_end(key)
        return key, self.pages[key]

    def invalidate_board(self):
        self.refresh_after.clear()
        self.board_generation += 1

    def request_detail(self, key, edit, function, *args):
        self.detail_request = self.item, key, edit, function, args
        self.start_detail()

    def start_detail(self):
        if self.detail_job is None and self.detail_request is not None:
            item, key, _, function, args = self.detail_request
            if item is self.item:
                self.detail_job = background(function, *args), key
            else:
                self.detail_request = None

    def poll(self):
        when = time.monotonic()
        redraw = self.state.revision != self.saved_revision
        if redraw:
            self.saved_revision = self.state.revision
            self.invalidate_board()
        if when >= self.settings_refresh:
            self.settings_refresh = when + 1
            try:
                quality = load_settings(self.settings_root)["explanation_quality"]
            except (OSError, ValueError):
                quality = DEFAULT_EXPLANATION_QUALITY
            if quality != self.explanation_quality:
                self.explanation_quality = quality
                redraw = True
        if self.web_job is not None and self.web_job.done():
            try:
                self.message = "Web journal opened" if self.web_job.result() else "Could not open a browser"
            except (OSError, RuntimeError, ValueError) as error:
                self.message = f"Could not open the web journal · {error}"
            self.web_job = None
            redraw = True
        if self.board_job is not None and self.board_job[0].done():
            job, key, generation = self.board_job
            self.board_job = None
            try:
                data = job.result()
            except (OSError, RuntimeError) as error:
                self.offline = True
                self.message = "Offline · cached data · " + str(error)
                self.refresh_after[key] = when + 4
            else:
                self.offline = False
                if self.message.startswith("Offline"):
                    self.message = ""
                if generation == self.board_generation:
                    self.pages[key] = data
                    self.last_read[key] = when
                    self.refresh_after[key] = when + (15 if data.get("later") else 3)
            redraw = True

        key, data = self.page(self.view.query)
        if self.board_job is None and when >= self.refresh_after.get(key, 0):
            job = background(self.state.board, self.view.query.copy(), data)
            self.board_job = job, key, self.board_generation

        if self.detail_job is not None and self.detail_job[0].done():
            job, key = self.detail_job
            self.detail_job = None
            request = self.detail_request
            if request and request[0] is self.item and request[1] == key:
                item, _, edit, _, _ = request
                self.detail_request = None
                item.loading = False
                try:
                    result = job.result()
                except (OSError, RuntimeError) as error:
                    item.message = str(error)
                    if item.summary is not None:
                        item.set_content("Transcript unavailable.")
                    if edit and item.note and "body" in item.note:
                        self.edit_item()
                else:
                    if result is not None:
                        if key[-1] == "server":
                            item.server_note = result
                            item.message = ""
                            return True
                        self.details[key] = result
                        self.details.move_to_end(key)
                        while len(self.details) > 10:
                            self.details.popitem(last=False)
                        if item.summary is not None:
                            summary, transcript = result
                            item.summary.set_content(summary)
                            item.set_content(transcript)
                        elif isinstance(result, dict):
                            item.note = result
                            item.title = note_title(result)
                            item.set_content("")
                            item.preview_due = when
                            if edit:
                                self.edit_item()
                        else:
                            item.set_content(result)
                redraw = True
            self.start_detail()

        if self.preview_job is not None and self.preview_job[0].done():
            job, item, source = self.preview_job
            self.preview_job = None
            if item is self.item and source == self.preview_input():
                item.preview = job.result()
                if not item.editor:
                    item.set_content(item.preview)
                redraw = True
        if self.item and self.item.preview_due and self.item.preview_due <= when and not self.preview_job:
            source = self.preview_input()
            self.preview_job = background(markdown_preview, *source), self.item, source
            self.item.preview_due = 0

        if self.mutation is not None and self.mutation[0].done():
            job, action, _, neighbor = self.mutation
            self.mutation = None
            try:
                result = job.result()
            except (OSError, RuntimeError) as error:
                self.message = str(error)
                if self.item:
                    self.item.message = self.message
            else:
                self.details.clear()
                self.patch_note_pages(result, action)
                self.invalidate_board()
                if action == "set-note-priority":
                    if self.cut and self.cut["id"] == result["id"]:
                        self.cut = result
                    if self.item and self.item.note and self.item.note["id"] == result["id"]:
                        if self.item.note.get("body") != result["body"]:
                            self.item.title = note_title(result)
                            self.item.set_content("")
                            self.item.preview_due = when
                        self.item.note = result
                        self.item.message = "Note status updated"
                    return True
                self.state.discard(result["id"])
                if self.cut and self.cut["id"] == result["id"]:
                    self.cut = None
                self.message = "Note moved" if action == "move-note" else "Note deleted"
                if action == "delete-note":
                    axis = self.view.active
                    if self.view.note_ids.get(axis) == result["id"] or (
                        self.item and self.item.note and self.item.note["id"] == result["id"]
                    ):
                        self.view.note_ids.pop(axis, None)
                        if neighbor:
                            self.view.note_ids[axis] = neighbor["id"]
                            if neighbor["turn"]:
                                self.view.turn = turn_key(neighbor)
                    if self.item and self.item.note and self.item.note["id"] == result["id"]:
                        self.item = None
                else:
                    self.view.note_ids[1] = result["id"]
            redraw = True
        return redraw

    def patch_note_pages(self, note, action):
        for page in self.pages.values():
            if not page:
                continue
            for name in ("turn_notes", "project_notes"):
                if action == "set-note-priority":
                    for item in page[name]:
                        if item["id"] == note["id"]:
                            item.update(priority=note["priority"], version=note["version"])
                else:
                    page[name] = [item for item in page[name] if item["id"] != note["id"]]
            if action == "move-note" and any(turn_key(row) == turn_key(note) for row in page["turns"]):
                summary = {key: value for key, value in note.items() if key != "body"}
                page["turn_notes"].append(summary | {"preview": note["body"][:200]})
                page["turn_notes"].sort(key=lambda item: (item["created"], item["id"]))
            page.pop("revision", None)

    def preview_input(self):
        if self.item.editor:
            return self.item.editor.note["body"], False
        return self.item.note["body"], True

    def data(self, view):
        _, data = self.page(view.query)
        turns = data.get("turns", [])
        graph = (
            [row["graph_lanes"] for row in turns]
            if all("graph_lanes" in row for row in turns) else list(layout(turns))
        )
        project_notes, turn_notes = sort_notes(
            (data.get("project_notes", []), data.get("turn_notes", [])),
            self.state.drafts, turns,
        )
        return data, turns, graph, turn_notes, project_notes

    def draw_header(self, win, width):
        label = f"Explanation quality · {self.explanation_quality}"
        if width - cell_width(label) < 12:
            label = self.explanation_quality
        position = width - cell_width(label) - 1
        write(win, 0, 0, self.project, position - 2, curses.A_BOLD)
        write(win, 0, position, label, cell_width(label), curses.A_DIM)

    def draw(self):
        win = self.window
        h, w = win.getmaxyx()
        win.erase()
        self.draw_header(win, w)
        self.cursor = None
        self.board_hit = self.item_hit = None
        if h < 9 or w < 25:
            write(win, 1, 0, "Enlarge the pane · Esc back · q quit", w - 1)
            win.refresh()
            return
        message = self.message
        if not message:
            key, snapshot = self.page(self.view.query)
            if not snapshot:
                message = "Loading journal…"
            elif key not in self.last_read or time.monotonic() - self.last_read[key] > 20:
                message = "Cached data · refreshing"
        for level, board in enumerate([self.root] if self.view is self.root else [self.root, self.view]):
            window = panel(win, level) if level else win
            if window is win:
                self.draw_header(win, w)
            page, turns, graph, turn_notes, project_notes = self.data(board)
            maps = draw_board(
                window, board, bool(level), turns, graph, turn_notes, project_notes,
                self.palette, message if board is self.view else "",
                page=page, card_background=self.card_background,
            )
            if board is self.view:
                self.board_hit = window.getbegyx(), window.getmaxyx(), maps
        if self.item:
            expanded = self.item.expanded
            if expanded:
                source = self.item.summary if self.item.summary_focused else self.item
                expanded.set_content(source.content)
            for level, item in enumerate((self.item, expanded), 2):
                if item is None:
                    break
                window = panel(win, level)
                status = self.state.status(item.note["id"]) if item.note else ""
                hits, cursor = draw_item(window, self.view, item, status, expanded=item is expanded)
                y, x = window.getbegyx()
                self.item_hit = (y, x, window.getmaxyx(), hits)
                if cursor is not None:
                    self.cursor = y + cursor[0], x + cursor[1]
                    win.move(*self.cursor)
        try:
            curses.curs_set(bool(self.cursor))
        except curses.error:
            pass
        win.refresh()

    def open_transcript(self, row):
        if self.view is self.root:
            self.view = self.root.focus(0)
        title = "Turn details · " + row["prompt"]
        key = (*turn_key(row), row.get("revision"))
        cached = self.details.get(key) if row.get("revision") is not None else None
        summary, transcript = cached or (row.get("summary") or "No explanation available.", "Loading transcript…")
        self.item = Item(
            title, content=transcript, loading=cached is None, summary=TextPane(content=summary)
        )
        if cached is None:
            self.request_detail(key, False, transcript_text, self.api, self.project, row)

    def open_note(self, note, turns, *, edit=False):
        if self.view is self.root:
            self.view = self.root.focus(1 if note["turn"] else 2)
        context = "Project note"
        if note["turn"]:
            prompt = next(
                (r["prompt"] for r in turns if turn_key(r) == turn_key(note)), note["turn"]
            )
            context = "Turn note · " + prompt
        key = (note["id"], note.get("version"))
        latest = self.details.get(key) or note
        saved = self.state.recoveries.get(note["id"], {})
        if saved.get("version", 0) > (latest.get("version") or 0):
            latest = latest | saved
        latest = latest | self.state.drafts.get(note["id"], {})
        cached = "body" in latest
        self.item = Item(
            note_title(latest), note=latest, context=context, content="" if cached else "Loading note…",
            loading=not cached, preview_due=time.monotonic() if cached else 0,
        )
        if not cached:
            self.request_detail(key, edit, self.state.detail, note)
        elif edit:
            self.edit_item()

    def edit_item(self):
        if self.mutation and self.mutation[2] == self.item.note["id"]:
            self.item.message = "The note change is still being saved."
            return
        self.item.note = self.item.note | self.state.drafts.get(self.item.note["id"], {})
        self.item.editor = Editor(self.item.note)
        self.item.preview_due = time.monotonic()
        self.item.message = ""

    def new_note(self, target, turns):
        note = {
            "id": uuid.uuid4().hex, "thread": target["thread"] if target else None,
            "turn": target["turn"] if target else None, "body": "", "version": None,
        }
        self.open_note(note, turns, edit=True)
        self.item.editor.mode = "Insert"

    def save(self, *, immediate=False):
        note = self.item.editor.note
        saved = self.state.recoveries.get(note["id"], {})
        if saved.get("version", 0) > (note["version"] or 0):
            note["version"] = saved["version"]
        try:
            self.state.edit(note, immediate=immediate)
        except OSError as error:
            self.item.message = f"Local draft could not be saved · {error}"
            return False
        self.item.preview_due = time.monotonic() + 0.14
        return True

    def close_item(self):
        if self.item.expanded is not None:
            self.item.expanded = None
            self.prefix = ""
            return
        if self.item.editor and self.item.note["id"] in self.state.drafts:
            if not self.save(immediate=True):
                return
        self.item = None
        self.detail_request = None

    def mutate(self, action, note, **target):
        if self.mutation is not None:
            return
        if note["id"] in self.state.drafts:
            self.message = "Sync this note's local draft first."
            if self.item:
                self.item.message = self.message
            return
        neighbor = None
        if action == "delete-note":
            _, turns, _, turn_notes, project_notes = self.data(self.view)
            notes = (
                [item for row in turns for item in turn_notes.get(turn_key(row), ())]
                if note["turn"] else project_notes
            )
            for index, item in enumerate(notes):
                if item["id"] == note["id"] and len(notes) > 1:
                    neighbor = notes[index + 1] if index + 1 < len(notes) else notes[index - 1]
                    break
        self.mutation = (
            background(self.api.request, action, {
                "project": self.project, "id": note["id"], "version": note["version"], **target
            }),
            action, note["id"], neighbor,
        )

    def detail_key(self, key):
        item = self.item.expanded or self.item
        if key == curses.KEY_EXIT:
            self.close_item()
            return
        if item.confirm:
            item.confirm = ""
            if key == "y":
                self.mutate("delete-note", item.note)
            return
        if isinstance(key, int) and key < 256 and key not in (curses.KEY_MOUSE,):
            key = chr(key)
        if item.server_note:
            server, item.server_note = item.server_note, None
            if key == "l":
                self.state.drafts[item.note["id"]]["conflict"] = False
                item.editor.note["version"] = server["version"]
                self.save(immediate=True)
            elif key == "s":
                self.state.discard(item.note["id"])
                item.note, item.editor = server, None
                item.title = note_title(server)
                item.set_content("")
                item.preview_due = time.monotonic()
            return
        if item.editor:
            if key == "\x1b" and item.editor.mode == "Normal" and not item.editor.command:
                self.close_item()
                return
            if key == "\x12" and self.state.drafts.get(item.note["id"], {}).get("conflict"):
                note = item.note.copy()
                self.request_detail((note["id"], "server"), False, self.state.detail, note)
                item.message = "Loading server version · local draft preserved"
                return
            result = item.editor.input(key)
            if result == "change":
                self.save()
            elif result in ("save", "save-close"):
                if self.save(immediate=True) and result == "save-close":
                    self.item = None
            elif result == "close":
                self.close_item()
            return
        if item.choosing_priority:
            item.choosing_priority = False
            if key in ("1", "2", "3"):
                priority = tuple(NOTE_PRIORITIES)[int(key) - 1]
                if priority != item.note["priority"]:
                    self.mutate("set-note-priority", item.note, priority=priority)
            return
        if item.summary is not None and key in ("1", "2", "\n", "\r"):
            if key in ("1", "2"):
                item.summary_focused = key == "1"
            title = "Turn explanation" if item.summary_focused else "Transcript"
            item.expanded = Item(title)
            self.prefix = ""
            return
        if key in ("\x1b", "\n", "\r"):
            self.close_item()
            return
        if key == "e" and item.note:
            if "body" in item.note:
                self.edit_item()
            elif self.detail_request and self.detail_request[0] is item:
                _, identity, _, function, args = self.detail_request
                self.detail_request = item, identity, True, function, args
            return
        if key == "s" and item.note and not item.loading and "priority" in item.note:
            item.choosing_priority = True
            return
        if key in ("x", "D") and item.note and item.note.get("version") is not None:
            self.cut_note(key)
            return
        if item.summary is not None and key in ("\t", curses.KEY_BTAB):
            item.summary_focused = not item.summary_focused
            self.prefix = ""
            return
        pane = item.summary if item.summary_focused and item.summary is not None else item
        page = max(1, pane.height // 2)
        if key == "g":
            if self.prefix == "g":
                pane.offset = 0
                pane.bottom = False
                self.prefix = ""
            else:
                self.prefix = "g"
            return
        self.prefix = ""
        if key == "G":
            pane.bottom = True
        else:
            step = {**MOVE, "j": 1, "k": -1, "d": page, "u": -page}.get(key)
            if step:
                pane.bottom = False
                pane.offset = max(0, min(len(pane.lines), pane.offset + step))

    def cut_note(self, key):
        if key == "D":
            self.item.confirm = "Delete this note? y to delete · any other key cancels"
        elif self.item.note["turn"]:
            self.cut = self.item.note.copy()
            self.message = "Turn note cut · select a turn and press p to move"
            self.close_item()

    def jump(self, position, data):
        project = self.view.active == 2
        rows = data.get("project_notes" if project else "turns", [])
        prefix = "project_" if project else ""
        fields = ("created", "id") if project else ("started", "turn", "thread")
        paging = position in ("earlier", "later")
        if paging and (not rows or not data.get(prefix + position)):
            direction = "older" if position == "earlier" else "newer"
            entries = "project notes" if project else "turns"
            self.message = f"No {direction} {entries}" if data else ""
            return
        self.message = ""
        before = self.view.query.get("before", data.get("legacy_before"))
        for name in ("before", "after", "edge"):
            self.view.query.pop(prefix + name, None)
        if paging:
            earlier = position == "earlier"
            if not project and (data.get("legacy") or self.state.modern is False):
                if earlier:
                    self.view.legacy_newer_pages += (before,)
                    self.view.query["before"] = page_cursor(rows[0], fields)
                elif self.view.legacy_newer_pages:
                    previous = self.view.legacy_newer_pages[-1]
                    self.view.legacy_newer_pages = self.view.legacy_newer_pages[:-1]
                    if previous is not None:
                        self.view.query["before"] = previous
            else:
                self.view.query[prefix + ("before" if earlier else "after")] = page_cursor(
                    rows[0 if earlier else -1], fields
                )
        else:
            if not project:
                self.view.legacy_newer_pages = ()
            if position == "first" or self.view.active == 1:
                self.view.query[prefix + "edge"] = position
        self.view.note_ids.pop(self.view.active, None)
        if project:
            self.view.project_top = None
        else:
            self.view.top = self.view.note_top = 0
            self.view.turn = None
        self.jump_when_ready = (self.view.query.copy(), self.view.active, position)

    def finish_jump(self, data, turns, turn_notes, project_notes):
        jump = self.jump_when_ready
        if not jump or not data or self.view.query != jump[0]:
            return
        self.jump_when_ready = None
        _, axis, position = jump
        if "legacy_newer_pages" in data and axis != 2:
            self.view.legacy_newer_pages = tuple(data["legacy_newer_pages"])
        self.select_page_end(axis, position in ("first", "later"), turns, turn_notes, project_notes)

    def select_page_end(self, axis, first, turns, turn_notes, project_notes):
        index = 0 if first else -1
        if axis == 2 and project_notes:
            self.view.note_ids[axis] = project_notes[index]["id"]
        elif axis == 1 and turns:
            select_turn_note(self.view, turn_note_entries(turns, turn_notes)[index])
        elif axis == 0 and turns:
            self.view.turn = turn_key(turns[index])

    def list_key(self, key, clicked=None):
        data, turns, _, turn_notes, project_notes = self.data(self.view)
        if key == "q":
            return False
        if key == "w":
            if self.web_job is None:
                self.web_job = background(open_web, self.api, self.project)
                self.message = "Opening the web journal…"
            return True
        if key == "\x1b":
            self.view = self.root
            self.message = ""
            return True
        if key == "X":
            self.cut = None
            self.message = "Cut cancelled"
            return True
        if isinstance(key, str) and len(key) == 1 and key in "123":
            axis = int(key) - 1
            if self.view is self.root:
                self.view = self.root.focus(axis)
            else:
                self.view.toggle(axis)
            return True
        if key in ("h", "l", "\t", curses.KEY_BTAB):
            axes = sorted(self.view.axes)
            index = axes.index(self.view.active)
            target = index + (1 if key in ("l", "\t") else -1)
            if key in ("\t", curses.KEY_BTAB):
                target %= len(axes)
            if 0 <= target < len(axes):
                self.view.active = axes[target]
            self.prefix = ""
            return True
        axis = self.view.active
        selected = next((row for row in turns if turn_key(row) == self.view.turn), None)
        if key == "g" and self.prefix != "g":
            self.prefix = "g"
            return True
        self.prefix = ""
        if key in ("g", "G"):
            self.select_page_end(axis, key == "g", turns, turn_notes, project_notes)
        elif isinstance(key, str) and key in PAGE_KEYS:
            self.jump(PAGE_KEYS[key], data)
        elif key in ("j", "k", "d", "u", curses.KEY_UP, curses.KEY_DOWN, curses.KEY_NPAGE, curses.KEY_PPAGE):
            shown = self.board_hit[2].visible.get(axis, 1) if self.board_hit else 1
            half = max(1, shown // 2)
            step = {**MOVE, "j": 1, "k": -1, "d": half, "u": -half,
                    curses.KEY_NPAGE: shown, curses.KEY_PPAGE: -shown}[key]
            if axis == 2:
                items = project_notes
                picked = note_in(items, self.view.note_ids.get(axis), latest=True)
                identifier = picked["id"] if picked else None
            elif axis == 1:
                entries = turn_note_entries(turns, turn_notes)
                if entries:
                    current = selected_turn_note(self.view, turn_notes)
                    index = entries.index(current) if current in entries else len(entries) - 1
                    select_turn_note(self.view, entries[max(0, min(index + step, len(entries) - 1))])
                return True
            else:
                items = turns
                identifier = self.view.turn
            if items:
                keys = [turn_key(row) if axis == 0 else row["id"] for row in items]
                index = keys.index(identifier) + step if identifier in keys else (0 if step > 0 else len(keys) - 1)
                choice = items[max(0, min(index, len(items) - 1))]
                if axis != 0:
                    self.view.note_ids[axis] = choice["id"]
                if axis != 2:
                    self.view.turn = turn_key(choice)
        elif key in ("m", "n"):
            if key == "n" or selected:
                self.new_note(selected if key == "m" else None, turns)
        elif key == "p" and self.cut and selected:
            self.mutate("move-note", self.cut, thread=selected["thread"], turn=selected["turn"])
        elif key in ("t", "\n", "\r", "e"):
            if key == "t" or (key in ("\n", "\r") and axis == 0):
                if selected:
                    self.open_transcript(selected)
            else:
                if axis == 0:
                    axis = 1 if turn_notes.get(self.view.turn) else 2
                candidates = project_notes if axis == 2 else turn_notes.get(self.view.turn, [])
                note = clicked or note_in(candidates, self.view.note_ids.get(axis), latest=axis == 2)
                if note:
                    self.open_note(note, turns, edit=key == "e")
        return True

    def scroll_axis(self, axis, step, maps):
        _, turns, _, turn_notes, project_notes = self.data(self.view)
        standalone_notes = axis == 1 and 0 not in self.view.axes
        if axis == 2:
            items, offset = project_notes, "project_top"
        elif standalone_notes:
            items, offset = turn_note_entries(turns, turn_notes), "note_top"
        else:
            items, offset = turns, "top"
        heights = maps.heights.get(axis, [])
        self.view.active = axis
        if not heights or len(items) != len(heights):
            return
        first = max(0, min(len(items) - 1, getattr(self.view, offset) + step))
        top, visible = card_window(heights, first, first, maps.end - maps.start)
        setattr(self.view, offset, top)
        if standalone_notes:
            selected = selected_turn_note(self.view, turn_notes)
            keys = items
        elif axis == 2:
            selected = self.view.note_ids.get(axis)
            keys = [item["id"] for item in items]
        else:
            selected = self.view.turn
            keys = [turn_key(item) for item in items]
        index = keys.index(selected) if selected in keys else (visible.stop - 1 if step < 0 else top)
        choice = items[max(top, min(index, visible.stop - 1))]
        if standalone_notes:
            select_turn_note(self.view, choice)
        elif axis == 2:
            self.view.note_ids[axis] = choice["id"]
        else:
            self.view.turn = turn_key(choice)

    def mouse_key(self):
        event = mouse_event()
        if event is None:
            return None, None
        x, y, key = event
        if self.item:
            if not self.item_hit:
                return None, None
            top, left, (height, width), hits = self.item_hit
            if not (left <= x < left + width and top <= y < top + height):
                return None, None
            hit = hits.get((y - top, x - left))
            item = self.item.expanded or self.item
            summary_row, transcript_row = item.pane_rows
            if item.summary is not None and not hit and summary_row <= y - top < height - 1:
                summary_focused = y - top < transcript_row
                if summary_focused != item.summary_focused:
                    self.prefix = ""
                    item.summary_focused = summary_focused
                if not key:
                    return "", None
            if key:
                return key, None
            return (chr(hit) if hit and hit < 256 else hit), None
        if not self.board_hit:
            return None, None
        (top, left), (height, width), maps = self.board_hit
        y, x = y - top, x - left
        if not (0 <= y < height and 0 <= x < width):
            return None, None
        axis = next(
            (axis for axis, (left, right) in reversed(tuple(maps.columns.items())) if left <= x <= right),
            None,
        )
        if key:
            if axis is None or not maps.start <= y < maps.end:
                return None, None
            self.scroll_axis(axis, MOVE[key], maps)
            return "", None
        if hit := maps.buttons.get((y, x)):
            return chr(hit), None
        if axis is not None and maps.start - 1 <= y < maps.end:
            self.view.active = axis
            if turn := maps.turns.get((axis, y)):
                self.view.turn = turn
            if note := maps.notes.get((axis, y)):
                self.view.note_ids[axis] = note["id"]
                return "\n", note
            return "", None
        return None, None

    def loop(self):
        terminal_input = select.poll()
        terminal_input.register(0, select.POLLHUP | select.POLLERR | select.POLLNVAL)
        dirty = True
        last_status = None
        while True:
            try:
                self.state.poll(time.time())
            except OSError as error:
                self.message = str(error)
            dirty |= self.poll()
            if self.jump_when_ready:
                data, turns, _, turn_notes, project_notes = self.data(self.view)
                self.finish_jump(data, turns, turn_notes, project_notes)
            status = self.state.status(self.item.note["id"]) if self.item and self.item.note else None
            needs_lines = self.item and any(
                pane.wrapping is not None and (pane.bottom or len(pane.lines) <= pane.offset)
                for pane in (self.item, self.item.summary, self.item.expanded) if pane is not None
            )
            if dirty or status != last_status or needs_lines:
                self.draw()
            dirty = False
            last_status = status
            key = self.keys.read()
            clicked = None
            if key == curses.KEY_MOUSE:
                key, clicked = self.mouse_key()
            if key is None:
                # curses reports a disconnected PTY as the same error as a key timeout.
                if terminal_input.poll(0):
                    return
                continue
            dirty = True
            if key == curses.KEY_RESIZE:
                continue
            if self.item:
                self.detail_key(key)
            elif not isinstance(key, Paste) and not self.list_key(key, clicked):
                return


def show(window, api, project, mouse):
    window.encoding = "utf-8"
    curses.set_escdelay(35)
    if mouse:
        curses.mousemask(CLICK | WHEEL_UP | WHEEL_DOWN)
    window.timeout(40)
    terminal = Terminal(window, api, project)
    sys.stdout.write("\x1b[?2004h")
    sys.stdout.flush()
    try:
        terminal.loop()
    finally:
        try:
            for draft in terminal.state.drafts.values():
                terminal.state.checkpoint(draft)
        finally:
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()


def exit_on_terminal_signal(signum, frame):
    raise SystemExit(128 + signum)


def run(api, project, *, mouse=True):
    if not (os.isatty(0) and os.isatty(1)):
        raise ValueError("Open the terminal journal in an interactive terminal")
    if locale.getencoding().lower().replace("-", "") != "utf8":
        for candidate in ("C.UTF-8", "en_US.UTF-8"):
            try:
                locale.setlocale(locale.LC_CTYPE, candidate)
            except locale.Error:
                continue
            break
        else:
            raise ValueError("Set a UTF-8 locale before opening the terminal journal")
    terminal = os.environ.get("TERM")
    use_compatibility_profile = (
        sys.platform == "darwin"
        and terminal == "xterm-ghostty"
        and getattr(curses, "ncurses_version", (0, 0))[:2] <= (6, 0)
    )
    tty = termios.tcgetattr(sys.stdin)
    handlers = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            handlers[sig] = signal.signal(sig, exit_on_terminal_signal)
        if use_compatibility_profile:
            # This curses version corrupts repeated UTF-8 glyphs with Ghostty's repeat capability.
            os.environ["TERM"] = "xterm-256color"
        editor_tty = tty.copy()
        editor_tty[0] &= ~termios.IXON
        termios.tcsetattr(sys.stdin, termios.TCSANOW, editor_tty)
        curses.wrapper(show, api, project, mouse)
    except curses.error as exc:
        raise RuntimeError(f"Could not use the terminal: {exc}") from exc
    finally:
        try:
            termios.tcsetattr(sys.stdin, termios.TCSANOW, tty)
        except termios.error:
            # A hung-up terminal may already be gone.
            pass
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if use_compatibility_profile:
            os.environ["TERM"] = terminal
