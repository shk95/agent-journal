import curses
import json
import os
import shlex
import subprocess
import tempfile
import textwrap
import time
import unicodedata
import uuid
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


def choose_note(win, candidates):
    selected = 0
    win.timeout(-1)
    try:
        while True:
            h, w = win.getmaxyx()
            win.erase()
            hits = {}
            draw_buttons(win, 0, w - 1, [("Cancel", 27)], hits)
            write(
                win, 0, 10, "Edit a note · j/k select · Enter edit · Esc cancel", w - 11
            )
            count = max(1, h - 2)
            top = max(0, selected - count + 1)
            rows = candidates[top : top + count]
            for index, note in enumerate(rows, top):
                label = "Turn" if note["turn"] else "Project"
                write(
                    win,
                    index - top + 1,
                    0,
                    f"{label} · {note['body']}",
                    w - 1,
                    curses.A_REVERSE if index == selected else 0,
                )
            win.refresh()
            key = win.getch()
            if key == curses.KEY_MOUSE:
                event = mouse_event()
                if event is None:
                    continue
                x, y, key = event
                if not key and 0 <= x < w - 1:
                    if (y, x) in hits:
                        key = hits[y, x]
                    elif 1 <= y <= len(rows):
                        return rows[y - 1]
            if key == 27:
                return None
            if key in (10, 13):
                return candidates[selected]
            if key in (ord("j"), curses.KEY_DOWN):
                selected = min(selected + 1, len(candidates) - 1)
            if key in (ord("k"), curses.KEY_UP):
                selected = max(0, selected - 1)
    finally:
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


def show(win, api, project, mouse):
    palette = terminal_palette()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if mouse:
        curses.mousemask(CLICK | (WHEEL_UP | WHEEL_DOWN if WHEEL_DOWN else 0))
    win.timeout(300)
    turns, graphs, notes = [], [], []
    selected, detail = None, None
    offset, last, message = 0, 0, ""
    top, project_offset = 0, 0
    visible = [True, True, True]
    before, has_older = "", False
    while True:
        if detail is None and time.monotonic() - last >= 3:
            try:
                batch = api.request(
                    "journey",
                    project=project,
                    limit=PAGE_SIZE + 1,
                    before=before,
                    lanes="true",
                )
                has_older = len(batch) > PAGE_SIZE
                turns = batch[-PAGE_SIZE:]
                if batch and "graph_lanes" not in batch[0]:
                    graphs = list(layout(batch))[-PAGE_SIZE:]
                else:
                    graphs = [row["graph_lanes"] for row in turns]
                notes = api.request("notes", project=project)
                message = ""
            except (OSError, RuntimeError) as e:
                message = str(e)
            last = time.monotonic()
        keys = [(r["thread"], r["turn"]) for r in turns]
        idx = keys.index(selected) if selected in keys else max(0, len(keys) - 1)
        selected = keys[idx] if keys else None
        h, w = win.getmaxyx()
        win.erase()
        write(win, 0, 0, project, w - 1, curses.A_BOLD)
        if h < 8 or w < 25:
            if h > 1:
                write(win, 1, 0, "Enlarge the pane · q quit", w - 1)
            win.refresh()
            if win.getch() == ord("q"):
                return
            continue
        hits, turn_rows, project_rows = {}, {}, {}
        if detail is not None:
            draw_buttons(win, 1, w - 1, [("Close", 27)], hits)
            write(win, 1, 8, "Transcript · j/k scroll · Esc close", w - 9)
            offset = min(offset, max(0, len(detail) - (h - 3)))
            for i, line in enumerate(detail[offset : offset + h - 3]):
                write(win, i + 2, 0, line, w - 1)
        else:
            labels = (
                ("1 Journey", "2 Turn notes", "3 Project notes")
                if w >= 47
                else ("1 Journey", "2 Turn", "3 Project")
                if w >= 34
                else ("1", "2", "3")
            )
            column_keys = (ord("1"), ord("2"), ord("3"))
            draw_buttons(
                win,
                1,
                w - 1,
                zip(labels, column_keys),
                hits,
                active=[key for enabled, key in zip(visible, column_keys) if enabled],
            )
            weights = [2 * visible[0], int(visible[1]), int(visible[2])]
            total = sum(weights)
            split = w * weights[0] // total
            second = w * sum(weights[:2]) // total
            for enabled, (x, label) in zip(
                visible,
                [(0, "Journey"), (split, "Turn notes"), (second, "Project notes")],
            ):
                if not enabled:
                    continue
                write(win, 2, x, label, w - x - 1, curses.A_BOLD)
            height = max(1, (h - 5) // 2)
            top = max(0, idx - height + 1, min(top, idx, len(keys) - height))
            free = [n for n in notes if not n["turn"]]
            max_project_offset = max(0, len(free) - height)
            project_offset = min(project_offset, max_project_offset)
            if visible[2]:
                end = len(free) - project_offset
                for i, n in enumerate(free[max(0, end - height) : end]):
                    y = 3 + 2 * i
                    write(win, y, second, n["body"], w - second - 1)
                    project_rows[y] = n
            lane_count = max(
                (lane + 1 for item in graphs for lane, _, _, _ in item["tracks"]),
                default=0,
            )
            room = max(0, min(split - 8, split // 2))
            step = 2 if 2 * lane_count - 1 <= room else 1
            overflow = 0 < room < lane_count
            lanes = min(lane_count, room - overflow)
            graph_width = max(0, (lanes - 1) * step + 1) + overflow
            text_x = 2 + graph_width
            for i, r in enumerate(turns[top : top + height]):
                y = 3 + 2 * i
                focused = top + i == idx
                pointer = ">" if focused else " "
                style = curses.A_REVERSE if focused else 0
                if visible[0]:
                    write(win, y, 0, pointer, 1, curses.A_BOLD)
                    draw_session_graph(
                        win, y, graphs[top + i], palette, lanes, step, overflow
                    )
                    write(
                        win,
                        y,
                        text_x,
                        f"{r['thread'][-5:]}  {r['prompt']}",
                        split - text_x - 1,
                        style,
                    )
                    status = OBSERVATION.get(r.get("observation"), STATE[r["status"]])
                    write(
                        win,
                        y + 1,
                        text_x,
                        f"{status} · {r['summary']}",
                        split - text_x - 1,
                    )
                attached = [
                    n
                    for n in notes
                    if (n["thread"], n["turn"]) == (r["thread"], r["turn"])
                ]
                write(
                    win,
                    y,
                    split,
                    " / ".join(n["body"] for n in attached),
                    second - split - 1,
                )
                turn_rows[y] = (keys[top + i], attached)
                turn_rows[y + 1] = (keys[top + i], [])
                if not visible[0] and visible[1]:
                    write(
                        win,
                        y + 1,
                        split,
                        f"{pointer} {r['prompt']}",
                        second - split - 1,
                        style,
                    )
            if not turns and visible[0]:
                write(win, 4, 0, "No recorded turns yet", max(1, split - 1))
            action_labels = (
                ("Transcript", "+ Turn note", "+ Project note", "Older", "Latest")
                if w >= 61
                else ("Open", "+ Turn", "+ Project", "<", ">")
                if w >= 38
                else ("Open", "+T", "+P", "<", ">")
            )
            draw_buttons(
                win,
                h - 2,
                w - 1,
                zip(action_labels, (10, ord("m"), ord("n"), ord("b"), ord("g"))),
                hits,
            )
            write(
                win,
                h - 1,
                0,
                message
                or "1–3 columns   j/k move   Enter transcript   m/n add   e edit   b/g older/latest   q quit",
                w - 1,
            )
        win.refresh()
        key = win.getch()
        note_choices = None
        if key == curses.KEY_MOUSE:
            event = mouse_event()
            if event is None:
                continue
            x, y, key = event
            if not (0 <= x < w - 1 and 0 <= y < h):
                continue
            if not key:
                key = hits.get((y, x), 0)
                if detail is None and not key:
                    if visible[2] and x >= second and y in project_rows:
                        key, note_choices = ord("e"), [project_rows[y]]
                    elif x < second and y in turn_rows:
                        selected, attached = turn_rows[y]
                        if x >= split and attached:
                            key, note_choices = ord("e"), attached
            elif detail is None and 3 <= y < h - 2:
                step = 1 if key == curses.KEY_DOWN else -1
                if visible[2] and x >= second:
                    project_offset = max(
                        0, min(project_offset - step, max_project_offset)
                    )
                    continue
                top = max(0, min(top + step, len(keys) - height))
            elif detail is None:
                continue
        if key == ord("q"):
            return
        if detail is not None:
            if key in (27, 10, 13):
                detail, offset = None, 0
            elif key in (ord("j"), curses.KEY_DOWN):
                offset = min(offset + 1, max(0, len(detail) - (h - 3)))
            elif key in (ord("k"), curses.KEY_UP):
                offset = max(0, offset - 1)
            continue
        if key in (ord("1"), ord("2"), ord("3")):
            col = key - ord("1")
            if not visible[col] or sum(visible) > 1:
                visible[col] = not visible[col]
        elif key in (ord("j"), curses.KEY_DOWN) and visible == [False, False, True]:
            project_offset = max(0, project_offset - 1)
        elif key in (ord("k"), curses.KEY_UP) and visible == [False, False, True]:
            project_offset = min(project_offset + 1, max_project_offset)
        elif key in (ord("j"), curses.KEY_DOWN) and keys:
            selected = keys[min(len(keys) - 1, idx + 1)]
        elif key in (ord("k"), curses.KEY_UP) and keys:
            selected = keys[max(0, idx - 1)]
        elif key == ord("b") and has_older and turns:
            first = turns[0]
            before = json.dumps([first["started"], first["turn"], first["thread"]])
            selected, last = None, 0
        elif key == ord("g"):
            before, selected, last = "", None, 0
        elif key in (10, 13, ord("m"), ord("n"), ord("e")):
            try:
                target = (
                    {"thread": selected[0], "turn": selected[1]} if selected else {}
                )
                if key in (10, 13) and selected:
                    items = api.request("transcript", project=project, **target)
                    detail = []
                    for item in items:
                        detail.append("[" + item["type"] + "]")
                        text = item.get("text") or json.dumps(
                            item, ensure_ascii=False, indent=2
                        )
                        for line in text.splitlines():
                            detail.extend(
                                textwrap.wrap(line, max(1, (w - 1) // 2)) or [""]
                            )
                elif key == ord("e"):
                    if note_choices is None:
                        candidates = [
                            n
                            for n in notes
                            if n["turn"] is None or (n["thread"], n["turn"]) == selected
                        ]
                        note = choose_note(win, candidates) if candidates else None
                    else:
                        note = (
                            note_choices[0]
                            if len(note_choices) == 1
                            else choose_note(win, note_choices)
                        )
                    if note:
                        body = edit_body(win, note["body"])
                        if body:
                            api.request(
                                "edit-note",
                                {
                                    "project": project,
                                    "id": note["id"],
                                    "version": note["version"],
                                    "body": body,
                                },
                            )
                            last = 0
                elif key == ord("n") or (key == ord("m") and selected):
                    body = entry(win, "Note · Enter saves · Empty line cancels")
                    if body:
                        api.request(
                            "notes",
                            {"project": project, "id": uuid.uuid4().hex, "body": body}
                            | (target if key == ord("m") else {}),
                        )
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
