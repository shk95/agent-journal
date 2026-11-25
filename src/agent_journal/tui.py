import curses
import json
import time
import unicodedata
import uuid

STATE = {
    "inProgress": "In progress",
    "completed": "Completed",
    "interrupted": "Interrupted",
    "failed": "Failed",
}


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


def entry(win, title):
    h, w = win.getmaxyx()
    win.move(h - 2, 0)
    win.clrtoeol()
    write(win, h - 2, 0, title, w - 1, curses.A_BOLD)
    win.move(h - 1, 0)
    win.clrtoeol()
    win.refresh()
    curses.echo()
    win.timeout(-1)
    try:
        return win.getstr(h - 1, 0, 2048).decode().strip()
    finally:
        curses.noecho()
        win.timeout(300)


def show(win, api, project):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    win.timeout(300)
    turns, notes, selected, detail = [], [], None, None
    offset, last, message = 0, 0, ""
    visible = [True, True, True]
    while True:
        if time.monotonic() - last >= 3:
            try:
                turns = api.request("journey", project=project)
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
        if detail is not None:
            write(win, 1, 0, "Transcript · j/k scroll · Esc close", w - 1)
            for i, line in enumerate(detail[offset : offset + max(1, h - 3)]):
                write(win, i + 2, 0, line, w - 1)
        else:
            weights = [2 * visible[0], int(visible[1]), int(visible[2])]
            total = sum(weights)
            split = w * weights[0] // total
            second = w * sum(weights[:2]) // total
            for enabled, (x, label) in zip(
                visible, [(0, "Journey"), (split, "Turn notes"), (second, "Project notes")]
            ):
                if not enabled:
                    continue
                write(win, 2, x, label, w - x - 1, curses.A_BOLD)
            height = max(1, (h - 5) // 2)
            top = max(0, idx - height + 1)
            free = [n for n in notes if not n["turn"]]
            for i, n in enumerate(free[-height:]):
                write(win, 3 + 2 * i, second, n["body"], w - second - 1)
            for i, r in enumerate(turns[top : top + height]):
                y = 3 + 2 * i
                style = curses.A_REVERSE if top + i == idx else 0
                marker = "⑂" if r["parent"] else "●"
                write(
                    win,
                    y,
                    0,
                    f"{marker} {r['thread'][-5:]}  {r['prompt']}",
                    split - 1,
                    style,
                )
                write(
                    win, y + 1, 2, f"{STATE[r['status']]} · {r['summary']}", split - 3
                )
                attached = [
                    n["body"]
                    for n in notes
                    if (n["thread"], n["turn"]) == (r["thread"], r["turn"])
                ]
                write(win, y, split, " / ".join(attached), second - split - 1)
            if not turns and visible[0]:
                write(win, 4, 0, "No recorded turns yet", max(1, split - 1))
            write(
                win,
                h - 1,
                0,
                message
                or "1/2/3 columns   j/k move   Enter transcript   m turn note   n project note   q quit",
                w - 1,
            )
        win.refresh()
        key = win.getch()
        if key == ord("q"):
            return
        if detail is not None:
            if key in (27, 10, 13):
                detail, offset = None, 0
            elif key in (ord("j"), curses.KEY_DOWN):
                offset = min(offset + 1, max(0, len(detail) - max(1, h - 3)))
            elif key in (ord("k"), curses.KEY_UP):
                offset = max(0, offset - 1)
            continue
        if key in (ord("1"), ord("2"), ord("3")):
            col = key - ord("1")
            if not visible[col] or sum(visible) > 1:
                visible[col] = not visible[col]
        elif key in (ord("j"), curses.KEY_DOWN) and keys:
            selected = keys[min(len(keys) - 1, idx + 1)]
        elif key in (ord("k"), curses.KEY_UP) and keys:
            selected = keys[max(0, idx - 1)]
        elif key in (10, 13, ord("m"), ord("n")):
            try:
                target = (
                    {"thread": selected[0], "turn": selected[1]} if selected else {}
                )
                if key in (10, 13) and selected:
                    items = api.request("transcript", project=project, **target)
                    import textwrap

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


def run(api, project):
    curses.wrapper(show, api, project)
