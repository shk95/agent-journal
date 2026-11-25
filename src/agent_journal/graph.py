"""Draw session lines over a chronological page of journey rows."""


def layout(rows):
    by_key = {(row["thread"], row["turn"]): i for i, row in enumerate(rows)}
    first, last, forks = {}, {}, {}
    for index, row in enumerate(rows):
        session = row["thread"]
        if session not in first:
            first[session] = index
            anchor = by_key.get((row["parent"], row["parent_turn"]))
            if anchor is not None and anchor < index:
                forks[session] = anchor
        last[session] = index

    starts = {session: forks.get(session, index) for session, index in first.items()}
    lane_ends, lanes = [], {}
    for session in sorted(starts, key=starts.get):
        start = starts[session]
        lane = next(
            (i for i, end in enumerate(lane_ends) if end < start), len(lane_ends)
        )
        if lane == len(lane_ends):
            lane_ends.append(last[session])
        else:
            lane_ends[lane] = last[session]
        lanes[session] = lane

    branches = {}
    for session, anchor in forks.items():
        branches.setdefault(anchor, []).append(lanes[session])
    for index, row in enumerate(rows):
        yield {
            "lane": lanes[row["thread"]],
            "tracks": [
                (lane, session, starts[session] < index, index < last[session])
                for session, lane in lanes.items()
                if starts[session] <= index <= last[session]
            ],
            "forks": branches.get(index, []),
        }


def text_graph(row):
    cells = [" "] * (2 * max(track[0] for track in row["tracks"]) + 1)
    for lane, _, _, _ in row["tracks"]:
        cells[lane * 2] = "│"
    point = row["lane"] * 2
    cells[point] = "●"
    for lane in row["forks"]:
        branch = lane * 2
        for i in range(min(point, branch) + 1, max(point, branch)):
            cells[i] = "┼" if cells[i] == "│" else "─"
        cells[branch] = "┐" if point < branch else "┌"
    return "".join(cells).rstrip()


def graph(rows):
    return [text_graph(row) for row in layout(rows)]
